"""Unit & integration tests for Permanent Installed-App Immutability, Orthogonal Repo SDLC (`git_manage`, `dev_preview`), `safe-deploy` Rollback, Background Autonomy, and Session Nudges."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest.mock import patch

from companion.agent import handle_slash_command
from companion.approvals import ApprovalQueue, consume_session_nudges, enqueue_session_nudge, peek_session_nudges, set_trust_mode
from companion.config import (
    Settings,
    agent_tool_turn_context,
    settings,
    workspace_context,
)
from companion.core_actions import execute_core_action
from companion.dag_runtime import execute_dag_by_name, resume_dag_run
from companion.agent import classify_turn_intent
from companion.memory_store import MemoryStore, memory_store_context
from companion.project_graph import get_project_graph
from companion.routines import (
    BUILTIN_IDLE_ROUTINES,
    IdleAutonomyWatcher,
    describe_operating_quadrant,
    get_routine_manager,
)
from companion.tools import check_path_safety
from companion.sdlc_tools import (
    PreviewProcessManager,
    discover_repo_sdlc_context,
    preview_turn_cleanup_scope,
    tool_dev_preview,
    tool_git_manage,
)
from companion.skill_forge import stage_and_promote_skill
from companion.tools import phase_tool_context
from companion.tools import dispatch
from companion.vault import VaultManager
from surfaces.api_extensions import register_service_reload_handler
from surfaces.web_server import create_web_server, dispatch_raw_http


class TestImmutableRuntimeAndOrthogonalSDLC(unittest.TestCase):
    def test_operating_postures_and_immutable_app_config(self) -> None:
        q_off = describe_operating_quadrant(False)
        q_on = describe_operating_quadrant(True)
        self.assertEqual(q_off["quadrant"], "interactive_standard")
        self.assertEqual(q_on["quadrant"], "autonomous_partner")
        self.assertTrue(q_off["immutable_app"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"COMPANION_IMMUTABLE_APP": "1"}, clear=False):
                cfg = Settings(env_path=root / "none.env")
                self.assertTrue(cfg.immutable_app)

    def test_human_only_privilege_boundary_blocks_llm_trust_escalation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "priv.db"
            with contextlib.closing(MemoryStore(db_path=db_file)) as store:
                with agent_tool_turn_context():
                    with self.assertRaises(PermissionError):
                        store.set_user_profile("trust_mode", "autonomous")

                with memory_store_context(store), phase_tool_context("IMPLEMENT"):
                    r_prof = dispatch("memory_manage", {"action": "set_profile", "key": "trust_mode", "value": "autonomous"})
                    self.assertIn("PRIVILEGE GUARD REJECTED", r_prof)

                    r_sh = dispatch(
                        "run_shell",
                        {"command": f"sqlite3 \"{db_file}\" \"UPDATE user_profile SET value='autonomous' WHERE key='trust_mode'\""},
                    )
                    self.assertIn("SAFETY GUARD REJECTED command", r_sh)

                    # Even an obfuscated subprocess SQLite mutation is rolled back & rejected by `_guard_priv_db`
                    py_attack = (
                        f"import sqlite3\n"
                        f"k = 'trust_' + 'mode'\n"
                        f"c = sqlite3.connect(r'{db_file}')\n"
                        f"c.execute('INSERT OR REPLACE INTO user_profile (key, value) VALUES (?, ?)', (k, 'autonomous'))\n"
                        f"c.commit()\nc.close()\nprint('mutated')\n"
                    )
                    r_py = dispatch("run_python", {"code": py_attack})
                    self.assertIn("SAFETY GUARD REJECTED subprocess privilege escalation", r_py)
                    self.assertEqual(store.get_user_profile().get("trust_mode", "balanced"), "balanced")

    def test_permanent_immutable_app_lock_preserves_scratch_data_and_external_repos(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp)
            fake_self = base / "companion_app"
            ext_proj = base / "external_repo"
            for sub in ("companion", "surfaces", "frontend", "specs", "skills", "data"):
                (fake_self / sub).mkdir(parents=True)
            ext_proj.mkdir(parents=True)

            readme = fake_self / "README.md"
            readme.write_text("# Immutable Installed Runtime\n", encoding="utf-8")
            core_py = fake_self / "companion" / "sample_mod.py"
            core_py.write_text("VALUE = 1\n", encoding="utf-8")

            orig_root, orig_data = settings.root, settings.data_dir
            with contextlib.closing(MemoryStore(db_path=fake_self / "data" / "companion.db")) as store:
                try:
                    settings.root = fake_self.resolve()
                    settings.data_dir = (fake_self / "data").resolve()
                    with workspace_context(fake_self, "companion-self"), memory_store_context(store), phase_tool_context("IMPLEMENT"):
                        for rel in ("companion/sample_mod.py", "surfaces/ui.py", "frontend/App.tsx", "specs/PRODUCT.md", "README.md"):
                            err = check_path_safety(fake_self / rel, write=True)
                            self.assertIsNotNone(err)
                            self.assertIn("immutable", str(err).lower())

                        w_core = dispatch("write_file", {"path": str(core_py), "content": "VALUE = 2\n"})
                        self.assertIn("SAFETY GUARD REJECTED path", w_core)
                        self.assertEqual(core_py.read_text(encoding="utf-8"), "VALUE = 1\n")

                        py_mut = f"from pathlib import Path\nPath(r'{readme}').write_text('HACKED\\n', encoding='utf-8')\n"
                        r_sub = dispatch("run_python", {"code": py_mut})
                        self.assertIn("SAFETY GUARD REJECTED subprocess mutation", r_sub)
                        self.assertEqual(readme.read_text(encoding="utf-8"), "# Immutable Installed Runtime\n")

                        # Relative writes in OS Mode automatically route into `data/scratch/`
                        w_scratch = dispatch("write_file", {"path": "quick_calc.py", "content": "print(42)\n"})
                        self.assertIn("wrote", w_scratch.lower())
                        self.assertTrue((fake_self / "data" / "scratch" / "quick_calc.py").is_file())

                        valid_skill = (
                            "---\nname: demo-skill\ndescription: Valid user skill description.\n"
                            "required_tools: [read_file]\n---\n## When to Use This Skill\nUse.\n## Step-by-Step Procedure\n1. Run.\n"
                        )
                        sk_core = stage_and_promote_skill("demo-skill", valid_skill, root=fake_self, scope="core")
                        self.assertFalse(sk_core["promoted"])
                        sk_user = stage_and_promote_skill("demo-skill", valid_skill, root=fake_self, scope="user")
                        self.assertTrue(sk_user["promoted"])

                        self.assertIn("Unknown slash command", str(handle_slash_command("/selfdev on", [], store=store)))

                        with workspace_context(ext_proj, "ext-repo"):
                            w_ext = dispatch("write_file", {"path": str(ext_proj / "main.py"), "content": "print('ok')\n"})
                            self.assertIn("wrote", w_ext.lower())
                finally:
                    settings.root, settings.data_dir = orig_root, orig_data

    def test_git_manage_branches_worktrees_secret_shield_and_delivery_policy(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp)
            repo = base / "work_repo"
            wt_dir = base / "wt_parallel"
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@companion.local"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.name", "Companion Test"], cwd=str(repo), check=True)
            subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/demo.git"], cwd=str(repo), check=True)

            (repo / ".agents" / "rules").mkdir(parents=True)
            (repo / ".agents" / "rules" / "style.md").write_text("# Strict Typed Python\n", encoding="utf-8")
            (repo / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

            with contextlib.closing(MemoryStore(db_path=base / "sdlc.db")) as store:
                pg = get_project_graph(store=store)
                proj = pg.create_project(title="Demo Repo", workspace_path=str(repo), delivery_policy="local_only")
                ctx_xml = discover_repo_sdlc_context(repo, project=proj)
                self.assertIn("style.md", ctx_xml)
                self.assertIn("delivery_policy=`local_only`", ctx_xml)

                with workspace_context(repo, proj["project_id"]), memory_store_context(store):
                    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
                    c_main = tool_git_manage({"action": "commit", "files": ["app.py"], "message": "bad main commit"})
                    self.assertIn("blocked", c_main.lower())

                    b_out = tool_git_manage({"action": "branch", "branch": "feat/widget"})
                    self.assertIn("feat/widget", b_out)
                    (repo / ".env").write_text("SECRET=123\n", encoding="utf-8")
                    c_sec = tool_git_manage({"action": "commit", "files": [".env"], "message": "leak"})
                    self.assertIn("SAFETY GUARD", c_sec)
                    (repo / ".env").unlink()

                    c_ok = tool_git_manage({"action": "commit", "files": ["app.py"], "message": "feat: update x"})
                    self.assertIn("feat: update x", c_ok)

                    wt_add = tool_git_manage({"action": "worktree_add", "branch": "feat/parallel", "worktree_path": str(wt_dir)})
                    self.assertTrue(wt_dir.is_dir(), wt_add)
                    wt_ls = tool_git_manage({"action": "worktree_list"})
                    self.assertIn("wt_parallel", wt_ls)
                    wt_rm = tool_git_manage({"action": "worktree_remove", "worktree_path": str(wt_dir)})
                    self.assertFalse(wt_dir.exists(), wt_rm)

                    p_blocked = tool_git_manage({"action": "push", "remote": "origin", "branch": "feat/widget"})
                    self.assertIn("local_only", p_blocked)

                    set_trust_mode("balanced", store=store)
                    pg.update_project(proj["project_id"], delivery_policy="ask_me")
                    p_ask = tool_git_manage({"action": "push", "remote": "origin", "branch": "feat/widget"})
                    self.assertIn("approval", p_ask.lower())

                    vm = VaultManager(store=store, secrets_path=base / "secrets.env")
                    tok = "ghp_UnitBlindGitToken_Secret_555"
                    vm.set_secret("git-pat", tok, cred_type="bearer_token", allow_list=[f"git:{proj['project_id']}"], actor="user")
                    pg.update_project(proj["project_id"], delivery_policy="remote_branch_pr", git_credential_ref="git-pat")

                    captured_askpass: List[str] = []

                    def _fake_git_push(_root: Path, git_args: List[str], env: Optional[Dict[str, str]] = None, timeout: int = 45) -> Tuple[int, str]:
                        if git_args[:1] == ["rev-parse"]:
                            return 0, "feat/widget"
                        assert env is not None and "GIT_ASKPASS" in env
                        out_pw = subprocess.run([env["GIT_ASKPASS"], "Password for https://github.com"], env=env, capture_output=True, text=True, check=True)
                        captured_askpass.append(out_pw.stdout.strip())
                        return 0, vm.scrub_output(f"To https://github.com/acme/demo.git (auth={tok})\n * [new branch] feat/widget -> feat/widget")

                    with patch.dict(os.environ, {"COMPANION_SECRETS_FILE": str(base / "secrets.env")}, clear=False), patch("companion.sdlc_tools._run_git_cmd", side_effect=_fake_git_push):
                        p_ok = tool_git_manage({"action": "push", "remote": "origin", "branch": "feat/widget"})
                    self.assertEqual(captured_askpass, [tok])
                    self.assertNotIn(tok, p_ok)
                    self.assertIn("feat/widget", p_ok)

    def test_dev_preview_lifecycle_probe_and_turn_exit_teardown(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo = Path(tmp) / "web_app"
            repo.mkdir()
            (repo / "srv.py").write_text(
                "import http.server, sys\n"
                "port = int(sys.argv[1])\n"
                "class H(http.server.BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        self.send_response(200)\n"
                "        self.send_header('Content-Type','text/html')\n"
                "        self.end_headers()\n"
                "        self.wfile.write(b'<h1>Preview Alive</h1>')\n"
                "    def log_message(self, *a): pass\n"
                "http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()\n",
                encoding="utf-8",
            )
            mgr = PreviewProcessManager()
            with patch("companion.sdlc_tools.preview_manager", mgr), workspace_context(repo, "web-app"):
                with preview_turn_cleanup_scope():
                    cmd = f'"{sys.executable}" srv.py {{port}}'
                    started = json.loads(tool_dev_preview({"action": "start", "command": cmd, "ready_path": "/"}))
                    self.assertTrue(started["ok"], started)
                    probe_txt = tool_dev_preview({"action": "http_probe", "preview_id": started["preview_id"], "path": "/"})
                    self.assertIn("200", probe_txt)
                    self.assertIn("Preview Alive", probe_txt)
                st = json.loads(tool_dev_preview({"action": "list"}))
                self.assertEqual(st["previews"], [])

    def test_safe_deploy_ops_builder_script_and_rollback(self) -> None:
        script = Path(__file__).resolve().parent.parent / ".agents" / "skills" / "devops-ops" / "scripts" / "safe_deploy_ops.py"
        proc = subprocess.run([sys.executable, "-B", str(script), "--self-test"], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
        self.assertIn("pass", proc.stdout.lower())

    def test_builtin_idle_routines_web_watcher_nudges_and_reload_endpoint(self) -> None:
        builtin_ids = {r["routine_id"] for r in BUILTIN_IDLE_ROUTINES}
        self.assertEqual(builtin_ids, {"wiki_gardener", "memory_curator", "trace_reviewer"})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.closing(MemoryStore(db_path=root / "data" / "web.db")) as store:
                q = ApprovalQueue(store=store)
                allowed, apr = q.check_or_enqueue(
                    category="wiki_edit", action_name="edit_user_note", target="02_knowledge/recipe.md",
                    payload={"path": "02_knowledge/recipe.md"}, summary="Curate recipe note",
                )
                self.assertFalse(allowed)
                aid = apr["approval_id"]
                enqueue_session_nudge(f"Paused (`{aid}`).", session_id="main", category="approval", ref_id=aid, store=store)
                q.resolve(aid, "approve", actor="user")
                self.assertEqual(peek_session_nudges(store=store), [])
                enqueue_session_nudge("Job done.", session_id="main", category="job", ref_id="bgj_1", store=store)
                self.assertIn("Job done.", consume_session_nudges("web_ui", store=store))

                dec = classify_turn_intent("Create a winter project milestone to clean the barn", store=store, root=root)
                self.assertIn(dec.mode, ("GUIDED_CHANGE", "CHAT_DIRECT"))
                with workspace_context(settings.root, "companion-self"), memory_store_context(store):
                    wf_out = dispatch("workflow_manage", {"action": "start", "workflow_type": "engineering_feature", "goal": "Clean the barn"})
                    self.assertIn("SAFETY GUARD REJECTED", wf_out)

                srv, sess = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=lambda *a, **k: {})
                try:
                    code, raw = dispatch_raw_http(srv, "GET", "/api/status", headers={"Host": "127.0.0.1"})
                    self.assertEqual(code, 200)
                    st_payload = json.loads(raw)
                    self.assertTrue(st_payload["immutable_app"])
                    self.assertEqual(st_payload["operating_quadrant"]["quadrant"], "interactive_standard")

                    watcher = IdleAutonomyWatcher(is_busy_fn=lambda: True, store=store)
                    self.assertEqual(watcher.tick_once(idle_seconds=3600)["action"], "busy_skip")
                    self.assertGreaterEqual(len(get_routine_manager(store=store).list_routines()), 3)

                    code_no_csrf, _ = dispatch_raw_http(srv, "POST", "/api/reload", body=b'{"dry_run":true}', headers={"Origin": "http://127.0.0.1"})
                    self.assertEqual(code_no_csrf, 403)
                    register_service_reload_handler(lambda: None)
                    try:
                        with patch("surfaces.api_extensions.threading.Timer") as mock_timer:
                            c_live, _ = dispatch_raw_http(
                                srv, "POST", "/api/reload", body=b"{}",
                                headers={"Origin": "http://127.0.0.1", "X-Companion-CSRF": sess.csrf_token},
                            )
                            self.assertEqual(c_live, 200)
                            mock_timer.assert_called_once()
                    finally:
                        register_service_reload_handler(None)
                finally:
                    srv.session.idle_watcher.stop()
                    srv.server_close()

    def test_sdlc_workflow_aliases_and_git_push_approval_execution(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp)
            bare = base / "remote.git"
            repo = base / "clone"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@companion.local"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.name", "Companion Test"], cwd=str(repo), check=True)
            subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=str(repo), check=True)
            (repo / "hello.py").write_text("val = 42\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

            with contextlib.closing(MemoryStore(db_path=base / "apr_git.db")) as store:
                set_trust_mode("balanced", store=store)
                pg = get_project_graph(store=store)
                proj = pg.create_project(title="AskMe Repo", workspace_path=str(repo), delivery_policy="ask_me")
                with workspace_context(repo, proj["project_id"]), memory_store_context(store):
                    tool_git_manage({"action": "branch", "branch": "feat/approved-push"})
                    (repo / "hello.py").write_text("val = 99\n", encoding="utf-8")
                    tool_git_manage({"action": "commit", "files": ["hello.py"], "message": "feat: bump val"})

                    dag_out = dispatch("workflow_manage", {"action": "run_dag", "dag_name": "pre-commit-verify", "params": {"repo_path": str(repo)}})
                    self.assertIn("pre-commit-verify", dag_out.lower())
                    scr_out = dispatch("workflow_manage", {"action": "run_script", "skill": "remote-endpoints", "script": "endpoint_probe.py", "args": ["--self-test"]})
                    self.assertIn("pass", scr_out.lower())

                    with agent_tool_turn_context():
                        paused = tool_git_manage({"action": "push", "remote": "origin", "branch": "feat/approved-push"})
                    self.assertIn("AWAITING_USER_APPROVAL", paused)
                    waiting = ApprovalQueue(store=store).list_requests(status="waiting")
                    self.assertEqual(len(waiting), 1)
                    apr_res = execute_core_action("approvals", {"subaction": "approve", "approval_id": waiting[0]["approval_id"]}, actor="web", store=store)
                    self.assertTrue(apr_res.get("ok"), apr_res)
                    self.assertTrue(apr_res.get("execution_ok"), apr_res)
                    refs = subprocess.run(["git", "branch", "--list"], cwd=str(bare), check=True, capture_output=True, text=True).stdout
                    self.assertIn("feat/approved-push", refs)


if __name__ == "__main__":
    unittest.main()
