"""
Hot/Cold Forever Chat Archive, Unified Approvals Queue, Trust Modes, Session Nudges & Saved Prompts
(`companion/archive_and_approvals.py` — A-Frame Domain Layer).

Provides:
1. 3-Tier Trust / Autonomy Mode (`strict`, `balanced`, `autonomous`) where hard safety blocks always win
   and Companion can never self-escalate trust.
2. Fingerprint-verified Unified Approvals Queue (`approval_requests`) shared across CLI, Web, Jobs,
   Wiki Dual-Author Guard, Vault allow-list grants, MCP server onboarding, and Routines.
3. Hot (`<=90d`) / Cold (`>90d`) Forever Chat Archive (`conversations_cold` + `FTS5` + Hybrid RRF).
4. Session-scoped quiet turn-boundary nudges (`session_nudges`) and Saved Prompts Library (`saved_prompts`).
"""

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, FrozenSet, Iterator, List, Optional, Tuple

from companion.config import is_in_agent_tool_turn
from companion.memory_store import MemoryStore, get_memory_store, hybrid_rrf_rank, parse_time_window
from companion.telemetry import redact_secrets

__all__ = [
    "ActionRiskTier", "TRUST_MODES", "ApprovalQueue", "archive_aged_conversations", "classify_action_risk",
    "compute_approval_fingerprint", "consume_session_nudges", "current_session_id", "delete_prompt_template",
    "dismiss_session_nudges", "enqueue_session_nudge", "format_forever_search_results", "get_approval_queue",
    "get_prompt_template", "get_trust_mode", "list_prompt_templates", "peek_session_nudges",
    "render_prompt_template", "save_prompt_template", "search_forever_history", "session_id_context", "set_trust_mode",
]


class ActionRiskTier(str, Enum):
    """Domain-neutral action risk classification (`R0`..`R3`) enforced at tool & workflow execution boundaries."""

    R0_READ_ONLY = "R0"
    R1_LOCAL_REVERSIBLE = "R1"
    R2_CAPABILITY_PROMOTION = "R2"
    R3_HIGH_IMPACT_MUTATION = "R3"


_R3_SHELL_RE = re.compile(
    r"(?:\b(?:ssh|scp|sftp|rsync)\s+\S+|\bgit\s+push\b|\b(?:npm|twine|cargo)\s+publish\b|"
    r"\b(?:curl|wget)\b[^\n]*(?:\b(?:-X|--request)\s*(?:POST|PUT|DELETE|PATCH)\b|(?:\s|^)(?:-d|--data|--data-raw|--data-binary|--json|-F|--form)\b)|"
    r"\bInvoke-(?:RestMethod|WebRequest)\b[^\n]*(?:-Method\s+(?:Post|Put|Delete|Patch)\b|-Body\b)|"
    r"\b(?:kill|pkill|killall|taskkill|Stop-Process)\b|"
    r"\bsystemctl\b(?!\s+(?:--[a-z0-9=-]+\s+|-[a-zA-Z]+\s+)*(?:status|list-(?:units|unit-files|timers|sockets|dependencies)|is-(?:active|enabled|failed|system-running)|show|cat|help)\b)|"
    r"\b(?:schtasks|reg\s+(?:add|delete)|netsh|sc\s+(?:create|delete|config))\b)",
    re.I,
)
_R3_PYTHON_RE = re.compile(
    r"(?:\bsmtplib\.SMTP\b|\bftplib\.FTP\b|"
    r"\bos\.(?:system|popen|kill|killpg)\b|"
    r"\bshutil\.rmtree\s*\(\s*['\"](?:/|[A-Za-z]:\\|\.\.))",
)
_R0_ACTIONS: Dict[str, FrozenSet[str]] = {
    "checkpoint_manage": frozenset({"", "list", "git_log"}),
    "memory_manage": frozenset({"get_notes", "get_adrs", "get_profile", "search", "search_history", "recent_history", "query_facts", "read_fact"}),
    "task_manage": frozenset({"list_tasks", "list_backlog", "list_all"}),
    "project_manage": frozenset({"", "status", "current", "list", "browse", "ls", "tree", "get_root", "browse_projects"}),
    "job_manage": frozenset({"", "list", "status"}),
    "wiki_manage": frozenset({"", "list", "tree", "read", "search", "graph", "diff", "list_stale", "list_trash", "list_templates", "read_template", "tag_authority", "inspect_graduation", "validate_curation", "curation_history"}),
    "vault_manage": frozenset({"", "list", "check_secret", "list_missing", "audit"}),
    "endpoint_manage": frozenset({"", "list", "test", "inspect_host", "ssh_read_file"}),
    "mcp_manage": frozenset({"", "list", "discover", "list_partners", "handshake_partner", "check_partner_job"}),
    "routine_manage": frozenset({"", "list", "autonomy_status", "morning", "list_approvals", "prompt_list"}),
    "agile_manage": frozenset({"", "board_view", "list_items", "roadmap", "list_boards", "list_epics", "list_sprints", "capacity", "probe_repo", "browse_projects", "browse_projects_dir", "browse"}),
    "git_manage": frozenset({"", "status", "diff", "log", "worktree_list"}),
    "dev_preview": frozenset({"", "status", "list", "logs", "http_probe"}),
}


