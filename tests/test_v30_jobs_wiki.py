"""
Verification Suite for Companion 3.0 Harvest Stages 1–3 (`tests/test_v30_jobs_wiki.py`):
- Stage 1: Shared `CORE_ACTIONS`, Trust Modes, Fingerprint `ApprovalQueue`, Hot/Cold Forever Chat, Session Nudges & Saved Prompts
- Stage 2: Forever-Chat Background Job Dispatcher, Crash Recovery, Tool Pairing & `<=200`-Token Wiki Inbox Overflow
- Stage 3: Co-Owned 4-Folder Knowledge Wiki 2.0, Dual-Author Guard, `content_hash` Locks, 2D Graph & `KnowledgePort` Bridge
"""

import json
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion.agent import handle_slash_command, run_agent_turn
from companion.approvals import archive_aged_conversations, consume_session_nudges, enqueue_session_nudge, get_approval_queue, get_trust_mode, peek_session_nudges, render_prompt_template, save_prompt_template, search_forever_history, session_id_context, set_trust_mode
from companion.jobs import MAX_ENVELOPE_CHARS, BackgroundJobManager, resolve_paired_worker_tools
from companion.config import execution_origin_context
from companion.core_actions import execute_core_action, list_core_actions
from companion.context import build_epistemic_grounding
from companion.memory_store import MemoryStore, memory_store_context
from companion.tools import dispatch
from companion.wiki_engine import WikiEngine
from surfaces.web_server import create_web_server, dispatch_raw_http


