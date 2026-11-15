"""Unit & integration tests for Two-Lever Operating Architecture (`Background Autonomy` x `Self-Dev Mode`), Read-Only Core Lock, and Session Nudges."""

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from companion.agent import handle_slash_command
from companion.archive_and_approvals import (
    ApprovalQueue,
    consume_session_nudges,
    enqueue_session_nudge,
    peek_session_nudges,
)
from companion.config import (
    Settings,
    agent_tool_turn_context,
    self_dev_override_context,
    settings,
    workspace_context,
)
from companion.core_actions import execute_core_action
from companion.intake import classify_turn_intent
from companion.memory_store import MemoryStore, memory_store_context
from companion.routines import (
    BUILTIN_IDLE_ROUTINES,
    IdleAutonomyWatcher,
    describe_operating_quadrant,
    get_routine_manager,
)
from companion.safety import check_path_safety
from companion.skill_forge import stage_and_promote_skill
from companion.tool_schemas import phase_tool_context
from companion.tools import dispatch
from surfaces.api_extensions import register_service_reload_handler
from surfaces.web_server import create_web_server, dispatch_raw_http


class TestTwoLeverSelfDevAndAutonomy(unittest.TestCase):
    def test_four_operating_quadrants_and_env_locks(self) -> None:
        q1 = describe_operating_quadrant(False, False)
        q2 = describe_operating_quadrant(True, False)
        q3 = describe_operating_quadrant(False, True)
        q4 = describe_operating_quadrant(True, True)
        self.assertEqual(q1["quadrant"], "interactive_standard")
        self.assertEqual(q2["quadrant"], "autonomous_partner")
        self.assertEqual(q3["quadrant"], "interactive_self_dev")
        self.assertEqual(q4["quadrant"], "self_evolving_os")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(
                os.environ,
                {"COMPANION_SELF_DEV": "1", "COMPANION_SELF_DEV_LOCKED": "1", "COMPANION_IMMUTABLE_CORE": "0"},
                clear=False,
            ):
                cfg = Settings(env_path=root / "none.env")
                self.assertTrue(cfg.self_dev_default)
                self.assertTrue(cfg.self_dev_locked)
                self.assertTrue(cfg.is_self_dev_locked_by_env())

            orig_def, orig_lock, orig_imm = settings.self_dev_default, settings.self_dev_locked, settings.immutable_core
            try:
                settings.self_dev_default = True
                settings.self_dev_locked = False
                settings.immutable_core = False
                with contextlib.closing(MemoryStore(db_path=root / "seeded.db")) as st:
                    self.assertTrue(st.is_self_dev_mode_enabled())
                    settings.immutable_core = True
                    self.assertFalse(st.is_self_dev_mode_enabled())
                    with self.assertRaises(PermissionError):
                        st.set_self_dev_mode(True, actor="user")
                    with self_dev_override_context(True):
                        self.assertTrue(st.is_self_dev_mode_enabled())
            finally:
                settings.self_dev_default, settings.self_dev_locked, settings.immutable_core = orig_def, orig_lock, orig_imm

    def test_human_only_privilege_boundary_blocks_all_llm_escalation_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "priv.db"
            with contextlib.closing(MemoryStore(db_path=db_file)) as store:
                self.assertFalse(store.is_self_dev_mode_enabled())

                with self.assertRaises(PermissionError):
                    store.set_self_dev_mode(True, actor="companion")

                with agent_tool_turn_context():
                    with self.assertRaises(PermissionError):
                        store.set_self_dev_mode(True, actor="user")

                with memory_store_context(store), phase_tool_context("IMPLEMENT"):
                    r_prof = dispatch("memory_manage", {"action": "set_profile", "key": "self_dev_mode", "value": "on"})
                    self.assertIn("PRIVILEGE GUARD REJECTED", r_prof)

                    r_act = execute_core_action("autonomy", {"subaction": "set_self_dev", "enabled": True}, actor="companion", store=store)
                    self.assertFalse(r_act["ok"])
                    self.assertIn("PRIVILEGE GUARD REJECTED", r_act["error"])

                    r_sh = dispatch(
                        "run_shell",
                        {"command": f"sqlite3 \"{db_file}\" \"UPDATE user_profile SET value='on' WHERE key='self_dev_mode'\""},
                    )
                    self.assertIn("SAFETY GUARD REJECTED command", r_sh)

                    py_attack = (
                        f"import sqlite3\n"
                        f"c = sqlite3.connect(r'{db_file}')\n"
                        f"c.execute(\"UPDATE user_profile SET value='on' WHERE key='self_dev_mode'\")\n"
                        f"c.commit()\nc.close()\nprint('mutated')\n"
                    )
                    r_py = dispatch("run_python", {"code": py_attack})
                    self.assertIn("SAFETY GUARD REJECTED subprocess privilege escalation", r_py)
                    self.assertFalse(store.is_self_dev_mode_enabled())

    def test_mechanical_readonly_core_lock_preserves_data_and_external_projects(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp)
            fake_self = base / "companion_repo"
            ext_proj = base / "external_app"
            for sub in ("companion", "surfaces", "frontend", "specs", "skills", "data"):
                (fake_self / sub).mkdir(parents=True)
            ext_proj.mkdir(parents=True)

            readme = fake_self / "README.md"
            readme.write_text("# Stable Release\n", encoding="utf-8")
            core_py = fake_self / "companion" / "sample_mod.py"
            core_py.write_text("VALUE = 1\n", encoding="utf-8")

            orig_root, orig_data = settings.root, settings.data_dir
            with contextlib.closing(MemoryStore(db_path=fake_self / "data" / "companion.db")) as store:
                try:
                    settings.root = fake_self.resolve()
                    settings.data_dir = (fake_self / "data").resolve()
                    with workspace_context(fake_self, "companion-self"), memory_store_context(store), phase_tool_context("IMPLEMENT"):
                        self.assertFalse(settings.is_self_dev_enabled())

                        for rel in ("companion/sample_mod.py", "surfaces/ui.py", "frontend/App.tsx", "specs/PRODUCT.md", "README.md"):
                            err = check_path_safety(fake_self / rel, write=True)
                            self.assertIsNotNone(err)
                            self.assertIn("Self-Dev Mode` is OFF", str(err))

                        w_core = dispatch("write_file", {"path": str(core_py), "content": "VALUE = 2\n"})
                        self.assertIn("SAFETY GUARD REJECTED path", w_core)
                        self.assertEqual(core_py.read_text(encoding="utf-8"), "VALUE = 1\n")

                        py_mut = f"from pathlib import Path\nPath(r'{readme}').write_text('HACKED\\n', encoding='utf-8')\n"
                        r_sub = dispatch("run_python", {"code": py_mut})
                        self.assertIn("SAFETY GUARD REJECTED subprocess mutation", r_sub)
                        self.assertEqual(readme.read_text(encoding="utf-8"), "# Stable Release\n")

                        sub_data_file = fake_self / "data" / "sub_ok.txt"
                        r_sub_ok = dispatch("run_python", {"code": f"from pathlib import Path\nPath(r'{sub_data_file}').write_text('ok\\n', encoding='utf-8')\nprint('data_ok')\n"})
                        self.assertIn("data_ok", r_sub_ok)
                        self.assertTrue(sub_data_file.is_file())

                        sk_res = stage_and_promote_skill("demo-skill", "---\nname: demo-skill\n---\n", root=fake_self)
                        self.assertFalse(sk_res["promoted"])
                        self.assertIn("Self-Dev Mode", sk_res["error"])
                        sbx_res = execute_core_action("sandbox", {"subaction": "create", "label": "exp"}, actor="web", store=store)
                        self.assertFalse(sbx_res["ok"])
                        adr_res = dispatch("sdd_manage", {"action": "add_adr", "title": "ADR-099: Test", "decision": "x"})
                        self.assertIn("SAFETY GUARD REJECTED", adr_res)

                        data_note = fake_self / "data" / "notes" / "daily.md"
                        w_data = dispatch("write_file", {"path": str(data_note), "content": "Daily driver note\n"})
                        self.assertIn("wrote", w_data.lower())
                        wk_res = execute_core_action(
                            "wiki", {"subaction": "capture_inbox", "title": "Homelab Idea", "content": "Check UPS battery."},
                            actor="companion", store=store,
                        )
                        self.assertTrue(wk_res["ok"])
                        ag_res = execute_core_action(
                            "agile", {"subaction": "create_card", "title": "Order air filter", "owner": "human"},
                            actor="companion", store=store,
                        )
                        self.assertTrue(ag_res["ok"])

                        with workspace_context(ext_proj, "ext-app"):
                            w_ext = dispatch("write_file", {"path": str(ext_proj / "main.py"), "content": "print('ok')\n"})
                            self.assertIn("wrote", w_ext.lower())

                        cli_on = handle_slash_command("/selfdev on", [], store=store)
                        self.assertIn("ON", str(cli_on))
                        self.assertTrue(settings.is_self_dev_enabled())
                        self.assertIsNone(check_path_safety(readme, write=True))
                        cli_off = handle_slash_command("/selfdev off", [], store=store)
                        self.assertIn("OFF", str(cli_off))
                        self.assertFalse(settings.is_self_dev_enabled())
                finally:
                    settings.root, settings.data_dir = orig_root, orig_data

    def test_builtin_idle_routines_self_dev_gating_and_web_watcher(self) -> None:
        builtin_ids = {r["routine_id"] for r in BUILTIN_IDLE_ROUTINES}
        self.assertTrue({"wiki_gardener", "memory_curator", "trace_reviewer", "code_hygiene"}.issubset(builtin_ids))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.closing(MemoryStore(db_path=root / "data" / "web.db")) as store:
                rm = get_routine_manager(store=store)
                store.set_background_autonomy(True)
                store.set_self_dev_mode(False, actor="user")
                eligible = [r["routine_id"] for r in rm.select_eligible_idle_routines(idle_seconds=3600)]
                self.assertNotIn("code_hygiene", eligible)

                srv, sess = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=lambda *a, **k: {})
                try:
                    store.set_background_autonomy(False)
                    t0 = sess.idle_watcher.last_human_activity_mono
                    code, raw = dispatch_raw_http(srv, "GET", "/api/status", headers={"Host": "127.0.0.1"})
                    self.assertEqual(code, 200)
                    st_payload = json.loads(raw)
                    self.assertEqual(st_payload["self_dev_mode"], "off")
                    self.assertEqual(st_payload["operating_quadrant"]["quadrant"], "interactive_standard")
                    self.assertEqual(sess.idle_watcher.last_human_activity_mono, t0)

                    hdrs = {
                        "Host": "127.0.0.1",
                        "Origin": "http://127.0.0.1",
                        "Content-Type": "application/json",
                        "X-Companion-CSRF": sess.csrf_token,
                    }
                    c2, raw2 = dispatch_raw_http(
                        srv, "POST", "/api/actions/autonomy",
                        body=json.dumps({"subaction": "set_autonomy", "enabled": True}).encode("utf-8"),
                        headers=hdrs,
                    )
                    self.assertEqual(c2, 200)
                    self.assertEqual(json.loads(raw2)["quadrant"]["quadrant"], "autonomous_partner")
                    self.assertGreaterEqual(sess.idle_watcher.last_human_activity_mono, t0)

                    watcher = IdleAutonomyWatcher(is_busy_fn=lambda: True, store=store)
                    self.assertEqual(watcher.tick_once(idle_seconds=3600)["action"], "busy_skip")
                finally:
                    srv.session.idle_watcher.stop()
                    srv.server_close()

    def test_session_nudge_hygiene_self_healing_and_dismissal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(MemoryStore(db_path=Path(tmp) / "data" / "nudges.db")) as store:
            q = ApprovalQueue(store=store)
            allowed, apr = q.check_or_enqueue(
                category="wiki_edit",
                action_name="edit_user_note",
                target="02_knowledge/recipe.md",
                payload={"path": "02_knowledge/recipe.md"},
                summary="Curate recipe note",
            )
            self.assertFalse(allowed)
            aid = apr["approval_id"]
            enqueue_session_nudge(f"Job paused awaiting your approval (`{aid}`).", session_id="main", category="approval", ref_id=aid, store=store)
            enqueue_session_nudge("Background job bgj_123 completed.", session_id="main", category="job", ref_id="bgj_123", store=store)
            self.assertEqual(len(peek_session_nudges(store=store)), 2)

            q.resolve(aid, "approve", actor="user")
            remaining = peek_session_nudges(store=store)
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["category"], "job")

            enqueue_session_nudge(f"Stale nudge for already resolved `{aid}`", session_id="main", category="approval", ref_id=aid, store=store)
            healed = peek_session_nudges(store=store)
            self.assertEqual(len(healed), 1)
            self.assertEqual(healed[0]["category"], "job")

            enqueue_session_nudge("Second job finished.", session_id="main", category="job", ref_id="bgj_456", store=store)
            d_res = execute_core_action("approvals", {"subaction": "dismiss_nudges", "nudge_id": healed[0]["id"]}, actor="user", store=store)
            self.assertTrue(d_res["ok"])
            self.assertEqual(d_res["cleared"], 1)

            consumed = consume_session_nudges("web_ui", store=store)
            self.assertIn("Second job finished.", consumed)
            self.assertEqual(peek_session_nudges(store=store), [])

    def test_workflow_start_blocks_personal_goals_in_daily_driver_and_web_reload_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            srv = None
            with contextlib.closing(MemoryStore(db_path=root / "reload_guard.db")) as store:
                try:
                    dec = classify_turn_intent("Create a winter project milestone to clean the barn", store=store, root=root)
                    self.assertIn(dec.mode, ("GUIDED_CHANGE", "CHAT_DIRECT"))

                    with workspace_context(settings.root, "companion-self"), memory_store_context(store):
                        self.assertFalse(settings.is_self_dev_enabled())
                        wf_out = dispatch(
                            "workflow_manage",
                            {"action": "start", "workflow_type": "self_evolution", "goal": "Clean the barn winter project"},
                        )
                        self.assertIn("SAFETY GUARD REJECTED", wf_out)
                        self.assertIsNone(store.get_active_workflow())

                    srv, sess = create_web_server(host="127.0.0.1", port=0, store=store)
                    code_no_csrf, _ = dispatch_raw_http(srv, "POST", "/api/reload", body=b'{"dry_run":true}', headers={"Origin": "http://127.0.0.1"})
                    self.assertEqual(code_no_csrf, 403)

                    code_dry, raw_dry = dispatch_raw_http(
                        srv, "POST", "/api/reload", body=b'{"dry_run":true}',
                        headers={"Origin": "http://127.0.0.1", "X-Companion-CSRF": sess.csrf_token},
                    )
                    self.assertEqual(code_dry, 200)
                    parsed_dry = json.loads(raw_dry)
                    self.assertTrue(parsed_dry["ok"] and parsed_dry["dry_run"])

                    triggered = []
                    register_service_reload_handler(lambda: triggered.append(True))
                    try:
                        with patch("surfaces.api_extensions.threading.Timer") as mock_timer:
                            code_live, raw_live = dispatch_raw_http(
                                srv, "POST", "/api/reload", body=b"{}",
                                headers={"Origin": "http://127.0.0.1", "X-Companion-CSRF": sess.csrf_token},
                            )
                            self.assertEqual(code_live, 200)
                            self.assertTrue(json.loads(raw_live)["reloading"])
                            mock_timer.assert_called_once()
                    finally:
                        register_service_reload_handler(None)
                finally:
                    if srv is not None:
                        srv.session.idle_watcher.stop()
                        srv.server_close()


if __name__ == "__main__":
    unittest.main()
