"""
Unified Multi-Source Calendar & Time-Horizon Engine
(`companion/calendar_engine.py` — A-Frame Application Layer).

Aggregates four live sources into one normalized Month / Week / Agenda timeline:
1. Explicit Appointments, Focus Blocks & Milestones (`calendar_events`).
2. Active Sprint Windows, Planned Task Days (`planned_date`) & Hard Deadlines (`due_date`) from `agile_boards`.
3. Scheduled Companion Routines (`routines.py`) projected cleanly across the calendar window.
4. Due Spaced-Repetition Study Reviews (`education_engine.py`).
"""

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from companion.agile_boards import get_agile_manager
from companion.education_engine import get_education_engine
from companion.memory_store import MemoryStore, get_memory_store
from companion.routines import compute_next_run_utc, get_routine_manager

__all__ = [
    "CalendarManager", "EVENT_KINDS", "execute_calendar_action",
    "get_calendar_manager", "parse_calendar_slash",
]

EVENT_KINDS: Tuple[str, ...] = ("appointment", "focus_block", "milestone", "reminder")

_EVENT_COLS = [
    "event_id", "title", "kind", "start_at", "end_at", "all_day",
    "board_id", "card_id", "notes", "created_at", "updated_at",
]

CALENDAR_DDL: Tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS calendar_events (event_id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT DEFAULT 'appointment', start_at TEXT NOT NULL, end_at TEXT DEFAULT '', all_day INTEGER DEFAULT 0, board_id TEXT DEFAULT '', card_id TEXT DEFAULT '', notes TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_calendar_events_start ON calendar_events(start_at)",
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now_utc()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_event_id() -> str:
    return f"evt_{_now_utc().strftime('%Y%m%d')}_{os.urandom(3).hex()}"


def _norm_date_prefix(val: str) -> str:
    clean = (val or "").strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", clean)
    return m.group(1) if m else ""


def _parse_ymd(ymd: str) -> Optional[datetime]:
    try:
        return datetime.strptime(ymd[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None


class CalendarManager:
    """Thread-safe SQLite Calendar Manager merging Events, Agile Sprints/Tasks, Routines & SRS Study Counts."""

    def __init__(self, store: Optional[MemoryStore] = None):
        self._store = store

    @property
    def store(self) -> MemoryStore:
        mem = self._store or get_memory_store()
        mem.ensure_schema("calendar", lambda m: [m.conn.execute(ddl) for ddl in CALENDAR_DDL] and None)
        return mem

    def upsert_event(
        self, title: str, start_at: str, end_at: str = "", kind: str = "appointment",
        all_day: bool = False, board_id: str = "", card_id: str = "", notes: str = "", event_id: str = "",
    ) -> Dict[str, Any]:
        clean_title = (title or "").strip()
        s_at = (start_at or "").strip()
        k = (kind or "appointment").strip().lower()
        if not clean_title or not _norm_date_prefix(s_at):
            return {"ok": False, "error": "Both `title` and `start_at` (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`) are required."}
        if k not in EVENT_KINDS:
            return {"ok": False, "error": f"Invalid event `kind` '{kind}'. Valid: {', '.join(EVENT_KINDS)}."}
        is_all_day = bool(all_day or len(s_at) == 10)
        eid = re.sub(r"[^a-z0-9_-]+", "-", event_id.strip().lower()).strip("-")[:56] if event_id.strip() else _make_event_id()
        now_s = _fmt_iso()
        with self.store._lock:
            self.store.conn.execute(
                """INSERT INTO calendar_events (event_id, title, kind, start_at, end_at, all_day, board_id, card_id, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_id) DO UPDATE SET title=excluded.title, kind=excluded.kind, start_at=excluded.start_at,
                   end_at=excluded.end_at, all_day=excluded.all_day, board_id=excluded.board_id, card_id=excluded.card_id,
                   notes=excluded.notes, updated_at=excluded.updated_at""",
                (eid, clean_title[:140], k, s_at[:25], (end_at or "").strip()[:25], 1 if is_all_day else 0, (board_id or "").strip(), (card_id or "").strip(), (notes or "").strip()[:800], now_s, now_s),
            )
            self.store.conn.commit()
        ev = self.get_event(eid)
        return {"ok": True, "event_id": eid, "event": ev, "summary": f"Scheduled `{clean_title}` (`{eid}`, {k} @ `{s_at}`)."}

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        rows = self.store._fetch_dicts(_EVENT_COLS, f"SELECT {', '.join(_EVENT_COLS)} FROM calendar_events WHERE event_id = ?", (event_id.strip(),))
        if not rows:
            return None
        r = rows[0]
        r["all_day"] = bool(r["all_day"])
        return r

    def delete_event(self, event_id: str) -> Dict[str, Any]:
        eid = (event_id or "").strip()
        with self.store._lock:
            cur = self.store.conn.execute("DELETE FROM calendar_events WHERE event_id = ?", (eid,))
            self.store.conn.commit()
        return {"ok": bool(cur.rowcount), "event_id": eid, "summary": f"Deleted calendar event `{eid}`."} if cur.rowcount else {"ok": False, "error": f"Event `{eid}` not found."}

    def list_events(self, start_date: str = "", end_date: str = "") -> List[Dict[str, Any]]:
        clauses, params = [], []
        if start_date:
            clauses.append("substr(start_at, 1, 10) >= ?")
            params.append(start_date[:10])
        if end_date:
            clauses.append("substr(start_at, 1, 10) <= ?")
            params.append(end_date[:10])
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = self.store._fetch_dicts(_EVENT_COLS, f"SELECT {', '.join(_EVENT_COLS)} FROM calendar_events {where}ORDER BY start_at ASC", tuple(params))
        for r in rows:
            r["all_day"] = bool(r["all_day"])
        return rows

    def _project_routines(self, start_dt: datetime, end_dt: datetime) -> List[Dict[str, Any]]:
        rm = get_routine_manager(store=self.store)
        projected: List[Dict[str, Any]] = []
        end_bound = end_dt + timedelta(days=1) - timedelta(seconds=1)
        for rt in rm.list_routines():
            if not rt.get("enabled"):
                continue
            expr = str(rt.get("schedule_expr") or "")
            seen_days: Dict[str, Tuple[str, int]] = {}
            cursor = start_dt - timedelta(minutes=1)
            for _ in range(400):
                try:
                    nxt = compute_next_run_utc(expr, after_dt=cursor)
                except Exception:
                    break
                if nxt > end_bound:
                    break
                d_key = nxt.strftime("%Y-%m-%d")
                t_str = nxt.strftime("%H:%M")
                if d_key not in seen_days:
                    seen_days[d_key] = (t_str, 1)
                else:
                    seen_days[d_key] = (seen_days[d_key][0], seen_days[d_key][1] + 1)
                cursor = nxt if nxt > cursor else (cursor + timedelta(minutes=1))
            for d_key, (first_hhmm, cnt) in seen_days.items():
                freq_note = f" ({expr})" if cnt > 1 else f" @ {first_hhmm} UTC"
                projected.append({
                    "id": f"rt_{rt['routine_id']}_{d_key}",
                    "date": d_key,
                    "time": "" if cnt > 1 else first_hhmm,
                    "source_type": "routine",
                    "kind": rt["target_kind"],
                    "title": f"Routine: {rt['title']}{freq_note}",
                    "routine_id": rt["routine_id"],
                    "schedule_expr": expr,
                    "all_day": cnt > 1,
                })
        return projected

    def get_calendar_window(self, start_date: str = "", end_date: str = "", days: int = 35, board_id: str = "") -> Dict[str, Any]:
        """Return a unified timeline of Events, Sprint Windows, Planned Tasks, Deadlines, Routines & SRS Reviews."""
        _ = self.store
        now_dt = _now_utc()
        today_s = now_dt.strftime("%Y-%m-%d")
        s_dt = _parse_ymd(start_date) if start_date else (now_dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now_dt.weekday()))
        if s_dt is None:
            return {"ok": False, "error": f"Invalid `start_date` '{start_date}'; expected YYYY-MM-DD."}
        span = max(1, min(180, int(days or 35)))
        e_dt = (_parse_ymd(end_date) if end_date else None) or (s_dt + timedelta(days=span - 1))
        s_ymd, e_ymd = s_dt.strftime("%Y-%m-%d"), e_dt.strftime("%Y-%m-%d")

        items: List[Dict[str, Any]] = []
        for ev in self.list_events(s_ymd, e_ymd):
            if board_id and ev.get("board_id") and ev["board_id"] != board_id:
                continue
            d_key = _norm_date_prefix(ev["start_at"])
            t_part = ev["start_at"][11:16] if len(ev["start_at"]) >= 16 and not ev["all_day"] else ""
            items.append({
                "id": ev["event_id"], "date": d_key, "time": t_part, "source_type": "event",
                "kind": ev["kind"], "title": ev["title"], "all_day": ev["all_day"],
                "board_id": ev["board_id"], "card_id": ev["card_id"], "notes": ev["notes"], "end_at": ev["end_at"],
            })

        agile = get_agile_manager(store=self.store)
        sprints = [sp for sp in agile.list_sprints(24) if sp["start_date"] <= e_ymd and sp["end_date"] >= s_ymd]
        board_view = agile.get_board_view(board_id or "all-active-sprint")
        hz_days = {"now": 30, "next": 90, "later": 180, "someday": 365}
        epics: List[Dict[str, Any]] = []
        for ep in agile.list_epics(board_id=board_id):
            eff_s = _norm_date_prefix(ep.get("start_date") or "") or _norm_date_prefix(ep.get("created_at") or "") or today_s
            t_ymd = _norm_date_prefix(ep.get("target_date") or "")
            s_obj = _parse_ymd(eff_s) or now_dt
            proj_end = t_ymd or (s_obj + timedelta(days=hz_days.get(ep.get("horizon") or "later", 90))).strftime("%Y-%m-%d")
            epics.append({**ep, "effective_start_date": eff_s, "roadmap_end_date": max(eff_s, proj_end)})
            if t_ymd and s_ymd <= t_ymd <= e_ymd:
                items.append({
                    "id": f"epic_{ep['epic_id']}", "date": t_ymd, "time": "", "source_type": "epic_milestone",
                    "kind": ep["horizon"], "title": f"Epic Target: {ep['title']}", "all_day": True,
                    "board_id": ep["board_id"], "epic_id": ep["epic_id"],
                })
        for c in agile.list_dated_cards(board_id=board_id):
            if c["planned_date"] and s_ymd <= c["planned_date"] <= e_ymd:
                items.append({
                    "id": f"plan_{c['card_id']}", "date": c["planned_date"], "time": "", "source_type": "sprint_task",
                    "kind": c["day_slot"], "title": c["title"], "all_day": True, "board_id": c["board_id"],
                    "card_id": c["card_id"], "epic_id": c.get("epic_id", ""), "status": c["status"], "effort_size": c["effort_size"],
                })
            if c["due_date"] and s_ymd <= c["due_date"] <= e_ymd:
                items.append({
                    "id": f"due_{c['card_id']}", "date": c["due_date"], "time": "", "source_type": "deadline",
                    "kind": "due_date", "title": f"Due: {c['title']}", "all_day": True, "board_id": c["board_id"],
                    "card_id": c["card_id"], "epic_id": c.get("epic_id", ""), "status": c["status"], "effort_size": c["effort_size"],
                })

        if not board_id:
            items.extend(self._project_routines(s_dt, e_dt))
            edu = get_education_engine(store=self.store)
            _ = edu.store
            srs_by_day: Dict[str, int] = {}
            for (due_iso,) in self.store._fetch_rows("SELECT due_at FROM education_mastery"):
                d_s = max(today_s, str(due_iso)[:10])
                if s_ymd <= d_s <= e_ymd:
                    srs_by_day[d_s] = srs_by_day.get(d_s, 0) + 1
            for d_s, cnt in sorted(srs_by_day.items()):
                items.append({
                    "id": f"srs_{d_s}", "date": d_s, "time": "", "source_type": "study_review",
                    "kind": "srs_due", "title": f"Study Review: {cnt} card{'s' if cnt != 1 else ''} due", "all_day": True, "count": cnt,
                })

        items.sort(key=lambda x: (x["date"], "00:00" if x.get("all_day") else (x.get("time") or "23:59"), x["source_type"], x["title"]))
        by_date: Dict[str, List[Dict[str, Any]]] = {}
        cur_d = s_dt
        while cur_d <= e_dt:
            by_date[cur_d.strftime("%Y-%m-%d")] = []
            cur_d += timedelta(days=1)
        for it in items:
            by_date.setdefault(it["date"], []).append(it)

        return {
            "ok": True, "today": today_s, "start_date": s_ymd, "end_date": e_ymd,
            "sprints": sprints, "epics": epics, "active_sprint": board_view.get("active_sprint"),
            "items": items, "by_date": by_date,
            "summary": f"Calendar `{s_ymd}` .. `{e_ymd}`: {len(items)} item(s), {len(sprints)} sprint window(s), {len(epics)} epic(s).",
        }


def get_calendar_manager(store: Optional[MemoryStore] = None) -> CalendarManager:
    return CalendarManager(store=store)


def execute_calendar_action(p: Dict[str, Any], store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """Unified CoreAction handler for `calendar` (`window`, `add_event`, `delete_event`, `list_events`)."""
    mgr = get_calendar_manager(store=store)
    sub = str(p.get("subaction") or p.get("action") or "window").strip().lower()
    if sub in ("add_event", "save_event", "upsert_event", "schedule"):
        return mgr.upsert_event(
            title=str(p.get("title") or ""), start_at=str(p.get("start_at") or p.get("date") or ""),
            end_at=str(p.get("end_at") or ""), kind=str(p.get("kind") or "appointment"),
            all_day=bool(p.get("all_day", False)), board_id=str(p.get("board_id") or ""),
            card_id=str(p.get("card_id") or ""), notes=str(p.get("notes") or ""), event_id=str(p.get("event_id") or ""),
        )
    if sub == "delete_event":
        return mgr.delete_event(str(p.get("event_id") or ""))
    if sub == "list_events":
        evs = mgr.list_events(str(p.get("start_date") or ""), str(p.get("end_date") or ""))
        return {"ok": True, "events": evs, "summary": f"{len(evs)} explicit calendar event(s)."}
    days = 7 if sub == "week" else (1 if sub == "today" else int(p.get("days") or 35))
    start_d = _now_utc().strftime("%Y-%m-%d") if sub == "today" else str(p.get("start_date") or "")
    win = mgr.get_calendar_window(start_date=start_d, end_date=str(p.get("end_date") or ""), days=days, board_id=str(p.get("board_id") or ""))
    if win.get("ok"):
        win["output"] = "\n".join(
            f"  - {it['date']}" + (f" {it['time']}" if it.get("time") else "") + f" [{it['source_type']}:{it['kind']}] {it['title']}"
            for it in win["items"][:30]
        ) or "  (No scheduled items in window)"
    return win


def parse_calendar_slash(parts: List[str]) -> Dict[str, Any]:
    """Parse `/calendar` CLI/REPL slash arguments into `execute_calendar_action` params."""
    sub, rest = (parts[0].lower() if parts else "week"), parts[1:]
    if sub in ("today", "week", "month"):
        return {"subaction": sub, "days": 35 if sub == "month" else (7 if sub == "week" else 1)}
    if sub == "add" and len(rest) >= 2:
        return {"subaction": "add_event", "start_at": rest[0], "title": " ".join(rest[1:])}
    return {"subaction": "window", "days": 14}
