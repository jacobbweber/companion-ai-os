"""Unit tests for Background Idle Routines, Skill Validation & Packaged Runtime Doctor."""

import contextlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from companion.config import settings
from companion.grounding import LocalKnowledgeAdapter, discover_skills
from companion.memory_store import MemoryStore, memory_store_context
from companion.project_graph import ProjectGraphManager
from companion.routines import format_morning_briefing, get_routine_manager
from companion.safety import run_system_doctor
from companion.skill_forge import run_skill_script, stage_and_promote_skill, validate_skill_markdown
from companion.telemetry import notify_db_closed
from companion.tools import dispatch


class TestRoutinesSkillsAndRuntimeDoctor(unittest.TestCase):
    def test_background_autonomy_routines_and_morning_briefing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.closing(MemoryStore(db_path=root / "memory" / "test.db")) as store:
                self.assertFalse(store.is_background_autonomy_enabled())
                store.set_background_autonomy(True)
                self.assertTrue(store.is_background_autonomy_enabled())

                store.log_autonomy_run(
                    "trace_reviewer",
                    "completed",
                    "Reviewed recent slow spans and logged recommendations.",
                    checkpoint_id="cp_test_1",
                    doctor_passed=True,
                )
                briefing = format_morning_briefing(store=store, mark_seen=True)
                self.assertIn("Morning Autonomy Briefing", briefing)
                self.assertIn("trace_reviewer", briefing)

                rm = get_routine_manager(store=store)
                by_id = {r["routine_id"]: r for r in rm.list_routines()}
                self.assertIn("trace_reviewer", by_id)
                self.assertNotIn("code_hygiene", by_id)

                with memory_store_context(store):
                    out = dispatch("routine_manage", {"action": "autonomy_status"})
                    self.assertIn("ON", out)

    def test_skill_forge_path_traversal_author_guard_and_knowledge_adapter(self) -> None:
        valid_md = (
            "---\nname: safe-pack\ndescription: Safe pack for guard tests.\n"
            "required_tools: [read_file]\n---\n\n# Safe Pack\n## When to Use This Skill\nTest.\n"
            "## Step-by-Step Procedure\n1. Run.\n"
        )
        self.assertIsNone(validate_skill_markdown(valid_md, expected_name="safe-pack"))
        self.assertIsNotNone(validate_skill_markdown(valid_md, expected_name="other-name"))
        self.assertIsNotNone(validate_skill_markdown("# Missing frontmatter\n"))

        good_script = '"""Check."""\nimport sys\nif "--self-test" in sys.argv:\n    sys.exit(0)\nprint("ok")\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIn("invalid skill name", run_skill_script("../../outside", "evil", root=root))
            self.assertIn("invalid skill name", run_skill_script("bad/slash", "evil", root=root))

            p1 = stage_and_promote_skill("safe-pack", valid_md, scripts={"check.py": good_script}, root=root)
            self.assertTrue(p1["promoted"], p1["summary"])
            self.assertEqual(discover_skills(root)[0]["scripts"], "check.py")
            user_ref = root / "skills" / "safe-pack" / "references" / "human.md"
            user_ref.parent.mkdir(parents=True, exist_ok=True)
            user_ref.write_text("---\nauthor: user\n---\n# Keep Me\n", encoding="utf-8")
            p2 = stage_and_promote_skill("safe-pack", valid_md, overwrite=True, root=root)
            self.assertFalse(p2["promoted"])
            self.assertIn("WIKI AUTHOR GUARD REJECTED", p2["summary"])
            self.assertTrue(user_ref.is_file())

            p_noself = stage_and_promote_skill(
                "no-selftest-pack",
                valid_md.replace("safe-pack", "no-selftest-pack"),
                scripts={"noop.py": '"""Noop."""\ndef helper() -> int:\n    return 1\n'},
                root=root,
            )
            self.assertFalse(p_noself["promoted"])
            self.assertIn("--self-test", p_noself["summary"])

            with contextlib.closing(MemoryStore(db_path=root / "mem.db")) as st:
                docs_dir = root / "refs"
                docs_dir.mkdir()
                (root / "secret.md").write_text("# Secret\n", encoding="utf-8")
                (docs_dir / "guide.md").write_text("# Guide Doc\nBody\n", encoding="utf-8")
                st.add_system_note("Pinned gateway timeout lesson", category="architecture")
                st.set_user_profile("known_projects_json", json.dumps({"alpha-app": str(root)}))
                adapter = LocalKnowledgeAdapter(store=st, docs_dir=docs_dir)
                self.assertIsNone(adapter.read_entry("doc:../secret"))
                self.assertIsNotNone(adapter.read_entry("doc:guide"))
                self.assertIn("Pinned gateway", adapter.read_entry("note:1").content)
                self.assertEqual(adapter.read_entry("project:alpha-app").content, str(root))
                st.upsert_knowledge_fact("cfg:endpoint", "http://127.0.0.1:9000", source="user")
                with memory_store_context(st):
                    self.assertIn("http://127.0.0.1:9000", dispatch("memory_manage", {"action": "query_facts", "key": "cfg:endpoint"}))

    def test_safe4_project_workspace_path_and_packaged_app_doctor_without_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            code_repo = base / "repo_alpha"
            code_repo.mkdir()
            with contextlib.closing(MemoryStore(db_path=base / "pg.db")) as st:
                pg = ProjectGraphManager(store=st)
                code_proj = pg.create_project("Alpha Service", kind="project", workspace_path=str(code_repo), project_id="alpha-svc")
                life_proj = pg.create_project("Weekend Garden", kind="goal", workspace_path="", project_id="garden-goal")
                self.assertEqual(Path(code_proj["workspace_path"]), code_repo.resolve())
                self.assertEqual(life_proj["workspace_path"], "")

                pg.set_focus_project("alpha-svc")
                with pg.project_workspace_scope():
                    self.assertEqual(settings.get_workspace().project_root, code_repo.resolve())
                    self.assertEqual(settings.get_workspace().project_id, "alpha-svc")

                pg.set_focus_project("garden-goal")
                with pg.project_workspace_scope():
                    self.assertTrue(settings.get_workspace().is_self_project)

            staged_app = base / "packaged_app"
            staged_app.mkdir()
            shutil.copy2(settings.root / "harness.py", staged_app / "harness.py")
            for dirname in ("companion", "surfaces", "skills", "deploy"):
                src_d = settings.root / dirname
                if src_d.is_dir():
                    shutil.copytree(src_d, staged_app / dirname, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            self.assertFalse((staged_app / "tests").exists())
            doc = run_system_doctor(root=staged_app, run_tests=True)
            self.assertTrue(doc["healthy"], doc["summary"])
            self.assertEqual(len(doc["checks"]), 7)
            self.assertTrue(all(c["passed"] for c in doc["checks"]))
            notify_db_closed(staged_app / "data" / "conversations.db")


if __name__ == "__main__":
    unittest.main()
