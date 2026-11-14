"""Unit tests for AST syntax guard, file backups, workspace checkpoints, and LoopGuard."""

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

            # Backup, modify file, create sprawl file, and prematurely mark task completed
            cp_mgr.backup_file(target)
            target.write_text('"""Modified."""\nVALUE = 2\n', encoding="utf-8")
            sprawl = root / "sprawl_extra.py"
            sprawl.write_text('"""Sprawl."""\nX = 99\n', encoding="utf-8")
            store.update_task(task_id, status="completed")
            self.assertEqual(len(store.get_pending_tasks()), 0)

            # Rollback to checkpoint v1 must restore target, delete sprawl, rewind task to pending, and save lesson
            rb = cp_mgr.rollback_checkpoint(cp["checkpoint_id"], reason="Unit test failed during feature X")
            self.assertTrue(rb["restored"])
            self.assertIn("VALUE = 1", target.read_text(encoding="utf-8"))
            self.assertFalse(sprawl.exists())
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


if __name__ == "__main__":
    unittest.main()
