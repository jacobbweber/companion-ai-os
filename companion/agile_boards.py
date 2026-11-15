"""Universal Agile, Multi-Board Kanban, Horizon Epics, Checklists & Anti-Procrastination Engine (`companion/agile_boards.py` — Domain Layer)."""

import json, os, re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from companion.config import SELF_PROJECT_ID, settings
from companion.memory_store import MemoryStore, get_memory_store

__all__ = [
    "AgileBoardManager", "BOARD_KINDS", "CARD_STATUSES", "DAY_SLOTS", "EFFORT_SLOTS",
    "HORIZONS", "MASTER_SPRINT_BOARD_ID", "execute_agile_action", "format_agile_hud",
    "get_agile_manager", "parse_agile_slash", "sync_workflow_to_agile_board",
]

MASTER_SPRINT_BOARD_ID = "all-active-sprint"
BOARD_KINDS: Tuple[str, ...] = ("life", "work", "project", "software")
CARD_STATUSES: Tuple[str, ...] = ("backlog", "sprint_todo", "in_progress", "blocked", "verify", "done")
HORIZONS: Tuple[str, ...] = ("now", "next", "later", "someday")
DAY_SLOTS: Tuple[str, ...] = ("weekday", "weekend", "any")
SPRINT_STATUSES: Tuple[str, ...] = ("planning", "active", "completed")
EFFORT_SLOTS: Dict[str, int] = {"quick": 1, "half_day": 2, "full_day": 4, "multi_day": 8}
AGING_DAYS_THRESHOLD = 14

DEFAULT_BOARDS: Tuple[Tuple[str, str, str, str, str], ...] = (
    ("life-home", "Life & Home", "life", "", "Home maintenance, vehicles, errands, and personal life projects."),
    ("work-career", "Work & Career", "work", "", "Work preparation, study blocks, and professional deliverables."),
    ("software-companion", "Software: Companion AI OS", "software", SELF_PROJECT_ID, "Live engineering Kanban board for Companion builds."),
)
_CARD_COLS = [
    "card_id", "board_id", "epic_id", "sprint_id", "title", "description", "status", "horizon", "priority",
    "effort_size", "day_slot", "planned_date", "due_date", "blocked_reason", "depends_on_json", "tags_json",
    "checklist_json", "rollover_count", "workflow_id", "task_id", "created_at", "last_touched_at", "completed_at",
]
_EPIC_COLS = ["epic_id", "board_id", "title", "horizon", "status", "start_date", "target_date", "wiki_path", "summary", "created_at", "updated_at"]
_SPRINT_COLS = ["sprint_id", "title", "goal_summary", "start_date", "end_date", "status", "weekday_capacity_slots", "weekend_capacity_slots", "retro_notes", "created_at", "updated_at"]

