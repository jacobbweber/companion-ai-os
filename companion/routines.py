"""
Unified Routines, `TriggerPolicy` Cron/Window Gate & Idle Autonomy Watcher (`companion/routines.py`).

Consolidates `TriggerPolicy` (`manual`, `scheduled`, `on_idle`, overnight windows, 5-field cron,
`<5ms` deterministic prechecks) with `RoutineManager`, Background Autonomy posture, and Morning Journal.
"""

import inspect
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from companion.approvals import archive_aged_conversations, enqueue_session_nudge, get_approval_queue
from companion.config import execution_origin_context, settings
from companion.integrations import get_endpoint_manager
from companion.jobs import get_job_manager
from companion.llm import chat, model_gate
from companion.memory_store import MemoryStore, get_memory_store
from companion.vault import create_snapshot_backup
from companion.wiki import get_wiki_engine
from companion.workflows import execute_dag_by_name

__all__ = [
    "BUILTIN_IDLE_ROUTINES",
    "IdleAutonomyWatcher",
    "PRECHECK_REGISTRY",
    "RoutineManager",
    "TriggerPolicy",
    "VALID_ROUTINE_TARGETS",
    "VALID_TRIGGER_MODES",
    "compute_next_run_utc",
    "compute_next_scheduled_iso",
    "describe_operating_quadrant",
    "evaluate_trigger_eligibility",
    "evaluate_trigger_precheck",
    "execute_autonomy_mode_action",
    "format_morning_briefing",
    "get_routine_manager",
    "is_within_allowed_window",
    "matches_cron_5field",
    "parse_allowed_window",
    "parse_iso_utc",
    "reconcile_startup_missed_runs",
    "register_trigger_precheck",
    "validate_schedule_expr",
]

VALID_TRIGGER_MODES = ("manual", "scheduled", "on_idle")
_EVERY_RE = re.compile(r"^every\s+(\d+)\s*([smhd])$", re.I)
_DAILY_RE = re.compile(r"^daily\s+(\d{1,2}):(\d{2})$", re.I)
_WINDOW_RE = re.compile(
    r"^(?:(?P<days>[a-z,\-]+)\s+)?(?P<sh>\d{1,2}):(?P<sm>\d{2})\s*-\s*(?P<eh>\d{1,2}):(?P<em>\d{2})$",
    re.I,
)
_DAY_MAP: Dict[str, int] = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

PrecheckFn = Callable[..., Tuple[bool, str]]
PRECHECK_REGISTRY: Dict[str, PrecheckFn] = {}


@dataclass(frozen=True)
class TriggerPolicy:
    """Unified execution trigger policy shared by Routines and Autonomous Goals."""

    mode: str = "manual"
    schedule_expr: str = ""
    idle_min_seconds: int = 1800
    allowed_window: str = ""
    cooldown_seconds: int = 1800
    precheck_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "schedule_expr": self.schedule_expr,
            "idle_min_seconds": int(self.idle_min_seconds),
            "allowed_window": self.allowed_window,
            "cooldown_seconds": int(self.cooldown_seconds),
            "precheck_name": self.precheck_name,
        }

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "TriggerPolicy":
        data = dict(raw or {})
        mode = str(data.get("mode") or "").strip().lower()
        sched = str(data.get("schedule_expr") or "").strip()
        if not mode:
            mode = "scheduled" if sched else "manual"
        if mode not in VALID_TRIGGER_MODES:
            raise ValueError(f"TriggerPolicy mode must be one of {VALID_TRIGGER_MODES}, got '{mode}'.")
        window = str(data.get("allowed_window") or "").strip()
        if window:
            parse_allowed_window(window)
        if mode == "scheduled":
            if not sched:
                sched = "every 6h"
            validate_schedule_expr(sched)
        return cls(
            mode=mode,
            schedule_expr=sched,
            idle_min_seconds=max(0, int(data.get("idle_min_seconds", settings.idle_minutes * 60))),
            allowed_window=window,
            cooldown_seconds=max(0, int(data.get("cooldown_seconds", settings.hobby_cooldown_minutes * 60))),
            precheck_name=str(data.get("precheck_name") or "").strip(),
        )


def _parse_day_spec(spec: str) -> Set[int]:
    """Parse weekday filter like `mon-fri` or `sat,sun` into Python weekday ints (`0=Mon..6=Sun`)."""
    days: Set[int] = set()
    for part in spec.lower().split(","):
        tok = part.strip()
        if not tok:
            continue
        if "-" in tok:
            lhs, rhs = [x.strip()[:3] for x in tok.split("-", 1)]
            if lhs not in _DAY_MAP or rhs not in _DAY_MAP:
                raise ValueError(f"Invalid weekday range `{tok}` in allowed_window.")
            start_d, end_d = _DAY_MAP[lhs], _DAY_MAP[rhs]
            cur = start_d
            for _ in range(8):
                days.add(cur)
                if cur == end_d:
                    break
                cur = (cur + 1) % 7
        else:
            key = tok[:3]
            if key not in _DAY_MAP:
                raise ValueError(f"Invalid weekday `{tok}` in allowed_window.")
            days.add(_DAY_MAP[key])
    if not days:
        raise ValueError(f"Empty weekday specification `{spec}`.")
    return days


def parse_allowed_window(window_str: str) -> Tuple[Optional[Set[int]], int, int]:
    """Parse `[days] HH:MM-HH:MM` into `(weekdays_or_None, start_minutes, end_minutes)`."""
    clean = (window_str or "").strip()
    if not clean:
        return None, 0, 1440
    m = _WINDOW_RE.match(clean)
    if not m:
        raise ValueError(
            f"Invalid allowed_window `{window_str}`. Use `HH:MM-HH:MM` (e.g. `22:00-06:00` or `mon-fri 09:00-17:00`)."
        )
    sh, sm, eh, em = int(m.group("sh")), int(m.group("sm")), int(m.group("eh")), int(m.group("em"))
    end_24 = eh == 24 and em == 0
    if not (0 <= sh <= 23 and 0 <= sm <= 59 and ((0 <= eh <= 23 and 0 <= em <= 59) or end_24)):
        raise ValueError(f"Out-of-range clock time in allowed_window `{window_str}`.")
    days = _parse_day_spec(m.group("days")) if m.group("days") else None
    return days, sh * 60 + sm, (1440 if end_24 else eh * 60 + em)


