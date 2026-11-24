"""Verification tests for Companion AI OS v2.4: SSE Streaming, LAN Security, PEP 723 Skills, DAG & Semantic Memory."""

import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

from companion.approvals import ActionRiskTier, classify_action_risk
from companion.agent import _execute_implement_tasks, run_autonomous_goal
from companion.agent import handle_blueprint_conversation_turn, is_pure_blueprint_question
from companion.vault import CheckpointManager
from companion.context import build_lossless_phase_handoff, extract_key_tokens
from companion.config import workspace_context
from companion.context import LocalKnowledgeAdapter
from companion.agent import classify_turn_intent
from companion.memory_store import MemoryStore, memory_store_context
from companion.tools import lint_single_file_edit
from companion.memory_store import cosine_f32_bytes, pack_f32_vector
from companion.skill_forge import build_skill_script_command, parse_pep723_dependencies
from companion.streaming import emit_stream_event, read_completion_or_sse_stream, stream_event_context
from companion.telemetry import TelemetryEngine
from companion.tools import tool_read_file, tool_search, tool_web_fetch
from companion.workflow_dag import GoalPlan, GoalStep, VerificationSpec, build_goal_plan_from_tasks
from companion.agent import WorkflowEngine
from surfaces.web_assets import read_static_asset, render_web_index_html
from surfaces.web_server import CompanionHTTPServer, CompanionWebSession, dispatch_raw_http


class _FakeSSEResponse(io.BytesIO):
    """Minimal HTTPResponse-compatible wrapper exposing Content-Type for SSE tests."""

    def __init__(self, body: bytes, content_type: str = "text/event-stream"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}


class _FakeHTMLResponse(io.BytesIO):
    """Minimal HTTPResponse-compatible wrapper for `tool_web_fetch` tests."""

    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8", status: int = 200):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}
        self.status = status

    def __enter__(self) -> "_FakeHTMLResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _make_stub_server(session: CompanionWebSession) -> CompanionHTTPServer:
    """Create a socket-free `CompanionHTTPServer` wrapper around `session` for `dispatch_raw_http`."""
    srv = object.__new__(CompanionHTTPServer)
    srv.session = session
    return srv


