"""Unit tests for Upgrades 7 & 8a: Thread-Safe SQLite, Async Mobile Job Streaming & Multi-Project WorkspaceContext."""

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

from companion.agent import handle_slash_command
from companion.agent import run_autonomous_goal
from companion.config import SELF_PROJECT_ID, settings, workspace_context
from companion.memory_store import MemoryStore, memory_store_context
from companion.telemetry import TelemetryEngine
from companion.tools import phase_tool_context
from companion.tools import dispatch, resolve, tool_project_manage, tool_read_file, tool_write_file
from surfaces import create_web_server, dispatch_raw_http


def _web_call(srv, path: str, method: str = "GET", payload: Dict[str, Any] | None = None) -> Tuple[int, Dict[str, Any]]:
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
            code, text = int(resp.status), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        code, text = int(exc.code), exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        if "10013" in str(exc) or "PermissionError" in str(exc) or "Errno 13" in str(exc):
            code, text = dispatch_raw_http(srv, method=method, path=path, body=raw_body, headers=headers)
        else:
            raise
    return code, json.loads(text)


class TestWorkspaceAndAsyncWeb(unittest.TestCase):
    def tearDown(self) -> None:
        settings.reset_workspace()

    def test_upgrade7a_multithreaded_sqlite_memory_and_telemetry_concurrency(self) -> None:
        """Stress-test shared MemoryStore and TelemetryEngine across 8 concurrent threads (200 writes, 0 errors)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "concurrent.db"
            store = MemoryStore(db_path=db_file)
            tel = TelemetryEngine(db_path=db_file)
            errors: List[str] = []

            def worker(tid_num: int) -> None:
                try:
                    trace_id = tel.start_trace("worker", f"thread-{tid_num}")
                    for i in range(25):
                        store.log_message("user", f"msg {tid_num}-{i}", session_tag=f"t{tid_num}")
                        store.add_system_note(f"note {tid_num}-{i}", category="concurrency")
                        tel.record_span("tool_call", "read_file", 1.2, status="ok", detail=f"{tid_num}:{i}", trace_id=trace_id)
                        _ = store.search_conversations("msg", limit=5)
                    tel.end_trace(trace_id, status="ok")
                except Exception as exc:
                    errors.append(f"Thread {tid_num}: {type(exc).__name__}: {exc}")

            threads = [threading.Thread(target=worker, args=(idx,)) for idx in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15.0)

            self.assertEqual(errors, [], f"Concurrent SQLite errors occurred: {errors}")
            self.assertEqual(tel.get_kpis()["total_traces"], 8)
            self.assertGreaterEqual(len(store.get_recent_messages(limit=250)), 200)
            tel.close()
            store.close()

    def test_upgrade7b_async_web_job_streaming_and_mobile_reconnect(self) -> None:
        """Verify `POST /api/chat` (`async: true` -> 202), live `active_job` status, and `/api/jobs/<id>` polling."""
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "async_web.db")
            release_gate = threading.Event()

            def slow_fake_chat(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                release_gate.wait(timeout=3.0)
                user_msg = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
                return {"choices": [{"message": {"role": "assistant", "content": f"Completed async job for: {user_msg}"}}]}

            srv, _ = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=slow_fake_chat)
            worker = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            worker.start()
            try:
                code, accepted = _web_call(srv, "/api/chat", method="POST", payload={"message": "Build mobile view", "async": True})
                self.assertEqual(code, 202)
                self.assertTrue(accepted["accepted"])
                job_id = accepted["job_id"]

                # Simulate mobile screen wake checking /api/status while job is still running
                st_code, st_live = _web_call(srv, "/api/status")
                self.assertEqual(st_code, 200)
                self.assertEqual(st_live["workspace"]["project_id"], SELF_PROJECT_ID)
                self.assertIsNotNone(st_live["active_job"])
                self.assertEqual(st_live["active_job"]["job_id"], job_id)

                # Unblock model completion and poll /api/jobs/<job_id>
                release_gate.set()
                final_job: Dict[str, Any] = {}
                for _ in range(40):
                    j_code, j_body = _web_call(srv, f"/api/jobs/{job_id}")
                    self.assertEqual(j_code, 200)
                    final_job = j_body["job"]
                    if final_job["status"] != "running":
                        break
                    time.sleep(0.03)

                self.assertEqual(final_job.get("status"), "completed")
                self.assertIn("Completed async job for: Build mobile view", final_job.get("response", ""))
            finally:
                release_gate.set()
                srv.shutdown()
                srv.server_close()
                worker.join(timeout=2.0)
                store.close()

    def test_upgrade8a_multi_project_workspace_context_locks_companion_core_readonly(self) -> None:
        """Verify ADR-007 WorkspaceContext switches roots and mechanically blocks writes to Companion core."""
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = Path(tmp) / "external-demo-app"
            store = MemoryStore(db_path=Path(tmp) / "proj.db")
            try:
                with memory_store_context(store):
                    init_out = handle_slash_command(f"/project init {ext_dir}", [], store=store)
                    self.assertIn("Initialized & activated project", str(init_out))
                    self.assertFalse(settings.get_workspace().is_self_project)
                    self.assertEqual(resolve("app.py", for_write=True), ext_dir.resolve() / "app.py")

                    # Writing inside external project succeeds cleanly
                    w_ok = tool_write_file({"path": "app.py", "content": '"""External app."""\nVALUE = 7\n'})
                    self.assertIn("wrote", w_ok)
                    self.assertTrue((ext_dir / "app.py").is_file())

                    # Attempting to overwrite Companion's own harness.py while external project is active MUST be mechanically blocked!
                    core_target = settings.root / "harness.py"
                    w_blocked = tool_write_file({"path": str(core_target), "content": "# blocked"})
                    self.assertIn("SAFETY GUARD REJECTED path", w_blocked)
                    self.assertIn("READ-ONLY", w_blocked)

                    # Companion can still read its own skills/ runbooks for guidance while in external mode
                    skill_read = tool_read_file({"path": "skills/proactive-checkin/SKILL.md", "end_line": 5})
                    self.assertIn("proactive-checkin", skill_read)

                    # Context manager & /project self restore companion-self cleanly
                    back_out = dispatch("project_manage", {"action": "self"})
                    self.assertIn(SELF_PROJECT_ID, back_out)
                    self.assertTrue(settings.get_workspace().is_self_project)

                    with workspace_context(ext_dir):
                        self.assertEqual(settings.active_project_root, ext_dir.resolve())
                    self.assertTrue(settings.get_workspace().is_self_project)
            finally:
                settings.reset_workspace()
                store.close()

    def test_project_manage_allowed_in_implement_and_recover_but_locked_in_read_only_phases(self) -> None:
        """Verify `project_manage(init|switch|self)` works in `IMPLEMENT`/`RECOVER` while staying locked in read-only phases."""
        with tempfile.TemporaryDirectory() as tmp:
            ext_a = Path(tmp) / "impl-proj-alpha"
            ext_b = Path(tmp) / "impl-proj-beta"
            ext_b.mkdir()
            store = MemoryStore(db_path=Path(tmp) / "phase_proj.db")
            try:
                with memory_store_context(store):
                    for ro_phase in ("INTAKE", "DISCOVER", "DESIGN", "PLAN", "VERIFY", "REFLECT"):
                        with phase_tool_context(ro_phase):
                            self.assertIn("PHASE TOOL GATE REJECTED", dispatch("project_manage", {"action": "init", "target": str(ext_a)}))
                            self.assertIn("PHASE TOOL GATE REJECTED", dispatch("project_manage", {"action": "", "target": str(ext_b)}))
                            self.assertIn("PHASE TOOL GATE REJECTED", dispatch("project_manage", {"target": str(ext_b)}))
                            self.assertIn("PHASE TOOL GATE REJECTED", dispatch("project_manage", {"action": "create", "target": str(ext_a)}))
                            self.assertIn("PHASE TOOL GATE REJECTED", dispatch("project_manage", {"action": "my-ext-app"}))
                            self.assertIn("PHASE TOOL GATE REJECTED", dispatch("project_manage", {"action": f"switch {ext_b}"}))
                            self.assertIn("PHASE TOOL GATE REJECTED", tool_project_manage({"action": "switch", "target": str(ext_b)}))
                            self.assertIn("PHASE TOOL GATE REJECTED", tool_project_manage({"action": "self"}))
                            self.assertIn("Active WorkspaceContext", dispatch("project_manage", {"action": "status"}))
                            self.assertIn("Active WorkspaceContext", dispatch("project_manage", {"action": "current"}))
                            self.assertIn("Registered Workspaces", dispatch("project_manage", {"action": "list"}))
                            self.assertTrue(settings.get_workspace().is_self_project)

                    with phase_tool_context("IMPLEMENT"):
                        r_init = dispatch("project_manage", {"action": "init", "target": str(ext_a)})
                        self.assertIn("Initialized & activated project", r_init)
                        self.assertEqual(settings.active_project_root, ext_a.resolve())
                        w_res = dispatch("write_file", {"path": "config.toml", "content": 'port = 8088\n'})
                        self.assertIn("wrote", w_res)
                        self.assertTrue((ext_a / "config.toml").is_file())

                        r_sw = dispatch("project_manage", {"action": "switch", "target": str(ext_b)})
                        self.assertIn("Switched active WorkspaceContext", r_sw)
                        self.assertEqual(settings.active_project_root, ext_b.resolve())

                    with phase_tool_context("RECOVER"):
                        r_self = dispatch("project_manage", {"action": "self"})
                        self.assertIn(SELF_PROJECT_ID, r_self)
                        self.assertTrue(settings.get_workspace().is_self_project)
            finally:
                settings.reset_workspace()
                store.close()

    def test_autonomous_goal_rebinds_workspace_and_workflow_on_mid_implement_project_init(self) -> None:
        """Verify mid-`IMPLEMENT` `project_manage(init)` rebinds `target_root`, workflow `project_id`, tasks, and `REFLECT` facts."""
        def _resp(content: str = "", tool_calls: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
            msg: Dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            return {"choices": [{"message": msg}]}

        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = Path(tmp) / "ext_calc_proj"
            store = MemoryStore(db_path=Path(tmp) / "mid_switch.db")
            state = {"planned": False, "built": False}
            try:
                settings.reset_workspace()

                def fake_chat(messages: List[Dict[str, Any]], tools: Any = None, tool_choice: str = "auto") -> Dict[str, Any]:
                    prompt_blob = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
                    last_role = messages[-1].get("role") if messages else ""
                    if "Inspect relevant workspace files" in prompt_blob or "Produce a concise technical design" in prompt_blob:
                        return _resp("Design ready: initialize ext_calc_proj and build calc.py + tests/test_calc.py.")
                    if "Batch 1-4 atomic `task_manage" in prompt_blob:
                        if last_role != "tool" and not state["planned"]:
                            state["planned"] = True
                            return _resp(tool_calls=[{
                                "id": "tc_plan", "type": "function",
                                "function": {"name": "task_manage", "arguments": json.dumps({"action": "add_task", "description": "Create calc.py and unit tests in external project"})},
                            }])
                        return _resp("Plan ready.")
                    if last_role != "tool" and not state["built"]:
                        state["built"] = True
                        return _resp(tool_calls=[
                            {"id": "tc_init", "type": "function", "function": {"name": "project_manage", "arguments": json.dumps({"action": "init", "target": str(ext_dir)})}},
                            {"id": "tc_src", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "calc.py", "content": '"""Calculator module."""\n\ndef add(a: int, b: int) -> int:\n    return a + b\n'})}},
                            {"id": "tc_tst", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "tests/test_calc.py", "content": '"""Calc tests."""\nimport unittest\nfrom calc import add\n\nclass TestCalc(unittest.TestCase):\n    def test_add(self) -> None:\n        self.assertEqual(add(2, 3), 5)\n'})}},
                        ])
                    return _resp("Implemented and verified external calc project.")

                res = run_autonomous_goal(
                    "Initialize external calc project and implement add(a, b) in calc.py with unit tests",
                    chat_fn=fake_chat, store=store, run_doctor_tests=True, rollback_on_failure=False, verbose=False,
                )
                self.assertEqual(res["status"], "COMPLETE", res["summary"])
                self.assertTrue(res["passed"])
                ext_pid = settings.get_workspace().project_id
                self.assertNotEqual(ext_pid, SELF_PROJECT_ID)
                self.assertIsNone(store.get_active_workflow(project_id=SELF_PROJECT_ID))
                self.assertEqual(store.get_all_tasks(project_id=SELF_PROJECT_ID), [])
                ext_tasks = store.get_all_tasks(project_id=ext_pid)
                self.assertEqual(len(ext_tasks), 1)
                self.assertEqual(ext_tasks[0]["status"], "completed")
                wf_row = store.conn.execute("SELECT project_id, current_state, status FROM workflow_runs WHERE id = ?", (int(res["workflow_id"]),)).fetchone()
                self.assertEqual(wf_row, (ext_pid, "COMPLETE", "completed"))
                facts = store.query_knowledge_facts("workflow:", project_id=ext_pid)
                self.assertGreaterEqual(len(facts), 1)
            finally:
                settings.reset_workspace()
                store.close()

    def test_web_serialize_job_isolates_workflow_state_across_projects(self) -> None:
        """Verify `_serialize_job` queries `workflow_runs` scoped to the active `project_id`."""
        from surfaces.web_server import CompanionWebSession

        with tempfile.TemporaryDirectory() as tmp:
            ext_a, ext_b = Path(tmp) / "proj_a", Path(tmp) / "proj_b"
            ext_a.mkdir()
            ext_b.mkdir()
            store = MemoryStore(db_path=Path(tmp) / "web_iso.db")
            try:
                store.create_workflow_run("engineering_feature", "Alpha goal", "PLAN", project_id="proj-a")
                web = CompanionWebSession(store=store)
                sample_job = {
                    "job_id": "j1", "mode": "chat", "message": "hi", "status": "running",
                    "elapsed_seconds": 0.2, "created_at": "now",
                }
                with workspace_context(ext_a, "proj-a"):
                    self.assertEqual(web._serialize_job(sample_job)["workflow_state"], "PLAN")
                with workspace_context(ext_b, "proj-b"):
                    self.assertEqual(web._serialize_job(sample_job)["workflow_state"], "IDLE")

                # Status polling does not clobber active workspace; update_settings refreshes system prompt
                store.set_user_profile("active_project_path", str(ext_a))
                store.set_user_profile("active_project_id", "proj-a")
                settings.reset_workspace()
                st_payload = web.get_status_payload()
                self.assertTrue(st_payload["ok"])
                self.assertTrue(settings.get_workspace().is_self_project)
                web.update_settings({"tone": "concise"})
                self.assertIn("concise", web.messages[0]["content"].lower())
            finally:
                store.close()

    def test_cancel_scope_wakes_blocked_socket_under_100ms(self) -> None:
        """Verify `CancelScope.cancel()` aborts a blocked `urllib.request.urlopen` `readline()` socket in `<100ms`."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        import time
        import urllib.request
        from companion.streaming import CancelScope

        release_srv = threading.Event()

        class _FrozenSSEHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: ping\n\n")
                self.wfile.flush()
                release_srv.wait(timeout=10.0)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _FrozenSSEHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        scope = CancelScope()
        blocked = threading.Event()
        woke = threading.Event()

        def _reader() -> None:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{srv.server_address[1]}/", timeout=15) as resp:
                with scope.track_response(resp):
                    _ = resp.readline()
                    _ = resp.readline()
                    blocked.set()
                    try:
                        _ = resp.readline()
                    except OSError:
                        pass
            woke.set()

        try:
            threading.Thread(target=_reader, daemon=True).start()
            self.assertTrue(blocked.wait(timeout=2.0))
            time.sleep(0.03)
            t0 = time.perf_counter()
            scope.cancel()
            self.assertTrue(woke.wait(timeout=1.0))
            wake_ms = (time.perf_counter() - t0) * 1000.0
            self.assertLess(wake_ms, 100.0)
        finally:
            release_srv.set()
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
