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
from companion.approvals import ActionRiskTier, classify_action_risk
from companion.config import Settings, settings, workspace_context
from companion.context import build_system_prompt, build_turn_context, discover_skills, manage_active_project
from companion.core_actions import execute_core_action
from companion.memory_store import MemoryStore, memory_store_context
from companion.projects import get_project_graph
from companion.tools import TOOLS, check_phase_tool_permission, dispatch, phase_tool_context
from companion.workflows import discover_dags, execute_dag_by_name, validate_skill_markdown
from companion.worktrees import (
    SDLC_SLASH_MAP,
    _fast_git_branch,
    browse_projects_dir,
    create_projects_subfolder,
    discover_repo_sdlc_context,
    get_projects_root,
    get_projects_root_info,
    init_greenfield_repo,
    inspect_sdlc_repo_state,
    probe_repo_workspace,
    resolve_sdlc_slash_turn,
    set_projects_root,
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
                for turn_idx in range(1, 12):
                    long_hist.append({"role": "assistant", "content": f"**Stop {(turn_idx % 4) + 1} of 4: Layer Contracts (turn {turn_idx})**"})
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


class TestProjectsRootExplorerAndParity(unittest.TestCase):
    """Adversarial verification for F01/inc-02: Pinned Projects Root, Confined Explorer & Companion Tool/Slash Parity."""

    def tearDown(self) -> None:
        settings.reset_workspace()

    def test_projects_root_persistence_fallback_and_adversarial_system_path_guards(self) -> None:
        """Verify user_profile['projects_root'] precedence, mkdir -p, fallback, and mechanical system/root blocks."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp).resolve()
            env_projects = tmp_p / "env_projects"
            env_projects.mkdir()
            pinned_projects = tmp_p / "pinned_workspace" / "repos"
            db_file = tmp_p / "root_test.db"

            with (
                patch.dict(os.environ, {"COMPANION_PROJECTS_DIR": str(env_projects)}, clear=False),
                contextlib.closing(MemoryStore(db_path=db_file)) as store,
                memory_store_context(store),
            ):
                # 1. Default falls back to COMPANION_PROJECTS_DIR when user_profile['projects_root'] is unset
                info0 = get_projects_root_info(store)
                self.assertEqual(info0["projects_root"], env_projects)
                self.assertFalse(info0["is_custom_root"])
                self.assertEqual(settings.projects_dir, env_projects)

                # 2. Mechanically reject `/`, `$HOME`, `/root`, `gettempdir()`, `/etc`, `/usr`, `/var`, `app/`, `data/`, `.ssh`, and Windows system dirs
                blocked_targets = [
                    "/",
                    str(Path.home().resolve()),
                    "/root",
                    "/root/private",
                    str(Path(tempfile.gettempdir()).resolve()),
                    "/etc",
                    "/usr/local",
                    "/var/log",
                    str(settings.root.resolve()),
                    str(settings.root.resolve() / "sub"),
                    str(settings.effective_data_dir.resolve()),
                    str(tmp_p / ".ssh" / "repos"),
                ]
                if os.name == "nt":
                    blocked_targets.extend(["C:\\Windows", "C:\\Windows\\System32", "C:\\Program Files"])
                for bad in blocked_targets:
                    r_bad = set_projects_root(bad, store=store)
                    self.assertFalse(r_bad["ok"], f"Expected rejection for {bad}: {r_bad}")
                    self.assertIn("SAFETY GUARD REJECTED", r_bad["error"])

                # Also block `memory_manage(action='set_profile', key='projects_root', value='/')` bypass!
                prof_bypass = dispatch("memory_manage", {"action": "set_profile", "key": "projects_root", "value": "/"})
                self.assertIn("SAFETY GUARD REJECTED", prof_bypass)

                # 3. Pinning a valid non-existent path creates it (`mkdir -p`) and overrides COMPANION_PROJECTS_DIR
                self.assertFalse(pinned_projects.exists())
                r_pin = set_projects_root(str(pinned_projects), store=store)
                self.assertTrue(r_pin["ok"], r_pin)
                self.assertTrue(pinned_projects.is_dir())
                self.assertEqual(get_projects_root(store), pinned_projects)
                self.assertEqual(settings.projects_dir, pinned_projects)
                self.assertTrue(get_projects_root_info(store)["is_custom_root"])

                # Surfaced in [RUNTIME & GOVERNANCE] system prompt & GET /api/status
                sys_prompt = build_system_prompt(store=store)
                self.assertIn(f"projects_root=`{pinned_projects}`", sys_prompt)
                srv, sess = create_web_server(port=0, store=store)
                try:
                    code, raw_st = dispatch_raw_http(srv, method="GET", path="/api/status")
                    self.assertEqual(code, 200)
                    st_json = json.loads(raw_st)
                    self.assertEqual(Path(st_json["projects_root"]).resolve(), pinned_projects)
                    self.assertTrue(st_json["projects_root_is_custom"])
                    self.assertEqual(Path(st_json["workspace"]["projects_root"]).resolve(), pinned_projects)
                finally:
                    srv.server_close()

                # 4. Corrupt DB value fails closed back to env/default without crashing
                store.set_user_profile("projects_root", str(settings.root.resolve()))
                self.assertEqual(get_projects_root(store), env_projects)
                self.assertFalse(get_projects_root_info(store)["is_custom_root"])

                # 5. Resetting restores fallback cleanly
                r_reset = set_projects_root("", reset=True, store=store)
                self.assertTrue(r_reset["ok"], r_reset)
                self.assertFalse(r_reset["is_custom_root"])
                self.assertEqual(get_projects_root(store), env_projects)

    def test_confined_explorer_metadata_noise_filter_subfolders_and_escape_blocks(self) -> None:
        """Verify browse_projects_dir metadata, noise filtering, nested subfolders, and ../ + symlink confinement."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp).resolve()
            proot = tmp_p / "spark_projects"
            proot.mkdir()
            outside_secret = tmp_p / "outside_secret"
            outside_secret.mkdir()
            db_file = tmp_p / "explorer.db"

            # Noise folders that MUST be hidden
            for noise in (".git", ".worktrees", "node_modules", "__pycache__", ".venv", ".hidden_cache"):
                (proot / noise).mkdir()
            (proot / "bundle_file.bundle").write_text("binary", encoding="utf-8")

            with contextlib.closing(MemoryStore(db_path=db_file)) as store, memory_store_context(store):
                set_projects_root(str(proot), store=store)

                # Create organizational subfolder `experiments/agents` (plain folder, NO .git)
                mk_res = create_projects_subfolder(rel_path="/experiments/agents/", store=store)
                self.assertTrue(mk_res["ok"], mk_res)
                self.assertTrue((proot / "experiments" / "agents").is_dir())
                self.assertFalse((proot / "experiments" / ".git").exists())
                self.assertEqual(mk_res["current_rel_path"], "experiments/agents")
                self.assertTrue(mk_res["can_go_up"])
                self.assertEqual(mk_res["parent_rel_path"], "experiments")

                # Block traversal & hidden/reserved subfolder creation
                for bad_mk in ("../escape", "experiments/../../outside", ".git", ".ssh", "node_modules"):
                    bad_res = create_projects_subfolder(rel_path=bad_mk, store=store)
                    self.assertFalse(bad_res["ok"], f"Expected rejection for {bad_mk}")
                    self.assertIn("SAFETY GUARD REJECTED", bad_res["error"])

                # Create linked Git repo at root + nested Git repo under `experiments/my-app`
                res_linked = execute_core_action(
                    "agile",
                    {"subaction": "create_project", "title": "Core Engine", "kind": "software", "init_git": True},
                    actor="web",
                    store=store,
                )
                self.assertTrue(res_linked["ok"], res_linked)
                linked_pid = res_linked["project"]["project_id"]

                init_nested = init_greenfield_repo("experiments/my-app", title="My Nested App")
                self.assertTrue(init_nested["ok"], init_nested)
                self.assertEqual(Path(init_nested["repo_root"]).resolve(), (proot / "experiments" / "my-app").resolve())

                # Optional symlink escape test (if OS privileges allow creating directory symlinks)
                with contextlib.suppress(OSError, NotImplementedError):
                    os.symlink(outside_secret, proot / "escaped_link", target_is_directory=True)

                root_browse = browse_projects_dir("", store=store)
                self.assertTrue(root_browse["ok"], root_browse)
                self.assertFalse(root_browse["can_go_up"])
                by_name = {e["name"]: e for e in root_browse["entries"]}
                self.assertEqual(set(by_name.keys()), {"core-engine", "experiments"})

                self.assertTrue(by_name["core-engine"]["is_git_repo"])
                self.assertEqual(by_name["core-engine"]["branch"], "main")
                self.assertEqual(by_name["core-engine"]["linked_project_id"], linked_pid)
                self.assertEqual(by_name["core-engine"]["linked_project_title"], "Core Engine")

                self.assertFalse(by_name["experiments"]["is_git_repo"])
                self.assertEqual(by_name["experiments"]["branch"], "")
                self.assertTrue(by_name["experiments"]["has_children"])

                sub_browse = browse_projects_dir("experiments", store=store)
                self.assertTrue(sub_browse["ok"], sub_browse)
                self.assertTrue(sub_browse["can_go_up"])
                self.assertEqual(sub_browse["parent_rel_path"], "")
                sub_names = {e["name"]: e for e in sub_browse["entries"]}
                self.assertEqual(set(sub_names.keys()), {"agents", "my-app"})
                self.assertFalse(sub_names["agents"]["is_git_repo"])
                self.assertTrue(sub_names["my-app"]["is_git_repo"])
                self.assertEqual(sub_names["my-app"]["branch"], "main")

                # Escape attempts via `..`, leading `/`, or symlink must be rejected
                for esc in ("..", "../outside_secret", "experiments/../../outside_secret", "/etc"):
                    esc_res = browse_projects_dir(esc, store=store)
                    self.assertFalse(esc_res["ok"], f"Expected rejection for {esc}")
                    self.assertIn("SAFETY GUARD REJECTED", esc_res["error"])
                if (proot / "escaped_link").is_symlink():
                    sym_res = browse_projects_dir("escaped_link", store=store)
                    self.assertFalse(sym_res["ok"])
                    self.assertIn("SAFETY GUARD REJECTED", sym_res["error"])

    def test_companion_ops_manage_and_slash_parity_while_focused_on_companion_self(self) -> None:
        """Verify ops_manage(domain='projects'), agile_manage, and /project slash parity from companion-self."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp).resolve()
            proot = tmp_p / "companion_parity_root"
            db_file = tmp_p / "parity.db"

            with contextlib.closing(MemoryStore(db_path=db_file)) as store, memory_store_context(store):
                settings.reset_workspace()
                self.assertTrue(settings.get_workspace().is_self_project)

                # While focused on companion-self, direct list_dir outside app/ is blocked by check_path_safety,
                # whereas ops_manage(domain='projects', action='set_root'|'mkdir'|'browse') works natively!
                out_root = dispatch("ops_manage", {"domain": "projects", "action": "set_root", "target": str(proot)})
                self.assertIn("Pinned Projects Root", out_root)
                self.assertTrue(proot.is_dir())

                out_mkdir = dispatch("ops_manage", {"domain": "projects", "action": "mkdir", "target": "products/billing"})
                self.assertIn("Created subfolder `products/billing`", out_mkdir)

                # Create mixed-case repo `Agent-OS` to verify exact-case switch resolution
                init_greenfield_repo(proot / "Agent-OS", title="Agent OS")

                # Browse works in read-only DISCOVER phase, while set_root/mkdir are phase-gated in DISCOVER
                with phase_tool_context("DISCOVER"):
                    out_browse_ro = dispatch("ops_manage", {"domain": "projects", "action": "browse"})
                    self.assertIn("Agent-OS", out_browse_ro)
                    self.assertIn("products", out_browse_ro)
                    out_locked = dispatch("ops_manage", {"domain": "projects", "action": "mkdir", "target": "blocked_in_discover"})
                    self.assertIn("PHASE TOOL GATE REJECTED", out_locked)

                # agile_manage browse_projects alias
                ag_browse = dispatch("agile_manage", {"action": "browse_projects", "rel_path": "products"})
                self.assertIn("products/billing/", ag_browse)

                # /project slash parity (`root`, `browse`, `mkdir`, exact-case `switch`, `self`)
                msgs: List[Dict[str, Any]] = [{"role": "system", "content": build_system_prompt(store=store)}]
                sl_root = handle_slash_command("/project root", msgs, store=store, interactive=False) or ""
                self.assertIn(str(proot), sl_root)
                sl_browse = handle_slash_command("/project browse", msgs, store=store, interactive=False) or ""
                self.assertIn("Agent-OS", sl_browse)
                sl_sw = handle_slash_command("/project switch Agent-OS", msgs, store=store, interactive=False) or ""
                self.assertIn("Switched active WorkspaceContext", sl_sw)
                self.assertEqual(settings.get_workspace().project_root.resolve(), (proot / "Agent-OS").resolve())
                handle_slash_command("/project self", msgs, store=store, interactive=False)
                self.assertTrue(settings.get_workspace().is_self_project)

                # R0/R3 ops_manage risk classification & omitted-action phase gate
                self.assertEqual(classify_action_risk("ops_manage", {"domain": "projects", "action": "browse"}), ActionRiskTier.R0_READ_ONLY)
                self.assertEqual(classify_action_risk("ops_manage", {"domain": "endpoints", "action": "ssh_exec"}), ActionRiskTier.R3_HIGH_IMPACT_MUTATION)
                self.assertIn(
                    "PHASE TOOL GATE REJECTED",
                    check_phase_tool_permission("ops_manage", phase="DISCOVER", args={"domain": "projects", "target": "Agent-OS"}) or "",
                )

                # Layer-1 vs Layer-3 guard: `projects_root` itself, plain files, and relative `..` escapes cannot be `git init`ed
                r_self_root = init_greenfield_repo(proot, title="Root Clobber", store=store)
                self.assertFalse(r_self_root["ok"], r_self_root)
                self.assertFalse((proot / ".git").exists())
                r_rel_esc = init_greenfield_repo("../escaped_outside", title="Escape", store=store)
                self.assertFalse(r_rel_esc["ok"], r_rel_esc)
                self.assertIn("SAFETY GUARD REJECTED", r_rel_esc["error"])
                plain_f = proot / "not_a_dir.txt"
                plain_f.write_text("file", encoding="utf-8")
                r_file = init_greenfield_repo(plain_f, store=store)
                self.assertFalse(r_file["ok"], r_file)
                self.assertIn("SAFETY GUARD REJECTED", r_file["error"])

                # Nested subfolder repo name collision (`experiments/my-app` vs `products/my-app`) preserves both project records!
                dispatch("ops_manage", {"domain": "projects", "action": "init", "target": "experiments/my-app"})
                dispatch("ops_manage", {"domain": "projects", "action": "init", "target": "products/my-app"})
                bound_paths = {
                    Path(str(p["workspace_path"])).resolve()
                    for p in get_project_graph(store).list_projects()
                    if str(p.get("workspace_path") or "").strip()
                }
                self.assertIn((proot / "experiments" / "my-app").resolve(), bound_paths)
                self.assertIn((proot / "products" / "my-app").resolve(), bound_paths)

                # `.git` gitdir worktree/submodule symbolic refs parse cleanly without truncation
                wt_dir, gd_dir = tmp_p / "wt_sub", tmp_p / "gd_meta"
                wt_dir.mkdir()
                gd_dir.mkdir()
                (wt_dir / ".git").write_text(f"gitdir: {gd_dir}\n", encoding="utf-8")
                (gd_dir / "HEAD").write_text("ref: refs/remotes/origin/release\n", encoding="utf-8")
                self.assertEqual(_fast_git_branch(wt_dir), "release")

            # Store-scoped `probe_repo` honors pinned `projects_root` even outside `memory_store_context`
            with contextlib.closing(MemoryStore(db_path=db_file)) as plain_store:
                probe_rel = execute_core_action("agile", {"subaction": "probe_repo", "workspace_path": "products/my-app"}, actor="web", store=plain_store)
                self.assertTrue(probe_rel["ok"], probe_rel)
                self.assertTrue(probe_rel["is_git_repo"])
                self.assertEqual(Path(probe_rel["projects_root"]).resolve(), proot.resolve())


if __name__ == "__main__":
    unittest.main()
