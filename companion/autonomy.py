"""
Continuous Multi-Phase Autonomous Goal Driver, Dynamic `GoalPlan` DAG Executor & Verifier
(`companion/autonomy.py` — A-Frame Application Layer).

Drives Companion's deterministic state machine across software and general technical goals:
1. Step-0 Adaptive Intent Classification & Readiness (`companion.intake` re-exported cleanly)
2. Mechanical phase-scoped `(tool, action)` gating (`DISCOVER`/`DESIGN`/`PLAN` locked read-only)
3. Dynamic `GoalPlan` DAG topological execution with `VerificationSpec` & `R3`/`human_gate` pauses
4. Pluggable `WorkspaceContract` verification (`CompanionSelfContract` vs `GeneralWorkspaceContract`)
5. Post-`VERIFY` `REFLECT` fact compounding into `KnowledgePort`
"""

import ast
import concurrent.futures
import contextvars
import hashlib
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from companion.compaction import build_lossless_phase_handoff, classify_task_autoskip, format_task_carry_forward
from companion.config import settings
from companion.dag_runtime import execute_dag_by_name
from companion.disciplines import (
    FORBIDDEN_EXTERNAL_IMPORTS as FORBIDDEN_STDLIB_VIOLATION_IMPORTS, STUB_PATTERNS,
    iter_ast_import_targets, resolve_workspace_contract, verify_repository_syntax, walk_pruned_files,
)
from companion.intake import PHASE_TIERS, IntentDecision, classify_turn_intent, resolve_workflow_phase_tier, run_http_smoke_probe
from companion.llm import chat, model_gate, tier_context
from companion.memory_store import MemoryStore, get_memory_store
from companion.project_graph import get_project_graph, sync_workflow_to_project_graph
from companion.safety import CheckpointManager, get_active_checkpoint_manager, run_system_doctor, run_unit_tests
from companion.sdd import sync_decisions_md
from companion.skill_forge import distill_workflow_reflection
from companion.streaming import emit_stream_event
from companion.telemetry import telemetry
from companion.tool_schemas import phase_tool_context, r3_approval_context
from companion.workflow_dag import GoalPlan, GoalStep, VerificationSpec, build_goal_plan_from_tasks, evaluate_step_verification
from companion.workflow_graph import WorkflowEngine, workflow_engine

__all__ = [
    "IntentDecision", "PHASE_TIERS", "classify_turn_intent", "resolve_goal_engines", "resolve_workflow_phase_tier",
    "run_autonomous_goal", "run_autonomous_goal_slice", "run_http_smoke_probe", "run_phase_turn", "scan_placeholder_violations",
]


def _snapshot_file_hashes(root: Path) -> Dict[str, str]:
    """Capture SHA-256 hashes of tracked workspace files under `root` to detect workflow mutations."""
    hashes: Dict[str, str] = {}
    tracked_exts = resolve_workspace_contract(root).tracked_extensions
    for item in walk_pruned_files(root):
        if item.suffix.lower() in tracked_exts:
            rel = str(item.relative_to(root)).replace("\\", "/")
            try:
                hashes[rel] = hashlib.sha256(item.read_bytes()).hexdigest()
            except Exception:
                continue
    return hashes


def _changed_paths_since(root: Path, baseline: Dict[str, str]) -> List[Path]:
    """Return paths of files created or modified since `baseline`."""
    current = _snapshot_file_hashes(root)
    return [root / rel for rel, sha in current.items() if baseline.get(rel) != sha]


