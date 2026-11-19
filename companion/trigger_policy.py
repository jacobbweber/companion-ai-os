"""
Unified TriggerPolicy, Time-Window Gate & Deterministic Pre-Check Engine
(`companion/trigger_policy.py` — A-Frame Domain Layer).

Provides one shared, deterministic trigger contract across both `Routines` and
long-running `Goals`/`Projects`:
- Modes: `manual`, `scheduled` (`every <N>m|h|d`, `daily HH:MM`, 5-field cron), `on_idle`
- Configurable `allowed_window` (including overnight wrap-around windows such as `22:00-06:00`
  and optional weekday filters such as `mon-fri 22:00-06:00`)
- Cooldown spacing (`cooldown_seconds`) and idle thresholds (`idle_min_seconds`)
- `<5ms` zero-token deterministic Python pre-checks (`PRECHECK_REGISTRY`)
"""

import inspect
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple

from companion.config import settings
from companion.memory_store import MemoryStore, get_memory_store

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
