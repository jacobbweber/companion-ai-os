"""Regression tests for security, governance, web auth/CSRF, SSRF/proxy, and concurrency hardening."""

import base64
import http.server
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from companion.archive_and_approvals import ApprovalQueue, peek_session_nudges, set_trust_mode
from companion.background_jobs import BackgroundJobManager
from companion.config import agent_tool_turn_context, settings
from companion.core_actions import execute_core_action
from companion.endpoints import EndpointManager
from companion.llm import ModelGate
from companion.llm_wire import urlopen_safe
from companion.memory_store import MemoryStore
from companion.safety import check_command_safety, check_path_safety
from companion.vault import VaultManager, restore_snapshot_backup
from companion.workflow_graph import workflow_engine
from surfaces.web_server import (
    create_web_server,
    dispatch_raw_http,
    is_allowed_lan_host,
    verify_lan_client_auth,
    verify_post_request_security,
)


class _PingHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, _fmt: str, *_args: object) -> None:
        return


class TestSecurityAndHardening(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp_path = Path(self._tmp.name)
        self._orig_data_dir = settings.data_dir
        self._orig_web_token = settings.web_token
        settings.data_dir = self.tmp_path / "data"
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = MemoryStore(db_path=settings.db_path)

    def tearDown(self) -> None:
        self.store.conn.close()
        settings.data_dir = self._orig_data_dir
        settings.web_token = self._orig_web_token
        self._tmp.cleanup()

    def test_agent_tool_turn_strips_confirmed_and_blocks_privilege_escalation(self) -> None:
        vm = VaultManager(store=self.store, secrets_path=settings.data_dir / "secrets.env")
        vm.set_secret("prod-key", "super-secret-value-123", cred_type="api_key", allow_list=["provider:openai"], actor="user")
        aq = ApprovalQueue(store=self.store)
        _ok, queued = aq.check_or_enqueue("vault", "delete", "prod-key", "Delete prod-key", payload={"name": "prod-key"})
        apr_id = str(queued["approval_id"])

        with agent_tool_turn_context():
            del_res = execute_core_action(
                "vault",
                {"subaction": "delete", "name": "prod-key", "confirmed": True},
                actor="user",
                store=self.store,
            )
            self.assertFalse(bool(del_res.get("ok")))
            res_apr = aq.resolve(apr_id, "approved", actor="user")
            self.assertFalse(bool(res_apr.get("ok")))
            trust_res = set_trust_mode("autonomous", actor="user", store=self.store)
            self.assertFalse(bool(trust_res.get("ok")))
            priv = execute_core_action(
                "models",
                {"subaction": "set_privacy", "privacy_mode": "hybrid"},
                actor="user",
                store=self.store,
            )
            self.assertFalse(bool(priv.get("ok")))

        entries = {e["name"]: e for e in vm.list_secrets()}
        self.assertTrue(entries["prod-key"]["is_set"])

    def test_vault_declare_placeholder_preserves_scope_and_restore_confines_path(self) -> None:
        secrets_file = settings.data_dir / "secrets.env"
        vm = VaultManager(store=self.store, secrets_path=secrets_file)
        vm.set_secret(
            "ssh-prod",
            "PRIVATE_KEY_MATERIAL_999",
            cred_type="ssh_key",
            allow_list=["endpoint:prod-node"],
            actor="user",
        )
        vm.declare_placeholder(
            "ssh-prod",
            cred_type="bearer_token",
            allow_list=["endpoint:*"],
            description="attempted scope widening",
        )
        entry = next(e for e in vm.list_secrets() if e["name"] == "ssh-prod")
        self.assertEqual(entry["cred_type"], "ssh_key")
        self.assertEqual(entry["allow_list"], ["endpoint:prod-node"])

        outside_dir = self.tmp_path / "outside_snap"
        outside_dir.mkdir(parents=True, exist_ok=True)
        (outside_dir / "manifest.json").write_text('{"snapshot_id": "outside_snap"}', encoding="utf-8")
        bad_restore = restore_snapshot_backup(
            "../outside_snap",
            actor="user",
            store=self.store,
            snapshots_dir=settings.data_dir / "snapshots",
        )
        self.assertFalse(bool(bad_restore.get("ok")))

    def test_web_csrf_host_guard_lan_token_and_attachment_headers(self) -> None:
        self.assertFalse(is_allowed_lan_host(""))
        self.assertFalse(is_allowed_lan_host("evil.attacker.example"))
        self.assertIsNotNone(
            verify_post_request_security({"Content-Type": "application/json"}, expected_csrf="csrf_abc")
        )
        self.assertIsNone(
            verify_post_request_security(
                {"Content-Type": "application/json", "X-Companion-CSRF": "csrf_abc"},
                expected_csrf="csrf_abc",
            )
        )

        settings.web_token = "lan_pair_tok_777"
        ok_unauth, _ = verify_lan_client_auth({}, "192.168.1.44")
        self.assertFalse(ok_unauth)
        ok_bearer, _ = verify_lan_client_auth({"Authorization": "Bearer lan_pair_tok_777"}, "192.168.1.44")
        self.assertTrue(ok_bearer)
        ok_pair, set_cookie = verify_lan_client_auth({}, "100.75.10.9", qs={"token": ["lan_pair_tok_777"]})
        self.assertTrue(ok_pair)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)

        srv, sess = create_web_server(host="127.0.0.1", port=0, store=self.store)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            csrf = sess.csrf_token
            code_401, _ = dispatch_raw_http(srv, "GET", "/api/status", client_ip="192.168.1.55")
            self.assertEqual(code_401, 401)
            code_hlth, hlth_body = dispatch_raw_http(srv, "GET", "/api/health", client_ip="192.168.1.55")
            self.assertEqual(code_hlth, 200)
            self.assertNotIn("csrf_token", json.loads(hlth_body))
            no_csrf_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/actions/agile",
                data=b'{"subaction":"board_view"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx_403:
                urlopen_safe(no_csrf_req, timeout=5, allow_private_lan=True)
            self.assertEqual(ctx_403.exception.code, 403)

            evil_host_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/status",
                headers={"Host": "evil.rebind.attacker"},
                method="GET",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx_host:
                urlopen_safe(evil_host_req, timeout=5, allow_private_lan=True)
            self.assertEqual(ctx_host.exception.code, 403)

            att_payload = json.dumps({
                "filename": "notes.md",
                "mime_type": "text/markdown",
                "data_base64": base64.b64encode(b"# Safe Attachment").decode("ascii"),
            }).encode("utf-8")
            att_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/attachments",
                data=att_payload,
                headers={"Content-Type": "application/json", "X-Companion-CSRF": csrf},
                method="POST",
            )
            with urlopen_safe(att_req, timeout=5, allow_private_lan=True) as resp:
                att_data = json.loads(resp.read().decode("utf-8"))
            att_id = att_data["attachment"]["id"]
            with urlopen_safe(f"http://127.0.0.1:{port}/api/attachments/{att_id}/raw", timeout=5, allow_private_lan=True) as raw_resp:
                self.assertIn("attachment", str(raw_resp.headers.get("Content-Disposition") or ""))
                self.assertIn("sandbox", str(raw_resp.headers.get("Content-Security-Policy") or ""))
        finally:
            sess.idle_watcher.stop()
            srv.shutdown()
            srv.server_close()
            th.join(timeout=3.0)

    def test_ssh_option_injection_and_command_and_wiki_path_guards(self) -> None:
        em = EndpointManager(store=self.store)
        bad_host = em.register_endpoint("bad1", "ssh", "-oProxyCommand=calc.exe", username="admin")
        self.assertFalse(bool(bad_host.get("ok")))
        bad_user = em.register_endpoint("bad2", "ssh", "192.168.1.25", username="-oLocalCommand=evil")
        self.assertFalse(bool(bad_user.get("ok")))

        self.assertIsNotNone(check_command_safety("rm -rf /var"))
        self.assertIsNotNone(check_command_safety("rm -rf ~/Documents"))
        self.assertIsNotNone(check_command_safety(r"Remove-Item -Recurse -Force C:\Users\victim"))

        wiki_target = settings.effective_data_dir / "wiki" / "02_knowledge" / "bypass.md"
        err = check_path_safety(wiki_target, write=True)
        self.assertIsNotNone(err)
        self.assertIn("wiki_manage", str(err))

    def test_loopback_proxy_bypass_model_gate_reentrancy_and_job_recipe_failure(self) -> None:
        ping_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PingHandler)
        ping_port = ping_srv.server_address[1]
        ping_th = threading.Thread(target=ping_srv.serve_forever, daemon=True)
        ping_th.start()
        try:
            with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1"}):
                with urlopen_safe(f"http://127.0.0.1:{ping_port}/ping", timeout=3, allow_private_lan=True) as resp:
                    self.assertEqual(resp.status, 200)
        finally:
            ping_srv.shutdown()
            ping_srv.server_close()
            ping_th.join(timeout=2.0)

        gate = ModelGate()
        entered_inner = False
        with gate.acquire_call(priority="foreground", serialize=True):
            with gate.acquire_call(priority="foreground", serialize=True):
                entered_inner = True
        self.assertTrue(entered_inner)

        nudges = peek_session_nudges(session_id="clean_tx_check", store=self.store)
        self.assertEqual(nudges, [])
        self.assertFalse(self.store.conn.in_transaction)

        jm = BackgroundJobManager(store=self.store)
        with patch.object(
            workflow_engine,
            "run_workflow_recipe",
            return_value={"status": "ABORTED", "valid": False, "summary": "gate failed"},
        ):
            dispatched = jm.dispatch_job(
                goal="check release",
                title="Failing Recipe Check",
                kind="recipe",
                recipe_name="release-check",
                run_in_background=False,
            )
        final_job = jm.get_job(str(dispatched["job_id"]))
        self.assertIsNotNone(final_job)
        self.assertEqual(final_job["status"], "failed")


if __name__ == "__main__":
    unittest.main()
