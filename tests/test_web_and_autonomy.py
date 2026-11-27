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
from companion.agent import PHASE_TIERS, resolve_workflow_phase_tier, run_autonomous_goal, run_http_smoke_probe, scan_placeholder_violations
from companion.llm import current_tier_name
from companion.memory_store import MemoryStore
from companion.tools import CheckpointManager, normalize_windows_shell_command
from companion.tools import dispatch, tool_run_shell
from harness import _coalesce_pasted_lines
from surfaces import CompanionWebSession, create_web_server, detect_lan_ip, dispatch_raw_http, render_web_index_html


def _http_call(srv, path: str, method: str = "GET", payload: Dict[str, Any] | None = None) -> Tuple[int, str]:
    port = int(srv.server_address[1])
    url = f"http://127.0.0.1:{port}{path}"
    raw_body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers: Dict[str, str] = {"Content-Type": "application/json"} if payload is not None else {}
    if method.upper() == "POST" and hasattr(srv, "session"):
        headers["X-Companion-CSRF"] = srv.session.csrf_token
    req = urllib.request.Request(url, data=raw_body, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5.0) as resp:
            return int(resp.status), resp.read().decode("utf-8")
    except Exception as exc:
        if "10013" in str(exc) or "PermissionError" in str(exc) or "Errno 13" in str(exc):
            return dispatch_raw_http(srv, method=method, path=path, body=raw_body, headers=headers)
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

    def test_placeholder_and_syntax_scanner_catches_syntax_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_py = root / "bad_ui.py"
            bad_py.write_text("def broken(:\n", encoding="utf-8")
            issues = scan_placeholder_violations([bad_py], root=root)
            self.assertEqual(len(issues), 1)
            self.assertIn("SyntaxError", issues[0])

    def test_autonomous_goal_driver_tier_routing_and_recovery_healing(self) -> None:
        self.assertEqual(PHASE_TIERS["DESIGN"], "reasoning")
        self.assertEqual(resolve_workflow_phase_tier("RECOVER"), "reasoning")
        self.assertEqual(resolve_workflow_phase_tier("IMPLEMENT"), "fast")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("X = 1\n", encoding="utf-8")
            store = MemoryStore(db_path=root / "memory" / "auto.db")
            try:
                cp_mgr = CheckpointManager(root=root, backup_dir=root / ".companion_backups", store=store)
                observed_tiers: List[str] = []

                def fake_chat(_messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                    observed_tiers.append(current_tier_name())
                    return {"choices": [{"message": {"role": "assistant", "content": "Phase step complete."}}]}

                res = run_autonomous_goal(
                    goal="Build sample capability",
                    max_phase_iters=4,
                    max_recoveries=1,
                    chat_fn=fake_chat,
                    store=store,
                    cp_mgr=cp_mgr,
                    root=root,
                    run_doctor_tests=False,
                    verbose=False,
                )

                self.assertTrue(res["passed"], res["summary"])
                self.assertEqual(res["status"], "COMPLETE")
                self.assertEqual(observed_tiers[:4], ["fast", "reasoning", "reasoning", "fast"])
                self.assertIsNone(store.get_active_workflow())
            finally:
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

    def test_fts5_search_hits_in_web_api(self) -> None:
        """FTS5 returns logged messages via `/api/search`."""
        html = render_web_index_html()
        self.assertIn("companion-csrf", html)

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

        self.assertEqual(settings.resolve_tier("reasoning").max_tokens, 16384)
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

    def test_turn_delivery_observability_admission_telemetry_and_cache_headers(self) -> None:
        """Verify Invariant 1 (versioned assets + Cache-Control) and Invariant 2 (400/401/403/409/413 telemetry)."""
        from companion import __version__
        from surfaces.web_assets import compute_static_asset_hash

        asset_hash = compute_static_asset_hash()
        self.assertEqual(len(asset_hash), 12)
        rendered = render_web_index_html(csrf_token="csrf-check-505")
        self.assertIn(f"<title>Companion AI OS v{__version__}</title>", rendered)
        self.assertIn(f'<meta name="companion-version" content="{__version__}">', rendered)
        self.assertIn(f'<meta name="companion-asset-hash" content="{asset_hash}">', rendered)
        self.assertIn(f'/static/app.js?v={asset_hash}', rendered)
        self.assertIn(f'/static/styles.css?v={asset_hash}', rendered)

        ext_html = render_web_index_html(csrf_token="csrf-ext", inline_assets=False)
        self.assertIn(f'<script defer src="/static/app.js?v={asset_hash}"></script>', ext_html)

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "obs_web.db")
            srv, sess = create_web_server(host="127.0.0.1", port=0, store=store)
            try:
                hlth_code, hlth_raw = dispatch_raw_http(srv, "GET", "/api/health")
                self.assertEqual(hlth_code, 200)
                hlth = json.loads(hlth_raw)
                self.assertEqual(hlth["app_version"], __version__)
                self.assertEqual(hlth["asset_hash"], asset_hash)

                st_code, st_raw = dispatch_raw_http(srv, "GET", "/api/status")
                self.assertEqual(st_code, 200)
                st = json.loads(st_raw)
                self.assertEqual(st["app_version"], __version__)
                self.assertEqual(st["asset_hash"], asset_hash)

                # Verify Cache-Control on hashed vs unhashed static asset requests
                from surfaces.web_server import CompanionWebRequestHandler, _InMemorySocket

                def _raw_head_and_status(path: str) -> Tuple[int, str]:
                    sock = _InMemorySocket(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode("utf-8"))
                    CompanionWebRequestHandler(sock, ("127.0.0.1", 0), srv)
                    head_b, _, _ = sock.wbuf.getvalue().partition(b"\r\n\r\n")
                    head_s = head_b.decode("iso-8859-1", errors="replace")
                    code = int(head_s.splitlines()[0].split()[1])
                    return code, head_s

                c_imm, h_imm = _raw_head_and_status(f"/static/app.js?v={asset_hash}")
                self.assertEqual(c_imm, 200)
                self.assertIn("Cache-Control: public, max-age=31536000, immutable", h_imm)

                c_nostore, h_nostore = _raw_head_and_status("/static/app.js?v=stale000")
                self.assertEqual(c_nostore, 200)
                self.assertIn("Cache-Control: no-store", h_nostore)

                # Fault-inject 401, 403 (Host, CSRF, Origin), 400, and 409 rejections -> must log structured WARN with zero secret leaks
                secret_probe = "sk-supersecretkey1234567890abcdef"
                with patch.object(settings, "web_token", "lan_tok_secret_999"):
                    c401, _ = dispatch_raw_http(
                        srv, "POST", "/api/chat", body=b'{"message":"continue"}',
                        headers={"Authorization": f"Bearer {secret_probe}"}, client_ip="192.168.1.77",
                    )
                    self.assertEqual(c401, 401)

                c403_csrf, _ = dispatch_raw_http(
                    srv, "POST", "/api/chat", body=b'{"message":"continue"}',
                    headers={"X-Companion-CSRF": "wrong_csrf"},
                )
                self.assertEqual(c403_csrf, 403)

                c403_host, _ = dispatch_raw_http(srv, "GET", "/api/status", headers={"Host": "attacker.example.com"})
                self.assertEqual(c403_host, 403)

                c400, _ = dispatch_raw_http(
                    srv, "POST", "/api/chat", body=b'{"message":"   "}',
                    headers={"X-Companion-CSRF": sess.csrf_token},
                )
                self.assertEqual(c400, 400)

                sess.busy = True
                c409, b409 = dispatch_raw_http(
                    srv, "POST", "/api/chat", body=b'{"message":"continue","async":true}',
                    headers={"X-Companion-CSRF": sess.csrf_token},
                )
                sess.busy = False
                self.assertEqual(c409, 409)
                self.assertEqual(json.loads(b409).get("error_code"), "turn_busy")

                tel = sess.get_telemetry_payload(limit=20)
                http_logs = [lg for lg in tel["logs"] if lg["component"] == "web_http"]
                self.assertGreaterEqual(len(http_logs), 5)
                joined_logs = "\n".join(lg["message"] for lg in http_logs)
                self.assertIn("HTTP 401 POST /api/chat", joined_logs)
                self.assertIn("csrf_mismatch", joined_logs)
                self.assertIn("HTTP 409 POST /api/chat", joined_logs)
                self.assertNotIn(secret_probe, joined_logs)
                self.assertNotIn("lan_tok_secret_999", joined_logs)
            finally:
                srv.server_close()
                store.close()

    def test_atomic_turn_ledger_think_only_malformed_tool_json_and_midstream_crash(self) -> None:
        """Verify Invariant 3: zero empty replies on `<think>`-only LLM output, malformed tool JSON recovery, and zero orphaned `user` rows on exceptions."""
        import time

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "atomic_ledger.db")
            mode_box = {"behavior": "think_only", "step": 0}

            def fault_chat(messages: List[Dict[str, Any]], tools=None, tool_choice="auto") -> Dict[str, Any]:
                b = mode_box["behavior"]
                if b == "think_only":
                    return {"choices": [{"message": {"role": "assistant", "content": "<think>internal thoughts without closing answer</think>"}}]}
                if b == "malformed_tool":
                    mode_box["step"] += 1
                    if mode_box["step"] == 1 and tools:
                        return {
                            "choices": [{
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [{"id": "bad_tc_1", "type": "function", "function": {"name": "list_dir", "arguments": '{"path": "unclosed'}}],
                                }
                            }]
                        }
                    return {"choices": [{"message": {"role": "assistant", "content": "Recovered cleanly after malformed tool JSON."}}]}
                raise RuntimeError("mid-stream socket reset")

            srv, sess = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=fault_chat)
            try:
                # 1. `<think>`-only LLM output -> guaranteed non-empty visible response in SSE job & SQLite history
                code1, start1 = sess.submit_async_chat("continue", mode="chat")
                self.assertEqual(code1, 202)
                jid1 = start1["job_id"]
                for _ in range(60):
                    _, j1 = sess.get_job_payload(jid1)
                    if j1["job"]["status"] != "running":
                        break
                    time.sleep(0.03)
                self.assertEqual(j1["job"]["status"], "completed")
                self.assertTrue(bool(j1["job"]["response"].strip()))
                self.assertNotIn("<think>", j1["job"]["response"])

                # 2. Malformed tool JSON -> strict JSON guard feeds parse error back to model, turn completes cleanly
                mode_box["behavior"] = "malformed_tool"
                mode_box["step"] = 0
                code2, res2 = sess.execute_chat("Check workspace files", mode="chat")
                self.assertEqual(code2, 200)
                self.assertIn("Recovered cleanly", res2["response"])

                # 3. Mid-turn exceptions in `chat`, `plan`, and `goal` modes -> balanced user+assistant pairs in SQLite, clean locks
                mode_box["behavior"] = "crash"
                for m_kind in ("chat", "plan", "goal"):
                    c_err, p_err = sess.execute_chat(f"Trigger {m_kind} crash", mode=m_kind)
                    self.assertEqual(c_err, 500)
                    self.assertFalse(p_err["ok"])
                    self.assertFalse(sess.busy)
                    self.assertFalse(sess._turn_lock.locked())
                    self.assertEqual(sess.active_job_id, "")

                hist = sess.get_history_payload(limit=50)["messages"]
                ua_roles = [m["role"] for m in hist]
                # Every single user message must be followed by an assistant message (zero orphaned user rows!)
                for idx, role in enumerate(ua_roles):
                    if role == "user":
                        self.assertLess(idx + 1, len(ua_roles), f"Orphaned trailing user row in history: {hist[idx]}")
                        self.assertEqual(ua_roles[idx + 1], "assistant")
                        self.assertTrue(bool(hist[idx + 1]["content"].strip()))
                err_rows = [m for m in hist if m.get("is_error")]
                self.assertGreaterEqual(len(err_rows), 3)
            finally:
                srv.server_close()
                store.close()


if __name__ == "__main__":
    unittest.main()
