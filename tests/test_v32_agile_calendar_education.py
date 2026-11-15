"""
Comprehensive Unit & Integration Tests for Companion 3.2:
1. Universal Agile, Multi-Board Kanban, Capacity Slots, Dependency Guard & 15m Unblocker Slicing
2. Live Software Engineering Workflow -> Kanban Board Sync
3. Unified Multi-Source Calendar (Events + Sprints + Deadlines + Routines + Due SRS Reviews)
4. Headless Education, Socratic Syllabus Progression, SM-2 Mastery, Wiki Study Notes & Inline Chat Flashcards
5. CoreActions, Slash Commands, Phase Gates, HUD & Web Status Parity
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion.agile_boards import MASTER_SPRINT_BOARD_ID, get_agile_manager, sync_workflow_to_agile_board
from companion.calendar_engine import get_calendar_manager
from companion.context import build_system_prompt
from companion.core_actions import execute_core_action, handle_harvest_slash
from companion.education_engine import get_education_engine
from companion.grounding import discover_skills, rank_relevant_skills
from companion.harvest_tools import tool_education_manage
from companion.intake import classify_turn_intent
from companion.memory_store import MemoryStore, memory_store_context
from companion.routines import get_routine_manager
from companion.tool_schemas import check_phase_tool_permission
from companion.wiki_templates import list_templates
from surfaces.api_extensions import enrich_web_status


class TestCompanionV32AgileCalendarEducation(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.store = MemoryStore(db_path=self.tmp_path / "test_v32.db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_agile_multiboard_capacity_dependencies_aging_and_unblocker(self) -> None:
        mgr = get_agile_manager(store=self.store)
        boards = mgr.list_boards()
        board_ids = {b["board_id"] for b in boards}
        self.assertTrue({"life-home", "work-career", "software-companion"}.issubset(board_ids))

        # Custom project board & Horizon Epic ("Renovate basement" -> later)
        b_res = mgr.upsert_board(title="Basement Renovation", kind="project", board_id="basement-reno")
        self.assertTrue(b_res["ok"])
        ep_res = mgr.upsert_epic(title="Clean & Renovate Basement", board_id="basement-reno", horizon="later")
        self.assertTrue(ep_res["ok"])

        # Start weekly sprint with 6 weekday slots and 4 weekend slots
        sp = mgr.upsert_sprint(
            title="Week 46 Focus",
            goal_summary="Prep work study + schedule truck + clean gutters",
            start_date="2026-11-09",
            end_date="2026-11-15",
            weekday_capacity_slots=6,
            weekend_capacity_slots=4,
        )
        self.assertTrue(sp["ok"])
        sid = sp["sprint_id"]

        # Add Work study prep (half_day=2 weekday) & Weekend gutters (full_day=4 weekend)
        c_work = mgr.upsert_card(
            title="Work Certification Study Block",
            board_id="work-career",
            status="sprint_todo",
            effort_size="half_day",
            day_slot="weekday",
            planned_date="2026-11-11",
        )
        c_gutters = mgr.upsert_card(
            title="Clean house gutters",
            board_id="life-home",
            status="sprint_todo",
            effort_size="full_day",
            day_slot="weekend",
            planned_date="2026-11-14",
        )
        self.assertTrue(c_work["ok"] and c_gutters["ok"])

        cap = mgr.compute_sprint_capacity(sid)
        self.assertEqual(cap["weekday_used"], 2)
        self.assertEqual(cap["weekend_used"], 4)
        self.assertFalse(cap["over_capacity"])

        # Simulate 8-month procrastinated truck repair card -> appears on Aging Radar
        eight_months_ago = (datetime.now(timezone.utc) - timedelta(days=240)).strftime("%Y-%m-%dT%H:%M:%SZ")
        c_truck = mgr.upsert_card(
            title="Take truck into shop to get fixed",
            board_id="life-home",
            status="backlog",
            effort_size="half_day",
            day_slot="weekday",
            created_at_override=eight_months_ago,
        )
        self.assertTrue(c_truck["card"]["is_aging"])
        self.assertGreaterEqual(c_truck["card"]["untouched_days"], 239)

        master_view = mgr.get_board_view(MASTER_SPRINT_BOARD_ID)
        aging_ids = [c["card_id"] for c in master_view["aging_cards"]]
        self.assertIn(c_truck["card_id"], aging_ids)

        # Slice a <30m unblocker card; verify dependency guard blocks parent until unblocker finishes
        sliced = mgr.slice_unblocker_card(
            stalled_card_id=c_truck["card_id"],
            first_step_title="Call mechanic & book Tuesday morning drop-off (15m)",
            day_slot="weekday",
            planned_date="2026-11-10",
        )
        self.assertTrue(sliced["ok"])
        unblocker_id = sliced["unblocker_card_id"]

        blocked_move = mgr.move_card(c_truck["card_id"], status="in_progress")
        self.assertFalse(blocked_move["ok"])
        self.assertIn("DEPENDENCY GUARD", blocked_move["error"])

        # Complete the 15m unblocker -> parent card can now move cleanly to in_progress!
        done_unblocker = mgr.move_card(unblocker_id, status="done")
        self.assertTrue(done_unblocker["ok"])
        unlocked_move = mgr.move_card(c_truck["card_id"], status="in_progress")
        self.assertTrue(unlocked_move["ok"])

        # Rollover sprint -> unfinished sprint cards increment rollover_count
        roll = mgr.rollover_sprint(retro_notes="Good progress; gutters moved to next weekend.", start_next=True, next_title="Week 47")
        self.assertTrue(roll["ok"])
        self.assertGreaterEqual(roll["rolled_over_count"], 1)

    def test_workflow_sync_to_software_kanban_board(self) -> None:
        wf_id = self.store.create_workflow_run("engineering_feature", "Build telemetry exporter", "PLAN")
        t1 = self.store.add_task("Create exporter schema", priority=1, workflow_id=wf_id)
        t2 = self.store.add_task("Add unit tests for exporter", priority=2, workflow_id=wf_id)

        sync_workflow_to_agile_board(self.store, wf_id, "Build telemetry exporter", "PLAN")
        mgr = get_agile_manager(store=self.store)
        sw_view = mgr.get_board_view("software-companion")
        self.assertEqual(len(sw_view["columns"]["sprint_todo"]), 2)

        sync_workflow_to_agile_board(self.store, wf_id, "Build telemetry exporter", "IMPLEMENT", active_task_id=t1)
        sw_view = mgr.get_board_view("software-companion")
        self.assertEqual(len(sw_view["columns"]["in_progress"]), 1)

        self.store.update_task(t1, status="completed")
        self.store.update_task(t2, status="completed")
        sync_workflow_to_agile_board(self.store, wf_id, "Build telemetry exporter", "VERIFY")
        sw_view = mgr.get_board_view("software-companion")
        self.assertEqual(len(sw_view["columns"]["verify"]), 2)

        sync_workflow_to_agile_board(self.store, wf_id, "Build telemetry exporter", "COMPLETE")
        sw_view = mgr.get_board_view("software-companion")
        self.assertEqual(len(sw_view["columns"]["done"]), 2)

    def test_unified_calendar_aggregates_events_sprints_tasks_routines_and_srs(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cal = get_calendar_manager(store=self.store)
        agile = get_agile_manager(store=self.store)
        rm = get_routine_manager(store=self.store)
        edu = get_education_engine(store=self.store)

        ev = cal.upsert_event(title="Dentist Appointment", start_at=f"{today}T14:30", kind="appointment")
        self.assertTrue(ev["ok"])
        agile.upsert_sprint(title="Current Sprint", start_date=today, end_date=today)
        agile.upsert_card(title="Clean Gutters", board_id="life-home", planned_date=today, due_date=today, day_slot="weekend")
        rm.create_or_update_routine("pulse-check", "every 30m", "endpoint_pulse", title="LAN Endpoint Pulse", actor="user")
        edu.upsert_course(title="Network Engineering", domain="networking", modules=["OSI & TCP/IP"])
        edu.upsert_study_items("network-engineering", [{"prompt": "What port is HTTPS?", "answer": "TCP 443"}])

        win = cal.get_calendar_window(start_date=today, end_date=today, days=1)
        self.assertTrue(win["ok"])
        src_types = {it["source_type"] for it in win["items"]}
        self.assertEqual(src_types, {"event", "sprint_task", "deadline", "routine", "study_review"})
        # Sub-daily 30m routine must be summarized to exactly 1 entry for the day (zero spam)
        routine_entries = [it for it in win["items"] if it["source_type"] == "routine"]
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

        # Grade i1 PASS -> box advances & due_at moves into future; grade i2 MISS -> records diagnostic reason & stays due today
        g_pass = edu.grade_item(i1, passed=True, confidence=5)
        self.assertGreaterEqual(g_pass["srs_box"], 1)
        self.assertGreaterEqual(g_pass["interval_days"], 1)

        g_miss = edu.grade_item(i2, passed=False, miss_reason="Confused heartbeat interval with split-vote backoff")
        self.assertEqual(g_miss["srs_box"], 0)
        self.assertEqual(g_miss["interval_days"], 0)

        # Inline Chat flashcard deck block
        deck_res = edu.build_inline_flashcard_block(course_id=cid, limit=5)
        self.assertTrue(deck_res["ok"])
        self.assertTrue(deck_res["markdown_block"].startswith("```companion-flashcards\n"))
        parsed_deck = json.loads(deck_res["markdown_block"].splitlines()[1])
        self.assertGreaterEqual(len(parsed_deck["cards"]), 1)

        # Curate study note into Wiki Warehouse & verify 11-template invariant remains untouched
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
        self.assertTrue(all("feynman" not in s and "quiz" not in s for s in tpl_slugs))

    def test_core_actions_slash_commands_phase_gates_and_status_enrichment(self) -> None:
        # Slash commands via handle_harvest_slash
        out_b = handle_harvest_slash("/board", "add Check tire pressure", store=self.store)
        self.assertIn("Check tire pressure", out_b)
        out_c = handle_harvest_slash("/calendar", "today", store=self.store)
        self.assertIn("Calendar", out_c)
        out_s = handle_harvest_slash("/study", "new Rust Ownership", store=self.store)
        self.assertIn("Rust Ownership", out_s)

        # CoreActions bus parity
        res_ag = execute_core_action("agile", {"subaction": "board_view"}, actor="web", store=self.store)
        self.assertTrue(res_ag["ok"])

        # Phase tool gate blocks mutating subactions in PLAN while allowing reads
        self.assertIsNone(check_phase_tool_permission("agile_manage", "PLAN", {"action": "board_view"}))
        self.assertIsNotNone(check_phase_tool_permission("agile_manage", "PLAN", {"action": "save_card"}))
        self.assertIsNone(check_phase_tool_permission("education_manage", "DISCOVER", {"action": "progress"}))
        self.assertIsNotNone(check_phase_tool_permission("education_manage", "DISCOVER", {"action": "grade_item"}))

        # HUD & Web status enrichment
        prompt = build_system_prompt(store=self.store)
        self.assertIn("[AGILE SPRINT, BOARDS & PROCRASTINATION RADAR]", prompt)
        self.assertIn("[EDUCATION & MASTERY ENGINE (CONVERSATIONAL SOCRATIC TUTOR)]", prompt)
        enriched = enrich_web_status(self.store)
        self.assertIn("agile_summary", enriched)
        self.assertIn("education_summary", enriched)
        self.assertEqual(enriched["education_summary"]["active_course_title"], "Rust Ownership")

    def test_dogfood_ergonomics_slash_output_persistence_aliases_and_intent_routing(self) -> None:
        mgr = get_agile_manager(store=self.store)
        mgr.upsert_sprint(title="Dogfood Sprint")

        # 1. LLM enum aliases (`pending`, `15m`, `evenings`, `completed`) & explicit date clearing
        c1 = mgr.upsert_card(
            title="Prep architecture slides",
            board_id="work-career",
            status="pending",
            effort_size="15m",
            day_slot="evenings",
            planned_date="2026-11-16",
            due_date="2026-11-18",
        )
        self.assertTrue(c1["ok"])
        self.assertEqual(c1["card"]["status"], "sprint_todo")
        self.assertEqual(c1["card"]["effort_size"], "quick")
        self.assertEqual(c1["card"]["day_slot"], "weekday")
        cleared = execute_core_action(
            "agile",
            {"subaction": "update_card", "card_id": c1["card_id"], "planned_date": "", "due_date": "clear"},
            actor="web",
            store=self.store,
        )
        self.assertEqual(cleared["card"]["planned_date"], "")
        self.assertEqual(cleared["card"]["due_date"], "")

        # 2. Fuzzy title unblocker slicing & standalone unblocker creation + epic description alias
        ep = execute_core_action(
            "agile",
            {"subaction": "save_epic", "title": "Basement Renovation", "description": "Long-horizon remodel", "horizon": "someday"},
            actor="companion",
            store=self.store,
        )
        self.assertEqual(ep["epic"]["summary"], "Long-horizon remodel")
        unb = execute_core_action(
            "agile",
            {"subaction": "slice_unblocker", "title": "Outline first 3 slide bullets (15m)", "board_id": "work-career"},
            actor="companion",
            store=self.store,
        )
        self.assertTrue(unb["ok"])

        # 3. Rich slash outputs (`/board` lists cards, `/study flashcards` includes `companion-flashcards` block)
        board_slash = handle_harvest_slash("/board", "", store=self.store)
        self.assertIn("Prep architecture slides", board_slash)

        edu = get_education_engine(store=self.store)
        cr = edu.upsert_course(title="Go Concurrency", domain="systems", modules=["Goroutines", "Channels"])
        edu.upsert_study_items(cr["course_id"], [{"prompt": "What buffered channel op blocks?", "answer": "Send when buffer is full."}])
        fc_slash = handle_harvest_slash("/study", "flashcards", store=self.store)
        self.assertIn("```companion-flashcards", fc_slash)

        with memory_store_context(self.store):
            tool_out = tool_education_manage({"action": "flashcard_deck", "course_id": cr["course_id"]})
        self.assertIn("```companion-flashcards", tool_out)

        # 4. Dealt flashcard deck SQLite persistence (`persist_to_chat=True`) & `get_items` live hydration
        persisted = execute_core_action(
            "education",
            {"subaction": "flashcard_deck", "course_id": cr["course_id"], "persist_to_chat": True},
            actor="web",
            store=self.store,
        )
        self.assertTrue(persisted["ok"])
        self.assertIn("chat_message", persisted)
        rows = self.store._fetch_rows("SELECT content FROM conversations WHERE session_tag = 'study_deck'")
        self.assertEqual(len(rows), 1)
        self.assertIn("```companion-flashcards", str(rows[0][0]))

        # 5. Coaching turns with "across multiple modules" or "whatever you think" stay conversational (`GUIDED_CHANGE`)
        d_edu = classify_turn_intent("Create a study course on Kubernetes across multiple modules, whatever you think is best.")
        self.assertEqual(d_edu.mode, "GUIDED_CHANGE")
        self.assertFalse(d_edu.needs_blueprint)
        d_spr = classify_turn_intent("I've procrastinated my chores, help me organize my sprint and slice an unblocker.")
        self.assertEqual(d_spr.mode, "GUIDED_CHANGE")

        skills = discover_skills()
        top_tutor = [s["name"] for s in rank_relevant_skills("Teach me Raft and quiz me with flashcards", skills, top_k=4)]
        self.assertIn("socratic-tutoring", top_tutor)
        top_agile = [s["name"] for s in rank_relevant_skills("Plan my weekend kanban board and schedule mechanic appointment", skills, top_k=4)]
        self.assertIn("agile-coach", top_agile)

    def test_epic_dedupe_roadmap_checklists_and_calendar_milestones(self) -> None:
        mgr = get_agile_manager(store=self.store)
        cal = get_calendar_manager(store=self.store)

        # Duplicate save_epic calls for the same (board_id, title) merge cleanly instead of duplicating
        ep1 = mgr.upsert_epic(
            title="Barn cleaning winter project",
            board_id="life-home",
            horizon="later",
            start_date="2026-11-14",
        )
        ep2 = mgr.upsert_epic(
            title="barn cleaning winter project",
            board_id="life-home",
            horizon="next",
            target_date="2026-12-20",
            summary="Clean stalls, haul scrap, winterize equipment",
        )
        self.assertTrue(ep1["ok"] and ep2["ok"])
        self.assertEqual(ep1["epic_id"], ep2["epic_id"])
        epics = [e for e in mgr.list_epics(board_id="life-home") if "barn cleaning" in e["title"].lower()]
        self.assertEqual(len(epics), 1)
        epic_id = ep1["epic_id"]
        self.assertEqual(epics[0]["horizon"], "next")
        self.assertEqual(epics[0]["start_date"], "2026-11-14")
        self.assertEqual(epics[0]["target_date"], "2026-12-20")

        # Linked child cards + interactive checklists (markdown lines + dict updates)
        c1 = mgr.upsert_card(
            title="Clean each stall & winterize equipment",
            board_id="life-home",
            epic_id=epic_id,
            status="sprint_todo",
            checklist=["[x] Muck stall 1", "[ ] Haul scrap junk", "Swap trimmer carburetor"],
        )
        c2 = mgr.upsert_card(
            title="Stack winter hay bales",
            board_id="life-home",
            epic_id=epic_id,
            status="backlog",
        )
        self.assertTrue(c1["ok"] and c2["ok"])
        self.assertEqual(c1["card"]["epic_title"], "Barn cleaning winter project")
        self.assertEqual(c1["card"]["checklist_total"], 3)
        self.assertEqual(c1["card"]["checklist_done"], 1)

        # Toggle checklist item via CoreAction update_card
        updated_cl = [
            {"id": c1["card"]["checklist"][0]["id"], "text": "Muck stall 1", "done": True},
            {"id": c1["card"]["checklist"][1]["id"], "text": "Haul scrap junk", "done": True},
            {"id": c1["card"]["checklist"][2]["id"], "text": "Swap trimmer carburetor", "done": False},
        ]
        upd = execute_core_action(
            "agile",
            {"subaction": "update_card", "card_id": c1["card_id"], "checklist": updated_cl},
            actor="web",
            store=self.store,
        )
        self.assertEqual(upd["card"]["checklist_done"], 2)

        # Moving an active sprint card back to backlog detaches sprint_id and surfaces in backlog_cards
        mgr.upsert_sprint(title="Active Barn Sprint", weekday_capacity_slots=10, weekend_capacity_slots=6)
        c3 = mgr.upsert_card(title="Order winter salt", board_id="life-home", status="sprint_todo", effort_size="half_day", day_slot="weekday")
        self.assertNotEqual(c3["card"]["sprint_id"], "")
        moved_bl = mgr.move_card(c3["card_id"], status="backlog")
        self.assertTrue(moved_bl["ok"])
        self.assertEqual(moved_bl["card"]["sprint_id"], "")
        self.assertIn(c3["card_id"], [c["card_id"] for c in mgr.get_board_view(MASTER_SPRINT_BOARD_ID)["backlog_cards"]])

        # Complete c1 and verify epic progress_pct (1 of 2 done = 50%) & board_view backlog_cards/all_cards
        mgr.move_card(c1["card_id"], status="done")
        view = mgr.get_board_view(MASTER_SPRINT_BOARD_ID)
        ep_enriched = next(e for e in view["epics"] if e["epic_id"] == epic_id)
        self.assertEqual(ep_enriched["card_count"], 2)
        self.assertEqual(ep_enriched["done_card_count"], 1)
        self.assertEqual(ep_enriched["progress_pct"], 50)
        self.assertIn(c2["card_id"], [c["card_id"] for c in view["backlog_cards"]])
        self.assertGreaterEqual(len(view["all_cards"]), 2)

        # Calendar window returns roadmap epics + epic_milestone on target_date
        win = cal.get_calendar_window(start_date="2026-11-01", end_date="2026-12-31")
        self.assertTrue(any(e["epic_id"] == epic_id for e in win["epics"]))
        milestones = [it for it in win["items"] if it["source_type"] == "epic_milestone" and it.get("epic_id") == epic_id]
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0]["date"], "2026-12-20")

        # Phase gate blocks delete_epic in PLAN; deleting epic unlinks child cards without deleting them
        self.assertIsNotNone(check_phase_tool_permission("agile_manage", "PLAN", {"action": "delete_epic"}))
        del_res = execute_core_action("agile", {"subaction": "delete_epic", "epic_id": epic_id}, actor="web", store=self.store)
        self.assertTrue(del_res["ok"])
        after_card = next(c for c in mgr.get_board_view("life-home")["all_cards"] if c["card_id"] == c2["card_id"])
        self.assertEqual(after_card["epic_id"], "")


if __name__ == "__main__":
    unittest.main()
