"""Unit tests for AST syntax guard, file backups, workspace checkpoints, and LoopGuard."""

import os
import tempfile
import unittest
from pathlib import Path

from companion.memory_store import MemoryStore
from companion.safety import (
    CheckpointManager,
    LoopGuard,
    check_command_safety,
    check_path_safety,
    try_indent_shifted_replace,
    validate_python_syntax,
)


class TestSafetyAndRecovery(unittest.TestCase):
    def test_validate_python_syntax(self) -> None:
        ok, err = validate_python_syntax("def add(a, b):\n    return a + b\n")
        self.assertTrue(ok)
        self.assertIsNone(err)

        bad_ok, bad_err = validate_python_syntax("def add(a, b)\n    return a + b\n")
        self.assertFalse(bad_ok)
        self.assertIn("SyntaxError", str(bad_err))

    def test_file_backup_and_checkpoint_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(db_path=root / "memory" / "test.db")
            task_id = store.add_task("Implement feature X", status="pending")
            cp_mgr = CheckpointManager(root=root, backup_dir=root / ".companion_backups", store=store)
            target = root / "module.py"
            target.write_text('"""Original."""\nVALUE = 1\n', encoding="utf-8")

            # Create workspace checkpoint (includes SQLite state snapshot)
            cp = cp_mgr.create_checkpoint(label="v1")
            self.assertEqual(cp["file_count"], 1)

            # Backup, modify file, create Companion-touched sprawl + untracked human file, and mark task completed
            cp_mgr.backup_file(target)
            target.write_text('"""Modified."""\nVALUE = 2\n', encoding="utf-8")
            sprawl = root / "sprawl_extra.py"
            sprawl.write_text('"""Sprawl."""\nX = 99\n', encoding="utf-8")
            cp_mgr.record_file_touch(sprawl, False, None, sprawl.read_bytes())
            human_untracked = root / "human_ide_draft.tsx"
            human_untracked.write_text("export const Draft = () => <div>WIP</div>;\n", encoding="utf-8")
            user_edited_sprawl = root / "companion_then_human.cs"
            user_edited_sprawl.write_text("class A {}\n", encoding="utf-8")
            cp_mgr.record_file_touch(user_edited_sprawl, False, None, user_edited_sprawl.read_bytes())
            user_edited_sprawl.write_text("class A { public int UserWork = 1; }\n", encoding="utf-8")
            store.update_task(task_id, status="completed")
            self.assertEqual(len(store.get_pending_tasks()), 0)

            # Rollback restores target, deletes Companion sprawl, preserves untracked & user-edited files, and rewinds DB
            rb = cp_mgr.rollback_checkpoint(cp["checkpoint_id"], reason="Unit test failed during feature X")
            self.assertTrue(rb["restored"])
            self.assertIn("VALUE = 1", target.read_text(encoding="utf-8"))
            self.assertFalse(sprawl.exists())
            self.assertTrue(human_untracked.exists())
            self.assertTrue(user_edited_sprawl.exists())
            self.assertIn("companion_then_human.cs", rb["skipped_user_modified"])
            self.assertEqual(len(store.get_pending_tasks()), 1)
            lessons = store.get_system_notes(category="rollback_lesson")
            self.assertEqual(len(lessons), 1)
            self.assertIn("Unit test failed during feature X", lessons[0][0])
            store.close()

    def test_try_indent_shifted_replace_and_subprocess_syntax_guard(self) -> None:
        source = "class Demo:\n    def run(self):\n        x = 1\n        return x\n"
        # Uniform +2 shift on lines 1..N with unindented first line
        old = "def run(self):\n          x = 1\n          return x"
        new = "def run(self):\n          x = 2\n          return x"
        updated = try_indent_shifted_replace(source, old, new)
        self.assertIsNotNone(updated)
        self.assertIn("    def run(self):\n        x = 2\n        return x", str(updated))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cp_mgr = CheckpointManager(root=root, backup_dir=root / ".companion_backups")
            py_f = root / "mod.py"
            py_f.write_text("x = 1\n", encoding="utf-8")
            snap = cp_mgr.snapshot_py_files()
            py_f.write_text("def broken(\n", encoding="utf-8")
            err = cp_mgr.guard_py_files_after_exec(snap)
            self.assertIsNotNone(err)
            self.assertIn("SYNTAX GUARD REJECTED subprocess file modification", str(err))
            self.assertEqual(py_f.read_text(encoding="utf-8"), "x = 1\n")

    def test_loop_guard_intervenes_on_repeated_identical_calls(self) -> None:
        guard = LoopGuard(max_identical_calls=2, max_consecutive_errors=2)
        self.assertIsNone(guard.check_before_call("list_dir", {"path": "."}))
        self.assertIsNone(guard.check_before_call("list_dir", {"path": "."}))
        # 3rd identical consecutive call must trigger intervention
        msg = guard.check_before_call("list_dir", {"path": "."})
        self.assertIsNotNone(msg)
        self.assertIn("LOOP GUARD INTERVENTION", str(msg))

    def test_loop_guard_detects_consecutive_errors(self) -> None:
        guard = LoopGuard(max_identical_calls=2, max_consecutive_errors=2)
        self.assertIsNone(guard.record_result("run_shell", "error: failed 1"))
        hint = guard.record_result("run_shell", "exit=1\nfailed 2")
        self.assertIsNotNone(hint)
        self.assertIn("SELF-RECOVERY HINT", str(hint))

    def test_command_and_path_safety_guardrails(self) -> None:
        # Destructive shell commands must be deterministically rejected
        for bad_cmd in (
            "rm -rf /",
            "git push origin main --force",
            "git reset --hard HEAD~1",
            "git clean -fd",
            "format C:",
            "DROP TABLE conversations;",
            "shutdown /s /t 0",
        ):
            err = check_command_safety(bad_cmd)
            self.assertIsNotNone(err, f"Expected `{bad_cmd}` to be blocked")
            self.assertIn("SAFETY GUARD REJECTED command", str(err))

        # Safe everyday commands must pass
        for ok_cmd in ("git status", "python -m unittest", "git push --force-with-lease"):
            self.assertIsNone(check_command_safety(ok_cmd))

        # Path confinement and protected file checks
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertIsNone(check_path_safety(root / "src" / "app.py", write=True, allowed_roots=[root]))
            self.assertIsNone(check_path_safety(root / ".env.example", write=True, allowed_roots=[root]))

            outside = root.parent / "outside_secret.txt"
            out_err = check_path_safety(outside, write=False, allowed_roots=[root])
            self.assertIn("SAFETY GUARD REJECTED path", str(out_err))

            git_err = check_path_safety(root / ".git" / "config", write=True, allowed_roots=[root])
            self.assertIn("SAFETY GUARD REJECTED path", str(git_err))

            env_err = check_path_safety(root / ".env", write=True, allowed_roots=[root])
            self.assertIn("SAFETY GUARD REJECTED path", str(env_err))

    def test_external_linter_scoping_and_vault_subprocess_fence(self) -> None:
        from companion.autonomy import scan_placeholder_violations
        from companion.config import settings, workspace_context
        from companion.grounding import LocalWorkspaceTarget
        from companion.memory_store import memory_store_context
        from companion.safety import lint_single_file_edit
        from companion.tools import dispatch
        from companion.vault import VaultManager

        with tempfile.TemporaryDirectory() as tmp:
            ext = Path(tmp) / "user_webapp"
            (ext / "skills").mkdir(parents=True)
            js_ok, js_warn = lint_single_file_edit(ext / "app.js", "const msg = `${title || 'Yes or No'}`;\n")
            self.assertIsNone(js_ok)
            self.assertEqual(js_warn, "")
            html_ld = '<html><head><script type="application/ld+json">{"@type": "Organization"}</script><script type="importmap">{"imports": {}}</script></head></html>'
            html_err, _ = lint_single_file_edit(ext / "index.html", html_ld)
            self.assertIsNone(html_err)
            ext_skill_err, _ = lint_single_file_edit(ext / "skills" / "custom.py", "import requests\nprint(requests)\n")
            self.assertIsNone(ext_skill_err)
            abc_py = ext / "base.py"
            abc_py.write_text("from abc import ABC, abstractmethod\nclass Port(ABC):\n    @abstractmethod\n    def run(self) -> None:\n        raise NotImplementedError\n", encoding="utf-8")
            with workspace_context(ext, "user-webapp"):
                self.assertEqual(scan_placeholder_violations([abc_py], root=ext), [])

            for secret_cmd in ("cat data/secrets.env", "Get-Content .env", "type .comp_ssh_key"):
                self.assertIsNotNone(check_command_safety(secret_cmd))

            st = MemoryStore(db_path=Path(tmp) / "vault_fence.db")
            orig_data = settings.data_dir
            try:
                settings.data_dir = Path(tmp) / "vdata"
                settings.data_dir.mkdir(parents=True, exist_ok=True)
                vm = VaultManager(store=st, secrets_path=settings.data_dir / "secrets.env")
                vm.set_secret("leak-probe", "SUPER_SECRET_VAULT_TOKEN_987654", cred_type="api_key", actor="user")
                os.environ["TEMP_TEST_API_KEY"] = "ENV_SECRET_VAL_123456"
                try:
                    with workspace_context(ext, "user-webapp"), memory_store_context(st):
                        env_child, _ = LocalWorkspaceTarget().exec_env_and_cwd()
                        self.assertNotIn("TEMP_TEST_API_KEY", env_child)
                        blocked_py = dispatch("run_python", {"code": "print(open('secrets.env').read())"})
                        self.assertIn("SAFETY GUARD REJECTED command", blocked_py)
                        echoed = dispatch("run_python", {"code": "print('SUPER_SECRET_' + 'VAULT_TOKEN_987654')"})
                        self.assertNotIn("SUPER_SECRET_VAULT_TOKEN_987654", echoed)
                        self.assertIn("[REDACTED", echoed)
                finally:
                    os.environ.pop("TEMP_TEST_API_KEY", None)
            finally:
                settings.data_dir = orig_data
                st.close()


if __name__ == "__main__":
    unittest.main()
