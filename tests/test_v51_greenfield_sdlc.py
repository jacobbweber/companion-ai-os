"""
Adversarial Unit & Integration Verification for Companion v5.1:
Greenfield Git Project Bootstrap + Native 7-Stage Co-Design SDLC Skill System.
"""

import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

from companion.agent import _is_active_sdlc_codesign_turn, handle_slash_command, is_awaiting_blueprint_approval, run_agent_turn, triage_turn
from companion.config import Settings, settings, workspace_context
from companion.context import build_turn_context, discover_skills, manage_active_project
from companion.core_actions import execute_core_action
from companion.memory_store import MemoryStore, memory_store_context
from companion.projects import get_project_graph
from companion.tools import TOOLS, dispatch
from companion.workflows import discover_dags, execute_dag_by_name, validate_skill_markdown
from companion.worktrees import (
    SDLC_SLASH_MAP,
    discover_repo_sdlc_context,
    init_greenfield_repo,
    inspect_sdlc_repo_state,
    probe_repo_workspace,
    resolve_sdlc_slash_turn,
    validate_external_repo_path,
)
from surfaces.web_server import create_web_server, dispatch_raw_http


class TestGreenfieldBootstrapAndSdlcSystem(unittest.TestCase):
    def tearDown(self) -> None:
        settings.reset_workspace()

    def test_projects_dir_resolution_and_path_safety_guards(self) -> None:
        """Verify COMPANION_PROJECTS_DIR, packaged Linux ~/projects default, and app/ + data/ rejection."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp).resolve()
            explicit_projects = tmp_p / "custom_projects"
            with patch.dict(os.environ, {"COMPANION_PROJECTS_DIR": str(explicit_projects)}, clear=False):
                s_env = Settings()
                self.assertEqual(s_env.projects_dir, explicit_projects)

            fake_home = tmp_p / "fake_home"
            packaged_app = fake_home / ".local" / "share" / "companion" / "app"
            packaged_app.mkdir(parents=True)
            with patch.dict(os.environ, {"COMPANION_PROJECTS_DIR": ""}, clear=False), patch("pathlib.Path.home", return_value=fake_home):
                s_pkg = Settings()
                s_pkg.root = packaged_app
                self.assertEqual(s_pkg.projects_dir, (fake_home / "projects").resolve())

            # Mechanically block repo creation inside Companion's app/ or data/
            err_app = validate_external_repo_path(settings.root / "nested_repo")
            self.assertIsNotNone(err_app)
            self.assertIn("SAFETY GUARD REJECTED", err_app or "")

            err_data = validate_external_repo_path(settings.effective_data_dir / "nested_repo")
            self.assertIsNotNone(err_data)
            self.assertIn("SAFETY GUARD REJECTED", err_data or "")

            probe_bad = probe_repo_workspace(str(settings.root / "bad_subrepo"))
            self.assertFalse(probe_bad["ok"])
            self.assertIn("SAFETY GUARD REJECTED", probe_bad["error"])

            clean_cand = tmp_p / "clean_greenfield"
            probe_ok = probe_repo_workspace(str(clean_cand))
            self.assertTrue(probe_ok["ok"])
            self.assertTrue(probe_ok["can_init"])
            self.assertFalse(probe_ok["is_git_repo"])

    def test_init_greenfield_repo_unset_git_author_idempotency_and_worktree_ready(self) -> None:
        """Verify init_greenfield_repo works without global git config, is idempotent, and supports immediate git worktree."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp).resolve()
            empty_home = tmp_p / "empty_git_home"
            empty_home.mkdir()
            repo_dir = tmp_p / "projects" / "invoice-reconciler"

            clean_env = {
                k: v for k, v in os.environ.items()
                if not k.upper().startswith("GIT_AUTHOR_") and not k.upper().startswith("GIT_COMMITTER_")
            }
            clean_env.update({
                "HOME": str(empty_home),
                "USERPROFILE": str(empty_home),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(empty_home / ".gitconfig"),
            })
            with patch.dict(os.environ, clean_env, clear=True):
                res1 = init_greenfield_repo(repo_dir, title="Invoice Reconciler")
                self.assertTrue(res1["ok"], res1)
                self.assertTrue(res1["created_repo"])
                self.assertTrue(res1["initial_commit_created"])
                self.assertFalse(res1["idempotent"])
                self.assertEqual(res1["branch"], "main")
                self.assertTrue(res1["commit_sha"])
                self.assertTrue((repo_dir / ".gitignore").is_file())
                self.assertTrue((repo_dir / "README.md").is_file())

                # Customizing README.md and re-running must be a strict idempotent NoOp
                (repo_dir / "README.md").write_text("# Custom User Readme\n", encoding="utf-8")
                res2 = init_greenfield_repo(repo_dir, title="Invoice Reconciler")
                self.assertTrue(res2["ok"], res2)
                self.assertTrue(res2["idempotent"])
                self.assertFalse(res2["created_repo"])
                self.assertEqual(res2["commit_sha"], res1["commit_sha"])
                self.assertEqual((repo_dir / "README.md").read_text(encoding="utf-8"), "# Custom User Readme\n")

            # Verify git worktree works immediately because HEAD exists on main
            with workspace_context(repo_dir, "proj_inv"):
                wt_out = dispatch("git_manage", {"action": "worktree_add", "branch": "feat/inc-01", "card_id": "inc01"})
                self.assertIn("Created isolated worktree", wt_out)
                wt_path = json.loads(wt_out)["worktree_path"]
                rm_out = dispatch("git_manage", {"action": "worktree_remove", "worktree_path": wt_path})
                self.assertIn("Removed worktree", rm_out)

    def test_project_create_focus_sync_attach_validation_and_slash_project_init(self) -> None:
        """Verify create_project(init_git=True), personal board focus reset, attach validation, and /project init."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp).resolve()
            proj_root = tmp_p / "user_projects"
            proj_root.mkdir()
            db_file = tmp_p / "proj.db"
            with (
                patch.dict(os.environ, {"COMPANION_PROJECTS_DIR": str(proj_root)}, clear=False),
                contextlib.closing(MemoryStore(db_path=db_file)) as store,
                memory_store_context(store),
            ):
                # 1. Create greenfield code project via agile CoreAction (UI "Create New Repo" path)
                res_create = execute_core_action(
                    "agile",
                    {
                        "subaction": "create_project",
                        "title": "Telemetry Gateway",
                        "goal_summary": "High-throughput OTEL collector",
                        "delivery_policy": "ask_me",
                        "kind": "software",
                        "init_git": True,
                        "focus": True,
                    },
                    actor="web",
                    store=store,
                )
                self.assertTrue(res_create["ok"], res_create)
                created_pid = res_create["project"]["project_id"]
                expected_repo = (proj_root / "telemetry-gateway").resolve()
                self.assertTrue((expected_repo / ".git").exists())
                self.assertEqual(settings.get_workspace().project_root.resolve(), expected_repo)
                self.assertFalse(settings.get_workspace().is_self_project)
                card = get_project_graph(store=store).create_item(project_id=created_pid, title="Implement OTLP parser")

                # 2. Switch to a Personal Goal Board -> must reset WorkspaceContext back to companion-self!
                personal = get_project_graph(store=store).create_project("Marathon Prep", kind="goal")
                get_project_graph(store=store).set_focus_project(personal["project_id"])
                self.assertTrue(settings.get_workspace().is_self_project)
                self.assertEqual(settings.get_workspace().project_root.resolve(), settings.root.resolve())

                # 2b. `/project switch telemetry-gateway` must reuse existing SQLite project (zero duplicate rows, zero clobber!)
                sw_out = manage_active_project("switch telemetry-gateway", store=store)
                self.assertIn("Switched active WorkspaceContext", sw_out)
                gw_rows = [p for p in get_project_graph(store=store).list_projects() if p["workspace_path"] == str(expected_repo)]
                self.assertEqual(len(gw_rows), 1)
                self.assertEqual(gw_rows[0]["project_id"], created_pid)
                self.assertEqual(gw_rows[0]["delivery_policy"], "ask_me")
                self.assertEqual(gw_rows[0]["goal_summary"], "High-throughput OTEL collector")
                self.assertEqual(len(get_project_graph(store=store).list_items(project_id=created_pid)), 1, card)

                # 3. "Attach Existing Clone" rejects BOTH missing AND non-git plain directories
                res_missing = execute_core_action(
                    "agile",
                    {
                        "subaction": "create_project",
                        "title": "Broken Clone",
                        "kind": "software",
                        "workspace_path": str(proj_root / "does-not-exist"),
                        "init_git": False,
                        "require_existing": True,
                    },
                    actor="web",
                    store=store,
                )
                self.assertFalse(res_missing["ok"])

                plain_dir = proj_root / "plain-non-git-folder"
                plain_dir.mkdir()
                res_plain = execute_core_action(
                    "agile",
                    {
                        "subaction": "create_project",
                        "title": "Plain Folder Clone",
                        "kind": "software",
                        "workspace_path": str(plain_dir),
                        "init_git": False,
                        "require_existing": True,
                    },
                    actor="web",
                    store=store,
                )
                self.assertFalse(res_plain["ok"])
                self.assertIn("not a Git repository", res_plain["error"])

                # 4. Rejects paths inside Companion's app/
                res_app = execute_core_action(
                    "agile",
                    {
                        "subaction": "create_project",
                        "title": "Illegal Inside App",
                        "kind": "software",
                        "workspace_path": str(settings.root / "illegal_subrepo"),
                        "init_git": True,
                    },
                    actor="web",
                    store=store,
                )
                self.assertFalse(res_app["ok"])
                self.assertIn("SAFETY GUARD REJECTED", res_app["error"])

                # 5. `/project init <slug>` initializes under COMPANION_PROJECTS_DIR on `main` with initial commit
                slash_out = manage_active_project("init ledger-cli", store=store)
                self.assertIn("Initialized & activated project", slash_out)
                ledger_repo = (proj_root / "ledger-cli").resolve()
                self.assertTrue((ledger_repo / ".git").exists())
                head_proc = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=str(ledger_repo), capture_output=True, text=True, check=True,
                )
                self.assertEqual(head_proc.stdout.strip(), "main")

                # 6. `git_manage(action='init', slug='...')` from `companion-self` initializes & focuses repo for immediate writes
                manage_active_project("self", store=store)
                self.assertTrue(settings.get_workspace().is_self_project)
                gm_out = dispatch("git_manage", {"action": "init", "slug": "trail-gear-cli", "title": "Trail Gear CLI"})
                self.assertIn("Initialized greenfield Git repository", gm_out)
                trail_repo = (proj_root / "trail-gear-cli").resolve()
                self.assertEqual(settings.get_workspace().project_root.resolve(), trail_repo)
                dispatch("write_file", {"path": "product.md", "content": "# Trail Gear CLI\n\nScoped product spec.\n"})
                self.assertTrue((trail_repo / "product.md").is_file())
                self.assertFalse((settings.scratch_dir / "product.md").exists())

    def test_cross_workspace_skill_template_read_and_external_write_isolation(self) -> None:
        """Verify external repos can read Companion's skills/*/references/*.template.md while writes stay in external repo."""
        with tempfile.TemporaryDirectory() as tmp:
            ext_repo = Path(tmp).resolve() / "ext_service"
            init_res = init_greenfield_repo(ext_repo, title="Ext Service")
            self.assertTrue(init_res["ok"], init_res)

            with workspace_context(ext_repo, "proj_ext"):
                tpl = dispatch("read_file", {"path": "skills/sdlc-design/references/engineering-design.template.md"})
                self.assertFalse(tpl.startswith("error:"), tpl[:200])
                self.assertIn("Engineering Design", tpl)

                tpl_dot = dispatch("read_file", {"path": "./skills/sdlc-product/references/product.template.md"})
                self.assertFalse(tpl_dot.startswith("error:"), tpl_dot[:200])
                self.assertIn("Product", tpl_dot)

                trav = dispatch("read_file", {"path": "skills/../companion/vault.py"})
                self.assertTrue(trav.startswith("error: file not found:") or "SAFETY GUARD REJECTED" in trav, trav)
                self.assertNotIn("VaultManager", trav)
                trav_escape = dispatch("read_file", {"path": "../../../../outside_secret.txt"})
                self.assertIn("SAFETY GUARD REJECTED", trav_escape)

                skill_doc = dispatch("read_file", {"path": "skills/sdlc-product/SKILL.md"})
                self.assertIn("name: sdlc-product", skill_doc)

                w_ok = dispatch("write_file", {"path": "product.md", "content": "# Ext Service Product\n\nClean spec.\n"})
                self.assertIn("wrote", w_ok.lower())
                self.assertTrue((ext_repo / "product.md").is_file())
                self.assertFalse((settings.root / "product.md").exists())

                # Writing to skills/ inside external repo must NOT touch Companion's app/skills/
                with patch.object(settings, "immutable_app", True):
                    w_app = dispatch("write_file", {"path": str(settings.root / "skills" / "sdlc-product" / "SKILL.md"), "content": "hack"})
                    self.assertIn("SAFETY GUARD REJECTED", w_app)

    def test_all_eight_sdlc_skills_and_sdlc_audit_workflow(self) -> None:
        """Verify all 8 sdlc-* skills validate cleanly, sdlc-engineering is retired, and sdlc-audit catches placeholders."""
        expected_skills = {
            "sdlc-product", "sdlc-architecture", "sdlc-backlog", "sdlc-design",
            "sdlc-implement", "sdlc-verify", "sdlc-operate", "sdlc-change",
        }
        discovered = {s["name"] for s in discover_skills(root=settings.root)}
        self.assertTrue(expected_skills.issubset(discovered))
        self.assertNotIn("sdlc-engineering", discovered)
        self.assertFalse((settings.root / "skills" / "sdlc-engineering").exists())

        exposed_tools = {t["function"]["name"] for t in TOOLS}
        disc_map = {s["name"]: s for s in discover_skills(root=settings.root)}
        for sname in sorted(expected_skills):
            md_path = settings.root / "skills" / sname / "SKILL.md"
            md_txt = md_path.read_text(encoding="utf-8")
            self.assertIsNone(validate_skill_markdown(md_txt, expected_name=sname))
            self.assertNotIn("(./references/", md_txt, f"{sname} still has unadapted relative ./references/ link")
            req_raw = str(disc_map[sname].get("required_tools") or "").strip("[] ")
            req_list = [tok.strip() for tok in req_raw.split(",") if tok.strip()]
            for rt in req_list:
                self.assertIn(rt, exposed_tools, f"{sname} required_tools `{rt}` is missing from live TOOLS schema")
            self.assertNotIn("workflow_manage(", md_txt, f"{sname} references legacy non-TOOLS `workflow_manage`")

        dags = {d["name"] for d in discover_dags(root=settings.root)}
        self.assertIn("sdlc-audit", dags)
        self.assertIn("pre-commit-verify", dags)

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve() / "audit_target"
            repo.mkdir()
            (repo / "product.md").write_text("# Spec\n\n[Product / System Name]\nTODO: finish scope\n", encoding="utf-8")
            with contextlib.closing(MemoryStore(db_path=Path(tmp) / "audit.db")) as store:
                bad_run = execute_dag_by_name("sdlc-audit", params={"repo_root": str(repo)}, store=store)
                self.assertFalse(bad_run["ok"], bad_run)

                (repo / "product.md").write_text("# Spec\n\nConcrete finalized product scope.\n", encoding="utf-8")
                good_run = execute_dag_by_name("sdlc-audit", params={"repo_root": str(repo)}, store=store)
                self.assertTrue(good_run["ok"], good_run)

    def test_deterministic_sdlc_stage_progression_choice_a_choice_b_and_brownfield(self) -> None:
        """Verify /sdlc-next deterministically detects Stages 1..7, Choice B Pending vs SHIPPED increments, and Brownfield."""
        # 1. Greenfield Choice A full 7-stage progression
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve() / "greenfield_a"
            init_greenfield_repo(repo, title="Greenfield A")
            self.assertEqual(inspect_sdlc_repo_state(repo)["recommended_skill"], "sdlc-product")

            (repo / "product.md").write_text("# Product\n\nFinalized product spec.\n", encoding="utf-8")
            self.assertEqual(inspect_sdlc_repo_state(repo)["recommended_skill"], "sdlc-architecture")

            (repo / "architecture.md").write_text("# Architecture\n\nPython 3.12 service.\n", encoding="utf-8")
            (repo / "docs" / "adr").mkdir(parents=True)
            (repo / "docs" / "adr" / "ADR-001-stack.md").write_text("# ADR-001\n\nAccepted.\n", encoding="utf-8")
            self.assertEqual(inspect_sdlc_repo_state(repo)["recommended_skill"], "sdlc-backlog")

            (repo / "docs" / "features.md").write_text("# Features\n\n- F01 Reconcile\n", encoding="utf-8")
            (repo / "docs" / "user-stories.md").write_text("# Stories\n\nGiven X When Y Then Z.\n", encoding="utf-8")
            self.assertEqual(inspect_sdlc_repo_state(repo)["recommended_skill"], "sdlc-design")

            (repo / "docs" / "engineering-design.md").write_text(
                "# Engineering Design\n\n## 1. Project & Module File Layout\n- `src/reconcile.py`\n\n## 2. Contracts\nLocked contract details for reconciliation.\n",
                encoding="utf-8",
            )
            self.assertEqual(inspect_sdlc_repo_state(repo)["recommended_skill"], "sdlc-implement")

            (repo / "src").mkdir()
            (repo / "src" / "reconcile.py").write_text("def reconcile() -> bool:\n    return True\n", encoding="utf-8")
            self.assertEqual(inspect_sdlc_repo_state(repo)["recommended_skill"], "sdlc-verify")

            (repo / "docs" / "testing-strategy.md").write_text("# Testing Strategy\n\n100% unit + integration coverage.\n", encoding="utf-8")
            (repo / "tests").mkdir()
            (repo / "tests" / "test_reconcile.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
            self.assertEqual(inspect_sdlc_repo_state(repo)["recommended_skill"], "sdlc-operate")

            (repo / "docs" / "operations-runbook.md").write_text("# Operations Runbook\n\n2 AM recovery steps.\n", encoding="utf-8")
            done_state = inspect_sdlc_repo_state(repo)
            self.assertTrue(done_state["lifecycle_complete"])
            self.assertEqual(done_state["recommended_skill"], "sdlc-change")

        # 2. Choice B Increment-by-Increment loop (`Pending` -> locked -> implemented -> `SHIPPED` -> `inc-02`)
        with tempfile.TemporaryDirectory() as tmp:
            repo_b = Path(tmp).resolve() / "greenfield_b"
            init_greenfield_repo(repo_b, title="Greenfield B")
            (repo_b / "product.md").write_text("# Product\n\nSpec.\n", encoding="utf-8")
            (repo_b / "architecture.md").write_text("# Architecture\n\nSpec.\n", encoding="utf-8")
            (repo_b / "adr.md").write_text("# ADRs\n\nLocked.\n", encoding="utf-8")
            fdir = repo_b / "docs" / "features" / "F01-core"
            fdir.mkdir(parents=True)
            (fdir / "00-feature-overview.md").write_text("# Feature F01\n\n Increments 01-02.\n", encoding="utf-8")
            (fdir / "inc-01-parse.md").write_text("# Inc 01\n\nStories.\n\n## Stage 4: Engineering Design (Pending)\n", encoding="utf-8")
            (fdir / "inc-02-export.md").write_text("# Inc 02\n\nStories.\n\n## Stage 4: Engineering Design (Pending)\n", encoding="utf-8")

            s_inc1_des = inspect_sdlc_repo_state(repo_b)
            self.assertEqual(s_inc1_des["recommended_skill"], "sdlc-design")
            self.assertEqual(s_inc1_des["active_increment"], "docs/features/F01-core/inc-01-parse.md")
            self.assertTrue(s_inc1_des["hygiene_clean"])  # `(Pending)` on unshipped Stage 4 is expected, not a violation!

            (fdir / "inc-01-parse.md").write_text(
                "# Inc 01\n\nStories.\n\n## Stage 4: Engineering Design\n### 1. Project & Module File Layout\n- `src/parser.py`\nLocked parser contracts and error matrix.\n",
                encoding="utf-8",
            )
            self.assertEqual(inspect_sdlc_repo_state(repo_b)["recommended_skill"], "sdlc-implement")

            (repo_b / "src").mkdir()
            (repo_b / "src" / "parser.py").write_text("def parse(x: str) -> str:\n    return x.strip()\n", encoding="utf-8")
            self.assertEqual(inspect_sdlc_repo_state(repo_b)["recommended_skill"], "sdlc-verify")

            # Mark inc-01 SHIPPED -> automatically advances to Stage 4 on inc-02!
            (fdir / "inc-01-parse.md").write_text(
                "# Inc 01\nSTATUS: SHIPPED\n\n## Stage 4: Engineering Design\nLocked.\n",
                encoding="utf-8",
            )
            s_inc2 = inspect_sdlc_repo_state(repo_b)
            self.assertEqual(s_inc2["recommended_skill"], "sdlc-design")
            self.assertEqual(s_inc2["active_increment"], "docs/features/F01-core/inc-02-export.md")

            # Lock inc-02 using level-2 `## 1.` template headings + ASCII directory tree + flat root `cli.py`:
            # Must NOT get stuck in Stage 4 (`design_pending=False`) and MUST route to Stage 5 (`sdlc-implement`)!
            (fdir / "inc-02-export.md").write_text(
                "# Inc 02\n\n## Stage 4: Engineering Design\n\n## 1. Project & Module File Layout\n\n"
                "```text\ngreenfield-b/\n├── cli.py              # Flat root CLI entrypoint\n├── src/\n"
                "│   ├── parser.py       # Existing parser\n│   └── exporter.py     # New CSV exporter\n"
                "└── tests/\n    └── test_exporter.py\n```\n\n## 2. Entry Surface & Input Contracts\n"
                "Complete typed CSV export contract and idempotency rules.\n",
                encoding="utf-8",
            )
            s_inc2_impl = inspect_sdlc_repo_state(repo_b)
            self.assertFalse(s_inc2_impl["increments"][1]["design_pending"], s_inc2_impl["increments"][1])
            self.assertEqual(s_inc2_impl["recommended_skill"], "sdlc-implement")
            self.assertIn("cli.py", s_inc2_impl["increments"][1]["missing_sources"])
            self.assertIn("src/exporter.py", s_inc2_impl["increments"][1]["missing_sources"])

            # Create only `src/exporter.py` -> still Stage 5 because flat root `cli.py` is still missing!
            (repo_b / "src" / "exporter.py").write_text("def export_csv() -> str:\n    return 'ok'\n", encoding="utf-8")
            self.assertEqual(inspect_sdlc_repo_state(repo_b)["recommended_skill"], "sdlc-implement")

            # Create `cli.py` -> all inc-02 planned sources exist -> advances to Stage 6 (`sdlc-verify`)!
            (repo_b / "cli.py").write_text("from src.exporter import export_csv\n", encoding="utf-8")
            self.assertEqual(inspect_sdlc_repo_state(repo_b)["recommended_skill"], "sdlc-verify")

        # 3. Brownfield repository (`AGENTS.md` + existing code, no `product.md`) skips Stage 1
        with tempfile.TemporaryDirectory() as tmp:
            brown = Path(tmp).resolve() / "existing_app"
            init_greenfield_repo(brown, title="Existing App")
            (brown / "AGENTS.md").write_text("# Host Architecture\n\nExisting FastAPI app.\n", encoding="utf-8")
            (brown / "src").mkdir()
            (brown / "src" / "main.py").write_text("app = object()\n", encoding="utf-8")
            s_brown = inspect_sdlc_repo_state(brown)
            self.assertEqual(s_brown["repo_kind"], "brownfield")
            self.assertEqual(s_brown["recommended_skill"], "sdlc-architecture")

    def test_sdlc_slash_router_interactive_codesign_never_hijacks_into_workflow_engine(self) -> None:
        """Verify /sdlc-* stays in interactive ReAct chat (never auto-promoting to WorkflowEngine) and streams via SSE 202."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp).resolve()
            repo = tmp_p / "codesign_repo"
            init_greenfield_repo(repo, title="Codesign Repo")
            db_file = tmp_p / "codesign.db"

            # Verify mode parsing (`Guided Co-Design` default vs `Express`)
            r_guided = resolve_sdlc_slash_turn("/sdlc-next Invoice CLI", repo)
            self.assertIsNotNone(r_guided)
            self.assertEqual(r_guided["mode"], "Guided Co-Design")
            self.assertEqual(r_guided["skill"], "sdlc-product")
            self.assertIn("name: sdlc-product", r_guided["prompt_block"])

            r_express = resolve_sdlc_slash_turn("/sdlc-next express Invoice CLI", repo)
            self.assertIsNotNone(r_express)
            self.assertEqual(r_express["mode"], "Express")

            # Verify all 9 slash verbs in SDLC_SLASH_MAP resolve cleanly
            for verb in SDLC_SLASH_MAP:
                self.assertIsNotNone(resolve_sdlc_slash_turn(f"/{verb} sample note", repo), verb)
                self.assertIsNone(handle_slash_command(verb, []))

            with contextlib.closing(MemoryStore(db_path=db_file)) as store, memory_store_context(store):
                pg = get_project_graph(store=store)
                proj = pg.create_project("Codesign Repo", kind="software", workspace_path=str(repo), focus=True)

                # Software project gets <repo_sdlc_context>; Personal Goal Board does NOT
                ctx_code = build_turn_context(store, user_query="/sdlc-next Build an invoice parser")
                self.assertIn("<repo_sdlc_context>", ctx_code)
                self.assertIn("[SDLC ROUTER DIRECTIVE", ctx_code)

                personal = pg.create_project("Fitness Tracker", kind="goal")
                pg.set_focus_project(personal["project_id"])
                self.assertEqual(discover_repo_sdlc_context(settings.root, project=personal), "")

                pg.set_focus_project(proj["project_id"])

                # Run interactive turn 1 (`/sdlc-next`) + turn 2 follow-up with `auto_promote=True`:
                # Neither turn may hijack into legacy WorkflowEngine Blueprint PLAN mode!
                def _fake_llm(messages: List[Dict[str, Any]], _tools=None, _choice="auto") -> Dict[str, Any]:
                    u_txt = str(messages[-1].get("content", ""))
                    if "/sdlc-next" in u_txt:
                        return {
                            "choices": [{
                                "message": {
                                    "role": "assistant",
                                    "content": "**Stage 1 (`sdlc-product`) — Guided Co-Design**\n\n### Stop 1 — Problem, Vision & Stakeholders\nProposed personas...",
                                }
                            }]
                        }
                    return {
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "### Stop 2 — Scope Boundaries & Happy Path\nIn-Scope vs Out-of-Scope...",
                            }
                        }]
                    }

                conv: List[Dict[str, Any]] = [{"role": "user", "content": "/sdlc-next Build a multi-tenant billing reconciliation platform"}]
                out1 = run_agent_turn(conv, chat_fn=_fake_llm, store=store, verbose=False, auto_promote=True)
                self.assertIn("Stop 1 —", out1)
                self.assertFalse(is_awaiting_blueprint_approval(store))
                self.assertIsNone(store.get_active_workflow())

                # Turn 2 contains ambiguous scoping words ("not sure how", "align with me first") that would normally trigger auto-Blueprint,
                # but because an active Guided Co-Design session is underway, it MUST stay in interactive ReAct chat!
                conv.append({"role": "user", "content": "Looks good, though I am not sure how to scope multi-currency—align with me first on Stop 2"})
                out2 = run_agent_turn(conv, chat_fn=_fake_llm, store=store, verbose=False, auto_promote=True)
                self.assertIn("Stop 2 —", out2)
                self.assertFalse(is_awaiting_blueprint_approval(store))
                self.assertIsNone(store.get_active_workflow())

                # Multi-turn (>8 back-and-forth turns, colon-formatted `### Stop 3: ...`) stays in active co-design,
                # then IMMEDIATELY exits co-design once `### Checkpoint 1: ...` completes or user switches to Personal Board!
                long_hist: List[Dict[str, Any]] = [{"role": "user", "content": "/sdlc-4-design"}]
                for turn_idx in range(1, 9):
                    long_hist.append({"role": "assistant", "content": f"### Stop {(turn_idx % 4) + 1}: Layer Contracts (turn {turn_idx})"})
                    long_hist.append({"role": "user", "content": f"Clarification {turn_idx} looks solid, next"})
                self.assertTrue(_is_active_sdlc_codesign_turn("Not sure about retry backoff—align with me first", long_hist, is_software_ws=True))
                self.assertFalse(_is_active_sdlc_codesign_turn("What is on my schedule?", long_hist, is_software_ws=False))
                long_hist.append({"role": "assistant", "content": "### Checkpoint 4: Engineering Design & Hardening Pass Complete\n\n1. Artifact Written: `docs/engineering-design.md`"})
                self.assertFalse(_is_active_sdlc_codesign_turn("What time is my meeting?", long_hist, is_software_ws=True))

                # Verify Web SSE `/api/chat` (`async: true`) accepts `/sdlc-next` with HTTP 202 (streaming job)
                srv, sess = create_web_server(port=0, store=store, chat_fn=_fake_llm)
                try:
                    code, raw_body = dispatch_raw_http(
                        srv,
                        method="POST",
                        path="/api/chat",
                        body=json.dumps({"message": "/sdlc-next", "async": True}).encode("utf-8"),
                        headers={"X-Companion-CSRF": sess.csrf_token},
                    )
                    body = json.loads(raw_body)
                    self.assertEqual(code, 202, body)
                    job_id = str(body.get("job_id") or "")
                    self.assertTrue(job_id)
                    cursor = 0
                    for _ in range(25):
                        job_snap, _evs, cursor = sess.wait_job_events(job_id, cursor, timeout=0.2)
                        if job_snap and job_snap.get("status") != "running":
                            break
                    self.assertEqual((job_snap or {}).get("status"), "completed")
                finally:
                    srv.server_close()


if __name__ == "__main__":
    unittest.main()