AGILE_DDL: Tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS agile_boards (board_id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT DEFAULT 'life', project_id TEXT DEFAULT '', description TEXT DEFAULT '', archived INTEGER DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS agile_epics (epic_id TEXT PRIMARY KEY, board_id TEXT NOT NULL, title TEXT NOT NULL, horizon TEXT DEFAULT 'next', status TEXT DEFAULT 'active', start_date TEXT DEFAULT '', target_date TEXT DEFAULT '', wiki_path TEXT DEFAULT '', summary TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS agile_sprints (sprint_id TEXT PRIMARY KEY, title TEXT NOT NULL, goal_summary TEXT DEFAULT '', start_date TEXT NOT NULL, end_date TEXT NOT NULL, status TEXT DEFAULT 'active', weekday_capacity_slots INTEGER DEFAULT 10, weekend_capacity_slots INTEGER DEFAULT 6, retro_notes TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS agile_cards (card_id TEXT PRIMARY KEY, board_id TEXT NOT NULL, epic_id TEXT DEFAULT '', sprint_id TEXT DEFAULT '', title TEXT NOT NULL, description TEXT DEFAULT '', status TEXT DEFAULT 'backlog', horizon TEXT DEFAULT 'next', priority INTEGER DEFAULT 3, effort_size TEXT DEFAULT 'quick', day_slot TEXT DEFAULT 'any', planned_date TEXT DEFAULT '', due_date TEXT DEFAULT '', blocked_reason TEXT DEFAULT '', depends_on_json TEXT DEFAULT '[]', tags_json TEXT DEFAULT '[]', checklist_json TEXT DEFAULT '[]', rollover_count INTEGER DEFAULT 0, workflow_id INTEGER, task_id INTEGER, created_at TEXT NOT NULL, last_touched_at TEXT NOT NULL, completed_at TEXT DEFAULT '')",
    "CREATE INDEX IF NOT EXISTS idx_agile_cards_board ON agile_cards(board_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_agile_cards_sprint ON agile_cards(sprint_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_agile_cards_dates ON agile_cards(planned_date, due_date)",
)

_STATUS_ALIASES = {"todo": "sprint_todo", "pending": "sprint_todo", "open": "backlog", "active": "in_progress", "doing": "in_progress", "working": "in_progress", "review": "verify", "testing": "verify", "completed": "done", "complete": "done", "closed": "done"}
_EFFORT_ALIASES = {"15m": "quick", "30m": "quick", "small": "quick", "xs": "quick", "s": "quick", "medium": "half_day", "m": "half_day", "half-day": "half_day", "large": "full_day", "l": "full_day", "full-day": "full_day", "xl": "multi_day", "multi-day": "multi_day"}
_SLOT_ALIASES = {"weekdays": "weekday", "workday": "weekday", "evening": "weekday", "evenings": "weekday", "weekends": "weekend", "saturday": "weekend", "sunday": "weekend", "flexible": "any", "either": "any"}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now_utc()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(raw: str, prefix: str = "item") -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", (raw or "").strip().lower()).strip("-")[:56] or f"{prefix}-{os.urandom(3).hex()}"


def _make_id(prefix: str) -> str:
    return f"{prefix}_{_now_utc().strftime('%Y%m%d')}_{os.urandom(3).hex()}"


def _opt_str(val: Optional[str], prev: str) -> str:
    if val is None:
        return prev
    cleaned = str(val).strip()
    return "" if cleaned.lower() in ("clear", "none") else cleaned


def _as_str_list(val: Any) -> List[str]:
    if not val:
        return []
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                pass
        return [x.strip() for x in s.split(",") if x.strip()]
    return [str(x).strip() for x in val if str(x).strip()] if isinstance(val, (list, tuple, set)) else []


def _as_checklist(val: Any) -> List[Dict[str, Any]]:
    raw: Any = val
    if not raw:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            raw = json.loads(s) if s.startswith("[") else [ln.strip() for ln in s.splitlines() if ln.strip()]
        except Exception:
            raw = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if not isinstance(raw, (list, tuple)):
        return []
    items: List[Dict[str, Any]] = []
    for idx, entry in enumerate(raw):
        if isinstance(entry, dict):
            txt = str(entry.get("text") or entry.get("title") or entry.get("label") or "").strip()
            if txt:
                items.append({"id": str(entry.get("id") or f"chk_{idx + 1}_{os.urandom(2).hex()}").strip(), "text": txt[:200], "done": bool(entry.get("done") or entry.get("checked"))})
        else:
            txt = str(entry or "").strip()
            done = txt.lower().startswith(("[x] ", "- [x] "))
            txt = re.sub(r"^(?:-\s*)?(?:\[[x\s]\]\s*)?", "", txt, flags=re.IGNORECASE).strip()
            if txt:
                items.append({"id": f"chk_{idx + 1}_{os.urandom(2).hex()}", "text": txt[:200], "done": done})
    return items


def _days_since(iso_str: str, now_dt: datetime) -> int:
    clean = (iso_str or "").strip().rstrip("Z")
    if not clean:
        return 0
    try:
        parsed = datetime.fromisoformat(clean.replace(" ", "T"))
        return max(0, (now_dt - (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc))).days)
    except Exception:
        return 0


class AgileBoardManager:
    """Thread-safe SQLite Agile, Multi-Board Kanban, Horizon, Capacity & Anti-Procrastination Engine."""

    def __init__(self, store: Optional[MemoryStore] = None):
        self._store = store

    @property
    def store(self) -> MemoryStore:
        mem = self._store or get_memory_store()

        def _init_agile(m: MemoryStore) -> None:
            now_s = _fmt_iso()
            for ddl in AGILE_DDL:
                m.conn.execute(ddl)
            if "start_date" not in {str(r[1]) for r in m.conn.execute("PRAGMA table_info(agile_epics)").fetchall()}:
                m.conn.execute("ALTER TABLE agile_epics ADD COLUMN start_date TEXT DEFAULT ''")
            if "checklist_json" not in {str(r[1]) for r in m.conn.execute("PRAGMA table_info(agile_cards)").fetchall()}:
                m.conn.execute("ALTER TABLE agile_cards ADD COLUMN checklist_json TEXT DEFAULT '[]'")
            for bid, btitle, bkind, pid, desc in DEFAULT_BOARDS:
                m.conn.execute("INSERT OR IGNORE INTO agile_boards (board_id, title, kind, project_id, description, archived, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)", (bid, btitle, bkind, pid, desc, now_s, now_s))
            self._dedupe_epics_locked(m)

        mem.ensure_schema("agile_boards", _init_agile)
        return mem

    @staticmethod
    def _dedupe_epics_locked(mem: MemoryStore) -> None:
        rows = mem.conn.execute("SELECT epic_id, board_id, title, start_date, target_date, wiki_path, summary FROM agile_epics ORDER BY created_at ASC, rowid ASC").fetchall()
        seen: Dict[Tuple[str, str], Tuple[str, str, str, str, str]] = {}
        for eid, bid, title, s_dt, t_dt, w_path, summ in rows:
            key = (str(bid or "").strip().lower(), str(title or "").strip().lower())
            if not key[1]:
                continue
            if key not in seen:
                seen[key] = (str(eid), str(s_dt or ""), str(t_dt or ""), str(w_path or ""), str(summ or ""))
                continue
            keep_id, k_s, k_t, k_w, k_sum = seen[key]
            merged = (k_s or str(s_dt or ""), k_t or str(t_dt or ""), k_w or str(w_path or ""), k_sum or str(summ or ""))
            if merged != (k_s, k_t, k_w, k_sum):
                mem.conn.execute("UPDATE agile_epics SET start_date=?, target_date=?, wiki_path=?, summary=? WHERE epic_id=?", (*merged, keep_id))
                seen[key] = (keep_id, *merged)
            mem.conn.execute("UPDATE agile_cards SET epic_id = ? WHERE epic_id = ?", (keep_id, str(eid)))
            mem.conn.execute("DELETE FROM agile_epics WHERE epic_id = ?", (str(eid),))

    def list_boards(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        cols = ["board_id", "title", "kind", "project_id", "description", "archived", "created_at", "updated_at"]
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM agile_boards {'' if include_archived else 'WHERE archived = 0 '}ORDER BY kind ASC, title ASC")
        counts = {str(r[0]): int(r[1]) for r in self.store._fetch_rows("SELECT board_id, COUNT(*) FROM agile_cards WHERE status != 'done' GROUP BY board_id")}
        for b in rows:
            b["archived"], b["open_cards"] = bool(b["archived"]), counts.get(b["board_id"], 0)
        return rows

    def upsert_board(self, title: str, kind: str = "project", board_id: str = "", project_id: str = "", description: str = "", archived: bool = False) -> Dict[str, Any]:
        k, clean_title = (kind or "project").strip().lower(), (title or board_id).strip()
        if k not in BOARD_KINDS or not clean_title:
            return {"ok": False, "error": f"Board `title` and valid `kind` ({', '.join(BOARD_KINDS)}) are required."}
        bid = _slug(board_id or clean_title, "board")
        if bid == MASTER_SPRINT_BOARD_ID:
            return {"ok": False, "error": f"`{MASTER_SPRINT_BOARD_ID}` is reserved for the Master Active Sprint view."}
        now_s = _fmt_iso()
        with self.store._lock:
            self.store.conn.execute("INSERT INTO agile_boards (board_id, title, kind, project_id, description, archived, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(board_id) DO UPDATE SET title=excluded.title, kind=excluded.kind, project_id=excluded.project_id, description=excluded.description, archived=excluded.archived, updated_at=excluded.updated_at", (bid, clean_title[:100], k, (project_id or "").strip(), (description or "").strip()[:300], 1 if archived else 0, now_s, now_s))
            self.store.conn.commit()
        return {"ok": True, "board_id": bid, "summary": f"Saved Kanban board `{clean_title}` (`{bid}`, kind=`{k}`)."}

    def ensure_project_board(self, project_id: str) -> str:
        pid = (project_id or SELF_PROJECT_ID).strip()
        if pid == SELF_PROJECT_ID:
            _ = self.store
            return "software-companion"
        bid = _slug(f"software-{pid}", "software")
        if not self.store._fetch_rows("SELECT board_id FROM agile_boards WHERE board_id = ?", (bid,)):
            self.upsert_board(title=f"Software: {pid}", kind="software", board_id=bid, project_id=pid, description=f"Engineering board for `{pid}`.")
        return bid

    def _get_raw_epic(self, epic_id: str) -> Optional[Dict[str, Any]]:
        rows = self.store._fetch_dicts(_EPIC_COLS, f"SELECT {', '.join(_EPIC_COLS)} FROM agile_epics WHERE epic_id = ?", (epic_id.strip(),))
        return rows[0] if rows else None

    def upsert_epic(self, title: str = "", board_id: str = "", horizon: str = "", target_date: Optional[str] = None, wiki_path: Optional[str] = None, summary: Optional[str] = None, epic_id: str = "", status: str = "", start_date: Optional[str] = None) -> Dict[str, Any]:
        ex = self._get_raw_epic(epic_id) if epic_id.strip() else None
        bid = _slug(board_id or (ex or {}).get("board_id") or "life-home", "life-home")
        bid = ((ex or {}).get("board_id") or "life-home") if bid == MASTER_SPRINT_BOARD_ID else bid
        clean_title = (title or (ex or {}).get("title") or "").strip()
        if not ex and clean_title:
            dup = self.store._fetch_rows("SELECT epic_id FROM agile_epics WHERE board_id = ? AND LOWER(TRIM(title)) = ? LIMIT 1", (bid, clean_title.lower()))
            ex = self._get_raw_epic(str(dup[0][0])) if dup else None
            clean_title = str(ex["title"]) if ex else clean_title
        hz = (horizon or (ex or {}).get("horizon") or "later").strip().lower()
        if not clean_title or hz not in HORIZONS:
            return {"ok": False, "error": f"Epic `title` and valid `horizon` ({', '.join(HORIZONS)}) are required."}
        eid, now_s, c_at = (str(ex["epic_id"]) if ex else (_slug(epic_id, "epic") if epic_id.strip() else _make_id("ep"))), _fmt_iso(), str((ex or {}).get("created_at") or _fmt_iso())
        s_date, t_date = _opt_str(start_date, str((ex or {}).get("start_date") or ""))[:20], _opt_str(target_date, str((ex or {}).get("target_date") or ""))[:20]
        w_path, summ, st = _opt_str(wiki_path, str((ex or {}).get("wiki_path") or "")), _opt_str(summary, str((ex or {}).get("summary") or ""))[:400], (status or (ex or {}).get("status") or "active").strip()
        with self.store._lock:
            self.store.conn.execute("INSERT INTO agile_epics (epic_id, board_id, title, horizon, status, start_date, target_date, wiki_path, summary, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(epic_id) DO UPDATE SET board_id=excluded.board_id, title=excluded.title, horizon=excluded.horizon, status=excluded.status, start_date=excluded.start_date, target_date=excluded.target_date, wiki_path=excluded.wiki_path, summary=excluded.summary, updated_at=excluded.updated_at", (eid, bid, clean_title[:120], hz, st, s_date, t_date, w_path, summ, c_at, now_s))
            self.store.conn.commit()
        ep_obj = next((e for e in self.list_epics() if e["epic_id"] == eid), {"epic_id": eid})
        return {"ok": True, "epic_id": eid, "epic": ep_obj, "summary": f"Saved Epic `{clean_title}` (`{eid}`, horizon=`{hz}`, board=`{bid}`)."}

    def delete_epic(self, epic_id: str) -> Dict[str, Any]:
        eid = (epic_id or "").strip()
        with self.store._lock:
            self.store.conn.execute("UPDATE agile_cards SET epic_id = '' WHERE epic_id = ?", (eid,))
            cur = self.store.conn.execute("DELETE FROM agile_epics WHERE epic_id = ?", (eid,))
            self.store.conn.commit()
        return {"ok": True, "epic_id": eid, "summary": f"Deleted Epic `{eid}`."} if cur.rowcount else {"ok": False, "error": f"Epic `{eid}` not found."}

    def list_epics(self, board_id: str = "", horizon: str = "") -> List[Dict[str, Any]]:
        clauses, params = [], []
        if board_id and board_id != MASTER_SPRINT_BOARD_ID:
            clauses.append("board_id = ?")
            params.append(_slug(board_id))
        if horizon:
            clauses.append("horizon = ?")
            params.append(horizon.strip().lower())
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = self.store._fetch_dicts(_EPIC_COLS, f"SELECT {', '.join(_EPIC_COLS)} FROM agile_epics {where}ORDER BY created_at ASC", tuple(params))
        board_names = {b["board_id"]: b["title"] for b in self.list_boards(include_archived=True)}
        card_stats: Dict[str, List[int]] = {}
        for r_eid, r_st in self.store._fetch_rows("SELECT epic_id, status FROM agile_cards WHERE epic_id != ''"):
            pair = card_stats.setdefault(str(r_eid), [0, 0])
            pair[0] += 1
            pair[1] += (1 if str(r_st) == "done" else 0)
        hz_rank = {h: idx for idx, h in enumerate(HORIZONS)}
        for ep in rows:
            tot, done = card_stats.get(ep["epic_id"], [0, 0])
            ep.update({"board_title": board_names.get(ep["board_id"], ep["board_id"]), "card_count": tot, "done_card_count": done, "progress_pct": round(100 * done / tot) if tot else 0})
        rows.sort(key=lambda e: (hz_rank.get(e["horizon"], 9), e.get("target_date") or "9999-99-99", e["created_at"]))
        return rows

    def get_active_sprint(self) -> Optional[Dict[str, Any]]:
        rows = self.store._fetch_dicts(_SPRINT_COLS, f"SELECT {', '.join(_SPRINT_COLS)} FROM agile_sprints WHERE status = 'active' ORDER BY start_date DESC, created_at DESC LIMIT 1")
        return rows[0] if rows else None

    def list_sprints(self, limit: int = 12) -> List[Dict[str, Any]]:
        return self.store._fetch_dicts(_SPRINT_COLS, f"SELECT {', '.join(_SPRINT_COLS)} FROM agile_sprints ORDER BY start_date DESC, created_at DESC LIMIT ?", (limit,))

    def upsert_sprint(self, title: str = "", goal_summary: str = "", start_date: str = "", end_date: str = "", weekday_capacity_slots: int = 10, weekend_capacity_slots: int = 6, status: str = "active", sprint_id: str = "", retro_notes: str = "") -> Dict[str, Any]:
        st = (status or "active").strip().lower()
        if st not in SPRINT_STATUSES:
            return {"ok": False, "error": f"Invalid sprint `status` '{status}'. Valid: {', '.join(SPRINT_STATUSES)}."}
        now_dt = _now_utc()
        s_date = (start_date or now_dt.strftime("%Y-%m-%d")).strip()[:10]
        try:
            s_dt = datetime.strptime(s_date, "%Y-%m-%d")
        except ValueError:
            return {"ok": False, "error": f"Invalid `start_date` '{s_date}'; use YYYY-MM-DD."}
        e_date = (end_date or (s_dt + timedelta(days=6)).strftime("%Y-%m-%d")).strip()[:10]
        sid, clean_title, now_s = (_slug(sprint_id, "spr") if sprint_id else _make_id("spr")), (title or f"Sprint ({s_date} .. {e_date})").strip()[:100], _fmt_iso(now_dt)
        with self.store._lock:
            if st == "active":
                self.store.conn.execute("UPDATE agile_sprints SET status = 'completed', updated_at = ? WHERE status = 'active' AND sprint_id != ?", (now_s, sid))
            self.store.conn.execute("INSERT INTO agile_sprints (sprint_id, title, goal_summary, start_date, end_date, status, weekday_capacity_slots, weekend_capacity_slots, retro_notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(sprint_id) DO UPDATE SET title=excluded.title, goal_summary=excluded.goal_summary, start_date=excluded.start_date, end_date=excluded.end_date, status=excluded.status, weekday_capacity_slots=excluded.weekday_capacity_slots, weekend_capacity_slots=excluded.weekend_capacity_slots, retro_notes=excluded.retro_notes, updated_at=excluded.updated_at", (sid, clean_title, (goal_summary or "").strip()[:300], s_date, e_date, st, max(1, int(weekday_capacity_slots)), max(1, int(weekend_capacity_slots)), (retro_notes or "").strip()[:1000], now_s, now_s))
            self.store.conn.commit()
        return {"ok": True, "sprint_id": sid, "sprint": self.get_active_sprint() if st == "active" else None, "summary": f"Saved Sprint `{clean_title}` (`{sid}`, {s_date} -> {e_date}, status=`{st}`)."}

    def _get_raw_card(self, card_id: str) -> Optional[Dict[str, Any]]:
        rows = self.store._fetch_dicts(_CARD_COLS, f"SELECT {', '.join(_CARD_COLS)} FROM agile_cards WHERE card_id = ?", (card_id.strip(),))
        return rows[0] if rows else None

    def _enrich_cards(self, raw_cards: List[Dict[str, Any]], now_dt: Optional[datetime] = None) -> List[Dict[str, Any]]:
        ref = now_dt or _now_utc()
        board_names = {b["board_id"]: b["title"] for b in self.list_boards(include_archived=True)}
        epic_names = {str(r[0]): str(r[1]) for r in self.store._fetch_rows("SELECT epic_id, title FROM agile_epics")}
        status_by_id = {str(r[0]): (str(r[1]), str(r[2])) for r in self.store._fetch_rows("SELECT card_id, status, title FROM agile_cards")}
        enriched: List[Dict[str, Any]] = []
        for c in raw_cards:
            deps, tags, chk = _as_str_list(c.pop("depends_on_json", "[]")), _as_str_list(c.pop("tags_json", "[]")), _as_checklist(c.pop("checklist_json", "[]"))
            unresolved = [{"card_id": d, "title": status_by_id[d][1]} for d in deps if d in status_by_id and status_by_id[d][0] != "done"]
            untouched_d, age_d, eff = _days_since(c.get("last_touched_at") or c.get("created_at") or "", ref), _days_since(c.get("created_at") or "", ref), c.get("effort_size") or "quick"
            is_aging = c.get("status") != "done" and (untouched_d >= AGING_DAYS_THRESHOLD or int(c.get("rollover_count") or 0) >= 2)
            enriched.append({
                **c, "board_title": board_names.get(c["board_id"], c["board_id"]), "epic_title": epic_names.get(str(c.get("epic_id") or ""), ""),
                "depends_on": deps, "unresolved_deps": unresolved, "tags": tags, "checklist": chk, "checklist_done": sum(1 for it in chk if it.get("done")),
                "checklist_total": len(chk), "slot_points": EFFORT_SLOTS.get(eff, 1), "age_days": age_d, "untouched_days": untouched_d, "is_aging": is_aging,
            })
        return enriched

    def upsert_card(
        self, title: str = "", board_id: str = "", card_id: str = "", description: Optional[str] = None, status: str = "", horizon: str = "",
        priority: int = 0, effort_size: str = "", day_slot: str = "", planned_date: Optional[str] = None, due_date: Optional[str] = None, blocked_reason: Optional[str] = None,
        depends_on: Any = None, tags: Any = None, checklist: Any = None, sprint_id: Optional[str] = None, epic_id: Optional[str] = None,
        created_at_override: str = "", force: bool = False, workflow_id: Optional[int] = None, task_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        ex = self._get_raw_card(card_id) if card_id else None
        clean_title = (title or (ex or {}).get("title") or "").strip()
        if not clean_title:
            return {"ok": False, "error": "Card `title` is required."}
        active_spr = self.get_active_sprint()
        raw_st = (status or (ex or {}).get("status") or "backlog").strip().lower()
        st = "backlog" if raw_st in ("todo", "pending") and not active_spr else _STATUS_ALIASES.get(raw_st, raw_st)
        eff = _EFFORT_ALIASES.get((effort_size or (ex or {}).get("effort_size") or "quick").strip().lower(), (effort_size or (ex or {}).get("effort_size") or "quick").strip().lower())
        slot = _SLOT_ALIASES.get((day_slot or (ex or {}).get("day_slot") or "any").strip().lower(), (day_slot or (ex or {}).get("day_slot") or "any").strip().lower())
        if st not in CARD_STATUSES or eff not in EFFORT_SLOTS or slot not in DAY_SLOTS:
            return {"ok": False, "error": f"Invalid status (`{st}` -> {CARD_STATUSES}), effort_size (`{eff}` -> {tuple(EFFORT_SLOTS)}), or day_slot (`{slot}` -> {DAY_SLOTS})."}
        deps = _as_str_list(depends_on) if depends_on is not None else _as_str_list((ex or {}).get("depends_on_json"))
        if st in ("in_progress", "verify", "done") and deps and not force:
            inc = [f"{d} ({r[0]})" for d in deps for r in self.store._fetch_rows("SELECT title, status FROM agile_cards WHERE card_id = ?", (d,)) if str(r[1]) != "done"]
            if inc:
                return {"ok": False, "error": f"DEPENDENCY GUARD: Cannot move `{clean_title}` to `{st}` until prerequisite card(s) finish: {', '.join(inc)}."}
        eff_sprint = "" if (st == "backlog" and not sprint_id) else (sprint_id if sprint_id is not None else ((ex or {}).get("sprint_id") or (active_spr["sprint_id"] if (active_spr and st in ("sprint_todo", "in_progress", "verify")) else ""))).strip()
        hz = (horizon or ("now" if (eff_sprint or st in ("sprint_todo", "in_progress", "verify")) else ((ex or {}).get("horizon") or "next"))).strip().lower()
        hz = hz if hz in HORIZONS else "next"
        cid = card_id.strip() if ex else (_slug(card_id, "card") if card_id else _make_id("crd"))
        bid = _slug(board_id or (ex or {}).get("board_id") or "life-home", "life-home")
        bid = ((ex or {}).get("board_id") or "life-home") if bid == MASTER_SPRINT_BOARD_ID else bid
        now_s = _fmt_iso()
        c_at, t_at = created_at_override or (ex or {}).get("created_at") or now_s, created_at_override or now_s
        comp_at = now_s if st == "done" and (ex or {}).get("status") != "done" else ("" if st != "done" else str((ex or {}).get("completed_at") or now_s))
        tag_list = _as_str_list(tags) if tags is not None else _as_str_list((ex or {}).get("tags_json"))
        chk_list = _as_checklist(checklist) if checklist is not None else _as_checklist((ex or {}).get("checklist_json"))
        desc, ep_id = _opt_str(description, str((ex or {}).get("description") or "")), _opt_str(epic_id, str((ex or {}).get("epic_id") or ""))
        p_date, d_date = _opt_str(planned_date, str((ex or {}).get("planned_date") or "")), _opt_str(due_date, str((ex or {}).get("due_date") or ""))
        b_reason = _opt_str(blocked_reason, str((ex or {}).get("blocked_reason") or "")) if st == "blocked" else ""
        prio = max(1, min(5, int(priority if priority else ((ex or {}).get("priority") or 3))))
        with self.store._lock:
            self.store.conn.execute(
                "INSERT INTO agile_cards (card_id, board_id, epic_id, sprint_id, title, description, status, horizon, priority, effort_size, day_slot, planned_date, due_date, blocked_reason, depends_on_json, tags_json, checklist_json, rollover_count, workflow_id, task_id, created_at, last_touched_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(card_id) DO UPDATE SET board_id=excluded.board_id, epic_id=excluded.epic_id, sprint_id=excluded.sprint_id, title=excluded.title, description=excluded.description, status=excluded.status, horizon=excluded.horizon, priority=excluded.priority, effort_size=excluded.effort_size, day_slot=excluded.day_slot, planned_date=excluded.planned_date, due_date=excluded.due_date, blocked_reason=excluded.blocked_reason, depends_on_json=excluded.depends_on_json, tags_json=excluded.tags_json, checklist_json=excluded.checklist_json, workflow_id=COALESCE(excluded.workflow_id, agile_cards.workflow_id), task_id=COALESCE(excluded.task_id, agile_cards.task_id), last_touched_at=excluded.last_touched_at, completed_at=excluded.completed_at",
                (cid, bid, ep_id, eff_sprint, clean_title[:160], desc[:1200], st, hz, prio, eff, slot, p_date[:10], d_date[:10], b_reason[:240], json.dumps(deps), json.dumps(tag_list), json.dumps(chk_list), int((ex or {}).get("rollover_count") or 0), workflow_id, task_id, c_at, t_at, comp_at),
            )
            self.store.conn.commit()
        saved = self._get_raw_card(cid)
        warn = " (Tip: `multi_day` card — slice a `<30m` first step for your sprint!)" if eff == "multi_day" else ""
        return {"ok": True, "card_id": cid, "card": self._enrich_cards([saved])[0] if saved else {"card_id": cid}, "summary": f"Saved card `{clean_title}` (`{cid}` -> `{bid}` [{st} | {eff}/{slot}]){warn}"}

    def move_card(self, card_id: str, status: str, blocked_reason: str = "", sprint_id: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        raw = self._get_raw_card(card_id)
        if not raw:
            return {"ok": False, "error": f"Card `{card_id}` not found."}
        return self.upsert_card(card_id=card_id, title=raw["title"], board_id=raw["board_id"], status=status, effort_size=raw["effort_size"], day_slot=raw["day_slot"], priority=int(raw["priority"]), blocked_reason=blocked_reason or raw.get("blocked_reason", ""), sprint_id=sprint_id, force=force)

    def delete_card(self, card_id: str) -> Dict[str, Any]:
        cid = (card_id or "").strip()
        with self.store._lock:
            cur = self.store.conn.execute("DELETE FROM agile_cards WHERE card_id = ?", (cid,))
            self.store.conn.commit()
        return {"ok": bool(cur.rowcount), "card_id": cid, "summary": f"Deleted card `{cid}`."} if cur.rowcount else {"ok": False, "error": f"Card `{cid}` not found."}

    def slice_unblocker_card(self, stalled_card_id: str, first_step_title: str = "", day_slot: str = "weekday", planned_date: str = "", board_id: str = "", description: str = "") -> Dict[str, Any]:
        """Break an aging or `multi_day` procrastinated card into a `<30m` (`quick`) sprint unblocker card."""
        parent = self._get_raw_card(stalled_card_id) if stalled_card_id.strip() else None
        if not parent and (stalled_card_id.strip() or first_step_title.strip()):
            probe = f"{stalled_card_id} {first_step_title}".lower()
            toks = [w for w in re.findall(r"[a-z0-9]{4,}", probe) if w not in {"unblocker", "first", "quick", "minute", "minutes", "slice", "step", "task", "card"}]
            open_rows = self.store._fetch_dicts(_CARD_COLS, f"SELECT {', '.join(_CARD_COLS)} FROM agile_cards WHERE status != 'done' ORDER BY created_at ASC")
            parent = next((r for r in open_rows if stalled_card_id.strip().lower() in r["title"].lower()), None) if stalled_card_id.strip() else (next((r for r in open_rows if "unblocker" not in r.get("tags_json", "") and any(t in r["title"].lower() for t in toks)), None) if toks else None)
        active_spr = self.get_active_sprint()
        norm_slot = _SLOT_ALIASES.get((day_slot or "weekday").strip().lower(), (day_slot or "weekday").strip().lower())
        eff_slot = norm_slot if norm_slot in DAY_SLOTS else "weekday"
        if not parent:
            if not first_step_title.strip() or stalled_card_id.strip():
                return {"ok": False, "error": f"Stalled card `{stalled_card_id}` not found."}
            created = self.upsert_card(title=first_step_title.strip(), board_id=board_id or "life-home", sprint_id=active_spr["sprint_id"] if active_spr else "", status="sprint_todo", horizon="now", priority=1, effort_size="quick", day_slot=eff_slot, planned_date=planned_date, description=description or "15-minute anti-procrastination unblocker.", tags=["unblocker"])
            return {**created, "unblocker_card_id": created.get("card_id", ""), "stalled_card_id": ""} if created.get("ok") else created
        step_title = (first_step_title or f"First 15m Unblocker: Schedule/start `{parent['title'][:70]}`").strip()
        created = self.upsert_card(title=step_title, board_id=parent["board_id"], epic_id=parent.get("epic_id", ""), sprint_id=active_spr["sprint_id"] if active_spr else "", status="sprint_todo", horizon="now", priority=1, effort_size="quick", day_slot=eff_slot, planned_date=planned_date, description=description or f"15-minute unblocker sliced from `{parent['card_id']}` ({parent['title']}).", tags=["unblocker", *_as_str_list(parent.get("tags_json"))])
        if not created.get("ok"):
            return created
        uid = str(created["card_id"])
        self.upsert_card(card_id=parent["card_id"], title=parent["title"], board_id=parent["board_id"], status="backlog" if parent["status"] == "sprint_todo" else parent["status"], effort_size=parent["effort_size"], day_slot=parent["day_slot"], depends_on=list(dict.fromkeys([*_as_str_list(parent.get("depends_on_json")), uid])), force=True)
        return {"ok": True, "unblocker_card_id": uid, "stalled_card_id": parent["card_id"], "card": created["card"], "summary": f"Created `<30m` unblocker `{step_title}` (`{uid}`) in Sprint Todo and linked `{parent['card_id']}` behind it."}

    def compute_sprint_capacity(self, sprint_id: str = "") -> Dict[str, Any]:
        spr = self.get_active_sprint() if not sprint_id else next((s for s in self.list_sprints(50) if s["sprint_id"] == sprint_id), None)
        if not spr:
            return {"has_active_sprint": False, "weekday_used": 0, "weekday_capacity": 10, "weekend_used": 0, "weekend_capacity": 6, "any_used": 0, "done_count": 0, "total_count": 0, "over_capacity": False, "warnings": []}
        cards = self._enrich_cards(self.store._fetch_dicts(_CARD_COLS, f"SELECT {', '.join(_CARD_COLS)} FROM agile_cards WHERE sprint_id = ?", (spr["sprint_id"],)))
        wd_used, we_used, any_used = sum(c["slot_points"] for c in cards if c["day_slot"] == "weekday"), sum(c["slot_points"] for c in cards if c["day_slot"] == "weekend"), sum(c["slot_points"] for c in cards if c["day_slot"] == "any")
        wd_cap, we_cap, warnings = int(spr["weekday_capacity_slots"]), int(spr["weekend_capacity_slots"]), []
        if wd_used > wd_cap:
            warnings.append(f"Weekday capacity overloaded ({wd_used}/{wd_cap} slots).")
        if we_used > we_cap:
            warnings.append(f"Weekend capacity overloaded ({we_used}/{we_cap} slots).")
        if wd_used + we_used + any_used > wd_cap + we_cap:
            warnings.append(f"Total sprint slots ({wd_used + we_used + any_used}) exceed combined weekly budget ({wd_cap + we_cap}).")
        for mc in [c for c in cards if c["effort_size"] == "multi_day" and c["status"] != "done"]:
            warnings.append(f"Card `{mc['title']}` is `multi_day` — slice into smaller sprint cards.")
        return {"has_active_sprint": True, "sprint": spr, "weekday_used": wd_used, "weekday_capacity": wd_cap, "weekend_used": we_used, "weekend_capacity": we_cap, "any_used": any_used, "total_used": wd_used + we_used + any_used, "total_capacity": wd_cap + we_cap, "done_count": sum(1 for c in cards if c["status"] == "done"), "total_count": len(cards), "over_capacity": bool(warnings), "warnings": warnings}

    def rollover_sprint(self, retro_notes: str = "", start_next: bool = True, next_title: str = "") -> Dict[str, Any]:
        active = self.get_active_sprint()
        if not active:
            return {"ok": False, "error": "No active sprint found to close/rollover."}
        sid, now_s = active["sprint_id"], _fmt_iso()
        with self.store._lock:
            self.store.conn.execute("UPDATE agile_sprints SET status = 'completed', retro_notes = ?, updated_at = ? WHERE sprint_id = ?", ((retro_notes or active.get("retro_notes") or "").strip()[:1000], now_s, sid))
            unfinished = [str(r[0]) for r in self.store._fetch_rows("SELECT card_id FROM agile_cards WHERE sprint_id = ? AND status != 'done'", (sid,))]
            next_sid = str(self.upsert_sprint(title=next_title, weekday_capacity_slots=int(active["weekday_capacity_slots"]), weekend_capacity_slots=int(active["weekend_capacity_slots"])).get("sprint_id") or "") if start_next else ""
            for cid in unfinished:
                self.store.conn.execute("UPDATE agile_cards SET rollover_count = rollover_count + 1, sprint_id = ? WHERE card_id = ?", (next_sid, cid))
            self.store.conn.commit()
        return {"ok": True, "closed_sprint_id": sid, "next_sprint_id": next_sid, "rolled_over_count": len(unfinished), "summary": f"Closed Sprint `{active['title']}` ({len(unfinished)} unfinished card(s) rolled over{f' into `{next_sid}`' if next_sid else ''})."}

    def list_dated_cards(self, board_id: str = "") -> List[Dict[str, Any]]:
        cards = self._enrich_cards(self.store._fetch_dicts(_CARD_COLS, f"SELECT {', '.join(_CARD_COLS)} FROM agile_cards WHERE (planned_date != '' OR due_date != '')"))
        return [c for c in cards if c["board_id"] == _slug(board_id)] if (board_id and board_id != MASTER_SPRINT_BOARD_ID) else cards

    def get_board_view(self, board_id: str = MASTER_SPRINT_BOARD_ID, horizon: str = "") -> Dict[str, Any]:
        active_spr, bid, sel = self.get_active_sprint(), (board_id or MASTER_SPRINT_BOARD_ID).strip(), f"SELECT {', '.join(_CARD_COLS)} FROM agile_cards"
        if bid == MASTER_SPRINT_BOARD_ID:
            raw = self.store._fetch_dicts(_CARD_COLS, f"{sel} WHERE sprint_id = ? OR status IN ('sprint_todo', 'in_progress', 'blocked', 'verify') ORDER BY priority ASC, created_at ASC", (active_spr["sprint_id"],)) if active_spr else self.store._fetch_dicts(_CARD_COLS, f"{sel} WHERE status != 'done' OR horizon = 'now' ORDER BY priority ASC, created_at ASC")
            scoped_all = self._enrich_cards(self.store._fetch_dicts(_CARD_COLS, f"{sel} ORDER BY priority ASC, created_at ASC"))
        else:
            scoped_raw = self.store._fetch_dicts(_CARD_COLS, f"{sel} WHERE board_id = ? ORDER BY priority ASC, created_at ASC", (_slug(bid),))
            raw, scoped_all = scoped_raw, self._enrich_cards(list(scoped_raw))
        cards = [c for c in self._enrich_cards(raw) if not (horizon and horizon in HORIZONS and c["horizon"] != horizon)]
        all_open = [c for c in scoped_all if c["status"] != "done"]
        aging, backlog_cards = [c for c in all_open if c["is_aging"]], [c for c in all_open if c["status"] == "backlog"]
        return {"ok": True, "board_id": bid, "boards": self.list_boards(), "epics": self.list_epics(board_id=bid), "active_sprint": active_spr, "capacity": self.compute_sprint_capacity(active_spr["sprint_id"] if active_spr else ""), "cards": cards, "all_cards": scoped_all, "backlog_cards": backlog_cards, "columns": {st: [c for c in cards if c["status"] == st] for st in CARD_STATUSES}, "horizons": {hz: [c for c in all_open if c["horizon"] == hz] for hz in HORIZONS}, "aging_cards": aging, "summary": f"Board `{bid}`: {len(cards)} card(s), {len(backlog_cards)} backlog, {len(aging)} aging/stalled item(s)."}


def get_agile_manager(store: Optional[MemoryStore] = None) -> AgileBoardManager:
    return AgileBoardManager(store=store)


def sync_workflow_to_agile_board(mem: MemoryStore, wf_id: Optional[int], goal: str, phase: str, project_id: Optional[str] = None, active_task_id: Optional[int] = None) -> None:
    """Automatically mirror Companion `/plan` and `/goal` software tasks onto the active project's Kanban board."""
    if wf_id is None:
        return
    try:
        mgr, eff_pid = get_agile_manager(store=mem), (project_id or settings.get_workspace().project_id or SELF_PROJECT_ID).strip()
        bid, sid = mgr.ensure_project_board(eff_pid), (mgr.get_active_sprint() or {}).get("sprint_id") or ""
        wf_tasks, ph = [t for t in mem.get_all_tasks(project_id=eff_pid) if t.get("workflow_id") == wf_id], (phase or "").upper()
        if not wf_tasks:
            col = "done" if ph == "COMPLETE" else ("verify" if ph == "VERIFY" else ("in_progress" if ph == "IMPLEMENT" else "sprint_todo"))
            mgr.upsert_card(card_id=f"wf_{wf_id}_main", board_id=bid, sprint_id=sid, title=f"[WF #{wf_id}] {goal[:110]}", status=col, horizon="now", priority=1, effort_size="half_day", day_slot="weekday", workflow_id=wf_id, force=True)
            return
        for t in wf_tasks:
            tid, t_stat = int(t["id"]), str(t.get("status") or "pending")
            if t_stat != "cancelled":
                col = "done" if ph == "COMPLETE" else ("verify" if ph == "VERIFY" and t_stat == "completed" else ("done" if t_stat == "completed" else ("blocked" if t_stat == "blocked" else ("in_progress" if (active_task_id == tid or (ph == "IMPLEMENT" and t_stat == "in_progress")) else "sprint_todo"))))
                mgr.upsert_card(card_id=f"wf_{wf_id}_t{tid}", board_id=bid, sprint_id=sid, title=str(t.get("description") or f"Task #{tid}")[:140], description=str(t.get("acceptance_criteria") or ""), status=col, horizon="now", priority=max(1, min(5, int(t.get("priority") or 2))), effort_size="quick", day_slot="weekday", workflow_id=wf_id, task_id=tid, force=True)
    except Exception:
        return


def format_agile_hud(store: Optional[MemoryStore] = None) -> str:
    """Render a compact Agile Sprint & Procrastination Radar summary for the System Prompt HUD."""
    view = get_agile_manager(store=store).get_board_view(MASTER_SPRINT_BOARD_ID)
    cap, aging, lines = view["capacity"], view["aging_cards"], ["[AGILE SPRINT, BOARDS & PROCRASTINATION RADAR]"]
    if cap["has_active_sprint"]:
        sp, warn_s = cap["sprint"], (f" | WARNINGS: {'; '.join(cap['warnings'][:2])}" if cap["warnings"] else "")
        lines.append(f"- Active Sprint: `{sp['title']}` ({sp['start_date']}..{sp['end_date']}) | Done: {cap['done_count']}/{cap['total_count']} | Weekday Slots: {cap['weekday_used']}/{cap['weekday_capacity']} | Weekend Slots: {cap['weekend_used']}/{cap['weekend_capacity']}{warn_s}")
    else:
        lines.append("- Active Sprint: (None active — offer 5-minute weekly Sprint Planning via `agile_manage` when planning work/life tasks)")
    if aging:
        sample = ", ".join(f"`{c['card_id']}` ({c['title'][:40]} — {c['untouched_days']}d untouched)" for c in aging[:3])
        lines.append(f"- [AGING / STALLED RADAR]: {len(aging)} stalled card(s): {sample} -> Offer `agile_manage(action='slice_unblocker', ...)`!")
    return "\n".join(lines)


def _opt_param(p: Dict[str, Any], key: str) -> Optional[str]:
    return str(p[key]) if key in p and p[key] is not None else None


def execute_agile_action(p: Dict[str, Any], store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """Unified CoreAction handler for `agile` (`boards`, `sprints`, `cards`, `epics`, `capacity`, `unblocker`)."""
    mgr, sub = get_agile_manager(store=store), str(p.get("subaction") or p.get("action") or "board_view").strip().lower()
    if sub in ("list_boards", "boards"):
        bds = mgr.list_boards(include_archived=bool(p.get("include_archived")))
        return {"ok": True, "boards": bds, "summary": "Boards: " + ", ".join(f"`{b['board_id']}` ({b['open_cards']} open)" for b in bds)}
    if sub in ("save_board", "create_board", "upsert_board"):
        return mgr.upsert_board(str(p.get("title") or ""), str(p.get("kind") or "project"), str(p.get("board_id") or ""), str(p.get("project_id") or ""), str(p.get("description") or ""), bool(p.get("archived", False)))
    if sub in ("save_epic", "upsert_epic", "create_epic", "update_epic"):
        summ_val = _opt_param(p, "summary") if "summary" in p else _opt_param(p, "description")
        return mgr.upsert_epic(str(p.get("title") or ""), str(p.get("board_id") or ""), str(p.get("horizon") or ""), _opt_param(p, "target_date"), _opt_param(p, "wiki_path"), summ_val, str(p.get("epic_id") or ""), str(p.get("status") or ""), _opt_param(p, "start_date"))
    if sub in ("delete_epic", "remove_epic"):
        return mgr.delete_epic(str(p.get("epic_id") or ""))
    if sub in ("list_epics", "epics"):
        eps = mgr.list_epics(str(p.get("board_id") or ""), str(p.get("horizon") or ""))
        return {"ok": True, "epics": eps, "summary": f"{len(eps)} epic(s) found."}
    if sub in ("start_sprint", "save_sprint", "upsert_sprint"):
        return mgr.upsert_sprint(str(p.get("title") or ""), str(p.get("goal_summary") or ""), str(p.get("start_date") or ""), str(p.get("end_date") or ""), int(p.get("weekday_capacity_slots") or 10), int(p.get("weekend_capacity_slots") or 6), str(p.get("status") or "active"), str(p.get("sprint_id") or ""), str(p.get("retro_notes") or ""))
    if sub in ("list_sprints", "sprints"):
        sps = mgr.list_sprints()
        return {"ok": True, "sprints": sps, "active_sprint": mgr.get_active_sprint(), "summary": f"{len(sps)} sprint(s) recorded."}
    if sub in ("rollover_sprint", "complete_sprint"):
        return mgr.rollover_sprint(str(p.get("retro_notes") or ""), bool(p.get("start_next", True)), str(p.get("next_title") or ""))
    if sub == "capacity":
        cap = mgr.compute_sprint_capacity(str(p.get("sprint_id") or ""))
        return {"ok": True, **cap, "summary": f"Weekday {cap['weekday_used']}/{cap['weekday_capacity']} slots | Weekend {cap['weekend_used']}/{cap['weekend_capacity']} slots."}
    if sub in ("save_card", "add_card", "upsert_card", "update_card"):
        return mgr.upsert_card(title=str(p.get("title") or ""), board_id=str(p.get("board_id") or ""), card_id=str(p.get("card_id") or ""), description=_opt_param(p, "description"), status=str(p.get("status") or ""), horizon=str(p.get("horizon") or ""), priority=int(p.get("priority") or 0), effort_size=str(p.get("effort_size") or ""), day_slot=str(p.get("day_slot") or ""), planned_date=_opt_param(p, "planned_date"), due_date=_opt_param(p, "due_date"), blocked_reason=_opt_param(p, "blocked_reason"), depends_on=p.get("depends_on"), tags=p.get("tags"), checklist=p.get("checklist"), sprint_id=p.get("sprint_id") if "sprint_id" in p else None, epic_id=_opt_param(p, "epic_id"), created_at_override=str(p.get("created_at_override") or ""), force=bool(p.get("force", False)))
    if sub == "move_card":
        return mgr.move_card(str(p.get("card_id") or ""), str(p.get("status") or "sprint_todo"), str(p.get("blocked_reason") or ""), p.get("sprint_id") if "sprint_id" in p else None, bool(p.get("force", False)))
    if sub == "delete_card":
        return mgr.delete_card(str(p.get("card_id") or ""))
    if sub in ("slice_unblocker", "unblock"):
        return mgr.slice_unblocker_card(str(p.get("card_id") or p.get("stalled_card_id") or ""), str(p.get("first_step_title") or p.get("title") or ""), str(p.get("day_slot") or "weekday"), str(p.get("planned_date") or ""), str(p.get("board_id") or ""), str(p.get("description") or ""))
    view = mgr.get_board_view(str(p.get("board_id") or MASTER_SPRINT_BOARD_ID), str(p.get("horizon") or ""))
    col_counts = ", ".join(f"{k}={len(v)}" for k, v in view["columns"].items() if v) or "empty"
    shown = list({c["card_id"]: c for c in [*view["cards"], *view["backlog_cards"]]}.values())[:25]
    view["output"] = "\n".join(f"  - [{c['status']}] `{c['card_id']}` ({c['board_id']} | {c['effort_size']}/{c['day_slot']}): {c['title']}" + (f" [epic={c['epic_id']}]" if c.get("epic_id") else "") + (f" [AGING {c['untouched_days']}d]" if c["is_aging"] else "") + (f" [BLOCKED: {c['blocked_reason']}]" if c["blocked_reason"] else "") for c in shown)
    view["summary"] = f"Board `{view['board_id']}` ({col_counts}; backlog={len(view['backlog_cards'])}; aging={len(view['aging_cards'])})."
    return view


def parse_agile_slash(verb: str, parts: List[str]) -> Dict[str, Any]:
    """Parse `/board`, `/boards`, and `/sprint` CLI/REPL slash arguments into `execute_agile_action` params."""
    sub, rest = (parts[0].lower() if parts else "view"), parts[1:]
    if verb == "/sprint":
        return {"subaction": "start_sprint", "title": " ".join(rest)} if sub in ("start", "new") else ({"subaction": "rollover_sprint", "retro_notes": " ".join(rest)} if sub in ("rollover", "close") else ({"subaction": "capacity"} if sub == "capacity" else {"subaction": "board_view", "board_id": MASTER_SPRINT_BOARD_ID}))
    if sub == "list":
        return {"subaction": "list_boards"}
    if sub == "add" and rest:
        return {"subaction": "add_card", "title": " ".join(rest), "status": "sprint_todo"}
    if sub == "move" and len(rest) >= 2:
        return {"subaction": "move_card", "card_id": rest[0], "status": rest[1]}
    if sub == "unblock" and rest:
        return {"subaction": "slice_unblocker", "card_id": rest[0], "first_step_title": " ".join(rest[1:])}
    return {"subaction": "board_view", "board_id": sub if (parts and sub != "view") else MASTER_SPRINT_BOARD_ID}
