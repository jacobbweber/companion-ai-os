"""Unit & integration tests for Interactive Blueprint Approval (`companion/blueprint.py`) and Flight Eval (`companion/eval_harness.py`)."""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List
from unittest.mock import patch

from companion.agent import handle_slash_command, run_agent_turn, run_subagent
from companion.autonomy import resolve_workflow_phase_tier, run_autonomous_goal, run_phase_turn
from companion.blueprint import (
    approve_blueprint_and_build,
    classify_blueprint_reply,
    create_or_revise_blueprint,
    format_blueprint_card,
    is_awaiting_blueprint_approval,
)
from companion.compaction import classify_task_autoskip, format_task_carry_forward
from companion.config import SELF_PROJECT_ID, TierConfig, settings, workspace_context
from companion.eval_harness import run_live_flight_eval
from companion.memory_store import MemoryStore
from companion.safety import CheckpointManager
from surfaces import create_web_server, dispatch_raw_http, render_web_index_html


def _make_fake_bp_chat(phases_seen: List[str]) -> Callable[..., Dict[str, Any]]:
    """Build a deterministic fake LLM driver for Blueprint pause, revise, and approve testing."""

    def _driver(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
        user_ctx = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        is_tool_reply = messages[-1].get("role") == "tool"
        if "[AUTONOMOUS WORKFLOW PHASE: DISCOVER" in user_ctx:
            phases_seen.append("DISCOVER")
            return {"choices": [{"message": {"role": "assistant", "content": "Inspected workspace `bp_proj`."}}]}
        if "[AUTONOMOUS WORKFLOW PHASE: DESIGN" in user_ctx:
            phases_seen.append("DESIGN")
            return {"choices": [{"message": {"role": "assistant", "content": "Design: create `greeter.py` cleanly."}}]}
        if "[AUTONOMOUS WORKFLOW PHASE: PLAN" in user_ctx:
            phases_seen.append("PLAN")
            if not is_tool_reply:
                tc = {"id": f"tc_p_{len(phases_seen)}", "function": {"name": "task_manage", "arguments": json.dumps({"action": "add_task", "description": "Build greeter.py"})}}
                return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "Plan: 1 atomic task queued."}}]}
        if "[AUTONOMOUS WORKFLOW PHASE: IMPLEMENT" in user_ctx:
            phases_seen.append("IMPLEMENT")
            if not is_tool_reply:
                tc = {"id": "tc_i_1", "function": {"name": "write_file", "arguments": json.dumps({"path": "greeter.py", "content": '"""Greeter."""\ndef hi() -> str:\n    return "hello"\n'})}}
                return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "Implemented `greeter.py`."}}]}
        return {"choices": [{"message": {"role": "assistant", "content": "Done."}}]}

    return _driver


def _scripted_flight_f1_f2(user_ctx: str, messages: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """Handle Flight 1 (AST symbol read) and Flight 2 (surgical inline-linted edit)."""
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    if "InvoiceEngine.compute_fee" in user_ctx:
        if not tool_msgs:
            tc = {"id": "f1_r", "function": {"name": "read_file", "arguments": json.dumps({"path": "billing.py", "symbol": "InvoiceEngine.compute_fee"})}}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}
        return {"choices": [{"message": {"role": "assistant", "content": "It returns `FEE_CODE_9417`."}}]}
    if "clamp_score" in user_ctx:
        if len(tool_msgs) == 0:
            tc = {"id": "f2_r", "function": {"name": "read_file", "arguments": json.dumps({"path": "math_ops.py"})}}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}
        if len(tool_msgs) == 1:
            args = {
                "path": "math_ops.py",
                "old_str": "def add_pair(a: int, b: int) -> int:\n    return a + b",
                "new_str": "def add_pair(a: int, b: int) -> int:\n    return a + b\n\n\ndef clamp_score(val: int, lo: int = 0, hi: int = 100) -> int:\n    return max(lo, min(hi, val))",
            }
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "f2_e", "function": {"name": "edit_file", "arguments": json.dumps(args)}}]}}]}
        return {"choices": [{"message": {"role": "assistant", "content": "Added `clamp_score` cleanly."}}]}
    return None