class TestStage1CoreActionsArchiveAndApprovals(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = MemoryStore(db_path=self.base / "conversations.db")

    def tearDown(self) -> None:
        self.store.conn.close()
        self._tmp.cleanup()

    def test_trust_modes_and_approval_fingerprint_governance(self) -> None:
        self.assertEqual(get_trust_mode(store=self.store), "balanced")
        esc = set_trust_mode("autonomous", actor="companion", store=self.store)
        self.assertFalse(esc["ok"])
        self.assertIn("TRUST GUARD REJECTED", esc["error"])

        ok_set = set_trust_mode("strict", actor="user", store=self.store)
        self.assertTrue(ok_set["ok"])
        self.assertEqual(get_trust_mode(store=self.store), "strict")

        aq = get_approval_queue(store=self.store)
        allowed, req = aq.check_or_enqueue(
            category="wiki_edit", action_name="graduate_note",
            target="01_inbox/a.md -> 02_knowledge/ops/a.md",
            summary="Graduate note A", payload={"from": "a", "to": "b"},
        )
        self.assertFalse(allowed)
        apr_id = req["approval_id"]

        self_res = aq.resolve(apr_id, "approve", actor="companion")
        self.assertFalse(self_res["ok"])
        self.assertIn("APPROVAL GUARD REJECTED", self_res["error"])

        user_res = aq.resolve(apr_id, "approve", actor="user", note="Looks great")
        self.assertTrue(user_res["ok"])

        allowed_same, _ = aq.check_or_enqueue(
            category="wiki_edit", action_name="graduate_note",
            target="01_inbox/a.md -> 02_knowledge/ops/a.md",
            summary="Graduate note A", payload={"from": "a", "to": "b"},
        )
        self.assertTrue(allowed_same)

        allowed_diff, _ = aq.check_or_enqueue(
            category="wiki_edit", action_name="graduate_note",
            target="01_inbox/a.md -> 02_knowledge/ops/DIFFERENT.md",
            summary="Changed target", payload={"from": "a", "to": "different"},
        )
        self.assertFalse(allowed_diff)

    def test_hot_cold_forever_chat_archive_and_unified_recall(self) -> None:
        old_ts = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d %H:%M:%S")
        with self.store._lock:
            for i in range(6):
                cur = self.store.conn.execute(
                    "INSERT INTO conversations (role, content, timestamp, session_tag) VALUES (?, ?, ?, 'main')",
                    ("user", f"Legacy GPU cluster migration milestone #{i} with NVLink topology", old_ts),
                )
                self.store.conn.execute("INSERT INTO conversations_fts(rowid, content) VALUES (?, ?)", (cur.lastrowid, f"Legacy GPU cluster migration milestone #{i} with NVLink topology"))
            self.store.conn.commit()
        for j in range(4):
            self.store.log_message("user", f"Active recent workstation homelab note #{j}")

        arch = archive_aged_conversations(store=self.store, older_than_days=90, keep_min_hot=4)
        self.assertTrue(arch["ok"])
        self.assertEqual(arch["archived_count"], 6)
        self.assertEqual(arch["hot_count"], 4)
        self.assertEqual(arch["cold_count"], 6)

        hits = search_forever_history("NVLink topology", store=self.store, limit=5)
        self.assertTrue(any(h["tier"] == "Cold Archive" and "NVLink" in h["content"] for h in hits))

        with memory_store_context(self.store):
            tool_out = dispatch("memory_manage", {"action": "search_history", "query": "NVLink topology"})
        self.assertIn("[Cold Archive #", tool_out)

    def test_session_nudges_saved_prompts_and_core_actions_parity(self) -> None:
        self.assertGreaterEqual(len(list_core_actions()), 12)
        enqueue_session_nudge("Job bgj_1 finished cleanly.", category="job", session_id="main", ref_id="bgj_1", store=self.store)
        enqueue_session_nudge("Other session event", category="job", session_id="other_sess", store=self.store)
        self.assertEqual(len(peek_session_nudges(session_id="main", store=self.store)), 1)

        captured_wire_user: list[str] = []

        def fake_chat(msgs, tools=None, tool_choice="auto"):
            from companion.llm_wire import inject_openai_turn_attachments
            wire_msgs = inject_openai_turn_attachments(msgs)
            captured_wire_user.append(str(wire_msgs[-1]["content"]))
            return {"choices": [{"message": {"role": "assistant", "content": "All caught up!"}}]}

        msgs = [{"role": "user", "content": "Hello!"}]
        reply = run_agent_turn(msgs, max_iters=2, chat_fn=fake_chat, store=self.store, verbose=False, auto_promote=False)
        self.assertEqual(reply, "All caught up!")
        self.assertIn("Job bgj_1 finished cleanly.", captured_wire_user[0])
        self.assertEqual(consume_session_nudges(session_id="main", store=self.store), "")
        self.assertEqual(len(peek_session_nudges(session_id="other_sess", store=self.store)), 1)

        sp = save_prompt_template("Server Audit", "Inspect {{host}} on port {{port}}.", category="ops", store=self.store)
        self.assertTrue(sp["ok"])
        rendered = render_prompt_template("server-audit", {"host": "192.168.1.100"}, store=self.store)
        self.assertEqual(rendered["rendered"], "Inspect 192.168.1.100 on port {{port}}.")
        self.assertEqual(rendered["unfilled_variables"], ["port"])

        slash_out = handle_slash_command("/prompts list", msgs, store=self.store, interactive=False)
        self.assertIn("server-audit", str(slash_out))

        srv, sess = create_web_server(host="127.0.0.1", port=0, store=self.store, chat_fn=fake_chat)
        try:
            code_g, body_g = dispatch_raw_http(srv, "GET", "/api/actions")
            self.assertEqual(code_g, 200)
            self.assertIn("prompts", body_g)
            code_p, body_p = dispatch_raw_http(
                srv, "POST", "/api/actions/prompts",
                body=json.dumps({"subaction": "list"}).encode("utf-8"),
                headers={"Origin": "http://127.0.0.1", "X-Companion-CSRF": sess.csrf_token},
            )
            self.assertEqual(code_p, 200)
            self.assertIn("server-audit", body_p)
        finally:
            srv.server_close()


class TestStage2BackgroundJobsAndStage3WikiEngine(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = MemoryStore(db_path=self.base / "conversations.db")

    def tearDown(self) -> None:
        self.store.conn.close()
        self._tmp.cleanup()

    def test_paired_tools_crash_recovery_and_overflow_envelope(self) -> None:
        paired = resolve_paired_worker_tools(["write_file", "job_manage"])
        self.assertIn("read_file", paired)
        self.assertIn("search", paired)
        self.assertNotIn("job_manage", paired)

        jm_init = BackgroundJobManager(store=self.store)
        _ = jm_init.store
        with self.store._lock:
            self.store.conn.execute(
                "INSERT INTO background_jobs (job_id, title, goal, status) VALUES ('bgj_crashed', 'Crashed Run', 'Audit', 'running')"
            )
            self.store.conn.commit()
        recovered = jm_init.recover_interrupted_jobs(self.store)
        self.assertEqual(recovered, 1)
        self.assertEqual(jm_init.get_job("bgj_crashed")["status"], "interrupted")  # type: ignore[index]

        long_report = "\n\n".join([f"Section {i}: Detailed GPU Server thermal & VRAM telemetry findings ({'x' * 180})." for i in range(10)])

        def worker_chat(msgs, tools=None, tool_choice="auto"):
            return {"choices": [{"message": {"role": "assistant", "content": long_report}}]}

        jm = BackgroundJobManager(store=self.store, chat_fn=worker_chat)
        res = jm.dispatch_job(goal="Compile full GPU telemetry report", title="Node Audit", run_in_background=False)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "done")
        self.assertLessEqual(len(res["envelope_summary"]), MAX_ENVELOPE_CHARS)
        self.assertTrue(res["overflow_wiki_path"].startswith("01_inbox/"))

        wk = WikiEngine(store=self.store)
        saved_note = wk.read_note(res["overflow_wiki_path"])
        self.assertTrue(saved_note["ok"])
        self.assertIn("Section 9", saved_note["body"])

    def test_job_pause_on_approval_and_auto_resume(self) -> None:
        calls = {"n": 0}

        def gated_tool(name, args):
            calls["n"] += 1
            if calls["n"] == 1:
                _, apr = get_approval_queue(self.store).check_or_enqueue("endpoint_exec", "ssh_exec", "gpu-node", "Restart container")
                return f"error: REMOTE EXEC APPROVAL REQUIRED (`{apr['approval_id']}`): awaits approval."
            return "Container restarted cleanly."

        step = {"turn": 0}

        def step_chat(msgs, tools=None, tool_choice="auto"):
            step["turn"] += 1
            if step["turn"] in (1, 2):
                return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "function": {"name": "endpoint_manage", "arguments": '{"action":"ssh_exec"}'}}]}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "Approved restart verified."}}]}

        jm = BackgroundJobManager(store=self.store, chat_fn=step_chat, tool_fn=gated_tool)
        paused = jm.dispatch_job(goal="Restart service", title="Service Restart", allowed_tools=["endpoint_manage"], run_in_background=False)
        self.assertEqual(paused["status"], "waiting_approval")
        apr_id = paused["approval_id"]
        self.assertTrue(apr_id.startswith("apr_"))

        execute_core_action("approvals", {"subaction": "approve", "approval_id": apr_id, "resume_job": False}, actor="user", store=self.store)
        resumed = jm.resume_job(paused["job_id"], run_in_background=False)
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["status"], "done")

    def test_wiki_warehouse_dual_author_locks_graph_and_grounding_bridge(self) -> None:
        wk = WikiEngine(store=self.store)
        blocked_direct = wk.save_or_update_note("02_knowledge/homelab/gpu-node.md", "# GPU Node\nDirect bypass attempt", actor="companion")
        self.assertFalse(blocked_direct["ok"])
        self.assertIn("ONE-DOOR INBOX GUARD", blocked_direct["error"])

        too_deep = wk.save_or_update_note("02_knowledge/a/b/c/deep.md", "# Too deep", actor="user")
        self.assertFalse(too_deep["ok"])
        self.assertIn("TAXONOMY GUARD", too_deep["error"])

        user_doc = wk.save_or_update_note(
            "02_knowledge/homelab/workstation.md",
            "# Workstation Host\nPrimary Windows workstation paired with [[gpu-node]]. #homelab",
            title="Primary Workstation", actor="user",
        )
        self.assertTrue(user_doc["ok"])
        orig_hash = user_doc["content_hash"]

        comp_edit = wk.save_or_update_note("02_knowledge/homelab/workstation.md", "# Overwrite attempt", actor="companion")
        self.assertFalse(comp_edit["ok"])
        self.assertTrue(comp_edit.get("awaiting_approval"))

        stale_conflict = wk.save_or_update_note("02_knowledge/homelab/workstation.md", "# Updated", actor="user", expected_hash="deadbeef00000000")
        self.assertFalse(stale_conflict["ok"])
        self.assertTrue(stale_conflict.get("conflict"))

        valid_upd = wk.save_or_update_note(
            "02_knowledge/homelab/workstation.md",
            "# Workstation Host\nUpdated LAN workstation paired with [[gpu-node]]. #homelab",
            actor="user", expected_hash=orig_hash,
        )
        self.assertTrue(valid_upd["ok"])
        self.assertTrue(str(valid_upd["archived_prior"]).startswith("04_archive/"))

        cap = wk.capture_inbox("GPU Server Notes", "Compute cluster node at 192.168.1.100 linked to [[workstation]].", tags=["gpu"], slug="gpu-node", actor="companion")
        self.assertTrue(cap["ok"])
        grad_wait = wk.graduate_note("01_inbox/gpu-node.md", domain="homelab", actor="companion")
        self.assertFalse(grad_wait["ok"])
        grad_ok = wk.graduate_note("01_inbox/gpu-node.md", domain="homelab", actor="user")
        self.assertTrue(grad_ok["ok"])

        disk_file = wk.root / "02_knowledge/homelab/gpu-node.md"
        bytes_before = disk_file.read_bytes()
        read_res = wk.read_note("gpu-node")
        self.assertTrue(read_res["ok"])
        self.assertIn("02_knowledge/homelab/workstation.md", read_res["backlinks"])
        self.assertEqual(disk_file.read_bytes(), bytes_before)

        graph = wk.export_mindmap_graph()
        self.assertEqual(graph["stats"]["note_count"], 2)
        self.assertGreaterEqual(graph["stats"]["edge_count"], 2)

        rebuilt = wk.rebuild_index()
        self.assertEqual(rebuilt["indexed_notes"], 4)  # 2 knowledge notes + 1 edit archive + 1 graduate archive
        g_pack = build_epistemic_grounding("Compute cluster node 192.168.1.100", store=self.store)
        self.assertTrue(any("wiki:02_knowledge/homelab/gpu-node.md" == f.key for f in g_pack.known_facts))

        # Regression: nested folder/path prefix filter in `wiki_manage(action="list", path=...)`
        with memory_store_context(self.store), unittest.mock.patch("companion.core_actions.get_wiki_engine", return_value=wk):
            nested_out = dispatch("wiki_manage", {"action": "list", "path": "02_knowledge/homelab"})
        self.assertIn("gpu-node.md", nested_out)

    def test_dogfood_regressions_session_context_and_approval_auto_exec(self) -> None:
        reg_wiki_dir = self.base / "wiki_reg"
        wk = WikiEngine(wiki_dir=reg_wiki_dir, store=self.store)
        wk.save_or_update_note("02_knowledge/ops/runbook.md", "# Runbook v1", title="Runbook", actor="user")
        wk.capture_inbox("Pending Grad", "Ready for knowledge warehouse.", slug="pending-grad", actor="companion")

        mock_jm = BackgroundJobManager(store=self.store, chat_fn=lambda m, t=None, tc="auto": {"choices": [{"message": {"role": "assistant", "content": "Quick check complete."}}]})
        with memory_store_context(self.store), unittest.mock.patch("companion.core_actions.get_wiki_engine", return_value=wk), unittest.mock.patch("companion.core_actions.get_job_manager", return_value=mock_jm):
            with session_id_context("forever_custom"):
                j_out = dispatch("job_manage", {
                    "action": "dispatch", "goal": "Verify custom session nudge",
                    "title": "Custom Session Job", "run_in_background": False,
                })
            self.assertIn("Quick check complete.", j_out)
            custom_nudges = peek_session_nudges(session_id="forever_custom", store=self.store)
            self.assertEqual(len(custom_nudges), 1)

            pause_edit = dispatch("wiki_manage", {"action": "save", "path": "02_knowledge/ops/runbook.md", "content": "# Runbook v2"})
            pause_grad = dispatch("wiki_manage", {"action": "graduate", "path": "01_inbox/pending-grad.md", "domain": "ops"})
            self.assertTrue(pause_edit.startswith("AWAITING_USER_APPROVAL"))
            self.assertTrue(pause_grad.startswith("AWAITING_USER_APPROVAL"))

            for req in get_approval_queue(self.store).list_requests(status="waiting"):
                slash_res = handle_slash_command(f"/approvals approve {req['approval_id']}", [], store=self.store, interactive=False)
                self.assertIn("-> Executed:", slash_res)

        self.assertEqual(wk.list_notes(folder="01_inbox"), [])
        self.assertTrue((reg_wiki_dir / "02_knowledge/ops/pending-grad.md").exists())
        self.assertIn("Runbook v2", wk.read_note("02_knowledge/ops/runbook.md")["body"])

    def test_dom1_r3_tool_approval_exec_error_surfacing_and_atomic_dedup(self) -> None:
        aq = get_approval_queue(self.store)
        with memory_store_context(self.store):
            r1 = dispatch("run_shell", {"command": "systemctl restart gdm"})
            r2 = dispatch("run_shell", {"command": "systemctl restart gdm"})
            self.assertIn("RISK GATE REJECTED (R3)", r1)
            waiting = aq.list_requests(status="waiting")
            self.assertEqual(len(waiting), 1)
            self.assertIn(waiting[0]["approval_id"], r2)
            with unittest.mock.patch.dict("companion.tools.TOOL_IMPLS", {"run_shell": lambda a: f"exit=0\nrestarted:{a['command']}"}):
                res_ok = execute_core_action("approvals", {"subaction": "approve", "approval_id": waiting[0]["approval_id"]}, actor="user", store=self.store)
            self.assertTrue(res_ok["ok"])
            self.assertIn("restarted:systemctl restart gdm", res_ok["applied_summary"])

            _, bad_apr = aq.check_or_enqueue(
                category="r3_tool", action_name="run_shell", target="bad-cmd",
                summary="Failing R3 command", payload={"tool": "missing_tool_xyz", "command": "bad"},
                requester="companion", confirmed=False, always_require_in_autonomous=True,
            )
            res_err = execute_core_action("approvals", {"subaction": "approve", "approval_id": bad_apr["approval_id"]}, actor="user", store=self.store)
            self.assertFalse(res_err["ok"])
            self.assertIn("Unknown R3 tool", res_err["execution_error"])

    def test_dom3_foreground_one_step_wiki_save_body_preservation_and_user_guard(self) -> None:
        fg_wiki_dir = self.base / "wiki_fg"
        wk = WikiEngine(wiki_dir=fg_wiki_dir, store=self.store)
        wk.save_or_update_note("02_knowledge/homelab/user-sacred.md", "# Sacred User Note", title="User Sacred", actor="user")
        with memory_store_context(self.store), unittest.mock.patch("companion.core_actions.get_wiki_engine", return_value=wk):
            with execution_origin_context("background_job"):
                bg_blk = dispatch("wiki_manage", {"action": "curate", "domain": "homelab", "title": "BG Blocked", "body": "Blocked in background."})
                self.assertIn("ONE-DOOR INBOX GUARD", bg_blk)

            with execution_origin_context("foreground_turn"):
                fg_save = dispatch("wiki_manage", {
                    "action": "curate", "domain": "homelab", "title": "Ubuntu RDP Black Screen",
                    "summary": "Restart GDM over SSH when Ubuntu RDP shows a black screen.",
                    "body": "Run `sudo systemctl restart gdm` over SSH to reset the GNOME display manager.",
                })
                self.assertIn("02_knowledge/homelab/ubuntu-rdp-black-screen.md", fg_save)
                first_read = wk.read_note("02_knowledge/homelab/ubuntu-rdp-black-screen.md")
                self.assertTrue(first_read["ok"])
                self.assertIn("sudo systemctl restart gdm", first_read["body"])

                meta_only = dispatch("wiki_manage", {
                    "action": "save", "path": "02_knowledge/homelab/ubuntu-rdp-black-screen.md",
                    "tags": "rdp,gdm,ubuntu",
                })
                self.assertIn("02_knowledge/homelab/ubuntu-rdp-black-screen.md", meta_only)
                second_read = wk.read_note("02_knowledge/homelab/ubuntu-rdp-black-screen.md")
                self.assertIn("sudo systemctl restart gdm", second_read["body"])
                self.assertIn("gdm", second_read["tags"])
                revs = list((fg_wiki_dir / "04_archive/revisions").glob("*.md"))
                self.assertGreaterEqual(len(revs), 1)

                sacred_pause = dispatch("wiki_manage", {"action": "save", "path": "02_knowledge/homelab/user-sacred.md", "body": "# Overwrite attempt"})
                self.assertTrue(sacred_pause.startswith("AWAITING_USER_APPROVAL"))


if __name__ == "__main__":
    unittest.main()