def classify_action_risk(tool_name: str, args: Optional[Dict[str, Any]] = None) -> ActionRiskTier:
    """Classify `(tool_name, args)` into `R0`..`R3` without domain-specific hardcoding."""
    payload, action = args or {}, str((args or {}).get("action") or (args or {}).get("subaction") or "").strip().lower()
    if tool_name in ("read_file", "list_dir", "search", "web_fetch", "web_search", "system_doctor"):
        return ActionRiskTier.R0_READ_ONLY
    if tool_name == "telemetry_manage":
        return ActionRiskTier.R0_READ_ONLY if action != "log_event" else ActionRiskTier.R1_LOCAL_REVERSIBLE
    if tool_name == "workflow_manage":
        return ActionRiskTier.R0_READ_ONLY if action in ("", "status", "list_dags", "list_recipes", "http_smoke", "check_skill") else (ActionRiskTier.R2_CAPABILITY_PROMOTION if action == "bootstrap_skill" else ActionRiskTier.R1_LOCAL_REVERSIBLE)
    if tool_name == "endpoint_manage":
        if action in ("ssh_exec", "winrm_exec", "propose", "propose_endpoint", "register", "create", "add", "set", "update", "upsert", "remove"):
            return ActionRiskTier.R3_HIGH_IMPACT_MUTATION
        if action == "api_request" and str(payload.get("method") or "GET").strip().upper() not in ("GET", "HEAD"):
            return ActionRiskTier.R3_HIGH_IMPACT_MUTATION
    if tool_name == "mcp_manage" and action in ("call_tool", "delegate_partner", "propose_server", "register_server", "register", "remove", "register_partner", "remove_partner"):
        return ActionRiskTier.R3_HIGH_IMPACT_MUTATION
    if tool_name == "mcp_manage" and action == "harvest_skill":
        return ActionRiskTier.R2_CAPABILITY_PROMOTION
    if tool_name in ("run_shell", "run_command") and _R3_SHELL_RE.search(str(payload.get("command") or "")):
        return ActionRiskTier.R3_HIGH_IMPACT_MUTATION
    if tool_name == "run_python":
        py_code = str(payload.get("code") or "")
        if _R3_SHELL_RE.search(py_code) or _R3_PYTHON_RE.search(py_code):
            return ActionRiskTier.R3_HIGH_IMPACT_MUTATION
    if tool_name in ("write_file", "edit_file") and (str(payload.get("path") or "").replace("\\", "/").startswith("skills/") or "/skills/" in str(payload.get("path") or "").replace("\\", "/")):
        return ActionRiskTier.R2_CAPABILITY_PROMOTION
    if tool_name == "project_manage" and not action and any(str(payload.get(k) or "").strip() for k in ("target", "path", "name")):
        return ActionRiskTier.R1_LOCAL_REVERSIBLE
    if tool_name in _R0_ACTIONS and action in _R0_ACTIONS[tool_name]:
        return ActionRiskTier.R0_READ_ONLY
    return ActionRiskTier.R1_LOCAL_REVERSIBLE

_ACTIVE_SESSION_ID: ContextVar[str] = ContextVar("companion_active_session_id", default="main")


def current_session_id() -> str:
    return _ACTIVE_SESSION_ID.get() or "main"


@contextmanager
def session_id_context(session_id: Optional[str]) -> Iterator[str]:
    eff = (session_id or "main").strip() or "main"
    tok = _ACTIVE_SESSION_ID.set(eff)
    try:
        yield eff
    finally:
        _ACTIVE_SESSION_ID.reset(tok)

TRUST_MODES: Dict[str, str] = {
    "strict": "Pause for human approval on any external, remote, capability, or user-note mutation.",
    "balanced": "Allow safe local reversible work & inbox captures; require approval for user-note edits, remote/MCP mutations, and scope grants.",
    "autonomous": "Auto-approve routine reversible & capability steps while enforcing hard safety blocks, Vault zero-reveal, and ADR-007 locks.",
}
_TRUST_RANK: Dict[str, int] = {"strict": 0, "balanced": 1, "autonomous": 2}

