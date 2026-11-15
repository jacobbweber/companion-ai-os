"""
Interactive "Plan -> Align -> Approve -> Build" Blueprint Gate (`companion/blueprint.py` — A-Frame Application Layer).

Gives product & engineering partners a stateful plain-English steering wheel:
1. `create_or_revise_blueprint(...)` runs `INTAKE -> DISCOVER -> DESIGN -> PLAN` while file writes stay
   mechanically locked (`AllowedTools`), saves lossless handoffs + `GoalPlan` DAG, and renders a Blueprint Card.
2. `handle_blueprint_conversation_turn(...)` statefully records conversational Q&A / alignment answers into
   `KnowledgePort` and `workflow_runs` metadata, updating `DESIGN`, `PLAN`, `goal_dag`, and tasks before `/approve`.
3. `approve_blueprint_and_build(...)` unlocks approved Blueprints, satisfies pending `human_gate` / `R3` gates,
   and drives `IMPLEMENT -> VERIFY -> REFLECT -> COMPLETE`.
"""

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from companion.autonomy import resolve_goal_engines, run_autonomous_goal, run_phase_turn
from companion.compaction import build_lossless_phase_handoff
from companion.config import settings
from companion.disciplines import resolve_workspace_contract
from companion.grounding import get_knowledge_port
from companion.llm import chat, model_gate
from companion.memory_store import MemoryStore, get_memory_store
from companion.project_graph import sync_workflow_to_project_graph
from companion.safety import CheckpointManager
from companion.telemetry import telemetry
from companion.workflow_dag import GoalPlan, build_goal_plan_from_tasks

_APPROVE_CLAUSE = (
    r"(?:yes|yep|sure|ok|okay|approve|approved|looks\s+(?:good|great|solid)|"
    r"go\s+ahead(?:\s+and\s+build(?:\s+it)?)?|proceed(?:\s+with\s+(?:the\s+)?(?:plan|build))?|"
    r"ship\s+it|build\s+it|green\s*light|let'?s\s+(?:do|build)\s+it|lgtm)"
)
_APPROVE_REPLY_RE = re.compile(rf"^\s*{_APPROVE_CLAUSE}(?:[,;!\s]+{_APPROVE_CLAUSE})*\s*[!.]*\s*$", re.I)
_ABORT_REPLY_RE = re.compile(
    r"^\s*(?:cancel|abort|discard|scrap|stop)\s+(?:the\s+)?(?:plan|blueprint|workflow|build)\s*[!.]*\s*$",
    re.I,
)
_DIRECTIVE_REVISION_RE = re.compile(
    r"\b(?:use|bind|set|change|update|add|remove|drop|switch|instead|make\s+it|revise|include|replace|target|port\s+\d+|rename|split)\b",
    re.I,
)
_ANALYTICAL_QUESTION_START_RE = re.compile(
    r"^\s*(?:why|how|what|where|when|which|who|does|do|did|is|are|will|would\s+this|would\s+that|would\s+it|explain)\b",
    re.I,
)
_POLITE_DIRECTIVE_QUESTION_RE = re.compile(
    r"^\s*(?:(?:can|could|should|would|shall|may|let'?s)\s+(?:you|we)\s+(?:please\s+|instead\s+)?|why\s+not\s+|what\s+if\s+we\s+)"
    r"(?:use|bind|set|change|update|add|remove|drop|switch|make|revise|include|replace|target|rename|split)\b",
    re.I,
)
_EMBEDDED_DIRECTIVE_RE = re.compile(
    r"(?:;\s*|\.\s+)(?:please\s+|can\s+(?:you|we)\s+|could\s+(?:you|we)\s+|let'?s\s+)?(?:use|change|update|add|remove|switch|make|revise|replace)\b",
    re.I,
)
_PLAN_DAG_HINT = (
    ' (Optional: include a fenced ```json {"steps": [{"step_id": "s1", "title": "...", "depends_on": [], '
    '"risk_tier": "R1", "verification": {"kind": "evidence_file", "target": "..."}}]} ``` block when dependencies or evidence gates apply.)'
)