class TestV24Evolution(unittest.TestCase):
    def test_phase1_static_assets_and_zero_login_lan_security(self) -> None:
        """Verify split static Web UI assets and invisible Zero-Login Home LAN Host/Origin/CSRF protections."""
        css_asset = read_static_asset("styles.css")
        js_asset = read_static_asset("app.js")
        self.assertIsNotNone(css_asset)
        self.assertIsNotNone(js_asset)
        css = css_asset[1].decode("utf-8") if css_asset else ""
        js = js_asset[1].decode("utf-8") if js_asset else ""
        html = render_web_index_html(csrf_token="lan-token-777")
        self.assertIn("100dvh", css)
        self.assertIn("EventSource", js)
        self.assertIn("X-Companion-CSRF", js)
        self.assertIn('<meta name="companion-csrf" content="lan-token-777">', html)

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "web_sec.db")
            try:
                sess = CompanionWebSession(
                    store=store,
                    chat_fn=lambda _m, tools=None, tool_choice="auto": {"choices": [{"message": {"role": "assistant", "content": "Hello from LAN!"}}]},
                )
                srv = _make_stub_server(sess)
                st_css, raw_css = dispatch_raw_http(srv, "GET", "/static/styles.css", headers={"Host": "192.168.1.55:8000"})
                self.assertEqual(st_css, 200)
                self.assertIn("100dvh", raw_css)

                # 1. DNS-rebinding Host header rejected with 403
                st_evil_host, _ = dispatch_raw_http(srv, "GET", "/api/health", headers={"Host": "evil.attacker.org"})
                self.assertEqual(st_evil_host, 403)

                # 2. Private LAN IPv4 Host header allowed without any login prompt
                st_lan_ok, raw_health = dispatch_raw_http(srv, "GET", "/api/health", headers={"Host": "192.168.1.55:8000"})
                self.assertEqual(st_lan_ok, 200)
                self.assertEqual(json.loads(raw_health)["csrf_token"], sess.csrf_token)

                # 3. Cross-site Origin, cross-site Sec-Fetch-Site, form-urlencoded POST, and mismatched CSRF rejected
                body = json.dumps({"message": "hi"}).encode("utf-8")
                st_orig, _ = dispatch_raw_http(
                    srv, "POST", "/api/chat", body,
                    headers={"Host": "192.168.1.55:8000", "Content-Type": "application/json", "Origin": "https://evil.example", "X-Companion-CSRF": sess.csrf_token},
                )
                self.assertEqual(st_orig, 403)

                st_sfs, _ = dispatch_raw_http(
                    srv, "POST", "/api/chat", body,
                    headers={"Host": "192.168.1.55:8000", "Content-Type": "application/json", "Sec-Fetch-Site": "cross-site", "X-Companion-CSRF": sess.csrf_token},
                )
                self.assertEqual(st_sfs, 403)

                st_form, _ = dispatch_raw_http(
                    srv, "POST", "/api/chat", b"message=hi",
                    headers={"Host": "192.168.1.55:8000", "Content-Type": "application/x-www-form-urlencoded"},
                )
                self.assertEqual(st_form, 403)

                st_bad_csrf, raw_csrf = dispatch_raw_http(
                    srv, "POST", "/api/chat", body,
                    headers={"Host": "192.168.1.55:8000", "Content-Type": "application/json", "Origin": "http://192.168.1.55:8000", "X-Companion-CSRF": "stale-token"},
                )
                self.assertEqual(st_bad_csrf, 403)
                self.assertIn("csrf_mismatch", json.loads(raw_csrf).get("error", ""))

                # 4. Valid same-LAN browser POST succeeds seamlessly with zero login screen
                st_good, raw_ok = dispatch_raw_http(
                    srv, "POST", "/api/chat", body,
                    headers={"Host": "192.168.1.55:8000", "Content-Type": "application/json", "Origin": "http://192.168.1.55:8000", "X-Companion-CSRF": sess.csrf_token},
                )
                self.assertEqual(st_good, 200)
                self.assertEqual(json.loads(raw_ok)["response"], "Hello from LAN!")
            finally:
                store.close()

    def test_phase1_live_sse_streaming_assembler_and_web_job_stream(self) -> None:
        """Verify `<think>` + delta + tool_call SSE assembly and end-to-end `/api/jobs/<id>/stream` SSE frames."""
        sse_lines = [
            b'data: {"model":"qwen3","choices":[{"delta":{"content":"<think>Plan step 1"}}]}\n\n',
            b'data: {"model":"qwen3","choices":[{"delta":{"content":"</think>Hello "}}]}\n\n',
            b'data: {"model":"qwen3","choices":[{"delta":{"content":"world!","tool_calls":[{"index":0,"id":"call_9","function":{"name":"list_","arguments":"{\\"pa"}}]}}]}\n\n',
            b'data: {"model":"qwen3","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"dir","arguments":"th\\":\\".\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        captured: List[Dict[str, Any]] = []
        with stream_event_context(captured.append):
            assembled = read_completion_or_sse_stream(_FakeSSEResponse(b"".join(sse_lines)))
        msg = assembled["choices"][0]["message"]
        self.assertEqual(msg["content"], "<think>Plan step 1</think>Hello world!")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "list_dir")
        self.assertEqual(msg["tool_calls"][0]["function"]["arguments"], '{"path":"."}')
        self.assertTrue(any(ev["type"] == "thinking_delta" and "Plan step 1" in ev["delta"] for ev in captured))
        self.assertEqual("".join(ev["delta"] for ev in captured if ev["type"] == "text_delta"), "Hello world!")

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "sse.db")
            try:
                def streaming_chat(_msgs: List[Dict[str, Any]], tools=None, tool_choice="auto") -> Dict[str, Any]:
                    emit_stream_event({"type": "text_delta", "delta": "Live token "})
                    emit_stream_event({"type": "text_delta", "delta": "stream!"})
                    return {"choices": [{"message": {"role": "assistant", "content": "Live token stream!"}}]}

                sess = CompanionWebSession(store=store, chat_fn=streaming_chat)
                srv = _make_stub_server(sess)
                st, raw_job = dispatch_raw_http(
                    srv, "POST", "/api/jobs", json.dumps({"message": "Stream test"}).encode("utf-8"),
                    headers={"Host": "localhost:8000", "Content-Type": "application/json", "X-Companion-CSRF": sess.csrf_token},
                )
                self.assertEqual(st, 202)
                jid = json.loads(raw_job)["job_id"]
                st_sse, sse_txt = dispatch_raw_http(srv, "GET", f"/api/jobs/{jid}/stream")
                self.assertEqual(st_sse, 200)
                self.assertIn('"type": "text_delta"', sse_txt)
                self.assertIn("Live token ", sse_txt)
                self.assertIn('"type": "done"', sse_txt)
            finally:
                store.close()

    def test_phase1_env_secret_read_block_ssrf_web_fetch_and_readonly_core_guard(self) -> None:
        """Verify `.env` read/search blocks, SSRF/HTML-to-Markdown `web_fetch`, R3 `run_python`, hygiene & read-only guard."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_dir = Path(tmp) / "ext_proj"
            ws_dir.mkdir()
            (ws_dir / ".env").write_text("SECRET_API_KEY=ultra_secret_123\n", encoding="utf-8")
            (ws_dir / ".env.example").write_text("SECRET_API_KEY=placeholder\n", encoding="utf-8")
            (ws_dir / "notes.txt").write_text("general project notes\n", encoding="utf-8")
            with workspace_context(ws_dir, "ext-proj"):
                self.assertIn("SAFETY GUARD REJECTED", tool_read_file({"path": ".env"}))
                self.assertIn("placeholder", tool_read_file({"path": ".env.example"}))
                srch = tool_search({"pattern": "SECRET_API_KEY", "path": "."})
                self.assertNotIn("ultra_secret_123", srch)
                self.assertIn(".env.example", srch)

            # Read-only tree snapshot & rollback guard for external subprocess protection
            core_sim = Path(tmp) / "core_sim"
            (core_sim / "companion").mkdir(parents=True)
            guarded = core_sim / "companion" / "mod.py"
            guarded.write_text("CLEAN = 1\n", encoding="utf-8")
            rogue = core_sim / "companion" / "rogue.py"
            ro_cp = CheckpointManager(root=core_sim, backup_dir=core_sim / ".companion_backups", store=False)
            snap = ro_cp.snapshot_readonly_tree()
            guarded.write_text("TAMPERED = 99\n", encoding="utf-8")
            rogue.write_text("BAD = True\n", encoding="utf-8")
            ro_err = ro_cp.enforce_readonly_tree(snap)
            self.assertIn("Reverted 2 unauthorized change(s)", str(ro_err))
            self.assertEqual(guarded.read_text(encoding="utf-8"), "CLEAN = 1\n")
            self.assertFalse(rogue.exists())

            # Telemetry retention pruning
            tel = TelemetryEngine(db_path=Path(tmp) / "prune.db")
            try:
                for idx in range(5):
                    tid = tel.start_trace("user_turn", f"t{idx}")
                    tel.log("INFO", "test", f"log-{idx}", trace_id=tid)
                    tel.end_trace(tid)
                pruned = tel.prune_old_telemetry(max_traces=2, max_logs=2)
                self.assertEqual(pruned["pruned_traces"], 3)
                self.assertEqual(pruned["pruned_logs"], 3)
            finally:
                tel.close()

        # SSRF blocks & HTML-to-Markdown conversion
        ssrf_meta = tool_web_fetch({"url": "http://169.254.169.254/latest/meta-data/"})
        self.assertIn("SAFETY GUARD REJECTED", ssrf_meta)
        self.assertIn("169.254.169.254", ssrf_meta)
        self.assertIn("SAFETY GUARD REJECTED", tool_web_fetch({"url": "http://127.0.0.1:22/"}))
        html_doc = b"<html><head><style>body{color:red}</style><script>evil()</script></head><body><h1>Docs</h1><p>Visit <a href='https://x.org'>Portal</a></p></body></html>"
        with patch("companion.tools._validate_web_fetch_url", return_value=None), patch("urllib.request.urlopen", return_value=_FakeHTMLResponse(html_doc)):
            md_out = tool_web_fetch({"url": "https://example.org/docs"})
        self.assertIn("# Docs", md_out)
        self.assertIn("[Portal](https://x.org)", md_out)
        self.assertNotIn("evil()", md_out)

        # R3 classification on `run_python`
        self.assertEqual(classify_action_risk("run_python", {"code": "import subprocess\nsubprocess.run('git push origin main')"}), ActionRiskTier.R3_HIGH_IMPACT_MUTATION)

    def test_phase2_pep723_uv_skill_sandboxes_and_ring0_stdlib_boundary(self) -> None:
        """Verify PEP 723 inline skill dependencies (`Pure Brain, Open Toolbox`) while keeping Ring-0 100% stdlib."""
        pep_script = (
            "# /// script\n"
            '# dependencies = ["requests>=2.31", "beautifulsoup4", "pyyaml"]\n'
            "# ///\n"
            '"""Skill helper."""\n'
            "import sys\nimport bs4\nimport requests\nimport yaml\n"
            "if '--self-test' in sys.argv:\n    print(bs4, requests, yaml)\n    sys.exit(0)\n"
        )
        raw_deps, allowed_imps, pep_err = parse_pep723_dependencies(pep_script)
        self.assertIsNone(pep_err)
        self.assertEqual(len(raw_deps), 3)
        self.assertTrue({"requests", "bs4", "yaml"}.issubset(allowed_imps))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_py = root / "skills" / "scraper-pack" / "scripts" / "fetch_page.py"
            skill_py.parent.mkdir(parents=True)
            skill_py.write_text(pep_script, encoding="utf-8")

            # Command builder uses `uv run` when `uv` is present, clear message when missing, and `sys.executable` for stdlib
            with patch("companion.skill_forge.shutil.which", return_value="C:/Tools/uv.exe"):
                cmd_uv, uv_err = build_skill_script_command(skill_py, ["--self-test"], pep_script)
                self.assertIsNone(uv_err)
                self.assertEqual(cmd_uv[:3], ["C:/Tools/uv.exe", "run", "--quiet"])
            with patch("companion.skill_forge.shutil.which", return_value=None):
                _, missing_err = build_skill_script_command(skill_py, ["--self-test"], pep_script)
                self.assertIn("requires `uv` on PATH", str(missing_err))
            stdlib_py = root / "skills" / "scraper-pack" / "scripts" / "pure.py"
            stdlib_src = '"""Pure."""\nimport json\nprint(json.dumps({"ok": True}))\n'
            stdlib_py.write_text(stdlib_src, encoding="utf-8")
            cmd_std, std_err = build_skill_script_command(stdlib_py, [], stdlib_src)
            self.assertIsNone(std_err)
            self.assertIn("pure.py", cmd_std[-1])

    def test_phase3_blueprint_analytical_qa_intake_and_parallel_dag_recovery(self) -> None:
        """Verify analytical Blueprint Q&A task preservation, natural build intake, token task matching & parallel R0 DAGs."""
        self.assertTrue(is_pure_blueprint_question("Why does the plan use port 8000 instead of 8080?"))
        self.assertTrue(is_pure_blueprint_question("How will the database schema migrate existing rows?"))
        self.assertFalse(is_pure_blueprint_question("Can we use port 8080 instead of 8000?"))
        self.assertFalse(is_pure_blueprint_question("Why not switch to SQLite WAL instead?"))

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "dag_ws"
            ws.mkdir()
            store = MemoryStore(db_path=Path(tmp) / "bp_qa.db")
            cp = CheckpointManager(root=ws, backup_dir=ws / ".companion_backups", store=store)
            try:
                with workspace_context(ws, "dag-proj"), memory_store_context(store):
                    wf = WorkflowEngine(store=store, cp_manager=cp, root=ws)
                    sr = wf.start_workflow("Design a LAN dashboard", workflow_type="blueprint")
                    for ph in ("DISCOVER", "DESIGN", "PLAN"):
                        wf.transition(ph, ph, run_tests_on_verify=False)
                    tid = store.add_task("Implement server.py on port 8000", workflow_id=sr.workflow_id)
                    store.update_workflow_metadata(sr.workflow_id, {"awaiting_plan_approval": True, "plan_handoff": "Use port 8000."})

                    reply = handle_blueprint_conversation_turn(
                        "Why does the plan use port 8000 instead of 8080?",
                        store=store,
                        chat_fn=lambda _m, tools=None, tool_choice="auto": {"choices": [{"message": {"role": "assistant", "content": "Port 8000 avoids conflicts with gateway 8099."}}]},
                        verbose=False,
                    )
                    self.assertIn("Port 8000", str(reply))
                    pending_ids = [t[0] for t in store.get_pending_tasks()]
                    self.assertIn(tid, pending_ids, "Read-only analytical question must NOT cancel pending Blueprint tasks!")

                    # Token-matched `build_goal_plan_from_tasks` maps out-of-order SQLite tasks accurately
                    t_ui = store.add_task("Create responsive frontend index.html layout", workflow_id=sr.workflow_id)
                    t_db = store.add_task("Build SQLite migration engine in storage.py", workflow_id=sr.workflow_id)
                    plan_txt = '```json\n{"steps": [{"step_id": "s1", "title": "Build SQLite migration engine", "risk_tier": "R1"}, {"step_id": "s2", "title": "Create responsive frontend index.html", "depends_on": ["s1"], "risk_tier": "R1"}]}\n```'
                    dag = build_goal_plan_from_tasks("Test goal", store.get_pending_tasks(), plan_text=plan_txt)
                    by_id = {s.step_id: s.task_id for s in dag.steps}
                    self.assertEqual(by_id["s1"], t_db)
                    self.assertEqual(by_id["s2"], t_ui)

                    # Natural multi-file build intake without buzzwords
                    dec = classify_turn_intent("Create server.py, storage.py, and index.html for my notes tracker", store=store)
                    self.assertEqual(dec.mode, "AUTONOMOUS_BUILD")

                    # Parallel R0 DAG execution + post-RECOVER human_gate pause
                    active_threads = 0
                    peak_threads = 0
                    t_lock = threading.Lock()

                    def parallel_chat(_m: List[Dict[str, Any]], _t=None, _c="auto") -> Dict[str, Any]:
                        nonlocal active_threads, peak_threads
                        with t_lock:
                            active_threads += 1
                            peak_threads = max(peak_threads, active_threads)
                        time.sleep(0.05)
                        with t_lock:
                            active_threads -= 1
                        return {"choices": [{"message": {"role": "assistant", "content": "inspected"}}]}

                    sr2 = wf.start_workflow("Parallel R0 + human gate")
                    for ph in ("DISCOVER", "DESIGN", "PLAN", "IMPLEMENT"):
                        wf.transition(ph, ph, run_tests_on_verify=False)
                    p_dag = GoalPlan(
                        "Parallel R0 + human gate",
                        [
                            GoalStep("r0_a", "Inspect A", [], "R0", VerificationSpec("assertion")),
                            GoalStep("r0_b", "Inspect B", [], "R0", VerificationSpec("assertion")),
                            GoalStep("gate_c", "Approve deploy", ["r0_a", "r0_b"], "R1", VerificationSpec("human_gate")),
                        ],
                    )
                    _, _, pause_reason, _, _, _, _, _ = _execute_implement_tasks(
                        "Parallel R0 + human gate", "d", "p", sr2.workflow_id, {"goal_dag": p_dag.to_dict()},
                        ws, False, 4, parallel_chat, wf, store, False, pinned_root=True, pinned_cp=True, eff_cp=cp,
                    )
                    self.assertGreaterEqual(peak_threads, 2)
                    self.assertIn("gate_c", pause_reason)

                    # Post-RECOVER unlocked `human_gate` step pauses cleanly (`AWAITING_APPROVAL`) instead of failing!
                    sr3 = wf.start_workflow("Recover unlocks human_gate")
                    for ph in ("DISCOVER", "DESIGN", "PLAN"):
                        wf.transition(ph, ph, run_tests_on_verify=False)
                    hg_dag = GoalPlan("Recover unlocks human_gate", [
                        GoalStep("s1", "Prepare artifact", [], "R1", VerificationSpec("assertion", contains="PREP_OK")),
                        GoalStep("s2", "Human sign-off", ["s1"], "R1", VerificationSpec("human_gate")),
                    ])
                    store.update_workflow_metadata(sr3.workflow_id, {"discovery_handoff": "d", "design_handoff": "d", "plan_handoff": "p", "goal_dag": hg_dag.to_dict()})

                    def rec_hg_chat(messages: List[Dict[str, Any]], _t=None, _c="auto") -> Dict[str, Any]:
                        u = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
                        if "[AUTONOMOUS WORKFLOW PHASE: RECOVER" in u:
                            return {"choices": [{"message": {"role": "assistant", "content": "PREP_OK"}}]}
                        return {"choices": [{"message": {"role": "assistant", "content": "missed token first try"}}]}

                    rec_res = run_autonomous_goal("resume", chat_fn=rec_hg_chat, store=store, cp_mgr=cp, root=ws, run_doctor_tests=False, verbose=False, resume=True)
                    self.assertEqual(rec_res["status"], "AWAITING_APPROVAL")
                    self.assertIn("s2", rec_res["pause_reason"])
            finally:
                store.close()

    def test_phase4_hybrid_semantic_memory_and_lossless_compaction(self) -> None:
        """Verify zero-swap Hybrid BM25 + Semantic RRF recall and fenced-JSON / error-preserving compaction."""
        vec_a = pack_f32_vector([1.0, 0.0, 1.0])
        vec_b = pack_f32_vector([1.0, 0.0, 1.0])
        self.assertAlmostEqual(cosine_f32_bytes(vec_a, vec_b), 1.0, places=5)

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "sem.db")
            try:
                store.log_message("user", "Why did the request take thirty seconds?")
                store.log_message("assistant", "The vLLM container unloads GPU VRAM during model tier transitions.")
                store.log_message("user", "Remind me to buy coffee beans tomorrow morning.")
                # Zero exact keyword overlap: "graphics card pause" -> "GPU VRAM ... transitions"
                hits = store.search_conversations("graphics card pause", limit=2)
                self.assertGreaterEqual(len(hits), 1)
                self.assertIn("GPU VRAM", hits[0][2])

                store.upsert_knowledge_fact("hw:dgx_swap", "Single GPU VRAM swaps take ~35s on container reload", source="agent")
                adapter = LocalKnowledgeAdapter(store=store, docs_dir=Path(tmp))
                k_hits = adapter.search("graphics card delay", limit=3)
                self.assertTrue(any("VRAM" in e.content for e in k_hits))
            finally:
                store.close()

        # Compaction preserves assistant/tool-discovered paths & error classes + mid-document fenced JSON DAG
        msgs = [
            {"role": "user", "content": "Investigate why startup failed."},
            {"role": "assistant", "content": "Found crash in surfaces/web_server.py raising ConnectionResetError on port 8099."},
        ]
        kt = extract_key_tokens(msgs)
        self.assertIn("surfaces/web_server.py", kt.paths)
        self.assertIn("ConnectionResetError", kt.names)

        long_plan = (
            "Architecture preamble " + ("alpha " * 300)
            + '\n```json\n{"steps": [{"step_id": "step_keep_me", "title": "Preserve DAG JSON"}]}\n```\n'
            + ("omega " * 300)
        )
        handoff = build_lossless_phase_handoff("PLAN", long_plan, max_chars=900)
        self.assertIn('"step_keep_me"', handoff)
        self.assertIn("```json", handoff)


if __name__ == "__main__":
    unittest.main()
