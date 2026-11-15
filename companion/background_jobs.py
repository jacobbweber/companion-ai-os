"""
Forever-Chat Background Job Dispatcher, Zero-Identity Worker Briefs & Departures Board
(`companion/background_jobs.py` — A-Frame Application Layer).

Implements Pillar 1 of the Companion 3.0 Harvest Blueprint:
1. Keeps the user's primary Forever Chat clean by offloading multi-step research, audits, recipes,
   and federated/routine work into background jobs (`queued`, `running`, `waiting_approval`, `done`,
   `failed`, `cancelled`, `timed_out`, `interrupted`).
2. Crash-safe startup recovery marks leftover `running` jobs as `interrupted` without blind replay.
3. Zero-Identity Worker Briefs + Complementary Tool Pairing run under `ModelGate.background_context()`
   so live foreground chat always holds GPU priority.
4. `<=200`-token (`<=800` char) Result Envelope marshalling overflows detailed reports to `01_inbox/`
   in the Knowledge Wiki Warehouse and delivers a quiet session-scoped nudge at turn boundaries.
"""

import contextvars
import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from companion.archive_and_approvals import enqueue_session_nudge
from companion.config import settings
from companion.llm import chat, model_gate, normalize_assistant_message, recover_empty_synthesis, tier_context
from companion.memory_store import MemoryStore, get_memory_store, memory_store_context
from companion.telemetry import redact_secrets, telemetry
from companion.tool_schemas import TOOLS
from companion.wiki_curation import pending_curation_nudge
from companion.wiki_engine import get_wiki_engine

__all__ = [
    "JOB_STATUSES",
    "MAX_ENVELOPE_CHARS",
    "BackgroundJobManager",
    "get_job_manager",
    "marshal_job_envelope",
    "resolve_paired_worker_tools",
]

JOB_STATUSES = ("queued", "running", "waiting_approval", "done", "failed", "cancelled", "timed_out", "interrupted")
MAX_ENVELOPE_CHARS = 780

JOBS_DDL: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS background_jobs (
        job_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        goal TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'worker',
        starter TEXT NOT NULL DEFAULT 'chat',
        session_id TEXT NOT NULL DEFAULT 'main',
        project_id TEXT NOT NULL DEFAULT 'companion-self',
        tier TEXT NOT NULL DEFAULT 'fast',
        status TEXT NOT NULL DEFAULT 'queued',
        allowed_tools_json TEXT DEFAULT '[]',
        max_iters INTEGER DEFAULT 8,
        timeout_sec INTEGER DEFAULT 600,
        envelope_summary TEXT DEFAULT '',
        overflow_wiki_path TEXT DEFAULT '',
        full_output TEXT DEFAULT '',
        partial_output TEXT DEFAULT '',
        approval_id TEXT DEFAULT '',
        error TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        started_at DATETIME DEFAULT '',
        finished_at DATETIME DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_bg_jobs_status ON background_jobs(status, created_at DESC)",
)

_COMPLEMENTARY_TOOL_PAIRS: Dict[str, Tuple[str, ...]] = {
    "write_file": ("read_file", "list_dir", "search"),
    "edit_file": ("read_file", "list_dir", "search"),
    "run_python": ("read_file", "list_dir"),
    "run_shell": ("read_file", "list_dir"),
    "wiki_manage": ("read_file", "search", "memory_manage"),
    "endpoint_manage": ("vault_manage", "wiki_manage"),
    "mcp_manage": ("vault_manage", "wiki_manage"),
}

_DEFAULT_WORKER_TOOLS: Tuple[str, ...] = (
    "read_file", "list_dir", "search", "web_fetch", "memory_manage",
    "wiki_manage", "vault_manage", "endpoint_manage", "mcp_manage", "system_doctor",
)

_RECOVERED_DBS: Set[str] = set()
_CANCEL_EVENTS: Dict[str, threading.Event] = {}
_JOBS_LOCK = threading.RLock()


