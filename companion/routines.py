"""
Respectful Hardware-Gated Routines & Clone-Based Self-Development Sandbox
(`companion/routines.py` — A-Frame Application Layer).

Implements Pillar 5 of the Companion 3.0 Harvest Blueprint:
1. SQLite-backed `routines` scheduler (`every <N>m|h|d`, `daily HH:MM`, and 5-field cron) targeting
    ONLY verified capability kinds (`recipe`, `wiki_inbox_triage`, `snapshot_backup`, `endpoint_pulse`,
   `chat_archive`) — mechanically refusing free-form unverified prompts and raw secrets.
2. Startup downtime reconciliation (`reconcile_startup_missed_runs`): zero thundering herd after downtime,
   1 quiet session summary nudge, single-fire `catch_up`, and overlap-skip protection.
3. Clone-Based Self-Development Sandbox (`create_sandbox_clone`, `verify_sandbox_clone`, `diff_sandbox_clone`,
   `cleanup_sandbox_clone`) isolating code experiments from the live Companion process until System Doctor passes.
"""

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from companion.archive_and_approvals import archive_aged_conversations, enqueue_session_nudge, get_approval_queue
from companion.background_jobs import get_job_manager
from companion.config import settings
from companion.disciplines import walk_pruned_files
from companion.endpoints import get_endpoint_manager
from companion.memory_store import MemoryStore, get_memory_store
from companion.vault import create_snapshot_backup
from companion.wiki_engine import get_wiki_engine

__all__ = [
    "VALID_ROUTINE_TARGETS", "RoutineManager", "cleanup_sandbox_clone",
    "compute_next_run_utc", "create_sandbox_clone", "diff_sandbox_clone",
    "get_routine_manager", "list_sandbox_clones", "reconcile_startup_missed_runs",
    "verify_sandbox_clone",
]

VALID_ROUTINE_TARGETS = ("recipe", "wiki_inbox_triage", "wiki_deep_curate", "snapshot_backup", "endpoint_pulse", "chat_archive")

ROUTINES_DDL: Tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS routines (routine_id TEXT PRIMARY KEY, title TEXT NOT NULL, schedule_expr TEXT NOT NULL, target_kind TEXT NOT NULL, target_name TEXT DEFAULT '', inputs_json TEXT DEFAULT '{}', enabled INTEGER DEFAULT 1, catch_up INTEGER DEFAULT 0, next_run_at TEXT NOT NULL, last_run_at TEXT DEFAULT '', last_job_id TEXT DEFAULT '', last_status TEXT DEFAULT 'idle', missed_count INTEGER DEFAULT 0, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
)

_RAW_SECRET_KEY_RE = re.compile(r"(password|secret|api_key|private_key|bearer|token_value)", re.I)
_RECONCILED_DBS: Set[str] = set()


def _slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", (raw or "").strip().lower()).strip("-")[:64]


def _match_cron_field(expr: str, value: int, lo: int, hi: int) -> bool:
    for part in expr.split(","):
        p = part.strip()
        step = 1
        if "/" in p:
            p, step_s = p.split("/", 1)
            step = max(1, int(step_s))
        if p == "*":
            if (value - lo) % step == 0:
                return True
        elif "-" in p:
            a_s, b_s = p.split("-", 1)
            a, b = int(a_s), int(b_s)
            if a <= value <= b and (value - a) % step == 0:
                return True
        elif int(p) == value:
            return True
    return False


