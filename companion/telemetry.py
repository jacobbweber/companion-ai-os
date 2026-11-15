"""
Unified Observability, Waterfall Tracing, Structured Logging & KPI Engine
(A-Frame Infrastructure Layer).

Provides thread-safe, `ContextVar`-isolated spine-level telemetry stored in SQLite (`data/conversations.db`) so
both Companion and any Frontend Web UI / external application share a single source of truth for:
1. Waterfall Execution Traces (`telemetry_traces` & `telemetry_spans`)
2. Structured Application & Harness Logs (`telemetry_logs`: DEBUG, INFO, WARN, ERROR)
3. Aggregated Performance & Reliability KPIs (`get_kpis`, `format_kpi_dashboard`, `prune_old_telemetry`)
"""

import contextlib
import contextvars
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from companion.config import settings


_SECRET_REGEXES: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{12,}|nvapi-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|hf_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_\-]{20,}|AKIA[A-Z0-9]{12,})\b"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|access[_-]?token|auth[_-]?token)\s*([:=])\s*(['\"]?)([^\s'\",;}{]{6,})\3"), r"\1\2\3[REDACTED]\3"),
)


class SecretRedactor:
    """Scrubs API keys, Bearer tokens, and known environment secrets before SQLite telemetry persistence."""

    def __init__(self, extra_secrets: Optional[List[str]] = None):
        self._lock = threading.RLock()
        self._extra = [s for s in (extra_secrets or []) if isinstance(s, str) and len(s.strip()) >= 8]

    def register_secret(self, secret: str) -> None:
        clean = (secret or "").strip()
        if len(clean) < 8:
            return
        with self._lock:
            if clean not in self._extra:
                self._extra.append(clean)

    def redact(self, text: str) -> str:
        if not text:
            return ""
        scrubbed = str(text)
        with self._lock:
            known: List[str] = list(self._extra)
        if settings.api_key and len(settings.api_key) >= 8:
            known.append(settings.api_key)
        for env_k, env_v in os.environ.items():
            if len(env_v) >= 8 and env_k.upper().endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")) and not env_v.startswith(("http://", "https://")):
                known.append(env_v)
        for secret in sorted(set(known), key=len, reverse=True):
            if secret in scrubbed:
                scrubbed = scrubbed.replace(secret, "[REDACTED]")
        for rx, repl in _SECRET_REGEXES:
            scrubbed = rx.sub(repl, scrubbed)
        return scrubbed


secret_redactor = SecretRedactor()


def redact_secrets(text: str) -> str:
    """Convenience helper to scrub secrets from a telemetry string."""
    return secret_redactor.redact(text)