def _scripted_flight_model(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
    """Deterministic multi-flight LLM mock covering Flights 1, 2, 3, and `compare` coding race."""
    user_ctx = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
    f12 = _scripted_flight_f1_f2(user_ctx, messages)
    if f12 is not None:
        return f12
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    if "merge_intervals" in user_ctx and "[AUTONOMOUS WORKFLOW PHASE: IMPLEMENT" in user_ctx:
        if not tool_msgs and "Execute Task #1:" in user_ctx:
            iv_src = (
                '"""Intervals."""\n\ndef merge_intervals(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:\n'
                "    if not ranges:\n        return []\n"
                "    ordered = sorted((int(a), int(b)) for a, b in ranges)\n"
                "    merged = [ordered[0]]\n"
                "    for lo, hi in ordered[1:]:\n"
                "        if lo <= merged[-1][1]:\n            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))\n"
                "        else:\n            merged.append((lo, hi))\n"
                "    return merged\n"
            )
            iv_tst = (
                '"""Tests."""\nimport unittest\nfrom intervals import merge_intervals\n\n'
                "class TestIntervals(unittest.TestCase):\n    def test_merge(self) -> None:\n"
                "        self.assertEqual(merge_intervals([(8, 10), (1, 3), (2, 6)]), [(1, 6), (8, 10)])\n"
                "        self.assertEqual(merge_intervals([(-5, -1), (-2, 3)]), [(-5, 3)])\n"
                "        self.assertEqual(merge_intervals([]), [])\n"
            )
            tcs = [
                {"id": "c_w1", "function": {"name": "write_file", "arguments": json.dumps({"path": "intervals.py", "content": iv_src})}},
                {"id": "c_w2", "function": {"name": "write_file", "arguments": json.dumps({"path": "tests/test_intervals.py", "content": iv_tst})}},
            ]
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": tcs}}]}
        return {"choices": [{"message": {"role": "assistant", "content": "Verified `intervals.py` and tests."}}]}
    if "[AUTONOMOUS WORKFLOW PHASE: PLAN" in user_ctx and not tool_msgs:
        tc = {"id": "f3_t", "function": {"name": "task_manage", "arguments": json.dumps({"action": "add_task", "description": "Create stats_box.py and tests"})}}
        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}
    if "[AUTONOMOUS WORKFLOW PHASE: IMPLEMENT" in user_ctx and not tool_msgs:
        mod_src = (
            '"""Stats."""\n\ndef summarize_numbers(values: list[float]) -> dict:\n'
            '    if not values:\n        raise ValueError("empty")\n'
            '    return {"count": len(values), "min": min(values), "max": max(values), "mean": sum(values) / len(values)}\n'
        )
        tst_src = (
            '"""Tests."""\nimport unittest\nfrom stats_box import summarize_numbers\n\n'
            "class TestStatsBox(unittest.TestCase):\n    def test_summary_and_empty(self) -> None:\n"
            '        self.assertEqual(summarize_numbers([2.0, 4.0])["mean"], 3.0)\n'
            "        with self.assertRaises(ValueError):\n            summarize_numbers([])\n"
        )
        tcs = [
            {"id": "f3_w1", "function": {"name": "write_file", "arguments": json.dumps({"path": "stats_box.py", "content": mod_src})}},
            {"id": "f3_w2", "function": {"name": "write_file", "arguments": json.dumps({"path": "tests/test_stats_box.py", "content": tst_src})}},
        ]
        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": tcs}}]}
    return {"choices": [{"message": {"role": "assistant", "content": "Phase completed."}}]}


