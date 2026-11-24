"""Unit tests for the Deterministic State-Machine Workflow Engine, Python DAG Runtime, and VerificationSpec."""

import tempfile
import unittest
from pathlib import Path

from companion.dag_runtime import (
    DAGNode,
    DAGRuntimeBindings,
    DAGSpec,
    discover_dags,
    execute_dag_by_name,
    execute_dag_spec,
)
from companion.tools import LocalWorkspaceTarget, register_execution_target
from companion.memory_store import MemoryStore
from companion.tools import CheckpointManager
from companion.workflow_dag import (
    GoalPlan,
    GoalStep,
    VerificationSpec,
    evaluate_step_verification,
    is_node_output_passed,
)
from companion.agent import WorkflowEngine, WorkflowState


class TestWorkflowGraph(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        (self.root / "sample.py").write_text('"""Sample module."""\nx = 1\n', encoding="utf-8")
        self.store = MemoryStore(db_path=self.root / "memory" / "conversations.db")
        self.cp_mgr = CheckpointManager(root=self.root, backup_dir=self.root / ".companion_backups")
        self.engine = WorkflowEngine(store=self.store, cp_manager=self.cp_mgr, root=self.root)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp_dir.cleanup()

    def test_full_deterministic_workflow_lifecycle(self) -> None:
        res = self.engine.start_workflow(
            goal="Implement feature X",
            workflow_type="self_evolution",
            acceptance_criteria="Feature X has clean syntax and tests",
        )
        self.assertTrue(res.success)
        self.assertEqual(res.to_state, WorkflowState.INTAKE.value)
        self.assertIsNotNone(res.checkpoint_id)

        bad = self.engine.transition("COMPLETE", summary="Try to skip states")
        self.assertFalse(bad.success)
        self.assertIn("GATE BLOCKED", bad.message)

        for next_state in ("DISCOVER", "DESIGN", "PLAN", "IMPLEMENT", "VERIFY", "REFLECT", "COMPLETE"):
            step_res = self.engine.transition(next_state, summary=f"Completed {next_state}", run_tests_on_verify=True)
            self.assertTrue(step_res.success, f"Transition to {next_state} failed: {step_res.message}")
            self.assertEqual(step_res.to_state, next_state)

        self.assertIsNone(self.store.get_active_workflow())

    def test_verification_gate_auto_routes_to_recover_on_broken_syntax(self) -> None:
        self.engine.start_workflow(goal="Test verification gate failure", workflow_type="bug_fix")
        self.engine.transition("PLAN", summary="Planned fix")
        self.engine.transition("IMPLEMENT", summary="Implementing")

        (self.root / "broken.py").write_text("def broken(\n", encoding="utf-8")

        self.engine.transition("VERIFY", summary="Entering verify", run_tests_on_verify=False)
        res = self.engine.transition("COMPLETE", summary="Try to finish with broken file", run_tests_on_verify=True)
        self.assertFalse(res.success)
        self.assertEqual(res.to_state, WorkflowState.RECOVER.value)
        self.assertIn("VERIFICATION GATE FAILED", res.message)

    def test_illegal_implement_to_complete_bypassing_verify(self) -> None:
        self.engine.start_workflow(goal="Test illegal transition", workflow_type="self_evolution")
        self.assertTrue(self.engine.transition("PLAN", summary="Plan step").success)
        imp_res = self.engine.transition("IMPLEMENT", summary="Implement something")
        self.assertTrue(imp_res.success)
        self.assertEqual(imp_res.to_state, WorkflowState.IMPLEMENT.value)
        bad = self.engine.transition("COMPLETE", summary="Try to skip VERIFY")
        self.assertFalse(bad.success)
        self.assertIn("GATE BLOCKED", bad.message)

    def test_code_controlled_python_dag_discovery_and_execution(self) -> None:
        dag_dir = self.root / "skills" / "demo-skill" / "dags"
        dag_dir.mkdir(parents=True)
        dag_code = (
            "from companion.dag_runtime import DAGNode, DAGSpec\n"
            "DAG = DAGSpec(\n"
            "    name='demo-pipeline',\n"
            "    description='Test tool + conditional Python DAG execution.',\n"
            "    input_schema={'target': 'str'},\n"
            "    nodes=[\n"
            "        DAGNode('step_check', kind='tool_or_script_step', tool_name='check_tool', args_builder=lambda c: {'item': c['params']['target']}),\n"
            "        DAGNode('step_summary', kind='python_step', depends_on=['step_check'], fn=lambda c: {'out': c['nodes']['step_check']['output']}),\n"
            "    ],\n"
            ")\n"
        )
        (dag_dir / "demo_pipeline.py").write_text(dag_code, encoding="utf-8")

        dags = discover_dags(root=self.root)
        self.assertEqual(len(dags), 1)
        self.assertEqual(dags[0]["name"], "demo-pipeline")

        res = execute_dag_by_name(
            "demo-pipeline",
            params={"target": "beta"},
            store=self.store,
            root=self.root,
            bindings=DAGRuntimeBindings(tool_fn=lambda name, args: f"ok:{name}:{args['item']}"),
        )
        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["final_output"]["out"], "ok:check_tool:beta")

    def test_bounded_llm_step_and_builtin_dags_discovery(self) -> None:
        spec = DAGSpec(
            name="tiered-llm-dag",
            description="Verify bounded_llm_step JSON parsing and tier routing.",
            nodes=[
                DAGNode("probe", kind="python_step", output_schema={"metric": "int"}, fn=lambda _c: {"metric": 42}),
                DAGNode(
                    "synthesize",
                    kind="bounded_llm_step",
                    depends_on=["probe"],
                    tier="reasoning",
                    prompt_template="Analyze metric={nodes[probe][metric]}",
                    output_schema={"verdict": "str"},
                ),
            ],
        )
        seen_tiers = []
        run_res = execute_dag_spec(
            spec,
            store=self.store,
            bindings=DAGRuntimeBindings(
                llm_fn=lambda prompt, _sys, tier: (seen_tiers.append((tier, prompt)) or '{"verdict": "healthy"}')
            ),
        )
        self.assertEqual(run_res["status"], "completed")
        self.assertEqual(seen_tiers[0][0], "reasoning")
        self.assertIn("metric=42", seen_tiers[0][1])
        self.assertEqual(run_res["final_output"]["verdict"], "healthy")

        builtin_names = {d["name"] for d in discover_dags()}
        self.assertTrue({"pre-commit-verify", "publish-pr"}.issubset(builtin_names))
        self.assertNotIn("full-audit", builtin_names)

    def test_manual_transition_during_active_dag_returns_clean_result(self) -> None:
        self.store.create_workflow_run(
            workflow_type="dag:full-audit",
            goal="Active DAG run",
            initial_state="audit_node",
        )
        res = self.engine.transition("COMPLETE", summary="Attempt manual transition inside DAG")
        self.assertFalse(res.success)
        self.assertEqual(res.from_state, "audit_node")
        self.assertIn("managed automatically by the DAG runtime", res.message)

    def test_hygiene_failure_detection_and_workflow_ac_update(self) -> None:
        self.assertFalse(is_node_output_passed("=== Codebase Hygiene & Simplicity Report: HYGIENE ISSUES DETECTED (NEEDS SIMPLIFICATION) ==="))
        self.assertTrue(is_node_output_passed("=== Codebase Hygiene & Simplicity Report: CLEAN (PASS) ==="))

        self.engine.start_workflow("AC update test", workflow_type="bug_fix", acceptance_criteria="Initial AC")
        self.engine.transition("PLAN", summary="Updated plan", acceptance_criteria="Refined AC")
        active = self.store.get_active_workflow()
        self.assertIsNotNone(active)
        self.assertEqual(active["acceptance_criteria"], "Refined AC")

    def test_goal_plan_sanitizes_unknown_duplicate_and_cyclic_dependencies(self) -> None:
        plan = GoalPlan(
            goal="Sanitize malformed LLM DAG",
            steps=[
                GoalStep("s1", "First duplicate ID + self + ghost dep", ["s1", "ghost_99"], "R1", VerificationSpec()),
                GoalStep("s1", "Second duplicate ID", ["s1"], "R1", VerificationSpec()),
                GoalStep("cyc_a", "Cycle node A", ["cyc_b"], "R1", VerificationSpec()),
                GoalStep("cyc_b", "Cycle node B", ["cyc_c"], "R1", VerificationSpec()),
                GoalStep("cyc_c", "Cycle node C", ["cyc_a"], "R1", VerificationSpec()),
            ],
        )
        ids = [s.step_id for s in plan.steps]
        self.assertEqual(ids, ["s1", "s1_2", "cyc_a", "cyc_b", "cyc_c"])
        self.assertEqual(plan.steps[0].depends_on, [])
        self.assertEqual(plan.steps[1].depends_on, ["s1"])

        executed_order = []
        for _ in range(10):
            ready = plan.ready_steps()
            if not ready:
                break
            ready[0].status = "completed"
            executed_order.append(ready[0].step_id)
        self.assertEqual(len(executed_order), 5)
        self.assertEqual(set(executed_order), set(ids))

    def test_verification_spec_blocks_path_traversal_and_uses_execution_target(self) -> None:
        ws = self.root / "nested_ws"
        ws.mkdir()
        outside_txt = self.root / "outside_secret.txt"
        outside_txt.write_text("TOP_SECRET_VALUE\n", encoding="utf-8")
        outside_py = self.root / "outside_validator.py"
        outside_py.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")

        ok_ev, msg_ev = evaluate_step_verification(
            VerificationSpec("evidence_file", target="../outside_secret.txt", contains="TOP_SECRET_VALUE"), ws
        )
        self.assertFalse(ok_ev)
        self.assertIn("escapes workspace root", msg_ev)

        for bad_script in ("../outside_validator.py", str(outside_py)):
            ok_sc, msg_sc = evaluate_step_verification(VerificationSpec("script_validator", target=bad_script), ws)
            self.assertFalse(ok_sc)
            self.assertIn("escapes workspace and skills roots", msg_sc)

        class _VirtualTarget(LocalWorkspaceTarget):
            def read_text(self, path: Path) -> str:
                if path.name == "virtual_out.json":
                    return '{"virtual_ready": true}\n'
                return super().read_text(path)

        register_execution_target(_VirtualTarget())
        try:
            ok_v, msg_v = evaluate_step_verification(
                VerificationSpec("evidence_file", target="virtual_out.json", contains='"virtual_ready": true'), ws
            )
            self.assertTrue(ok_v, msg_v)
        finally:
            register_execution_target(None)


if __name__ == "__main__":
    unittest.main()