def is_pure_blueprint_question(user_text: str) -> bool:
    """Return `True` when a paused-Blueprint reply is a pure clarifying question rather than a directive plan change."""
    clean = (user_text or "").strip()
    if not clean.endswith("?"):
        return False
    if _POLITE_DIRECTIVE_QUESTION_RE.search(clean) or _EMBEDDED_DIRECTIVE_RE.search(clean):
        return False
    if _ANALYTICAL_QUESTION_START_RE.search(clean):
        return True
    return not _DIRECTIVE_REVISION_RE.search(clean)


def is_awaiting_blueprint_approval(store: Optional[MemoryStore] = None) -> bool:
    """Return `True` if an active workflow is paused at `PLAN` awaiting user Blueprint/DAG approval."""
    mem = store or get_memory_store()
    active = mem.get_active_workflow()
    if not active or str(active.get("current_state")) != "PLAN":
        return False
    return bool((active.get("metadata") or {}).get("awaiting_plan_approval"))


def classify_blueprint_reply(user_text: str) -> Optional[str]:
    """Detect unambiguous natural-language approval (`'APPROVE'`) or cancellation (`'ABORT'`) for an active Blueprint."""
    clean = (user_text or "").strip()
    if not clean or clean.startswith(("/", ".")):
        return None
    if _APPROVE_REPLY_RE.match(clean):
        return "APPROVE"
    if _ABORT_REPLY_RE.match(clean):
        return "ABORT"
    return None


def format_blueprint_card(
    wf_id: Optional[int],
    goal: str,
    cp_id: str,
    design_handoff: str,
    plan_handoff: str,
    pending_tasks: List[Tuple[int, str, str, int]],
    revised: bool = False,
    goal_dag: Optional[Dict[str, Any]] = None,
    pause_reason: str = "",
) -> str:
    """Render a clear, plain-English Blueprint review card with tasks/DAG steps and 1-step next actions."""
    heading = "Revised Blueprint Ready for Your Review" if revised else "Blueprint Ready for Your Review"
    dag_steps = GoalPlan.from_dict(goal_dag).steps if isinstance(goal_dag, dict) and goal_dag.get("steps") else []
    if pending_tasks:
        task_lines = [f"  {idx}. **[Task #{tid} | P{prio}]** {desc}" for idx, (tid, desc, _st, prio) in enumerate(pending_tasks, 1)]
        gated = [f"`{s.step_id}` ({s.risk_tier}/{s.verification.kind})" for s in dag_steps if s.risk_tier == "R3" or s.verification.kind == "human_gate"]
        if gated:
            task_lines.append(f"  * Approval Gates in DAG: {', '.join(gated)}")
    elif dag_steps:
        task_lines = [
            f"  {idx}. **[Step `{s.step_id}` | {s.risk_tier} | `{s.verification.kind}`]** {s.title}"
            for idx, s in enumerate(dag_steps, 1)
        ]
    else:
        task_lines = ["  1. Implement & verify requested deliverable against acceptance gates."]
    design_excerpt = (design_handoff or "").strip()[:1400]
    plan_excerpt = (plan_handoff or "").strip()[:1600]
    gate_banner = f"**Active Approval Gate**: {pause_reason}\n" if pause_reason else ""
    return (
        f"=== {heading} (Workflow #{wf_id} | Checkpoint `{cp_id or 'ready'}`) ===\n"
        f"**Goal**: {goal}\n"
        f"{gate_banner}"
        "**Safety Lock**: File-writing tools remain **LOCKED** (`PLAN` phase) until you approve.\n\n"
        f"### 1. Proposed Architecture & Approach\n{design_excerpt}\n\n"
        f"### 2. Execution Summary\n{plan_excerpt}\n\n"
        "### 3. Step-by-Step Build Checklist\n"
        + "\n".join(task_lines)
        + "\n\n### Your Steering Options\n"
        "- **Approve & Build**: Reply `approve` or run `/approve` (or click **Approve & Build** in the Web UI)\n"
        "- **Clarify or Tweak**: Reply in plain English with answers/adjustments or run `/plan revise <feedback>`\n"
        "- **Discard Plan**: Run `/workflow abort`"
    )


