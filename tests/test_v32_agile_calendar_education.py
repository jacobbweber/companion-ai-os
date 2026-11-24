"""
Comprehensive Unit & Integration Tests for Universal Headless Project & Goal Graph,
Unified Multi-Source Calendar, Headless Education Engine, and `personal-agile-coach`:
1. Universal Project & Goal Graph (`projects`, `milestones`, `project_timeboxes`, `work_items`), Dependencies & `owner` Readiness
2. Live Software Engineering Workflow -> Project Graph Sync
3. Unified Multi-Source Calendar (Events + Dated Items + Deadlines + Milestones + Routines + Due SRS Reviews)
4. Headless Education, Socratic Syllabus Progression, SM-2 Mastery, Wiki Study Notes & Inline Chat Flashcards
5. CoreActions, Slash Commands, Phase Gates, HUD & Web Status Parity
"""

import json
import os
import tempfile
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path

from companion.calendar_engine import get_calendar_manager
from companion.config import settings, workspace_context
from companion.context import build_system_prompt
from companion.core_actions import execute_core_action, handle_harvest_slash
from companion.education_engine import get_education_engine
from companion.context import discover_skills, rank_relevant_skills
from companion.tools import tool_agile_manage, tool_education_manage
from companion.agent import classify_turn_intent
from companion.memory_store import MemoryStore, memory_store_context
from companion.project_graph import get_project_graph, sync_workflow_to_project_graph
from companion.routines import get_routine_manager
from companion.workflows import run_skill_script
from companion.tools import check_phase_tool_permission, filter_tools_for_phase
from companion.tools import tool_search, tool_web_search
from companion.wiki import list_templates
from surfaces.api_extensions import enrich_web_status