def resolve_paired_worker_tools(requested: Optional[List[str]] = None) -> List[str]:
    """Ensure every worker receives complementary read/inspect tools alongside any action tool (never `job_manage`)."""
    base = [str(t).strip() for t in (requested or list(_DEFAULT_WORKER_TOOLS)) if str(t).strip()]
    known = {t["function"]["name"] for t in TOOLS}
    expanded: List[str] = []
    for name in base:
        if name == "job_manage":
            continue
        if name in known and name not in expanded:
            expanded.append(name)
        for mate in _COMPLEMENTARY_TOOL_PAIRS.get(name, ()):
            if mate in known and mate not in expanded:
                expanded.append(mate)
    return expanded


def marshal_job_envelope(
    job_id: str, title: str, raw_output: str, store: Optional[MemoryStore] = None,
    max_chars: int = MAX_ENVELOPE_CHARS, skip_inbox_overflow: bool = False,
) -> Tuple[str, str]:
    """Guarantee a `<=200`-token (`<=max_chars`) summary envelope; overflow full reports to `01_inbox/`."""
    clean = redact_secrets((raw_output or "").strip())
    if not clean:
        return "(Completed with no output.)", ""
    if len(clean) <= max_chars:
        return clean, ""

    overflow_rel = ""
    if not skip_inbox_overflow:
        wiki = get_wiki_engine(store=store)
        slug = re.sub(r"[^a-z0-9]+", "-", f"{job_id}-{title}".lower()).strip("-")[:64]
        cap = wiki.capture_inbox(
            title=f"Job Report: {title[:90]} ({job_id})",
            content=clean,
            tags=["background-job", job_id],
            source=f"background_job:{job_id}",
            slug=slug,
            actor="companion",
        )
        overflow_rel = str(cap.get("path") or "")
    link_note = f"\n[Full report saved to Wiki Inbox: `{overflow_rel}`]" if overflow_rel else f"\n[Full curation log stored in job `{job_id}`]"
    head_budget = max(160, max_chars - len(link_note) - 6)
    paras = [p.strip() for p in clean.splitlines() if p.strip()]
    concise = " | ".join(paras[:4])
    if len(concise) > head_budget:
        concise = concise[: head_budget - 3].rstrip() + "..."
    return f"{concise}{link_note}", overflow_rel


def _extract_approval_pause(tool_result: str) -> Optional[str]:
    m = re.search(r"\b(apr_\d+_[a-f0-9]+)\b", tool_result or "")
    if m and ("APPROVAL" in tool_result.upper() or "AWAITS" in tool_result.upper() or "REQUIRED" in tool_result.upper()):
        return m.group(1)
    return None