def is_within_allowed_window(window_str: str, now_local: Optional[datetime] = None) -> bool:
    """Return True when `now_local` falls inside `window_str` (supports overnight wrap-around like `22:00-06:00`)."""
    clean = (window_str or "").strip()
    if not clean:
        return True
    days, start_min, end_min = parse_allowed_window(clean)
    dt = now_local or datetime.now().astimezone()
    cur_min = dt.hour * 60 + dt.minute
    if start_min == end_min:
        return days is None or dt.weekday() in days
    if start_min < end_min:
        day_ok = days is None or dt.weekday() in days
        return day_ok and (start_min <= cur_min < end_min)
    if cur_min >= start_min:
        return days is None or dt.weekday() in days
    if cur_min < end_min:
        prev_weekday = (dt.weekday() - 1) % 7
        return days is None or prev_weekday in days
    return False


def _parse_cron_field(field_str: str, lo: int, hi: int) -> Set[int]:
    """Parse a single 5-field cron token (`*`, `*/5`, `1-5`, `0,15,30`) into valid integer values."""
    vals: Set[int] = set()
    for part in field_str.split(","):
        tok = part.strip()
        step = 1
        if "/" in tok:
            tok, step_s = tok.split("/", 1)
            step = max(1, int(step_s))
        if tok == "*":
            vals.update(range(lo, hi + 1, step))
        elif "-" in tok:
            a_s, b_s = tok.split("-", 1)
            a, b = int(a_s), int(b_s)
            if not (lo <= a <= hi and lo <= b <= hi and a <= b):
                raise ValueError(f"Cron range `{part}` out of bounds [{lo}..{hi}].")
            vals.update(range(a, b + 1, step))
        else:
            v = int(tok)
            if hi == 6 and v == 7:
                v = 0
            if not (lo <= v <= hi):
                raise ValueError(f"Cron value `{v}` out of bounds [{lo}..{hi}].")
            vals.add(v)
    if not vals:
        raise ValueError(f"Empty cron field `{field_str}`.")
    return vals


