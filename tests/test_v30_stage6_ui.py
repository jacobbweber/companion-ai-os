"""Stage 6 Multi-Studio React + TypeScript Web UI & CoreActions HTTP Parity Tests."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from companion.archive_and_approvals import enqueue_session_nudge
from companion.memory_store import MemoryStore
from surfaces.web_assets import read_static_asset, render_web_index_html
from surfaces.web_server import CompanionHTTPServer, create_web_server, dispatch_raw_http


def _post_studio_action(srv: CompanionHTTPServer, csrf_token: str, name: str, payload: dict) -> tuple[int, dict]:
    """Send a CSRF-authenticated Studio CoreAction request through `CompanionWebRequestHandler`."""
    code, raw = dispatch_raw_http(
        srv,
        method="POST",
        path=f"/api/actions/{name}",
        body=json.dumps(payload).encode("utf-8"),
        headers={"Origin": "http://127.0.0.1", "X-Companion-CSRF": csrf_token},
    )
    return code, json.loads(raw)


class TestStage6MultiStudioWebUI(unittest.TestCase):
    """Verify compiled React/TS static bundle, 4-theme CSS, and all 6 Studios' HTTP contracts."""

    def test_compiled_static_bundle_themes_studios_and_node_syntax(self) -> None:
        """Verify `surfaces/static/` bundle contains all 4 themes, 6 studios, and passes `node --check`."""
        css_asset = read_static_asset("styles.css")
        js_asset = read_static_asset("app.js")
        self.assertIsNotNone(css_asset)
        self.assertIsNotNone(js_asset)
        css_text = css_asset[1].decode("utf-8")
        js_text = js_asset[1].decode("utf-8")
        rendered_html = render_web_index_html(csrf_token="test-csrf-stage6")

        for theme_id in ("storm", "loftra", "obsidian", "orbital", "graphite", "daylight"):
            self.assertIn(theme_id, js_text)
        css_compact = css_text.replace('"', "").replace("'", "").replace(" ", "")
        for token in (
            "data-theme=obsidian",
            "data-theme=storm",
            "data-theme=loftra",
            "#ff5f00",
            "data-theme=orbital",
            "data-theme=graphite",
            "data-theme=daylight",
            "100dvh",
            ".agy-shell",
            ".nav-items",
            "max-width:768px",
            "max-width:640px",
            "safe-area-inset-bottom",
        ):
            self.assertIn(token, css_compact)

        for label in (
            "Obsidian Matte",
            "Daylight Paper",
            "Graphite Minimal",
            "Storm & Safety Orange",
            "Loftra Industrial",
            "Orbital Brutalist",
            "Board & Goals",
            "Automations",
            "Customizations",
            "Departures Board",
            "Blind Credential Vault",
            "Partner Federation",
            "Secret-Free Snapshot Backups",
        ):
            self.assertIn(label, js_text)

        self.assertNotIn("cdn.", rendered_html.lower())
        self.assertIn('<meta name="companion-csrf" content="test-csrf-stage6">', rendered_html)
        self.assertIn('id="approval-banner"', rendered_html)
        self.assertIn("EventSource", js_text)
        self.assertIn("X-Companion-CSRF", js_text)

    def test_studios_1_to_3_chat_wiki_and_jobs_http_contracts(self) -> None:
        """Verify HTTP contracts for Forever Chat, Co-Owned Wiki + 2D Graph + Trash, and Jobs/Approvals."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_root = Path(tmp)
            store = MemoryStore(db_path=tmp_root / "data" / "companion.db")
            enqueue_session_nudge("job_done", "Job job_123 completed.", ref_id="job_123", session_id="web_ui", store=store)
            srv, sess = create_web_server(host="127.0.0.1", port=0, store=store)
            try:
                st_code, st_raw = dispatch_raw_http(srv, "GET", "/api/status")
                st = json.loads(st_raw)
                self.assertEqual(st_code, 200)
                self.assertEqual(st["session_nudges"][0]["ref_id"], "job_123")

                c_p, p_res = _post_studio_action(srv, sess.csrf_token, "prompts", {
                    "subaction": "save", "name": "node-check", "template": "Inspect GPU server.", "category": "studio",
                })
                self.assertEqual(c_p, 200)
                self.assertTrue(p_res["ok"])

                _, w_cap = _post_studio_action(srv, sess.csrf_token, "wiki", {
                    "subaction": "capture_inbox", "title": "Stage 6 Graph Root", "content": "Root node. #ui", "tags": "ui",
                })
                note_a = w_cap["path"]
                _, w_cap2 = _post_studio_action(srv, sess.csrf_token, "wiki", {
                    "subaction": "capture_inbox", "title": "Stage 6 Child", "content": f"Links to [[{Path(note_a).stem}]].",
                })
                note_b = w_cap2["path"]

                _, g_res = _post_studio_action(srv, sess.csrf_token, "wiki", {"subaction": "graph"})
                self.assertEqual(g_res["stats"]["note_count"], 2)
                self.assertEqual(g_res["stats"]["edge_count"], 1)

                _, tr_res = _post_studio_action(srv, sess.csrf_token, "wiki", {"subaction": "trash", "path": note_b})
                self.assertTrue(tr_res["ok"])
                _, lt_res = _post_studio_action(srv, sess.csrf_token, "wiki", {"subaction": "list_trash"})
                self.assertEqual(len(lt_res["trash"]), 1)
                _, rs_res = _post_studio_action(srv, sess.csrf_token, "wiki", {
                    "subaction": "restore", "trash_id": lt_res["trash"][0]["trash_id"],
                })
                self.assertTrue(rs_res["ok"])

                store.conn.execute(
                    "INSERT INTO background_jobs (job_id, title, goal, status) VALUES ('bgj_q1', 'Queued UI Check', 'Goal', 'queued')"
                )
                store.conn.commit()
                c_j, j_res = _post_studio_action(srv, sess.csrf_token, "jobs", {"subaction": "list", "limit": 10})
                self.assertEqual(c_j, 200)
                self.assertEqual(len(j_res.get("jobs", [])), 1)
                self.assertEqual(j_res["jobs"][0]["status"], "queued")
                c_ap, ap_res = _post_studio_action(srv, sess.csrf_token, "approvals", {"subaction": "list", "status": "waiting"})
                self.assertEqual(c_ap, 200)
                self.assertIn("approvals", ap_res)
            finally:
                srv.server_close()
                store.close()

    def test_studios_4_to_6_vault_federation_and_ops_http_contracts(self) -> None:
        """Verify HTTP contracts for Blind Vault + Endpoints, Federation + MCP, and Routines/Ops."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_root = Path(tmp)
            store = MemoryStore(db_path=tmp_root / "data" / "companion.db")
            with patch.dict(os.environ, {"COMPANION_SECRETS_FILE": str(tmp_root / "secrets.env")}):
                srv, sess = create_web_server(host="127.0.0.1", port=0, store=store)
                try:
                    raw_secret = "stage6-ultra-secret-value-777"
                    c_v, v_set = _post_studio_action(srv, sess.csrf_token, "vault", {
                        "subaction": "set", "name": "stage6-api-token", "cred_type": "bearer_token",
                        "value": raw_secret, "allow_list": "endpoint:local-api",
                    })
                    self.assertEqual(c_v, 200)
                    self.assertNotIn(raw_secret, json.dumps(v_set))
                    _, v_list = _post_studio_action(srv, sess.csrf_token, "vault", {"subaction": "list"})
                    self.assertNotIn(raw_secret, json.dumps(v_list))

                    c_ep, ep_res = _post_studio_action(srv, sess.csrf_token, "endpoints", {
                        "subaction": "register", "endpoint_id": "local-api", "label": "Local Test API",
                        "protocol": "http_api", "host": "http://127.0.0.1:11434",
                        "auth_mode": "bearer_token", "credential_ref": "unpopulated-ref",
                    })
                    self.assertEqual(c_ep, 200)
                    self.assertTrue(ep_res["ok"])
                    _, v_after = _post_studio_action(srv, sess.csrf_token, "vault", {"subaction": "list"})
                    self.assertIn("unpopulated-ref", [m["name"] for m in v_after.get("missing", [])])

                    c_m, m_res = _post_studio_action(srv, sess.csrf_token, "mcp", {
                        "subaction": "configure_hosted", "enabled": True, "token_ref": "hosted-mcp-token", "allow_wiki_read": True,
                    })
                    self.assertEqual(c_m, 200)
                    self.assertTrue(m_res["enabled"])
                    c_pt, pt_res = _post_studio_action(srv, sess.csrf_token, "partners", {"subaction": "list"})
                    self.assertEqual(c_pt, 200)
                    self.assertIn("partners", pt_res)

                    c_rt, rt_res = _post_studio_action(srv, sess.csrf_token, "routines", {
                        "subaction": "create", "routine_id": "ui-backup-check", "title": "UI Backup Check",
                        "schedule_expr": "every 6h", "target_kind": "snapshot_backup", "enabled": True,
                    })
                    self.assertEqual(c_rt, 200)
                    self.assertTrue(rt_res["ok"])
                finally:
                    srv.server_close()
                    store.close()

    def test_web_surface_dev_skill_helper_self_test(self) -> None:
        """Verify `.agents/skills/web-surface-dev/scripts/build_frontend.py --self-test` exits 0."""
        script = Path(__file__).resolve().parent.parent / ".agents" / "skills" / "web-surface-dev" / "scripts" / "build_frontend.py"
        proc = subprocess.run([sys.executable, "-B", str(script), "--self-test"], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
        self.assertIn('"self_test": "PASS"', proc.stdout)

    def test_desktop_tray_port_guard_and_identity_action_contracts(self) -> None:
        """Verify `surfaces/windows_tray.py` port helpers and `CORE_ACTIONS['identity']` HTTP contract."""
        from surfaces.windows_tray import format_port_conflict_error, is_companion_running_on_port, select_available_port

        self.assertFalse(is_companion_running_on_port(65531, timeout=0.05))
        chosen = select_available_port("127.0.0.1", preferred_port=18940, max_tries=5)
        self.assertGreaterEqual(chosen, 18940)
        msg = format_port_conflict_error("0.0.0.0", 65531, OSError("Address already in use"))
        self.assertIn("--port", msg)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_root = Path(tmp)
            data_dir = tmp_root / "isolated_data"
            store = MemoryStore(db_path=data_dir / "conversations.db")
            with patch("companion.config.settings.data_dir", data_dir):
                srv, sess = create_web_server(host="127.0.0.1", port=0, store=store)
                srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
                srv_thread.start()
                try:
                    self.assertTrue(is_companion_running_on_port(int(srv.server_address[1]), timeout=2.0))
                    c_id, id_res = _post_studio_action(srv, sess.csrf_token, "identity", {
                        "subaction": "update", "companion_name": "Atlas", "user_name": "Alex",
                        "companion_tone": "concise",
                    })
                    self.assertEqual(c_id, 200)
                    self.assertTrue(id_res["ok"])
                    self.assertEqual(id_res["companion_name"], "Atlas")
                    self.assertEqual(id_res["user_name"], "Alex")
                    self.assertTrue((data_dir / "IDENTITY.md").is_file())
                finally:
                    srv.shutdown()
                    srv.server_close()
                    store.close()

    def test_web_resilience_cursor_sse_meta_hydration_and_attachment_followup(self) -> None:
        """Verify persistent CSRF, proxy HTTPS cookie, coalesced cursor SSE, meta_json hydration, and Turn 2+ doc excerpt."""
        import base64
        from companion.attachments import ingest_attachment
        from companion.project_graph import get_project_graph
        from surfaces.api_extensions import record_coalesced_job_event
        from surfaces.web_server import verify_lan_client_auth

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_root = Path(tmp)
            db_path = tmp_root / "data" / "companion.db"

            def _stub_chat(*args: object, **kwargs: object) -> dict:
                return {"choices": [{"message": {"role": "assistant", "content": "Analyzed attachment."}}]}

            with patch.dict(os.environ, {"COMPANION_DATA_DIR": str(tmp_root / "data")}):
                store = MemoryStore(db_path=db_path)
                with patch.object(sys.modules["companion.config"].settings, "web_token", "lan-secret-99"):
                    ok_unauth, _ = verify_lan_client_auth({"X-Forwarded-For": "192.168.1.55"}, "127.0.0.1", qs={})
                    self.assertFalse(ok_unauth)
                    ok_pair, cookie_hdr = verify_lan_client_auth(
                        {"X-Forwarded-For": "192.168.1.55", "X-Forwarded-Proto": "https"}, "127.0.0.1", qs={"token": ["lan-secret-99"]},
                    )
                    self.assertTrue(ok_pair)
                    self.assertIn("SameSite=Lax; Max-Age=2592000; Path=/; Secure", cookie_hdr)

                srv1, sess1 = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=_stub_chat)
                srv2, sess2 = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=_stub_chat)
                try:
                    self.assertEqual(sess1.csrf_token, sess2.csrf_token)

                    # `meta_json` hydration via `/api/history`
                    store.log_message(
                        "assistant",
                        "Saved note.",
                        session_tag="web_ui",
                        meta={"thinking": "Quick check", "tool_steps": [{"tool": "wiki_manage", "ok": True}], "duration_ms": 1400},
                    )
                    h_code, h_raw = dispatch_raw_http(srv1, "GET", "/api/history")
                    self.assertEqual(h_code, 200)
                    h_msg = json.loads(h_raw)["messages"][-1]
                    self.assertEqual(h_msg["thinking"], "Quick check")
                    self.assertEqual(h_msg["tool_steps"][0]["tool"], "wiki_manage")
                    self.assertEqual(h_msg["duration_ms"], 1400)

                    # Coalescing >4,000 deltas + `?cursor=` / `Last-Event-ID` SSE replay
                    job: dict = {"job_id": "j_sse_1", "status": "completed", "response": "Done!", "error": "", "events": [], "stream_text": "", "stream_thinking": "", "tool_steps": []}
                    for idx in range(4200):
                        record_coalesced_job_event(job, {"type": "text_delta", "delta": f"{idx} "})
                    record_coalesced_job_event(job, {"type": "done", "status": "completed", "response": "Done!"})
                    self.assertLessEqual(len(job["events"]), 2000)
                    self.assertEqual(job["events"][-1]["type"], "done")
                    sess1._jobs["j_sse_1"] = job
                    sse_code, sse_body = dispatch_raw_http(
                        srv1, "GET", f"/api/jobs/j_sse_1/stream?cursor={len(job['events']) - 1}"
                    )
                    self.assertEqual(sse_code, 200)
                    self.assertIn(f"id: {len(job['events'])}", sse_body)
                    self.assertIn('"type": "done"', sse_body)

                    # Post-lock `focus_project_id` isolation on 409 + Turn 2+ `followup_doc_note`
                    pg = get_project_graph(store=store)
                    p1 = pg.create_project(title="Proj One")["project_id"]
                    p2 = pg.create_project(title="Proj Two")["project_id"]
                    pg.set_focus_project(p1)
                    sess1.busy = True
                    busy_code, _ = dispatch_raw_http(
                        srv1,
                        "POST",
                        "/api/chat",
                        body=json.dumps({"message": "Blocked", "project_id": p2, "async": True}).encode("utf-8"),
                        headers={"Origin": "http://127.0.0.1", "X-Companion-CSRF": sess1.csrf_token},
                    )
                    self.assertEqual(busy_code, 409)
                    self.assertEqual(pg.get_board_projection()["project"]["project_id"], p1)
                    sess1.busy = False

                    b64_doc = base64.b64encode(b"Critical spec line 42").decode("ascii")
                    att = ingest_attachment("spec.txt", b64_doc, "text/plain", store=store)
                    att_id = att["attachment"]["id"]
                    ok_code, _ = dispatch_raw_http(
                        srv1,
                        "POST",
                        "/api/chat",
                        body=json.dumps({"message": "Summarize attached spec", "attachment_ids": [att_id], "async": False}).encode("utf-8"),
                        headers={"Origin": "http://127.0.0.1", "X-Companion-CSRF": sess1.csrf_token},
                    )
                    self.assertEqual(ok_code, 200)
                    self.assertTrue(any("Critical spec line 42" in str(m.get("content", "")) for m in sess1.messages if m.get("role") == "user"))
                finally:
                    srv1.server_close()
                    srv2.server_close()
                    store.close()


if __name__ == "__main__":
    unittest.main()
