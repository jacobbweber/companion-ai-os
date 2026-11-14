"""Unit & integration tests for the Stdlib LAN Web UI, Multi-Phase Autonomy Driver, and Guardrails."""

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

from companion.agent import run_agent_turn
from companion.config import settings
from companion.autonomy import (
    PHASE_TIERS,
    FORBIDDEN_STDLIB_VIOLATION_IMPORTS,
    resolve_workflow_phase_tier,
    run_autonomous_goal,
    run_http_smoke_probe,
    scan_placeholder_violations,
)
from companion.llm import current_tier_name
from companion.memory_store import MemoryStore
from companion.safety import CheckpointManager, normalize_windows_shell_command
from companion.tools import dispatch, tool_run_shell
from harness import _coalesce_pasted_lines
from surfaces import CompanionWebSession, create_web_server, detect_lan_ip, dispatch_raw_http, render_web_index_html


def _http_call(srv, path: str, method: str = "GET", payload: Dict[str, Any] | None = None) -> Tuple[int, str]:
    port = int(srv.server_address[1])
    url = f"http://127.0.0.1:{port}{path}"
    raw_body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=raw_body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return int(resp.status), resp.read().decode("utf-8")
    except Exception as exc:
        if "10013" in str(exc) or "PermissionError" in str(exc) or "Errno 13" in str(exc):
            return dispatch_raw_http(srv, method=method, path=path, body=raw_body)
        raise