TELEMETRY_DDL = (
    """CREATE TABLE IF NOT EXISTS telemetry_traces (
        trace_id TEXT PRIMARY KEY,
        trace_type TEXT NOT NULL,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        started_epoch REAL NOT NULL,
        duration_ms REAL DEFAULT 0.0,
        span_count INTEGER DEFAULT 0,
        error_count INTEGER DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS telemetry_spans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trace_id TEXT NOT NULL,
        span_type TEXT NOT NULL,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ok',
        offset_ms REAL DEFAULT 0.0,
        duration_ms REAL NOT NULL,
        detail TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS telemetry_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT NOT NULL,
        component TEXT NOT NULL,
        trace_id TEXT DEFAULT '',
        message TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tel_spans_trace ON telemetry_spans(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_tel_spans_type ON telemetry_spans(span_type)",
    "CREATE INDEX IF NOT EXISTS idx_tel_logs_level_comp ON telemetry_logs(level, component)",
    "CREATE INDEX IF NOT EXISTS idx_tel_logs_trace ON telemetry_logs(trace_id)",
)

_CONTEXTUAL_TELEMETRY_DB: contextvars.ContextVar[Optional[Path]] = contextvars.ContextVar(
    "companion_contextual_telemetry_db", default=None
)
_CTX_DB_LOCK = threading.RLock()
_CTX_CONNS: Dict[str, sqlite3.Connection] = {}
_CTX_REFCOUNTS: Dict[str, int] = {}


def _init_telemetry_conn(conn: sqlite3.Connection, wal: bool = True) -> sqlite3.Connection:
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
    for ddl in TELEMETRY_DDL:
        conn.execute(ddl)
    conn.commit()
    return conn


@contextlib.contextmanager
def telemetry_db_context(db_path: Optional[Path]) -> Iterator[None]:
    """Route global `telemetry` writes/reads to `db_path` within this `ContextVar` scope, closing temp handles cleanly."""
    if db_path is None:
        yield
        return
    resolved = Path(db_path).resolve()
    is_custom = resolved != settings.db_path.resolve()
    key = str(resolved)
    if is_custom:
        with _CTX_DB_LOCK:
            _CTX_REFCOUNTS[key] = _CTX_REFCOUNTS.get(key, 0) + 1
    tok = _CONTEXTUAL_TELEMETRY_DB.set(resolved if is_custom else None)
    try:
        yield
    finally:
        _CONTEXTUAL_TELEMETRY_DB.reset(tok)
        if is_custom:
            with telemetry._lock, _CTX_DB_LOCK:
                rem = _CTX_REFCOUNTS.get(key, 1) - 1
                if rem <= 0:
                    _CTX_REFCOUNTS.pop(key, None)
                    c = _CTX_CONNS.pop(key, None)
                    if c is not None:
                        try:
                            c.close()
                        except Exception:
                            pass
                else:
                    _CTX_REFCOUNTS[key] = rem


class TelemetryEngine:
    """Thread-safe SQLite-backed Waterfall Tracer, Structured Logger, and KPI Aggregator."""

    def __init__(self, db_path: Optional[Path] = None):
        self._lock = threading.RLock()
        self._explicit_db = db_path is not None
        self.db_path = Path(db_path or settings.db_path)
        self.conn: Optional[sqlite3.Connection] = None
        self._conn_path: Optional[Path] = None
        self._test_mem_conn: Optional[sqlite3.Connection] = None
        if self._explicit_db:
            self._ensure_disk_conn(self.db_path)
        self._active_trace_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
            f"companion_trace_{id(self)}", default=None
        )
        self._trace_starts: Dict[str, float] = {}
        self._ended_counter = 0

    def _ensure_disk_conn(self, target_path: Path) -> sqlite3.Connection:
        resolved = Path(target_path).resolve()
        if self.conn is not None and self._conn_path == resolved:
            return self.conn
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self.conn = _init_telemetry_conn(sqlite3.connect(str(resolved), timeout=30.0, check_same_thread=False), wal=True)
        self._conn_path = resolved
        self.db_path = resolved
        return self.conn

    @property
    def active_trace_id(self) -> Optional[str]:
        return self._active_trace_var.get()

    @active_trace_id.setter
    def active_trace_id(self, value: Optional[str]) -> None:
        self._active_trace_var.set(value)

    def _active_conn(self) -> sqlite3.Connection:
        """Resolve the active SQLite connection (honoring explicit DBs, `telemetry_db_context`, and test isolation)."""
        if self._explicit_db:
            return self._ensure_disk_conn(self.db_path)
        ctx_db = _CONTEXTUAL_TELEMETRY_DB.get()
        if ctx_db is not None:
            key = str(ctx_db.resolve())
            with _CTX_DB_LOCK:
                c = _CTX_CONNS.get(key)
                if c is None:
                    ctx_db.parent.mkdir(parents=True, exist_ok=True)
                    c = _init_telemetry_conn(sqlite3.connect(key, timeout=30.0, check_same_thread=False), wal=True)
                    _CTX_CONNS[key] = c
                return c
        if "unittest" in sys.modules:
            if self._test_mem_conn is None:
                self._test_mem_conn = _init_telemetry_conn(
                    sqlite3.connect(":memory:", check_same_thread=False), wal=False
                )
            return self._test_mem_conn
        return self._ensure_disk_conn(settings.db_path)

    def start_trace(self, trace_type: str = "user_turn", name: str = "turn") -> str:
        """Start a new waterfall trace and set it as the active trace in the current `ContextVar`."""
        tid = f"tr_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        now = time.perf_counter()
        safe_name = redact_secrets(str(name).replace("\n", " "))[:120]
        self.active_trace_id = tid
        with self._lock:
            conn = self._active_conn()
            self._trace_starts[tid] = now
            conn.execute(
                "INSERT INTO telemetry_traces (trace_id, trace_type, name, status, started_epoch) VALUES (?, ?, ?, 'active', ?)",
                (tid, trace_type[:40], safe_name, time.time()),
            )
            conn.commit()
        return tid

    def end_trace(self, trace_id: Optional[str] = None, status: str = "ok") -> None:
        """Finalize a waterfall trace with total duration, span summary, and bounded retention housekeeping."""
        with self._lock:
            tid = trace_id or self.active_trace_id
            if not tid:
                return
            conn = self._active_conn()
            start_perf = self._trace_starts.pop(tid, None)
            dur_ms = round((time.perf_counter() - start_perf) * 1000.0, 2) if start_perf is not None else 0.0
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*), SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), COALESCE(SUM(duration_ms), 0.0) FROM telemetry_spans WHERE trace_id = ?",
                (tid,),
            )
            row = cur.fetchone() or (0, 0, 0.0)
            spans, errs, sum_ms = int(row[0] or 0), int(row[1] or 0), float(row[2] or 0.0)
            final_dur = dur_ms if dur_ms > 0 else round(sum_ms, 2)
            final_status = status if status != "ok" or errs == 0 else "warn"
            conn.execute(
                "UPDATE telemetry_traces SET status = ?, duration_ms = ?, span_count = ?, error_count = ? WHERE trace_id = ?",
                (final_status, final_dur, spans, errs, tid),
            )
            conn.commit()
            if self.active_trace_id == tid:
                self.active_trace_id = None
            self._ended_counter += 1
            if self._ended_counter >= 50:
                self._ended_counter = 0
                self.prune_old_telemetry()

    def prune_old_telemetry(self, max_traces: int = 500, max_logs: int = 2000) -> Dict[str, int]:
        """Retain the newest `max_traces` waterfall traces (plus their spans) and `max_logs` application logs."""
        t_cap, l_cap = max(1, int(max_traces)), max(1, int(max_logs))
        with self._lock:
            conn = self._active_conn()
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM telemetry_traces WHERE rowid NOT IN (SELECT rowid FROM telemetry_traces ORDER BY rowid DESC LIMIT ?)",
                (t_cap,),
            )
            pruned_traces = max(0, int(cur.rowcount or 0))
            cur.execute("DELETE FROM telemetry_spans WHERE trace_id NOT IN (SELECT trace_id FROM telemetry_traces)")
            pruned_spans = max(0, int(cur.rowcount or 0))
            cur.execute(
                "DELETE FROM telemetry_logs WHERE id NOT IN (SELECT id FROM telemetry_logs ORDER BY id DESC LIMIT ?)",
                (l_cap,),
            )
            pruned_logs = max(0, int(cur.rowcount or 0))
            conn.commit()
        return {"pruned_traces": pruned_traces, "pruned_spans": pruned_spans, "pruned_logs": pruned_logs}

    def record_span(
        self,
        span_type: str,
        name: str,
        duration_ms: float,
        status: str = "ok",
        detail: str = "",
        trace_id: Optional[str] = None,
    ) -> int:
        """Record a timed waterfall step (e.g. llm_call, tool_call, safety_guard) in the active trace."""
        with self._lock:
            tid = trace_id or self.active_trace_id
            if not tid:
                return 0
            conn = self._active_conn()
            start_perf = self._trace_starts.get(tid)
            offset_ms = 0.0
            if start_perf is not None:
                offset_ms = max(0.0, round((time.perf_counter() - start_perf) * 1000.0 - duration_ms, 2))
            safe_detail = redact_secrets(str(detail))[:400]
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO telemetry_spans (trace_id, span_type, name, status, offset_ms, duration_ms, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (tid, span_type[:40], name[:80], status[:16], offset_ms, round(float(duration_ms), 2), safe_detail),
            )
            conn.commit()
            return int(cur.lastrowid)

    def log(self, level: str, component: str, message: str, trace_id: Optional[str] = None) -> int:
        """Write a structured application log event (DEBUG, INFO, WARN, ERROR)."""
        lvl = (level or "INFO").strip().upper()
        if lvl not in ("DEBUG", "INFO", "WARN", "ERROR"):
            lvl = "INFO"
        safe_msg = redact_secrets(str(message))[:600]
        with self._lock:
            conn = self._active_conn()
            tid = trace_id if trace_id is not None else (self.active_trace_id or "")
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO telemetry_logs (level, component, trace_id, message) VALUES (?, ?, ?, ?)",
                (lvl, (component or "core").strip().lower()[:40], tid, safe_msg),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_kpis(self) -> Dict[str, Any]:
        """Compute live system KPIs across traces, LLM calls, tools, safety guards, and logs."""
        with self._lock:
            cur = self._active_conn().cursor()
            cur.execute("SELECT COUNT(*), COALESCE(AVG(duration_ms), 0.0), COALESCE(MAX(duration_ms), 0.0) FROM telemetry_traces WHERE status != 'active'")
            tr_count, tr_avg, tr_max = cur.fetchone() or (0, 0.0, 0.0)
            cur.execute("SELECT COUNT(*), COALESCE(AVG(duration_ms), 0.0), COALESCE(MAX(duration_ms), 0.0), SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) FROM telemetry_spans WHERE span_type = 'llm_call'")
            llm_cnt, llm_avg, llm_max, llm_err = cur.fetchone() or (0, 0.0, 0.0, 0)
            cur.execute("SELECT COUNT(*), COALESCE(AVG(duration_ms), 0.0), SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) FROM telemetry_spans WHERE span_type = 'tool_call'")
            tool_cnt, tool_avg, tool_err = cur.fetchone() or (0, 0.0, 0)
            cur.execute("SELECT level, COUNT(*) FROM telemetry_logs GROUP BY level")
            log_counts = {row[0]: int(row[1]) for row in cur.fetchall()}
            cur.execute("SELECT COUNT(*) FROM telemetry_logs WHERE component = 'safety'")
            safety_saves = int((cur.fetchone() or (0,))[0])

        tool_total, tool_errors = int(tool_cnt or 0), int(tool_err or 0)
        tool_ok_pct = round(((tool_total - tool_errors) / tool_total) * 100.0, 1) if tool_total > 0 else 100.0
        return {
            "total_traces": int(tr_count or 0),
            "avg_trace_ms": round(float(tr_avg or 0.0), 1),
            "max_trace_ms": round(float(tr_max or 0.0), 1),
            "llm_calls": int(llm_cnt or 0),
            "avg_llm_ms": round(float(llm_avg or 0.0), 1),
            "max_llm_ms": round(float(llm_max or 0.0), 1),
            "llm_errors": int(llm_err or 0),
            "tool_calls": tool_total,
            "avg_tool_ms": round(float(tool_avg or 0.0), 1),
            "tool_errors": tool_errors,
            "tool_success_pct": tool_ok_pct,
            "safety_saves": safety_saves,
            "logs_by_level": log_counts,
        }

    def format_kpi_dashboard(self) -> str:
        """Render a human-readable KPI & Observability Scorecard."""
        k = self.get_kpis()
        logs = k["logs_by_level"]
        return (
            "=== Companion Telemetry & KPI Scorecard ===\n"
            f"Traces Recorded   : {k['total_traces']} (Avg Turn: {k['avg_trace_ms']} ms | Peak: {k['max_trace_ms']} ms)\n"
            f"LLM Calls         : {k['llm_calls']} (Avg Latency: {k['avg_llm_ms']} ms | Peak: {k['max_llm_ms']} ms | Errors: {k['llm_errors']})\n"
            f"Tool Executions   : {k['tool_calls']} (Success Rate: {k['tool_success_pct']}% | Avg: {k['avg_tool_ms']} ms | Errors: {k['tool_errors']})\n"
            f"Safety Guard Saves: {k['safety_saves']} (Syntax Guard / Loop Guard / Rollback interventions)\n"
            f"Application Logs  : INFO={logs.get('INFO', 0)} | WARN={logs.get('WARN', 0)} | ERROR={logs.get('ERROR', 0)} | DEBUG={logs.get('DEBUG', 0)}\n"
            "(Use `/traces` for the step-by-step Waterfall view or `/logs` for application logs.)"
        )

    def format_recent_traces(self, limit: int = 10) -> str:
        """List recent traces with their waterfall span counts and durations."""
        with self._lock:
            cur = self._active_conn().cursor()
            cur.execute(
                "SELECT trace_id, trace_type, name, status, duration_ms, span_count, error_count, created_at FROM telemetry_traces ORDER BY rowid DESC LIMIT ?",
                (max(1, int(limit)),),
            )
            rows = cur.fetchall()
        if not rows:
            return "=== Recent Waterfall Traces ===\n(No traces recorded yet.)"
        lines = ["=== Recent Waterfall Traces (Newest First) ==="]
        for tid, ttype, name, st, dur, spans, errs, ts in rows:
            lines.append(f"  * `{tid}` [{ts} | {ttype} | {st.upper()} | {dur:.1f} ms | {spans} spans, {errs} err] {name}")
        return "\n".join(lines)

    def format_waterfall(self, trace_id: Optional[str] = None) -> str:
        """Render a step-by-step Waterfall timeline for a trace (or the latest trace if None)."""
        with self._lock:
            cur = self._active_conn().cursor()
            tid = (trace_id or "").strip()
            if not tid:
                cur.execute("SELECT trace_id FROM telemetry_traces ORDER BY rowid DESC LIMIT 1")
                row = cur.fetchone()
                if not row:
                    return "=== Waterfall Trace ===\n(No traces recorded yet.)"
                tid = str(row[0])
            cur.execute("SELECT trace_type, name, status, duration_ms, span_count, error_count, created_at FROM telemetry_traces WHERE trace_id = ?", (tid,))
            tr = cur.fetchone()
            cur.execute("SELECT id, span_type, name, status, offset_ms, duration_ms, detail FROM telemetry_spans WHERE trace_id = ? ORDER BY id ASC", (tid,))
            spans = cur.fetchall()

        header = (
            f"=== Waterfall Trace `{tid}` ({tr[0]} | {tr[2].upper()} | {tr[3]:.1f} ms | {tr[6]}) ===\nGoal/Turn: {tr[1]}"
            if tr
            else f"=== Waterfall Trace `{tid}` ==="
        )
        lines = [header, "Timeline:"]
        if not spans:
            lines.append("  (No spans recorded for this trace)")
        for idx, (_, stype, sname, status, offset, dur, detail) in enumerate(spans, 1):
            badge = "PASS" if status == "ok" else status.upper()
            det = f" -> {detail}" if detail else ""
            lines.append(f"  {idx:2d}. [+{offset:7.1f}ms | {dur:7.1f}ms | {badge:5s}] {stype}:{sname}{det}")
        return "\n".join(lines)

    def format_recent_logs(self, level: Optional[str] = None, component: Optional[str] = None, limit: int = 25) -> str:
        """Render structured application logs filtered by severity level or component."""
        clauses, params = [], []
        if level and level.strip():
            clauses.append("level = ?")
            params.append(level.strip().upper())
        if component and component.strip():
            clauses.append("component = ?")
            params.append(component.strip().lower())
        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            cur = self._active_conn().cursor()
            cur.execute(
                f"SELECT id, level, component, trace_id, message, created_at FROM telemetry_logs{where_sql} ORDER BY id DESC LIMIT ?",
                (*params, max(1, int(limit))),
            )
            rows = cur.fetchall()
        if not rows:
            return "=== Application Telemetry Logs ===\n(No matching log entries.)"
        lines = ["=== Application Telemetry Logs (Newest First) ==="]
        for lid, lvl, comp, tid, msg, ts in rows:
            tr_tag = f" ({tid})" if tid else ""
            lines.append(f"  [#{lid} {ts} | {lvl:5s} | {comp}{tr_tag}] {msg}")
        return "\n".join(lines)

    def handle_tool(self, args: Dict[str, Any]) -> str:
        """Dispatch `telemetry_manage` tool actions for the agent or UI bridge."""
        def _as_int(val: Any, default: int) -> int:
            try:
                return int(val) if val is not None and str(val).strip() else default
            except (TypeError, ValueError):
                return default

        action = str(args.get("action") or "kpis").strip().lower()
        if action in ("kpis", "metrics", "status", "scorecard"):
            return self.format_kpi_dashboard()
        if action in ("waterfall", "trace"):
            return self.format_waterfall(trace_id=str(args.get("trace_id") or ""))
        if action in ("recent_traces", "traces"):
            return self.format_recent_traces(limit=_as_int(args.get("limit"), 10))
        if action in ("logs", "app_logs"):
            return self.format_recent_logs(level=str(args.get("level") or ""), component=str(args.get("component") or ""), limit=_as_int(args.get("limit"), 25))
        if action == "prune":
            res = self.prune_old_telemetry(max_traces=_as_int(args.get("max_traces"), 500), max_logs=_as_int(args.get("max_logs"), 2000))
            return f"Pruned telemetry retention: {res}"
        if action == "log_event":
            msg = str(args.get("message") or "").strip()
            if not msg:
                return "error: message is required for log_event"
            lid = self.log(level=str(args.get("level") or "INFO"), component=str(args.get("component") or "ui_bridge"), message=msg)
            return f"Logged telemetry event #{lid}"
        return f"error: unknown telemetry_manage action '{action}'"

    def close(self) -> None:
        with self._lock:
            for c in (self.conn, self._test_mem_conn):
                if c is not None:
                    try:
                        c.close()
                    except Exception:
                        pass
            self.conn = None
            self._conn_path = None
            self._test_mem_conn = None


telemetry = TelemetryEngine()