def compute_next_run_utc(schedule_expr: str, after_dt: Optional[datetime] = None) -> datetime:
    """Parse `every <N>m|h|d`, `daily HH:MM`, or 5-field cron (`m h dom mon dow`) into the next UTC datetime."""
    base = (after_dt or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(second=0, microsecond=0)
    expr = re.sub(r"\s+", " ", (schedule_expr or "").strip().lower())
    m_every = re.fullmatch(r"every\s+(\d+)\s*([mhd])", expr)
    if m_every:
        amt = max(1, int(m_every.group(1)))
        unit = m_every.group(2)
        delta = timedelta(minutes=amt) if unit == "m" else (timedelta(hours=amt) if unit == "h" else timedelta(days=amt))
        return base + delta

    m_daily = re.fullmatch(r"daily\s+(\d{1,2}):(\d{2})", expr)
    if m_daily:
        hh, mm = int(m_daily.group(1)), int(m_daily.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(f"Invalid `daily HH:MM` time in `{schedule_expr}`.")
        cand = base.replace(hour=hh, minute=mm)
        return cand if cand > base else cand + timedelta(days=1)

    fields = expr.split(" ")
    if len(fields) == 5:
        probe = base + timedelta(minutes=1)
        for _ in range(525_600):
            cron_dow = (probe.weekday() + 1) % 7
            if (
                _match_cron_field(fields[0], probe.minute, 0, 59)
                and _match_cron_field(fields[1], probe.hour, 0, 23)
                and _match_cron_field(fields[2], probe.day, 1, 31)
                and _match_cron_field(fields[3], probe.month, 1, 12)
                and (_match_cron_field(fields[4], cron_dow, 0, 6) or (fields[4] == "7" and cron_dow == 0))
            ):
                return probe
            probe += timedelta(minutes=1)
    raise ValueError(f"Unsupported schedule `{schedule_expr}`. Use `every 30m`, `every 6h`, `daily 08:00`, or 5-field cron.")


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(iso_str: str) -> datetime:
    s = (iso_str or "").strip().rstrip("Z")
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class RoutineManager:
    """SQLite-persisted Respectful Routines with downtime missed-run reconciliation & overlap protection."""

    def __init__(self, store: Optional[MemoryStore] = None):
        self._store = store

    @property
    def store(self) -> MemoryStore:
        mem = self._store or get_memory_store()
        with mem._lock:
            for ddl in ROUTINES_DDL:
                mem.conn.execute(ddl)
            mem.conn.commit()
        return mem

    def create_or_update_routine(
        self, routine_id: str, schedule_expr: str, target_kind: str, title: str = "",
        target_name: str = "", inputs: Optional[Dict[str, Any]] = None, enabled: bool = True,
        catch_up: bool = False, actor: str = "user", confirmed: bool = False, now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Register a verified routine (`target_kind` in `VALID_ROUTINE_TARGETS`); gate Companion enables via ApprovalQueue."""
        rid = _slug(routine_id or title)
        tkind = (target_kind or "").strip().lower()
        if not rid or tkind not in VALID_ROUTINE_TARGETS:
            return {"ok": False, "error": f"`routine_id` and verified `target_kind` ({', '.join(VALID_ROUTINE_TARGETS)}) are required; free-form text prompts are forbidden."}
        if tkind == "recipe" and not (target_name or "").strip():
            return {"ok": False, "error": "`target_name` (workflow recipe name) is required when `target_kind='recipe'`."}
        clean_inputs = inputs or {}
        for k, v in clean_inputs.items():
            if _RAW_SECRET_KEY_RE.search(str(k)) or ("-----BEGIN " in str(v)):
                return {"ok": False, "error": f"ROUTINE VAULT GUARD: Do not embed raw secret field `{k}` in routine inputs; use symbolic Vault references."}
        try:
            next_dt = compute_next_run_utc(schedule_expr, after_dt=now)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        if enabled and actor.strip().lower() not in ("user", "cli", "web"):
            allowed, apr = get_approval_queue(self.store).check_or_enqueue(
                category="routine_enable", action_name="enable_routine", target=rid,
                summary=f"Enable recurring routine `{title or rid}` (`{schedule_expr}` -> `{tkind}:{target_name or 'default'}`).",
                payload={"routine_id": rid, "schedule": schedule_expr, "target_kind": tkind, "title": title, "target_name": target_name, "inputs": clean_inputs, "catch_up": catch_up},
                requester=actor, confirmed=confirmed, always_require_in_autonomous=True,
            )
            if not allowed:
                return {"ok": False, "awaiting_approval": True, "approval_id": apr["approval_id"], "error": f"ROUTINE APPROVAL REQUIRED (`{apr['approval_id']}`): Enabling recurring routine `{rid}` awaits user approval."}

        with self.store._lock:
            self.store.conn.execute(
                """INSERT INTO routines (routine_id, title, schedule_expr, target_kind, target_name, inputs_json, enabled, catch_up, next_run_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(routine_id) DO UPDATE SET
                     title=excluded.title, schedule_expr=excluded.schedule_expr, target_kind=excluded.target_kind,
                     target_name=excluded.target_name, inputs_json=excluded.inputs_json, enabled=excluded.enabled,
                     catch_up=excluded.catch_up, next_run_at=excluded.next_run_at, updated_at=CURRENT_TIMESTAMP""",
                (rid, (title.strip() or rid)[:100], schedule_expr.strip(), tkind, target_name.strip()[:80], json.dumps(clean_inputs), 1 if enabled else 0, 1 if catch_up else 0, _fmt_utc(next_dt)),
            )
            self.store.conn.commit()
        return {"ok": True, "routine": self.get_routine(rid), "summary": f"Saved routine `{rid}` (`{schedule_expr}` -> `{tkind}`, next_run=`{_fmt_utc(next_dt)}`)."}

    def get_routine(self, routine_id: str) -> Optional[Dict[str, Any]]:
        cols = ["routine_id", "title", "schedule_expr", "target_kind", "target_name", "inputs_json", "enabled", "catch_up", "next_run_at", "last_run_at", "last_job_id", "last_status", "missed_count", "updated_at"]
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM routines WHERE routine_id = ?", (_slug(routine_id),))
        if not rows:
            return None
        r = rows[0]
        r["inputs"] = json.loads(r.pop("inputs_json") or "{}")
        r["enabled"], r["catch_up"] = bool(r["enabled"]), bool(r["catch_up"])
        return r

    def list_routines(self) -> List[Dict[str, Any]]:
        ids = [str(r[0]) for r in self.store._fetch_rows("SELECT routine_id FROM routines ORDER BY routine_id ASC")]
        return [rt for rid in ids if (rt := self.get_routine(rid)) is not None]

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

    def _execute_routine_once(self, rt: Dict[str, Any], now_dt: datetime, run_in_background: bool) -> Dict[str, Any]:
        if self._is_overlapping(rt):
            return {"ok": True, "skipped_overlap": True, "routine_id": rt["routine_id"]}
        tkind, inputs = rt["target_kind"], rt.get("inputs") or {}
        jm = get_job_manager(store=self.store)
        job_id, status_lbl = "", "done"

        if tkind == "snapshot_backup":
            res = create_snapshot_backup(label=f"routine_{rt['routine_id']}", store=self.store)
            status_lbl = "done" if res.get("ok") else "failed"
        elif tkind == "chat_archive":
            res = archive_aged_conversations(store=self.store, older_than_days=int(inputs.get("older_than_days") or 90))
            status_lbl = "done" if res.get("ok") else "failed"
        elif tkind == "endpoint_pulse":
            em = get_endpoint_manager(store=self.store)
            targets = [inputs["endpoint_id"]] if inputs.get("endpoint_id") else [e["endpoint_id"] for e in em.list_endpoints()]
            results = [em.test_endpoint(eid, timeout=3.0) for eid in targets]
            offline = [eid for eid, r in zip(targets, results) if not r.get("ok")]
            if offline:
                enqueue_session_nudge(f"Routine `{rt['title']}` detected unreachable endpoint(s): {', '.join(offline)}.", category="routine", ref_id=rt["routine_id"], store=self.store)
            status_lbl = "done" if not offline else f"degraded ({len(offline)} offline)"
        elif tkind == "wiki_inbox_triage":
            inbox = get_wiki_engine(store=self.store).list_notes(folder="01_inbox", limit=30)
            goal = f"Run `wiki_manage(action='inspect_graduation', path=...)` across the {len(inbox)} note(s) in `01_inbox/` and write a triage digest in `01_inbox/inbox-triage-digest.md` recommending templates, overlap strategies, and ontology bridges without modifying `02_knowledge/`."
            dispatched = jm.dispatch_job(goal=goal, title=f"Routine: {rt['title']}", kind="routine", starter=f"routine:{rt['routine_id']}", allowed_tools=["wiki_manage"], run_in_background=run_in_background)
            job_id, status_lbl = str(dispatched.get("job_id") or ""), str(dispatched.get("status") or "queued")
        elif tkind == "wiki_deep_curate":
            wk = get_wiki_engine(store=self.store)
            inbox = [n for n in wk.list_notes(folder="01_inbox", limit=15) if not str(n["rel_path"]).endswith("inbox-triage-digest.md")]
            if not inbox:
                status_lbl = "done (inbox clean)"
            else:
                target_list = ", ".join(f"`{n['rel_path']}`" for n in inbox[:5])
                goal = (
                    f"Deliberately curate staged `01_inbox/` note(s) ({target_list}) into `02_knowledge/` with maximum quality: "
                    "1) Call `wiki_manage(action='inspect_graduation', path=...)` for each note; "
                    "2) Read EVERY high-overlap candidate in full via `wiki_manage(action='read', path=...)`; "
                    "3) Pick the exact governed template (`03_resources/templates/`) and strategy (`CREATE_NEW`, `MERGE_AND_REFINE`, `SPLIT_MULTI`, or `KEEP_SEPARATE`); "
                    "4) Call `wiki_manage(action='validate_curation', ...)` until 0 errors; "
                    "5) Call `wiki_manage(action='commit_curation', curation_id=...)`."
                )
                dispatched = jm.dispatch_job(goal=goal, title=f"Routine Deep Curate: {rt['title']}", kind="routine", starter=f"routine:{rt['routine_id']}", tier="reasoning", allowed_tools=["wiki_manage"], max_iters=16, run_in_background=run_in_background)
                job_id, status_lbl = str(dispatched.get("job_id") or ""), str(dispatched.get("status") or "queued")
        elif tkind == "recipe":
            dispatched = jm.dispatch_job(goal=f"Routine recipe {rt['target_name']}", title=f"Routine: {rt['title']}", kind="recipe", starter=f"routine:{rt['routine_id']}", recipe_name=rt["target_name"], recipe_params=inputs, run_in_background=run_in_background)
            job_id, status_lbl = str(dispatched.get("job_id") or ""), str(dispatched.get("status") or "queued")

        next_iso = _fmt_utc(compute_next_run_utc(rt["schedule_expr"], after_dt=now_dt))
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE routines SET last_run_at = ?, next_run_at = ?, last_job_id = ?, last_status = ?, updated_at = CURRENT_TIMESTAMP WHERE routine_id = ?",
                (_fmt_utc(now_dt), next_iso, job_id, status_lbl[:40], rt["routine_id"]),
            )
            self.store.conn.commit()
        return {"ok": True, "routine_id": rt["routine_id"], "job_id": job_id, "status": status_lbl, "next_run_at": next_iso}

    def reconcile_startup_missed_runs(self, now: Optional[datetime] = None, run_in_background: bool = True) -> Dict[str, Any]:
        """Advance overdue schedules after downtime without a thundering herd; run at most 1 catch-up per `catch_up=True` routine."""
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        skipped_routines: List[str] = []
        caught_up: List[str] = []
        for rt in self.list_routines():
            if not rt["enabled"]:
                continue
            due_dt = _parse_utc(rt["next_run_at"])
            if due_dt > now_dt:
                continue
            missed_slots, cursor = 0, due_dt
            while cursor <= now_dt and missed_slots < 500:
                missed_slots += 1
                cursor = compute_next_run_utc(rt["schedule_expr"], after_dt=cursor)
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

    def tick_due_routines(self, now: Optional[datetime] = None, run_in_background: bool = True) -> Dict[str, Any]:
        """Trigger any currently due enabled routines (respecting overlap skip & hardware priority)."""
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        fired, overlapped = [], []
        for rt in self.list_routines():
            if not rt["enabled"] or _parse_utc(rt["next_run_at"]) > now_dt:
                continue
            res = self._execute_routine_once(rt, now_dt, run_in_background=run_in_background)
            (overlapped if res.get("skipped_overlap") else fired).append(rt["routine_id"])
        return {"ok": True, "fired": fired, "skipped_overlap": overlapped}


def reconcile_startup_missed_runs(store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    mem = store or get_memory_store()
    key = str(mem.db_path.resolve())
    if key in _RECONCILED_DBS:
        return {"ok": True, "skipped": [], "caught_up": []}
    _RECONCILED_DBS.add(key)
    return RoutineManager(store=mem).reconcile_startup_missed_runs(run_in_background=True)


def get_routine_manager(store: Optional[MemoryStore] = None) -> RoutineManager:
    return RoutineManager(store=store)


# --- Clone-Based Self-Development Sandbox (Isolated from Live Companion Process) ---

_SANDBOX_COPY_DIRS = ("companion", "configs", "memory", "skills", "specs", "surfaces", "tests")
_SANDBOX_COPY_FILES = ("harness.py", "README.md")


def create_sandbox_clone(label: str = "experiment", src_root: Optional[Path] = None, sandboxes_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Clone Companion's tracked source tree into `data/sandboxes/<id>/` without copying `data/` or `secrets.env`."""
    src = Path(src_root or settings.root).resolve()
    dst_base = Path(sandboxes_dir or settings.sandboxes_dir).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    sid = f"sbx_{stamp}_{_slug(label or 'exp')[:24]}"
    clone_dir = dst_base / sid
    clone_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for dname in _SANDBOX_COPY_DIRS:
        sdir = src / dname
        if not sdir.is_dir():
            continue
        for p in walk_pruned_files(sdir):
            rel = p.relative_to(src)
            target = clone_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            copied += 1
    for fname in _SANDBOX_COPY_FILES:
        if (src / fname).is_file():
            shutil.copy2(src / fname, clone_dir / fname)
            copied += 1
    (clone_dir / ".sandbox_meta.json").write_text(json.dumps({"sandbox_id": sid, "label": label, "source_root": str(src), "copied_files": copied}), encoding="utf-8")
    return {"ok": True, "sandbox_id": sid, "path": str(clone_dir), "copied_files": copied, "summary": f"Created isolated self-dev clone `{sid}` ({copied} files)."}


def list_sandbox_clones(sandboxes_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    base = Path(sandboxes_dir or settings.sandboxes_dir).resolve()
    if not base.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for d in sorted(base.iterdir(), key=lambda p: p.name, reverse=True):
        mf = d / ".sandbox_meta.json"
        if d.is_dir() and mf.is_file():
            out.append({**json.loads(mf.read_text(encoding="utf-8")), "path": str(d)})
    return out


def verify_sandbox_clone(sandbox_id: str, run_tests: bool = False, sandboxes_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Run System Doctor in an isolated child process against a sandbox clone without touching live state."""
    clone_dir = Path(sandboxes_dir or settings.sandboxes_dir).resolve() / (sandbox_id or "").strip()
    if not clone_dir.is_dir():
        return {"ok": False, "error": f"Sandbox clone `{sandbox_id}` not found."}
    snippet = (
        "import json, sys; from pathlib import Path; from companion.safety import run_system_doctor; "
        "print(json.dumps(run_system_doctor(root=Path('.').resolve(), run_tests=(sys.argv[1]=='1'))))"
    )
    env = {**os.environ, "PYTHONPATH": str(clone_dir), "COMPANION_DATA_DIR": str(clone_dir / ".sandbox_data")}
    proc = subprocess.run([sys.executable, "-B", "-c", snippet, "1" if run_tests else "0"], cwd=str(clone_dir), env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0 or not proc.stdout.strip():
        err = (proc.stderr or proc.stdout or "Sandbox Doctor subprocess failed.").strip()[-500:]
        return {"ok": False, "sandbox_id": sandbox_id, "error": err, "summary": f"Sandbox `{sandbox_id}` Doctor: FAIL ({err[:160]})."}
    rep = json.loads(proc.stdout.strip().splitlines()[-1])
    ok = bool(rep.get("healthy"))
    return {"ok": ok, "sandbox_id": sandbox_id, "doctor": rep, "summary": f"Sandbox `{sandbox_id}` Doctor: {'PASS' if ok else 'FAIL'}."}


def diff_sandbox_clone(sandbox_id: str, src_root: Optional[Path] = None, sandboxes_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Generate a unified diff between an isolated sandbox clone and the live Companion workspace."""
    live = Path(src_root or settings.root).resolve()
    clone = Path(sandboxes_dir or settings.sandboxes_dir).resolve() / (sandbox_id or "").strip()
    if not clone.is_dir():
        return {"ok": False, "error": f"Sandbox clone `{sandbox_id}` not found."}
    diffs: List[str] = []
    changed_files: List[str] = []
    for dname in _SANDBOX_COPY_DIRS:
        cdir = clone / dname
        if not cdir.is_dir():
            continue
        for cp in (p for p in walk_pruned_files(cdir) if p.suffix.lower() == ".py"):
            rel = str(cp.relative_to(clone)).replace("\\", "/")
            lp = live / rel
            old_lns = lp.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if lp.is_file() else []
            new_lns = cp.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            if old_lns != new_lns:
                changed_files.append(rel)
                diffs.extend(difflib.unified_diff(old_lns, new_lns, fromfile=f"live/{rel}", tofile=f"sandbox/{rel}", n=2))
    return {"ok": True, "sandbox_id": sandbox_id, "changed_files": changed_files, "diff": "".join(diffs)[:12000]}


def cleanup_sandbox_clone(sandbox_id: str, sandboxes_dir: Optional[Path] = None) -> Dict[str, Any]:
    clone = Path(sandboxes_dir or settings.sandboxes_dir).resolve() / (sandbox_id or "").strip()
    if not clone.is_dir():
        return {"ok": False, "error": f"Sandbox clone `{sandbox_id}` not found."}
    shutil.rmtree(clone, ignore_errors=True)
    return {"ok": True, "sandbox_id": sandbox_id, "summary": f"Deleted sandbox clone `{sandbox_id}`."}