def _http_json(srv, path: str, method: str = "GET", payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    _, text = _http_call(srv, path=path, method=method, payload=payload)
    return json.loads(text)


class TestWebSurfaceAndAutonomy(unittest.TestCase):
    def test_web_server_end_to_end_chat_slash_settings_and_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "web_test.db")

            def fake_chat(messages: List[Dict[str, Any]], tools=None, tool_choice="auto") -> Dict[str, Any]:
                user_msg = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
                return {"choices": [{"message": {"role": "assistant", "content": f"Real engine reply to: {user_msg}"}}]}

            srv, sess = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=fake_chat)
            self.assertIsInstance(sess, CompanionWebSession)
            self.assertTrue(detect_lan_ip())
            port = int(srv.server_address[1])
            worker = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            worker.start()
            try:
                html = render_web_index_html()
                self.assertIn("100dvh", html)
                self.assertNotIn("cdn.", html.lower())

                idx_code, idx_body = _http_call(srv, "/")
                self.assertEqual(idx_code, 200)
                self.assertIn("Companion AI OS", idx_body)

                st = _http_json(srv, "/api/status")
                self.assertTrue(st["ok"])
                self.assertIn(str(port), st["lan_url"])

                chat_res = _http_json(srv, "/api/chat", method="POST", payload={"message": "Hello from mobile LAN"})
                self.assertTrue(chat_res["ok"])
                self.assertEqual(chat_res["response"], "Real engine reply to: Hello from mobile LAN")
                self.assertNotIn("[Echo]", chat_res["response"])

                slash_res = _http_json(srv, "/api/chat", method="POST", payload={"message": "/help"})
                self.assertIn("/goal", slash_res["response"])

                _http_json(srv, "/api/settings", method="POST", payload={"tier": "reasoning", "tone": "concise"})
                st_after = _http_json(srv, "/api/status")
                self.assertEqual(st_after["active_model_tier"], "reasoning")
                self.assertEqual(st_after["tone"], "concise")

                hist = _http_json(srv, "/api/history?limit=20")
                self.assertGreaterEqual(hist["count"], 4)
                self.assertTrue(any("Hello from mobile LAN" in m["content"] for m in hist["messages"]))

                tel = _http_json(srv, "/api/telemetry")
                self.assertTrue(tel["ok"])
                self.assertIn("total_traces", tel["kpis"])
            finally:
                srv.shutdown()
                srv.server_close()
                worker.join(timeout=2.0)
                store.close()

    def test_http_smoke_probe_helper_and_workflow_tool_dispatch(self) -> None:
        probe = run_http_smoke_probe(module_name="surfaces.web_server", endpoints=["/", "/api/health", "/api/status"])
        self.assertTrue(probe["passed"], probe["summary"])
        self.assertEqual(len(probe["results"]), 3)

        tool_out = dispatch("workflow_manage", {"action": "http_smoke", "module": "surfaces.web_server"})
        self.assertIn("PASS", tool_out)

    def test_placeholder_and_adr001_scanner_catches_stubs_and_external_frameworks(self) -> None:
        self.assertIn("fastapi", FORBIDDEN_STDLIB_VIOLATION_IMPORTS)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            surf = root / "surfaces"
            surf.mkdir()
            bad_py = surf / "bad_ui.py"
            bad_py.write_text(
                '"""Bad UI."""\nimport fastapi\n\ndef reply(msg: str) -> str:\n    return "[Echo] " + msg\n',
                encoding="utf-8",
            )
            issues = scan_placeholder_violations([bad_py], root=root)
            self.assertEqual(len(issues), 2)
            self.assertTrue(any("Echo" in i for i in issues))
            self.assertTrue(any("fastapi" in i for i in issues))

    def test_autonomous_goal_driver_tier_routing_and_recovery_healing(self) -> None:
        self.assertEqual(PHASE_TIERS["DESIGN"], "reasoning")
        self.assertEqual(resolve_workflow_phase_tier("RECOVER"), "reasoning")
        self.assertEqual(resolve_workflow_phase_tier("IMPLEMENT"), "fast")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(db_path=root / "memory" / "auto.db")
            cp_mgr = CheckpointManager(root=root, backup_dir=root / ".companion_backups", store=store)
            observed_tiers: List[str] = []
            doctor_calls = {"n": 0}

            def fake_chat(_messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                observed_tiers.append(current_tier_name())
                return {"choices": [{"message": {"role": "assistant", "content": "Phase step complete."}}]}

            def fake_doctor(root=None, run_tests: bool = True) -> Dict[str, Any]:
                doctor_calls["n"] += 1
                if doctor_calls["n"] == 1:
                    return {"healthy": False, "checks": [], "summary": "=== Companion System Doctor: ISSUES DETECTED (FAIL) ==="}
                return {"healthy": True, "checks": [], "summary": "=== Companion System Doctor: HEALTHY (PASS) ==="}

            with patch("companion.autonomy.run_system_doctor", side_effect=fake_doctor):
                res = run_autonomous_goal(
                    goal="Build sample capability",
                    max_phase_iters=4,
                    max_recoveries=2,
                    chat_fn=fake_chat,
                    store=store,
                    cp_mgr=cp_mgr,
                    root=root,
                    run_doctor_tests=False,
                    verbose=False,
                )

            self.assertTrue(res["passed"], res["summary"])
            self.assertEqual(res["status"], "COMPLETE")
            self.assertEqual(res["recoveries"], 1)
            # Sequence: DISCOVER(fast), DESIGN(reasoning), PLAN(reasoning), IMPLEMENT(fast), RECOVER(reasoning)
            self.assertEqual(observed_tiers, ["fast", "reasoning", "reasoning", "fast", "reasoning"])
            self.assertIsNone(store.get_active_workflow())
            store.close()

    def test_error_loop_escalates_to_reasoning_and_budget_exit_injects_ground_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "loop.db")
            try:
                step_tiers: List[str] = []
                captured_closing: List[str] = []
                call_idx = {"n": 0}

                def fake_chat(messages: List[Dict[str, Any]], tools=None, tool_choice="auto") -> Dict[str, Any]:
                    if tools is None or tool_choice == "none":
                        captured_closing.append(str(messages[-1].get("content", "")))
                        return {"choices": [{"message": {"role": "assistant", "content": "Honest status: doctor NOT_RUN."}}]}
                    step_tiers.append(current_tier_name())
                    call_idx["n"] += 1
                    return {
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [{"id": f"c{call_idx['n']}", "function": {"name": "read_file", "arguments": json.dumps({"path": f"missing_{call_idx['n']}.py"})}}],
                            }
                        }]
                    }

                msgs: List[Dict[str, Any]] = [{"role": "user", "content": "Trigger consecutive tool errors"}]
                with patch.object(settings, "tiers_share_endpoint", return_value=False):
                    out = run_agent_turn(list(msgs), max_iters=3, chat_fn=fake_chat, store=store, verbose=False)
                self.assertEqual(out, "Honest status: doctor NOT_RUN.")
                self.assertEqual(step_tiers, ["fast", "fast", "reasoning"])
                self.assertTrue(any("system_doctor_in_this_turn=`NOT_RUN`" in c for c in captured_closing))

                step_tiers.clear()
                with patch.object(settings, "tiers_share_endpoint", return_value=True):
                    run_agent_turn(list(msgs), max_iters=3, chat_fn=fake_chat, store=store, verbose=False)
                self.assertEqual(step_tiers, ["fast", "fast", "reasoning"])
            finally:
                store.close()

    def test_windows_shell_bashisms_and_multiline_paste_coalescing(self) -> None:
        if os.name == "nt":
            norm_cmd, ignore_err = normalize_windows_shell_command("taskkill /F /IM nonexistent_xyz_99.exe 2>/dev/null || true")
            self.assertTrue(ignore_err)
            self.assertIn("2>nul", norm_cmd)
            self.assertNotIn("|| true", norm_cmd)
            res = tool_run_shell({"command": "taskkill /F /IM nonexistent_xyz_99.exe 2>/dev/null || true"})
            self.assertIn("exit=0", res)

        buffered = iter(["second pasted line\n", "third pasted line\n"])
        remaining = [True, True, False]
        merged = _coalesce_pasted_lines("first pasted line\n", lambda: remaining.pop(0), lambda: next(buffered))
        self.assertEqual(merged, "first pasted line\nsecond pasted line\nthird pasted line")

    def test_api_search_endpoint_returns_results(self) -> None:
        """GET /api/search?q=test → 200, ok=True, results list."""
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "web_test.db")

            def fake_chat(messages: List[Dict[str, Any]], tools=None, tool_choice="auto") -> Dict[str, Any]:
                return {"choices": [{"message": {"role": "assistant", "content": "echo"}}]}

            srv, sess = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=fake_chat)
            self.assertIsInstance(sess, CompanionWebSession)
            port = int(srv.server_address[1])
            worker = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            worker.start()
            try:
                code, body = _http_call(srv, "/api/search?q=test")
                self.assertEqual(code, 200)
                st = json.loads(body)
                self.assertTrue(st["ok"])
                self.assertIn("results", st)
                self.assertIsInstance(st["results"], list)
            finally:
                srv.shutdown()
                srv.server_close()
                worker.join(timeout=2.0)
                store.close()

    def test_api_search_empty_query(self) -> None:
        """GET /api/search?q= → 200, ok=True, empty or all results."""
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "web_test.db")

            def fake_chat(messages: List[Dict[str, Any]], tools=None, tool_choice="auto") -> Dict[str, Any]:
                return {"choices": [{"message": {"role": "assistant", "content": "echo"}}]}

            srv, sess = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=fake_chat)
            self.assertIsInstance(sess, CompanionWebSession)
            port = int(srv.server_address[1])
            worker = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            worker.start()
            try:
                code, body = _http_call(srv, "/api/search?q=")
                self.assertEqual(code, 200)
                st = json.loads(body)
                self.assertTrue(st["ok"])
                self.assertIn("results", st)
                # empty query should return ok=True with results list (possibly empty)
                self.assertIsInstance(st["results"], list)
            finally:
                srv.shutdown()
                srv.server_close()
                worker.join(timeout=2.0)
                store.close()

    def test_status_includes_recent_savepoints(self) -> None:
        """GET /api/status → 200 and recent_savepoints key present (list)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "web_test.db")

            def fake_chat(messages: List[Dict[str, Any]], tools=None, tool_choice="auto") -> Dict[str, Any]:
                return {"choices": [{"message": {"role": "assistant", "content": "echo"}}]}

            srv, sess = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=fake_chat)
            self.assertIsInstance(sess, CompanionWebSession)
            port = int(srv.server_address[1])
            worker = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            worker.start()
            try:
                code, body = _http_call(srv, "/api/status")
                self.assertEqual(code, 200)
                st = json.loads(body)
                self.assertTrue(st["ok"])
                self.assertIn("recent_savepoints", st)
                self.assertIsInstance(st["recent_savepoints"], list)
            finally:
                srv.shutdown()
                srv.server_close()
                worker.join(timeout=2.0)
                store.close()

    def test_embedded_js_syntax_gate_and_fts5_search_hits(self) -> None:
        """Embedded `<script>` / `_WEB_JS` validator catches Pythonisms in JS and FTS5 returns logged messages."""
        from companion.disciplines import validate_embedded_scripts
        from companion.safety import lint_single_file_edit

        html = render_web_index_html()
        self.assertIsNone(validate_embedded_scripts(html, "index.html"))

        bad_py = '"""Web asset."""\n_WEB_JS = """const x = `${esc(sp.label or sp[:50])}`;"""\n'
        err_py, _ = lint_single_file_edit(Path("surfaces/fake_assets.py"), bad_py, op_name="edit_file")
        self.assertIsNotNone(err_py)
        self.assertIn("JavaScript SyntaxError", str(err_py))

        bad_html = "<html><body><script>const y = items[:10];</script></body></html>"
        err_html, _ = lint_single_file_edit(Path("surfaces/index.html"), bad_html, op_name="write_file")
        self.assertIsNotNone(err_html)
        self.assertIn("JavaScript SyntaxError", str(err_html))

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "fts_web.db")
            store.log_message("user", "quantum hyperdrive calibration complete")
            srv, _ = create_web_server(host="127.0.0.1", port=0, store=store)
            try:
                code, body = dispatch_raw_http(srv, "GET", "/api/search?q=hyperdrive")
                self.assertEqual(code, 200)
                payload = json.loads(body)
                self.assertEqual(payload["count"], 1)
                self.assertIn("hyperdrive", payload["results"][0]["content"])
            finally:
                srv.server_close()
                store.close()

    def test_async_plan_exception_transitions_job_to_error_without_hanging(self) -> None:
        """Async `/api/chat` in `plan`/`goal` mode records `status='error'` and clears `active_job_id` on LLM timeout."""
        import time

        self.assertEqual(settings.resolve_tier("reasoning").max_tokens, 4096)
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "err_job.db")

            def boom_chat(_m, _t=None, _c="auto"):
                raise RuntimeError("simulated gateway timeout")

            srv, sess = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=boom_chat)
            try:
                code, start = sess.submit_async_chat("Design a widget", mode="plan")
                self.assertEqual(code, 202)
                jid = start["job_id"]
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    _, jdata = sess.get_job_payload(jid)
                    if jdata["job"]["status"] != "running":
                        break
                    time.sleep(0.05)
                self.assertEqual(jdata["job"]["status"], "error")
                self.assertIn("simulated gateway timeout", jdata["job"]["error"])
                self.assertEqual(sess.active_job_id, "")
            finally:
                srv.server_close()
                store.close()


if __name__ == "__main__":
    unittest.main()