class TestBlueprintAndFlightEval(unittest.TestCase):
    def test_blueprint_pause_revise_and_natural_language_approve(self) -> None:
        """Verify `/plan` pauses read-only at `PLAN`, `/plan revise` updates tasks, and natural-language `approve` builds."""
        self.assertEqual(classify_blueprint_reply("Looks great, go ahead and build it!"), "APPROVE")
        self.assertEqual(classify_blueprint_reply("Cancel the blueprint"), "ABORT")
        self.assertIsNone(classify_blueprint_reply("Wait, does this touch config.py?"))

        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp) / "bp_proj"
            ws_root.mkdir()
            store = MemoryStore(db_path=Path(tmp) / "bp.db")
            cp_mgr = CheckpointManager(root=ws_root, backup_dir=ws_root / ".companion_backups", store=store)
            phases_seen: List[str] = []
            fake_bp_chat = _make_fake_bp_chat(phases_seen)
            try:
                with workspace_context(ws_root, "bp-proj"):
                    bp1 = create_or_revise_blueprint("Create a simple `greeter.py` module.", chat_fn=fake_bp_chat, store=store, cp_mgr=cp_mgr, root=ws_root, verbose=False)
                    self.assertTrue(bp1["awaiting_approval"])
                    self.assertTrue(is_awaiting_blueprint_approval(store))
                    self.assertIn("Blueprint Ready for Your Review", bp1["summary"])
                    self.assertFalse((ws_root / "greeter.py").exists())
                    self.assertEqual(phases_seen, ["DISCOVER", "DESIGN", "PLAN", "PLAN"])

                    bp2 = create_or_revise_blueprint("revise Keep `hi()` ultra concise.", chat_fn=fake_bp_chat, store=store, cp_mgr=cp_mgr, root=ws_root, verbose=False)
                    self.assertTrue(bp2["awaiting_approval"])
                    self.assertIn("Revised Blueprint Ready for Your Review", bp2["summary"])
                    self.assertNotIn("DISCOVER", phases_seen[4:])

                    with patch("companion.autonomy.run_unit_tests", return_value={"passed": True, "ran": 1, "output": "OK"}):
                        reply = run_agent_turn([{"role": "user", "content": "Looks great, go ahead and build it!"}], chat_fn=fake_bp_chat, store=store, verbose=False)
                    self.assertIn("Autonomous Goal Execution: COMPLETE", reply)
                    self.assertTrue((ws_root / "greeter.py").is_file())
                    self.assertFalse(is_awaiting_blueprint_approval(store))
            finally:
                store.close()

    def test_web_ui_plan_first_mode_and_approval_banner(self) -> None:
        """Verify Web UI `mode='plan'`, `awaiting_approval` status flag, and `/approve` execution."""
        html = render_web_index_html()
        self.assertIn('id="approval-banner"', html)
        self.assertIn("Plan First: OFF", html)
        self.assertIn("Approve &amp; Build", html)

        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp) / "web_bp"
            ws_root.mkdir()
            store = MemoryStore(db_path=Path(tmp) / "web_bp.db")
            fake_web_chat = _make_fake_bp_chat([])
            srv, _ = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=fake_web_chat)
            try:
                with workspace_context(ws_root, "web-bp"):
                    code, raw = dispatch_raw_http(srv, method="POST", path="/api/chat", body=json.dumps({"message": "Build widget module", "mode": "plan", "async": False}).encode("utf-8"))
                    self.assertEqual(code, 200)
                    self.assertTrue(json.loads(raw)["awaiting_approval"])
                    _, st_raw = dispatch_raw_http(srv, method="GET", path="/api/status")
                    self.assertTrue(json.loads(st_raw)["awaiting_approval"])

                    with patch("companion.autonomy.run_unit_tests", return_value={"passed": True, "ran": 1, "output": "OK"}):
                        code2, raw2 = dispatch_raw_http(srv, method="POST", path="/api/chat", body=json.dumps({"message": "/approve", "async": False}).encode("utf-8"))
                    self.assertEqual(code2, 200)
                    self.assertIn("COMPLETE", json.loads(raw2)["response"])
                    _, st_raw2 = dispatch_raw_http(srv, method="GET", path="/api/status")
                    self.assertFalse(json.loads(st_raw2)["awaiting_approval"])
            finally:
                srv.server_close()
                store.close()

    def test_live_flight_eval_harness_runs_all_three_flights_in_isolated_workspace(self) -> None:
        """Verify `run_live_flight_eval` executes all 3 flights cleanly and restores `companion-self`."""
        res = run_live_flight_eval(suite="full", chat_fn=_scripted_flight_model, check_endpoints=False, verbose=False)
        self.assertTrue(res["passed"], res["summary"])
        self.assertEqual(len(res["flights"]), 3)
        self.assertEqual(settings.get_workspace().project_id, SELF_PROJECT_ID)

    def test_carry_forward_autoskip_coding_tier_and_git_savepoint(self) -> None:
        """Verify Carry-Forward ledger, 0-LLM auto-skip on trailing test task, `coding` tier routing, and Git save-points."""
        prev_coding = settings.tiers["coding"]
        try:
            self.assertEqual(resolve_workflow_phase_tier("IMPLEMENT"), "fast")
            settings.tiers["coding"] = TierConfig("coding", "http://127.0.0.1:11434", "qwen3-coder:latest", "", 131072, 0.15, True)
            self.assertEqual(resolve_workflow_phase_tier("IMPLEMENT"), "coding")
        finally:
            settings.tiers["coding"] = prev_coding

        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp) / "git_cf_ws"
            ws_root.mkdir()
            (ws_root / ".env").write_text("SECRET_KEY=never_commit_me\n", encoding="utf-8")
            (ws_root / "app.py").write_text('"""App."""\ndef ok() -> bool:\n    return True\n', encoding="utf-8")
            cp_mgr = CheckpointManager(root=ws_root, backup_dir=ws_root / ".companion_backups")

            cf_text = format_task_carry_forward(ws_root, ["app.py"], ["Task #1 [VERIFIED]: Create app.py"])
            self.assertIn("[WORK ALREADY COMPLETED IN THIS WORKFLOW]", cf_text)
            self.assertIn("`app.py` (3 lines on disk, verified)", cf_text)
            self.assertEqual(classify_task_autoskip("Run full verification via unittest discover", ["app.py"], ws_root), (True, False))
            self.assertEqual(classify_task_autoskip("Inspect app.py docstring", ["app.py"], ws_root), (False, True))

            save_res = cp_mgr.create_git_savepoint("Initial verified build", init_if_needed=True)
            if save_res.get("reason") != "git_unavailable":
                self.assertTrue(save_res["committed"], save_res)
                logs = cp_mgr.list_git_savepoints(limit=5)
                self.assertGreaterEqual(len(logs), 1)
                self.assertIn("[Companion] Initial verified build", logs[0]["subject"])
                _, tracked = cp_mgr._run_git(["ls-files"])
                self.assertNotIn(".env", tracked.splitlines())
                with workspace_context(ws_root, "git-cf-ws"):
                    slash_log = handle_slash_command("/checkpoint log", [{"role": "system", "content": "sys"}])
                    self.assertIn("Initial verified build", str(slash_log))

        cmp_res = run_live_flight_eval(suite="compare", chat_fn=_scripted_flight_model, check_endpoints=False, verbose=False)
        self.assertTrue(cmp_res["passed"], cmp_res["summary"])
        self.assertGreaterEqual(cmp_res["candidates"][0]["auto_skipped_tasks"], 1)

    def test_autonomous_goal_injects_carry_forward_and_autoskips_trailing_verify(self) -> None:
        """Verify `_execute_implement_tasks` injects `[WORK ALREADY COMPLETED]` on Task #2 and skips pure-verify Task #3."""
        prompts_seen: List[str] = []

        def _cf_driver(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
            u_text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
            tool_msgs = [m for m in messages if m.get("role") == "tool"]
            if "[AUTONOMOUS WORKFLOW PHASE: PLAN" in u_text and not tool_msgs:
                tcs = [
                    {"id": "p1", "function": {"name": "task_manage", "arguments": json.dumps({"action": "add_task", "description": "Create calc.py and tests/test_calc.py"})}},
                    {"id": "p2", "function": {"name": "task_manage", "arguments": json.dumps({"action": "add_task", "description": "Review calc.py edge cases"})}},
                    {"id": "p3", "function": {"name": "task_manage", "arguments": json.dumps({"action": "add_task", "description": "Run full verification suite via unittest"})}},
                ]
                return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": tcs}}]}
            if "[AUTONOMOUS WORKFLOW PHASE: IMPLEMENT" in u_text:
                if not tool_msgs:
                    prompts_seen.append(u_text)
                    if "Execute Task #1" in u_text:
                        tcs = [
                            {"id": "w1", "function": {"name": "write_file", "arguments": json.dumps({"path": "calc.py", "content": '"""Calc."""\ndef double(x: int) -> int:\n    return x * 2\n'})}},
                            {"id": "w2", "function": {"name": "write_file", "arguments": json.dumps({"path": "tests/test_calc.py", "content": '"""T."""\nimport unittest\nfrom calc import double\nclass T(unittest.TestCase):\n    def test_d(self) -> None:\n        self.assertEqual(double(3), 6)\n'})}},
                        ]
                        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": tcs}}]}
                return {"choices": [{"message": {"role": "assistant", "content": "Done."}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "OK."}}]}

        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp) / "cf_goal"
            ws_root.mkdir()
            store = MemoryStore(db_path=Path(tmp) / "cf.db")
            cp_mgr = CheckpointManager(root=ws_root, backup_dir=ws_root / ".companion_backups", store=store)
            try:
                with workspace_context(ws_root, "cf-goal"):
                    res = run_autonomous_goal("Build calc.py", chat_fn=_cf_driver, store=store, cp_mgr=cp_mgr, root=ws_root, verbose=False, auto_git_commit=True)
                self.assertTrue(res["passed"], res["summary"])
                self.assertEqual(len(prompts_seen), 2)  # Task #3 auto-verified in Python with 0 LLM calls!
                self.assertIn("[WORK ALREADY COMPLETED IN THIS WORKFLOW]", prompts_seen[1])
                self.assertIn("`calc.py`", prompts_seen[1])
            finally:
                store.close()

    def test_subagent_reasoning_tier_preservation_error_escalation_and_blueprint_dag_card(self) -> None:
        """Verify subagent `reasoning` tier preservation, autonomous IMPLEMENT error escalation, and Blueprint DAG cards."""
        from companion.llm import current_tier_name

        dag_card = format_blueprint_card(
            wf_id=7, goal="Deploy config safely", cp_id="cp_7", design_handoff="Design", plan_handoff="Plan",
            pending_tasks=[],
            goal_dag={"steps": [
                {"step_id": "s1", "title": "Draft config", "risk_tier": "R1", "verification": {"kind": "evidence_file"}},
                {"step_id": "s2", "title": "Remote push", "risk_tier": "R3", "verification": {"kind": "human_gate"}},
            ]},
        )
        self.assertIn("Step `s1` | R1 | `evidence_file`", dag_card)
        self.assertIn("Step `s2` | R3 | `human_gate`", dag_card)

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "sub_tier.db")
            try:
                sub_tiers: List[str] = []

                def _sub_chat(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                    sub_tiers.append(current_tier_name())
                    if len(sub_tiers) == 1:
                        tc = {"id": "s_tc1", "function": {"name": "list_dir", "arguments": '{"path": "."}'}}
                        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}
                    return {"choices": [{"message": {"role": "assistant", "content": "Reasoning subagent complete."}}]}

                out = run_subagent("Analyze architecture tradeoffs", max_iters=4, chat_fn=_sub_chat, store=store, verbose=False, tier="reasoning")
                self.assertIn("Reasoning subagent complete", out)
                self.assertEqual(sub_tiers, ["reasoning", "reasoning"])

                # Autonomous IMPLEMENT starts on `fast`, then escalates to `reasoning` after 2 consecutive tool errors
                impl_tiers: List[str] = []

                def _impl_err_chat(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                    impl_tiers.append(current_tier_name())
                    if len(impl_tiers) <= 2:
                        bad_tc = {"id": f"bad_{len(impl_tiers)}", "function": {"name": "read_file", "arguments": "{}"}}
                        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [bad_tc]}}]}
                    return {"choices": [{"message": {"role": "assistant", "content": "Recovered on reasoning tier."}}]}

                with patch.object(settings, "tiers_share_endpoint", return_value=True):
                    res_impl = run_phase_turn("IMPLEMENT", "Fix broken step", 4, _impl_err_chat, store, False)
                self.assertIn("Recovered on reasoning tier", res_impl)
                self.assertEqual(impl_tiers, ["fast", "fast", "reasoning"])

                # Manual `/model fast` user pin stays `fast` even on consecutive errors
                store.set_user_profile("active_model_tier", "fast")
                pinned_tiers: List[str] = []

                def _pin_chat(_messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                    pinned_tiers.append(current_tier_name())
                    if len(pinned_tiers) <= 2:
                        bad_tc = {"id": f"p_{len(pinned_tiers)}", "function": {"name": "read_file", "arguments": "{}"}}
                        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [bad_tc]}}]}
                    return {"choices": [{"message": {"role": "assistant", "content": "Done."}}]}

                run_agent_turn([{"role": "user", "content": "Quick check"}], max_iters=4, chat_fn=_pin_chat, store=store, verbose=False, auto_promote=False)
                self.assertEqual(pinned_tiers, ["fast", "fast", "fast"])
            finally:
                store.close()

    def test_concurrent_subagent_depth_isolation_sequential_gates_and_recover_dag_unlock(self) -> None:
        """Verify thread-isolated `_SUBAGENT_DEPTH`, distinct `human_gate`->`R3` approvals, and post-`RECOVER` DAG continuation."""
        import threading

        from companion.memory_store import memory_store_context
        from companion.workflow_dag import GoalPlan, GoalStep, VerificationSpec
        from companion.workflow_graph import WorkflowEngine

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(db_path=root / "conc.db")
            barrier = threading.Barrier(2)
            nested_results: List[str] = []
            depth3_results: List[str] = []
            try:
                def _worker_thread(idx: int) -> None:
                    def _chat_lvl1(_msgs: List[Dict[str, Any]], _t=None, _c="auto") -> Dict[str, Any]:
                        barrier.wait(timeout=5.0)

                        def _chat_lvl2(_m2: List[Dict[str, Any]], _t2=None, _c2="auto") -> Dict[str, Any]:
                            d3 = run_subagent("Level 3 blocked", chat_fn=lambda *a, **k: {}, store=store, verbose=False)
                            depth3_results.append(d3)
                            return {"choices": [{"message": {"role": "assistant", "content": f"lvl2-ok-{idx}"}}]}

                        r2 = run_subagent(f"Nested-{idx}", chat_fn=_chat_lvl2, store=store, verbose=False)
                        nested_results.append(r2)
                        return {"choices": [{"message": {"role": "assistant", "content": f"lvl1-ok-{idx}"}}]}

                    run_subagent(f"Outer-{idx}", chat_fn=_chat_lvl1, store=store, verbose=False)

                threads = [threading.Thread(target=_worker_thread, args=(i,)) for i in range(2)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10.0)
                self.assertEqual(sorted(nested_results), ["lvl2-ok-0", "lvl2-ok-1"])
                self.assertTrue(all("maximum subagent nesting depth (2)" in x for x in depth3_results))

                ws = root / "gate_ws"
                ws.mkdir()
                cp = CheckpointManager(root=ws, backup_dir=ws / ".companion_backups", store=store)
                with workspace_context(ws, "gate-proj"), memory_store_context(store):
                    wf = WorkflowEngine(store=store, cp_manager=cp, root=ws)
                    # 1. human_gate (s1) followed by R3 (s2) requires TWO distinct approvals
                    sr = wf.start_workflow("Human gate then R3")
                    for ph in ("DISCOVER", "DESIGN", "PLAN"):
                        wf.transition(ph, ph, run_tests_on_verify=False)
                    dag1 = GoalPlan("Human gate then R3", [
                        GoalStep("s1", "Confirm maintenance window", [], "R1", VerificationSpec("human_gate")),
                        GoalStep("s2", "Execute R3 push", ["s1"], "R3", VerificationSpec("evidence_file", target="done.json")),
                    ])
                    store.update_workflow_metadata(sr.workflow_id, {"discovery_handoff": "d", "design_handoff": "d", "plan_handoff": "p", "goal_dag": dag1.to_dict(), "awaiting_plan_approval": True})

                    def _gate_chat(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                        u = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
                        if "Execute R3 push" in u and messages[-1].get("role") != "tool":
                            tc = {"id": "w_done", "function": {"name": "write_file", "arguments": json.dumps({"path": "done.json", "content": '{"ok": true}\n'})}}
                            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}
                        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

                    ap1 = approve_blueprint_and_build(chat_fn=_gate_chat, store=store, cp_mgr=cp, root=ws, verbose=False)
                    self.assertEqual(ap1["status"], "AWAITING_APPROVAL")
                    self.assertIn("s2", ap1["pause_reason"])
                    ap2 = approve_blueprint_and_build(chat_fn=_gate_chat, store=store, cp_mgr=cp, root=ws, verbose=False)
                    self.assertEqual(ap2["status"], "COMPLETE")

                    # 2. Recovering blocked s1 unlocks dependent R1 step s2 and completes workflow
                    sr_rec = wf.start_workflow("Recover unlocks s2")
                    for ph in ("DISCOVER", "DESIGN", "PLAN"):
                        wf.transition(ph, ph, run_tests_on_verify=False)
                    dag2 = GoalPlan("Recover unlocks s2", [
                        GoalStep("s1", "Write step1.json", [], "R1", VerificationSpec("evidence_file", target="step1.json")),
                        GoalStep("s2", "Write step2.json", ["s1"], "R1", VerificationSpec("evidence_file", target="step2.json")),
                    ])
                    store.update_workflow_metadata(sr_rec.workflow_id, {"discovery_handoff": "d", "design_handoff": "d", "plan_handoff": "p", "goal_dag": dag2.to_dict()})

                    def _rec_dag_chat(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                        u = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
                        is_tool = messages[-1].get("role") == "tool"
                        if "[AUTONOMOUS WORKFLOW PHASE: RECOVER" in u and not is_tool:
                            tc = {"id": "r_s1", "function": {"name": "write_file", "arguments": json.dumps({"path": "step1.json", "content": '{"step": 1}\n'})}}
                            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}
                        if "Write step2.json" in u and not is_tool:
                            tc = {"id": "i_s2", "function": {"name": "write_file", "arguments": json.dumps({"path": "step2.json", "content": '{"step": 2}\n'})}}
                            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tc]}}]}
                        return {"choices": [{"message": {"role": "assistant", "content": "Missed step1 on first pass."}}]}

                    fin_rec = run_autonomous_goal("resume", chat_fn=_rec_dag_chat, store=store, cp_mgr=cp, root=ws, verbose=False, resume=True)
                    self.assertEqual(fin_rec["status"], "COMPLETE", fin_rec["summary"])
                    self.assertTrue((ws / "step1.json").is_file() and (ws / "step2.json").is_file())
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
