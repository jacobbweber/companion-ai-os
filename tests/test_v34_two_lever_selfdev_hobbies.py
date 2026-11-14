import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from companion.agent import handle_slash_command
from companion.config import (
    Settings,
    agent_tool_turn_context,
    self_dev_override_context,
    settings,
    workspace_context,
)
from companion.core_actions import execute_core_action
from companion.hobbies import (
    HOBBY_MAP,
    HobbyEngine,
    WebIdleHobbyWatcher,
    describe_operating_quadrant,
)
from companion.memory_store import MemoryStore, memory_store_context
from companion.safety import CheckpointManager, check_path_safety
from companion.skill_forge import stage_and_promote_skill
from companion.tool_schemas import phase_tool_context
from companion.tools import dispatch
from surfaces.web_server import create_web_server, dispatch_raw_http


class TestTwoLeverSelfDevAndHobbies(unittest.TestCase):
    def test_four_operating_quadrants_and_env_locks(self) -> None:
        q1 = describe_operating_quadrant(False, False)
        q2 = describe_operating_quadrant(True, False)
        q3 = describe_operating_quadrant(False, True)
        q4 = describe_operating_quadrant(True, True)
        self.assertEqual((q1["code"], q1["core_read_only"], q1["eligible_tracks"]), ("Q1", True, []))
        self.assertEqual((q2["code"], q2["core_read_only"], q2["eligible_tracks"]), ("Q2", True, ["personal"]))
        self.assertEqual((q3["code"], q3["core_read_only"], q3["eligible_tracks"]), ("Q3", False, []))
        self.assertEqual((q4["code"], q4["core_read_only"], q4["eligible_tracks"]), ("Q4", False, ["personal", "self_dev"]))

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

            # Verify fresh DB seeding from settings.self_dev_default and immutable_core override
            orig_def, orig_lock, orig_imm = settings.self_dev_default, settings.self_dev_locked, settings.immutable_core
            try:
                settings.self_dev_default = True
                settings.self_dev_locked = False
                settings.immutable_core = False
                st = MemoryStore(db_path=root / "seeded.db")
                try:
                    self.assertTrue(st.is_self_dev_mode_enabled())
                    settings.immutable_core = True
                    self.assertFalse(st.is_self_dev_mode_enabled())
                    with self.assertRaises(PermissionError):
                        st.set_self_dev_mode(True, actor="user")
                    with self_dev_override_context(True):
                        self.assertTrue(st.is_self_dev_mode_enabled())
                finally:
                    st.close()
            finally:
                settings.self_dev_default, settings.self_dev_locked, settings.immutable_core = orig_def, orig_lock, orig_imm

    def test_human_only_privilege_boundary_blocks_all_llm_escalation_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            db_file = tmp_dir / "priv.db"
            store = MemoryStore(db_path=db_file)
            try:
                self.assertFalse(store.is_self_dev_mode_enabled())

                # 1. Non-human actor rejected
                with self.assertRaises(PermissionError):
                    store.set_self_dev_mode(True, actor="companion")

                # 2. Even actor='user' inside agent_tool_turn_context() is rejected
                with agent_tool_turn_context():
                    with self.assertRaises(PermissionError):
                        store.set_self_dev_mode(True, actor="user")

                # 3. memory_manage(set_profile) and CoreActions actor='companion' rejected
                with memory_store_context(store), phase_tool_context("IMPLEMENT"):
                    r_prof = dispatch("memory_manage", {"action": "set_profile", "key": "self_dev_mode", "value": "on"})
                    self.assertIn("PRIVILEGE GUARD REJECTED", r_prof)

                    r_act = execute_core_action("hobbies", {"subaction": "set_self_dev", "enabled": True}, actor="companion", store=store)
                    self.assertFalse(r_act["ok"])
                    self.assertIn("PRIVILEGE GUARD REJECTED", r_act["error"])

                    # 4. Shell SQL escalation blocked pre-exec
                    r_sh = dispatch(
                        "run_shell",
                        {"command": f"sqlite3 \"{db_file}\" \"UPDATE user_profile SET value='on' WHERE key='self_dev_mode'\""},
                    )
                    self.assertIn("SAFETY GUARD REJECTED command", r_sh)

                    # 5. Subprocess Python direct sqlite3 write reverted immediately by _guard_self_dev_db!
                    py_attack = (
                        f"import sqlite3\n"
                        f"c = sqlite3.connect(r'{db_file}')\n"
                        f"c.execute(\"UPDATE user_profile SET value='on' WHERE key='self_dev_mode'\")\n"
                        f"c.commit()\nc.close()\nprint('mutated')\n"
                    )
                    r_py = dispatch("run_python", {"code": py_attack})
                    self.assertIn("SAFETY GUARD REJECTED subprocess privilege escalation", r_py)
                    self.assertFalse(store.is_self_dev_mode_enabled())
            finally:
                store.close()

    def test_mechanical_readonly_core_lock_preserves_data_and_external_projects(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp)
            fake_self = base / "companion_repo"
            ext_proj = base / "external_app"
            (fake_self / "companion").mkdir(parents=True)
            (fake_self / "surfaces").mkdir(parents=True)
            (fake_self / "frontend").mkdir(parents=True)
            (fake_self / "specs").mkdir(parents=True)
            (fake_self / "skills").mkdir(parents=True)
            (fake_self / "data").mkdir(parents=True)
            ext_proj.mkdir(parents=True)

            readme = fake_self / "README.md"
            readme.write_text("# Stable Release\n", encoding="utf-8")
            core_py = fake_self / "companion" / "sample_mod.py"
            core_py.write_text("VALUE = 1\n", encoding="utf-8")

            store = MemoryStore(db_path=fake_self / "data" / "companion.db")
            orig_root, orig_data = settings.root, settings.data_dir
            try:
                settings.root = fake_self.resolve()
                settings.data_dir = (fake_self / "data").resolve()
                with workspace_context(fake_self, "companion-self"), memory_store_context(store), phase_tool_context("IMPLEMENT"):
                    self.assertFalse(settings.is_self_dev_enabled())

                    # Core tracked paths are mechanically locked READ-ONLY
                    for rel in ("companion/sample_mod.py", "surfaces/ui.py", "frontend/App.tsx", "specs/PRODUCT.md", "README.md"):
                        err = check_path_safety(fake_self / rel, write=True)
                        self.assertIsNotNone(err, f"Expected `{rel}` to be read-only when Self-Dev OFF")
                        self.assertIn("Self-Dev Mode` is OFF", str(err))

                    w_core = dispatch("write_file", {"path": str(core_py), "content": "VALUE = 2\n"})
                    self.assertIn("SAFETY GUARD REJECTED path", w_core)
                    self.assertEqual(core_py.read_text(encoding="utf-8"), "VALUE = 1\n")

                    # Subprocess file mutation inside fake_self is reverted by enforce_readonly_tree
                    py_mut = f"from pathlib import Path\nPath(r'{readme}').write_text('HACKED\\n', encoding='utf-8')\n"
                    r_sub = dispatch("run_python", {"code": py_mut})
                    self.assertIn("SAFETY GUARD REJECTED subprocess mutation", r_sub)
                    self.assertEqual(readme.read_text(encoding="utf-8"), "# Stable Release\n")

                    # Subprocess file write inside fake_self/data/ IS permitted when Self-Dev OFF
                    sub_data_file = fake_self / "data" / "sub_ok.txt"
                    r_sub_ok = dispatch("run_python", {"code": f"from pathlib import Path\nPath(r'{sub_data_file}').write_text('ok\\n', encoding='utf-8')\nprint('data_ok')\n"})
                    self.assertIn("data_ok", r_sub_ok)
                    self.assertTrue(sub_data_file.is_file())

                    # Skill promotion, sandbox clone, and ADR mutation are blocked while Self-Dev OFF
                    sk_res = stage_and_promote_skill("demo-skill", "---\nname: demo-skill\n---\n", root=fake_self)
                    self.assertFalse(sk_res["promoted"])
                    self.assertIn("Self-Dev Mode", sk_res["error"])
                    sbx_res = execute_core_action("sandbox", {"subaction": "create", "label": "exp"}, actor="web", store=store)
                    self.assertFalse(sbx_res["ok"])
                    adr_res = dispatch("sdd_manage", {"action": "add_adr", "title": "ADR-099: Test", "decision": "x"})
                    self.assertIn("SAFETY GUARD REJECTED", adr_res)

                    # Meanwhile, data/ (Wiki, Agile Boards, notes) and external /project remain 100% writable!
                    data_note = fake_self / "data" / "notes" / "daily.md"
                    w_data = dispatch("write_file", {"path": str(data_note), "content": "Daily driver note\n"})
                    self.assertIn("wrote", w_data.lower())
                    wk_res = execute_core_action(
                        "wiki", {"subaction": "capture_inbox", "title": "Homelab Idea", "content": "Check UPS battery."},
                        actor="companion", store=store,
                    )
                    self.assertTrue(wk_res["ok"])
                    ag_res = execute_core_action(
                        "agile", {"subaction": "create_card", "board_id": "life-home", "title": "Order air filter"},
                        actor="companion", store=store,
                    )
                    self.assertTrue(ag_res["ok"])

                    with workspace_context(ext_proj, "ext-app"):
                        w_ext = dispatch("write_file", {"path": str(ext_proj / "main.py"), "content": "print('ok')\n"})
                        self.assertIn("wrote", w_ext.lower())

                    # Human CLI `/selfdev on` unlocks core; `/selfdev off` re-locks it
                    cli_on = handle_slash_command("/selfdev on", [])
                    self.assertIn("ON", str(cli_on))
                    self.assertTrue(settings.is_self_dev_enabled())
                    self.assertIsNone(check_path_safety(readme, write=True))
                    cli_off = handle_slash_command("/selfdev off", [])
                    self.assertIn("OFF", str(cli_off))
                    self.assertFalse(settings.is_self_dev_enabled())
            finally:
                settings.root, settings.data_dir = orig_root, orig_data
                store.close()

    def test_two_track_hobby_rotation_gating_and_life_study_organizer(self) -> None:
        self.assertEqual(
            {k for k, v in HOBBY_MAP.items() if v.track == "personal"},
            {"wiki_gardener", "memory_curator", "trace_reviewer", "life_study_organizer"},
        )
        self.assertEqual(
            {k for k, v in HOBBY_MAP.items() if v.track == "self_dev"},
            {"code_simplifier", "backlog_builder", "code_gardener"},
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(db_path=root / "data" / "hobbies.db")
            cp_mgr = CheckpointManager(root=root, backup_dir=root / "data" / "backups")
            eng = HobbyEngine(store=store, cp_manager=cp_mgr, root=root)
            try:
                # While Self-Dev OFF, rotation strictly cycles the 4 personal hobbies
                seen = []
                for _ in range(4):
                    nxt = eng.select_next_hobby()
                    self.assertEqual(nxt.track, "personal")
                    seen.append(nxt.name)
                    store.log_hobby_run(nxt.name, "completed", "ok", checkpoint_id="cp_1", doctor_passed=True)
                self.assertEqual(seen, ["wiki_gardener", "memory_curator", "trace_reviewer", "life_study_organizer"])

                # Direct request to run a self_dev hobby is cleanly refused when Self-Dev OFF
                refused = eng.run_hobby_cycle(hobby_name="backlog_builder", runner_fn=lambda _m, _l: "nope")
                self.assertEqual(refused["status"], "refused_self_dev_off")
                self.assertFalse(refused["ok"])

                # Running life_study_organizer succeeds when Self-Dev OFF without writing specs/DECISIONS.md
                with patch("companion.hobbies.run_system_doctor", return_value={"healthy": True, "summary": "7/7 PASS", "checks": []}):
                    ok_run = eng.run_hobby_cycle(
                        hobby_name="life_study_organizer",
                        runner_fn=lambda _m, _l: "2 FSRS cards due; Sprint capacity healthy.",
                    )
                self.assertTrue(ok_run["ok"])
                self.assertEqual(ok_run["track"], "personal")
                self.assertFalse((root / "specs" / "DECISIONS.md").exists())
            finally:
                store.close()

    def test_web_api_hobbies_actions_and_idle_hobby_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(db_path=root / "data" / "web.db")
            srv, sess = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=lambda *a, **k: {})
            try:
                t0 = sess.idle_watcher.last_human_activity_mono
                code, raw = dispatch_raw_http(srv, "GET", "/api/status", headers={"Host": "127.0.0.1"})
                self.assertEqual(code, 200)
                st_payload = json.loads(raw)
                self.assertEqual(st_payload["self_dev_mode"], "off")
                self.assertEqual(st_payload["operating_quadrant"]["code"], "Q1")
                # Background status polling must NOT reset human inactivity timer!
                self.assertEqual(sess.idle_watcher.last_human_activity_mono, t0)

                hdrs = {
                    "Host": "127.0.0.1",
                    "Origin": "http://127.0.0.1",
                    "Content-Type": "application/json",
                    "X-Companion-CSRF": sess.csrf_token,
                }
                c2, raw2 = dispatch_raw_http(
                    srv, "POST", "/api/actions/hobbies",
                    body=json.dumps({"subaction": "set_hobby", "enabled": True}).encode("utf-8"),
                    headers=hdrs,
                )
                self.assertEqual(c2, 200)
                self.assertEqual(json.loads(raw2)["quadrant"]["code"], "Q2")
                self.assertGreaterEqual(sess.idle_watcher.last_human_activity_mono, t0)

                # Verify WebIdleHobbyWatcher runs personal hobby when idle and skips when busy
                calls = []
                fake_eng = HobbyEngine(store=store, root=root)
                fake_eng.run_hobby_cycle = lambda abort_check=None: (calls.append("ran") or {"hobby": "wiki_gardener", "status": "completed"})  # type: ignore[method-assign]
                busy_flag = {"busy": True}
                watcher = WebIdleHobbyWatcher(is_busy_fn=lambda: busy_flag["busy"], engine=fake_eng)
                watcher.last_human_activity_mono = 10.0
                self.assertIsNone(watcher.tick_once(now_mono=500.0, idle_sec_override=60.0, cooldown_sec_override=30.0))
                busy_flag["busy"] = False
                res = watcher.tick_once(now_mono=500.0, idle_sec_override=60.0, cooldown_sec_override=30.0)
                self.assertIsNotNone(res)
                self.assertEqual(calls, ["ran"])
                # Cooldown blocks immediate back-to-back run
                self.assertIsNone(watcher.tick_once(now_mono=510.0, idle_sec_override=60.0, cooldown_sec_override=30.0))
            finally:
                srv.server_close()
                store.close()

    def test_session_nudge_hygiene_self_healing_and_dismissal(self) -> None:
        from companion.archive_and_approvals import (
            ApprovalQueue,
            consume_session_nudges,
            enqueue_session_nudge,
            peek_session_nudges,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(db_path=Path(tmp) / "data" / "nudges.db")
            try:
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
                enqueue_session_nudge(
                    f"Job paused awaiting your approval (`{aid}`).",
                    session_id="main",
                    category="approval",
                    ref_id=aid,
                    store=store,
                )
                enqueue_session_nudge(
                    "Background job bgj_123 completed.",
                    session_id="main",
                    category="job",
                    ref_id="bgj_123",
                    store=store,
                )
                self.assertEqual(len(peek_session_nudges(store=store)), 2)

                # Resolving the approval immediately marks its session nudge seen
                q.resolve(aid, "approve", actor="user")
                remaining = peek_session_nudges(store=store)
                self.assertEqual(len(remaining), 1)
                self.assertEqual(remaining[0]["category"], "job")

                # Simulate a pre-existing stale approval nudge from an older DB row and confirm peek self-heals it
                enqueue_session_nudge(
                    f"Stale nudge for already resolved `{aid}`",
                    session_id="main",
                    category="approval",
                    ref_id=aid,
                    store=store,
                )
                healed = peek_session_nudges(store=store)
                self.assertEqual(len(healed), 1)
                self.assertEqual(healed[0]["category"], "job")

                # Dismiss via CORE_ACTIONS['approvals'] and verify consume_session_nudges('web_ui') also spans 'main'
                enqueue_session_nudge(
                    "Second job finished.",
                    session_id="main",
                    category="job",
                    ref_id="bgj_456",
                    store=store,
                )
                d_res = execute_core_action(
                    "approvals",
                    {"subaction": "dismiss_nudges", "nudge_id": healed[0]["id"]},
                    actor="user",
                    store=store,
                )
                self.assertTrue(d_res["ok"])
                self.assertEqual(d_res["cleared"], 1)

                consumed = consume_session_nudges("web_ui", store=store)
                self.assertIn("Second job finished.", consumed)
                self.assertEqual(peek_session_nudges(store=store), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
