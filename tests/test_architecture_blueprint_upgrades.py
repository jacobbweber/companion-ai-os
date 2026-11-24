"""
Comprehensive unit, integration & adversarial edge-case tests for ADR-014
(`companion/grounding.py`, `companion/intake.py`, `companion/workflow_dag.py`, `companion/skill_forge.py`,
stateful Blueprint alignment, cross-project isolation, and domain-neutral DAG verification).
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

from companion.agent import run_agent_turn
from companion.agent import run_autonomous_goal
from companion.agent import approve_blueprint_and_build, create_or_revise_blueprint, is_awaiting_blueprint_approval
from companion.approvals import ActionRiskTier, classify_action_risk
from companion.context import compact_messages_if_needed
from companion.config import SELF_PROJECT_ID, settings, workspace_context
from companion.context import build_system_prompt
from companion.context import KnowledgeEntry, build_epistemic_grounding, build_workspace_radar, get_knowledge_port, register_knowledge_adapter
from companion.tools import LocalWorkspaceTarget, register_execution_target
from companion.agent import assess_turn_readiness, select_turn_step_tier
from companion.llm import current_tier_name
from companion.memory_store import MemoryStore, memory_store_context
from companion.tools import CheckpointManager
from companion.skill_forge import check_make_vs_borrow, run_skill_script, stage_and_promote_skill
from companion.tools import check_phase_tool_permission, phase_tool_context
from companion.tools import dispatch
from companion.workflow_dag import GoalPlan, GoalStep, VerificationSpec, build_goal_plan_from_tasks, evaluate_step_verification
from companion.agent import WorkflowEngine
from surfaces.web_server import CompanionWebSession


class _InMemoryWikiAdapter:
    """Mock external Wiki `KnowledgePort` adapter verifying clean port pluggability."""

    def __init__(self) -> None:
        self.rows: Dict[str, KnowledgeEntry] = {}

    def search(self, query: str, project_id: str | None = None, limit: int = 8) -> List[KnowledgeEntry]:
        q = query.lower()
        return [e for e in self.rows.values() if not q or any(tok in (e.key + " " + e.content).lower() for tok in q.split())][:limit]

    def read_entry(self, key: str, project_id: str | None = None) -> KnowledgeEntry | None:
        return self.rows.get(key)

    def list_index(self, project_id: str | None = None, limit: int = 20) -> List[KnowledgeEntry]:
        return list(self.rows.values())[:limit]

    def propose_update(
        self, key: str, value: str, project_id: str | None = None, source: str = "wiki", confidence: float = 1.0,
    ) -> str:
        self.rows[key] = KnowledgeEntry(key=key, title=key, summary=value[:120], source=source, confidence=confidence, content=value)
        return f"wiki:{key}"


class TestArchitectureBlueprintUpgrades(unittest.TestCase):
    def test_knowledge_port_epistemic_grounding_and_zero_compaction_spam(self) -> None:
        """Verify `KnowledgePort`, `<10ms` epistemic grounding, external Wiki adapter seam, and spam-free compaction."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp) / "ground_ws"
            ws_root.mkdir()
            (ws_root / "gateway_routes.json").write_text('{"listen_port": 8443}\n', encoding="utf-8")
            store = MemoryStore(db_path=Path(tmp) / "ground.db")
            try:
                with workspace_context(ws_root, "ground-proj"), memory_store_context(store):
                    res_up = dispatch("memory_manage", {"action": "upsert_fact", "key": "gateway:port", "value": "8443 TLS", "source": "verified"})
                    self.assertIn("gateway:port", res_up)
                    self.assertIn("8443 TLS", dispatch("memory_manage", {"action": "query_facts", "query": "gateway"}))

                    t0 = time.perf_counter()
                    eg = build_epistemic_grounding("Update gateway_routes.json to integrate CustomTelemetryBus on port 8443", store=store, root=ws_root)
                    self.assertLess((time.perf_counter() - t0) * 1000.0, 50.0)
                    self.assertTrue(any(f.key == "gateway:port" for f in eg.known_facts))
                    self.assertIn("gateway_routes.json", eg.matched_files)
                    self.assertIn("CustomTelemetryBus", eg.unknown_terms)
                    self.assertIn("[EPISTEMIC GROUNDING", eg.prompt_block)
                    self.assertIn("gateway:port", build_epistemic_grounding("", store=store, root=ws_root).prompt_block)
                    self.assertIn("gateway_routes.json", build_workspace_radar(ws_root))

                    # LocalKnowledgeAdapter also surfaces system_notes & known_projects
                    store.add_system_note("Always pin sentinel_port_9443 on internal listeners.", category="ops")
                    store.set_user_profile("known_projects_json", json.dumps({"edge-lab": "/srv/edge-lab"}))
                    note_hits = get_knowledge_port(store).search("sentinel_port_9443")
                    proj_hits = get_knowledge_port(store).search("edge-lab")
                    self.assertTrue(any(h.source == "system-note" for h in note_hits))
                    self.assertTrue(any(h.source == "known-project" for h in proj_hits))

                    # Pluggable external Wiki adapter intercepts live tools AND system-prompt grounding when store is passed
                    wiki = _InMemoryWikiAdapter()
                    register_knowledge_adapter(wiki)
                    try:
                        dispatch("memory_manage", {"action": "upsert_fact", "key": "runbook:canary", "value": "Shift 10% traffic first"})
                        self.assertIn("runbook:canary", wiki.rows)
                        hits = get_knowledge_port(store).search("canary")
                        self.assertEqual(len(hits), 1)
                        self.assertEqual(hits[0].content, "Shift 10% traffic first")
                        sys_p = build_system_prompt(store=store, user_query="Check canary runbook policy")
                        self.assertIn("runbook:canary", sys_p)
                    finally:
                        register_knowledge_adapter(None)

                    # Compaction must NOT spam system_notes
                    msgs: List[Dict[str, Any]] = [{"role": "system", "content": "sys"}]
                    for idx in range(14):
                        msgs.append({"role": "user", "content": f"Turn {idx} " + ("payload " * 80)})
                        msgs.append({"role": "assistant", "content": f"Reply {idx} " + ("detail " * 80)})
                    compact_messages_if_needed(msgs, store=store, force=True)
                    notes = store.get_system_notes(limit=30)
                    self.assertFalse(any(cat == "context-compaction" for _n, cat, _ts in notes))
            finally:
                store.close()

    def test_cross_project_isolation_db_checkpoint_rollback_and_pythonpath(self) -> None:
        """Verify project-scoped SQLite tasks/workflows/facts, external DB rollback, and `PYTHONPATH` isolation."""
        with tempfile.TemporaryDirectory() as tmp:
            ext_a = Path(tmp) / "proj_alpha"
            ext_b = Path(tmp) / "proj_beta"
            ext_a.mkdir()
            ext_b.mkdir()
            store = MemoryStore(db_path=Path(tmp) / "iso.db")
            try:
                self_tid = store.add_task("Companion self task", project_id=SELF_PROJECT_ID)
                with workspace_context(ext_a, "proj-alpha"), memory_store_context(store):
                    (ext_a / "settings.toml").write_text('mode = "stable"\n', encoding="utf-8")
                    store.upsert_knowledge_fact("env:mode", "stable")
                    cp_a = CheckpointManager(root=ext_a, backup_dir=ext_a / ".companion_backups", store=store)
                    snap = cp_a.create_checkpoint(label="before_experiment")

                    # Mutate files + SQLite tasks & facts in proj-alpha
                    (ext_a / "settings.toml").write_text('mode = "broken"\n', encoding="utf-8")
                    orphan_p = ext_a / "orphan.json"
                    orphan_p.write_text('{"temp": true}\n', encoding="utf-8")
                    cp_a.record_file_touch(orphan_p, False, None, orphan_p.read_bytes())
                    human_cs = ext_a / "Program.cs"
                    human_cs.write_text("Console.WriteLine(1);\n", encoding="utf-8")
                    alpha_tid = store.add_task("Alpha experimental task")
                    store.upsert_knowledge_fact("env:mode", "broken")
                    self.assertEqual([t[0] for t in store.get_pending_tasks()], [alpha_tid])

                    # Switch to proj-beta: must not see proj-alpha or companion-self tasks
                    with workspace_context(ext_b, "proj-beta"):
                        self.assertEqual(store.get_pending_tasks(), [])

                    # Rollback proj-alpha checkpoint: rewinds TOML, deletes Companion orphan.json, preserves human Program.cs & self_tid!
                    rb = cp_a.rollback_checkpoint(snap["checkpoint_id"])
                    self.assertTrue(rb["restored"])
                    self.assertEqual((ext_a / "settings.toml").read_text(encoding="utf-8"), 'mode = "stable"\n')
                    self.assertFalse((ext_a / "orphan.json").exists())
                    self.assertTrue(human_cs.exists())
                    self.assertEqual(store.get_pending_tasks(), [])
                    self.assertEqual(store.query_knowledge_facts("env:mode")[0]["value"], "stable")

                    # Verify LocalWorkspaceTarget PYTHONPATH does NOT leak Companion self_root
                    env, cwd = LocalWorkspaceTarget().exec_env_and_cwd()
                    self.assertEqual(Path(cwd), ext_a.resolve())
                    self.assertEqual(env["PYTHONPATH"], str(ext_a.resolve()))
                    self.assertNotIn(str(settings.root.resolve()), env["PYTHONPATH"])

                # Companion-self task remained 100% untouched
                self.assertEqual([t[0] for t in store.get_pending_tasks(project_id=SELF_PROJECT_ID)], [self_tid])
            finally:
                store.close()

    def test_uncompromised_reasoning_routing_sticky_swap_guard_and_readiness(self) -> None:
        """Verify `reasoning` never downgrades on single-GPU swap endpoints while staying sticky mid-turn."""
        with patch.object(settings, "tiers_share_endpoint", return_value=True):
            self.assertEqual(select_turn_step_tier("auto", None, 0, step=0, intent_mode="DEEP_CONSULT"), "reasoning")
            self.assertEqual(select_turn_step_tier("auto", "DESIGN", 0, step=0, intent_mode="GUIDED_CHANGE"), "reasoning")
            self.assertEqual(select_turn_step_tier("auto", "IMPLEMENT", 2, step=1, intent_mode="GUIDED_CHANGE"), "reasoning")
            self.assertEqual(select_turn_step_tier("auto", None, 0, step=1, intent_mode="DEEP_CONSULT", prior_tier="reasoning"), "reasoning")

        with patch.object(settings, "tiers_share_endpoint", return_value=False):
            self.assertEqual(select_turn_step_tier("auto", None, 0, step=0, intent_mode="DEEP_CONSULT"), "reasoning")
            self.assertEqual(select_turn_step_tier("auto", None, 0, step=1, intent_mode="DEEP_CONSULT", prior_tier="reasoning"), "reasoning")

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ready_ws"
            ws.mkdir()
            (ws / "app.py").write_text('"""App."""\n', encoding="utf-8")
            st = MemoryStore(db_path=Path(tmp) / "r.db")
            try:
                r_ok = assess_turn_readiness("Edit app.py to add a helper function", store=st, root=ws)
                self.assertEqual(r_ok.status, "READY")
                r_gap = assess_turn_readiness("Configure ExternalBillingCluster and SyncBrokerV2 endpoints", store=st, root=ws)
                self.assertEqual(r_gap.status, "NEEDS_ALIGNMENT")
                self.assertIn("ExternalBillingCluster", r_gap.unknown_terms)
                r_r3 = assess_turn_readiness("Run git push origin main to publish release", store=st, root=ws)
                self.assertEqual(r_r3.status, "READY")
                self.assertIn("RISK GATE REJECTED (R3)", dispatch("run_shell", {"command": "git push origin main"}))
            finally:
                st.close()

    def test_fine_grained_phase_action_gates_and_r3_risk_gate(self) -> None:
        """Verify `(tool, action)` locks in `PLAN`/`VERIFY` and `R3` high-impact shell command gating."""
        self.assertIsNone(check_phase_tool_permission("checkpoint_manage", "PLAN", {"action": "list"}))
        self.assertIn("PHASE TOOL GATE REJECTED", check_phase_tool_permission("checkpoint_manage", "PLAN", {"action": "rollback"}) or "")
        self.assertIn("PHASE TOOL GATE REJECTED", check_phase_tool_permission("workflow_manage", "PLAN", {"action": "run_dag"}) or "")
        self.assertIn("PHASE TOOL GATE REJECTED", check_phase_tool_permission("workflow_manage", "PLAN", {"action": "bootstrap_skill"}) or "")
        self.assertIn("PHASE TOOL GATE REJECTED", check_phase_tool_permission("workflow_manage", "PLAN", {"action": "run_script"}) or "")
        self.assertIsNone(check_phase_tool_permission("workflow_manage", "VERIFY", {"action": "run_script"}))
        self.assertIn("PHASE TOOL GATE REJECTED", check_phase_tool_permission("project_manage", "DISCOVER", {"action": "switch"}) or "")
        self.assertIsNone(check_phase_tool_permission("project_manage", "IMPLEMENT", {"action": "switch"}))
        self.assertIn("PHASE TOOL GATE REJECTED", check_phase_tool_permission("memory_manage", "PLAN", {"action": "upsert_fact"}) or "")
        self.assertIsNone(check_phase_tool_permission("memory_manage", "REFLECT", {"action": "upsert_fact"}))

        self.assertEqual(classify_action_risk("read_file", {"path": "README.md"}), ActionRiskTier.R0_READ_ONLY)
        self.assertEqual(classify_action_risk("write_file", {"path": "app.py", "content": "x=1"}), ActionRiskTier.R1_LOCAL_REVERSIBLE)
        self.assertEqual(classify_action_risk("workflow_manage", {"action": "bootstrap_skill"}), ActionRiskTier.R2_CAPABILITY_PROMOTION)
        self.assertEqual(classify_action_risk("run_shell", {"command": "git push origin main"}), ActionRiskTier.R3_HIGH_IMPACT_MUTATION)

        with tempfile.TemporaryDirectory() as tmp:
            st = MemoryStore(db_path=Path(tmp) / "r3.db")
            try:
                with memory_store_context(st):
                    blocked = dispatch("run_shell", {"command": "git push origin main"})
                    self.assertIn("RISK GATE REJECTED (R3)", blocked)
                    with phase_tool_context("PLAN"):
                        p_blk = dispatch("checkpoint_manage", {"action": "rollback"})
                        self.assertIn("PHASE TOOL GATE REJECTED", p_blk)
            finally:
                st.close()

    def test_stateful_blueprint_conversational_alignment_persists_to_build(self) -> None:
        """Verify multi-turn user answers during `PLAN` accumulate in `KnowledgePort` and `workflow_runs` through `/approve`."""
        seen_prompts: List[str] = []

        def _align_driver(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
            user_ctx = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
            seen_prompts.append(user_ctx)
            is_tool = messages[-1].get("role") == "tool"
            if "[AUTONOMOUS WORKFLOW PHASE: PLAN" in user_ctx and not is_tool:
                tc = {"id": f"tp_{len(seen_prompts)}", "function": {"name": "task_manage", "arguments": json.dumps({"action": "add_task", "description": "Create config_out.json"})}}
                return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}
            if "[AUTONOMOUS WORKFLOW PHASE: IMPLEMENT" in user_ctx and not is_tool:
                payload = json.dumps({"port": 9120, "format": "strict_json"}, indent=2)
                tc = {"id": "ti_1", "function": {"name": "write_file", "arguments": json.dumps({"path": "config_out.json", "content": payload})}}
                return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}
            return {"choices": [{"message": {"role": "assistant", "content": f"Aligned summary ({len(seen_prompts)})."}}]}

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "align_ws"
            ws.mkdir()
            st = MemoryStore(db_path=Path(tmp) / "align.db")
            cp = CheckpointManager(root=ws, backup_dir=ws / ".companion_backups", store=st)
            try:
                with workspace_context(ws, "align-proj"), memory_store_context(st):
                    bp = create_or_revise_blueprint("Create `config_out.json` for local listener.", chat_fn=_align_driver, store=st, cp_mgr=cp, root=ws, verbose=False)
                    self.assertTrue(bp["awaiting_approval"])

                    # Two conversational clarification turns without `/plan revise` prefix
                    r1 = run_agent_turn([{"role": "user", "content": "Bind listener on port 9120."}], chat_fn=_align_driver, store=st, verbose=False)
                    self.assertIn("Revised Blueprint Ready for Your Review", r1)
                    self.assertTrue(is_awaiting_blueprint_approval(st))

                    r2 = run_agent_turn([{"role": "user", "content": "Use strict_json output format."}], chat_fn=_align_driver, store=st, verbose=False)
                    self.assertIn("Revised Blueprint Ready for Your Review", r2)

                    wf = st.get_active_workflow()
                    assert wf is not None
                    notes = wf["metadata"].get("alignment_notes", [])
                    self.assertEqual(len(notes), 2)
                    facts = st.query_knowledge_facts("alignment")
                    self.assertTrue(any("9120" in f["value"] and "strict_json" in f["value"] for f in facts))

                    # Approve and verify both alignment turns reached IMPLEMENT
                    done = run_agent_turn([{"role": "user", "content": "Approve"}], chat_fn=_align_driver, store=st, verbose=False)
                    self.assertIn("Autonomous Goal Execution: COMPLETE", done)
                    impl_prompts = [p for p in seen_prompts if "[AUTONOMOUS WORKFLOW PHASE: IMPLEMENT" in p]
                    self.assertTrue(any("9120" in p and "strict_json" in p for p in impl_prompts))
                    self.assertEqual(json.loads((ws / "config_out.json").read_text(encoding="utf-8"))["port"], 9120)
            finally:
                st.close()

    def test_dynamic_goal_plan_dag_human_gate_pause_and_domain_neutral_verify(self) -> None:
        """Verify `VerificationSpec`, mid-DAG `human_gate` pause/resume, external non-stdlib allowance, and `ExecutorBindings`."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "dag_ws"
            ws.mkdir()

            # Invalid JSON fails structured VerificationSpec; valid TOML passes
            bad_json = ws / "bad.json"
            bad_json.write_text("{broken_json", encoding="utf-8")
            ok_bad, msg_bad = evaluate_step_verification(VerificationSpec("evidence_file", target="bad.json"), ws)
            self.assertFalse(ok_bad)
            self.assertIn("Invalid JSON", msg_bad)
            bad_json.unlink()

            st = MemoryStore(db_path=Path(tmp) / "dag.db")
            cp = CheckpointManager(root=ws, backup_dir=ws / ".companion_backups", store=st)
            wf = WorkflowEngine(store=st, cp_manager=cp, root=ws)

            def _dag_chat(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                u = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
                is_tool = messages[-1].get("role") == "tool"
                if "[AUTONOMOUS WORKFLOW PHASE: IMPLEMENT" in u and not is_tool:
                    if "Create summary.json" in u:
                        tc = {"id": "d3", "function": {"name": "write_file", "arguments": json.dumps({"path": "summary.json", "content": '{"ready": true}\n'})}}
                        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}
                    if "Create manifest.toml" in u:
                        tc = {"id": "d1", "function": {"name": "write_file", "arguments": json.dumps({"path": "manifest.toml", "content": '[app]\nname = "demo"\nport = 7070\n'})}}
                        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}
                return {"choices": [{"message": {"role": "assistant", "content": "Step finished."}}]}

            try:
                with workspace_context(ws, "dag-proj"), memory_store_context(st):
                    from companion.dag_runtime import execute_dag_by_name
                    dag_res = execute_dag_by_name("full-audit", params={"run_tests": False}, store=st)
                    self.assertIn("status", dag_res)

                    start = wf.start_workflow("Roll out manifest.toml + human review + summary.json")
                    for s_name in ("DISCOVER", "DESIGN", "PLAN"):
                        wf.transition(s_name, f"Seed {s_name}", run_tests_on_verify=False)
                    t1 = st.add_task("Create manifest.toml", workflow_id=start.workflow_id)
                    t2 = st.add_task("Human review gate", workflow_id=start.workflow_id)
                    t3 = st.add_task("Create summary.json", workflow_id=start.workflow_id)
                    dag = GoalPlan(
                        goal="Roll out manifest.toml + human review + summary.json",
                        steps=[
                            GoalStep("s1", "Create manifest.toml", [], "R1", VerificationSpec("evidence_file", "manifest.toml", "7070"), task_id=t1),
                            GoalStep("s2", "Checkpoint review before summary", ["s1"], "R1", VerificationSpec("human_gate", description="Confirm manifest.toml"), task_id=t2),
                            GoalStep("s3", "Create summary.json", ["s2"], "R1", VerificationSpec("evidence_file", "summary.json", "true"), task_id=t3),
                        ],
                    )
                    assert start.workflow_id is not None
                    st.update_workflow_metadata(start.workflow_id, {
                        "discovery_handoff": "Empty dag_ws.", "design_handoff": "Write manifest.toml, pause at human_gate, then summary.json.",
                        "plan_handoff": "3-step GoalPlan DAG.", "goal_dag": dag.to_dict(),
                    })

                    # Run 1: executes s1, pauses cleanly at s2 (human_gate), does NOT create summary.json yet!
                    r_pause = run_autonomous_goal("resume", chat_fn=_dag_chat, store=st, cp_mgr=cp, root=ws, verbose=False, resume=True)
                    self.assertEqual(r_pause["status"], "AWAITING_APPROVAL")
                    self.assertTrue((ws / "manifest.toml").is_file())
                    self.assertFalse((ws / "summary.json").exists())

                    # Approve: satisfies s2 human_gate, executes s3, passes VERIFY, distills REFLECT fact -> COMPLETE!
                    r_done = approve_blueprint_and_build(chat_fn=_dag_chat, store=st, cp_mgr=cp, root=ws, verbose=False)
                    self.assertEqual(r_done["status"], "COMPLETE")
                    self.assertTrue((ws / "summary.json").is_file())
                    facts = st.query_knowledge_facts("deliverables")
                    # Pre-pause manifest.toml AND post-pause summary.json must BOTH be recorded in REFLECT deliverables!
                    self.assertTrue(any("manifest.toml" in f["value"] and "summary.json" in f["value"] for f in facts))
                    art_facts = st.query_knowledge_facts("artifact:manifest.toml")
                    self.assertTrue(any("TOML sections" in f["value"] for f in art_facts))
            finally:
                st.close()

    def test_transactional_skill_forge_rejection_promotion_and_general_eval_flight(self) -> None:
        """Verify `skill_forge` rejects bad skills with zero residue, promotes `--self-test` skills, and runs `general` eval."""
        valid_md = (
            "---\nname: json-schema-check\ndescription: Validate JSON keys deterministically.\nrequired_tools: [workflow_manage]\n---\n\n"
            "# JSON Schema Check\n\n## When to Use This Skill\nWhen checking JSON artifacts.\n\n## Step-by-Step Procedure\n1. Run `checker.py`.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "skill_repo"
            (repo / "skills").mkdir(parents=True)
            st = MemoryStore(db_path=Path(tmp) / "sf.db")
            try:
                with memory_store_context(st):
                    r_name = stage_and_promote_skill("Bad_Name!", valid_md, root=repo)
                    self.assertFalse(r_name["promoted"])
                    r_syn = stage_and_promote_skill("json-schema-check", valid_md, scripts={"checker.py": "def broken(:\n"}, root=repo)
                    self.assertFalse(r_syn["promoted"])
                    self.assertFalse((repo / "skills" / "json-schema-check").exists())
                    r_fail = stage_and_promote_skill("json-schema-check", valid_md, scripts={"checker.py": '"""Doc."""\nimport sys\nif "--self-test" in sys.argv:\n    sys.exit(2)\n'}, root=repo)
                    self.assertFalse(r_fail["promoted"])
                    self.assertFalse((repo / "skills" / "json-schema-check").exists())
                    good_script = (
                        '"""Deterministic checker."""\nimport sys\n\n'
                        'def main() -> int:\n    if "--self-test" in sys.argv:\n        print("SELF_TEST_OK")\n        return 0\n'
                        '    print("CHECKED:" + ",".join(sys.argv[1:]))\n    return 0\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
                    )
                    r_ok = stage_and_promote_skill("json-schema-check", valid_md, scripts={"checker.py": good_script}, root=repo)
                    self.assertTrue(r_ok["promoted"], r_ok.get("error"))
                    self.assertEqual(check_make_vs_borrow("Validate JSON keys deterministically", root=repo)["recommendation"], "BORROW")
                    out = run_skill_script("json-schema-check", "checker.py", ["alpha", "beta"], root=repo)
                    self.assertIn("CHECKED:alpha,beta", out)
            finally:
                st.close()

    def test_execution_target_seam_and_live_grounding_and_consult_lock(self) -> None:
        """Verify custom `ExecutionTarget` interception, live `reasoning` readiness, `DEEP_CONSULT` tool lock, and auto-Blueprint."""
        ops: List[str] = []

        class _RecTarget(LocalWorkspaceTarget):
            def read_text(self, path: Path) -> str:
                ops.append("read")
                return super().read_text(path)

            def write_text(self, path: Path, content: str, backup: bool = True) -> None:
                ops.append("write")
                super().write_text(path, content, backup=backup)

            def list_dir(self, path: Path) -> List[str]:
                ops.append("list")
                return super().list_dir(path)

            def exec_command(self, cmd_or_args, shell=False, timeout=None, extra_env=None, cwd_override=None):
                ops.append("exec")
                return super().exec_command(cmd_or_args, shell=shell, timeout=timeout, extra_env=extra_env, cwd_override=cwd_override)

            def create_checkpoint(self, label: str = "auto") -> Dict[str, Any]:
                ops.append("cp_create")
                return super().create_checkpoint(label=label)

        with tempfile.TemporaryDirectory() as tmp:
            ws, st = Path(tmp) / "edge_ws", MemoryStore(db_path=Path(tmp) / "edge.db")
            ws.mkdir()
            try:
                with workspace_context(ws, "edge-proj"), memory_store_context(st):
                    register_execution_target(_RecTarget())
                    try:
                        dispatch("write_file", {"path": "item.json", "content": '{"v": 1}\n'})
                        dispatch("edit_file", {"path": "item.json", "old_str": "1", "new_str": "2"})
                        dispatch("read_file", {"path": "item.json"})
                        dispatch("list_dir", {"path": "."})
                        dispatch("run_python", {"code": "print(42)"})
                        dispatch("checkpoint_manage", {"action": "create", "label": "t"})
                        evaluate_step_verification(VerificationSpec("check_command", target="echo ok_probe", contains="ok_probe"), ws)
                        self.assertTrue({"write", "read", "list", "exec", "cp_create"}.issubset(set(ops)))
                    finally:
                        register_execution_target(None)

                    seen_tiers: List[str] = []

                    def _probe_chat(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                        seen_tiers.append(current_tier_name() or "")
                        u = str(messages[-1].get("content", ""))
                        if "Compare tradeoffs" in u and messages[-1].get("role") == "user":
                            fname = "allowed_q.txt" if "?" in u else "illegal.txt"
                            tc = {"id": "mut1", "function": {"name": "write_file", "arguments": json.dumps({"path": fname, "content": "x"})}}
                            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}
                        return {"choices": [{"message": {"role": "assistant", "content": "Inspected cleanly."}}]}

                    run_agent_turn([{"role": "user", "content": "Patch ExternalBillingCluster and SyncBrokerV2 routing"}], max_iters=2, chat_fn=_probe_chat, store=st, verbose=False)
                    self.assertEqual(seen_tiers[0], "reasoning")
                    # Normal Chat `?` prompts retain write tools (CORE-1), whereas explicit PLAN phase locks write tools
                    q_msgs: List[Dict[str, Any]] = [{"role": "user", "content": "Compare tradeoffs between architecture options and save summary for local queues?"}]
                    run_agent_turn(q_msgs, max_iters=2, chat_fn=_probe_chat, store=st, verbose=False)
                    self.assertTrue((ws / "allowed_q.txt").exists())
                    consult_msgs: List[Dict[str, Any]] = [{"role": "user", "content": "Compare tradeoffs between architecture options for local queues"}]
                    with phase_tool_context("PLAN"):
                        run_agent_turn(consult_msgs, max_iters=2, chat_fn=_probe_chat, store=st, verbose=False)
                    self.assertFalse((ws / "illegal.txt").exists())
                    self.assertTrue(any("PHASE TOOL GATE REJECTED" in str(m.get("content", "")) for m in consult_msgs if m.get("role") == "tool"))
                    bp_auto = run_agent_turn(
                        [{"role": "user", "content": "Build a service listener — I am not sure how to structure it, align with me first"}],
                        chat_fn=_probe_chat, store=st, verbose=False, auto_promote=True,
                    )
                    self.assertIn("Blueprint Ready for Your Review", bp_auto)
                    self.assertTrue(is_awaiting_blueprint_approval(st))
            finally:
                st.close()

    def test_web_plan_mode_alignment_and_per_step_r3_and_assertion_recovery(self) -> None:
        """Verify Web `Plan First` alignment Q&A, per-step `R3` DAG gates, and blocked `assertion` DAG recovery."""
        with tempfile.TemporaryDirectory() as tmp:
            ws, st = Path(tmp) / "web_ws", MemoryStore(db_path=Path(tmp) / "web.db")
            ws.mkdir()
            cp = CheckpointManager(root=ws, backup_dir=ws / ".companion_backups", store=st)
            try:
                with workspace_context(ws, "web-proj"), memory_store_context(st):
                    def _web_driver(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                        u = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
                        is_tool = messages[-1].get("role") == "tool"
                        if "[AUTONOMOUS WORKFLOW PHASE: PLAN" in u and "User Question:" not in u and not is_tool:
                            return {"choices": [{"message": {"role": "assistant", "content": '```json\n{"steps":[{"step_id":"s1","title":"Create out.json","depends_on":[],"risk_tier":"R1","verification":{"kind":"evidence_file","target":"out.json"}}]}\n```'}}]}
                        if "[AUTONOMOUS WORKFLOW PHASE: IMPLEMENT" in u and not is_tool:
                            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "wi", "function": {"name": "write_file", "arguments": '{"path":"out.json","content":"{\\"port\\":9120}"}'}}]}}]}
                        return {"choices": [{"message": {"role": "assistant", "content": "JSON is stdlib-validated."}}]}

                    web = CompanionWebSession(store=st, chat_fn=_web_driver)
                    _, p1 = web.execute_chat("Create out.json listener config", mode="plan")
                    wf_orig = st.get_active_workflow()["id"]
                    self.assertTrue(p1["awaiting_approval"])
                    _, q1 = web.execute_chat("Why did you choose JSON?", mode="plan")
                    self.assertTrue(q1["awaiting_approval"])
                    self.assertEqual(st.get_active_workflow()["id"], wf_orig)
                    _, a1 = web.execute_chat("Bind listener on port 9120.", mode="plan")
                    self.assertEqual(st.get_active_workflow()["id"], wf_orig)
                    _, ok1 = web.execute_chat("Approve", mode="plan")
                    self.assertIn("Autonomous Goal Execution: COMPLETE", ok1["response"])

                    wf = WorkflowEngine(store=st, cp_manager=cp, root=ws)
                    sr = wf.start_workflow("Multi-R3 + assertion recovery")
                    for ph in ("DISCOVER", "DESIGN", "PLAN"):
                        wf.transition(ph, ph, run_tests_on_verify=False)
                    dag = GoalPlan("Multi-R3 + assertion recovery", [
                        GoalStep("s1", "Prep local check", [], "R1", VerificationSpec("assertion", contains="READY_TOKEN")),
                        GoalStep("s2", "Cutover alpha", ["s1"], "R3", VerificationSpec("assertion")),
                        GoalStep("s3", "Cutover beta", ["s2"], "R3", VerificationSpec("assertion")),
                    ])
                    st.update_workflow_metadata(sr.workflow_id, {"discovery_handoff": "d", "design_handoff": "d", "plan_handoff": "p", "goal_dag": dag.to_dict()})
                    r1_r3_check: List[str] = []

                    def _r3_chat(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                        u = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
                        if "Prep local check" in u and not r1_r3_check:
                            r1_r3_check.append(dispatch("run_shell", {"command": "git push origin main"}))
                            return {"choices": [{"message": {"role": "assistant", "content": "missing token on purpose"}}]}
                        if "[AUTONOMOUS WORKFLOW PHASE: RECOVER" in u:
                            return {"choices": [{"message": {"role": "assistant", "content": "Fixed: READY_TOKEN confirmed"}}]}
                        return {"choices": [{"message": {"role": "assistant", "content": "READY_TOKEN"}}]}

                    # Run 1: s1 omits READY_TOKEN -> s1 is blocked -> s2/s3 stay pending -> VERIFY catches incomplete s1 -> RECOVER supplies READY_TOKEN!
                    rec_run = run_autonomous_goal("resume", chat_fn=_r3_chat, store=st, cp_mgr=cp, root=ws, verbose=False, resume=True)
                    self.assertIn("RISK GATE REJECTED (R3)", r1_r3_check[0])
                    self.assertEqual(rec_run["status"], "FAILED")  # s2 & s3 still pending after s1 blocked!
                    # Fresh multi-R3 run where s1 succeeds -> pauses at s2 -> pauses at s3 -> COMPLETE!
                    sr2 = wf.start_workflow("Multi-R3 clean")
                    for ph in ("DISCOVER", "DESIGN", "PLAN"):
                        wf.transition(ph, ph, run_tests_on_verify=False)
                    st.update_workflow_metadata(sr2.workflow_id, {"discovery_handoff": "d", "design_handoff": "d", "plan_handoff": "p", "goal_dag": dag.to_dict()})
                    p_s2 = run_autonomous_goal("resume", chat_fn=_r3_chat, store=st, cp_mgr=cp, root=ws, verbose=False, resume=True)
                    self.assertIn("s2", p_s2["pause_reason"])
                    p_s3 = approve_blueprint_and_build(chat_fn=_r3_chat, store=st, cp_mgr=cp, root=ws, verbose=False)
                    self.assertIn("s3", p_s3["pause_reason"])
                    fin = approve_blueprint_and_build(chat_fn=_r3_chat, store=st, cp_mgr=cp, root=ws, verbose=False)
                    self.assertEqual(fin["status"], "COMPLETE")
                    self.assertEqual(len(build_goal_plan_from_tasks("g", [], '```json\n{"steps":[{"id":"x1","title":"T"}]}\n```').steps), 1)
            finally:
                st.close()


if __name__ == "__main__":
    unittest.main()
