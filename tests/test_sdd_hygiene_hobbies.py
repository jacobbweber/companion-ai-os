"""Unit tests for Living Canonical SDD, AST Dead-Code Hygiene, and Autonomous Agent Hobbies."""

import tempfile
import unittest
from pathlib import Path

from companion.hobbies import HobbyEngine
from companion.hygiene import analyze_workspace_hygiene
from companion.memory_store import MemoryStore
from companion.safety import CheckpointManager
from companion.sdd import (
    MAX_ACTIVE_ADRS,
    sync_decisions_md,
    validate_skill_markdown,
    validate_workflow_recipe_json,
    verify_sdd,
    verify_skills_directory,
)
from companion.tools import dispatch


class TestSDDHygieneAndHobbies(unittest.TestCase):
    def test_live_workspace_passes_sdd_verification(self) -> None:
        rep = verify_sdd()
        self.assertTrue(rep.valid, f"SDD verification failed:\n{rep.format_summary()}")
        self.assertEqual(len(rep.present_spec_files), 4)
        self.assertEqual(len(rep.sprawl_files), 0)
        self.assertEqual(len(rep.undocumented_modules), 0)

    def test_sdd_detects_spec_sprawl_and_undocumented_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs_dir = root / "specs"
            specs_dir.mkdir()
            for fname in ("PRODUCT.md", "ARCHITECTURE.md", "CONTRACTS.md", "DECISIONS.md"):
                (specs_dir / fname).write_text("## SPEC-01: Test\nOwned Modules:\nInvariants:\nVerification Tests:\n", encoding="utf-8")
            # Create an extra sprawl file in specs/
            (specs_dir / "001_extra_sprawl.md").write_text("sprawl", encoding="utf-8")
            # Create an undocumented companion module
            comp_dir = root / "companion"
            comp_dir.mkdir()
            (comp_dir / "mystery.py").write_text('"""Mystery."""\nx = 1\n', encoding="utf-8")

            store = MemoryStore(db_path=root / "memory" / "test.db")
            rep = verify_sdd(root=root, store=store)
            self.assertFalse(rep.valid)
            self.assertIn("001_extra_sprawl.md", rep.sprawl_files)
            self.assertIn("companion/mystery.py", rep.undocumented_modules)
            store.close()

    def test_adr_compaction_keeps_active_adrs_within_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(db_path=root / "memory" / "test.db")
            try:
                for i in range(20):
                    store.add_adr(
                        title=f"ADR-1{i:02d}: Decision {i}",
                        context="Ctx",
                        decision="Dec",
                        consequences="Cons",
                        status="accepted",
                    )
                dec_path = sync_decisions_md(store=store, root=root)
                text = dec_path.read_text(encoding="utf-8")
                self.assertIn("Historical / Superseded Decisions (Compacted)", text)
                self.assertEqual(text.count("### ADR-"), MAX_ACTIVE_ADRS)
            finally:
                store.close()

    def test_live_workspace_passes_ast_hygiene_and_dead_code_check(self) -> None:
        rep = analyze_workspace_hygiene()
        self.assertTrue(rep.clean, f"Workspace hygiene check failed:\n{rep.format_summary()}")

    def test_hygiene_scanner_detects_unused_imports_and_dead_functions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            comp = root / "companion"
            comp.mkdir()
            (comp / "sample.py").write_text(
                '"""Sample."""\nfrom math import sqrt\n\nclass Box:\n    sqrt = 1\n\ndef unused_recursive(n):\n    return unused_recursive(n - 1)\n\ndef misindented_method(self):\n    return Box().sqrt\n',
                encoding="utf-8",
            )
            rep = analyze_workspace_hygiene(root=root)
            self.assertFalse(rep.clean)
            self.assertEqual(len(rep.unused_imports), 1)
            self.assertIn("sqrt", rep.unused_imports[0])
            self.assertEqual(len(rep.dead_definitions), 2)
            self.assertTrue(any("unused_recursive" in d for d in rep.dead_definitions))
            self.assertTrue(any("misindented_method(self)" in d for d in rep.dead_definitions))

    def test_hobby_engine_toggle_and_morning_briefing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(db_path=root / "memory" / "test.db")
            cp_mgr = CheckpointManager(root=root, backup_dir=root / ".companion_backups")
            engine = HobbyEngine(store=store, cp_manager=cp_mgr, root=root)

            self.assertFalse(engine.is_enabled())
            engine.set_enabled(True)
            self.assertTrue(engine.is_enabled())

            store.log_hobby_run(
                hobby_name="code_simplifier",
                status="completed",
                summary="Verified zero dead code and synced specs.",
                checkpoint_id="cp_test_1",
                doctor_passed=True,
            )
            briefing = engine.get_morning_briefing(mark_seen=True)
            self.assertIn("Morning Briefing (1 new autonomous session(s)", briefing)
            self.assertIn("code_simplifier", briefing)

            # Verify ModelGate abort_check preemption during hobby cycle rolls back cleanly
            from companion.llm import model_gate

            target_file = root / "sample.py"
            target_file.write_text("X = 1\n", encoding="utf-8")

            def preempted_runner(_msgs, _limit) -> str:
                target_file.write_text("X = 999\n", encoding="utf-8")
                with model_gate.acquire_call():
                    return "Should not reach here"

            # Verify self_dev hobby is mechanically refused while Self-Dev Mode is default OFF, then preempts cleanly when ON
            blocked_res = engine.run_hobby_cycle(hobby_name="code_simplifier", runner_fn=preempted_runner, abort_check=lambda: True)
            self.assertEqual(blocked_res["status"], "refused_self_dev_off")
            store.set_self_dev_mode(True, actor="user")
            res = engine.run_hobby_cycle(hobby_name="code_simplifier", runner_fn=preempted_runner, abort_check=lambda: True)
            self.assertEqual(res["status"], "preempted")
            self.assertEqual(target_file.read_text(encoding="utf-8"), "X = 1\n")

            # Verify trace_reviewer hobby is registered and selectable, and singleton respects memory_store_context
            from companion.hobbies import hobby_engine
            from companion.memory_store import memory_store_context

            tr_spec = engine.select_next_hobby("trace_reviewer")
            self.assertEqual(tr_spec.name, "trace_reviewer")
            self.assertIn("telemetry_manage", tr_spec.instructions)
            with memory_store_context(store):
                self.assertIs(hobby_engine.store, store)
                self.assertIn("ON", dispatch("hobby_manage", {"action": "status"}))
            store.close()

    def test_skill_and_workflow_recipe_linter_and_write_guards(self) -> None:
        valid_md = (
            "---\nname: demo-skill\ndescription: A valid demo skill for testing.\n"
            "required_tools: [read_file]\n---\n\n# Demo\n## When to Use This Skill\nTest.\n"
            "## Step-by-Step Procedure\n1. Run.\n"
        )
        self.assertIsNone(validate_skill_markdown(valid_md, expected_name="demo-skill"))
        self.assertIsNotNone(validate_skill_markdown(valid_md, expected_name="other-name"))
        self.assertIsNotNone(validate_skill_markdown("# Missing frontmatter\n"))

        valid_recipe = (
            '{"name": "demo-flow", "description": "Valid workflow recipe.", '
            '"nodes": [{"id": "n1", "type": "tool", "tool": "sdd_manage", "args": {"action": "verify"}}], '
            '"edges": [{"from": "n1", "to": "COMPLETE", "condition": "passed"}]}'
        )
        self.assertIsNone(validate_workflow_recipe_json(valid_recipe, expected_name="demo-flow"))
        self.assertIsNotNone(validate_workflow_recipe_json('{"name": "bad", "description": "short"}'))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_skill_path = root / "skills" / "demo-skill" / "SKILL.md"
            res1 = dispatch("write_file", {"path": str(bad_skill_path), "content": "# No frontmatter"})
            self.assertIn("SKILL LINTER REJECTED", res1)
            self.assertFalse(bad_skill_path.exists())

            bad_wf_path = root / "skills" / "demo-skill" / "workflows" / "demo-flow.json"
            res2 = dispatch("write_file", {"path": str(bad_wf_path), "content": '{"name": "demo-flow"}'})
            self.assertIn("WORKFLOW RECIPE LINTER REJECTED", res2)
            self.assertFalse(bad_wf_path.exists())

            # Write valid skill referencing a missing Tier-3 file and verify `verify_skills_directory` catches it
            ref_md = valid_md + "\nSee `references/missing_doc.md`.\n"
            res3 = dispatch("write_file", {"path": str(bad_skill_path), "content": ref_md})
            self.assertIn("wrote", res3)
            issues = verify_skills_directory(root=root)
            self.assertTrue(any("missing_doc.md" in i for i in issues), f"Expected missing_doc.md issue in {issues}")

            # Verify `author: user` provenance guard protects user-authored Wiki reference articles
            user_wiki = root / "skills" / "knowledge-wiki" / "references" / "user-guide.md"
            user_content = "---\ntitle: User Guide\nauthor: user\n---\n\n# Protected Note\n"
            self.assertIn("wrote", dispatch("write_file", {"path": str(user_wiki), "content": user_content}))
            blocked_w = dispatch("write_file", {"path": str(user_wiki), "content": "# Overwritten\n"})
            self.assertIn("WIKI AUTHOR GUARD REJECTED", blocked_w)
            blocked_e = dispatch("edit_file", {"path": str(user_wiki), "old_str": "# Protected Note", "new_str": "# Changed"})
            self.assertIn("WIKI AUTHOR GUARD REJECTED", blocked_e)
            self.assertEqual(user_wiki.read_text(encoding="utf-8"), user_content)

    def test_skill_forge_path_traversal_author_guard_and_script_write_guards(self) -> None:
        import json

        from companion.grounding import LocalKnowledgeAdapter, discover_skills
        from companion.skill_forge import run_skill_script, stage_and_promote_skill

        valid_md = (
            "---\nname: safe-pack\ndescription: Safe pack for guard tests.\n"
            "required_tools: [read_file]\n---\n\n# Safe Pack\n## When to Use This Skill\nTest.\n"
            "## Step-by-Step Procedure\n1. Run.\n"
        )
        good_script = '"""Check."""\nimport sys\nif "--self-test" in sys.argv:\n    sys.exit(0)\nprint("ok")\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIn("invalid skill name", run_skill_script("../../outside", "evil", root=root))
            self.assertIn("invalid skill name", run_skill_script("bad/slash", "evil", root=root))

            # Direct write_file to skills/*/scripts/*.py rejects missing --self-test and third-party imports
            sc_target = root / "skills" / "safe-pack" / "scripts" / "check.py"
            r_noself = dispatch("write_file", {"path": str(sc_target), "content": '"""No self test."""\nprint(1)\n'})
            self.assertIn("SKILL LINTER REJECTED", r_noself)
            r_ext = dispatch("write_file", {"path": str(sc_target), "content": '"""Ext."""\nimport requests\n# --self-test\nprint(requests)\n'})
            self.assertIn("SKILL LINTER REJECTED", r_ext)
            self.assertFalse(sc_target.exists())

            # Promote initial skill, add `author: user` reference doc, and verify `overwrite=True` refuses to delete it
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

            # Staged script missing --self-test entrypoint is rejected even if exit code would be 0
            p_noself = stage_and_promote_skill(
                "no-selftest-pack",
                valid_md.replace("safe-pack", "no-selftest-pack"),
                scripts={"noop.py": '"""Noop."""\ndef helper() -> int:\n    return 1\n'},
                root=root,
            )
            self.assertFalse(p_noself["promoted"])
            self.assertIn("--self-test", p_noself["summary"])

            # External project with `surfaces/` or `companion/` subfolder keeps `enforce_stdlib_only=False`
            from companion.disciplines import resolve_workspace_contract
            from companion.memory_store import memory_store_context

            ext_ui = root / "ext_3d_app"
            (ext_ui / "surfaces").mkdir(parents=True)
            (ext_ui / "companion").mkdir(parents=True)
            ext_contract = resolve_workspace_contract(ext_ui, is_self_project=False)
            self.assertEqual(ext_contract.contract_id, "general-workspace")
            self.assertFalse(ext_contract.enforce_stdlib_only)

            # Verify LocalKnowledgeAdapter.read_entry blocks doc:../ traversal and resolves note:/project: keys
            st = MemoryStore(db_path=root / "mem.db")
            try:
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
            finally:
                st.close()


if __name__ == "__main__":
    unittest.main()