def validate_schedule_expr(expr: str) -> None:
    """Validate `every <N>s|m|h|d`, `daily HH:MM`, or standard 5-field cron expression."""
    clean = (expr or "").strip()
    if _EVERY_RE.match(clean):
        if int(_EVERY_RE.match(clean).group(1)) <= 0:  # type: ignore[union-attr]
            raise ValueError("Schedule interval must be >= 1.")
        return
    dm = _DAILY_RE.match(clean)
    if dm:
        hh, mm = int(dm.group(1)), int(dm.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(f"Invalid daily clock time `{expr}`.")
        return
    parts = clean.split()
    if len(parts) == 5:
        _parse_cron_field(parts[0], 0, 59)
        _parse_cron_field(parts[1], 0, 23)
        _parse_cron_field(parts[2], 1, 31)
        _parse_cron_field(parts[3], 1, 12)
        _parse_cron_field(parts[4], 0, 6)
        return
    raise ValueError(
        f"Invalid schedule `{expr}`. Use `every <N>m|h|d`, `daily HH:MM`, or 5-field cron `min hour dom mon dow`."
    )


def matches_cron_5field(expr: str, dt: datetime) -> bool:
    """Return True if `dt` matches a 5-field cron expression (`0=Sunday` or `7=Sunday`, `1=Monday..6=Saturday`)."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return False
    mins = _parse_cron_field(parts[0], 0, 59)
    hrs = _parse_cron_field(parts[1], 0, 23)
    doms = _parse_cron_field(parts[2], 1, 31)
    mons = _parse_cron_field(parts[3], 1, 12)
    dows = _parse_cron_field(parts[4], 0, 6)
    cron_dow = (dt.weekday() + 1) % 7
    return dt.minute in mins and dt.hour in hrs and dt.day in doms and dt.month in mons and cron_dow in dows


def compute_next_scheduled_iso(
    expr: str,
    from_ts: Optional[datetime] = None,
    now_utc: Optional[datetime] = None,
) -> str:
    """Compute next UTC ISO-8601 fire timestamp evaluating `daily HH:MM` and 5-field cron in local host timezone."""
    base_utc = (from_ts or now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    base_local = base_utc.astimezone()
    clean = (expr or "").strip()
    em = _EVERY_RE.match(clean)
    if em:
        qty, unit = max(1, int(em.group(1))), em.group(2).lower()
        sec = qty * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return (base_utc + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
    dm = _DAILY_RE.match(clean)
    if dm:
        hh, mm = int(dm.group(1)), int(dm.group(2))
        cand_local = base_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand_local <= base_local:
            cand_local += timedelta(days=1)
        return cand_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    validate_schedule_expr(clean)
    local_probe = (base_local + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(60 * 24 * 366):
        if matches_cron_5field(clean, local_probe):
            return local_probe.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        local_probe += timedelta(minutes=1)
    return (base_utc + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(ts_str: str) -> Optional[datetime]:
    """Parse SQLite or ISO-8601 UTC timestamp string safely into an aware UTC `datetime`."""
    raw = (ts_str or "").strip()
    if not raw:
        return None
    try:
        norm = raw.replace("Z", "+00:00") if ("T" in raw) else (raw.replace(" ", "T") + "+00:00")
        dt = datetime.fromisoformat(norm)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def register_trigger_precheck(name: str, fn: PrecheckFn) -> None:
    """Register a `<5ms` zero-LLM-token deterministic precheck callable."""
    PRECHECK_REGISTRY[name.strip()] = fn


def evaluate_trigger_precheck(name: str, store: Optional[MemoryStore] = None, last_run_at: str = "") -> Tuple[bool, str]:
    """Execute a registered `<5ms` deterministic pre-check; passes `last_run_at` when supported."""
    clean = (name or "").strip()
    if not clean or clean == "always":
        return True, "unconditional"
    fn = PRECHECK_REGISTRY.get(clean)
    if fn is None:
        return False, f"unknown_precheck:{clean}"
    mem = store or get_memory_store()
    try:
        params = list(inspect.signature(fn).parameters.values())
        accepts_last = len(params) >= 2 or any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        return fn(mem, last_run_at) if accepts_last else fn(mem)
    except Exception as exc:
        return False, f"precheck_error:{type(exc).__name__}"


def evaluate_trigger_eligibility(
    policy: TriggerPolicy,
    *,
    idle_seconds: float,
    last_run_at: str = "",
    next_run_at: str = "",
    background_autonomy_enabled: bool = False,
    now_utc: Optional[datetime] = None,
    now_local: Optional[datetime] = None,
    store: Optional[MemoryStore] = None,
) -> Tuple[bool, str]:
    """Evaluate whether a `TriggerPolicy` should fire right now without spending LLM tokens."""
    cur_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cur_local = now_local or cur_utc.astimezone()
    if policy.mode == "manual":
        return False, "manual_only"
    if policy.allowed_window and not is_within_allowed_window(policy.allowed_window, now_local=cur_local):
        return False, "outside_allowed_window"
    last_dt = parse_iso_utc(last_run_at)
    if last_dt is not None and policy.cooldown_seconds > 0:
        if (cur_utc - last_dt).total_seconds() < policy.cooldown_seconds:
            return False, "cooldown_active"
    if policy.mode == "on_idle":
        if not background_autonomy_enabled:
            return False, "background_autonomy_off"
        if idle_seconds < policy.idle_min_seconds:
            return False, "not_idle_long_enough"
    elif policy.mode == "scheduled":
        nxt_dt = parse_iso_utc(next_run_at)
        if nxt_dt is not None and cur_utc < nxt_dt:
            return False, "not_yet_due"
    ok_pre, reason_pre = evaluate_trigger_precheck(policy.precheck_name, store=store, last_run_at=last_run_at)
    if not ok_pre:
        return False, f"precheck_skipped:{reason_pre}"
    return True, f"eligible_{policy.mode}"


def _iter_md_files(folder: Path):
    if not folder.is_dir():
        return
    for dirpath, _dirs, files in os.walk(folder):
        for fname in files:
            if fname.endswith(".md") and fname != "README.md":
                yield Path(dirpath) / fname


def _sqlite_since(last_run_at: str) -> str:
    dt = parse_iso_utc(last_run_at)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt is not None else ""


def _precheck_wiki_inbox_or_stale(store: MemoryStore, last_run_at: str = "") -> Tuple[bool, str]:
    _ = last_run_at
    wiki_root: Path = settings.wiki_dir
    if any(True for _ in _iter_md_files(wiki_root / "01_inbox")):
        return True, "wiki_inbox_has_notes"
    cutoff = datetime.now(timezone.utc).timestamp() - 14 * 86400
    if any(p.stat().st_mtime < cutoff for p in _iter_md_files(wiki_root / "02_knowledge")):
        return True, "wiki_has_stale_notes"
    return (True, "pending_notes") if store.get_system_notes(limit=1) else (False, "wiki_clean")


def _precheck_recent_chat(store: MemoryStore, last_run_at: str = "") -> Tuple[bool, str]:
    since_sql = _sqlite_since(last_run_at)
    with store._lock:
        if since_sql:
            cnt = int(store.conn.execute("SELECT COUNT(*) FROM conversations WHERE role = 'user' AND timestamp > ?", (since_sql,)).fetchone()[0])
        else:
            cnt = int(store.conn.execute("SELECT COUNT(*) FROM conversations WHERE role = 'user'").fetchone()[0])
    return (True, f"user_turns={cnt}") if cnt > 0 else (False, "no_new_chat_turns")


def _precheck_warn_or_slow_spans(store: MemoryStore, last_run_at: str = "") -> Tuple[bool, str]:
    since_sql = _sqlite_since(last_run_at)
    try:
        with store._lock:
            if since_sql:
                err_cnt = int(store.conn.execute("SELECT COUNT(*) FROM telemetry_logs WHERE level IN ('WARN', 'ERROR') AND created_at > ?", (since_sql,)).fetchone()[0])
            else:
                err_cnt = int(store.conn.execute("SELECT COUNT(*) FROM telemetry_logs WHERE level IN ('WARN', 'ERROR')").fetchone()[0])
        if err_cnt > 0:
            return True, f"warn_or_error_logs={err_cnt}"
    except Exception:
        pass
    return False, "telemetry_clean"


register_trigger_precheck("wiki_inbox_or_stale", _precheck_wiki_inbox_or_stale)
register_trigger_precheck("recent_unconsolidated_chat", _precheck_recent_chat)
register_trigger_precheck("recent_warn_or_slow_spans", _precheck_warn_or_slow_spans)


VALID_ROUTINE_TARGETS: Tuple[str, ...] = (
    "dag", "maintenance_agent", "goal_slice", "wiki_inbox_triage",
    "wiki_deep_curate", "snapshot_backup", "endpoint_pulse", "chat_archive",
)

ROUTINES_DDL: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS routines (
        routine_id TEXT PRIMARY KEY, title TEXT NOT NULL, schedule_expr TEXT NOT NULL DEFAULT '',
        trigger_policy_json TEXT NOT NULL DEFAULT '{}', target_kind TEXT NOT NULL, target_name TEXT DEFAULT '',
        inputs_json TEXT DEFAULT '{}', enabled INTEGER DEFAULT 1, catch_up INTEGER DEFAULT 0,
        next_run_at TEXT NOT NULL DEFAULT '', last_run_at TEXT DEFAULT '', last_job_id TEXT DEFAULT '',
        last_status TEXT DEFAULT 'idle', missed_count INTEGER DEFAULT 0, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
)

BUILTIN_IDLE_ROUTINES: Tuple[Dict[str, Any], ...] = (
    {
        "routine_id": "wiki_gardener", "title": "Wiki Inbox Gardener & Link Auditor",
        "target_kind": "wiki_inbox_triage", "target_name": "wiki_gardener",
        "trigger_policy": {"mode": "on_idle", "idle_min_seconds": 900, "cooldown_seconds": 3600, "precheck_name": "wiki_inbox_or_stale"},
    },
    {
        "routine_id": "memory_curator", "title": "Episodic Memory Fact Consolidator",
        "target_kind": "maintenance_agent", "target_name": "memory_curator",
        "trigger_policy": {"mode": "on_idle", "idle_min_seconds": 900, "cooldown_seconds": 3600, "precheck_name": "recent_unconsolidated_chat"},
    },
    {
        "routine_id": "trace_reviewer", "title": "Telemetry Anomaly & Slow-Span Reviewer",
        "target_kind": "maintenance_agent", "target_name": "trace_reviewer",
        "trigger_policy": {"mode": "on_idle", "idle_min_seconds": 900, "cooldown_seconds": 3600, "precheck_name": "recent_warn_or_slow_spans"},
    },
)

_MAINTENANCE_BRIEFS: Dict[str, Tuple[str, List[str]]] = {
    "memory_curator": (
        "Review recent conversation history via `memory_manage` and consolidate durable facts or preferences cleanly without inventing data.",
        ["memory_manage", "wiki_manage"],
    ),
    "trace_reviewer": (
        "Inspect recent warning/error telemetry spans and slow traces; capture concise root-cause notes into `01_inbox/` via `wiki_manage` if anomalies exist.",
        ["system_doctor", "wiki_manage", "read_file"],
    ),
}

_RAW_SECRET_KEY_RE = re.compile(r"(password|secret|api_key|private_key|bearer|token_value)", re.I)
_RECONCILED_DBS: Set[str] = set()


def _slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", (raw or "").strip().lower()).strip("-")[:64]


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(iso_str: str) -> Optional[datetime]:
    s = (iso_str or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.rstrip("Z"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except Exception:
        return None


def compute_next_run_utc(schedule_expr: str, after_dt: Optional[datetime] = None) -> datetime:
    """Parse `every <N>s|m|h|d`, `daily HH:MM`, or 5-field cron into the next UTC `datetime`."""
    iso = compute_next_scheduled_iso(schedule_expr, now_utc=after_dt)
    parsed = _parse_utc(iso)
    if parsed is None:
        raise ValueError(f"Unsupported schedule `{schedule_expr}`.")
    return parsed


def describe_operating_quadrant(bg_autonomy_on: bool) -> Dict[str, Any]:
    """Describe the active Operating Posture (`Background Autonomy` ON/OFF with immutable installed `app/`)."""
    qid, label, desc = (
        ("autonomous_partner", "Autonomous Partner", "Scheduled & windowed `on_idle` routines and Goal slices active; installed `app/` immutable.")
        if bg_autonomy_on
        else ("interactive_standard", "Interactive Partner", "Foreground-only assistance; installed `app/` immutable.")
    )
    return {
        "quadrant": qid,
        "label": label,
        "background_autonomy": bool(bg_autonomy_on),
        "immutable_app": True,
        "summary": f"{label} (`Background Autonomy={'ON' if bg_autonomy_on else 'OFF'}`) — {desc}",
    }


def format_morning_briefing(store: Optional[MemoryStore] = None, mark_seen: bool = True, limit: int = 15) -> str:
    """Format the `/morning` briefing over recent `autonomy_journal` routine and goal-slice runs."""
    mem = store or get_memory_store()
    entries = mem.get_autonomy_journal(limit=limit, unseen_only=False, mark_seen=mark_seen)
    quad = describe_operating_quadrant(mem.is_background_autonomy_enabled())
    lines = [f"=== Morning Autonomy Briefing ({quad['label']}) ==="]
    if not entries:
        lines.append("  (No background routine or goal-slice runs recorded yet.)")
        return "\n".join(lines)
    for e in entries:
        seen_tag = "" if e.get("seen_by_user") else " [NEW]"
        t_label = e.get("entry_name") or e.get("target_id") or "run"
        lines.append(f"  * [{e['created_at']}] `{e['entry_kind']}:{t_label}` ({e['status'].upper()}){seen_tag} — {e['summary']}")
    return "\n".join(lines)


class RoutineManager:
    """Unified SQLite Routine Manager supporting `manual`, `scheduled`, and windowed `on_idle` `TriggerPolicy`."""

    def __init__(self, store: Optional[MemoryStore] = None):
        self._store = store

    @property
    def store(self) -> MemoryStore:
        mem = self._store or get_memory_store()

        def _init_routines(m: MemoryStore) -> None:
            for ddl in ROUTINES_DDL:
                m.conn.execute(ddl)
            cols = {r[1] for r in m.conn.execute("PRAGMA table_info(routines)").fetchall()}
            if "trigger_policy_json" not in cols:
                m.conn.execute("ALTER TABLE routines ADD COLUMN trigger_policy_json TEXT NOT NULL DEFAULT '{}'")
            for item in BUILTIN_IDLE_ROUTINES:
                tp_json = json.dumps(TriggerPolicy.from_dict(item["trigger_policy"]).to_dict())
                m.conn.execute(
                    """INSERT OR IGNORE INTO routines
                       (routine_id, title, schedule_expr, trigger_policy_json, target_kind, target_name, enabled, next_run_at)
                       VALUES (?, ?, '', ?, ?, ?, 1, '')""",
                    (item["routine_id"], item["title"], tp_json, item["target_kind"], item["target_name"]),
                )

        mem.ensure_schema("routines_v2", _init_routines)
        return mem

    def create_or_update_routine(
        self, routine_id: str, schedule_expr: str = "", target_kind: str = "dag", title: str = "",
        target_name: str = "", inputs: Optional[Dict[str, Any]] = None,
        trigger_policy: Optional[Union[TriggerPolicy, Dict[str, Any]]] = None,
        enabled: bool = True, catch_up: bool = False, actor: str = "user",
        confirmed: bool = False, now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Register or update a routine governed by `TriggerPolicy` (`manual`, `scheduled`, or `on_idle`)."""
        rid = _slug(routine_id or title)
        raw_tk = (target_kind or "").strip().lower()
        tkind = "dag" if raw_tk == "recipe" else raw_tk
        if not rid or tkind not in VALID_ROUTINE_TARGETS:
            return {"ok": False, "error": f"`routine_id` and verified `target_kind` ({', '.join(VALID_ROUTINE_TARGETS)}) are required; free-form prompts are forbidden."}
        if tkind == "dag" and not (target_name or "").strip():
            return {"ok": False, "error": "`target_name` (verified Python DAG name) is required when `target_kind='dag'`."}

        clean_inputs = inputs or {}
        for k, v in clean_inputs.items():
            if _RAW_SECRET_KEY_RE.search(str(k)) or ("-----BEGIN " in str(v)):
                return {"ok": False, "error": f"ROUTINE VAULT GUARD: Do not embed raw secret field `{k}` in routine inputs; use symbolic Vault references."}

        try:
            if isinstance(trigger_policy, TriggerPolicy):
                tp = trigger_policy
            elif isinstance(trigger_policy, dict) and trigger_policy:
                merged = dict(trigger_policy)
                if schedule_expr and not merged.get("schedule_expr"):
                    merged["schedule_expr"] = schedule_expr
                tp = TriggerPolicy.from_dict(merged)
            elif (schedule_expr or "").strip():
                tp = TriggerPolicy(mode="scheduled", schedule_expr=schedule_expr.strip())
            else:
                tp = TriggerPolicy(mode="manual")
            next_iso = _fmt_utc(compute_next_run_utc(tp.schedule_expr, after_dt=now)) if tp.mode == "scheduled" else ""
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        if enabled and actor.strip().lower() not in ("user", "cli", "web"):
            allowed, apr = get_approval_queue(self.store).check_or_enqueue(
                category="routine_enable", action_name="enable_routine", target=rid,
                summary=f"Enable routine `{title or rid}` (`{tp.mode}` -> `{tkind}:{target_name or 'default'}`).",
                payload={"routine_id": rid, "schedule": tp.schedule_expr, "trigger_policy": tp.to_dict(), "target_kind": tkind, "title": title, "target_name": target_name, "inputs": clean_inputs, "catch_up": catch_up},
                requester=actor, confirmed=confirmed, always_require_in_autonomous=True,
            )
            if not allowed:
                return {"ok": False, "awaiting_approval": True, "approval_id": apr["approval_id"], "error": f"ROUTINE APPROVAL REQUIRED (`{apr['approval_id']}`): Enabling routine `{rid}` awaits user approval."}

        with self.store._lock:
            self.store.conn.execute(
                """INSERT INTO routines
                   (routine_id, title, schedule_expr, trigger_policy_json, target_kind, target_name, inputs_json, enabled, catch_up, next_run_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(routine_id) DO UPDATE SET
                     title=excluded.title, schedule_expr=excluded.schedule_expr, trigger_policy_json=excluded.trigger_policy_json,
                     target_kind=excluded.target_kind, target_name=excluded.target_name, inputs_json=excluded.inputs_json,
                     enabled=excluded.enabled, catch_up=excluded.catch_up, next_run_at=excluded.next_run_at, updated_at=CURRENT_TIMESTAMP""",
                (rid, (title.strip() or rid)[:100], tp.schedule_expr, json.dumps(tp.to_dict()), tkind, target_name.strip()[:80], json.dumps(clean_inputs), 1 if enabled else 0, 1 if catch_up else 0, next_iso),
            )
            self.store.conn.commit()
        return {"ok": True, "routine": self.get_routine(rid), "summary": f"Saved routine `{rid}` (`{tp.mode}` -> `{tkind}`, next_run=`{next_iso or tp.allowed_window or 'on_demand'}`)."}

    def get_routine(self, routine_id: str) -> Optional[Dict[str, Any]]:
        cols = [
            "routine_id", "title", "schedule_expr", "trigger_policy_json", "target_kind", "target_name",
            "inputs_json", "enabled", "catch_up", "next_run_at", "last_run_at", "last_job_id", "last_status", "missed_count", "updated_at",
        ]
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM routines WHERE routine_id = ?", (_slug(routine_id),))
        if not rows:
            return None
        r = rows[0]
        r["inputs"] = json.loads(r.pop("inputs_json") or "{}")
        raw_tp = json.loads(r.pop("trigger_policy_json") or "{}")
        if not raw_tp and r.get("schedule_expr"):
            raw_tp = {"mode": "scheduled", "schedule_expr": r["schedule_expr"]}
        r["trigger_policy"] = TriggerPolicy.from_dict(raw_tp).to_dict()
        r["enabled"], r["catch_up"] = bool(r["enabled"]), bool(r["catch_up"])
        return r

    def list_routines(self, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        ids = [str(r[0]) for r in self.store._fetch_rows("SELECT routine_id FROM routines ORDER BY routine_id ASC")]
        items = [rt for rid in ids if (rt := self.get_routine(rid)) is not None]
        return [rt for rt in items if rt["trigger_policy"]["mode"] == mode] if mode else items

    def set_routine_enabled(self, routine_id: str, enabled: bool) -> Dict[str, Any]:
        rt = self.get_routine(routine_id)
        if not rt:
            return {"ok": False, "error": f"Routine `{routine_id}` not found."}
        with self.store._lock:
            self.store.conn.execute("UPDATE routines SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE routine_id = ?", (1 if enabled else 0, rt["routine_id"]))
            self.store.conn.commit()
        return {"ok": True, "routine": self.get_routine(rt["routine_id"]), "summary": f"Routine `{rt['routine_id']}` {'enabled' if enabled else 'disabled'}."}

    def delete_routine(self, routine_id: str) -> Dict[str, Any]:
        rid = _slug(routine_id)
        with self.store._lock:
            cur = self.store.conn.execute("DELETE FROM routines WHERE routine_id = ?", (rid,))
            self.store.conn.commit()
        return {"ok": bool(cur.rowcount), "routine_id": rid, "summary": f"Deleted routine `{rid}`."} if cur.rowcount else {"ok": False, "error": f"Routine `{rid}` not found."}

    def _is_overlapping(self, routine: Dict[str, Any]) -> bool:
        jid = str(routine.get("last_job_id") or "").strip()
        if not jid:
            return False
        prev = get_job_manager(store=self.store).get_job(jid)
        return bool(prev and prev.get("status") in ("queued", "running", "waiting_approval"))

    def _execute_routine_once(
        self,
        rt: Dict[str, Any],
        now_dt: datetime,
        run_in_background: bool,
        chat_fn: Callable[..., Dict[str, Any]] = chat,
    ) -> Dict[str, Any]:
        if self._is_overlapping(rt):
            return {"ok": True, "skipped_overlap": True, "routine_id": rt["routine_id"]}
        tp = TriggerPolicy.from_dict(rt.get("trigger_policy"))

        tkind, inputs = rt["target_kind"], rt.get("inputs") or {}
        jm = get_job_manager(store=self.store, chat_fn=chat_fn)
        job_id, status_lbl, summary_txt = "", "done", f"Executed `{rt['title']}`."
        with execution_origin_context("idle_routine"):
            if tkind == "snapshot_backup":
                res = create_snapshot_backup(label=f"routine_{rt['routine_id']}", store=self.store)
                status_lbl, summary_txt = ("done" if res.get("ok") else "failed"), str(res.get("summary") or "")
            elif tkind == "chat_archive":
                res = archive_aged_conversations(store=self.store, older_than_days=int(inputs.get("older_than_days") or 90))
                status_lbl, summary_txt = ("done" if res.get("ok") else "failed"), str(res.get("summary") or "")
            elif tkind == "endpoint_pulse":
                em = get_endpoint_manager(store=self.store)
                targets = [inputs["endpoint_id"]] if inputs.get("endpoint_id") else [e["endpoint_id"] for e in em.list_endpoints()]
                results = [em.test_endpoint(eid, timeout=3.0) for eid in targets]
                offline = [eid for eid, r in zip(targets, results) if not r.get("ok")]
                if offline:
                    enqueue_session_nudge(f"Routine `{rt['title']}` detected unreachable endpoint(s): {', '.join(offline)}.", category="routine", ref_id=rt["routine_id"], store=self.store)
                status_lbl = "done" if not offline else f"degraded ({len(offline)} offline)"
                summary_txt = f"Probed {len(targets)} endpoint(s): {status_lbl}."
            elif tkind == "wiki_inbox_triage":
                inbox = get_wiki_engine(store=self.store).list_notes(folder="01_inbox", limit=30)
                goal = f"Run `wiki_manage(action='inspect_graduation', path=...)` across the {len(inbox)} note(s) in `01_inbox/` and write a triage digest in `01_inbox/inbox-triage-digest.md` without modifying `02_knowledge/`."
                dispatched = jm.dispatch_job(goal=goal, title=f"Routine: {rt['title']}", kind="routine", starter=f"routine:{rt['routine_id']}", allowed_tools=["wiki_manage"], run_in_background=run_in_background)
                job_id, status_lbl, summary_txt = str(dispatched.get("job_id") or ""), str(dispatched.get("status") or "queued"), str(dispatched.get("summary") or "")
            elif tkind == "wiki_deep_curate":
                wk = get_wiki_engine(store=self.store)
                inbox = [n for n in wk.list_notes(folder="01_inbox", limit=15) if not str(n["rel_path"]).endswith("inbox-triage-digest.md")]
                if not inbox:
                    status_lbl, summary_txt = "done (inbox clean)", "Wiki inbox already clean."
                else:
                    target_list = ", ".join(f"`{n['rel_path']}`" for n in inbox[:5])
                    goal = f"Curate staged `01_inbox/` note(s) ({target_list}) into `02_knowledge/` using `inspect_graduation`, `validate_curation`, and `commit_curation`."
                    dispatched = jm.dispatch_job(goal=goal, title=f"Routine Deep Curate: {rt['title']}", kind="routine", starter=f"routine:{rt['routine_id']}", tier="reasoning", allowed_tools=["wiki_manage"], max_iters=14, run_in_background=run_in_background)
                    job_id, status_lbl, summary_txt = str(dispatched.get("job_id") or ""), str(dispatched.get("status") or "queued"), str(dispatched.get("summary") or "")
            elif tkind == "maintenance_agent":
                brief, tools = _MAINTENANCE_BRIEFS.get(rt["target_name"], (f"Execute maintenance routine `{rt['title']}` concisely.", ["read_file", "wiki_manage", "memory_manage"]))
                dispatched = jm.dispatch_job(goal=brief, title=f"Maintenance: {rt['title']}", kind="routine", starter=f"routine:{rt['routine_id']}", allowed_tools=tools, max_iters=6, run_in_background=run_in_background)
                job_id, status_lbl, summary_txt = str(dispatched.get("job_id") or ""), str(dispatched.get("status") or "queued"), str(dispatched.get("summary") or "")
            elif tkind == "goal_slice":
                from companion.agent import run_autonomous_goal_slice
                slice_res = run_autonomous_goal_slice(project_id=str(inputs.get("project_id") or rt.get("target_name") or "") or None, chat_fn=chat_fn, store=self.store)
                status_lbl, summary_txt = str(slice_res.get("status") or "done"), str(slice_res.get("summary") or "")
            elif tkind in ("dag", "recipe"):
                if run_in_background:
                    dispatched = jm.dispatch_job(goal=f"Routine DAG {rt['target_name']}", title=f"Routine: {rt['title']}", kind="dag", starter=f"routine:{rt['routine_id']}", dag_name=rt["target_name"], dag_params=inputs, run_in_background=True)
                    job_id, status_lbl, summary_txt = str(dispatched.get("job_id") or ""), str(dispatched.get("status") or "queued"), str(dispatched.get("summary") or "")
                else:
                    dag_res = execute_dag_by_name(rt["target_name"], params=inputs, store=self.store)
                    status_lbl, summary_txt = str(dag_res.get("status") or ("done" if dag_res.get("ok") else "failed")), str(dag_res.get("summary") or "")

        next_iso = _fmt_utc(compute_next_run_utc(tp.schedule_expr, after_dt=now_dt)) if tp.mode == "scheduled" and tp.schedule_expr else ""
        now_iso = _fmt_utc(now_dt)
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE routines SET last_run_at = ?, next_run_at = ?, last_job_id = ?, last_status = ?, updated_at = CURRENT_TIMESTAMP WHERE routine_id = ?",
                (now_iso, next_iso, job_id, status_lbl[:40], rt["routine_id"]),
            )
            self.store.conn.commit()
        self.store.log_autonomy_run(rt["routine_id"], status_lbl[:32], summary_txt or f"Routine `{rt['routine_id']}` -> {status_lbl}", doctor_passed=(status_lbl != "failed"), entry_kind="routine")
        return {"ok": status_lbl != "failed", "routine_id": rt["routine_id"], "job_id": job_id, "status": status_lbl, "next_run_at": next_iso, "summary": summary_txt}

    def run_routine_now(
        self, routine_id: str, run_in_background: bool = False,
        now: Optional[datetime] = None, chat_fn: Callable[..., Dict[str, Any]] = chat,
    ) -> Dict[str, Any]:
        """Manually or programmatically run a specific routine immediately."""
        rt = self.get_routine(routine_id)
        if not rt:
            return {"ok": False, "error": f"Routine `{routine_id}` not found."}
        return self._execute_routine_once(rt, (now or datetime.now(timezone.utc)).astimezone(timezone.utc), run_in_background=run_in_background, chat_fn=chat_fn)

    def select_eligible_idle_routines(
        self, idle_seconds: float, now_utc: Optional[datetime] = None, now_local: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Return enabled `on_idle` routines passing `TriggerPolicy` window, cooldown, and `<5ms` prechecks."""
        bg_on = self.store.is_background_autonomy_enabled()
        eligible: List[Dict[str, Any]] = []
        for rt in self.list_routines(mode="on_idle"):
            if not rt["enabled"]:
                continue
            ok, reason = evaluate_trigger_eligibility(
                TriggerPolicy.from_dict(rt.get("trigger_policy")), idle_seconds=idle_seconds,
                last_run_at=rt.get("last_run_at") or "",
                background_autonomy_enabled=bg_on, now_utc=now_utc, now_local=now_local, store=self.store,
            )
            if ok:
                eligible.append({**rt, "eligibility_reason": reason})
        eligible.sort(key=lambda r: str(r.get("last_run_at") or ""))
        return eligible

    def reconcile_startup_missed_runs(self, now: Optional[datetime] = None, run_in_background: bool = True) -> Dict[str, Any]:
        """Advance overdue scheduled routines after downtime without a thundering herd."""
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        skipped_routines: List[str] = []
        caught_up: List[str] = []
        for rt in self.list_routines(mode="scheduled"):
            if not rt["enabled"]:
                continue
            due_dt = _parse_utc(rt["next_run_at"])
            if due_dt is None or due_dt > now_dt:
                continue
            expr = rt["trigger_policy"]["schedule_expr"] or rt["schedule_expr"]
            missed_slots, cursor = 0, due_dt
            while cursor <= now_dt and missed_slots < 500:
                missed_slots += 1
                cursor = compute_next_run_utc(expr, after_dt=cursor)
            with self.store._lock:
                self.store.conn.execute(
                    "UPDATE routines SET missed_count = missed_count + ?, next_run_at = ?, updated_at = CURRENT_TIMESTAMP WHERE routine_id = ?",
                    (missed_slots, _fmt_utc(cursor), rt["routine_id"]),
                )
                self.store.conn.commit()
            if rt["catch_up"]:
                self._execute_routine_once(self.get_routine(rt["routine_id"]) or rt, now_dt, run_in_background=run_in_background)
                caught_up.append(rt["routine_id"])
            else:
                skipped_routines.append(f"{rt['routine_id']} ({missed_slots}x)")
        if skipped_routines:
            enqueue_session_nudge(f"Skipped overdue routine run(s) during downtime (no thundering herd): {', '.join(skipped_routines)}.", category="routine", store=self.store)
        return {"ok": True, "skipped": skipped_routines, "caught_up": caught_up}

    def tick_due_routines(
        self, now: Optional[datetime] = None, run_in_background: bool = True, abort_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Trigger any currently due `scheduled` routines under `model_gate.background_context` preemption."""
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        fired, overlapped = [], []
        for rt in self.list_routines(mode="scheduled"):
            due_dt = _parse_utc(rt["next_run_at"])
            if not rt["enabled"] or due_dt is None or due_dt > now_dt:
                continue
            with model_gate.background_context(abort_check=abort_check):
                res = self._execute_routine_once(rt, now_dt, run_in_background=run_in_background)
            (overlapped if res.get("skipped_overlap") else fired).append(rt["routine_id"])
        return {"ok": True, "fired": fired, "skipped_overlap": overlapped}


class IdleAutonomyWatcher:
    """Preemptible background watcher driving `scheduled` routines, `on_idle` routines, and windowed Goal slices."""

    def __init__(
        self, is_busy_fn: Callable[[], bool], store: Optional[MemoryStore] = None,
        poll_interval_sec: float = 15.0, chat_fn: Callable[..., Dict[str, Any]] = chat,
    ):
        self.is_busy_fn = is_busy_fn
        self._store = store
        self.poll_interval_sec = max(1.0, float(poll_interval_sec))
        self.chat_fn = chat_fn
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_busy_mono = time.monotonic()

    @property
    def store(self) -> MemoryStore:
        return self._store or get_memory_store()

    @property
    def last_human_activity_mono(self) -> float:
        return self._last_busy_mono

    def record_human_activity(self) -> None:
        self._last_busy_mono = time.monotonic()

    def mark_user_activity(self) -> None:
        self.record_human_activity()

    def tick_once(
        self, idle_seconds: Optional[float] = None, now_utc: Optional[datetime] = None,
        now_local: Optional[datetime] = None, abort_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Evaluate one non-blocking autonomy tick (`scheduled` due routines -> `on_idle` routines -> `on_idle` Goal slices)."""
        if self.is_busy_fn():
            self._last_busy_mono = time.monotonic()
            return {"ok": True, "action": "busy_skip"}

        eff_now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        abort_fn = abort_check or (lambda: self._stop_event.is_set() or self.is_busy_fn())
        rm = get_routine_manager(store=self.store)
        sched = rm.tick_due_routines(now=eff_now, run_in_background=False, abort_check=abort_fn)
        if sched.get("fired"):
            return {"ok": True, "action": "scheduled_routine", "fired": sched["fired"]}

        if not self.store.is_background_autonomy_enabled():
            return {"ok": True, "action": "autonomy_off"}

        eff_idle = float(idle_seconds) if idle_seconds is not None else max(0.0, time.monotonic() - self._last_busy_mono)

        idle_rts = rm.select_eligible_idle_routines(idle_seconds=eff_idle, now_utc=eff_now, now_local=now_local)
        if idle_rts:
            chosen = idle_rts[0]
            with model_gate.background_context(abort_check=abort_fn):
                res = rm.run_routine_now(chosen["routine_id"], run_in_background=False, now=eff_now, chat_fn=self.chat_fn)
            return {"ok": bool(res.get("ok")), "action": "idle_routine", "routine_id": chosen["routine_id"], "result": res}

        from companion.projects import get_project_graph
        from companion.agent import run_autonomous_goal_slice
        idle_goals = get_project_graph(self.store).eligible_idle_goal_projects(idle_seconds=eff_idle, now_utc=eff_now, now_local=now_local)
        if idle_goals:
            proj = idle_goals[0]
            with model_gate.background_context(abort_check=abort_fn):
                s_res = run_autonomous_goal_slice(project_id=proj["project_id"], chat_fn=self.chat_fn, store=self.store)
            return {"ok": bool(s_res.get("ok")), "action": "idle_goal_slice", "project_id": proj["project_id"], "result": s_res}

        return {"ok": True, "action": "idle_noop"}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.wait(self.poll_interval_sec):
                try:
                    self.tick_once()
                except InterruptedError:
                    continue
                except Exception:
                    continue

        self._thread = threading.Thread(target=_loop, daemon=True, name="companion-idle-autonomy")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()


def execute_autonomy_mode_action(p: Dict[str, Any], actor: str = "user", store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """CoreAction handler for Background Autonomy, Idle Routines, and Morning Journal."""
    _ = actor
    mem = store or get_memory_store()
    rm = get_routine_manager(store=mem)
    sub = str(p.get("subaction") or p.get("action") or "status").strip().lower()

    if sub in ("set_autonomy", "on", "off", "toggle"):
        val = True if sub == "on" else (False if sub == "off" else (not mem.is_background_autonomy_enabled() if sub == "toggle" else bool(p.get("enabled", True))))
        mem.set_background_autonomy(val)
    elif sub in ("run", "run_now"):
        rid = str(p.get("routine_id") or "wiki_gardener")
        return rm.run_routine_now(rid, run_in_background=bool(p.get("run_in_background", False)))
    elif sub in ("morning", "journal"):
        entries = mem.get_autonomy_journal(limit=int(p.get("limit") or 15), mark_seen=bool(p.get("mark_seen", True)))
        return {"ok": True, "journal": entries, "summary": format_morning_briefing(store=mem, mark_seen=False, limit=int(p.get("limit") or 15))}

    bg_on = mem.is_background_autonomy_enabled()
    quad = describe_operating_quadrant(bg_on)
    routines = rm.list_routines()
    journal = mem.get_autonomy_journal(limit=10, mark_seen=False)
    return {
        "ok": True, "background_autonomy": bg_on, "immutable_app": True,
        "operating_quadrant": quad, "quadrant": quad, "routines": routines, "journal": journal, "summary": quad["summary"],
    }


def reconcile_startup_missed_runs(store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    mem = store or get_memory_store()
    key = str(mem.db_path.resolve())
    if key in _RECONCILED_DBS:
        return {"ok": True, "skipped": [], "caught_up": []}
    _RECONCILED_DBS.add(key)
    return RoutineManager(store=mem).reconcile_startup_missed_runs(run_in_background=True)


def get_routine_manager(store: Optional[MemoryStore] = None) -> RoutineManager:
    return RoutineManager(store=store)
