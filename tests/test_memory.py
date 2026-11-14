"""Unit tests for SQLite MemoryStore and backward-compatible ChatMemory facade."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion.memory_store import MemoryStore, parse_time_window
from memory.chat_memory import ChatMemory, get_backlog


class TestMemoryStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_conversations.db"
        self.store = MemoryStore(db_path=self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp_dir.cleanup()

    def test_seeded_user_profile_and_adrs(self) -> None:
        profile = self.store.get_user_profile()
        self.assertIn("technical_delegation", profile)
        self.assertIn("partner", profile["role"].lower())
        self.assertIn("app_version", profile)
        adrs = self.store.get_adrs()
        self.assertGreaterEqual(len(adrs), 4)

    def test_pre_upgrade_sqlite_backup_on_version_change(self) -> None:
        self.store.set_user_profile("app_version", "0.9.0")
        self.store.close()
        upgraded = MemoryStore(db_path=self.db_path)
        try:
            backups = list((Path(self.tmp_dir.name) / ".companion_backups").glob("pre_upgrade_0.9.0_to_*.db"))
            self.assertEqual(len(backups), 1)
        finally:
            upgraded.close()
        self.store = MemoryStore(db_path=self.db_path)

    def test_conversations_logging_and_search(self) -> None:
        self.store.log_message("user", "Build a state machine workflow")
        self.store.log_message("assistant", "I will design the directed graph engine.")
        history = self.store.get_conversation_history(limit=10)
        self.assertEqual(len(history), 2)
        hits = self.store.search_conversations("directed graph")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], "assistant")

    def test_tasks_and_backlog_lifecycle(self) -> None:
        tid = self.store.add_task("Write unit tests", priority=1, acceptance_criteria="All tests pass")
        pending = self.store.get_pending_tasks()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][0], tid)

        self.store.update_task(tid, status="completed", verification_result="unittest passed")
        self.assertEqual(len(self.store.get_pending_tasks()), 0)

        bid = self.store.add_backlog("Add semantic search", category="memory", priority=2)
        items = self.store.get_backlog(category="memory")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][0], bid)

    def test_chat_memory_backward_compatibility(self) -> None:
        cm = ChatMemory(db_path=self.db_path)
        cm.add_backlog("Test alias method", category="test")
        # Both get_backlog(cm) and cm.get_backlog() should work without error
        rows = get_backlog(cm, category="test")
        self.assertEqual(len(rows), 1)
        cm.close()

    def test_sync_memory_md(self) -> None:
        md_path = Path(self.tmp_dir.name) / "MEMORY.md"
        self.store.add_system_note("Always verify syntax via AST", category="lesson")
        self.store.sync_memory_md(target_path=md_path)
        text = md_path.read_text(encoding="utf-8")
        self.assertIn("User Profile & Partnership Ethos", text)
        self.assertIn("ADR-001", text)
        self.assertIn("Always verify syntax via AST", text)

    def test_fts5_multiword_and_time_window_search(self) -> None:
        rid_today = self.store.log_message("user", "We implemented SQLite FTS5 indexing for fast conversation search")
        rid_old = self.store.log_message("assistant", "Legacy archive note from two days ago about SQLite search")
        old_ts = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        self.store.conn.execute("UPDATE conversations SET timestamp = ? WHERE id = ?", (old_ts, rid_old))
        self.store.conn.commit()

        # Non-contiguous multi-word FTS5 match ("SQLite conversation")
        fts_hits = self.store.search_conversations("SQLite conversation")
        self.assertEqual(len(fts_hits), 1)
        self.assertEqual(fts_hits[0][0], rid_today)

        # Time-window filtered search ("today" vs "last 7d")
        today_hits = self.store.search_conversations("SQLite", time_window="today")
        self.assertEqual([h[0] for h in today_hits], [rid_today])
        week_hits = self.store.search_conversations("SQLite", time_window="last 7d")
        self.assertEqual(len(week_hits), 2)

        # Verify parse_time_window formats (including bare '24h', '-7 days', '-24 hours', 'past 2 weeks', '30m')
        fixed = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_time_window("today", now=fixed), ("2026-11-08 00:00:00", "2026-11-08 23:59:59"))
        self.assertEqual(parse_time_window("yesterday", now=fixed), ("2026-11-07 00:00:00", "2026-11-07 23:59:59"))
        self.assertEqual(parse_time_window("24h", now=fixed), ("2026-11-07 12:00:00", "2026-11-08 23:59:59"))
        self.assertEqual(parse_time_window("-24 hours", now=fixed), ("2026-11-07 12:00:00", "2026-11-08 23:59:59"))
        self.assertEqual(parse_time_window("-7 days", now=fixed), ("2026-11-01 12:00:00", "2026-11-08 23:59:59"))
        self.assertEqual(parse_time_window("past 2 weeks", now=fixed), ("2026-10-25 12:00:00", "2026-11-08 23:59:59"))
        self.assertEqual(parse_time_window("30m", now=fixed), ("2026-11-08 11:30:00", "2026-11-08 23:59:59"))
        self.assertEqual(parse_time_window("2026-11-01", now=fixed), ("2026-11-01 00:00:00", "2026-11-01 23:59:59"))

        # Verify FTS5 OR fallback when query contains boolean operator or partial tokens
        or_hits = self.store.search_conversations("indexing OR unmatched_token_xyz")
        self.assertEqual([h[0] for h in or_hits], [rid_today])

    def test_secret_redaction_in_messages_and_notes(self) -> None:
        self.store.log_message("user", "Auth header: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 key=sk-1234567890abcdef")
        self.store.add_system_note("Token ghp_1234567890abcdefghijklmnopqrstuv saved", category="sec")
        msgs = self.store.get_recent_messages(limit=1)
        notes = self.store.get_system_notes(category="sec", limit=1)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", msgs[0][1])
        self.assertNotIn("sk-1234567890abcdef", msgs[0][1])
        self.assertNotIn("ghp_1234567890abcdefghijklmnopqrstuv", notes[0][0])


    def test_empty_query_returns_no_results(self) -> None:
        hits = self.store.search_conversations("")
        self.assertEqual(len(hits), 0)

if __name__ == "__main__":
    unittest.main()