def scan_placeholder_violations(paths: List[Path], root: Optional[Path] = None) -> List[str]:
    """Scan modified production files for stubs, syntax errors, and (when governed) ADR-001 imports."""
    target_root = (root or settings.active_project_root).resolve()
    contract = resolve_workspace_contract(target_root, is_self_project=settings.get_workspace().is_self_project)
    violations: List[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            rel = str(path.resolve().relative_to(target_root)).replace("\\", "/")
        except ValueError:
            rel = path.name
        top = rel.split("/", 1)[0]
        if top in ("tests", "skills", ".companion_backups") or rel in ("companion/autonomy.py", "companion/safety.py", "companion/disciplines.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() in {".py", ".html", ".js"}:
            for rx, label in STUB_PATTERNS:
                m = rx.search(text)
                if m:
                    violations.append(f"{rel}:{text[: m.start()].count(chr(10)) + 1} contains {label}")
        if path.suffix.lower() == ".py":
            try:
                tree = ast.parse(text, filename=rel)
            except SyntaxError as exc:
                violations.append(f"{rel}:{exc.lineno} SyntaxError: {exc.msg}")
                continue
            if contract.enforce_stdlib_only:
                for target, lineno in iter_ast_import_targets(tree):
                    mod_name = target.split(".")[0]
                    if mod_name.lower() in FORBIDDEN_STDLIB_VIOLATION_IMPORTS:
                        violations.append(f"{rel}:{lineno} imports external package `{mod_name}` (violates ADR-001 stdlib-only invariant)")
    return violations


def resolve_goal_engines(
    store: Optional[MemoryStore] = None, cp_mgr: Optional[CheckpointManager] = None, root: Optional[Path] = None,
) -> Tuple[MemoryStore, Path, CheckpointManager, WorkflowEngine]:
    """Resolve scoped `(MemoryStore, target_root, CheckpointManager, WorkflowEngine)` for autonomous or blueprint runs."""
    mem, target_root = store or get_memory_store(), (root or settings.active_project_root).resolve()
    if cp_mgr is not None:
        eff_cp = cp_mgr
    elif target_root == settings.active_project_root.resolve() and mem.db_path.resolve() != settings.db_path.resolve() and settings.get_workspace().is_self_project:
        eff_cp = CheckpointManager(root=target_root, backup_dir=mem.db_path.parent / ".companion_backups", store=mem)
    elif target_root == settings.active_project_root.resolve():
        eff_cp = get_active_checkpoint_manager()
    else:
        eff_cp = CheckpointManager(root=target_root, backup_dir=target_root / ".companion_backups", store=mem)
    wf = WorkflowEngine(store=mem, cp_manager=eff_cp, root=target_root) if (store is not None or cp_mgr is not None or root is not None or not settings.get_workspace().is_self_project) else workflow_engine
    return mem, target_root, eff_cp, wf


def run_phase_turn(
    phase: str, prompt: str, max_iters: int, chat_fn: Callable[..., Dict[str, Any]], mem: MemoryStore, verbose: bool,
) -> str:
    """Execute one isolated phase turn bound to its mechanical model tier and `AllowedTools` gate."""
    from companion.agent import run_agent_turn

    tier = resolve_workflow_phase_tier(phase)
    emit_stream_event({"type": "phase", "phase": phase, "tier": tier})
    phase_msgs: List[Dict[str, Any]] = [{"role": "user", "content": f"[AUTONOMOUS WORKFLOW PHASE: {phase} | TIER: {tier}]\n{prompt}"}]
    with tier_context(tier), phase_tool_context(phase):
        return run_agent_turn(phase_msgs, max_iters=max_iters, chat_fn=chat_fn, store=mem, session_tag=f"autonomy:{phase.lower()}:{tier}", verbose=verbose, auto_promote=False)


def _verify_task_completion(root: Path, changed_paths: List[Path], run_doctor_tests: bool) -> Tuple[bool, str]:
    """Per-task micro-gate: verify AST/structured syntax, zero stubs, and newly modified test files."""
    syntax_ok, syn_results = verify_repository_syntax(root)
    if not syntax_ok:
        bad = [f"{r.path}: {r.syntax_error}" for r in syn_results if not r.valid_syntax]
        return False, f"SyntaxError in {', '.join(bad[:3])}"
    struct_ok, struct_msg = evaluate_step_verification(VerificationSpec("assertion"), root, "ok", changed_paths)
    if not struct_ok:
        return False, struct_msg
    stubs = scan_placeholder_violations(changed_paths, root=root)
    if stubs:
        return False, f"Stub/Contract violation: {'; '.join(stubs[:3])}"
    if run_doctor_tests:
        for p in changed_paths:
            if p.name.startswith("test_") and p.suffix.lower() == ".py" and p.is_file():
                tres = run_unit_tests(root=root, timeout=30, pattern=p.name)
                if not tres.get("passed"):
                    return False, f"Unit test `{p.name}` failed:\n{str(tres.get('output', ''))[-300:]}"
    return True, f"Verified ({len(changed_paths)} file(s) touched; syntax & contract checks clean)"


def _evaluate_hard_verification(
    root: Path,
    baseline_hashes: Dict[str, str],
    run_doctor_tests: bool,
    goal: str = "",
    goal_plan: Optional[GoalPlan] = None,
) -> Tuple[bool, str]:
    """Run deterministic contract scan + workspace gate (`system_doctor` on Companion core; evidence & tests externally)."""
    changed = _changed_paths_since(root, baseline_hashes)
    stub_issues = scan_placeholder_violations(changed, root=root)
    struct_ok, struct_msg = evaluate_step_verification(VerificationSpec("assertion"), root, "ok", changed)
    if not struct_ok:
        stub_issues.append(struct_msg)
    if goal_plan is not None:
        for st in goal_plan.steps:
            if st.verification.kind in ("evidence_file", "check_command", "script_validator", "domain_validator"):
                ev_ok, ev_msg = evaluate_step_verification(st.verification, root, "ok", changed)
                if ev_ok:
                    st.status = "completed"
                else:
                    stub_issues.append(f"Step `{st.step_id}` verification failed: {ev_msg}")
            elif st.status != "completed":
                stub_issues.append(f"Step `{st.step_id}` ({st.title}) incomplete (`{st.status}`): {st.evidence or 'step did not pass verification'}")

    contract = resolve_workspace_contract(root, is_self_project=settings.get_workspace().is_self_project)
    if contract.enforce_sdd_and_doctor:
        doc = run_system_doctor(root=root, run_tests=run_doctor_tests)
        doc_ok, doc_summary = bool(doc.get("healthy")), str(doc.get("summary", ""))
    else:
        all_files = [p for p in walk_pruned_files(root) if p.name != ".gitignore"]
        has_src = len(all_files) > 0
        has_py = any(p.suffix.lower() == ".py" for p in all_files)
        syn_ok, _ = verify_repository_syntax(root)
        t_res = run_unit_tests(root=root) if run_doctor_tests else {"passed": True, "ran": 1, "output": "Tests skipped."}
        low_goal = goal.lower()
        needs_tests = run_doctor_tests and ("test" in low_goal) and (has_py or any(k in low_goal for k in ("unittest", "unit test", "test_", "pytest")))
        tests_ok = bool(t_res.get("passed")) and (not needs_tests or int(t_res.get("ran", 0)) >= 1)
        doc_ok = (has_src or not run_doctor_tests) and syn_ok and tests_ok
        extra = "" if has_src else "\nFAIL: Zero source files or artifacts created in external workspace — write the requested files with `write_file`."
        if needs_tests and int(t_res.get("ran", 0)) == 0:
            extra += "\nFAIL: Goal requires unit tests, but 0 unit tests were found/executed in `tests/`."
        doc_summary = f"External Workspace Gate (`{root}`): artifacts={'PASS' if has_src else 'FAIL'}, syntax={'PASS' if syn_ok else 'FAIL'}, tests={'PASS' if tests_ok else 'FAIL'}\n{t_res.get('output', '')}{extra}"
    passed = (len(stub_issues) == 0) and doc_ok
    parts = (["Verification / Contract Violations:\n" + "\n".join(f"  - {v}" for v in stub_issues)] if stub_issues else []) + [doc_summary]
    return passed, "\n\n".join(parts)


def _sync_switched_workspace(
    pinned_root: bool, pinned_cp: bool, prev_root: Path, eff_cp: CheckpointManager, wf: WorkflowEngine,
    wf_id: Optional[int], cp_id: str, baseline_hashes: Dict[str, str], goal_plan: GoalPlan, mem: MemoryStore,
) -> Tuple[Path, CheckpointManager, WorkflowEngine, str, Dict[str, str]]:
    """Rebind workflow engines, SQLite `project_id`, and `baseline_hashes` when `project_manage` switches workspaces."""
    ws = settings.get_workspace()
    active_root = ws.project_root.resolve()
    if pinned_root or active_root == prev_root:
        return prev_root, eff_cp, wf, cp_id, baseline_hashes
    new_cp = eff_cp if pinned_cp else CheckpointManager(root=active_root, backup_dir=ws.backup_dir, store=mem)
    new_wf = WorkflowEngine(store=mem, cp_manager=new_cp, root=active_root)
    if wf_id is not None:
        tids = [int(s.task_id) for s in goal_plan.steps if s.task_id]
        mem.rebind_workflow_project(wf_id, ws.project_id, task_ids=tids, checkpoint_id="")
        mem.update_workflow_metadata(wf_id, {"baseline_hashes": {}})
    return active_root, new_cp, new_wf, "", {}


def _execute_single_dag_step(
    step: GoalStep, idx: int, goal: str, design_handoff: str, plan_handoff: str,
    target_root: Path, built_rels: List[str], notes: List[str], run_doctor_tests: bool,
    max_phase_iters: int, chat_fn: Callable[..., Dict[str, Any]], mem: MemoryStore, verbose: bool,
    pinned_root: bool = False,
) -> Tuple[bool, str, Path]:
    """Execute and verify one `GoalStep` inside `IMPLEMENT`."""
    tid = step.task_id or idx
    pure_verify, already_built = classify_task_autoskip(step.title, built_rels, target_root)
    if pure_verify and step.verification.kind == "assertion":
        ok_pre, v_pre = _verify_task_completion(target_root, [], False)
        t_res = run_unit_tests(root=target_root, timeout=30) if run_doctor_tests else {"passed": True, "ran": 0}
        if ok_pre and bool(t_res.get("passed")):
            auto_msg = f"Auto-verified in Python ({v_pre}; ran={t_res.get('ran', 0)} tests PASS)"
            if step.task_id:
                mem.update_task(step.task_id, status="completed", verification_result=auto_msg)
            return True, auto_msg, target_root

    pre_hashes = _snapshot_file_hashes(target_root)
    carry_block = format_task_carry_forward(target_root, built_rels, notes)
    fast_notice = "\n[FAST-PATH NOTICE: Target file(s) for this task already exist on disk from a prior step — inspect briefly and conclude if satisfied.]" if already_built else ""
    contract = resolve_workspace_contract(target_root, is_self_project=settings.get_workspace().is_self_project)
    impl_prompt = (
        f"Overall Goal: {goal}\n\n{carry_block}Lossless Architecture Design Handoff:\n{design_handoff}\n\n"
        f"Lossless Plan Handoff:\n{plan_handoff}\n\nExecute Task #{tid}: {step.title}{fast_notice}\n{contract.impl_rules_prompt}"
    )
    out = run_phase_turn("IMPLEMENT", impl_prompt, min(max_phase_iters, 4) if already_built else max_phase_iters, chat_fn, mem, verbose)
    if not pinned_root and settings.active_project_root.resolve() != target_root:
        target_root = settings.active_project_root.resolve()
        pre_hashes = {}
        built_rels.clear()
    changed = _changed_paths_since(target_root, pre_hashes)
    for ch_p in changed:
        rel_s = str(ch_p.resolve().relative_to(target_root)).replace("\\", "/") if ch_p.resolve().is_relative_to(target_root) else ch_p.name
        if rel_s not in built_rels:
            built_rels.append(rel_s)
    ok_task, v_detail = _verify_task_completion(target_root, changed, run_doctor_tests)
    ok_spec, spec_msg = evaluate_step_verification(step.verification, target_root, out, changed)
    ok_all = ok_task and ok_spec
    combined = f"{v_detail}; {spec_msg}" if ok_all else (v_detail if not ok_task else spec_msg)
    if step.task_id:
        mem.update_task(step.task_id, status="completed" if ok_all else "in_progress", verification_result=f"{combined} | {out[:160]}" if ok_all else f"NEEDS_FIX: {combined}")
    return ok_all, combined, target_root


def _execute_implement_tasks(
    goal: str, design_handoff: str, plan_handoff: str, wf_id: Optional[int], meta: Dict[str, Any],
    target_root: Path, run_doctor_tests: bool, max_phase_iters: int,
    chat_fn: Callable[..., Dict[str, Any]], wf: WorkflowEngine, mem: MemoryStore, verbose: bool,
    pinned_root: bool = False, pinned_cp: bool = False, eff_cp: Optional[CheckpointManager] = None,
    cp_id: str = "", baseline_hashes: Optional[Dict[str, str]] = None, pause_only_human_gate: bool = False,
) -> Tuple[List[str], GoalPlan, str, Path, CheckpointManager, WorkflowEngine, str, Dict[str, str]]:
    """Execute `GoalPlan` DAG steps in topological order, pausing cleanly if an unapproved `human_gate`/`R3` step is reached."""
    raw_dag = meta.get("goal_dag")
    has_saved_dag = isinstance(raw_dag, dict) and bool(raw_dag.get("steps"))
    pending = mem.get_pending_tasks()
    if not pending and not has_saved_dag:
        tid = mem.add_task(f"Implement and verify: {goal[:120]}", priority=3, acceptance_criteria="Passes verification gate with zero stubs.", workflow_id=wf_id)
        pending = [(tid, f"Implement and verify: {goal[:120]}", "pending", 3)]

    goal_plan = GoalPlan.from_dict(raw_dag) if has_saved_dag else build_goal_plan_from_tasks(goal, pending, plan_handoff)
    if not goal_plan.steps:
        goal_plan = build_goal_plan_from_tasks(goal, pending, plan_handoff)

    cur_cp = eff_cp or get_active_checkpoint_manager()
    cur_hashes = baseline_hashes if baseline_hashes is not None else {}
    notes: List[str] = []
    completed_ids: List[int] = list(meta.get("completed_task_ids") or [])
    built_rels: List[str] = list(meta.get("built_rels") or [])
    approved_r3 = list(meta.get("approved_r3_steps") or [])
    legacy_r3 = bool(meta.get("r3_approved")) and not has_saved_dag

    for idx in range(1, min(len(goal_plan.steps), 25) + 1):
        ready = goal_plan.ready_steps()
        if not ready:
            break
        r0_batch = [s for s in ready if s.risk_tier == "R0" and not goal_plan.requires_pause(s, r3_approved=legacy_r3, approved_r3_steps=approved_r3)[0]][:3]
        if len(r0_batch) > 1:
            def _run_r0(st_item: Tuple[int, GoalStep]) -> Tuple[GoalStep, bool, str]:
                s_offset, s_obj = st_item
                ok_s, msg_s, _ = _execute_single_dag_step(s_obj, idx + s_offset, goal, design_handoff, plan_handoff, target_root, list(built_rels), list(notes), run_doctor_tests, max_phase_iters, chat_fn, mem, verbose, pinned_root=True)
                return s_obj, ok_s, msg_s

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(r0_batch)) as pool:
                futs = [pool.submit(contextvars.copy_context().run, _run_r0, (off, s_obj)) for off, s_obj in enumerate(r0_batch)]
                results = [f.result() for f in futs]
            for s_obj, ok_s, msg_s in results:
                s_obj.status = "completed" if ok_s else "blocked"
                s_obj.evidence = msg_s[:240]
                if ok_s and s_obj.task_id and s_obj.task_id not in completed_ids:
                    completed_ids.append(s_obj.task_id)
                notes.append(f"Step `{s_obj.step_id}` [{'VERIFIED' if ok_s else 'NEEDS_FIX'}]: {s_obj.title}" + ("" if ok_s else f" ({msg_s})"))
            if wf_id is not None:
                mem.update_workflow_metadata(wf_id, {"goal_dag": goal_plan.to_dict(), "completed_task_ids": completed_ids, "built_rels": built_rels})
            continue

        step = ready[0]
        if wf_id is not None:
            sync_workflow_to_project_graph(mem, wf_id, goal, "IMPLEMENT")
        must_pause, pause_reason = goal_plan.requires_pause(step, r3_approved=legacy_r3, approved_r3_steps=approved_r3)
        if must_pause:
            if pause_only_human_gate and step.verification.kind != "human_gate":
                break
            if wf_id is not None:
                mem.update_workflow_metadata(wf_id, {"goal_dag": goal_plan.to_dict(), "built_rels": built_rels, "awaiting_plan_approval": True, "pause_reason": pause_reason})
            wf.transition("PLAN", f"Paused at DAG approval gate: {pause_reason[:120]}", run_tests_on_verify=False)
            return notes, goal_plan, pause_reason, target_root, cur_cp, wf, cp_id, cur_hashes

        prev_root = target_root
        step_r3_ok = legacy_r3 or (step.step_id in set(approved_r3))
        with r3_approval_context(step_r3_ok):
            ok_step, ev_msg, _ = _execute_single_dag_step(step, idx, goal, design_handoff, plan_handoff, target_root, built_rels, notes, run_doctor_tests, max_phase_iters, chat_fn, mem, verbose, pinned_root=pinned_root)
        target_root, cur_cp, wf, cp_id, cur_hashes = _sync_switched_workspace(pinned_root, pinned_cp, prev_root, cur_cp, wf, wf_id, cp_id, cur_hashes, goal_plan, mem)
        step.status = "completed" if ok_step else "blocked"
        step.evidence = ev_msg[:240]
        if ok_step and step.task_id and step.task_id not in completed_ids:
            completed_ids.append(step.task_id)
        tag = "AUTO-VERIFIED" if "Auto-verified" in ev_msg else ("VERIFIED" if ok_step else "NEEDS_FIX")
        notes.append(f"Step `{step.step_id}` [{tag}]: {step.title}" + ("" if ok_step else f" ({ev_msg})"))
        if wf_id is not None:
            mem.update_workflow_metadata(wf_id, {"goal_dag": goal_plan.to_dict(), "completed_task_ids": completed_ids, "built_rels": built_rels})
    return notes, goal_plan, "", target_root, cur_cp, wf, cp_id, cur_hashes


def _ensure_wf_state(wf: WorkflowEngine, mem: MemoryStore, target_state: str, summary: str) -> None:
    active = mem.get_active_workflow()
    if active and str(active.get("current_state")) == target_state:
        return
    wf.transition(target_state, summary, run_tests_on_verify=False)


def _prepare_workflow_start_or_resume(
    clean_goal: str, workflow_type: str, resume: bool, wf: WorkflowEngine, mem: MemoryStore,
) -> Tuple[Optional[int], str, str, Dict[str, Any], Optional[Dict[str, Any]]]:
    """Start a fresh workflow or restore an interrupted workflow when `resume=True`."""
    if not resume:
        start_res = wf.start_workflow(goal=clean_goal, workflow_type=workflow_type, acceptance_criteria="All verification gates pass with zero placeholder stubs.")
        return start_res.workflow_id, start_res.checkpoint_id or "", clean_goal, {}, None
    saved = mem.get_resumable_workflow()
    if not saved:
        return None, "", "", {}, {"status": "error", "passed": False, "summary": "No interrupted or active autonomous workflow found to resume."}
    wf_id, cp_id, meta = int(saved["id"]), str(saved.get("checkpoint_id") or ""), dict(saved.get("metadata") or {})
    restored_goal = str(saved.get("goal") or clean_goal)
    if meta.get("last_revision") and str(meta["last_revision"]) not in restored_goal:
        restored_goal = f"{restored_goal}\nRevision requirement: {meta['last_revision']}"
    curr = str(saved.get("current_state") or "INTAKE")
    if saved.get("status") != "active" or curr in ("ABORTED", "COMPLETE", "IDLE"):
        reentry = "PLAN" if meta.get("plan_handoff") else ("DESIGN" if meta.get("design_handoff") else ("DISCOVER" if meta.get("discovery_handoff") else "INTAKE"))
        mem.update_workflow_state(wf_id, curr, reentry, "Resumed interrupted autonomous workflow", status="active")
    return wf_id, cp_id, restored_goal, meta, None


def _finalize_autonomous_goal(
    passed: bool, clean_goal: str, wf_id: Optional[int], cp_id: str, recoveries_used: int, max_recoveries: int,
    impl_count: int, gate_report: str, elapsed_s: float, rollback_on_failure: bool, target_root: Path,
    baseline_hashes: Dict[str, str], mem: MemoryStore, eff_cp: CheckpointManager, wf: WorkflowEngine, auto_git_commit: bool = False,
) -> Dict[str, Any]:
    """Finalize workflow state, `REFLECT` fact compounding, optional rollback/Git save-point, and summary."""
    if not passed:
        rb_note = ""
        if rollback_on_failure and cp_id:
            rb = eff_cp.rollback_checkpoint(cp_id, reason=f"Autonomous goal failed verification: {clean_goal[:100]}")
            rb_note = f" Workspace rolled back to `{cp_id}` ({rb.get('restored_count', 0)} files restored)."
        wf.transition("ABORTED", f"Verification failed after {recoveries_used} recovery attempt(s).{rb_note}", run_tests_on_verify=False)
        telemetry.log("WARN", "autonomy", f"Autonomous goal aborted ({elapsed_s}s): {clean_goal[:100]}")
        summary = (
            f"=== Autonomous Goal Execution: FAILED ({elapsed_s}s) ===\n"
            f"Goal: {clean_goal}\nRecoveries Attempted: {recoveries_used}/{max_recoveries}.{rb_note}\n\n"
            f"Ground-Truth Verification Report:\n{gate_report}"
        )
        return {"status": "FAILED", "passed": False, "workflow_id": wf_id, "checkpoint_id": cp_id, "recoveries": recoveries_used, "summary": summary}

    wf.transition("REFLECT", "All verification gates and contract checks passed", run_tests_on_verify=False)
    for ip_task in mem.get_all_tasks(status_filter="in_progress"):
        mem.update_task(int(ip_task["id"]), status="completed", verification_result=f"Verified by Workflow #{wf_id} final verification gate")
    distill_workflow_reflection(clean_goal, wf_id, _changed_paths_since(target_root, baseline_hashes), target_root, mem)
    if target_root == settings.root.resolve():
        sync_decisions_md(store=mem, root=target_root)
        mem.sync_memory_md()
    git_res = eff_cp.create_git_savepoint(f"Complete Workflow #{wf_id}: {clean_goal[:72]}", init_if_needed=(target_root != settings.root.resolve())) if auto_git_commit else {}
    git_line = f"\nGit Save-Point: {git_res['summary']}" if git_res.get("summary") else ""
    wf.transition("COMPLETE", f"Autonomous goal verified and completed in {elapsed_s}s", run_tests_on_verify=False)
    if wf_id is not None:
        sync_workflow_to_project_graph(mem, wf_id, clean_goal, "COMPLETE")
    telemetry.log("INFO", "autonomy", f"Autonomous goal completed ({elapsed_s}s): {clean_goal[:100]}")
    summary = (
        f"=== Autonomous Goal Execution: COMPLETE ({elapsed_s}s) ===\n"
        f"Goal: {clean_goal}\nWorkflow #{wf_id} | Checkpoint: `{cp_id}` | Recoveries Used: {recoveries_used}\n"
        f"Tasks Processed: {impl_count}{git_line}\n\n{gate_report}"
    )
    return {"status": "COMPLETE", "passed": True, "workflow_id": wf_id, "checkpoint_id": cp_id, "git_commit": git_res.get("commit", ""), "recoveries": recoveries_used, "summary": summary}


def _run_planning_phases(
    clean_goal: str, wf_id: Optional[int], meta: Dict[str, Any], target_root: Path,
    max_phase_iters: int, chat_fn: Callable[..., Dict[str, Any]], wf: WorkflowEngine, mem: MemoryStore, verbose: bool,
) -> Tuple[str, str, str]:
    """Execute or reuse lossless `DISCOVER -> DESIGN -> PLAN` handoffs and build `GoalPlan` DAG."""
    half = max(4, max_phase_iters // 2)
    contract = resolve_workspace_contract(target_root, is_self_project=settings.get_workspace().is_self_project)
    disc = str(meta.get("discovery_handoff") or "")
    if not disc:
        _ensure_wf_state(wf, mem, "DISCOVER", "Autonomous DISCOVER phase started")
        raw = run_phase_turn("DISCOVER", f"Goal: {clean_goal}\nInspect relevant workspace files, configs, specs, and tests. Do NOT edit files; return a grounded impact summary with exact file paths.", half, chat_fn, mem, verbose)
        disc = build_lossless_phase_handoff("DISCOVER", raw, max_chars=5000)
        if wf_id is not None:
            mem.update_workflow_metadata(wf_id, {"discovery_handoff": disc})

    design = str(meta.get("design_handoff") or "")
    if not design:
        _ensure_wf_state(wf, mem, "DESIGN", "Discovery complete; entering reasoning DESIGN phase")
        rule_hint = "100% Python stdlib per ADR-001, <=550L/module, <=100L/fn, SDD sync" if contract.enforce_sdd_and_doctor else "clean modular design, explicit verification evidence"
        raw = run_phase_turn("DESIGN", f"Goal: {clean_goal}\nLossless Discovery Findings:\n{disc}\n\nKeep internal <think> reasoning brief (<200 words). Produce a concise technical design (<400 words; {rule_hint}) directly from the Discovery Findings.", 1, chat_fn, mem, verbose)
        design = build_lossless_phase_handoff("DESIGN", raw, max_chars=5000)
        if wf_id is not None:
            mem.update_workflow_metadata(wf_id, {"design_handoff": design})

    plan = str(meta.get("plan_handoff") or "")
    if not plan:
        _ensure_wf_state(wf, mem, "PLAN", "Design complete; breaking down atomic tasks")
        dag_hint = (
            ' (Optional: include a fenced ```json {"steps": [{"step_id": "s1", "title": "...", "depends_on": [], '
            '"risk_tier": "R1", "verification": {"kind": "evidence_file", "target": "..."}}]} ``` block when dependencies or evidence gates apply.)'
        )
        raw = run_phase_turn("PLAN", f"Goal: {clean_goal}\nLossless Design:\n{design}\n\nKeep internal <think> reasoning brief (<200 words). Batch 1-4 atomic `task_manage(action='add_task', ...)` calls in a single step, then summarize the execution plan concisely in plain English. File writes are locked in PLAN.{dag_hint}", min(half, 3), chat_fn, mem, verbose)
        plan = build_lossless_phase_handoff("PLAN", raw, max_chars=4500)
        dag = build_goal_plan_from_tasks(clean_goal, mem.get_pending_tasks(), plan)
        meta["goal_dag"] = dag.to_dict()
        if wf_id is not None:
            mem.update_workflow_metadata(wf_id, {"plan_handoff": plan, "goal_dag": meta["goal_dag"]})
    get_project_graph(mem).seed_dikw_goal_milestones(settings.get_workspace().project_id)
    if wf_id is not None:
        sync_workflow_to_project_graph(mem, wf_id, clean_goal, "PLAN")
    return disc, design, plan


def _reconcile_recovered_steps(
    goal_plan: GoalPlan, root: Path, rec_out: str, baseline_hashes: Dict[str, str], mem: MemoryStore,
) -> None:
    """Mark previously blocked DAG steps completed when a `RECOVER` turn resolves their verification."""
    changed = _changed_paths_since(root, baseline_hashes)
    for st in goal_plan.steps:
        if st.status == "blocked" and st.verification.kind != "human_gate":
            ev_ok, ev_msg = evaluate_step_verification(st.verification, root, rec_out, changed)
            if ev_ok:
                st.status = "completed"
                st.evidence = ev_msg[:240]
                if st.task_id:
                    mem.update_task(st.task_id, status="completed", verification_result=f"Recovered: {ev_msg[:180]}")


def run_autonomous_goal(
    goal: str, workflow_type: str = "engineering_feature", max_phase_iters: int = 10, max_recoveries: int = 2,
    chat_fn: Callable[..., Dict[str, Any]] = chat, store: Optional[MemoryStore] = None, cp_mgr: Optional[CheckpointManager] = None,
    root: Optional[Path] = None, run_doctor_tests: bool = True, rollback_on_failure: bool = True,
    verbose: bool = True, resume: bool = False, pause_at_plan: bool = False, auto_git_commit: Optional[bool] = None,
) -> Dict[str, Any]:
    """Execute, pause-at-plan (`Blueprint`), or resume a multi-phase DAG workflow."""
    raw_goal = (goal or "").strip()
    is_resume = resume or raw_goal.lower() in ("resume", "continue", "approve")
    if not raw_goal and not is_resume:
        return {"status": "error", "passed": False, "summary": "error: goal is required for autonomous execution."}

    pinned_root, pinned_cp = (root is not None), (cp_mgr is not None)
    mem, target_root, eff_cp, wf = resolve_goal_engines(store, cp_mgr, root)
    t0 = time.perf_counter()

    with model_gate.foreground_turn():
        wf_id, cp_id, clean_goal, meta, resume_err = _prepare_workflow_start_or_resume(raw_goal, workflow_type, is_resume, wf, mem)
        if resume_err is not None:
            return resume_err
        saved_hashes = meta.get("baseline_hashes")
        if is_resume and isinstance(saved_hashes, dict):
            baseline_hashes = {str(k): str(v) for k, v in saved_hashes.items()}
        else:
            baseline_hashes = _snapshot_file_hashes(target_root)
            if wf_id is not None:
                mem.update_workflow_metadata(wf_id, {"baseline_hashes": baseline_hashes})

        _, design_handoff, plan_handoff = _run_planning_phases(clean_goal, wf_id, meta, target_root, max_phase_iters, chat_fn, wf, mem, verbose)
        if pause_at_plan:
            if wf_id is not None:
                mem.update_workflow_metadata(wf_id, {"awaiting_plan_approval": True})
            return {"status": "AWAITING_APPROVAL", "passed": True, "awaiting_approval": True, "workflow_id": wf_id, "checkpoint_id": cp_id, "goal": clean_goal, "design_handoff": design_handoff, "plan_handoff": plan_handoff, "summary": plan_handoff}

        if wf_id is not None and meta.get("awaiting_plan_approval"):
            mem.update_workflow_metadata(wf_id, {"awaiting_plan_approval": False})
        _ensure_wf_state(wf, mem, "IMPLEMENT", "Plan locked; executing GoalPlan DAG steps")
        impl_notes, goal_plan, pause_reason, target_root, eff_cp, wf, cp_id, baseline_hashes = _execute_implement_tasks(
            clean_goal, design_handoff, plan_handoff, wf_id, meta, target_root, run_doctor_tests, max_phase_iters,
            chat_fn, wf, mem, verbose, pinned_root=pinned_root, pinned_cp=pinned_cp, eff_cp=eff_cp, cp_id=cp_id, baseline_hashes=baseline_hashes,
        )
        if pause_reason:
            return {
                "status": "AWAITING_APPROVAL", "passed": True, "awaiting_approval": True,
                "workflow_id": wf_id, "checkpoint_id": cp_id, "goal": clean_goal, "pause_reason": pause_reason,
                "summary": f"=== Workflow Paused for User Approval (`PLAN`) ===\n{pause_reason}\nReply `approve` or run `/approve` to authorize and continue.",
            }

        _ensure_wf_state(wf, mem, "VERIFY", "Implementation steps finished; running hard verification gate")
        if wf_id is not None:
            sync_workflow_to_project_graph(mem, wf_id, clean_goal, "VERIFY")
        passed, gate_report = _evaluate_hard_verification(target_root, baseline_hashes, run_doctor_tests, clean_goal, goal_plan)
        recoveries_used = 0
        while not passed and recoveries_used < max_recoveries:
            recoveries_used += 1
            wf.recover(reason=f"Hard verification failed (attempt {recoveries_used}): {gate_report[:240]}", rollback=False)
            rec_report = build_lossless_phase_handoff("VERIFY_FAILURE", gate_report, max_chars=4500)
            prev_rec_root = target_root
            rec_out = run_phase_turn("RECOVER", f"Goal: {clean_goal}\nHard Verification Gate FAILED (Recovery {recoveries_used}/{max_recoveries}):\n{rec_report}\n\nApply OODA Loop: fix all failing checks and missing artifacts directly.", max_phase_iters, chat_fn, mem, verbose)
            target_root, eff_cp, wf, cp_id, baseline_hashes = _sync_switched_workspace(pinned_root, pinned_cp, prev_rec_root, eff_cp, wf, wf_id, cp_id, baseline_hashes, goal_plan, mem)
            _reconcile_recovered_steps(goal_plan, target_root, rec_out, baseline_hashes, mem)
            nxt = goal_plan.ready_steps()
            if nxt:
                meta["goal_dag"] = goal_plan.to_dict()
                _ensure_wf_state(wf, mem, "IMPLEMENT", f"Executing unlocked DAG steps after RECOVER {recoveries_used}")
                more, goal_plan, rec_pause, target_root, eff_cp, wf, cp_id, baseline_hashes = _execute_implement_tasks(
                    clean_goal, design_handoff, plan_handoff, wf_id, meta, target_root, run_doctor_tests, max_phase_iters,
                    chat_fn, wf, mem, verbose, pinned_root=pinned_root, pinned_cp=pinned_cp, eff_cp=eff_cp, cp_id=cp_id,
                    baseline_hashes=baseline_hashes, pause_only_human_gate=True,
                )
                impl_notes.extend(more)
                if rec_pause:
                    return {
                        "status": "AWAITING_APPROVAL", "passed": True, "awaiting_approval": True,
                        "workflow_id": wf_id, "checkpoint_id": cp_id, "goal": clean_goal, "pause_reason": rec_pause,
                        "summary": f"=== Workflow Paused for User Approval (`PLAN`) ===\n{rec_pause}\nReply `approve` or run `/approve` to authorize and continue.",
                    }
            if wf_id is not None:
                mem.update_workflow_metadata(wf_id, {"goal_dag": goal_plan.to_dict()})
            _ensure_wf_state(wf, mem, "VERIFY", f"Re-verifying after RECOVER attempt {recoveries_used}")
            passed, gate_report = _evaluate_hard_verification(target_root, baseline_hashes, run_doctor_tests, clean_goal, goal_plan)

        eff_git = (settings.auto_git_commit and chat_fn is chat) if auto_git_commit is None else bool(auto_git_commit)
        return _finalize_autonomous_goal(
            passed, clean_goal, wf_id, cp_id, recoveries_used, max_recoveries,
            len(impl_notes), gate_report, round(time.perf_counter() - t0, 1),
            rollback_on_failure, target_root, baseline_hashes, mem, eff_cp, wf, auto_git_commit=eff_git,
        )


def run_autonomous_goal_slice(
    project_id: Optional[str] = None,
    chat_fn: Callable[..., Dict[str, Any]] = chat,
    store: Optional[MemoryStore] = None,
    root: Optional[Path] = None,
    max_iters: int = 6,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Execute a single unblocked `companion`/`collaborative` work-item slice on the Universal Project & Goal Graph."""
    mem, target_root, eff_cp, _ = resolve_goal_engines(store, None, root)
    mgr = get_project_graph(mem)
    ready = mgr.next_ready_companion_items(project_id=project_id, limit=1)
    if not ready:
        return {"ok": True, "ran": False, "status": "idle", "summary": "No ready companion work items."}

    item = ready[0]
    pid = str(item["project_id"])
    proj = mgr.get_project(pid)
    cp_id = eff_cp.create_checkpoint(label=f"slice_{item['item_id']}")["checkpoint_id"]
    pre_hashes = _snapshot_file_hashes(target_root)
    mgr.update_item(item["item_id"], status="in_progress", checkpoint_id=cp_id)

    if item.get("dag_name"):
        dag_res = execute_dag_by_name(
            str(item["dag_name"]),
            params={"goal": item["title"], "item_id": item["item_id"], "project_id": pid},
            store=mem,
            root=target_root,
        )
        if dag_res.get("status") == "waiting_approval":
            summary = str(dag_res.get("summary") or f"Paused awaiting approval (`{dag_res.get('approval_id')}`).")
            mgr.update_item(item["item_id"], status="waiting_approval", checkpoint_id=cp_id, verification_result=summary[:240])
            mem.log_autonomy_run(pid, summary, entry_kind="goal_slice", status="waiting_approval", details={"item_id": item["item_id"], "approval_id": dag_res.get("approval_id", "")})
            return {"ok": True, "ran": True, "status": "waiting_approval", "project_id": pid, "item_id": item["item_id"], "approval_id": dag_res.get("approval_id", ""), "summary": summary}
        out = str(dag_res.get("summary") or "")
        exec_ok = bool(dag_res.get("ok"))
    else:
        prompt = (
            f"Project / Goal: {proj['title']} ({proj['goal_summary']})\n"
            f"Prerequisite Stage: {item['stage_kind']}\n"
            f"Execute Work Item `{item['item_id']}`: {item['title']}\n"
            f"Description: {item['description']}\n"
            f"Acceptance Criteria: {item['acceptance_criteria'] or 'Complete cleanly with grounded verification.'}"
        )
        out = run_phase_turn("IMPLEMENT", prompt, max_iters, chat_fn, mem, verbose)
        exec_ok = True

    changed = _changed_paths_since(target_root, pre_hashes)
    v_dict = item.get("verification") or {}
    vspec = VerificationSpec.from_dict(v_dict) if v_dict else VerificationSpec("assertion")
    ok_ver, ver_msg = evaluate_step_verification(vspec, target_root, out, changed)
    ok_task, task_msg = _verify_task_completion(target_root, changed, run_doctor_tests=False)
    passed = exec_ok and ok_ver and ok_task
    new_status = "done" if passed else "blocked"
    detail_msg = f"{ver_msg}; {task_msg}" if passed else f"NEEDS_FIX: {ver_msg if not ok_ver else task_msg}"
    updated_item = mgr.update_item(item["item_id"], status=new_status, checkpoint_id=cp_id, verification_result=f"{detail_msg} | {out[:140]}"[:320])
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    mgr.update_project(pid, metadata={"last_slice_at": now_iso, "last_slice_item": item["item_id"]})
    summary = f"Goal slice `{item['item_id']}` ({item['title'][:80]}) -> {new_status.upper()}: {detail_msg[:160]}"
    mem.log_autonomy_run(pid, new_status, summary, checkpoint_id=cp_id, doctor_passed=passed, entry_kind="goal_slice")
    return {"ok": passed, "ran": True, "status": new_status, "project_id": pid, "item": updated_item, "checkpoint_id": cp_id, "summary": summary}
