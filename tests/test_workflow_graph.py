"""Unit tests for the Deterministic State-Machine Workflow Graph Engine."""

import tempfile
import unittest
from pathlib import Path

from companion.memory_store import MemoryStore
from companion.safety import CheckpointManager
from companion.workflow_graph import WorkflowEngine, WorkflowState


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
        # 1. Start workflow in INTAKE
        res = self.engine.start_workflow(
            goal="Implement feature X",
            workflow_type="self_evolution",
            acceptance_criteria="Feature X has clean syntax and tests",
        )
        self.assertTrue(res.success)
        self.assertEqual(res.to_state, WorkflowState.INTAKE.value)
        self.assertIsNotNone(res.checkpoint_id)

        # 2. Invalid jump directly from INTAKE to COMPLETE must be blocked by graph rules
        bad = self.engine.transition("COMPLETE", summary="Try to skip states")
        self.assertFalse(bad.success)
        self.assertIn("GATE BLOCKED", bad.message)

        # 3. Valid transitions through DISCOVER -> DESIGN -> PLAN -> IMPLEMENT -> VERIFY -> REFLECT -> COMPLETE
        for next_state in ("DISCOVER", "DESIGN", "PLAN", "IMPLEMENT", "VERIFY", "REFLECT", "COMPLETE"):
            step_res = self.engine.transition(next_state, summary=f"Completed {next_state}", run_tests_on_verify=True)
            self.assertTrue(step_res.success, f"Transition to {next_state} failed: {step_res.message}")
            self.assertEqual(step_res.to_state, next_state)

        # Workflow should now be completed (no active workflow)
        self.assertIsNone(self.store.get_active_workflow())

    def test_verification_gate_auto_routes_to_recover_on_broken_syntax(self) -> None:
        self.engine.start_workflow(goal="Test verification gate failure", workflow_type="bug_fix")
        self.engine.transition("PLAN", summary="Planned fix")
        self.engine.transition("IMPLEMENT", summary="Implementing")

        # Introduce a broken .py file on disk (bypassing tool guard to test workflow gate)
        (self.root / "broken.py").write_text("def broken(\n", encoding="utf-8")

        # Transitioning to VERIFY -> COMPLETE must fail verification and route to RECOVER
        self.engine.transition("VERIFY", summary="Entering verify", run_tests_on_verify=False)
        res = self.engine.transition("COMPLETE", summary="Try to finish with broken file", run_tests_on_verify=True)
        self.assertFalse(res.success)
        self.assertEqual(res.to_state, WorkflowState.RECOVER.value)
        self.assertIn("VERIFICATION GATE FAILED", res.message)

    def test_illegal_implement_to_complete_bypassing_verify(self) -> None:
        # Start a workflow and transition to PLAN -> IMPLEMENT
        self.engine.start_workflow(
            goal="Test illegal transition",
            workflow_type="self_evolution",
            acceptance_criteria="",
        )
        plan_res = self.engine.transition("PLAN", summary="Plan step")
        self.assertTrue(plan_res.success)
        imp_res = self.engine.transition("IMPLEMENT", summary="Implement something")
        self.assertTrue(imp_res.success)
        self.assertEqual(imp_res.to_state, WorkflowState.IMPLEMENT.value)
        # Attempt an illegal jump from IMPLEMENT to COMPLETE bypassing VERIFY
        bad = self.engine.transition("COMPLETE", summary="Try to skip VERIFY")
        self.assertFalse(bad.success)
        self.assertIn("GATE BLOCKED", bad.message)

    def test_declarative_json_workflow_recipe_discovery_and_execution(self) -> None:
        wf_dir = self.root / "skills" / "demo-skill" / "workflows"
        wf_dir.mkdir(parents=True)
        recipe_json = """{
          "name": "demo-pipeline",
          "description": "Test conditional tool + subagent DAG recipe.",
          "inputs": {"target": "alpha"},
          "nodes": [
            {"id": "step_check", "type": "tool", "tool": "check_tool", "args": {"item": "{{target}}"}},
            {"id": "step_repair", "type": "subagent", "skill": "demo-skill", "prompt": "Repair {{target}}: {{last_output}}"}
          ],
          "edges": [
            {"from": "step_check", "to": "COMPLETE", "condition": "passed"},
            {"from": "step_check", "to": "step_repair", "condition": "failed"},
            {"from": "step_repair", "to": "COMPLETE", "condition": "contains:repaired"}
          ]
        }"""
        (wf_dir / "demo-pipeline.json").write_text(recipe_json, encoding="utf-8")

        recipes = self.engine.discover_workflow_recipes()
        self.assertEqual(len(recipes), 1)
        self.assertEqual(recipes[0]["name"], "demo-pipeline")
        self.assertTrue(recipes[0]["valid"])
        self.assertIn("demo-pipeline", self.engine.format_recipes_catalog())

        # Case 1: Tool passes -> routes directly to COMPLETE in 1 step (0 subagent calls)
        res_pass = self.engine.run_workflow_recipe(
            "demo-pipeline",
            params={"target": "beta"},
            tool_fn=lambda name, args: f"ok:{name}:{args['item']}",
            subagent_fn=lambda g, s, m: "should not run",
        )
        self.assertEqual(res_pass["status"], "COMPLETE")
        self.assertEqual(len(res_pass["steps"]), 1)
        self.assertIn("ok:check_tool:beta", res_pass["steps"][0]["output"])

        # Case 2: Tool fails -> conditional edge routes to subagent_repair -> COMPLETE
        res_repair = self.engine.run_workflow_recipe(
            "demo-pipeline",
            params={"target": "gamma"},
            tool_fn=lambda name, args: f"error: broken {args['item']}",
            subagent_fn=lambda g, s, m: f"Successfully repaired ({g})",
        )
        self.assertEqual(res_repair["status"], "COMPLETE")
        self.assertEqual(len(res_repair["steps"]), 2)
        self.assertEqual(res_repair["steps"][1]["node"], "step_repair")
        self.assertIn("error: broken gamma", res_repair["steps"][1]["output"])

    def test_recipe_step_output_field_interpolation(self) -> None:
        wf_dir = self.root / "skills" / "demo-skill" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        recipe_json = """{
          "name": "chained-steps",
          "description": "Test {{steps.<id>.output.<field>}} and {{inputs.<key>}} interpolation.",
          "inputs": {"env": "prod"},
          "nodes": [
            {"id": "step_probe", "type": "tool", "tool": "probe_tool", "args": {"env": "{{inputs.env}}"}},
            {"id": "step_kv", "type": "tool", "tool": "kv_tool", "args": {"file": "{{steps.step_probe.output.meta.target_file}}"}},
            {"id": "step_final", "type": "subagent", "skill": "demo-skill", "prompt": "Deploy {{steps.step_probe.output.meta.target_file}} ({{steps.step_kv.output.artifact_id}}){{inputs.optional_suffix}}"}
          ],
          "edges": [
            {"from": "step_probe", "to": "step_kv", "condition": "passed"},
            {"from": "step_kv", "to": "step_final", "condition": "passed"},
            {"from": "step_final", "to": "COMPLETE", "condition": "passed"}
          ]
        }"""
        (wf_dir / "chained-steps.json").write_text(recipe_json, encoding="utf-8")

        def fake_tool(name: str, args: dict) -> str:
            if name == "probe_tool":
                return '{"status": "ok", "meta": {"target_file": "companion/safety.py"}}'
            return f"status=ready\nartifact_id=art-42\nsource={args['file']}"

        res = self.engine.run_workflow_recipe(
            "chained-steps",
            tool_fn=fake_tool,
            subagent_fn=lambda prompt, skill, iters: f"Summary: {prompt}",
        )
        self.assertEqual(res["status"], "COMPLETE")
        self.assertEqual(len(res["steps"]), 3)
        self.assertIn("source=companion/safety.py", res["steps"][1]["output"])
        self.assertEqual(res["steps"][2]["output"], "Summary: Deploy companion/safety.py (art-42)")

    def test_recipe_tier_routing_and_canonical_recipes_discovery(self) -> None:
        from companion.llm import current_tier_name
        from companion.workflow_graph import workflow_engine

        wf_dir = self.root / "skills" / "demo-skill" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        recipe_json = """{
          "name": "tiered-pipeline",
          "description": "Test per-node fast and reasoning tier routing.",
          "nodes": [
            {"id": "fast_probe", "type": "tool", "tier": "fast", "tool": "probe", "args": {}},
            {"id": "deep_step", "type": "subagent", "tier": "reasoning", "skill": "demo-skill", "prompt": "Reason over {{steps.fast_probe.output}}"}
          ],
          "edges": [
            {"from": "fast_probe", "to": "deep_step", "condition": "always"},
            {"from": "deep_step", "to": "COMPLETE", "condition": "passed"}
          ]
        }"""
        (wf_dir / "tiered-pipeline.json").write_text(recipe_json, encoding="utf-8")

        observed_tiers = []

        def fake_subagent(prompt: str, skill: str, iters: int) -> str:
            observed_tiers.append(current_tier_name())
            return f"Reasoned ({current_tier_name()}): {prompt}"

        res = self.engine.run_workflow_recipe(
            "tiered-pipeline",
            tool_fn=lambda n, a: f"probe_tier={current_tier_name()}",
            subagent_fn=fake_subagent,
        )
        self.assertEqual(res["status"], "COMPLETE")
        self.assertEqual(res["steps"][0]["tier"], "fast")
        self.assertEqual(res["steps"][1]["tier"], "reasoning")
        self.assertEqual(observed_tiers, ["reasoning"])

        real_recipes = {r["name"]: r for r in workflow_engine.discover_workflow_recipes()}
        self.assertIn("deep-analysis", real_recipes)
        self.assertIn("ooda-diagnose", real_recipes)
        self.assertIn("full-audit", real_recipes)
        self.assertTrue(all(r["valid"] for r in real_recipes.values()))

    def test_recipe_guaranteed_checkpoint_auto_rollback_and_nested_recipe_node(self) -> None:
        wf_dir = self.root / "skills" / "demo-skill" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        child_json = """{
          "name": "child-check",
          "description": "Child recipe invoked as a composable node.",
          "nodes": [
            {"id": "child_step", "type": "tool", "tool": "verify_tool", "args": {"mode": "{{inputs.mode}}"}}
          ],
          "edges": [
            {"from": "child_step", "to": "COMPLETE", "condition": "passed"},
            {"from": "child_step", "to": "ABORTED", "condition": "failed"}
          ]
        }"""
        parent_json = """{
          "name": "parent-mutate",
          "description": "Parent recipe that mutates a file then runs child-check and auto-rolls back if child fails.",
          "inputs": {"mode": "fail"},
          "nodes": [
            {"id": "mutate_step", "type": "tool", "tool": "mutate_file", "args": {}},
            {"id": "nested_verify", "type": "recipe", "recipe": "child-check", "args": {"mode": "{{inputs.mode}}"}}
          ],
          "edges": [
            {"from": "mutate_step", "to": "nested_verify", "condition": "passed"},
            {"from": "nested_verify", "to": "COMPLETE", "condition": "passed"},
            {"from": "nested_verify", "to": "ABORTED", "condition": "failed"}
          ]
        }"""
        (wf_dir / "child-check.json").write_text(child_json, encoding="utf-8")
        (wf_dir / "parent-mutate.json").write_text(parent_json, encoding="utf-8")

        target_py = self.root / "sample.py"
        self.assertEqual(target_py.read_text(encoding="utf-8"), '"""Sample module."""\nx = 1\n')

        def fake_tool(name: str, args: dict) -> str:
            if name == "mutate_file":
                target_py.write_text('"""Sample module."""\nx = 999\n', encoding="utf-8")
                return "mutated sample.py"
            if args.get("mode") == "fail":
                return "error: verification failed in child recipe"
            return "ok: child verified"

        # 1. When nested recipe fails -> parent aborts AND automatically rolls back sample.py to x = 1!
        res_abort = self.engine.run_workflow_recipe("parent-mutate", params={"mode": "fail"}, tool_fn=fake_tool)
        self.assertEqual(res_abort["status"], "ABORTED")
        self.assertTrue(res_abort["checkpoint_id"].startswith("cp_"))
        self.assertTrue(res_abort["rolled_back"])
        self.assertEqual(target_py.read_text(encoding="utf-8"), '"""Sample module."""\nx = 1\n')

        # 2. When nested recipe passes -> parent completes and keeps x = 999
        res_ok = self.engine.run_workflow_recipe("parent-mutate", params={"mode": "pass"}, tool_fn=fake_tool)
        self.assertEqual(res_ok["status"], "COMPLETE")
        self.assertFalse(res_ok["rolled_back"])
        self.assertEqual(target_py.read_text(encoding="utf-8"), '"""Sample module."""\nx = 999\n')

        # 3. Running a recipe while a state-machine workflow is active preserves & restores the active workflow
        self.engine.start_workflow("Feature X", workflow_type="engineering_feature", acceptance_criteria="Tests pass")
        active_before = self.store.get_active_workflow()
        self.assertIsNotNone(active_before)
        res_child = self.engine.run_workflow_recipe("child-check", params={"mode": "pass"}, tool_fn=fake_tool)
        self.assertEqual(res_child["status"], "COMPLETE")
        active_after = self.store.get_active_workflow()
        self.assertIsNotNone(active_after)
        self.assertEqual(active_after["id"], active_before["id"])
        self.assertEqual(active_after["current_state"], "INTAKE")

    def test_manual_transition_during_active_recipe_returns_clean_result(self) -> None:
        self.store.create_workflow_run(
            workflow_type="recipe:full-audit",
            goal="Active recipe run",
            initial_state="subagent_repair",
        )
        res = self.engine.transition("COMPLETE", summary="Attempt manual transition inside recipe")
        self.assertFalse(res.success)
        self.assertEqual(res.from_state, "subagent_repair")
        self.assertIn("managed automatically by the recipe DAG engine", res.message)

    def test_hygiene_failure_detection_and_recipe_goal_param_mapping(self) -> None:
        from companion.workflow_graph import _is_node_output_passed

        self.assertFalse(_is_node_output_passed("=== Codebase Hygiene & Simplicity Report: HYGIENE ISSUES DETECTED (NEEDS SIMPLIFICATION) ==="))
        self.assertTrue(_is_node_output_passed("=== Codebase Hygiene & Simplicity Report: CLEAN (PASS) ==="))

        wf_dir = self.root / "skills" / "demo-skill" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        q_recipe = """{
          "name": "ask-flow",
          "description": "Verify CLI goal param maps to declared question input.",
          "inputs": {"question": "default question", "run_tests": "true"},
          "nodes": [
            {"id": "step_q", "type": "tool", "tool": "echo_q", "args": {"q": "{{inputs.question}}", "rt": "{{inputs.run_tests}}"}}
          ],
          "edges": [{"from": "step_q", "to": "COMPLETE", "condition": "passed"}]
        }"""
        (wf_dir / "ask-flow.json").write_text(q_recipe, encoding="utf-8")
        res = self.engine.run_workflow_recipe(
            "ask-flow",
            params={"goal": "Why did swap take 300s?"},
            tool_fn=lambda _n, a: f"q={a['q']}|rt={a['rt']}",
        )
        self.assertEqual(res["status"], "COMPLETE")
        self.assertEqual(res["steps"][0]["output"], "q=Why did swap take 300s?|rt=true")

        self.engine.start_workflow("AC update test", workflow_type="bug_fix", acceptance_criteria="Initial AC")
        self.engine.transition("PLAN", summary="Updated plan", acceptance_criteria="Refined AC")
        active = self.store.get_active_workflow()
        self.assertIsNotNone(active)
        self.assertEqual(active["acceptance_criteria"], "Refined AC")

    def test_goal_plan_sanitizes_unknown_duplicate_and_cyclic_dependencies(self) -> None:
        from companion.workflow_dag import GoalPlan, GoalStep, VerificationSpec

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
        from companion.grounding import LocalWorkspaceTarget, register_execution_target
        from companion.workflow_dag import VerificationSpec, evaluate_step_verification

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