def _revise_existing_blueprint(
    feedback: str,
    max_phase_iters: int,
    chat_fn: Callable[..., Dict[str, Any]],
    mem: MemoryStore,
    cp_mgr: Optional[CheckpointManager],
    root: Optional[Path],
    verbose: bool,
) -> Dict[str, Any]:
    """Incorporate user feedback/answers into an existing `PLAN` workflow without losing `DISCOVER` context."""
    active = mem.get_active_workflow()
    if not active:
        return {"status": "error", "passed": False, "awaiting_approval": False, "summary": "No active Blueprint workflow found to revise. Start one with `/plan <objective>`."}
    wf_id = int(active["id"])
    goal = str(active.get("goal") or "")
    cp_id = str(active.get("checkpoint_id") or "")
    meta = dict(active.get("metadata") or {})
    disc_handoff = str(meta.get("discovery_handoff") or "")
    prev_design = str(meta.get("design_handoff") or "")
    alignment_notes = [*list(meta.get("alignment_notes") or []), feedback]
    combined_feedback = "; ".join(alignment_notes)
    _, target_root, _, wf = resolve_goal_engines(mem, cp_mgr, root)
    contract = resolve_workspace_contract(target_root, is_self_project=settings.get_workspace().is_self_project and target_root == settings.root.resolve())

    for t in mem.get_all_tasks(status_filter="pending"):
        if t.get("workflow_id") == wf_id:
            mem.update_task(int(t["id"]), status="cancelled", verification_result="Superseded by Blueprint revision")

    half = max(4, max_phase_iters // 2)
    rule_hint = "100% Python stdlib per ADR-001" if contract.enforce_stdlib_only else "clean modular design & explicit verification"
    with model_gate.foreground_turn():
        mem.update_workflow_state(wf_id, str(active.get("current_state") or "PLAN"), "DESIGN", f"Revising Blueprint design per feedback: {feedback[:80]}", status="active")
        design_raw = run_phase_turn(
            "DESIGN",
            f"Goal: {goal}\nAccumulated User Alignment & Feedback: {combined_feedback}\nPrior Design:\n{prev_design}\nDiscovery Context:\n{disc_handoff}\n\n"
            f"Keep internal <think> reasoning brief (<200 words). Update the technical design concisely (<400 words; {rule_hint}) to incorporate all user alignment details.",
            1, chat_fn, mem, verbose,
        )
        design_handoff = build_lossless_phase_handoff("DESIGN", design_raw, max_chars=5000)
        wf.transition("PLAN", "Revised design ready; updating atomic task plan", run_tests_on_verify=False)
        plan_raw = run_phase_turn(
            "PLAN",
            f"Goal: {goal}\nAccumulated User Alignment & Feedback: {combined_feedback}\nRevised Design:\n{design_handoff}\n\n"
            f"Keep internal <think> reasoning brief (<200 words). Batch 1-4 updated atomic `task_manage(action='add_task', ...)` calls in a single step, then explain the updated plan concisely in plain English.{_PLAN_DAG_HINT}",
            min(half, 3), chat_fn, mem, verbose,
        )
        plan_handoff = build_lossless_phase_handoff("PLAN", plan_raw, max_chars=4500)
        if not mem.get_pending_tasks():
            mem.add_task(f"Implement and verify: {goal[:140]} (Alignment: {combined_feedback[:140]})", priority=1, acceptance_criteria="Passes verification gate with zero stubs.", workflow_id=wf_id)
        dag_dict = build_goal_plan_from_tasks(goal, mem.get_pending_tasks(), plan_handoff).to_dict()
        mem.update_workflow_metadata(wf_id, {
            "design_handoff": design_handoff, "plan_handoff": plan_handoff, "goal_dag": dag_dict,
            "awaiting_plan_approval": True, "pause_reason": "", "last_revision": combined_feedback, "alignment_notes": alignment_notes,
        })
        sync_workflow_to_project_graph(mem, wf_id, goal, "PLAN")

    card = format_blueprint_card(wf_id, goal, cp_id, design_handoff, plan_handoff, mem.get_pending_tasks(), revised=True, goal_dag=dag_dict)
    telemetry.log("INFO", "blueprint", f"Revised Blueprint for Workflow #{wf_id}: {feedback[:80]}")
    return {"status": "AWAITING_APPROVAL", "passed": True, "awaiting_approval": True, "workflow_id": wf_id, "checkpoint_id": cp_id, "summary": card}


def handle_blueprint_conversation_turn(
    user_text: str,
    max_phase_iters: int = 10,
    chat_fn: Callable[..., Dict[str, Any]] = chat,
    store: Optional[MemoryStore] = None,
    cp_mgr: Optional[CheckpointManager] = None,
    root: Optional[Path] = None,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Answer read-only Blueprint questions or statefully persist user alignment into `KnowledgePort` and revise `PLAN`."""
    mem = store or get_memory_store()
    clean = (user_text or "").strip()
    active = mem.get_active_workflow()
    wf_id = int(active["id"]) if active else None
    meta = dict((active or {}).get("metadata") or {})
    if active and is_pure_blueprint_question(clean):
        with model_gate.foreground_turn():
            ans = run_phase_turn(
                "PLAN",
                f"Active Blueprint Goal: {active.get('goal', '')}\nCurrent Design:\n{meta.get('design_handoff', '')}\n"
                f"Current Plan:\n{meta.get('plan_handoff', '')}\n\nUser Question: {clean}\n"
                "Answer the user's question directly and concisely in plain English without modifying tasks or files. Remind them they can reply `approve` or suggest adjustments.",
                3, chat_fn, mem, verbose,
            )
        return {"status": "AWAITING_APPROVAL", "passed": True, "awaiting_approval": True, "workflow_id": wf_id, "summary": ans}
    notes = [*list(meta.get("alignment_notes") or []), clean]
    get_knowledge_port(mem).propose_update(
        key=f"blueprint:{wf_id or 'active'}:alignment",
        value="; ".join(notes)[:800],
        project_id=settings.get_workspace().project_id,
        source="user",
        confidence=1.0,
    )
    return _revise_existing_blueprint(clean, max_phase_iters, chat_fn, mem, cp_mgr, root, verbose)


def create_or_revise_blueprint(
    goal_or_feedback: str,
    workflow_type: str = "engineering_feature",
    revise: bool = False,
    max_phase_iters: int = 10,
    chat_fn: Callable[..., Dict[str, Any]] = chat,
    store: Optional[MemoryStore] = None,
    cp_mgr: Optional[CheckpointManager] = None,
    root: Optional[Path] = None,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Draft a new read-only `DISCOVER -> DESIGN -> PLAN` Blueprint or revise an active one."""
    mem = store or get_memory_store()
    clean = (goal_or_feedback or "").strip()
    if clean.lower() == "status":
        active = mem.get_active_workflow()
        if not active or not (active.get("metadata") or {}).get("awaiting_plan_approval"):
            return {"status": "IDLE", "passed": True, "awaiting_approval": False, "summary": "No pending Blueprint awaiting approval. Start one with `/plan <objective>`."}
        meta = active.get("metadata") or {}
        card = format_blueprint_card(
            int(active["id"]), str(active.get("goal") or ""), str(active.get("checkpoint_id") or ""),
            str(meta.get("design_handoff") or ""), str(meta.get("plan_handoff") or ""), mem.get_pending_tasks(),
            goal_dag=meta.get("goal_dag"), pause_reason=str(meta.get("pause_reason") or ""),
        )
        return {"status": "AWAITING_APPROVAL", "passed": True, "awaiting_approval": True, "workflow_id": int(active["id"]), "summary": card}

    if clean.lower().startswith("revise "):
        revise = True
        clean = clean[7:].strip()
    if not clean:
        return {"status": "error", "passed": False, "awaiting_approval": False, "summary": "Usage: `/plan <objective>` or `/plan revise <feedback>` or `/approve`."}
    if revise:
        return _revise_existing_blueprint(clean, max_phase_iters, chat_fn, mem, cp_mgr, root, verbose)

    res = run_autonomous_goal(goal=clean, workflow_type=workflow_type, max_phase_iters=max_phase_iters, chat_fn=chat_fn, store=mem, cp_mgr=cp_mgr, root=root, verbose=verbose, pause_at_plan=True)
    if not res.get("awaiting_approval"):
        return res
    saved_wf = mem.get_active_workflow()
    saved_dag = (saved_wf.get("metadata") or {}).get("goal_dag") if saved_wf else None
    card = format_blueprint_card(res.get("workflow_id"), str(res.get("goal") or clean), str(res.get("checkpoint_id") or ""), str(res.get("design_handoff") or ""), str(res.get("plan_handoff") or ""), mem.get_pending_tasks(), goal_dag=saved_dag)
    telemetry.log("INFO", "blueprint", f"Drafted Blueprint for Workflow #{res.get('workflow_id')}: {clean[:80]}")
    return {**res, "summary": card}


def approve_blueprint_and_build(
    max_phase_iters: int = 10,
    max_recoveries: int = 2,
    chat_fn: Callable[..., Dict[str, Any]] = chat,
    store: Optional[MemoryStore] = None,
    cp_mgr: Optional[CheckpointManager] = None,
    root: Optional[Path] = None,
    run_doctor_tests: bool = True,
    rollback_on_failure: bool = True,
    verbose: bool = True,
    auto_git_commit: Optional[bool] = None,
) -> Dict[str, Any]:
    """Unlock an approved Blueprint/DAG gate at `PLAN` and drive `IMPLEMENT -> VERIFY -> COMPLETE`."""
    mem = store or get_memory_store()
    saved = mem.get_resumable_workflow()
    if not saved:
        return {"status": "error", "passed": False, "summary": "No pending Blueprint or interrupted workflow found to approve. Run `/plan <objective>` first."}
    wf_id = int(saved["id"])
    meta = dict(saved.get("metadata") or {})
    raw_dag = meta.get("goal_dag")
    has_dag = isinstance(raw_dag, dict) and bool(raw_dag.get("steps"))
    approved_r3 = list(meta.get("approved_r3_steps") or [])
    updates: Dict[str, Any] = {"awaiting_plan_approval": False, "blueprint_approved": True, "pause_reason": ""}
    if not has_dag:
        updates["r3_approved"] = True
    if has_dag:
        dag = GoalPlan.from_dict(raw_dag)
        ready = dag.ready_steps()
        if ready:
            gate_step = ready[0]
            if gate_step.verification.kind == "human_gate":
                gate_step.evidence = "human_approved"
                gate_step.status = "completed"
                if gate_step.task_id:
                    mem.update_task(gate_step.task_id, status="completed", verification_result="Approved by user at human_gate")
                updates["goal_dag"] = dag.to_dict()
            if gate_step.risk_tier == "R3" and gate_step.step_id not in approved_r3:
                approved_r3.append(gate_step.step_id)
        updates["approved_r3_steps"] = approved_r3
    mem.update_workflow_metadata(wf_id, updates)
    telemetry.log("INFO", "blueprint", f"User approved Blueprint #{wf_id}; starting IMPLEMENT -> VERIFY")
    return run_autonomous_goal(
        goal="approve", max_phase_iters=max_phase_iters, max_recoveries=max_recoveries, chat_fn=chat_fn,
        store=mem, cp_mgr=cp_mgr, root=root, run_doctor_tests=run_doctor_tests,
        rollback_on_failure=rollback_on_failure, verbose=verbose, resume=True, pause_at_plan=False, auto_git_commit=auto_git_commit,
    )