class BackgroundJobManager:
    """Persistent SQLite-backed Background Job Dispatcher & Departures Board."""

    def __init__(
        self,
        store: Optional[MemoryStore] = None,
        chat_fn: Callable[..., Dict[str, Any]] = chat,
        tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None,
    ):
        self._store = store
        self._chat_fn = chat_fn
        self._tool_fn = tool_fn

    @property
    def store(self) -> MemoryStore:
        mem = self._store or get_memory_store()
        db_key = str(mem.db_path.resolve())
        mem.ensure_schema("background_jobs", lambda m: [m.conn.execute(ddl) for ddl in JOBS_DDL] and None)
        if db_key not in _RECOVERED_DBS:
            with _JOBS_LOCK:
                if db_key not in _RECOVERED_DBS:
                    _RECOVERED_DBS.add(db_key)
                    self.recover_interrupted_jobs(mem)
        return mem

    def _dispatch_tool(self, name: str, args: Dict[str, Any]) -> str:
        if self._tool_fn is not None:
            return self._tool_fn(name, args)
        from companion.tools import dispatch

        return dispatch(name, args)

    def recover_interrupted_jobs(self, store: Optional[MemoryStore] = None) -> int:
        """Mark leftover `running` jobs from a crashed/stopped process as `interrupted` without blind replay."""
        mem = store or (self._store or get_memory_store())
        with mem._lock:
            cur = mem.conn.execute(
                """UPDATE background_jobs
                   SET status = 'interrupted',
                       error = CASE WHEN error = '' THEN 'Interrupted by process restart; inspect partial_output or resume.' ELSE error END,
                       finished_at = CURRENT_TIMESTAMP
                   WHERE status = 'running'"""
            )
            mem.conn.commit()
            return int(cur.rowcount or 0)

    def is_cancel_requested(self, job_id: str) -> bool:
        with _JOBS_LOCK:
            ev = _CANCEL_EVENTS.get(job_id)
            if ev is not None and ev.is_set():
                return True
        row = self.get_job(job_id)
        return bool(row and row.get("status") == "cancelled")

    def dispatch_job(
        self,
        goal: str,
        title: str = "",
        kind: str = "worker",
        starter: str = "chat",
        session_id: str = "main",
        tier: str = "fast",
        allowed_tools: Optional[List[str]] = None,
        max_iters: int = 8,
        timeout_sec: int = 600,
        run_in_background: bool = True,
        recipe_name: str = "",
        recipe_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Enqueue and start a background job, returning immediately (`queued`/`running`) or synchronously when `run_in_background=False`."""
        clean_goal = redact_secrets((goal or "").strip())
        if not clean_goal and not recipe_name:
            return {"ok": False, "error": "Job `goal` (or `recipe_name`) is required."}
        eff_title = (title.strip() or (f"Recipe: {recipe_name}" if recipe_name else clean_goal.splitlines()[0]))[:110]
        eff_kind = kind if kind in ("worker", "recipe", "routine", "mcp_inbound") else "worker"
        eff_tier = (tier or "fast").strip().lower()
        paired_tools = resolve_paired_worker_tools(allowed_tools)
        job_id = f"bgj_{int(time.time() * 1000)}_{os.urandom(3).hex()}"
        pid = settings.get_workspace().project_id

        meta_goal = json.dumps({"recipe": recipe_name, "params": recipe_params or {}, "goal": clean_goal}) if eff_kind == "recipe" and recipe_name else clean_goal
        with self.store._lock:
            self.store.conn.execute(
                """INSERT INTO background_jobs
                   (job_id, title, goal, kind, starter, session_id, project_id, tier, status, allowed_tools_json, max_iters, timeout_sec)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
                (
                    job_id, eff_title, meta_goal, eff_kind, (starter or "chat")[:32],
                    (session_id or "main")[:64], pid, eff_tier, json.dumps(paired_tools),
                    max(1, min(int(max_iters or 8), 20)), max(5, int(timeout_sec or 600)),
                ),
            )
            self.store.conn.commit()

        with _JOBS_LOCK:
            _CANCEL_EVENTS[job_id] = threading.Event()

        if run_in_background:
            ctx = contextvars.copy_context()
            threading.Thread(target=lambda: ctx.run(self._execute_job, job_id), daemon=True, name=f"companion-{job_id}").start()
            return {
                "ok": True, "job_id": job_id, "status": "queued", "title": eff_title, "paired_tools": paired_tools,
                "summary": f"Dispatched background job `{job_id}` (`{eff_title}`) — foreground chat remains free.",
            }

        self._execute_job(job_id)
        done_row = self.get_job(job_id) or {"job_id": job_id, "status": "unknown"}
        return {
            "ok": done_row.get("status") in ("done", "waiting_approval"),
            "job_id": job_id,
            "status": done_row.get("status"),
            "title": eff_title,
            "envelope_summary": done_row.get("envelope_summary", ""),
            "overflow_wiki_path": done_row.get("overflow_wiki_path", ""),
            "approval_id": done_row.get("approval_id", ""),
            "error": done_row.get("error", ""),
            "summary": done_row.get("envelope_summary") or done_row.get("error") or f"Job `{job_id}` finished ({done_row.get('status')}).",
        }

    def _run_zero_identity_worker(self, job: Dict[str, Any], deadline_mono: float) -> Tuple[str, str, str]:
        """Run a lean Zero-Identity Worker loop; return `(final_status, raw_output, approval_id_or_err)`."""
        allowed_names = set(job.get("allowed_tools") or _DEFAULT_WORKER_TOOLS) - {"job_manage"}
        tool_schemas = [t for t in TOOLS if t["function"]["name"] in allowed_names]
        ws_root = settings.get_workspace().project_root
        sys_msg = (
            "[ZERO-IDENTITY BACKGROUND WORKER BRIEF]\n"
            f"Job ID: {job['job_id']} | Title: {job['title']}\n"
            f"Workspace Root: {ws_root}\n"
            "Rules:\n"
            "1. Execute the assigned task concisely using your paired tools.\n"
            "2. Never invent facts; cite exact files, endpoints, or notes inspected.\n"
            "3. If a tool returns an APPROVAL REQUIRED message (`apr_...`), stop immediately and report that approval ID.\n"
            "4. Return a crisp, outcome-first report."
        )
        prior_partial = str(job.get("partial_output") or "").strip()
        user_brief = str(job["goal"]) + (f"\n\n[PRIOR PARTIAL PROGRESS]\n{prior_partial[-1500:]}" if prior_partial else "")
        msgs: List[Dict[str, Any]] = [{"role": "system", "content": sys_msg}, {"role": "user", "content": user_brief}]
        partial_chunks: List[str] = []
        max_steps = max(1, int(job.get("max_iters") or 8))
        job_iso, cur_nudges = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), 0

        for _step in range(max_steps):
            if self.is_cancel_requested(job["job_id"]):
                return "cancelled", "\n".join(partial_chunks), "Cancelled by user."
            if time.monotonic() > deadline_mono:
                return "timed_out", "\n".join(partial_chunks), f"Timed out after {job.get('timeout_sec')}s."
            data = self._chat_fn(msgs, tool_schemas if tool_schemas else None, "auto" if tool_schemas else "none")
            clean_txt, think_txt, tcalls = normalize_assistant_message((data.get("choices") or [{}])[0].get("message") or {})
            if not tcalls:
                cur_nudge = pending_curation_nudge(self.store, job_iso, clean_txt, require_intent_phrase=False) if ("wiki_manage" in allowed_names and _step + 1 < max_steps and cur_nudges < 2) else None
                if cur_nudge:
                    cur_nudges += 1
                    msgs.extend([{"role": "assistant", "content": clean_txt or "Continuing curation."}, {"role": "user", "content": cur_nudge}])
                    continue
                final_txt = clean_txt if clean_txt.strip() else recover_empty_synthesis(msgs, self._chat_fn, prior_thinking=think_txt)
                return "done", final_txt, ""
            msgs.append({"role": "assistant", "content": clean_txt, "tool_calls": tcalls})
            for tc in tcalls:
                fn = tc.get("function") or {}
                tname = str(fn.get("name") or "")
                raw_a = fn.get("arguments") or "{}"
                try:
                    targs = json.loads(raw_a) if isinstance(raw_a, str) else dict(raw_a)
                except Exception:
                    targs = {}
                res = (
                    "error: recursive `job_manage` is disabled inside background workers"
                    if tname == "job_manage" or tname not in allowed_names
                    else self._dispatch_tool(tname, targs)
                )
                partial_chunks.append(f"{tname}: {res[:240]}")
                self._save_partial(job["job_id"], "\n".join(partial_chunks[-8:]))
                apr_id = _extract_approval_pause(res)
                if apr_id:
                    return "waiting_approval", "\n".join(partial_chunks), apr_id
                msgs.append({"role": "tool", "tool_call_id": tc.get("id"), "content": res})

        synth_msgs = msgs + [{"role": "user", "content": "Iteration cap reached. Summarize findings concisely without calling tools."}]
        synth_data = self._chat_fn(synth_msgs, None, "none")
        final_s, _, _ = normalize_assistant_message((synth_data.get("choices") or [{}])[0].get("message") or {})
        return "done", final_s or "\n".join(partial_chunks), ""

    def _save_partial(self, job_id: str, partial_text: str) -> None:
        with self.store._lock:
            self.store.conn.execute("UPDATE background_jobs SET partial_output = ? WHERE job_id = ?", (redact_secrets(partial_text)[:4000], job_id))
            self.store.conn.commit()

    def _execute_job(self, job_id: str) -> None:
        mem = self.store
        job = self.get_job(job_id)
        if not job or job["status"] == "cancelled":
            return
        with mem._lock:
            mem.conn.execute("UPDATE background_jobs SET status = 'running', started_at = CURRENT_TIMESTAMP, error = '' WHERE job_id = ?", (job_id,))
            mem.conn.commit()

        deadline = time.monotonic() + float(job.get("timeout_sec") or 600)
        tid = telemetry.start_trace("background_job", f"{job_id}:{job['title'][:60]}") if mem.db_path.resolve() == settings.db_path.resolve() else None
        try:
            with memory_store_context(mem), tier_context(job.get("tier") or "fast"), model_gate.background_context(abort_check=lambda: self.is_cancel_requested(job_id)):
                if job["kind"] == "recipe" and str(job["goal"]).startswith("{"):
                    spec = json.loads(job["goal"])
                    from companion.workflow_graph import workflow_engine

                    rec_res = workflow_engine.run_workflow_recipe(str(spec.get("recipe") or ""), params=spec.get("params") or {}, tool_fn=self._dispatch_tool)
                    rec_ok = rec_res.get("status") == "COMPLETE" and bool(rec_res.get("valid", True))
                    st, raw_out, meta_err = ("done" if rec_ok else "failed"), str(rec_res.get("summary") or ""), ("" if rec_ok else str(rec_res.get("summary") or "Recipe aborted")[:300])
                else:
                    st, raw_out, meta_err = self._run_zero_identity_worker(job, deadline)
        except InterruptedError:
            st, raw_out, meta_err = "cancelled", str(job.get("partial_output") or ""), "Cancelled during background wait."
        except Exception as exc:
            st, raw_out, meta_err = "failed", str(job.get("partial_output") or ""), f"{type(exc).__name__}: {exc}"
        finally:
            if tid:
                telemetry.end_trace(tid, status="ok" if st in ("done", "waiting_approval") else "error")
            with _JOBS_LOCK:
                _CANCEL_EVENTS.pop(job_id, None)

        if st == "done":
            is_curation_job = job.get("starter") == "wiki_studio" or any(k in job["title"].lower() for k in ("wiki curation", "wiki_deep_curate", "routine deep curate"))
            env_sum, overflow_path = marshal_job_envelope(job_id, job["title"], raw_out, store=mem, skip_inbox_overflow=is_curation_job)
            with mem._lock:
                mem.conn.execute(
                    """UPDATE background_jobs
                       SET status = 'done', envelope_summary = ?, overflow_wiki_path = ?, full_output = ?, finished_at = CURRENT_TIMESTAMP
                       WHERE job_id = ?""",
                    (env_sum, overflow_path, redact_secrets(raw_out)[:32000], job_id),
                )
                mem.conn.commit()
            enqueue_session_nudge(f"Job `{job['title']}` completed -> {env_sum[:170]}", category="job", session_id=job["session_id"], ref_id=job_id, store=mem)
        elif st == "waiting_approval":
            msg = f"Job `{job['title']}` paused awaiting your approval (`{meta_err}`)."
            with mem._lock:
                mem.conn.execute(
                    "UPDATE background_jobs SET status = 'waiting_approval', approval_id = ?, envelope_summary = ?, partial_output = ? WHERE job_id = ?",
                    (meta_err, msg, redact_secrets(raw_out)[:4000], job_id),
                )
                mem.conn.commit()
            enqueue_session_nudge(msg, category="approval", session_id=job["session_id"], ref_id=meta_err, store=mem)
        else:
            err_clean = redact_secrets(meta_err or raw_out or st)[:400]
            with mem._lock:
                mem.conn.execute(
                    "UPDATE background_jobs SET status = ?, error = ?, partial_output = ?, finished_at = CURRENT_TIMESTAMP WHERE job_id = ?",
                    (st, err_clean, redact_secrets(raw_out)[:4000], job_id),
                )
                mem.conn.commit()
            enqueue_session_nudge(f"Job `{job['title']}` ended with status `{st}`: {err_clean[:140]}", category="job", session_id=job["session_id"], ref_id=job_id, store=mem)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        cols = [
            "job_id", "title", "goal", "kind", "starter", "session_id", "project_id", "tier", "status",
            "allowed_tools_json", "max_iters", "timeout_sec", "envelope_summary", "overflow_wiki_path",
            "full_output", "partial_output", "approval_id", "error", "created_at", "started_at", "finished_at",
        ]
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM background_jobs WHERE job_id = ?", ((job_id or "").strip(),))
        if not rows:
            return None
        r = rows[0]
        r["allowed_tools"] = json.loads(r.pop("allowed_tools_json") or "[]")
        return r

    def list_jobs(self, status: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        cols = [
            "job_id", "title", "kind", "starter", "session_id", "tier", "status",
            "envelope_summary", "overflow_wiki_path", "approval_id", "error", "created_at", "started_at", "finished_at",
        ]
        if status and status.strip().lower() not in ("", "all"):
            return self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM background_jobs WHERE status = ? ORDER BY rowid DESC LIMIT ?", (status.strip().lower(), max(1, int(limit))))
        return self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM background_jobs ORDER BY rowid DESC LIMIT ?", (max(1, int(limit)),))

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        row = self.get_job(job_id)
        if not row:
            return {"ok": False, "error": f"Background job `{job_id}` not found."}
        with _JOBS_LOCK:
            ev = _CANCEL_EVENTS.get(row["job_id"])
            if ev is not None:
                ev.set()
        if row["status"] in ("queued", "running", "waiting_approval"):
            with self.store._lock:
                self.store.conn.execute("UPDATE background_jobs SET status = 'cancelled', error = 'Cancelled by user.', finished_at = CURRENT_TIMESTAMP WHERE job_id = ?", (row["job_id"],))
                self.store.conn.commit()
        return {"ok": True, "job_id": row["job_id"], "status": "cancelled", "summary": f"Cancelled background job `{row['job_id']}`."}

    def resume_job(self, job_id: str, run_in_background: bool = True) -> Dict[str, Any]:
        """Resume an `interrupted` or approved `waiting_approval` background job."""
        row = self.get_job(job_id)
        if not row:
            return {"ok": False, "error": f"Background job `{job_id}` not found."}
        if row["status"] not in ("waiting_approval", "interrupted", "failed", "timed_out"):
            return {"ok": False, "error": f"Job `{job_id}` has status `{row['status']}` and cannot be resumed."}
        with self.store._lock:
            self.store.conn.execute("UPDATE background_jobs SET status = 'queued', approval_id = '', error = '' WHERE job_id = ?", (row["job_id"],))
            self.store.conn.commit()
        with _JOBS_LOCK:
            _CANCEL_EVENTS[row["job_id"]] = threading.Event()
        if run_in_background:
            ctx = contextvars.copy_context()
            threading.Thread(target=lambda: ctx.run(self._execute_job, row["job_id"]), daemon=True).start()
            return {"ok": True, "job_id": row["job_id"], "status": "queued", "summary": f"Resumed background job `{row['job_id']}`."}
        self._execute_job(row["job_id"])
        after = self.get_job(row["job_id"]) or row
        return {"ok": after.get("status") == "done", "job_id": row["job_id"], "status": after.get("status"), "summary": after.get("envelope_summary") or after.get("error", "")}

    def format_departures_board(self, limit: int = 15) -> str:
        items = self.list_jobs(limit=limit)
        lines = ["=== Background Jobs Departures Board ==="]
        if not items:
            lines.append("  (No background jobs dispatched yet.)")
            return "\n".join(lines)
        for j in items:
            apr = f" | approval={j['approval_id']}" if j.get("approval_id") else ""
            wiki = f" | wiki=`{j['overflow_wiki_path']}`" if j.get("overflow_wiki_path") else ""
            raw_det = (j.get("envelope_summary") or j.get("error") or "").splitlines()
            detail = raw_det[0][:120] if raw_det else ""
            lines.append(
                f"  * `{j['job_id']}` [{j['status'].upper():16s} | {j['kind']}:{j['tier']}{apr}{wiki}] {j['title']}"
                + (f"\n    -> {detail}" if detail else "")
            )
        return "\n".join(lines)


def get_job_manager(
    store: Optional[MemoryStore] = None,
    chat_fn: Callable[..., Dict[str, Any]] = chat,
    tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None,
) -> BackgroundJobManager:
    return BackgroundJobManager(store=store, chat_fn=chat_fn, tool_fn=tool_fn)