class TestCompanionProjectGraphCalendarEducation(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.store = MemoryStore(db_path=self.tmp_path / "test_v32.db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_project_graph_milestones_timeboxes_dependencies_and_coach_helper(self) -> None:
        mgr = get_project_graph(store=self.store)
        proj = mgr.create_project(title="Basement Renovation", goal_summary="Clean & renovate basement", kind="goal")
        pid = proj["project_id"]
        ms = mgr.create_milestone(pid, title="Phase 1 Prep", stage_kind="OBSERVE_DATA", target_date="2026-12-20")
        tb = mgr.create_timebox(pid, title="Week 46 Focus", goal="Prep truck + gutters", start_date="2026-11-09", end_date="2026-11-15")
        mgr.update_timebox_status(tb["timebox_id"], "active")

        unblocker = mgr.create_item(
            pid, "Call mechanic & book Tuesday morning drop-off (15m)",
            owner="human", estimate_minutes=15, milestone_id=ms["milestone_id"], timebox_id=tb["timebox_id"],
        )
        parent = mgr.create_item(
            pid, "Take truck into shop to get fixed",
            owner="human", estimate_minutes=180, depends_on=[unblocker["item_id"]],
            milestone_id=ms["milestone_id"], timebox_id=tb["timebox_id"],
            checklist=["[x] Locate service records", "[ ] Drop off keys"],
        )
        self.assertEqual(parent["status"], "blocked")
        self.assertEqual(len(parent["checklist"]), 2)

        mgr.update_item(unblocker["item_id"], status="done")
        refreshed = mgr.get_item(parent["item_id"])
        self.assertEqual(refreshed["status"], "ready")

        rmap = mgr.get_roadmap_projection(pid)
        self.assertEqual(rmap["milestones"][0]["progress_pct"], 50.0)

        coach_out = run_skill_script("personal-agile-coach", "coach_helper.py", ["--self-test"])
        self.assertIn('"ok": true', coach_out.lower())

    def test_workflow_sync_to_project_graph(self) -> None:
        wf_id = self.store.create_workflow_run("engineering_feature", "Build telemetry exporter", "PLAN")
        sync_workflow_to_project_graph(self.store, wf_id, "Build telemetry exporter", "PLAN")
        mgr = get_project_graph(store=self.store)
        view = mgr.get_board_projection()
        self.assertTrue(any("[WF #" in c["title"] and c["column_key"] == "ready" for c in view["cards"]))

        sync_workflow_to_project_graph(self.store, wf_id, "Build telemetry exporter", "IMPLEMENT")
        view = mgr.get_board_projection()
        self.assertTrue(any("[WF #" in c["title"] and c["column_key"] == "in_progress" for c in view["cards"]))

        sync_workflow_to_project_graph(self.store, wf_id, "Build telemetry exporter", "VERIFY")
        view = mgr.get_board_projection()
        self.assertTrue(any("[WF #" in c["title"] and c["column_key"] == "verify" for c in view["cards"]))

        sync_workflow_to_project_graph(self.store, wf_id, "Build telemetry exporter", "COMPLETE")
        view = mgr.get_board_projection()
        self.assertTrue(any("[WF #" in c["title"] and c["column_key"] == "done" for c in view["cards"]))

    def test_unified_calendar_aggregates_events_items_milestones_routines_and_srs(self) -> None:
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        cal = get_calendar_manager(store=self.store)
        pg = get_project_graph(store=self.store)
        rm = get_routine_manager(store=self.store)
        edu = get_education_engine(store=self.store)

        ev = cal.upsert_event(title="Dentist Appointment", start_at=f"{today}T14:30", kind="appointment")
        self.assertTrue(ev["ok"])
        pid = pg.resolve_project_id()
        pg.create_milestone(pid, title="Today Milestone", target_date=today)
        pg.create_item(pid, title="Clean Gutters", owner="human", planned_date=today, due_date=today)
        rm.create_or_update_routine("pulse-check", "every 30m", "endpoint_pulse", title="LAN Endpoint Pulse", actor="user")
        edu.upsert_course(title="Network Engineering", domain="networking", modules=["OSI & TCP/IP"])
        edu.upsert_study_items("network-engineering", [{"prompt": "What port is HTTPS?", "answer": "TCP 443"}])

        win = cal.get_calendar_window(start_date=today, end_date=today, days=1)
        self.assertTrue(win["ok"])
        src_types = {it["source_type"] for it in win["items"]}
        self.assertTrue({"event", "deadline", "routine", "study_review"}.issubset(src_types))
        routine_entries = [it for it in win["items"] if it["source_type"] == "routine" and it.get("title", "").endswith("LAN Endpoint Pulse")]
        self.assertEqual(len(routine_entries), 1)

    def test_headless_education_srs_flashcard_deck_and_wiki_curation(self) -> None:
        edu = get_education_engine(store=self.store)
        c_res = edu.upsert_course(
            title="Distributed Systems Fundamentals",
            domain="architecture",
            goal_summary="Master consensus, replication, and partitioning.",
            modules=["Priming & CAP Tradeoffs", "Raft Leader Election", "Consistent Hashing"],
        )
        self.assertTrue(c_res["ok"])
        cid = c_res["course_id"]

        items_res = edu.upsert_study_items(
            cid,
            [
                {"prompt": "What does CAP stand for?", "answer": "Consistency, Availability, Partition Tolerance", "topic": "CAP Theorem"},
                {"prompt": "In Raft, what triggers a follower to become a candidate?", "answer": "Election timeout elapsing without a leader heartbeat.", "topic": "Raft"},
            ],
        )
        self.assertEqual(items_res["count"], 2)
        i1, i2 = items_res["item_ids"]

        g_pass = edu.grade_item(i1, passed=True, confidence=5)
        self.assertGreaterEqual(g_pass["srs_box"], 1)
        self.assertGreaterEqual(g_pass["interval_days"], 1)

        g_miss = edu.grade_item(i2, passed=False, miss_reason="Confused heartbeat interval with split-vote backoff")
        self.assertEqual(g_miss["srs_box"], 0)
        self.assertEqual(g_miss["interval_days"], 0)

        deck_res = edu.build_inline_flashcard_block(course_id=cid, limit=5)
        self.assertTrue(deck_res["ok"])
        self.assertTrue(deck_res["markdown_block"].startswith("```companion-flashcards\n"))
        parsed_deck = json.loads(deck_res["markdown_block"].splitlines()[1])
        self.assertGreaterEqual(len(parsed_deck["cards"]), 1)

        note_res = edu.curate_study_note(
            course_id=cid,
            module_title="Priming & CAP Tradeoffs",
            core_idea="During a network partition, a distributed data store must trade off linearizable consistency against total availability.",
            mechanism_markdown="When nodes cannot communicate across a network split, writes must either block/reject (CP) or accept divergence (AP).",
            mermaid_diagram="flowchart LR\n  P[Network Partition] --> CP[CP: Preserve Consistency]\n  P --> AP[AP: Preserve Availability]",
        )
        self.assertTrue(note_res["ok"], msg=str(note_res))
        self.assertTrue(str(note_res["path"]).startswith("02_knowledge/education/"))
        tpl_slugs = [t["slug"] for t in list_templates(self.tmp_path / "wiki")]
        self.assertEqual(len(tpl_slugs), 11)

    def test_core_actions_slash_commands_phase_gates_and_status_enrichment(self) -> None:
        out_b = handle_harvest_slash("/board", "add Check tire pressure", store=self.store)
        self.assertIn("Check tire pressure", out_b)
        out_c = handle_harvest_slash("/calendar", "today", store=self.store)
        self.assertIn("Calendar", out_c)
        out_s = handle_harvest_slash("/study", "new Rust Ownership", store=self.store)
        self.assertIn("Rust Ownership", out_s)

        res_ag = execute_core_action("agile", {"subaction": "board_view"}, actor="web", store=self.store)
        self.assertTrue(res_ag["ok"])

        self.assertIsNone(check_phase_tool_permission("agile_manage", "PLAN", {"action": "board_view"}))
        self.assertIsNotNone(check_phase_tool_permission("agile_manage", "PLAN", {"action": "create_card"}))
        self.assertIsNone(check_phase_tool_permission("education_manage", "DISCOVER", {"action": "progress"}))
        self.assertIsNotNone(check_phase_tool_permission("education_manage", "DISCOVER", {"action": "grade_item"}))

        prompt = build_system_prompt(store=self.store)
        self.assertIn("[PROJECT & GOAL GRAPH", prompt)
        self.assertIn("[EDUCATION & MASTERY ENGINE (CONVERSATIONAL SOCRATIC TUTOR)]", prompt)
        enriched = enrich_web_status(self.store)
        self.assertIn("agile_summary", enriched)
        self.assertIn("education_summary", enriched)
        self.assertEqual(enriched["education_summary"]["active_course_title"], "Rust Ownership")

        edu = get_education_engine(store=self.store)
        cr = edu.upsert_course(title="Go Concurrency", domain="systems", modules=["Goroutines", "Channels"])
        edu.upsert_study_items(cr["course_id"], [{"prompt": "What buffered channel op blocks?", "answer": "Send when buffer is full."}])
        gen_res = edu.generate_study_items(
            course_id=cr["course_id"],
            item_kind="quiz",
            count=2,
            chat_fn=lambda *_a, **_kw: {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": '{"items":[{"prompt":"When does an unbuffered channel send complete?","answer":"When a receiver is ready","choices":["Immediately","When a receiver is ready","On close","Never"],"explanation":"Rendezvous synchronization.","topic":"Channels"}]}',
                    }
                }]
            },
        )
        self.assertTrue(gen_res["ok"], gen_res)
        self.assertGreaterEqual(gen_res["count"], 1)
        ai_course = edu.upsert_course(
            title="French Mother Sauces",
            chat_fn=lambda *_a, **_kw: {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": '{"domain":"cooking","goal_summary":"Master the five classical French mother sauces.","modules":[{"slug":"roux","title":"Roux Ratios & Starch","objective":"Cook white, blond, and brown roux."},{"slug":"bechamel","title":"Bechamel & Veloute","objective":"Emulsify dairy and stock bases."}]}',
                    }
                }]
            },
        )
        self.assertEqual(ai_course["course"]["total_modules"], 2)
        self.assertEqual(ai_course["course"]["domain"], "cooking")
        with memory_store_context(self.store):
            tool_out = tool_education_manage({"action": "flashcard_deck", "course_id": cr["course_id"]})
        self.assertIn("```companion-flashcards", tool_out)

        d_edu = classify_turn_intent("Create a study course on Kubernetes across multiple modules, whatever you think is best.")
        self.assertEqual(d_edu.mode, "GUIDED_CHANGE")
        self.assertFalse(d_edu.needs_blueprint)

        skills = discover_skills()
        top_tutor = [s["name"] for s in rank_relevant_skills("Teach me Raft and quiz me with flashcards", skills, top_k=4)]
        self.assertIn("socratic-tutoring", top_tutor)
        top_agile = [s["name"] for s in rank_relevant_skills("Plan my weekend kanban capacity and slice a 15m chore unblocker", skills, top_k=4)]
        self.assertIn("personal-agile-coach", top_agile)

    def test_dom2_agile_subactions_search_glob_web_search_and_scoped_repo_tools(self) -> None:
        with memory_store_context(self.store):
            p_res = execute_core_action("agile", {"subaction": "create_project", "title": "Home Ops", "description": "Initial summary"}, actor="user", store=self.store)
            pid = p_res["project"]["project_id"]
            u_res = execute_core_action("agile", {"subaction": "update_project", "project_id": pid, "description": "Updated goal summary"}, actor="user", store=self.store)
            self.assertEqual(u_res["project"]["goal_summary"], "Updated goal summary")

            ep1 = execute_core_action("agile", {"subaction": "save_epic", "project_id": pid, "title": "NAS Upgrade"}, actor="companion", store=self.store)
            mid = ep1["epic"]["milestone_id"]
            ep2 = execute_core_action("agile", {"subaction": "save_epic", "project_id": pid, "epic_id": mid, "title": "NAS 10GbE Upgrade"}, actor="companion", store=self.store)
            self.assertEqual(ep2["epic"]["title"], "NAS 10GbE Upgrade")
            self.assertEqual(len(execute_core_action("agile", {"subaction": "list_epics", "project_id": pid}, store=self.store)["epics"]), 1)

            c_res = execute_core_action("agile", {
                "subaction": "create_item", "project_id": pid, "epic_id": mid, "title": "Install SFP+ card",
                "status": "sprint_todo", "effort_size": "half_day", "checklist": ["Power down NAS", "Seat NIC"],
            }, actor="companion", store=self.store)
            cid = c_res["item"]["item_id"]
            self.assertEqual(c_res["item"]["estimate_minutes"], 180)
            self.assertEqual(len(c_res["item"]["checklist"]), 2)
            self.assertEqual(len(get_project_graph(self.store).next_ready_companion_items(pid)), 1)

            ub = execute_core_action("agile", {"subaction": "slice_unblocker", "card_id": cid, "title": "Order DAC cable"}, actor="companion", store=self.store)
            self.assertTrue(ub["ok"])
            self.assertEqual(ub["unblocker"]["estimate_minutes"], 15)
            self.assertEqual(get_project_graph(self.store).get_item(cid)["status"], "blocked")
            self.assertEqual(len(execute_core_action("agile", {"subaction": "list_items", "project_id": pid}, store=self.store)["items"]), 2)
            self.assertTrue(execute_core_action("agile", {"subaction": "delete_epic", "epic_id": mid}, store=self.store)["ok"])
            bad_sub = execute_core_action("agile", {"subaction": "nonexistent_subaction"}, store=self.store)
            self.assertFalse(bad_sub["ok"])
            self.assertIn("error:", tool_agile_manage({"action": "nonexistent_subaction"}))

            # Search file_glob + surfaces/static skipping
            s_dir = self.tmp_path / "search_ws"
            (s_dir / "surfaces" / "static").mkdir(parents=True)
            (s_dir / "surfaces" / "static" / "app.js").write_text("UNIQUE_NEEDLE_XYZ", encoding="utf-8")
            (s_dir / "notes.md").write_text("UNIQUE_NEEDLE_XYZ in markdown", encoding="utf-8")
            (s_dir / "code.py").write_text("UNIQUE_NEEDLE_XYZ in python", encoding="utf-8")
            with workspace_context(s_dir):
                s_all = tool_search({"pattern": "UNIQUE_NEEDLE_XYZ", "path": str(s_dir)})
                self.assertNotIn("app.js", s_all)
                self.assertIn("notes.md", s_all)
                s_py = tool_search({"pattern": "UNIQUE_NEEDLE_XYZ", "path": str(s_dir), "file_glob": "*.py"})
                self.assertIn("code.py", s_py)
                self.assertNotIn("notes.md", s_py)

            # web_search privacy lock + HTML Lite parsing
            with unittest.mock.patch("companion.tools.settings.effective_privacy_mode", return_value="local_only"), unittest.mock.patch.dict(os.environ, {"COMPANION_SEARCH_URL": ""}, clear=False):
                self.assertIn("PRIVACY LOCK", tool_web_search({"query": "Ubuntu GDM black screen"}))
            fake_html = b'<html><body><a class="result__a" href="https://example.com/gdm">Fix GDM Black Screen</a><a class="result__snippet">Restart gdm service.</a></body></html>'
            fake_resp = unittest.mock.MagicMock()
            fake_resp.read.return_value = fake_html
            fake_resp.__enter__.return_value = fake_resp
            with unittest.mock.patch("companion.tools.settings.effective_privacy_mode", return_value="hybrid"), unittest.mock.patch("companion.tools.urlopen_safe", return_value=fake_resp), unittest.mock.patch.dict(os.environ, {"COMPANION_SEARCH_URL": ""}, clear=False):
                ws_out = tool_web_search({"query": "Ubuntu GDM", "max_results": 3})
            self.assertIn("Fix GDM Black Screen", ws_out)
            self.assertIn("https://example.com/gdm", ws_out)

            # Scoped repo-internal tools: hidden in daily OS mode, exposed when focused on an external repo workspace
            with workspace_context(settings.root, "companion-self"):
                daily_names = {t["function"]["name"] for t in filter_tools_for_phase("")}
                self.assertNotIn("system_doctor", daily_names)
                self.assertNotIn("git_manage", daily_names)
                self.assertNotIn("dev_preview", daily_names)
            ext_ws = self.tmp_path / "ext_sdlc_repo"
            (ext_ws / ".git").mkdir(parents=True, exist_ok=True)
            with workspace_context(ext_ws, "ext-sdlc-repo"):
                dev_names = {t["function"]["name"] for t in filter_tools_for_phase("")}
                self.assertIn("git_manage", dev_names)
                self.assertIn("dev_preview", dev_names)


if __name__ == "__main__":
    unittest.main()
