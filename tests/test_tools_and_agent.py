"""Unit tests for safe tools, Windows shell heredoc interception, and Executive Agent Loop."""

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from companion.agent import handle_slash_command, run_agent_turn
from companion.memory_store import MemoryStore, memory_store_context
from companion.tools import (
    dispatch,
    tool_edit_file,
    tool_run_python,
    tool_run_shell,
    tool_write_file,
)


class TestToolsAndAgentLoop(unittest.TestCase):
    def test_write_and_edit_file_syntax_guard_blocks_broken_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            py_file = Path(tmp) / "test_mod.py"
            # Attempt to write invalid Python syntax
            res_bad = tool_write_file({"path": str(py_file), "content": "def broken(\n"})
            self.assertIn("SYNTAX GUARD REJECTED", res_bad)
            self.assertFalse(py_file.exists())

            # Write valid Python syntax
            res_ok = tool_write_file({"path": str(py_file), "content": '"""Doc."""\nx = 10\n'})
            self.assertIn("wrote", res_ok)
            self.assertTrue(py_file.exists())

            # Attempt an edit that breaks syntax
            edit_bad = tool_edit_file({"path": str(py_file), "old_str": "x = 10", "new_str": "x = ("})
            self.assertIn("SYNTAX GUARD REJECTED", edit_bad)
            # Original valid content must remain intact!
            self.assertIn("x = 10", py_file.read_text(encoding="utf-8"))

    def test_run_python_handles_multiline_and_unicode(self) -> None:
        code = "import sys\nprint('Arrow -> and check \\u2713')\n"
        out = tool_run_python({"code": code})
        self.assertIn("exit=0", out)
        self.assertIn("Arrow ->", out)

    def test_run_shell_intercepts_bash_heredoc_on_windows(self) -> None:
        heredoc_cmd = "python << 'EOF'\nprint(21 * 2)\nEOF"
        out = tool_run_shell({"command": heredoc_cmd})
        self.assertIn("exit=0", out)
        self.assertIn("42", out)

    def test_agent_loop_forced_final_synthesis_when_hitting_max_iters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "mem.db")

            def fake_chat(messages: List[Dict[str, Any]], tools=None, tool_choice="auto") -> Dict[str, Any]:
                # If tools are disabled (final synthesis call), return a clean summary!
                if tools is None or tool_choice == "none":
                    return {
                        "choices": [
                            {"message": {"role": "assistant", "content": "Synthesized final summary after budget."}}
                        ]
                    }
                # Otherwise simulate a tool call
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "function": {"name": "list_dir", "arguments": '{"path": "."}'},
                                    }
                                ],
                            }
                        }
                    ]
                }

            msgs: List[Dict[str, Any]] = [{"role": "user", "content": "Test budget wrapup"}]
            answer = run_agent_turn(msgs, max_iters=2, chat_fn=fake_chat, store=store, verbose=False)
            self.assertEqual(answer, "Synthesized final summary after budget.")
            store.close()

    def test_slash_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "slash.db")
            try:
                msgs: List[Dict[str, Any]] = []
                help_out = handle_slash_command("/help", msgs, store=store)
                self.assertIsNotNone(help_out)
                self.assertIn("/workflow", str(help_out))
                self.assertIn("/tone", str(help_out))
                tone_out = handle_slash_command("/tone", msgs, store=store)
                self.assertIn("Communication Tone:", str(tone_out))
            finally:
                store.close()

    def test_check_in_skill(self) -> None:
        """Test that check_in returns a friendly summary of system state."""
        from companion.agent import check_in

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "checkin.db")
            try:
                result = check_in(store=store)
                self.assertIsInstance(result, str)
                self.assertIn("Autonomy Levers", result)
                self.assertIn("Workflow State", result)
                self.assertIn("Pending Tasks", result)
                self.assertIn("Pending Backlog", result)
            finally:
                store.close()

    def test_null_args_read_spec_and_context_compaction(self) -> None:
        """Test tool resilience against None args, read_spec case handling, and user-boundary compaction."""
        from companion.context import compact_messages_if_needed
        from companion.tools import dispatch

        spec_out = dispatch("sdd_manage", {"action": "read_spec", "spec_name": "PRODUCT.md", "title": None})
        self.assertIn("Companion AI OS", spec_out)

        head_out = tool_run_shell({"command": "python -c \"for i in range(5): print(i)\" | head -2", "cwd": None})
        self.assertIn("exit=0", head_out)
        self.assertIn("0", head_out)
        self.assertNotIn("4", head_out)

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "compact.db")
            msgs: List[Dict[str, Any]] = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "u1 " + ("x" * 200)},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "list_dir", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "t1 " + ("y" * 200)},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "function": {"name": "list_dir", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c2", "content": "t2 " + ("z" * 200)},
            ]
            compacted = compact_messages_if_needed(msgs, max_chars=300, keep_recent=2, store=store)
            self.assertTrue(compacted)
            # Paired checkpoint user/assistant handoff keeps strict alternation (zero mid-array system or orphan tool)
            self.assertEqual(msgs[1]["role"], "user")
            self.assertEqual(msgs[2]["role"], "assistant")
            self.assertEqual(msgs[3]["role"], "user")
            self.assertTrue(all(m["role"] != "system" for m in msgs[1:]))
            store.close()

    def test_first_boot_soul_and_user_identity_onboarding(self) -> None:
        """Test first-boot 4-question Soul & User Identity bootstrap, /tone presets, Core Action `identity`, and user skills."""
        from unittest.mock import patch
        from companion.config import settings
        from companion.context import execute_identity_action, is_identity_bootstrapped, manage_companion_tone, run_first_boot_onboarding
        from companion.grounding import discover_skills
        from companion.skill_forge import stage_and_promote_skill

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            store = MemoryStore(db_path=tmp_p / "onboard.db")
            try:
                self.assertFalse(is_identity_bootstrapped(store=store))
                answers = iter(["Atlas", "Calm, loyal architect.", "Alex", "Product builder who prefers clear summaries."])
                summary = run_first_boot_onboarding(store=store, input_fn=lambda _p: next(answers))
                self.assertTrue(is_identity_bootstrapped(store=store))
                self.assertIn("Atlas", summary)
                self.assertIn("Alex", summary)
                self.assertIn("concise", manage_companion_tone("concise", store=store))
                self.assertIn("Invalid tone", manage_companion_tone("loud", store=store))
                id_text = (tmp_p / "IDENTITY.md").read_text(encoding="utf-8")
                self.assertIn("concise", id_text)
                self.assertLessEqual(len(id_text.splitlines()), 13)
                non_tty = handle_slash_command("/onboard", [], store=store, interactive=False)
                self.assertIn("requires a terminal TTY", str(non_tty))

                upd = execute_identity_action(
                    {"subaction": "update", "companion_name": "Nova", "user_name": "Taylor", "companion_tone": "balanced"},
                    actor="web", store=store,
                )
                self.assertTrue(upd["ok"])
                self.assertEqual(upd["companion_name"], "Nova")
                self.assertEqual(upd["user_name"], "Taylor")
                blocked = execute_identity_action({"subaction": "update", "companion_name": "Rogue"}, actor="companion", store=store)
                self.assertFalse(blocked["ok"])

                user_sk_md = (
                    "---\nname: custom-user-pack\ndescription: User-scoped custom capability pack.\nrequired_tools: [read_file]\n---\n\n"
                    "## When to Use This Skill\nWhen running a custom check.\n\n## Step-by-Step Procedure\n1. Read target file.\n"
                )
                with patch.object(settings, "data_dir", tmp_p / "data"), memory_store_context(store):
                    sk_res = stage_and_promote_skill(name="custom-user-pack", skill_md=user_sk_md, scope="user")
                    self.assertTrue(sk_res["promoted"], sk_res)
                    found = {s["name"]: s for s in discover_skills(settings.root)}
                    self.assertIn("custom-user-pack", found)
                    self.assertEqual(found["custom-user-pack"]["scope"], "user")
            finally:
                store.close()

    def test_telemetry_waterfall_traces_logs_and_kpis(self) -> None:
        """Test TelemetryEngine waterfall trace spans, structured app logs, and KPI scorecard."""
        from companion.telemetry import TelemetryEngine

        with tempfile.TemporaryDirectory() as tmp:
            tel = TelemetryEngine(db_path=Path(tmp) / "tel.db")
            tid = tel.start_trace(trace_type="user_turn", name="Test waterfall turn")
            tel.record_span("llm_call", "qwen2.5:7b", 125.4, status="ok", detail="msgs=3")
            tel.record_span("tool_call", "read_file", 8.2, status="ok", detail="read 50 lines")
            tel.log("WARN", "safety", "LoopGuard warning test", trace_id=tid)
            tel.end_trace(tid, status="ok")

            kpis = tel.format_kpi_dashboard()
            self.assertIn("Traces Recorded   : 1", kpis)
            self.assertIn("LLM Calls         : 1", kpis)
            self.assertIn("Tool Executions   : 1", kpis)

            wf = tel.format_waterfall(tid)
            self.assertIn("llm_call:qwen2.5:7b", wf)
            self.assertIn("tool_call:read_file", wf)

            logs_out = tel.handle_tool({"action": "logs", "level": "WARN"})
            self.assertIn("LoopGuard warning test", logs_out)
            tel.close()

    def test_percentage_trigger_and_two_stage_compaction(self) -> None:
        """Test 262k percentage trigger calculation, Stage-1 stale tool output pruning, and /compact command."""
        from companion.config import settings
        from companion.context import format_context_budget_status, prune_stale_tool_outputs

        self.assertEqual(settings.context_window_tokens, 262144)
        self.assertEqual(settings.compact_trigger_tokens, int(262144 * 0.75))
        self.assertEqual(settings.compact_target_tokens, int(262144 * 0.35))

        msgs: List[Dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Read huge file"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "LINE\n" * 400},
            {"role": "user", "content": "Recent question"},
            {"role": "assistant", "content": "Recent answer"},
        ]
        freed = prune_stale_tool_outputs(msgs, keep_recent=2, max_old_tool_chars=500)
        self.assertGreater(freed, 1000)
        self.assertIn("[stale tool output pruned", msgs[3]["content"])
        self.assertEqual(len(msgs), 6)  # Stage 1 preserves all conversation turns!

        status_text = format_context_budget_status(msgs)
        self.assertIn("262,144 tokens", status_text)
        self.assertIn("196,608 tokens", status_text)

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "compact_slash.db")
            try:
                slash_compact = handle_slash_command("/compact status", msgs, store=store)
                self.assertIn("Adaptive Context Window", str(slash_compact))
            finally:
                store.close()

    def test_fresh_clone_auto_bootstraps_db_memory_and_identity_files(self) -> None:
        """Simulate a fresh GitHub clone with no DB, MEMORY.md, or IDENTITY.md and verify auto-bootstrap."""
        from companion.context import build_system_prompt, is_identity_bootstrapped

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            db_file = tmp_dir / "conversations.db"
            mem_md = tmp_dir / "MEMORY.md"
            id_md = tmp_dir / "IDENTITY.md"
            self.assertFalse(db_file.exists())
            self.assertFalse(mem_md.exists())
            self.assertFalse(id_md.exists())

            store = MemoryStore(db_path=db_file)
            try:
                self.assertTrue(db_file.is_file())
                self.assertEqual(len(store.get_adrs()), 20)

                prompt = build_system_prompt(store=store)
                self.assertIn("[COMPANION SOUL & USER IDENTITY]", prompt)
                self.assertIn("[AVAILABLE SKILLS — PROGRESSIVE DISCLOSURE RUNBOOKS (L2)]", prompt)
                self.assertTrue(mem_md.is_file())
                self.assertTrue(id_md.is_file())
                self.assertFalse(is_identity_bootstrapped(store=store))
            finally:
                store.close()

    def test_progressive_disclosure_skills_discovery_and_slash_command(self) -> None:
        """Test Tier-1 skill discovery from skills/*/SKILL.md and Tier-2 /skills runbook inspection."""
        from companion.context import discover_skills

        skills = discover_skills()
        names = {s["name"] for s in skills}
        self.assertIn("proactive-checkin", names)
        self.assertIn("sdd-compliance", names)
        self.assertIn("engineering-workflow", names)
        self.assertIn("skill-creator", names)

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "skills_slash.db")
            try:
                catalog_out = handle_slash_command("/skills", [], store=store)
                self.assertIn("Progressive-Disclosure Skill Registry", str(catalog_out))
                self.assertIn("skill-creator", str(catalog_out))

                runbook_out = handle_slash_command("/skills skill-creator", [], store=store)
                self.assertIn("# Skill Creator & Capability Pack Forge Guide", str(runbook_out))
                wiki_out = handle_slash_command("/skills knowledge-wiki", [], store=store)
                self.assertIn("agentic-patterns.md", str(wiki_out))
            finally:
                store.close()

    def test_isolated_subagent_worker_dispatch_and_skill_preloading(self) -> None:
        """Test that run_subagent isolates worker tool history, pre-loads SKILL.md, and restores parent trace."""
        from companion.agent import run_subagent
        from companion.telemetry import telemetry

        captured_prompts: List[str] = []

        def fake_worker_chat(messages: List[Dict[str, Any]], tools=None, tool_choice="auto") -> Dict[str, Any]:
            for m in messages:
                if m.get("role") == "user":
                    captured_prompts.append(str(m.get("content", "")))
            if not any(m.get("role") == "tool" for m in messages):
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {"id": "sub_1", "function": {"name": "list_dir", "arguments": '{"path": "skills"}'}}
                                ],
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "Subagent audit complete: 6 skills verified."}}
                ]
            }

        with tempfile.TemporaryDirectory() as tmp:
            from companion.telemetry import TelemetryEngine

            sub_db = Path(tmp) / "sub.db"
            store = MemoryStore(db_path=sub_db)
            tmp_tel = TelemetryEngine(db_path=sub_db)
            old_db, old_conn = telemetry.db_path, telemetry.conn
            telemetry.db_path, telemetry.conn = tmp_tel.db_path, tmp_tel.conn
            parent_msgs: List[Dict[str, Any]] = [{"role": "user", "content": "Parent turn"}]
            parent_tid = telemetry.start_trace("user_turn", "Parent test turn")
            try:
                summary = run_subagent(
                    goal="Audit all skills",
                    skill_name="sdd-compliance",
                    max_iters=4,
                    chat_fn=fake_worker_chat,
                    store=store,
                    verbose=False,
                )
                self.assertEqual(summary, "Subagent audit complete: 6 skills verified.")
                # Parent message list must still have only 1 message (zero worker tool bloat!)
                self.assertEqual(len(parent_msgs), 1)
                # Pre-loaded Tier-2 SKILL.md must be injected into the worker prompt
                self.assertTrue(any("PRE-LOADED TIER-2 SKILL RUNBOOK (`skills/sdd-compliance/SKILL.md`)" in p for p in captured_prompts))
                # Parent trace ID must be restored!
                self.assertEqual(telemetry.active_trace_id, parent_tid)
                wf_text = telemetry.format_waterfall(parent_tid)
                self.assertIn("subagent_call:subagent:sdd-compliance", wf_text)
                self.assertIn("ADR-001", dispatch("sdd_manage", {"action": "get_adrs"}))
            finally:
                telemetry.end_trace(parent_tid, status="ok")
                telemetry.db_path, telemetry.conn = old_db, old_conn
                tmp_tel.close()
                store.close()

    def test_phase1_safety_tools_secret_redactor_and_empty_synthesis_recovery(self) -> None:
        """Test tool-level safety guardrails, SecretRedactor in telemetry, and `<think>` / t81 recovery."""
        from companion.llm import strip_thinking_tags
        from companion.telemetry import SecretRedactor, TelemetryEngine

        # 1. Tool-level command and path safety blocks
        shell_blocked = tool_run_shell({"command": "git reset --hard HEAD"})
        self.assertIn("SAFETY GUARD REJECTED command", shell_blocked)

        env_blocked = tool_write_file({"path": ".env", "content": "COMPANION_API_KEY=secret"})
        self.assertIn("SAFETY GUARD REJECTED path", env_blocked)

        # 2. SecretRedactor scrubs Bearer, sk-/nvapi-/ghp_ keys, key=value pairs, and custom secrets
        redactor = SecretRedactor(extra_secrets=["my-super-secret-custom-token-999"])
        raw_log = (
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 "
            "key1=sk-proj1234567890abcdef key2=nvapi-9876543210fedcba "
            "api_key='topsecretvalue123' custom=my-super-secret-custom-token-999"
        )
        scrubbed = redactor.redact(raw_log)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", scrubbed)
        self.assertNotIn("sk-proj1234567890abcdef", scrubbed)
        self.assertNotIn("nvapi-9876543210fedcba", scrubbed)
        self.assertNotIn("topsecretvalue123", scrubbed)
        self.assertNotIn("my-super-secret-custom-token-999", scrubbed)
        self.assertIn("[REDACTED]", scrubbed)

        with tempfile.TemporaryDirectory() as tmp:
            tel = TelemetryEngine(db_path=Path(tmp) / "redacted_tel.db")
            tid = tel.start_trace("user_turn", "Turn with sk-live9999888877776666")
            tel.record_span("llm_call", "model", 10.0, detail="Bearer supersecrettoken12345")
            tel.log("INFO", "auth", "password=my_secret_pass_123", trace_id=tid)
            tel.end_trace(tid)
            wf_out = tel.format_waterfall(tid)
            logs_out = tel.format_recent_logs()
            self.assertNotIn("sk-live9999888877776666", wf_out)
            self.assertNotIn("supersecrettoken12345", wf_out)
            self.assertNotIn("my_secret_pass_123", logs_out)
            tel.close()

        # 3. <think> tag extraction and t81 1-shot empty-synthesis recovery in run_agent_turn
        clean_txt, thought_txt = strip_thinking_tags("<think>Let me analyze this step by step.</think>Here is the clean answer.")
        self.assertEqual(clean_txt, "Here is the clean answer.")
        self.assertEqual(thought_txt, "Let me analyze this step by step.")

        call_count = 0

        def fake_reasoning_t81_chat(messages: List[Dict[str, Any]], tools=None, tool_choice="auto") -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simulate t81 bug: model spends tokens inside <think> and returns no visible text & no tool calls
                return {"choices": [{"message": {"role": "assistant", "content": "<think>Thinking deeply...</think>"}}]}
            # Second call is the 1-shot synthesis retry (tools=None)
            self.assertIsNone(tools)
            return {"choices": [{"message": {"role": "assistant", "content": "<think>Done</think>Recovered clean synthesis."}}]}

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "t81.db")
            msgs: List[Dict[str, Any]] = [{"role": "user", "content": "Explain status"}]
            reply = run_agent_turn(msgs, max_iters=3, chat_fn=fake_reasoning_t81_chat, store=store, verbose=False)
            self.assertEqual(reply, "Recovered clean synthesis.")
            self.assertEqual(call_count, 2)
            store.close()

    def test_phase2_keytokens_lossless_compaction_and_model_gate_preemption(self) -> None:
        """Test KeyTokens lossless compaction verification and ModelGate foreground preemption."""
        import threading
        import time
        from companion.context import (
            compact_messages_if_needed,
            extract_key_tokens,
            normalize_compaction_text,
            verify_compaction_summary,
        )
        from companion.llm import ModelGate

        # 1. Unicode dash folding & KeyTokens extraction
        norm = normalize_compaction_text("Range 2026\u201311\u201408\u200b on companion/safety.py")
        self.assertEqual(norm, "Range 2026-11-08 on companion/safety.py")

        older_msgs: List[Dict[str, Any]] = [
            {
                "role": "user",
                "content": "Update companion/workflow_graph.py on 192.168.1.100:8099 for run_agent_turn and ADR-007 " + ("pad " * 80),
            },
            {"role": "assistant", "content": "Working on it."},
        ]
        kt = extract_key_tokens(older_msgs)
        self.assertIn("companion/workflow_graph.py", kt.paths)
        self.assertIn("192.168.1.100", kt.numbers)
        self.assertIn("run_agent_turn", kt.names)
        self.assertIn("ADR-007", kt.names)

        # Verify lossy LLM summary is caught and automatically healed with missing KeyTokens
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "kt.db")
            msgs: List[Dict[str, Any]] = [
                {"role": "system", "content": "sys"},
                *older_msgs,
                {"role": "user", "content": "Recent user message 1"},
                {"role": "assistant", "content": "Recent assistant reply 1"},
                {"role": "user", "content": "Recent user message 2"},
            ]
            compact_messages_if_needed(
                msgs, max_chars=100, keep_recent=2, store=store, force=True,
                summarizer_fn=lambda _prompt: "Short summary that dropped all specific tokens.",
            )
            summary_note = msgs[1]["content"]
            self.assertIn("Preserved Key Entities (Lossless KeyTokens Verification)", summary_note)
            ok, missing = verify_compaction_summary(older_msgs, summary_note)
            self.assertTrue(ok, f"Expected 0 missing KeyTokens after healing, got {missing}")
            store.close()

        # 2. ModelGate foreground preemption and abort_check
        gate = ModelGate()
        events: List[str] = []
        bg_waiting_event = threading.Event()

        def bg_worker() -> None:
            with gate.background_context():
                bg_waiting_event.set()
                with gate.acquire_call():
                    events.append("bg_executed")

        with gate.foreground_turn():
            self.assertEqual(gate.status()["foreground_active"], 1)
            t = threading.Thread(target=bg_worker)
            t.start()
            bg_waiting_event.wait(timeout=2.0)
            time.sleep(0.08)
            # Background worker must still be waiting while foreground turn is active!
            self.assertEqual(events, [])
            self.assertEqual(gate.status()["background_waiting"], 1)
            events.append("fg_finishing")

        t.join(timeout=2.0)
        self.assertEqual(events, ["fg_finishing", "bg_executed"])

        # abort_check must immediately preempt a background LLM call with InterruptedError
        with gate.background_context(abort_check=lambda: True):
            with self.assertRaises(InterruptedError):
                with gate.acquire_call():
                    pass

    def test_multi_tier_routing_endpoint_gates_and_failover(self) -> None:
        """Test per-endpoint ModelGates, /model commands, reasoning subagent tier routing, and OODA LoopGuard nudge."""
        from companion.agent import run_subagent
        from companion.llm import current_tier_name, get_endpoint_gate, model_gate, tier_context
        from companion.safety import LoopGuard

        # 1. Per-endpoint ModelGate isolation
        g_default = get_endpoint_gate()
        g_secondary = get_endpoint_gate("http://10.0.0.99:11434")
        self.assertIs(g_default, model_gate)
        self.assertIsNot(g_secondary, model_gate)

        # 2. Contextvar tier binding
        self.assertEqual(current_tier_name(), "fast")
        with tier_context("reasoning"):
            self.assertEqual(current_tier_name(), "reasoning")
        self.assertEqual(current_tier_name(), "fast")

        # 3. /model slash command & reasoning subagent tier routing
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "tier.db")
            try:
                msgs: List[Dict[str, Any]] = []
                status_out = handle_slash_command("/model status", msgs, store=store)
                self.assertIn("Companion Multi-Tier Model Router", str(status_out))

                observed_sub_tiers: List[str] = []

                def fake_reasoning_chat(messages: List[Dict[str, Any]], tools=None, tool_choice="auto") -> Dict[str, Any]:
                    observed_sub_tiers.append(current_tier_name())
                    return {"choices": [{"message": {"role": "assistant", "content": "Deep tradeoff synthesis complete."}}]}

                sub_out = run_subagent(
                    goal="Evaluate architecture options", skill_name="deep-thinking",
                    tier="reasoning", chat_fn=fake_reasoning_chat, store=store, verbose=False,
                )
                self.assertEqual(sub_out, "Deep tradeoff synthesis complete.")
                self.assertEqual(observed_sub_tiers, ["reasoning"])
            finally:
                store.close()

        # 4. LoopGuard OODA nudge after 2 consecutive tool errors
        lg = LoopGuard(max_consecutive_errors=2)
        self.assertIsNone(lg.record_result("run_python", "error: syntax failure 1"))
        hint = lg.record_result("run_python", "error: syntax failure 2")
        self.assertIsNotNone(hint)
        self.assertIn("OODA Loop", str(hint))
        self.assertIn("ooda-troubleshooting", str(hint))

    def test_frontier_core_parsers_wire_and_bounded_read(self) -> None:
        """Verify CORE-4, CORE-5, CORE-8: prefix-orphan </think>, unclosed <think> tool rescue, parallel deltas, strict JSON args, & bounded read_file."""
        from companion.llm import normalize_assistant_message
        from companion.llm_wire import repair_message_protocol_invariants
        from companion.streaming import ThinkTagStreamSplitter, _merge_tool_call_deltas, stream_event_context, strip_thinking_tags
        from companion.tools import tool_read_file

        clean_o, think_o = strip_thinking_tags("chain of thought first</think>Final crisp answer")
        self.assertEqual(clean_o, "Final crisp answer")
        self.assertEqual(think_o, "chain of thought first")
        events: List[Dict[str, Any]] = []
        with stream_event_context(events.append):
            sp = ThinkTagStreamSplitter()
            sp.feed("prefilled reasoning ")
            sp.feed("still thinking</think>Visible reply")
            sp.flush()
        self.assertTrue(any(e.get("type") == "orphan_think_closed" for e in events))

        unclosed_msg = {
            "role": "assistant",
            "content": '<think>Need two reads\n<tool_call>[{"name":"list_dir","arguments":{"path":"."}},{"name":"read_file","arguments":{"path":"README.md"}}]</tool_call>',
        }
        c_u, t_u, tcalls_u = normalize_assistant_message(unclosed_msg)
        self.assertEqual(c_u, "")
        self.assertIn("Need two reads", t_u)
        self.assertEqual([tc["function"]["name"] for tc in tcalls_u], ["list_dir", "read_file"])
        self.assertNotEqual(tcalls_u[0]["id"], tcalls_u[1]["id"])

        acc: Dict[int, Dict[str, Any]] = {}
        _merge_tool_call_deltas(acc, [{"index": 0, "id": "call_a", "function": {"name": "read_file", "arguments": '{"path":"a"}'}}])
        _merge_tool_call_deltas(acc, [{"index": 0, "id": "call_b", "function": {"name": "write_file", "arguments": '{"path":"b"}'}}])
        self.assertEqual([v["function"]["name"] for v in acc.values()], ["read_file", "write_file"])

        repaired = repair_message_protocol_invariants([
            {"role": "system", "content": "sys0"},
            {"role": "system", "content": "mid-sys"},
            {"role": "user", "content": "u1"},
            {"role": "user", "content": "u2"},
        ])
        self.assertEqual([m["role"] for m in repaired], ["system", "user"])

        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.txt"
            big.write_text("\n".join(f"L{i}" for i in range(1, 1201)), encoding="utf-8")
            rf_out = tool_read_file({"path": str(big)})
            self.assertIn("Showing lines 1-800 of 1200; pass start_line=801 to continue", rf_out)
            st = MemoryStore(db_path=Path(tmp) / "core.db")
            try:
                calls_seen = {"n": 0}

                def _bad_json_chat(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                    calls_seen["n"] += 1
                    if calls_seen["n"] == 1:
                        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "b1", "function": {"name": "list_dir", "arguments": "{truncated"}}]}}]}
                    self.assertIn("Tool argument JSON parse error", str(messages[-1].get("content", "")))
                    return {"choices": [{"message": {"role": "assistant", "content": "Fixed JSON."}}]}

                m_list: List[Dict[str, Any]] = [{"role": "user", "content": "Run tool"}]
                self.assertEqual(run_agent_turn(m_list, max_iters=3, chat_fn=_bad_json_chat, store=st, verbose=False), "Fixed JSON.")
            finally:
                st.close()

    def test_frontier_cancel_scope_and_execution_origin(self) -> None:
        """Verify CORE-6 & CORE-7: <100ms CancelScope socket/subprocess abort, WebSession Stop balance, & execution_origin."""
        import socket
        import subprocess
        import sys
        import threading
        import time

        from companion.config import execution_origin_context, get_execution_origin
        from companion.streaming import CancelScope, check_cancelled, emit_stream_event
        from surfaces.web_server import CompanionWebSession

        self.assertEqual(get_execution_origin(), "direct")
        with execution_origin_context("foreground_turn"):
            self.assertEqual(get_execution_origin(), "foreground_turn")

        s_srv, s_cli = socket.socketpair()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        scope = CancelScope()
        scope.register_socket(s_cli)
        t0 = time.perf_counter()
        threading.Timer(0.02, scope.cancel).start()
        with scope.track_popen(proc):
            try:
                s_cli.recv(16)
            except OSError:
                pass
            proc.wait(timeout=2.0)
        self.assertLess(time.perf_counter() - t0, 0.5)
        s_srv.close()

        with tempfile.TemporaryDirectory() as tmp:
            st = MemoryStore(db_path=Path(tmp) / "stop.db")
            try:
                entered = threading.Event()

                def _slow_chat(_m: List[Dict[str, Any]], _t=None, _c="auto") -> Dict[str, Any]:
                    emit_stream_event({"type": "text_delta", "delta": "Partial stream"})
                    entered.set()
                    for _ in range(200):
                        check_cancelled()
                        time.sleep(0.01)
                    return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

                sess = CompanionWebSession(store=st, chat_fn=_slow_chat)
                code, sub_p = sess.submit_async_chat("Long query")
                self.assertEqual(code, 202)
                entered.wait(timeout=2.0)
                c_code, c_res = sess.cancel_active_turn(sub_p["job_id"])
                self.assertEqual(c_code, 200)
                self.assertTrue(c_res["cancelled"])
                for _ in range(50):
                    if not sess.busy:
                        break
                    time.sleep(0.02)
                self.assertIn("*(Stopped by user.)*", sess.messages[-1]["content"])
                self.assertIn("Partial stream", sess.messages[-1]["content"])
            finally:
                st.close()


if __name__ == "__main__":
    unittest.main()
