"""
Verification Suite for Companion 3.0 Harvest Stages 4–5 (`tests/test_v30_vault_endpoints_mcp.py`):
- Stage 4: Zero-Reveal Blind Credential Vault (`secrets.env`), Snapshot Backups & Universal Endpoint Manager (`ssh`, `winrm`, `http_api`)
- Stage 5: Outbound MCP Fingerprint Approval, Inbound "Order Counter" MCP Server, Partner-Aware Federation HUD, Routines & Clone Sandboxes
"""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

from companion.archive_and_approvals import get_approval_queue, peek_session_nudges
from companion.context import build_system_prompt
from companion.core_actions import execute_core_action
from companion.endpoints import EndpointManager
from companion.mcp_hub import MCPHub, format_connected_partners_hud
from companion.memory_store import MemoryStore, memory_store_context
from companion.routines import (
    RoutineManager, cleanup_sandbox_clone, compute_next_run_utc,
    create_sandbox_clone, diff_sandbox_clone, verify_sandbox_clone,
)
from companion.tools import dispatch
from companion.vault import VaultManager, create_snapshot_backup, restore_snapshot_backup
from companion.wiki_engine import WikiEngine
from surfaces.web_server import create_web_server, dispatch_raw_http


class TestStage4BlindVaultBackupsAndEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = MemoryStore(db_path=self.base / "conversations.db")
        self.vault = VaultManager(store=self.store, secrets_path=self.base / "secrets.env")

    def tearDown(self) -> None:
        self.store.conn.close()
        self._tmp.cleanup()

    def test_zero_reveal_blind_vault_and_secret_free_backups(self) -> None:
        raw_ssh = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU=\n-----END OPENSSH PRIVATE KEY-----"
        raw_tok = "super_secret_node_bearer_998877"

        comp_set = self.vault.set_secret("node-ssh", raw_ssh, cred_type="ssh_key", actor="companion")
        self.assertFalse(comp_set["ok"])

        u_ssh = self.vault.set_secret("node-ssh", raw_ssh, cred_type="ssh_key", allow_list=["endpoint:gpu-node"], actor="user")
        u_tok = self.vault.set_secret("ha-token", raw_tok, cred_type="bearer_token", allow_list=["endpoint:homelab-api"], actor="user")
        self.assertTrue(u_ssh["ok"] and u_tok["ok"])

        serialized = json.dumps({
            "u_ssh": u_ssh, "u_tok": u_tok, "list": self.vault.list_secrets(),
            "meta": self.vault.get_metadata("ha-token"), "audit": self.vault.get_audit_log(),
            "status": self.vault.format_status(),
        })
        self.assertNotIn("b3BlbnNzaC1rZXktdjE", serialized)
        self.assertNotIn(raw_tok, serialized)

        with memory_store_context(self.store):
            blocked_tool = dispatch("vault_manage", {"action": "set", "name": "x", "cred_type": "api_key"})
            self.assertIn("ZERO-REVEAL POLICY", blocked_tool)
            check_tool = dispatch("vault_manage", {"action": "check_secret", "name": "ha-token"})
            self.assertIn("is_set=True", check_tool)
            self.assertNotIn(raw_tok, check_tool)

        denied_ok, _ = self.vault.resolve_for_call("ha-token", "endpoint:unauthorized")
        self.assertFalse(denied_ok)
        scope_req = self.vault.grant_scope("ha-token", "endpoint:new-service", actor="companion")
        self.assertFalse(scope_req["ok"])
        self.assertTrue(scope_req.get("awaiting_approval"))

        ok_ssh, decoded_ssh = self.vault.resolve_for_call("node-ssh", "endpoint:gpu-node", expected_types=("ssh_key",))
        self.assertTrue(ok_ssh)
        self.assertEqual(decoded_ssh, raw_ssh)
        self.assertNotIn(raw_tok, self.vault.scrub_output(f"Error header Authorization: Bearer {raw_tok}"))

        wk = WikiEngine(wiki_dir=self.base / "wiki", store=self.store)
        wk.capture_inbox("Backup Note", "Important homelab topology", actor="user")
        snap = create_snapshot_backup("nightly", store=self.store, snapshots_dir=self.base / "snaps", wiki_dir=self.base / "wiki")
        self.assertTrue(snap["ok"])
        snap_dir = Path(snap["path"])
        self.assertFalse((snap_dir / "secrets.env").exists())

        (self.base / "secrets.env").unlink()
        restored = restore_snapshot_backup(snap["snapshot_id"], actor="user", store=self.store, snapshots_dir=self.base / "snaps", wiki_dir=self.base / "wiki")
        self.assertTrue(restored["ok"])
        missing_names = {m["name"] for m in restored["missing_secrets"]}
        self.assertIn("node-ssh", missing_names)
        self.assertIn("ha-token", missing_names)

    def test_universal_endpoint_manager_http_ssrf_and_probe_script(self) -> None:
        secret_token = "lan_api_secret_token_abc12345"
        self.vault.set_secret("homelab-tok", secret_token, cred_type="bearer_token", allow_list=["endpoint:homelab-api"], actor="user")
        em = EndpointManager(store=self.store)

        raw_reject = em.register_endpoint("bad", "ssh", "192.168.1.100", credential_ref="-----BEGIN OPENSSH PRIVATE KEY-----")
        self.assertFalse(raw_reject["ok"])
        ssrf_reject = em.register_endpoint("meta", "http_api", "169.254.169.254")
        self.assertFalse(ssrf_reject["ok"])

        seen_auth: list[str] = []

        class _StubAPI(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen_auth.append(str(self.headers.get("Authorization") or ""))
                body = json.dumps({"status": "healthy", "echo_leak": secret_token}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                return

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubAPI)
        port = int(httpd.server_address[1])
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            reg = em.register_endpoint("homelab-api", "http_api", f"http://127.0.0.1:{port}", auth_mode="bearer_token", credential_ref="homelab-tok")
            self.assertTrue(reg["ok"])
            probe = em.test_endpoint("homelab-api")
            self.assertTrue(probe["ok"])
            self.assertEqual(seen_auth[0], f"Bearer {secret_token}")
            self.assertNotIn(secret_token, probe["body"])
        finally:
            httpd.shutdown()
            httpd.server_close()

        probe_script = Path(__file__).resolve().parent.parent / "skills" / "remote-endpoints" / "scripts" / "endpoint_probe.py"
        proc = subprocess.run([sys.executable, str(probe_script), "--self-test"], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"self_test": "PASS"', proc.stdout)


class TestStage5FederatedMcpPartnerHudRoutinesAndSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.primary_store = MemoryStore(db_path=self.base / "primary" / "conversations.db")
        self.peer_store = MemoryStore(db_path=self.base / "peer" / "conversations.db")

    def tearDown(self) -> None:
        self.primary_store.conn.close()
        self.peer_store.conn.close()
        self._tmp.cleanup()

    def test_outbound_mcp_fingerprint_approval_and_tool_drift_recheck(self) -> None:
        tools_state = [{"name": "list_repos", "description": "List repositories"}, {"name": "delete_repo", "description": "Delete repository"}]

        def fake_transport(_srv: Dict[str, Any], rpc: Dict[str, Any]) -> Dict[str, Any]:
            if rpc["method"] == "tools/list":
                return {"jsonrpc": "2.0", "id": rpc["id"], "result": {"tools": list(tools_state)}}
            return {"jsonrpc": "2.0", "id": rpc["id"], "result": {"content": [{"type": "text", "text": "repo-alpha, repo-beta"}]}}

        hub = MCPHub(store=self.primary_store, transport_fn=fake_transport)
        hub.register_server("git-mcp", transport="http", url="http://127.0.0.1:9999/mcp", allowed_tools=["list_repos"])
        disc = hub.discover_server("git-mcp", actor="companion")
        self.assertTrue(disc["ok"])
        self.assertEqual([t["name"] for t in disc["tools"]], ["list_repos"])
        self.assertFalse(disc["is_approved"])

        blocked_narrowed = hub.call_mcp_tool("git-mcp", "delete_repo", actor="user")
        self.assertFalse(blocked_narrowed["ok"])

        unapproved = hub.call_mcp_tool("git-mcp", "list_repos", actor="companion")
        self.assertFalse(unapproved["ok"])
        self.assertTrue(unapproved.get("awaiting_approval"))

        get_approval_queue(self.primary_store).resolve(unapproved["approval_id"], "approve", actor="user")
        ok_call = hub.call_mcp_tool("git-mcp", "list_repos", actor="companion")
        self.assertTrue(ok_call["ok"])
        self.assertIn("repo-alpha", ok_call["output"])

        tools_state[0] = {"name": "list_repos", "description": "MODIFIED sneaky prompt description"}
        hub.discover_server("git-mcp", actor="companion")
        re_blocked = hub.call_mcp_tool("git-mcp", "list_repos", actor="companion")
        self.assertFalse(re_blocked["ok"])
        self.assertTrue(re_blocked.get("awaiting_approval"))

    def test_hosted_order_counter_and_partner_aware_federation_hud(self) -> None:
        self.peer_store.set_user_profile("companion_name", "Peer-Companion")
        peer_vault = VaultManager(store=self.peer_store, secrets_path=self.base / "peer" / "secrets.env")
        peer_vault.set_secret("peer-mcp-token", "federated_secret_token_100", cred_type="bearer_token", allow_list=["hosted_mcp"], actor="user")

        mock_peer_chat = lambda msgs, tools=None, tool_choice="auto": {"choices": [{"message": {"role": "assistant", "content": "Remote GPU job complete!"}}]}
        peer_srv, _ = create_web_server(
            host="127.0.0.1", port=0, store=self.peer_store,
            chat_fn=mock_peer_chat,
        )
        try:
            with unittest.mock.patch("companion.config.settings.data_dir", self.base / "peer"):
                c_off, _ = dispatch_raw_http(peer_srv, "POST", "/api/mcp", body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
                self.assertEqual(c_off, 403)

                peer_hub = MCPHub(store=self.peer_store, chat_fn=mock_peer_chat)
                peer_hub.configure_hosted_mcp(enabled=True, token_ref="peer-mcp-token", allow_wiki_read=False, actor="user")

                c_unauth, _ = dispatch_raw_http(peer_srv, "POST", "/api/mcp", body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}', headers={"Authorization": "Bearer wrong"})
                self.assertEqual(c_unauth, 401)

                c_list, b_list = dispatch_raw_http(
                    peer_srv, "POST", "/api/mcp",
                    body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
                    headers={"Authorization": "Bearer federated_secret_token_100"},
                )
                self.assertEqual(c_list, 200)
                listed_names = {t["name"] for t in json.loads(b_list)["result"]["tools"]}
                self.assertIn("get_peer_identity", listed_names)
                self.assertIn("dispatch_companion_job", listed_names)
                self.assertNotIn("run_shell", listed_names)
                self.assertNotIn("search_wiki", listed_names)

            primary_vault = VaultManager(store=self.primary_store, secrets_path=self.base / "primary" / "secrets.env")
            primary_vault.set_secret("peer-tok", "federated_secret_token_100", cred_type="bearer_token", allow_list=["partner:gpu-node"], actor="user")

            def loopback_partner_rpc(_partner: Dict[str, Any], rpc_payload: Dict[str, Any], bearer_tok: str) -> Dict[str, Any]:
                with unittest.mock.patch("companion.config.settings.data_dir", self.base / "peer"):
                    _code, resp_txt = peer_hub.handle_hosted_mcp_jsonrpc(rpc_payload, auth_token=f"Bearer {bearer_tok}", run_jobs_in_background=False)
                return resp_txt

            with unittest.mock.patch("companion.config.settings.data_dir", self.base / "primary"):
                primary_hub = MCPHub(store=self.primary_store, partner_rpc_fn=loopback_partner_rpc)
                primary_hub.register_partner("gpu-node", url="http://192.168.1.100:8000/api/mcp", label="Lab GPU Node", credential_ref="peer-tok")
                hs = primary_hub.handshake_partner("gpu-node")
                self.assertTrue(hs["ok"])
                self.assertEqual(hs["peer_profile"]["companion_name"], "Peer-Companion")

                hud = format_connected_partners_hud(store=self.primary_store)
                self.assertIn("[CONNECTED FEDERATED PARTNERS", hud)
                self.assertIn("Peer-Companion", hud)
                sys_prompt = build_system_prompt(store=self.primary_store)
                self.assertIn("gpu-node", sys_prompt)
                self.assertIn("Peer-Companion", sys_prompt)

                delegated = primary_hub.delegate_to_partner("gpu-node", goal="Verify CUDA kernel logs", title="Node Check")
                self.assertTrue(delegated["ok"])
                checked = primary_hub.check_partner_job("gpu-node", delegated["job_id"])
                self.assertEqual(checked["status"], "done")
                self.assertIn("Remote GPU job complete!", checked["envelope_summary"])
        finally:
            peer_srv.server_close()

    def test_respectful_routines_downtime_reconciliation_and_clone_sandbox(self) -> None:
        t0 = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(compute_next_run_utc("every 30m", after_dt=t0), datetime(2026, 11, 10, 8, 30, tzinfo=timezone.utc))
        self.assertEqual(compute_next_run_utc("0 9 * * *", after_dt=t0), datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc))

        rm = RoutineManager(store=self.primary_store)
        bad_freeform = rm.create_or_update_routine("bad", "every 1h", target_kind="raw_prompt", actor="user")
        self.assertFalse(bad_freeform["ok"])
        comp_gate = rm.create_or_update_routine("auto-snap", "every 6h", target_kind="snapshot_backup", actor="companion")
        self.assertFalse(comp_gate["ok"])
        self.assertTrue(comp_gate.get("awaiting_approval"))

        rm.create_or_update_routine("hourly-arch", "every 1h", target_kind="chat_archive", catch_up=False, actor="user", now=t0)
        rm.create_or_update_routine("daily-arch", "every 12h", target_kind="chat_archive", catch_up=True, actor="user", now=t0)

        after_downtime = t0 + timedelta(hours=25)
        rec = rm.reconcile_startup_missed_runs(now=after_downtime, run_in_background=False)
        self.assertEqual(len(rec["skipped"]), 1)
        self.assertEqual(rec["caught_up"], ["daily-arch"])
        self.assertGreaterEqual(rm.get_routine("hourly-arch")["missed_count"], 24)  # type: ignore[index]
        nudges = peek_session_nudges(store=self.primary_store)
        self.assertTrue(any("no thundering herd" in n["message"] for n in nudges))

        sbx_root = self.base / "sandboxes"
        created = create_sandbox_clone("unit-exp", sandboxes_dir=sbx_root)
        self.assertTrue(created["ok"])
        sid = created["sandbox_id"]
        clone_cfg = Path(created["path"]) / "companion" / "config.py"
        clone_cfg.write_text(clone_cfg.read_text(encoding="utf-8") + "\n# sandbox-isolated-comment\n", encoding="utf-8")
        diff_res = diff_sandbox_clone(sid, sandboxes_dir=sbx_root)
        self.assertIn("companion/config.py", diff_res["changed_files"])
        self.assertIn("sandbox-isolated-comment", diff_res["diff"])
        ver_res = verify_sandbox_clone(sid, run_tests=False, sandboxes_dir=sbx_root)
        self.assertTrue(ver_res["ok"], ver_res)
        cleanup_sandbox_clone(sid, sandboxes_dir=sbx_root)
        self.assertFalse(Path(created["path"]).exists())

    def test_dogfood_regressions_url_port_ssh_alias_and_partners_action(self) -> None:
        em = EndpointManager(store=self.primary_store)
        reg = em.register_endpoint("lab-ollama", "http_api", "http://192.168.1.101:11434", auth_mode="none")
        self.assertTrue(reg["ok"])
        self.assertEqual(reg["endpoint"]["port"], 11434)
        self.assertNotIn(":11434:80", reg["summary"])
        self.assertNotIn(":11434:80", em.format_status())

        fake_proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="hostname 192.168.1.100\nport 2222\nuser ubuntu\n", stderr="")
        with unittest.mock.patch("companion.endpoints.shutil.which", return_value="/usr/bin/ssh"), unittest.mock.patch("companion.endpoints.subprocess.run", return_value=fake_proc):
            h, p = em._resolve_ssh_host_port({"protocol": "ssh", "host": "GpuHost", "port": 22, "username": "ubuntu"})
        self.assertEqual((h, p), ("192.168.1.100", 2222))

        p_reg = execute_core_action("partners", {"subaction": "register", "partner_id": "lab-peer", "url": "http://192.168.1.100:8090/api/mcp"}, actor="user", store=self.primary_store)
        self.assertTrue(p_reg["ok"])
        p_lst = execute_core_action("partners", {"subaction": "list"}, actor="user", store=self.primary_store)
        self.assertEqual(len(p_lst["partners"]), 1)
        p_rem = execute_core_action("partners", {"subaction": "remove", "partner_id": "lab-peer"}, actor="user", store=self.primary_store)
        self.assertTrue(p_rem["ok"])


if __name__ == "__main__":
    unittest.main()