ARCHIVE_APPROVALS_DDL: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS conversations_cold (
        id INTEGER PRIMARY KEY,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp DATETIME NOT NULL,
        tool_call_id TEXT,
        session_tag TEXT,
        archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE VIRTUAL TABLE IF NOT EXISTS conversations_cold_fts USING fts5(content)",
    "CREATE INDEX IF NOT EXISTS idx_conv_cold_ts ON conversations_cold(timestamp)",
    """CREATE TABLE IF NOT EXISTS approval_requests (
        approval_id TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        action_name TEXT NOT NULL,
        target TEXT NOT NULL,
        summary TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        payload_json TEXT DEFAULT '{}',
        requester TEXT DEFAULT 'companion',
        session_id TEXT DEFAULT 'main',
        status TEXT DEFAULT 'waiting',
        decided_by TEXT DEFAULT '',
        decision_note TEXT DEFAULT '',
        execution_output TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        decided_at DATETIME
    )""",
    "CREATE INDEX IF NOT EXISTS idx_approvals_status_fp ON approval_requests(status, fingerprint)",
    """CREATE TABLE IF NOT EXISTS session_nudges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL DEFAULT 'main',
        category TEXT NOT NULL DEFAULT 'job',
        ref_id TEXT DEFAULT '',
        message TEXT NOT NULL,
        seen INTEGER DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_nudges_sess_seen ON session_nudges(session_id, seen)",
    """CREATE TABLE IF NOT EXISTS saved_prompts (
        name TEXT PRIMARY KEY,
        description TEXT DEFAULT '',
        category TEXT DEFAULT 'general',
        template TEXT NOT NULL,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
)


def _ensure_tables(mem: MemoryStore) -> None:
    def _init(m: MemoryStore) -> None:
        cur = m.conn.cursor()
        for ddl in ARCHIVE_APPROVALS_DDL:
            cur.execute(ddl)
        cols = {str(r[1]) for r in cur.execute("PRAGMA table_info(approval_requests)").fetchall()}
        if "execution_output" not in cols:
            cur.execute("ALTER TABLE approval_requests ADD COLUMN execution_output TEXT DEFAULT ''")
        m.conn.commit()

    mem.ensure_schema("archive_approvals_v2", _init)


def get_trust_mode(store: Optional[MemoryStore] = None) -> str:
    """Return the active global trust mode (`strict`, `balanced`, or `autonomous`)."""
    mem = store or get_memory_store()
    raw = mem.get_user_profile().get("trust_mode", "balanced").strip().lower()
    return raw if raw in TRUST_MODES else "balanced"


def set_trust_mode(mode: str, actor: str = "user", store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """Update the global trust mode; mechanically block Companion from raising its own autonomy."""
    mem = store or get_memory_store()
    clean = (mode or "").strip().lower()
    if clean not in TRUST_MODES:
        return {"ok": False, "error": f"Invalid trust mode '{mode}'. Choose from: {', '.join(TRUST_MODES)}."}
    curr = get_trust_mode(store=mem)
    is_human = actor.strip().lower() in ("user", "cli", "web") and not is_in_agent_tool_turn()
    if not is_human and _TRUST_RANK[clean] > _TRUST_RANK[curr]:
        return {
            "ok": False,
            "error": f"TRUST GUARD REJECTED: `{actor}` cannot escalate trust mode from `{curr}` to `{clean}`. Only the user can raise autonomy.",
        }
    mem.set_user_profile("trust_mode", clean)
    mem.sync_memory_md()
    return {"ok": True, "trust_mode": clean, "description": TRUST_MODES[clean], "summary": f"Trust mode set to `{clean}` — {TRUST_MODES[clean]}"}


def compute_approval_fingerprint(category: str, action_name: str, target: str, payload: Optional[Dict[str, Any]] = None) -> str:
    """Compute a deterministic SHA-256 request fingerprint so approvals bind to the exact target & parameters."""
    safe_payload = json.loads(redact_secrets(json.dumps(payload or {}, sort_keys=True, default=str)))
    canonical = json.dumps(
        {"category": category.strip().lower(), "action": action_name.strip().lower(), "target": target.strip(), "payload": safe_payload},
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


class ApprovalQueue:
    """Unified human-in-the-loop approval registry shared across CLI, Web, Jobs, Wiki, Vault, MCP, and Routines."""

    def __init__(self, store: Optional[MemoryStore] = None):
        self._store = store

    @property
    def store(self) -> MemoryStore:
        mem = self._store or get_memory_store()
        _ensure_tables(mem)
        return mem

    def is_fingerprint_approved(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        cols = [
            "approval_id", "category", "action_name", "target", "summary", "fingerprint",
            "payload_json", "requester", "session_id", "status", "decided_by", "decision_note", "created_at", "decided_at",
        ]
        rows = self.store._fetch_dicts(
            cols,
            f"SELECT {', '.join(cols)} FROM approval_requests WHERE status = 'approved' AND fingerprint = ? "
            "AND (decided_at IS NULL OR decided_at >= datetime('now', '-15 minutes')) ORDER BY rowid DESC LIMIT 1",
            (fingerprint,),
        )
        return rows[0] if rows else None

    def mark_executed(self, approval_id: str, output: str = "") -> None:
        clean_id = (approval_id or "").strip()
        if not clean_id:
            return
        safe_out = redact_secrets(output or "")[:16000]
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE approval_requests SET status = 'executed', execution_output = ? WHERE approval_id = ? AND status = 'approved'",
                (safe_out, clean_id),
            )
            self.store.conn.commit()

    def consume_recent_executed(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        """Consume cached one-shot execution output when Companion immediately follows up after human Web/CLI approval."""
        cols = [
            "approval_id", "category", "action_name", "target", "summary", "fingerprint",
            "payload_json", "requester", "session_id", "status", "decided_by", "decision_note",
            "execution_output", "created_at", "decided_at",
        ]
        with self.store._lock:
            row = self.store.conn.execute(
                f"SELECT {', '.join(cols)} FROM approval_requests WHERE status = 'executed' AND fingerprint = ? "
                "AND execution_output != '' AND (decided_at IS NULL OR decided_at >= datetime('now', '-10 minutes')) "
                "ORDER BY rowid DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
            if not row:
                return None
            rec = dict(zip(cols, row))
            self.store.conn.execute("UPDATE approval_requests SET execution_output = '' WHERE approval_id = ?", (rec["approval_id"],))
            self.store.conn.commit()
            return rec

    def check_or_enqueue(
        self,
        category: str,
        action_name: str,
        target: str,
        summary: str,
        payload: Optional[Dict[str, Any]] = None,
        requester: str = "companion",
        session_id: str = "main",
        confirmed: bool = False,
        always_require_in_autonomous: bool = False,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Return `(True, record)` if authorized, else enqueue a `waiting` approval request and return `(False, record)`."""
        mem = self.store
        fp = compute_approval_fingerprint(category, action_name, target, payload)
        if confirmed:
            return True, {"status": "approved", "fingerprint": fp, "decided_by": "confirmed_flag"}
        existing_ok = self.is_fingerprint_approved(fp)
        if existing_ok is not None:
            self.mark_executed(str(existing_ok.get("approval_id") or ""))
            return True, existing_ok
        trust = get_trust_mode(store=mem)
        if trust == "autonomous" and not always_require_in_autonomous:
            return True, {"status": "approved", "fingerprint": fp, "decided_by": "autonomous_trust_mode"}

        cat_clean, act_clean, tgt_clean = category.strip().lower(), action_name.strip().lower(), target.strip()
        safe_json = redact_secrets(json.dumps(payload or {}, sort_keys=True, default=str))
        safe_sum = redact_secrets(summary.strip())[:300]
        with mem._lock:
            cols = [
                "approval_id", "category", "action_name", "target", "summary", "fingerprint",
                "payload_json", "requester", "session_id", "status", "decided_by", "decision_note", "created_at", "decided_at",
            ]
            fp_row = mem.conn.execute(
                f"SELECT {', '.join(cols)} FROM approval_requests WHERE status = 'waiting' AND fingerprint = ? ORDER BY rowid DESC LIMIT 1",
                (fp,),
            ).fetchone()
            if fp_row:
                return False, dict(zip(cols, fp_row))
            dup_row = mem.conn.execute(
                "SELECT approval_id FROM approval_requests WHERE status = 'waiting' AND category = ? AND action_name = ? AND target = ? ORDER BY rowid DESC LIMIT 1",
                (cat_clean, act_clean, tgt_clean),
            ).fetchone()
            if dup_row:
                aid = str(dup_row[0])
                mem.conn.execute(
                    "UPDATE approval_requests SET summary = ?, fingerprint = ?, payload_json = ?, requester = ?, session_id = ? WHERE approval_id = ?",
                    (safe_sum, fp, safe_json, requester, session_id or "main", aid),
                )
            else:
                aid = f"apr_{int(time.time() * 1000)}_{os.urandom(3).hex()}"
                mem.conn.execute(
                    """INSERT INTO approval_requests
                       (approval_id, category, action_name, target, summary, fingerprint, payload_json, requester, session_id, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting')""",
                    (aid, cat_clean, act_clean, tgt_clean, safe_sum, fp, safe_json, requester, session_id or "main"),
                )
            mem.conn.commit()
        created = self.get_request(aid) or {"approval_id": aid, "status": "waiting", "fingerprint": fp, "summary": safe_sum}
        return False, created

    def get_request(self, approval_id: str) -> Optional[Dict[str, Any]]:
        cols = [
            "approval_id", "category", "action_name", "target", "summary", "fingerprint",
            "payload_json", "requester", "session_id", "status", "decided_by", "decision_note", "created_at", "decided_at",
        ]
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM approval_requests WHERE approval_id = ?", (approval_id.strip(),))
        return rows[0] if rows else None

    def list_requests(self, status: Optional[str] = "waiting", limit: int = 30) -> List[Dict[str, Any]]:
        cols = [
            "approval_id", "category", "action_name", "target", "summary", "fingerprint",
            "payload_json", "requester", "session_id", "status", "decided_by", "decision_note", "created_at", "decided_at",
        ]
        if status and status.strip().lower() != "all":
            eff_st = "waiting" if status.strip().lower() == "pending" else status.strip().lower()
            sql = f"SELECT {', '.join(cols)} FROM approval_requests WHERE status = ? ORDER BY rowid DESC LIMIT ?"
            return self.store._fetch_dicts(cols, sql, (eff_st, max(1, int(limit))))
        sql = f"SELECT {', '.join(cols)} FROM approval_requests ORDER BY rowid DESC LIMIT ?"
        return self.store._fetch_dicts(cols, sql, (max(1, int(limit)),))

    def resolve(self, approval_id: str, decision: str, actor: str = "user", note: str = "") -> Dict[str, Any]:
        """Approve or reject a pending request; mechanically forbid Companion from approving its own requests."""
        if actor.strip().lower() not in ("user", "cli", "web") or is_in_agent_tool_turn():
            return {"ok": False, "error": "APPROVAL GUARD REJECTED: Companion cannot approve or deny pending human approval requests."}
        dec = decision.strip().lower()
        if dec in ("approve", "approved", "yes", "allow"):
            final_status = "approved"
        elif dec in ("reject", "rejected", "deny", "denied", "no"):
            final_status = "rejected"
        else:
            return {"ok": False, "error": f"Invalid decision '{decision}'. Use `approve` or `deny`."}
        req = self.get_request(approval_id)
        if not req:
            return {"ok": False, "error": f"Approval request `{approval_id}` not found."}
        with self.store._lock:
            self.store.conn.execute(
                """UPDATE approval_requests
                   SET status = ?, decided_by = ?, decision_note = ?, decided_at = CURRENT_TIMESTAMP
                   WHERE approval_id = ?""",
                (final_status, actor.strip().lower(), redact_secrets(note)[:240], req["approval_id"]),
            )
            self.store.conn.execute(
                "UPDATE session_nudges SET seen = 1 WHERE seen = 0 AND (ref_id = ? OR message LIKE ?)",
                (req["approval_id"], f"%{req['approval_id']}%"),
            )
            self.store.conn.commit()
        updated = self.get_request(req["approval_id"]) or req
        return {
            "ok": True,
            "approval": updated,
            "summary": f"Marked `{req['approval_id']}` as `{final_status.upper()}` ({req['category']}:{req['action_name']} on `{req['target']}`).",
        }

    def format_board(self, status: str = "waiting") -> str:
        items = self.list_requests(status=status, limit=25)
        trust = get_trust_mode(store=self.store)
        lines = [f"=== Unified Approvals Queue (Trust Mode: `{trust}` | Filter: `{status}`) ==="]
        if not items:
            lines.append("  (No matching approval requests.)")
            return "\n".join(lines)
        for r in items:
            lines.append(
                f"  * `{r['approval_id']}` [{r['status'].upper()} | {r['category']}:{r['action_name']}] target=`{r['target']}` "
                f"(by {r['requester']} @ {r['created_at']})\n    {r['summary']}"
            )
        lines.append("\n(Resolve via `/approvals approve <id>` or `/approvals deny <id>`.)")
        return "\n".join(lines)


def get_approval_queue(store: Optional[MemoryStore] = None) -> ApprovalQueue:
    return ApprovalQueue(store=store)


def enqueue_session_nudge(
    message: str, category: str = "job", session_id: str = "main", ref_id: str = "", store: Optional[MemoryStore] = None,
) -> int:
    """Queue a turn-boundary notification scoped strictly to `session_id` (preserving full multi-line output for `approval_executed`)."""
    mem = store or get_memory_store()
    _ensure_tables(mem)
    cat_clean = (category or "job").strip()[:32]
    if cat_clean == "approval_executed":
        clean_msg = redact_secrets((message or "").strip())[:8000]
    else:
        clean_msg = redact_secrets((message or "").strip().replace("\n", " "))[:280]
    if not clean_msg:
        return 0
    with mem._lock:
        cur = mem.conn.execute(
            "INSERT INTO session_nudges (session_id, category, ref_id, message, seen) VALUES (?, ?, ?, ?, 0)",
            ((session_id or "main").strip(), cat_clean, (ref_id or "").strip()[:64], clean_msg),
        )
        mem.conn.commit()
        return int(cur.lastrowid or 0)


def peek_session_nudges(session_id: str = "main", store: Optional[MemoryStore] = None, limit: int = 10) -> List[Dict[str, Any]]:
    """Inspect unseen nudges for `session_id` (auto-clearing resolved approval nudges) without consuming active ones."""
    mem = store or get_memory_store()
    _ensure_tables(mem)
    stale_where = (
        "seen = 0 AND category = 'approval' AND ref_id != '' "
        "AND ref_id NOT IN (SELECT approval_id FROM approval_requests WHERE status = 'waiting')"
    )
    with mem._lock:
        if mem.conn.execute(f"SELECT 1 FROM session_nudges WHERE {stale_where} LIMIT 1").fetchone():
            mem.conn.execute(f"UPDATE session_nudges SET seen = 1 WHERE {stale_where}")
            mem.conn.commit()
    cols = ["id", "session_id", "category", "ref_id", "message", "created_at"]
    rows = mem._fetch_dicts(
        cols,
        f"SELECT {', '.join(cols)} FROM session_nudges WHERE session_id = ? AND seen = 0 ORDER BY id ASC LIMIT ?",
        ((session_id or "main").strip(), max(1, int(limit))),
    )
    for r in rows:
        r["message"] = str(r.get("message") or "").replace("\n", " ")[:220]
    return rows


def dismiss_session_nudges(
    nudge_ids: Optional[List[int]] = None, category: str = "", session_ids: Tuple[str, ...] = ("web_ui", "main"), store: Optional[MemoryStore] = None,
) -> Dict[str, Any]:
    """Mark matching unseen `session_nudges` as seen (from Notification Bell, Departures Board, or explicit dismiss)."""
    mem = store or get_memory_store()
    _ensure_tables(mem)
    with mem._lock:
        if nudge_ids:
            ids = [int(x) for x in nudge_ids]
            cur = mem.conn.execute(f"UPDATE session_nudges SET seen = 1 WHERE id IN ({','.join('?' for _ in ids)})", tuple(ids))
        elif category.strip():
            sids = tuple(s for s in session_ids if s) or ("web_ui", "main")
            cur = mem.conn.execute(f"UPDATE session_nudges SET seen = 1 WHERE seen = 0 AND category = ? AND session_id IN ({','.join('?' for _ in sids)})", (category.strip(), *sids))
        else:
            sids = tuple(s for s in session_ids if s) or ("web_ui", "main")
            cur = mem.conn.execute(f"UPDATE session_nudges SET seen = 1 WHERE seen = 0 AND session_id IN ({','.join('?' for _ in sids)})", sids)
        mem.conn.commit()
        cleared = int(cur.rowcount or 0)
    return {"ok": True, "cleared": cleared, "summary": f"Cleared {cleared} notification(s)."}


def consume_session_nudges(session_id: str = "main", store: Optional[MemoryStore] = None, limit: int = 8) -> str:
    """Atomically consume unseen nudges for `session_id` and format a quiet turn-boundary HUD banner."""
    mem = store or get_memory_store()
    _ensure_tables(mem)
    sess = (session_id or "main").strip()
    sids = ("web_ui", "main") if sess == "web_ui" else (sess,)
    cols = ["id", "category", "ref_id", "message", "created_at"]
    with mem._lock:
        rows = [
            dict(zip(cols, r))
            for r in mem.conn.execute(
                f"SELECT {', '.join(cols)} FROM session_nudges WHERE session_id IN ({','.join('?' for _ in sids)}) AND seen = 0 ORDER BY id ASC LIMIT ?",
                (*sids, max(1, int(limit))),
            ).fetchall()
        ]
        if not rows:
            return ""
        ids = [int(r["id"]) for r in rows]
        mem.conn.execute(f"UPDATE session_nudges SET seen = 1 WHERE id IN ({','.join('?' for _ in ids)})", tuple(ids))
        mem.conn.commit()
    apr_exec_rows = [r for r in rows if r["category"] == "approval_executed"]
    other_rows = [r for r in rows if r["category"] != "approval_executed"]
    blocks: List[str] = []
    if apr_exec_rows:
        apr_lines = [
            "[APPROVED ACTION EXECUTION RESULTS — Complete output from user-approved request(s); analyze directly and DO NOT re-run the same command]"
        ]
        for r in apr_exec_rows:
            ref = f" (`{r['ref_id']}`)" if r["ref_id"] else ""
            apr_lines.append(f"--- Approved Result{ref} ---\n{r['message']}\n--- End Approved Result ---")
        blocks.append("\n".join(apr_lines))
    if other_rows:
        lines = ["[COMPLETED BACKGROUND UPDATES (mention briefly if helpful, do not derail the user's request)]"]
        for r in other_rows:
            ref = f" (`{r['ref_id']}`)" if r["ref_id"] else ""
            lines.append(f"  - [{r['category']}{ref}] {r['message']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def archive_aged_conversations(
    store: Optional[MemoryStore] = None, older_than_days: int = 90, keep_min_hot: int = 24, now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Move raw chat turns older than `older_than_days` into `conversations_cold` while keeping recent hot continuity."""
    mem = store or get_memory_store()
    _ensure_tables(mem)
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=max(1, int(older_than_days)))).strftime("%Y-%m-%d %H:%M:%S")
    with mem._lock:
        cur = mem.conn.cursor()
        protected_ids = {
            int(r[0])
            for r in cur.execute("SELECT id FROM conversations ORDER BY id DESC LIMIT ?", (max(0, int(keep_min_hot)),)).fetchall()
        }
        candidates = cur.execute(
            "SELECT id, role, content, timestamp, tool_call_id, session_tag FROM conversations WHERE timestamp < ? ORDER BY id ASC",
            (cutoff,),
        ).fetchall()
        to_move = [row for row in candidates if int(row[0]) not in protected_ids]
        for cid, role, content, ts, tc_id, stag in to_move:
            cur.execute(
                "INSERT OR REPLACE INTO conversations_cold (id, role, content, timestamp, tool_call_id, session_tag) VALUES (?, ?, ?, ?, ?, ?)",
                (cid, role, content, ts, tc_id, stag),
            )
            cur.execute("INSERT OR REPLACE INTO conversations_cold_fts(rowid, content) VALUES (?, ?)", (cid, content))
            cur.execute("DELETE FROM conversations WHERE id = ?", (cid,))
            cur.execute("DELETE FROM conversations_fts WHERE rowid = ?", (cid,))
        hot_remaining = int((cur.execute("SELECT COUNT(*) FROM conversations").fetchone() or (0,))[0])
        cold_total = int((cur.execute("SELECT COUNT(*) FROM conversations_cold").fetchone() or (0,))[0])
        mem.conn.commit()
    return {
        "ok": True,
        "archived_count": len(to_move),
        "cutoff_utc": cutoff,
        "hot_count": hot_remaining,
        "cold_count": cold_total,
        "summary": f"Archived {len(to_move)} turn(s) older than {older_than_days}d (Hot={hot_remaining}, Cold Archive={cold_total}).",
    }


def _search_cold_table(mem: MemoryStore, query: str, limit: int, bounds: Optional[Tuple[str, str]]) -> List[Tuple[int, str, str, str]]:
    clean_q = (query or "").strip()
    if not clean_q:
        w_sql = "WHERE timestamp BETWEEN ? AND ? " if bounds else ""
        params: Tuple[Any, ...] = (*bounds, limit) if bounds else (limit,)
        return mem._fetch_rows(f"SELECT id, role, content, timestamp FROM conversations_cold {w_sql}ORDER BY id DESC LIMIT ?", params)
    tokens = [t for t in re.findall(r"[a-zA-Z0-9_]+", clean_q) if t.lower() not in ("and", "or")]
    fts_rows: List[Tuple[int, str, str, str]] = []
    with mem._lock:
        cur = mem.conn.cursor()
        if tokens:
            for joiner in (" AND ", " OR "):
                fts_q = joiner.join(f'"{t}"*' for t in tokens)
                sql = (
                    "SELECT c.id, c.role, c.content, c.timestamp FROM conversations_cold_fts f "
                    "JOIN conversations_cold c ON c.id = f.rowid WHERE conversations_cold_fts MATCH ? "
                    + ("AND c.timestamp BETWEEN ? AND ? " if bounds else "")
                    + "ORDER BY rank, c.id DESC LIMIT ?"
                )
                cur.execute(sql, (fts_q, *bounds, max(limit * 2, 25)) if bounds else (fts_q, max(limit * 2, 25)))
                rows = cur.fetchall()
                if rows:
                    fts_rows = rows
                    break
        c_where = "WHERE timestamp BETWEEN ? AND ? " if bounds else ""
        c_params: Tuple[Any, ...] = (*bounds, 250) if bounds else (250,)
        recent = cur.execute(f"SELECT id, role, content, timestamp FROM conversations_cold {c_where}ORDER BY id DESC LIMIT ?", c_params).fetchall()
    by_id: Dict[int, Tuple[int, str, str, str]] = {int(r[0]): r for r in recent}
    for r in fts_rows:
        by_id[int(r[0])] = r
    ranked = hybrid_rrf_rank(clean_q, [(cid, row[2]) for cid, row in by_id.items()], lexical_ids=[int(r[0]) for r in fts_rows], limit=limit)
    return [by_id[cid] for cid in ranked if cid in by_id]


def search_forever_history(
    query: str = "", store: Optional[MemoryStore] = None, limit: int = 20,
    time_window: Optional[str] = None, include_cold: bool = True,
) -> List[Dict[str, Any]]:
    """Unified Hybrid RRF search across both Hot (`<=90d`) active turns and Cold (`>90d`) archived turns."""
    mem = store or get_memory_store()
    _ensure_tables(mem)
    cap = max(1, min(int(limit), 100))
    hot_hits = mem.search_conversations(query=query, limit=cap, time_window=time_window)
    bounds = parse_time_window(time_window) if time_window else None
    cold_hits = _search_cold_table(mem, query, cap, bounds) if include_cold else []

    combined: Dict[str, Dict[str, Any]] = {}
    doc_pairs: List[Tuple[str, str]] = []
    lex_order: List[str] = []
    for tier_label, rows in (("Active Session", hot_hits), ("Cold Archive", cold_hits)):
        for cid, role, content, ts in rows:
            uid = f"{tier_label}:{cid}"
            combined[uid] = {"id": int(cid), "tier": tier_label, "role": str(role), "content": str(content), "timestamp": str(ts)}
            doc_pairs.append((uid, str(content)))
            lex_order.append(uid)
    if not (query or "").strip() or len(doc_pairs) <= 1:
        return list(combined.values())[:cap]
    ranked_keys = hybrid_rrf_rank(query, doc_pairs, lexical_ids=lex_order, limit=cap)
    return [combined[k] for k in ranked_keys if k in combined]


def format_forever_search_results(hits: List[Dict[str, Any]], query: str = "") -> str:
    """Render Hot + Cold forever chat recall hits with provenance badges."""
    if not hits:
        return "No matches across Active Session or Cold Archive."
    tok = next(iter(re.findall(r"[a-zA-Z0-9_]+", (query or "").lower())), "")
    lines: List[str] = []
    for h in hits:
        c = str(h["content"])
        pos = c.lower().find(tok) if tok else -1
        snippet = ("..." + c[max(0, pos - 80) : pos + 220]) if pos > 100 else c[:300]
        lines.append(f"[{h['tier']} #{h['id']} | {h['timestamp']} | {h['role']}]: {snippet}")
    return "\n".join(lines)


def save_prompt_template(
    name: str, template: str, description: str = "", category: str = "general", store: Optional[MemoryStore] = None,
) -> Dict[str, Any]:
    """Create or update a reusable prompt template in the Saved Prompts Library."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")[:64]
    if not slug or not (template or "").strip():
        return {"ok": False, "error": "Both prompt `name` and `template` text are required."}
    mem = store or get_memory_store()
    _ensure_tables(mem)
    with mem._lock:
        mem.conn.execute(
            """INSERT INTO saved_prompts (name, description, category, template, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(name) DO UPDATE SET
                 description = excluded.description, category = excluded.category,
                 template = excluded.template, updated_at = CURRENT_TIMESTAMP""",
            (slug, redact_secrets(description.strip())[:240], (category or "general").strip()[:40], redact_secrets(template.strip())),
        )
        mem.conn.commit()
    return {"ok": True, "name": slug, "summary": f"Saved prompt template `{slug}` ({len(template.strip())} chars)."}


def list_prompt_templates(category: Optional[str] = None, store: Optional[MemoryStore] = None) -> List[Dict[str, Any]]:
    mem = store or get_memory_store()
    _ensure_tables(mem)
    cols = ["name", "description", "category", "template", "updated_at"]
    if category and category.strip():
        return mem._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM saved_prompts WHERE category = ? ORDER BY name ASC", (category.strip(),))
    return mem._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM saved_prompts ORDER BY name ASC")


def get_prompt_template(name: str, store: Optional[MemoryStore] = None) -> Optional[Dict[str, Any]]:
    slug = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")
    mem = store or get_memory_store()
    _ensure_tables(mem)
    cols = ["name", "description", "category", "template", "updated_at"]
    rows = mem._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM saved_prompts WHERE name = ?", (slug,))
    return rows[0] if rows else None


def render_prompt_template(name: str, variables: Optional[Dict[str, Any]] = None, store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    item = get_prompt_template(name, store=store)
    if not item:
        return {"ok": False, "error": f"Prompt template `{name}` not found."}
    rendered = str(item["template"])
    for k, v in (variables or {}).items():
        rendered = rendered.replace(f"{{{{{k}}}}}", str(v))
    unfilled = sorted(set(re.findall(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}", rendered)))
    return {"ok": True, "name": item["name"], "rendered": rendered, "unfilled_variables": unfilled}


def delete_prompt_template(name: str, store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    slug = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")
    mem = store or get_memory_store()
    _ensure_tables(mem)
    with mem._lock:
        cur = mem.conn.execute("DELETE FROM saved_prompts WHERE name = ?", (slug,))
        mem.conn.commit()
    if (cur.rowcount or 0) == 0:
        return {"ok": False, "error": f"Prompt template `{slug}` not found."}
    return {"ok": True, "name": slug, "summary": f"Deleted prompt template `{slug}`."}
