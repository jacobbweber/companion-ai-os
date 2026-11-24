"""
Unified Respectful Routines & `TriggerPolicy` Idle Autonomy Watcher
(`companion/routines.py` — A-Frame Application Layer).

Consolidates scheduled routines, idle-triggered maintenance routines (retiring `hobbies.py`),
Background Autonomy posture, and `/morning` Autonomy Journal under one `TriggerPolicy` contract.
"""

import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from companion.approvals import archive_aged_conversations, enqueue_session_nudge, get_approval_queue
from companion.agent import run_autonomous_goal_slice
from companion.background_jobs import get_job_manager
from companion.config import execution_origin_context
from companion.dag_runtime import execute_dag_by_name
from companion.endpoints import get_endpoint_manager
from companion.llm import chat, model_gate
from companion.memory_store import MemoryStore, get_memory_store
from companion.project_graph import get_project_graph
from companion.trigger_policy import TriggerPolicy, compute_next_scheduled_iso, evaluate_trigger_eligibility
from companion.vault import create_snapshot_backup
from companion.wiki_engine import get_wiki_engine

__all__ = [
    "BUILTIN_IDLE_ROUTINES", "IdleAutonomyWatcher", "RoutineManager", "VALID_ROUTINE_TARGETS",
    "compute_next_run_utc", "describe_operating_quadrant", "execute_autonomy_mode_action",
    "format_morning_briefing", "get_routine_manager", "reconcile_startup_missed_runs",
]

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
