"""Unit tests for Upgrades 1-6 & 8b: Headroom, Cognitive Continuity, AST Grounding, Inline Linter, Phase Gates & Autonomy."""

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

from companion import __doc__ as companion_pkg_doc
from companion.agent import handle_slash_command, run_agent_turn
from companion.autonomy import (
    IntentDecision,
    _evaluate_hard_verification,
    _snapshot_file_hashes,
    classify_turn_intent,
    run_autonomous_goal,
)
from companion.compaction import build_lossless_phase_handoff
from companion.config import SKIP_DIRS, Settings, settings, walk_pruned_files, workspace_context
from companion.context import build_system_prompt
from companion.llm import current_tier_name, normalize_assistant_message, rescue_text_tool_calls
from companion.memory_store import MemoryStore, memory_store_context
from companion.safety import CheckpointManager, lint_single_file_edit
from companion.tool_schemas import TOOLS, check_phase_tool_permission, filter_tools_for_phase, phase_tool_context
from companion.tools import dispatch, extract_python_symbol, tool_edit_file, tool_read_file, tool_write_file


class TestCognitiveAndGroundingUpgrades(unittest.TestCase):
    def test_upgrade2_per_tier_temperature_tool_call_rescue_and_rolling_scratchpad(self) -> None:
        """Verify per-tier temperature defaults, JSON/Qwen XML `<tool_call>` rescue, and 1-step `<think>` continuity."""
        saved_ft, saved_rt = os.environ.pop("COMPANION_FAST_TEMP", None), os.environ.pop("COMPANION_REASONING_TEMP", None)
        try:
            cfg = Settings(reload_env=False)
            self.assertAlmostEqual(cfg.resolve_tier("fast").temperature, 0.15)
            self.assertAlmostEqual(cfg.resolve_tier("reasoning").temperature, 0.60)
        finally:
            if saved_ft is not None:
                os.environ["COMPANION_FAST_TEMP"] = saved_ft
            if saved_rt is not None:
                os.environ["COMPANION_REASONING_TEMP"] = saved_rt

        raw_qwen = (
            "<think>Need to inspect config first.</think>"
            "<tool_call><function=read_file><parameter=path>companion/config.py</parameter><parameter=end_line>5</parameter></function></tool_call>"
        )
        cleaned, calls = rescue_text_tool_calls(raw_qwen)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertNotIn("<tool_call>", cleaned)

        msg_json = {
            "role": "assistant",
            "content": '<think>Step-1 plan: check root files then summarize.</think><tool_call>{"name": "list_dir", "arguments": {"path": "specs"}}</tool_call>',
        }
        c_txt, t_txt, norm_calls = normalize_assistant_message(msg_json)
        self.assertEqual(c_txt, "")
        self.assertIn("Step-1 plan", t_txt)
        self.assertEqual(norm_calls[0]["function"]["name"], "list_dir")

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "scratch.db")
            seen_sys_prompts: List[str] = []
            seen_tail_msgs: List[str] = []
            step_n = {"n": 0}

            def fake_chat(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                seen_sys_prompts.append(str(messages[0]["content"]))
                seen_tail_msgs.append(str(messages[-1].get("content", "")))
                step_n["n"] += 1
                if step_n["n"] == 1:
                    return {"choices": [{"message": dict(msg_json)}]}
                return {"choices": [{"message": {"role": "assistant", "content": "Verified specs directory cleanly."}}]}

            msgs: List[Dict[str, Any]] = [{"role": "user", "content": "Check specs folder"}]
            ans = run_agent_turn(msgs, max_iters=3, chat_fn=fake_chat, store=store, verbose=False)
            self.assertEqual(ans, "Verified specs directory cleanly.")
            self.assertEqual(len(seen_sys_prompts), 2)
            self.assertEqual(seen_sys_prompts[0], seen_sys_prompts[1])
            self.assertNotIn("[PRIOR STEP WORKING SCRATCHPAD]", seen_sys_prompts[1])
            self.assertIn("[PRIOR STEP WORKING SCRATCHPAD]", seen_tail_msgs[1])
            self.assertIn("Step-1 plan: check root files", seen_tail_msgs[1])
            store.close()

    def test_upgrade3_workspace_radar_and_symbol_read_file(self) -> None:
        """Verify workspace radar and surgical `read_file(..., symbol=...)` slicing."""
        from companion.grounding import build_workspace_radar

        radar = build_workspace_radar(settings.root)
        self.assertIn("companion/config.py", radar)

        with tempfile.TemporaryDirectory() as tmp:
            ext_repo = Path(tmp) / "ext_repo"
            (ext_repo / "companion").mkdir(parents=True)
            (ext_repo / "companion" / "mod.py").write_text('"""Mod."""\nx = 1\n', encoding="utf-8")
            store = MemoryStore(db_path=Path(tmp) / "radar.db")
            try:
                with workspace_context(settings.root, "companion-self"):
                    runtime_p = build_system_prompt(store=store)
                    self.assertNotIn("[CODEBASE AST & LINE-BUDGET RADAR", runtime_p)
                with workspace_context(ext_repo, "ext-repo"):
                    sys_p = build_system_prompt(store=store)
                    self.assertIn("[CODEBASE AST & LINE-BUDGET RADAR", sys_p)
            finally:
                store.close()

            sample = Path(tmp) / "demo_sym.py"
            sample.write_text(
                '"""Demo."""\n\nclass Engine:\n    def start(self) -> str:\n        return "up"\n\ndef helper_fn() -> int:\n    return 42\n',
                encoding="utf-8",
            )
            span, avail = extract_python_symbol(sample.read_text(encoding="utf-8"), "Engine.start")
            self.assertEqual(span, (4, 5, "method"))
            self.assertIn("helper_fn", avail)

            sliced = tool_read_file({"path": str(sample), "symbol": "Engine.start"})
            self.assertIn("symbol `Engine.start` [method], lines 4-5", sliced)
            self.assertIn("def start(self)", sliced)
            self.assertNotIn("def helper_fn", sliced)

            missing = tool_read_file({"path": str(sample), "symbol": "nonexistent_sym"})
            self.assertIn("error: symbol `nonexistent_sym` not found", missing)
            self.assertIn("Engine.start", missing)

    def test_upgrade4_inline_post_edit_syntax_guard(self) -> None:
        """Verify inline syntax guard rejects invalid Python/JSON while allowing clean edits."""
        with tempfile.TemporaryDirectory() as tmp:
            gov = Path(tmp)
            err_bad, _ = lint_single_file_edit(gov / "bad.py", "def broken(:\n", governed_root=gov)
            self.assertIn("SYNTAX GUARD REJECTED", str(err_bad))

            target_py = Path(tmp) / "warn_demo.py"
            res_w = tool_write_file({"path": str(target_py), "content": '"""Demo."""\nimport math\n\nx = 1\n'})
            self.assertIn("wrote", res_w)

            res_e = tool_edit_file({"path": str(target_py), "old_str": "import math\n\n", "new_str": ""})
            self.assertIn("edited", res_e)

    def test_upgrade5_rich_tool_parameter_schemas_and_phase_tool_gating(self) -> None:
        """Verify every parameter across all tools has a description and `DESIGN`/`PLAN` lock out file writes."""
        self.assertGreaterEqual(len(TOOLS), 10)
        for tool_spec in TOOLS:
            fn_name = tool_spec["function"]["name"]
            props = tool_spec["function"]["parameters"]["properties"]
            for param_name, p_schema in props.items():
                desc = str(p_schema.get("description") or "").strip()
                self.assertGreaterEqual(len(desc), 10, f"Tool `{fn_name}` param `{param_name}` lacks description")

        design_tools = {t["function"]["name"] for t in filter_tools_for_phase("DESIGN")}
        self.assertIn("read_file", design_tools)
        self.assertNotIn("write_file", design_tools)
        self.assertNotIn("edit_file", design_tools)

        impl_tools = {t["function"]["name"] for t in filter_tools_for_phase("IMPLEMENT")}
        self.assertIn("write_file", impl_tools)
        self.assertIn("edit_file", impl_tools)

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "blocked.py"
            with phase_tool_context("PLAN"):
                self.assertIsNotNone(check_phase_tool_permission("write_file"))
                out = dispatch("write_file", {"path": str(p), "content": '"""No."""\nx = 1\n'})
                self.assertIn("PHASE TOOL GATE REJECTED", out)
                self.assertFalse(p.exists())

    def test_upgrade6_intent_router_lossless_handoffs_and_goal_resume(self) -> None:
        """Verify Step-0 intent classification, lossless `KeyTokens` phase handoff, and `/goal resume`."""
        d_chat = classify_turn_intent("Hey Companion, how are you doing today?")
        self.assertIsInstance(d_chat, IntentDecision)
        self.assertEqual(d_chat.mode, "CHAT_DIRECT")

        d_consult = classify_turn_intent("Before we do that, do a complete 360 architecture review and compare tradeoffs.")
        self.assertEqual(d_consult.mode, "DEEP_CONSULT")
        self.assertEqual(d_consult.suggested_tier, "reasoning")

        d_build = classify_turn_intent(
            "Build and implement a complete mobile web front-end UI with REST API endpoints, unit tests and specs end-to-end."
        )
        self.assertEqual(d_build.mode, "AUTONOMOUS_BUILD")

        # Deep consult routes Step 0 to reasoning on both multi-GPU and single-GPU endpoints (ADR-014)
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "intent.db")
            try:
                seen_tiers: List[str] = []
                fake_consult = lambda _m, _t=None, _c="auto": (seen_tiers.append(current_tier_name()) or {"choices": [{"message": {"role": "assistant", "content": "360 review summary."}}]})
                prompt_msgs = [{"role": "user", "content": "Do a 360 architecture review and evaluate tradeoffs between options."}]
                with patch.object(settings, "tiers_share_endpoint", return_value=False):
                    run_agent_turn(list(prompt_msgs), max_iters=2, chat_fn=fake_consult, store=store, verbose=False)
                with patch.object(settings, "tiers_share_endpoint", return_value=True):
                    run_agent_turn(list(prompt_msgs), max_iters=2, chat_fn=fake_consult, store=store, verbose=False)
                self.assertEqual(seen_tiers, ["reasoning", "reasoning"])
            finally:
                store.close()

        # Lossless >2000-char phase handoff preserves tail/middle KeyTokens
        long_disc = (
            "Header analysis. "
            + ("filler text " * 250)
            + " Touch `surfaces/web_server.py` and `companion/tool_schemas.py` for `ADR-011` and `SPEC-06` on 192.168.1.42:8000. "
            + ("more context " * 250)
        )
        handoff = build_lossless_phase_handoff("DISCOVER", long_disc, max_chars=1200)
        for token in ("surfaces/web_server.py", "companion/tool_schemas.py", "ADR-011", "SPEC-06", "192.168.1.42"):
            self.assertIn(token, handoff)

        # /goal resume skips already-saved DISCOVER/DESIGN/PLAN handoffs and finishes cleanly
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(db_path=root / "memory" / "resume.db")
            try:
                cp_mgr = CheckpointManager(root=root, backup_dir=root / ".companion_backups", store=store)
                wf_id = store.create_workflow_run(
                    "engineering_feature",
                    "Build resumable widget",
                    "PLAN",
                    acceptance_criteria="Pass doctor",
                    checkpoint_id="cp_init",
                )
                store.update_workflow_state(wf_id, "PLAN", "ABORTED", "Simulated interruption", status="aborted")
                store.update_workflow_metadata(
                    wf_id,
                    {
                        "discovery_handoff": "Saved discovery notes",
                        "design_handoff": "Saved design notes",
                        "plan_handoff": "Saved plan notes",
                    },
                )
                phases_called: List[str] = []

                def fake_resume_chat(messages: List[Dict[str, Any]], _t=None, _c="auto") -> Dict[str, Any]:
                    phases_called.append(str(messages[-1]["content"])[:55])
                    return {"choices": [{"message": {"role": "assistant", "content": "Implemented widget cleanly."}}]}

                with patch("companion.autonomy.run_system_doctor", return_value={"healthy": True, "checks": [], "summary": "PASS"}):
                    res = run_autonomous_goal(
                        goal="resume",
                        chat_fn=fake_resume_chat,
                        store=store,
                        cp_mgr=cp_mgr,
                        root=root,
                        run_doctor_tests=False,
                        verbose=False,
                    )
                self.assertTrue(res["passed"])
                self.assertEqual(len(phases_called), 1)
                self.assertIn("PHASE: IMPLEMENT", phases_called[0])
                slash_help = handle_slash_command("/help", [], store=store)
                self.assertIn("/goal <obj|resume>", str(slash_help))
            finally:
                store.close()

    def test_read_only_phase_memory_action_locks_and_reflect_exceptions(self) -> None:
        """Verify mutating `memory_manage` actions are locked in read-only phases and selectively open in `REFLECT`."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "phase_locks_ws"
            ws.mkdir()
            store = MemoryStore(db_path=Path(tmp) / "phase_locks.db")
            try:
                with workspace_context(ws, "locks-proj"), memory_store_context(store):
                    for ph in ("INTAKE", "DISCOVER", "DESIGN", "PLAN", "VERIFY"):
                        with phase_tool_context(ph):
                            for act in ("add_note", "set_profile", "add_adr", "upsert_fact", "sync", "  UPSERT_FACT  "):
                                res = dispatch("memory_manage", {"action": act, "key": "k", "value": "v", "content": "n", "title": "ADR-X", "decision": "d"})
                                self.assertIn("PHASE TOOL GATE REJECTED", res)
                            self.assertIsNotNone(dispatch("memory_manage", {"action": "query_facts", "query": "k"}))
                    self.assertEqual(store.query_knowledge_facts("k"), [])

                    with phase_tool_context("REFLECT"):
                        ok_fact = dispatch("memory_manage", {"action": "upsert_fact", "key": "reflect:lesson", "value": "verified", "source": "reflect"})
                        ok_note = dispatch("memory_manage", {"action": "add_note", "content": "Reflect lesson recorded", "category": "reflect"})
                        self.assertIn("reflect:lesson", ok_fact)
                        self.assertIn("Saved system note", ok_note)
                        self.assertIn("PHASE TOOL GATE REJECTED", dispatch("memory_manage", {"action": "sync"}))
                        self.assertIn("PHASE TOOL GATE REJECTED", dispatch("memory_manage", {"action": "set_profile", "key": "x", "value": "y"}))
                        self.assertIn("PHASE TOOL GATE REJECTED", dispatch("memory_manage", {"action": "add_adr", "title": "T", "decision": "D"}))
                    self.assertEqual(store.query_knowledge_facts("reflect:lesson")[0]["value"], "verified")
            finally:
                store.close()

    def test_pruned_os_walk_skips_ignored_dirs_in_o1(self) -> None:
        """Verify `walk_pruned_files` and hash/hard-verify scans prune `SKIP_DIRS` in O(1)."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / ".venv" / "clean_proj"
            ws.mkdir(parents=True)
            (ws / "app.py").write_text('"""Clean module."""\nVALUE = 1\n', encoding="utf-8")
            (ws / "config.json").write_text('{"ok": true}\n', encoding="utf-8")
            for ignored in (".git", ".venv", "node_modules", ".companion_backups"):
                nested = ws / ignored / "deep" / "sub"
                nested.mkdir(parents=True)
                (nested / "broken.py").write_text("def syntax_bomb(:\n", encoding="utf-8")
                (nested / "ignored.json").write_text("{bad_json", encoding="utf-8")

            visited_roots: List[str] = []
            real_walk = os.walk

            def spy_walk(top, *args, **kwargs):
                for cur_r, dirs, files in real_walk(top, *args, **kwargs):
                    visited_roots.append(Path(cur_r).as_posix())
                    yield cur_r, dirs, files

            with patch("companion.config.os.walk", side_effect=spy_walk):
                pruned = walk_pruned_files(ws)
                with workspace_context(ws, "walk-proj"):
                    hashes = _snapshot_file_hashes(ws)
                    hv_ok, hv_msg = _evaluate_hard_verification(ws, {}, run_doctor_tests=False, goal="Create app.py")

            self.assertEqual([p.name for p in pruned], ["app.py", "config.json"])
            self.assertEqual(set(hashes.keys()), {"app.py", "config.json"})
            self.assertTrue(hv_ok, hv_msg)
            ws_posix = ws.as_posix()
            rel_visits = [r[len(ws_posix) :].lstrip("/") for r in visited_roots if r.startswith(ws_posix)]
            self.assertTrue(all((rv.split("/", 1)[0] not in SKIP_DIRS) for rv in rel_visits if rv))


if __name__ == "__main__":
    unittest.main()
