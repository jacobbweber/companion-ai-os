"""
Universal Remote Endpoints (`ssh`, `winrm`, `http_api`), Persistent Outbound MCP Client,
Hosted MCP 'Order Counter' & Federated Partner Hub (`companion/integrations.py`).

Consolidates `endpoints.py` and `mcp_hub.py` into a single cohesive external-integrations module:
1. `EndpointManager`: Blind-Vault SSH, WinRM, and HTTP/REST endpoints with SSRF & read-only command guards.
2. `MCPHub`: Persistent `stdio` subprocess session pooling (eliminating per-call Node/Python cold starts),
   Streamable HTTP MCP client, SHA-256 tool-fingerprint approval, inbound Hosted MCP Order Counter,
   and Federated Companion Partner handshakes/job delegation.
3. `harvest_mcp_capability_pack`: LLM-enriched `SKILL.md` + declarative `*.workflow.json` generator
   validated deterministically via `stage_and_promote_skill`.
"""

import atexit
import base64
import contextlib
import ctypes
import hashlib
import json
import os
import platform
import queue
import re
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from companion import __version__
from companion.approvals import compute_approval_fingerprint, get_approval_queue, get_trust_mode
from companion.config import is_in_agent_tool_turn, settings
from companion.llm import chat, normalize_assistant_message, tier_context
from companion.llm_wire import check_url_ssrf, urlopen_safe
from companion.memory_store import MemoryStore, get_memory_store
from companion.tools import check_command_safety
from companion.vault import _lock_windows_owner_dacl, get_vault_manager, normalize_secret_name

__all__ = [
    "EndpointManager",
    "HOSTED_MCP_SAFE_TOOLS",
    "MCPHub",
    "VALID_AUTH_MODES",
    "VALID_PROTOCOLS",
    "build_local_peer_identity",
    "close_all_stdio_mcp_sessions",
    "format_connected_partners_hud",
    "get_endpoint_manager",
    "get_mcp_hub",
    "harvest_mcp_capability_pack",
]

# ---------------------------------------------------------------------------
# 1. Universal Endpoint Registry (`ssh`, `winrm`, `http_api`)
# ---------------------------------------------------------------------------
VALID_PROTOCOLS = ("ssh", "winrm", "http_api")
VALID_AUTH_MODES = ("none", "ssh_key", "ssh_agent", "basic_auth", "bearer_token", "api_key_header")
_AUTH_MODE_ALIASES: Dict[str, str] = {
    "password": "basic_auth",
    "passwd": "basic_auth",
    "userpass": "basic_auth",
    "key": "ssh_key",
    "private_key": "ssh_key",
    "agent": "ssh_agent",
    "bearer": "bearer_token",
    "token": "bearer_token",
    "api_key": "api_key_header",
    "header": "api_key_header",
}


def _resolve_effective_auth_mode(raw_mode: str, proto: str, cred_slug: str, store: MemoryStore) -> str:
    """Resolve explicit/aliased `auth_mode` or infer cleanly from the linked Blind Vault credential's `cred_type`."""
    m = (raw_mode or "").strip().lower()
    if m and m != "auto":
        return _AUTH_MODE_ALIASES.get(m, m)
    meta = get_vault_manager(store=store).get_metadata(cred_slug) if cred_slug else None
    mtype = str((meta or {}).get("cred_type") or "").strip().lower()
    if proto == "ssh":
        if not cred_slug:
            return "ssh_agent"
        return "basic_auth" if mtype == "basic_auth" else "ssh_key"
    if proto == "winrm":
        return "basic_auth"
    if proto == "http_api":
        if not cred_slug:
            return "none"
        if mtype == "basic_auth":
            return "basic_auth"
        if mtype == "api_key":
            return "api_key_header"
        return "bearer_token"
    return "none"
_DEFAULT_PORTS = {"ssh": 22, "winrm": 5985, "http_api": 80}
_BLOCKED_META_HOSTS = {"metadata.google.internal", "metadata", "instance-data", "169.254.169.254"}
_SECRET_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASS")
_READONLY_SINGLE_CMDS = frozenset({
    "echo", "uname", "hostname", "uptime", "df", "free", "lscpu", "lspci", "lsblk",
    "nproc", "ps", "ls", "cat", "head", "tail", "grep", "rg", "wc", "stat",
    "id", "whoami", "find", "journalctl", "nvidia-smi", "ss",
})
_READONLY_TWO_WORD_CMDS = frozenset({"systemctl status", "docker ps", "docker info", "docker logs", "ip addr", "ip route"})
_SAFE_SED_READ_RE = re.compile(r"^sed\s+-n\s+'1,\d+p'\s+--\s+\S+$")
_SENSITIVE_REMOTE_SUBSTRS = (".ssh", "shadow", "secrets", ".env", "id_rsa", "id_ed25519", "authorized_keys", "private_key")
_STDERR_REDIRECT_RE = re.compile(r"(?:^|\s)2>(?:/dev/null|&1)(?=\s|$|;|&&|\|\||\|)")


def _parse_user_host_port(raw_host: str, raw_user: str, raw_port: int) -> Tuple[str, str, int]:
    h, u, p = (raw_host or "").strip(), (raw_user or "").strip(), int(raw_port or 0)
    if "://" in h:
        return h, u, p
    if "@" in h:
        u_part, h = h.rsplit("@", 1)
        u = u or u_part.strip()
    if h.count(":") == 1:
        host_part, port_part = h.rsplit(":", 1)
        if port_part.isdigit():
            h = host_part.strip()
            p = p or int(port_part)
    return h, u, p


def _is_readonly_segment(toks: List[str]) -> bool:
    if not toks:
        return False
    raw0 = toks[0]
    if ("/" in raw0 or "\\" in raw0) and not raw0.startswith(("/bin/", "/usr/bin/", "/sbin/", "/usr/sbin/")):
        return False
    cmd0 = Path(raw0).name.lower()
    if cmd0 == "find" and any(t in ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fls") for t in toks[1:]):
        return False
    if cmd0 in _READONLY_SINGLE_CMDS:
        return True
    if len(toks) >= 2 and f"{cmd0} {toks[1].lower()}" in _READONLY_TWO_WORD_CMDS:
        return True
    return False


def _is_readonly_remote_command(command: str, protocol: str) -> bool:
    if protocol != "ssh":
        return False
    clean = (command or "").strip()
    if not clean or any(ch in clean for ch in ("`", "\n", "\r")) or "$(" in clean or "${" in clean:
        return False
    low = clean.lower()
    if any(s in low for s in _SENSITIVE_REMOTE_SUBSTRS):
        return False
    if _SAFE_SED_READ_RE.match(clean):
        return True
    scrubbed = _STDERR_REDIRECT_RE.sub(" ", clean)
    if ">" in scrubbed or "<" in scrubbed:
        return False
    try:
        lex = shlex.shlex(scrubbed, posix=True, punctuation_chars=";&|()")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return False
    if not tokens:
        return False
    segments: List[List[str]] = []
    cur: List[str] = []
    paren_depth = 0
    for tok in tokens:
        if tok == "&" or any(c in tok for c in (">", "<", "`")):
            return False
        if tok == "(":
            if cur:
                return False
            paren_depth += 1
            continue
        if tok == ")":
            paren_depth -= 1
            if paren_depth < 0:
                return False
            if cur:
                segments.append(cur)
                cur = []
            continue
        if tok in ("&&", "||", ";", "|"):
            if not cur and not segments:
                return False
            if cur:
                segments.append(cur)
                cur = []
            continue
        cur.append(tok)
    if paren_depth != 0:
        return False
    if cur:
        segments.append(cur)
    return bool(segments) and all(_is_readonly_segment(seg) for seg in segments)


ENDPOINTS_DDL: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS remote_endpoints (
        endpoint_id TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        protocol TEXT NOT NULL,
        host TEXT NOT NULL,
        port INTEGER NOT NULL,
        username TEXT DEFAULT '',
        auth_mode TEXT DEFAULT 'none',
        auth_header TEXT DEFAULT 'X-API-Key',
        credential_ref TEXT DEFAULT '',
        os_hint TEXT DEFAULT '',
        verify_tls INTEGER DEFAULT 1,
        tags_json TEXT DEFAULT '[]',
        last_probe_status TEXT DEFAULT 'untested',
        last_probe_ms REAL DEFAULT 0.0,
        host_profile_json TEXT DEFAULT '{}',
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
)


def _slug_id(raw: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", (raw or "").strip().lower()).strip("-")[:64]


def _LooksLikeRawSecret(val: str) -> bool:
    v = (val or "").strip()
    return ("-----BEGIN " in v) or v.startswith(("sk-", "ghp_", "github_pat_", "nvapi-", "AIza")) or (" " in v and len(v) > 24)


def _check_metadata_ssrf(host_or_url: str) -> Optional[str]:
    raw = (host_or_url or "").strip()
    host = (urllib.parse.urlsplit(raw).hostname if "://" in raw else raw.split(":")[0]).strip().lower()
    if not host:
        return "Endpoint `host` is empty."
    if host in _BLOCKED_META_HOSTS:
        return f"SAFETY GUARD REJECTED: cloud metadata endpoint `{host}` is blocked."
    return check_url_ssrf(raw, allow_private_lan=True)


class EndpointManager:
    """Manages SSH, Windows WinRM, and Authenticated HTTP API endpoints backed by Blind Vault credentials."""

    def __init__(self, store: Optional[MemoryStore] = None):
        self._store = store

    @property
    def store(self) -> MemoryStore:
        mem = self._store or get_memory_store()
        mem.ensure_schema("endpoints", lambda m: [m.conn.execute(ddl) for ddl in ENDPOINTS_DDL] and None)
        return mem

    def register_endpoint(
        self,
        endpoint_id: str,
        protocol: str,
        host: str,
        label: str = "",
        port: int = 0,
        username: str = "",
        auth_mode: str = "",
        credential_ref: str = "",
        auth_header: str = "X-API-Key",
        os_hint: str = "",
        verify_tls: bool = True,
        tags: Optional[List[str]] = None,
        actor: str = "user",
        confirmed: bool = False,
    ) -> Dict[str, Any]:
        """Register or update a remote endpoint using a symbolic Vault `credential_ref` (never raw secrets)."""
        if (actor.strip().lower() not in ("user", "cli", "web") or is_in_agent_tool_turn()) and not confirmed:
            return {"ok": False, "error": "ENDPOINT GUARD REJECTED: Only the user can register remote endpoints directly; call `propose` to enqueue an approval request."}
        proto = (protocol or "").strip().lower()
        clean_host, clean_user, port = _parse_user_host_port(host, username, port)
        eid = _slug_id(endpoint_id or label or clean_host)
        if not eid or proto not in VALID_PROTOCOLS or not clean_host:
            return {"ok": False, "error": f"`endpoint_id`, `host`, and `protocol` ({', '.join(VALID_PROTOCOLS)}) are required."}
        host_only_chk = (urllib.parse.urlsplit(clean_host).hostname if "://" in clean_host else clean_host.split(":")[0]).strip()
        if (
            host_only_chk.startswith("-")
            or clean_user.startswith("-")
            or any(c.isspace() or ord(c) < 32 for c in clean_host)
            or any(c.isspace() or ord(c) < 32 for c in clean_user)
        ):
            return {"ok": False, "error": "SAFETY GUARD REJECTED: `host` and `username` must not start with `-` or contain whitespace/control characters."}
        ssrf_err = _check_metadata_ssrf(clean_host)
        if ssrf_err:
            return {"ok": False, "error": ssrf_err}
        if credential_ref and _LooksLikeRawSecret(credential_ref):
            return {"ok": False, "error": "VAULT GUARD REJECTED: `credential_ref` must be a symbolic Vault name (like `node-ssh`), NEVER a raw key or token."}

        cred_slug = normalize_secret_name(credential_ref) if credential_ref else ""
        eff_auth = _resolve_effective_auth_mode(auth_mode, proto, cred_slug, self.store)
        if eff_auth not in VALID_AUTH_MODES:
            return {"ok": False, "error": f"Invalid `auth_mode` '{eff_auth}'. Valid: {', '.join(VALID_AUTH_MODES)}."}
        parsed_url = urllib.parse.urlsplit(clean_host) if "://" in clean_host else None
        url_port = int(parsed_url.port) if (parsed_url and parsed_url.port) else 0
        eff_port = int(port) if int(port or 0) > 0 else (url_port or (443 if proto == "http_api" and clean_host.lower().startswith("https://") else _DEFAULT_PORTS[proto]))

        if cred_slug:
            ctype = "ssh_key" if eff_auth == "ssh_key" else ("basic_auth" if eff_auth == "basic_auth" else ("bearer_token" if eff_auth == "bearer_token" else "api_key"))
            get_vault_manager(store=self.store).declare_placeholder(
                cred_slug, cred_type=ctype, description=f"Credential for endpoint `{eid}`", allow_list=[f"endpoint:{eid}"], actor=actor,
            )

        with self.store._lock:
            self.store.conn.execute(
                """INSERT INTO remote_endpoints
                   (endpoint_id, label, protocol, host, port, username, auth_mode, auth_header, credential_ref, os_hint, verify_tls, tags_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(endpoint_id) DO UPDATE SET
                     label = excluded.label, protocol = excluded.protocol, host = excluded.host, port = excluded.port,
                     username = excluded.username, auth_mode = excluded.auth_mode, auth_header = excluded.auth_header,
                     credential_ref = excluded.credential_ref, os_hint = excluded.os_hint, verify_tls = excluded.verify_tls,
                     tags_json = excluded.tags_json, updated_at = CURRENT_TIMESTAMP""",
                (
                    eid, (label.strip() or eid)[:100], proto, clean_host, eff_port, clean_user[:80],
                    eff_auth, (auth_header.strip() or "X-API-Key")[:64], cred_slug, os_hint.strip()[:40],
                    1 if verify_tls else 0, json.dumps([str(t).strip() for t in (tags or []) if str(t).strip()]),
                ),
            )
            self.store.conn.commit()
        disp_addr = clean_host if "://" in clean_host else f"{clean_host}:{eff_port}"
        return {"ok": True, "endpoint": self.get_endpoint(eid), "summary": f"Registered `{proto}` endpoint `{eid}` -> `{disp_addr}` (credential_ref=`{cred_slug or 'none'}`)."}

    def propose_endpoint(
        self,
        endpoint_id: str,
        protocol: str,
        host: str,
        label: str = "",
        port: int = 0,
        username: str = "",
        auth_mode: str = "",
        credential_ref: str = "",
        auth_header: str = "X-API-Key",
        os_hint: str = "",
        verify_tls: bool = True,
        tags: Optional[List[str]] = None,
        actor: str = "companion",
    ) -> Dict[str, Any]:
        """Stage an SSRF-validated remote endpoint + symbolic Vault placeholder into `ApprovalQueue` (`actor='companion'` safe)."""
        proto = (protocol or "").strip().lower()
        clean_host, clean_user, port = _parse_user_host_port(host, username, port)
        eid = _slug_id(endpoint_id or label or clean_host)
        if not eid or proto not in VALID_PROTOCOLS or not clean_host:
            return {"ok": False, "error": f"`endpoint_id`, `host`, and `protocol` ({', '.join(VALID_PROTOCOLS)}) are required."}
        ssrf_err = _check_metadata_ssrf(clean_host)
        if ssrf_err:
            return {"ok": False, "error": ssrf_err}
        if credential_ref and _LooksLikeRawSecret(credential_ref):
            return {"ok": False, "error": "VAULT GUARD REJECTED: `credential_ref` must be a symbolic Vault name, NEVER a raw secret."}
        cred_slug = normalize_secret_name(credential_ref) if credential_ref else ""
        eff_auth = _resolve_effective_auth_mode(auth_mode, proto, cred_slug, self.store)
        if eff_auth not in VALID_AUTH_MODES:
            return {"ok": False, "error": f"Invalid `auth_mode` '{eff_auth}'. Valid: {', '.join(VALID_AUTH_MODES)}."}
        if cred_slug:
            ctype = "ssh_key" if eff_auth == "ssh_key" else ("basic_auth" if eff_auth == "basic_auth" else ("bearer_token" if eff_auth == "bearer_token" else "api_key"))
            get_vault_manager(store=self.store).declare_placeholder(
                cred_slug, cred_type=ctype, description=f"Proposed credential for endpoint `{eid}`", allow_list=[f"endpoint:{eid}"], actor=actor,
            )
        payload = {
            "endpoint_id": eid, "protocol": proto, "host": clean_host, "label": label.strip() or eid,
            "port": int(port or 0), "username": clean_user, "auth_mode": eff_auth,
            "credential_ref": cred_slug, "auth_header": auth_header.strip() or "X-API-Key",
            "os_hint": os_hint.strip(), "verify_tls": bool(verify_tls), "tags": list(tags or []),
        }
        _allowed, apr = get_approval_queue(self.store).check_or_enqueue(
            category="endpoint_register",
            action_name="register_endpoint",
            target=eid,
            summary=f"Register `{proto}` endpoint `{label.strip() or eid}` (`{clean_host}`, credential_ref=`{cred_slug or 'none'}`).",
            payload=payload,
            requester=actor,
            confirmed=False,
            always_require_in_autonomous=True,
        )
        return {
            "ok": True,
            "awaiting_approval": True,
            "approval_id": apr["approval_id"],
            "endpoint_id": eid,
            "credential_ref": cred_slug,
            "summary": f"Staged endpoint `{eid}` (`{proto}` -> `{clean_host}`) for 1-click user approval (`{apr['approval_id']}`).",
        }

    def get_endpoint(self, endpoint_id: str) -> Optional[Dict[str, Any]]:
        cols = [
            "endpoint_id", "label", "protocol", "host", "port", "username", "auth_mode", "auth_header",
            "credential_ref", "os_hint", "verify_tls", "tags_json", "last_probe_status", "last_probe_ms", "host_profile_json", "updated_at",
        ]
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM remote_endpoints WHERE endpoint_id = ?", (_slug_id(endpoint_id),))
        if not rows:
            return None
        r = rows[0]
        r["verify_tls"] = bool(r["verify_tls"])
        r["tags"] = json.loads(r.pop("tags_json") or "[]")
        r["host_profile"] = json.loads(r.pop("host_profile_json") or "{}")
        return r

    def list_endpoints(self, protocol: str = "") -> List[Dict[str, Any]]:
        cols = [
            "endpoint_id", "label", "protocol", "host", "port", "username", "auth_mode", "auth_header",
            "credential_ref", "os_hint", "verify_tls", "tags_json", "last_probe_status", "last_probe_ms", "host_profile_json", "updated_at",
        ]
        if protocol and protocol.strip():
            rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM remote_endpoints WHERE protocol = ? ORDER BY endpoint_id ASC", (protocol.strip().lower(),))
        else:
            rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM remote_endpoints ORDER BY endpoint_id ASC")
        for r in rows:
            r["verify_tls"] = bool(r["verify_tls"])
            r["tags"] = json.loads(r.pop("tags_json") or "[]")
            r["host_profile"] = json.loads(r.pop("host_profile_json") or "{}")
        return rows

    def remove_endpoint(self, endpoint_id: str, actor: str = "user") -> Dict[str, Any]:
        if actor.strip().lower() not in ("user", "cli", "web") or is_in_agent_tool_turn():
            return {"ok": False, "error": "ENDPOINT GUARD REJECTED: Only the user can remove remote endpoints."}
        eid = _slug_id(endpoint_id)
        with self.store._lock:
            cur = self.store.conn.execute("DELETE FROM remote_endpoints WHERE endpoint_id = ?", (eid,))
            self.store.conn.commit()
        if (cur.rowcount or 0) == 0:
            return {"ok": False, "error": f"Endpoint `{eid}` not found."}
        return {"ok": True, "endpoint_id": eid, "summary": f"Removed endpoint `{eid}`."}

    def _record_probe(self, eid: str, status: str, ms: float, host_profile: Optional[Dict[str, Any]] = None) -> None:
        with self.store._lock:
            if host_profile is not None:
                self.store.conn.execute(
                    "UPDATE remote_endpoints SET last_probe_status = ?, last_probe_ms = ?, host_profile_json = ?, updated_at = CURRENT_TIMESTAMP WHERE endpoint_id = ?",
                    (status[:80], round(ms, 1), json.dumps(host_profile), eid),
                )
            else:
                self.store.conn.execute(
                    "UPDATE remote_endpoints SET last_probe_status = ?, last_probe_ms = ?, updated_at = CURRENT_TIMESTAMP WHERE endpoint_id = ?",
                    (status[:80], round(ms, 1), eid),
                )
            self.store.conn.commit()

    def _resolve_ssh_host_port(self, ep: Dict[str, Any]) -> Tuple[str, int]:
        host_only = str(urllib.parse.urlsplit(ep["host"]).hostname or ep["host"]).strip()
        port = int(ep["port"])
        uname = str(ep.get("username") or "").strip()
        if host_only.startswith("-") or uname.startswith("-"):
            return host_only, port
        ssh_bin = shutil.which("ssh")
        if ep["protocol"] == "ssh" and ssh_bin:
            dest = f"{uname}@{host_only}" if uname else host_only
            try:
                proc = subprocess.run([ssh_bin, "-G", "-p", str(port), "--", dest], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3)
                if proc.returncode == 0:
                    cfg = dict(ln.strip().split(" ", 1) for ln in proc.stdout.splitlines() if " " in ln.strip())
                    cand_host = cfg.get("hostname", host_only)
                    if not _check_metadata_ssrf(cand_host):
                        host_only = cand_host
                    port = int(cfg.get("port", port))
            except Exception:
                pass
        return host_only, port

    def test_endpoint(self, endpoint_id: str, timeout: float = 5.0) -> Dict[str, Any]:
        """Connectivity & handshake probe across `ssh`, `winrm`, and `http_api` endpoints."""
        ep = self.get_endpoint(endpoint_id)
        if not ep:
            return {"ok": False, "error": f"Endpoint `{endpoint_id}` not found."}
        t0 = time.perf_counter()
        if ep["protocol"] == "http_api":
            res = self.api_request(ep["endpoint_id"], method="GET", path="/", timeout=timeout)
            ms = (time.perf_counter() - t0) * 1000.0
            status_lbl = f"online ({res.get('status_code')})" if res.get("ok") else f"error ({res.get('error', 'unreachable')[:40]})"
            self._record_probe(ep["endpoint_id"], status_lbl, ms)
            return {**res, "latency_ms": round(ms, 1), "summary": f"HTTP probe `{ep['endpoint_id']}`: {status_lbl} in {ms:.1f}ms."}

        host_only, eff_port = self._resolve_ssh_host_port(ep)
        try:
            with socket.create_connection((host_only, eff_port), timeout=timeout) as sock:
                sock.settimeout(min(timeout, 2.5))
                banner = sock.recv(128).decode("utf-8", errors="replace").strip() if ep["protocol"] == "ssh" else ""
            ms = (time.perf_counter() - t0) * 1000.0
            detail = f"online ({banner[:36]})" if banner else "online (tcp_ok)"
            self._record_probe(ep["endpoint_id"], detail, ms)
            return {"ok": True, "endpoint_id": ep["endpoint_id"], "protocol": ep["protocol"], "resolved_host": host_only, "latency_ms": round(ms, 1), "banner": banner, "summary": f"Endpoint `{ep['endpoint_id']}` reachable ({host_only}:{eff_port}): {detail} in {ms:.1f}ms."}
        except Exception as exc:
            ms = (time.perf_counter() - t0) * 1000.0
            err_s = f"offline ({type(exc).__name__}: {exc})"
            self._record_probe(ep["endpoint_id"], err_s[:80], ms)
            return {"ok": False, "endpoint_id": ep["endpoint_id"], "latency_ms": round(ms, 1), "error": err_s}

    def _gate_remote_exec(self, ep: Dict[str, Any], command: str, actor: str, confirmed: bool, internal_readonly: bool = False) -> Optional[Dict[str, Any]]:
        if not internal_readonly:
            safety_err = check_command_safety(command)
            if safety_err:
                return {"ok": False, "error": safety_err}
        human_ok = (actor.strip().lower() in ("user", "cli", "web") or confirmed) and not is_in_agent_tool_turn()
        if human_ok:
            return None
        trust = get_trust_mode(store=self.store)
        is_ro = internal_readonly or _is_readonly_remote_command(command, str(ep.get("protocol") or ""))
        if trust != "strict" and is_ro:
            return None
        aq = get_approval_queue(self.store)
        act_name = f"{ep['protocol']}_exec"
        req_payload = {"endpoint_id": ep["endpoint_id"], "command": command}
        fp = compute_approval_fingerprint("endpoint_exec", act_name, ep["endpoint_id"], req_payload)
        recent_exec = aq.consume_recent_executed(fp)
        if recent_exec is not None:
            return {
                "_cached_approved_result": {
                    "ok": True,
                    "returncode": 0,
                    "output": recent_exec["execution_output"],
                    "error": "",
                }
            }
        allowed, apr = aq.check_or_enqueue(
            category="endpoint_exec",
            action_name=act_name,
            target=ep["endpoint_id"],
            summary=f"Execute `{command[:100]}` on remote `{ep['protocol']}` host `{ep['label']}` (`{ep['host']}`).",
            payload=req_payload,
            requester=actor,
            confirmed=human_ok,
        )
        if not allowed:
            return {"ok": False, "awaiting_approval": True, "approval_id": apr["approval_id"], "error": f"REMOTE EXEC APPROVAL REQUIRED (`{apr['approval_id']}`): Command on `{ep['endpoint_id']}` awaits approval."}
        return None

    def ssh_exec(
        self, endpoint_id: str, command: str, timeout: int = 30, actor: str = "companion",
        confirmed: bool = False, _internal_readonly: bool = False,
    ) -> Dict[str, Any]:
        """Execute a safety-checked command over SSH using JIT Vault key injection, Vault password (`SSH_ASKPASS`), or SSH agent."""
        ep = self.get_endpoint(endpoint_id)
        if not ep or ep["protocol"] != "ssh":
            return {"ok": False, "error": f"SSH endpoint `{endpoint_id}` not found."}
        if not (command or "").strip():
            return {"ok": False, "error": "Non-empty `command` is required."}
        raw_host, user_s = str(ep.get("host") or "").strip(), str(ep.get("username") or "").strip()
        if raw_host.startswith("-") or user_s.startswith("-") or any(c.isspace() or ord(c) < 32 for c in raw_host + user_s):
            return {"ok": False, "error": "SAFETY GUARD REJECTED: SSH host/username must not start with `-` or contain whitespace."}
        host_s, eff_port = self._resolve_ssh_host_port(ep)
        ssrf_err = _check_metadata_ssrf(host_s)
        if ssrf_err:
            return {"ok": False, "error": ssrf_err}
        gate_block = self._gate_remote_exec(ep, command, actor=actor, confirmed=confirmed, internal_readonly=_internal_readonly)
        if gate_block:
            if "_cached_approved_result" in gate_block:
                return dict(gate_block["_cached_approved_result"])
            return gate_block
        ssh_bin = shutil.which("ssh")
        if not ssh_bin:
            return {"ok": False, "error": "OpenSSH `ssh` client binary not found on PATH."}

        vault = get_vault_manager(store=self.store)
        tmp_key_path = ""
        tmp_askpass_path = ""
        extra_secrets: List[str] = []
        ssh_env: Optional[Dict[str, str]] = None
        try:
            eff_auth = str(ep.get("auth_mode") or "ssh_agent").strip().lower()
            cred_ref = str(ep.get("credential_ref") or "").strip()
            if cred_ref and eff_auth == "ssh_key":
                meta = vault.get_metadata(cred_ref)
                if meta and str(meta.get("cred_type") or "").lower() == "basic_auth":
                    eff_auth = "basic_auth"

            ssh_args = [
                ssh_bin, "-T", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
                "-o", "StrictHostKeyChecking=accept-new", "-p", str(eff_port),
            ]
            if eff_auth == "basic_auth" and cred_ref:
                ok, val_or_err = vault.resolve_for_call(cred_ref, f"endpoint:{ep['endpoint_id']}", expected_types=("basic_auth",))
                if not ok:
                    return {"ok": False, "error": val_or_err}
                extra_secrets.append(val_or_err)
                if not user_s and ":" in val_or_err:
                    u_cand, ssh_pass = val_or_err.split(":", 1)
                    if not u_cand.startswith("-") and not any(c.isspace() or ord(c) < 32 for c in u_cand):
                        user_s = u_cand
                else:
                    ssh_pass = val_or_err
                if ssh_pass:
                    extra_secrets.append(ssh_pass)
                ssh_tmp_dir = settings.effective_data_dir / ".ssh_tmp"
                ssh_tmp_dir.mkdir(parents=True, exist_ok=True)
                _lock_windows_owner_dacl(ssh_tmp_dir)
                if os.name == "nt":
                    fd, tmp_askpass_path = tempfile.mkstemp(prefix=".comp_askpass_", suffix=".cmd", dir=str(ssh_tmp_dir))
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as af:
                        af.write("@echo off\r\necho %_COMP_SSH_PASS%\r\n")
                else:
                    fd, tmp_askpass_path = tempfile.mkstemp(prefix=".comp_askpass_", suffix=".sh", dir=str(ssh_tmp_dir))
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as af:
                        af.write('#!/bin/sh\nprintf \'%s\\n\' "$_COMP_SSH_PASS"\n')
                    with contextlib.suppress(OSError):
                        os.chmod(tmp_askpass_path, 0o700)
                _lock_windows_owner_dacl(Path(tmp_askpass_path))
                ssh_env = {
                    k: v for k, v in os.environ.items()
                    if k.upper() != "COMPANION_WEB_TOKEN" and not k.upper().endswith(_SECRET_ENV_SUFFIXES)
                }
                ssh_env["DISPLAY"] = "companion:0"
                ssh_env["SSH_ASKPASS"] = tmp_askpass_path
                ssh_env["SSH_ASKPASS_REQUIRE"] = "force"
                ssh_env["_COMP_SSH_PASS"] = ssh_pass
                ssh_args.extend([
                    "-o", "BatchMode=no",
                    "-o", "PreferredAuthentications=password,keyboard-interactive",
                    "-o", "PubkeyAuthentication=no",
                    "-o", "NumberOfPasswordPrompts=1",
                ])
            elif eff_auth == "ssh_key" and cred_ref:
                ssh_args.extend(["-o", "BatchMode=yes"])
                ok, val_or_err = vault.resolve_for_call(cred_ref, f"endpoint:{ep['endpoint_id']}", expected_types=("ssh_key",))
                if not ok:
                    return {"ok": False, "error": val_or_err}
                extra_secrets.append(val_or_err)
                ssh_tmp_dir = settings.effective_data_dir / ".ssh_tmp"
                ssh_tmp_dir.mkdir(parents=True, exist_ok=True)
                _lock_windows_owner_dacl(ssh_tmp_dir)
                fd, tmp_key_path = tempfile.mkstemp(prefix=".comp_ssh_", suffix=".key", dir=str(ssh_tmp_dir))
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as kf:
                    kf.write(val_or_err if val_or_err.endswith("\n") else val_or_err + "\n")
                with contextlib.suppress(OSError):
                    os.chmod(tmp_key_path, 0o600)
                _lock_windows_owner_dacl(Path(tmp_key_path))
                ssh_args.extend(["-i", tmp_key_path])
            else:
                ssh_args.extend(["-o", "BatchMode=yes"])

            dest = f"{user_s}@{host_s}" if user_s else host_s
            proc = subprocess.run([*ssh_args, "--", dest, command], env=ssh_env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=max(5, int(timeout)))
            out = vault.scrub_output((proc.stdout or "").strip(), extra_secrets=extra_secrets)
            err = vault.scrub_output((proc.stderr or "").strip(), extra_secrets=extra_secrets)
            combined = (out + (f"\n[stderr]\n{err}" if err else "")).strip() or "(no output)"
            return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": combined[: settings.out_limit], "error": "" if proc.returncode == 0 else combined[:400]}
        except Exception as exc:
            return {"ok": False, "error": vault.scrub_output(f"SSH error: {type(exc).__name__}: {exc}", extra_secrets=extra_secrets)}
        finally:
            for p_tmp in (tmp_key_path, tmp_askpass_path):
                if p_tmp and os.path.exists(p_tmp):
                    with contextlib.suppress(OSError):
                        os.unlink(p_tmp)

    def ssh_read_file(self, endpoint_id: str, remote_path: str, max_lines: int = 200, actor: str = "user", confirmed: bool = False) -> Dict[str, Any]:
        """Safely read bounded lines from a file on a remote SSH host without shell injection."""
        clean_p = (remote_path or "").strip()
        if not clean_p:
            return {"ok": False, "error": "`remote_path` is required."}
        cap = max(1, min(int(max_lines or 200), 800))
        safe_cmd = f"sed -n '1,{cap}p' -- {shlex.quote(clean_p)}"
        return self.ssh_exec(endpoint_id, safe_cmd, actor=actor, confirmed=confirmed)

    def winrm_exec(
        self, endpoint_id: str, command: str, timeout: int = 30, actor: str = "companion",
        confirmed: bool = False, _internal_readonly: bool = False,
    ) -> Dict[str, Any]:
        """Execute a safety-checked PowerShell command on a Windows WinRM host with env-isolated Vault credentials."""
        ep = self.get_endpoint(endpoint_id)
        if not ep or ep["protocol"] != "winrm":
            return {"ok": False, "error": f"WinRM endpoint `{endpoint_id}` not found."}
        if not (command or "").strip():
            return {"ok": False, "error": "Non-empty PowerShell `command` is required."}
        gate_block = self._gate_remote_exec(ep, command, actor=actor, confirmed=confirmed, internal_readonly=_internal_readonly)
        if gate_block:
            if "_cached_approved_result" in gate_block:
                return dict(gate_block["_cached_approved_result"])
            return gate_block
        ps_bin = shutil.which("pwsh") or shutil.which("powershell")
        if not ps_bin:
            return {"ok": False, "error": "PowerShell (`pwsh` / `powershell`) is required locally to drive WinRM sessions."}

        vault = get_vault_manager(store=self.store)
        env = {
            k: v for k, v in os.environ.items()
            if k.upper() != "COMPANION_WEB_TOKEN" and not k.upper().endswith(_SECRET_ENV_SUFFIXES)
        }
        extra_secrets: List[str] = []
        if ep["credential_ref"]:
            ok, secret_val = vault.resolve_for_call(ep["credential_ref"], f"endpoint:{ep['endpoint_id']}", expected_types=("basic_auth", "api_key"))
            if not ok:
                return {"ok": False, "error": secret_val}
            extra_secrets.append(secret_val)
            user_part, pass_part = (secret_val.split(":", 1) if ":" in secret_val and not ep.get("username") else (ep.get("username") or "Administrator", secret_val))
            env["_COMP_WINRM_USER"] = user_part
            env["_COMP_WINRM_PASS"] = pass_part
        op_sec = max(5, int(timeout))
        env["_COMP_WINRM_HOST"] = str(ep["host"])
        env["_COMP_WINRM_PORT"] = str(ep["port"])
        env["_COMP_WINRM_CMD"] = command
        ssl_flag = "-UseSSL" if int(ep["port"]) == 5986 else ""
        tl_opt = "-SkipCACheck -SkipCNCheck " if not ep.get("verify_tls", True) else ""
        sess_opt = f"-SessionOption (New-PSSessionOption {tl_opt}-OperationTimeout ({op_sec * 1000}))"
        ps_driver = (
            "$ErrorActionPreference = 'Stop'; "
            "if ($env:_COMP_WINRM_PASS) { "
            "  $sec = ConvertTo-SecureString $env:_COMP_WINRM_PASS -AsPlainText -Force; "
            "  $cred = New-Object System.Management.Automation.PSCredential($env:_COMP_WINRM_USER, $sec); "
            f"  Invoke-Command -ComputerName $env:_COMP_WINRM_HOST -Port ([int]$env:_COMP_WINRM_PORT) {ssl_flag} {sess_opt} -Credential $cred -ScriptBlock ([ScriptBlock]::Create($env:_COMP_WINRM_CMD)) "
            "} else { "
            f"  Invoke-Command -ComputerName $env:_COMP_WINRM_HOST -Port ([int]$env:_COMP_WINRM_PORT) {ssl_flag} {sess_opt} -ScriptBlock ([ScriptBlock]::Create($env:_COMP_WINRM_CMD)) "
            "}"
        )
        try:
            proc = subprocess.run([ps_bin, "-NoProfile", "-NonInteractive", "-Command", ps_driver], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=op_sec + 5)
            out = vault.scrub_output((proc.stdout or "").strip(), extra_secrets=extra_secrets)
            err = vault.scrub_output((proc.stderr or "").strip(), extra_secrets=extra_secrets)
            combined = (out + (f"\n[stderr]\n{err}" if err else "")).strip() or "(no output)"
            return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": combined[: settings.out_limit], "error": "" if proc.returncode == 0 else combined[:400]}
        except Exception as exc:
            return {"ok": False, "error": vault.scrub_output(f"WinRM error: {type(exc).__name__}: {exc}", extra_secrets=extra_secrets)}

    def api_request(
        self, endpoint_id: str, method: str = "GET", path: str = "/",
        json_body: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None,
        timeout: float = 20.0, actor: str = "companion", confirmed: bool = False,
    ) -> Dict[str, Any]:
        """Execute an authenticated HTTP/REST API call injecting Vault credentials in-memory without exposing tokens."""
        ep = self.get_endpoint(endpoint_id)
        if not ep or ep["protocol"] != "http_api":
            return {"ok": False, "error": f"HTTP API endpoint `{endpoint_id}` not found."}
        verb = (method or "GET").strip().upper()
        if verb not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
            return {"ok": False, "error": f"Unsupported HTTP method `{verb}`."}
        human_ok = (actor.strip().lower() in ("user", "cli", "web") or confirmed) and not is_in_agent_tool_turn()
        if verb in ("POST", "PUT", "PATCH", "DELETE") and not human_ok:
            if get_trust_mode(store=self.store) == "strict" or verb == "DELETE":
                allowed, apr = get_approval_queue(self.store).check_or_enqueue(
                    category="endpoint_exec", action_name=f"http_{verb.lower()}", target=f"{ep['endpoint_id']}:{path}",
                    summary=f"Send `{verb} {path}` to HTTP endpoint `{ep['label']}`.",
                    payload={"endpoint_id": ep["endpoint_id"], "method": verb, "path": path, "json_body": json_body, "headers": headers or {}},
                    requester=actor, confirmed=human_ok,
                )
                if not allowed:
                    return {"ok": False, "awaiting_approval": True, "approval_id": apr["approval_id"], "error": f"HTTP `{verb}` requires approval (`{apr['approval_id']}`)."}

        base = ep["host"] if "://" in ep["host"] else f"{'https' if int(ep['port']) == 443 else 'http'}://{ep['host']}:{ep['port']}"
        rel_p = "/" + (path or "/").lstrip("/")
        full_url = base.rstrip("/") + rel_p
        ssrf_err = _check_metadata_ssrf(full_url)
        if ssrf_err:
            return {"ok": False, "error": ssrf_err}

        vault = get_vault_manager(store=self.store)
        req_headers: Dict[str, str] = {"User-Agent": "companion-os/4.1", **(headers or {})}
        extra_secrets: List[str] = []
        if ep["credential_ref"] and ep["auth_mode"] != "none":
            ok, secret_val = vault.resolve_for_call(ep["credential_ref"], f"endpoint:{ep['endpoint_id']}")
            if not ok:
                return {"ok": False, "error": secret_val}
            extra_secrets.append(secret_val)
            if ep["auth_mode"] == "bearer_token":
                req_headers["Authorization"] = f"Bearer {secret_val}"
            elif ep["auth_mode"] == "basic_auth":
                raw_pair = secret_val if ":" in secret_val else f"{ep.get('username') or 'api'}:{secret_val}"
                b64 = base64.b64encode(raw_pair.encode("utf-8")).decode("ascii")
                extra_secrets.append(b64)
                req_headers["Authorization"] = f"Basic {b64}"
            elif ep["auth_mode"] == "api_key_header":
                req_headers[ep.get("auth_header") or "X-API-Key"] = secret_val

        data_bytes = None
        if json_body is not None:
            req_headers.setdefault("Content-Type", "application/json")
            data_bytes = json.dumps(json_body).encode("utf-8")
        ctx = None if ep.get("verify_tls", True) else ssl._create_unverified_context()
        try:
            req = urllib.request.Request(full_url, data=data_bytes, method=verb, headers=req_headers)
            with urlopen_safe(req, timeout=max(2.0, float(timeout)), context=ctx, allow_private_lan=True, allow_redirects=False) as resp:
                raw_body = resp.read(524_288).decode("utf-8", errors="replace")
                status_code = int(resp.status)
            scrubbed = vault.scrub_output(raw_body, extra_secrets=extra_secrets)
            return {"ok": 200 <= status_code < 400, "status_code": status_code, "body": scrubbed[: settings.out_limit]}
        except urllib.error.HTTPError as exc:
            ebody = exc.read(65_536).decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
            clean_err = vault.scrub_output(ebody, extra_secrets=extra_secrets)
            return {"ok": False, "status_code": int(exc.code), "error": f"HTTP {exc.code}: {clean_err[:300]}"}
        except Exception as exc:
            return {"ok": False, "error": vault.scrub_output(f"{type(exc).__name__}: {exc}", extra_secrets=extra_secrets)}

    def inspect_host(self, endpoint_id: str, actor: str = "user", confirmed: bool = False) -> Dict[str, Any]:
        """Collect OS, CPU, RAM, and GPU/VRAM hardware telemetry for an `ssh` or `winrm` endpoint."""
        ep = self.get_endpoint(endpoint_id)
        if not ep:
            return {"ok": False, "error": f"Endpoint `{endpoint_id}` not found."}
        if ep["protocol"] == "ssh":
            probe_cmd = (
                "uname -srm; hostname; nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null; "
                "free -h 2>/dev/null | awk '/Mem:/ {print $2}'; "
                "(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null "
                "|| lspci 2>/dev/null | grep -iE 'vga|3d|display' | sed 's/^[^:]*: //') || echo no-gpu"
            )
            res = self.ssh_exec(ep["endpoint_id"], probe_cmd, actor=actor, confirmed=confirmed, _internal_readonly=True)
            if not res.get("ok") or "'uname'" in str(res.get("output") or "").lower():
                win_ssh_cmd = 'powershell -NoProfile -Command "$o=Get-CimInstance Win32_OperatingSystem;$g=(Get-CimInstance Win32_VideoController|Select-Object -ExpandProperty Name)-join \'; \';@{os_kernel=$o.Caption;hostname=$env:COMPUTERNAME;cpu_cores=$env:NUMBER_OF_PROCESSORS;ram_total=([string][math]::Round($o.TotalVisibleMemorySize/1MB,1)+\'Gi\');gpu_summary=$g}|ConvertTo-Json -Compress"'
                wres = self.ssh_exec(ep["endpoint_id"], win_ssh_cmd, actor=actor, confirmed=confirmed, _internal_readonly=True)
                if wres.get("ok"):
                    try:
                        prof = json.loads(str(wres.get("output") or "{}"))
                        self._record_probe(ep["endpoint_id"], "online (profiled)", ep.get("last_probe_ms") or 0.0, host_profile=prof)
                        return {"ok": True, "endpoint_id": ep["endpoint_id"], "host_profile": prof, "summary": f"Profiled `{ep['endpoint_id']}`: {prof}"}
                    except Exception:
                        pass
                return res
            lns = [ln.strip() for ln in str(res.get("output") or "").splitlines() if ln.strip()]
            gpu_raw = "; ".join(lns[4:]).replace(", [N/A]", " (Unified Memory)") if len(lns) > 4 else "none"
            prof = {
                "os_kernel": lns[0] if len(lns) > 0 else "", "hostname": lns[1] if len(lns) > 1 else ep["host"],
                "cpu_cores": lns[2] if len(lns) > 2 else "", "ram_total": lns[3] if len(lns) > 3 else "",
                "gpu_summary": gpu_raw or "none",
            }
            self._record_probe(ep["endpoint_id"], "online (profiled)", ep.get("last_probe_ms") or 0.0, host_profile=prof)
            return {"ok": True, "endpoint_id": ep["endpoint_id"], "host_profile": prof, "summary": f"Profiled `{ep['endpoint_id']}`: {prof}"}
        if ep["protocol"] == "winrm":
            ps_probe = (
                "$os = Get-CimInstance Win32_OperatingSystem; $gpu = (Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name) -join '; '; "
                "@{hostname=$env:COMPUTERNAME; os=$os.Caption; ram_gb=[math]::Round($os.TotalVisibleMemorySize/1MB,1); gpu_summary=$gpu} | ConvertTo-Json -Compress"
            )
            res = self.winrm_exec(ep["endpoint_id"], ps_probe, actor=actor, confirmed=confirmed, _internal_readonly=True)
            if not res.get("ok"):
                return res
            try:
                prof = json.loads(str(res.get("output") or "{}"))
            except Exception:
                prof = {"raw": str(res.get("output") or "")[:300]}
            self._record_probe(ep["endpoint_id"], "online (profiled)", ep.get("last_probe_ms") or 0.0, host_profile=prof)
            return {"ok": True, "endpoint_id": ep["endpoint_id"], "host_profile": prof, "summary": f"Profiled Windows host `{ep['endpoint_id']}`: {prof}"}
        return self.test_endpoint(ep["endpoint_id"])

    def format_status(self) -> str:
        items = self.list_endpoints()
        lines = ["=== Universal Endpoint Registry (`ssh`, `winrm`, `http_api`) ==="]
        if not items:
            lines.append("  (No remote endpoints registered yet.)")
            return "\n".join(lines)
        for ep in items:
            cred = ep["credential_ref"] or "none"
            gpu = f" | gpu={ep['host_profile'].get('gpu_summary')}" if ep.get("host_profile", {}).get("gpu_summary") else ""
            addr = ep["host"] if "://" in ep["host"] else f"{ep['host']}:{ep['port']}"
            lines.append(
                f"  * `{ep['endpoint_id']}` ({ep['label']}) [{ep['protocol'].upper()} | {addr} | auth={ep['auth_mode']}:{cred} | {ep['last_probe_status']}]{gpu}"
            )
        return "\n".join(lines)


def get_endpoint_manager(store: Optional[MemoryStore] = None) -> EndpointManager:
    return EndpointManager(store=store)


# ---------------------------------------------------------------------------
# 2. Persistent `stdio` MCP Session Pool & Federated MCP Hub (`MCPHub`)
# ---------------------------------------------------------------------------
MCP_PROTOCOL_VERSION = "2025-03-26"

MCP_DDL: Tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS mcp_servers (server_id TEXT PRIMARY KEY, label TEXT NOT NULL, transport TEXT NOT NULL, command TEXT DEFAULT '', args_json TEXT DEFAULT '[]', url TEXT DEFAULT '', credential_ref TEXT DEFAULT '', allowed_tools_json TEXT DEFAULT '[]', discovered_tools_json TEXT DEFAULT '[]', tools_fingerprint TEXT DEFAULT '', approved_fingerprint TEXT DEFAULT '', session_id TEXT DEFAULT '', enabled INTEGER DEFAULT 1, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS federated_partners (partner_id TEXT PRIMARY KEY, label TEXT NOT NULL, url TEXT NOT NULL, credential_ref TEXT DEFAULT '', enabled INTEGER DEFAULT 1, online INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0.0, peer_profile_json TEXT DEFAULT '{}', health_json TEXT DEFAULT '{}', last_seen_at TEXT DEFAULT '', updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
)

HOSTED_MCP_SAFE_TOOLS: Tuple[Dict[str, Any], ...] = (
    {"name": "get_peer_identity", "description": "Return this Companion's peer identity, OS, CPU/RAM/GPU hardware profile, and active model.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_system_health", "description": "Return Companion queue depth, trust mode, and operational health summary.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "dispatch_companion_job", "description": "Dispatch a bounded background job to this Companion governed by its local trust and safety rules.", "inputSchema": {"type": "object", "properties": {"goal": {"type": "string"}, "title": {"type": "string"}, "tier": {"type": "string"}}, "required": ["goal"]}},
    {"name": "check_job_status", "description": "Check the execution status of a previously dispatched Companion background job.", "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}},
    {"name": "get_job_result", "description": "Fetch the bounded summary envelope and any overflow Wiki path for a completed Companion job.", "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}},
)

_WIKI_READ_MCP_TOOLS: Tuple[Dict[str, Any], ...] = (
    {"name": "search_wiki", "description": "Search this Companion's read-only Knowledge Wiki Warehouse.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "read_wiki_note", "description": "Read a specific Markdown note from this Companion's Knowledge Wiki Warehouse.", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
)

_LOCAL_HW_CACHE: Optional[Dict[str, Any]] = None


class _StdioMcpSession:
    """Warm, initialized `stdio` MCP subprocess session reused across tool invocations."""

    def __init__(self, argv: List[str], env: Dict[str, str]):
        self.argv = argv
        self.lock = threading.Lock()
        self.req_seq = 10
        self.out_q: queue.Queue[Dict[str, Any]] = queue.Queue()
        self.err_lines: List[str] = []
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._initialized = False

    def _read_stdout(self) -> None:
        if not self.proc.stdout:
            return
        with contextlib.suppress(Exception):
            for raw_ln in self.proc.stdout:
                s = raw_ln.strip()
                if s.startswith("{"):
                    with contextlib.suppress(Exception):
                        obj = json.loads(s)
                        if isinstance(obj, dict):
                            self.out_q.put(obj)

    def _read_stderr(self) -> None:
        if not self.proc.stderr:
            return
        with contextlib.suppress(Exception):
            for raw_ln in self.proc.stderr:
                if len(self.err_lines) < 30:
                    self.err_lines.append(raw_ln.rstrip()[:300])

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def ensure_initialized(self, timeout: float = 10.0) -> None:
        if self._initialized and self.alive:
            return
        init_req = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "companion", "version": __version__}}}
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(init_req) + "\n" + json.dumps(notif) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            try:
                msg = self.out_q.get(timeout=0.25)
                if msg.get("id") == 0:
                    self._initialized = True
                    return
            except queue.Empty:
                if not self.alive:
                    break
        self._initialized = self.alive

    def call(self, rpc_req: Dict[str, Any], timeout: float = 20.0) -> Tuple[bool, Dict[str, Any], str]:
        with self.lock:
            if not self.alive:
                return False, {}, "stdio MCP subprocess exited"
            try:
                self.ensure_initialized(timeout=min(10.0, timeout))
                self.req_seq += 1
                req_id = rpc_req.get("id") if rpc_req.get("id") is not None else self.req_seq
                sent = {**rpc_req, "id": req_id}
                assert self.proc.stdin is not None
                self.proc.stdin.write(json.dumps(sent) + "\n")
                self.proc.stdin.flush()
                deadline = time.monotonic() + max(1.0, float(timeout))
                last_obj: Optional[Dict[str, Any]] = None
                while time.monotonic() < deadline:
                    try:
                        obj = self.out_q.get(timeout=0.2)
                        last_obj = obj
                        if obj.get("id") == req_id:
                            return True, obj, ""
                    except queue.Empty:
                        if not self.alive:
                            if last_obj is not None and rpc_req.get("id") is None:
                                return True, last_obj, ""
                            err_tail = "\n".join(self.err_lines[-5:]) or "Missing matching JSON-RPC response ID in stdio MCP output"
                            return False, {}, err_tail
                return False, {}, "Timed out waiting for stdio MCP response"
            except Exception as exc:
                return False, {}, f"MCP stdio error: {type(exc).__name__}: {exc}"

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if self.proc.stdin:
                self.proc.stdin.close()
        with contextlib.suppress(Exception):
            self.proc.terminate()
            self.proc.wait(timeout=0.5)
        with contextlib.suppress(Exception):
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=0.5)
        for pipe in (self.proc.stdout, self.proc.stderr):
            with contextlib.suppress(Exception):
                if pipe:
                    pipe.close()


_STDIO_POOL_LOCK = threading.Lock()
_STDIO_SESSIONS: Dict[Tuple[str, str, Tuple[str, ...], str], _StdioMcpSession] = {}


def close_all_stdio_mcp_sessions(server_id: str = "") -> None:
    with _STDIO_POOL_LOCK:
        keys = [k for k in list(_STDIO_SESSIONS) if not server_id or k[0] == server_id]
        sessions = [_STDIO_SESSIONS.pop(k) for k in keys]
    for sess in sessions:
        sess.close()


atexit.register(close_all_stdio_mcp_sessions)


def _slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", (raw or "").strip().lower()).strip("-")[:64]


def _compute_tools_fingerprint(tools: List[Dict[str, Any]]) -> str:
    canonical = json.dumps([(str(t.get("name") or ""), str(t.get("description") or ""), t.get("inputSchema") or {}) for t in sorted(tools, key=lambda x: str(x.get("name") or ""))], sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _detect_local_hardware() -> Dict[str, Any]:
    global _LOCAL_HW_CACHE
    if _LOCAL_HW_CACHE is not None:
        return dict(_LOCAL_HW_CACHE)
    ram_gb = 0.0
    if os.name == "nt":
        class _MemStat(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        stat = _MemStat(dwLength=ctypes.sizeof(_MemStat))
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
            ram_gb = round(stat.ullTotalPhys / (1024 ** 3), 1)
    elif os.path.exists("/proc/meminfo"):
        for ln in open("/proc/meminfo", encoding="utf-8", errors="ignore"):
            if ln.startswith("MemTotal:"):
                ram_gb = round(int(ln.split()[1]) / (1024 ** 2), 1)
                break
    gpu_summary = "cpu-only"
    smi, lspci = shutil.which("nvidia-smi"), shutil.which("lspci")
    if smi:
        try:
            proc = subprocess.run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if proc.returncode == 0 and proc.stdout.strip():
                gpu_summary = "; ".join(ln.strip().replace(", [N/A]", " (Unified Memory)") for ln in proc.stdout.splitlines() if ln.strip())[:140]
        except Exception:
            pass
    elif lspci:
        try:
            proc = subprocess.run([lspci], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2)
            devs = [ln.split(": ", 1)[1].strip() for ln in proc.stdout.splitlines() if re.search(r"VGA|3D|Display", ln, re.I) and ": " in ln]
            if devs:
                gpu_summary = "; ".join(devs)[:140]
        except Exception:
            pass
    _LOCAL_HW_CACHE = {
        "hostname": socket.gethostname(),
        "os_platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "cpu_count": os.cpu_count() or 1,
        "ram_gb": ram_gb,
        "gpu_summary": gpu_summary,
    }
    return dict(_LOCAL_HW_CACHE)


def build_local_peer_identity(store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """Build this Companion's live Peer Identity & Host Profile for Federated Partner handshakes."""
    mem, hw = store or get_memory_store(), _detect_local_hardware()
    prof = mem.get_user_profile()
    return {
        "companion_name": prof.get("companion_name") or settings.companion_name or hw["hostname"],
        "version": __version__, **hw, "active_project_id": settings.get_workspace().project_id,
        "fast_model": settings.resolve_tier("fast").model, "reasoning_model": settings.resolve_tier("reasoning").model,
        "trust_mode": get_trust_mode(store=mem), "wiki_read_enabled": prof.get("hosted_mcp_allow_wiki_read", "off") == "on",
    }


def _parse_http_mcp_body(raw_text: str, expected_id: Any = None) -> Dict[str, Any]:
    txt = (raw_text or "").strip()
    if not txt or txt.startswith("{"):
        return json.loads(txt) if txt else {}
    last_json: Dict[str, Any] = {}
    for ln in txt.splitlines():
        if ln.startswith("data:") and ln[5:].strip().startswith("{"):
            obj = json.loads(ln[5:].strip())
            last_json = obj
            if expected_id is not None and obj.get("id") == expected_id:
                return obj
    return last_json


class MCPHub:
    """Unified Outbound MCP Client, Inbound Hosted MCP 'Order Counter', and Partner-Aware Federation Hub."""

    def __init__(
        self,
        store: Optional[MemoryStore] = None,
        transport_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]] = None,
        partner_rpc_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any], str], Dict[str, Any]]] = None,
        chat_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    ):
        self._store, self._transport_fn, self._partner_rpc_fn, self._chat_fn = store, transport_fn, partner_rpc_fn, chat_fn

    @property
    def store(self) -> MemoryStore:
        mem = self._store or get_memory_store()
        mem.ensure_schema("mcp_hub", lambda m: [m.conn.execute(ddl) for ddl in MCP_DDL] and None)
        return mem

    def _validate_mcp_server_input(self, server_id: str, label: str, transport: str, command: str, url: str) -> Tuple[str, str, Optional[str]]:
        sid, tr = _slug(server_id or label), (transport or "").strip().lower()
        if not sid or tr not in ("stdio", "http"):
            return sid, tr, "`server_id` and `transport` (`stdio` or `http`) are required."
        if tr == "stdio" and not (command or "").strip():
            return sid, tr, "`command` is required for `stdio` MCP servers."
        if tr == "http":
            if not (url or "").strip().startswith(("http://", "https://")):
                return sid, tr, "Valid `http://` or `https://` `url` is required for `http` MCP servers."
            if ssrf_err := check_url_ssrf(url, allow_private_lan=True):
                return sid, tr, ssrf_err
        return sid, tr, None

    def register_server(
        self, server_id: str, transport: str, label: str = "", command: str = "",
        args: Optional[List[str]] = None, url: str = "", credential_ref: str = "",
        allowed_tools: Optional[List[str]] = None, enabled: bool = True, actor: str = "user", confirmed: bool = False,
    ) -> Dict[str, Any]:
        """Register a `stdio` or `http` outbound MCP server using a symbolic Vault `credential_ref`."""
        if (actor.strip().lower() not in ("user", "cli", "web") or is_in_agent_tool_turn()) and not confirmed:
            return {"ok": False, "error": "MCP GUARD REJECTED: Only the user can register MCP servers directly; call `propose_server` to stage an approval request."}
        sid, tr, v_err = self._validate_mcp_server_input(server_id, label, transport, command, url)
        if v_err:
            return {"ok": False, "error": v_err}
        close_all_stdio_mcp_sessions(sid)
        cred_slug = normalize_secret_name(credential_ref) if credential_ref else ""
        if cred_slug:
            get_vault_manager(store=self.store).declare_placeholder(cred_slug, cred_type="bearer_token", description=f"Credential for MCP server `{sid}`", allow_list=[f"mcp:{sid}"], actor=actor)
        with self.store._lock:
            self.store.conn.execute(
                "INSERT INTO mcp_servers (server_id, label, transport, command, args_json, url, credential_ref, allowed_tools_json, enabled, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(server_id) DO UPDATE SET label=excluded.label, transport=excluded.transport, command=excluded.command, args_json=excluded.args_json, url=excluded.url, credential_ref=excluded.credential_ref, allowed_tools_json=excluded.allowed_tools_json, enabled=excluded.enabled, updated_at=CURRENT_TIMESTAMP",
                (sid, (label.strip() or sid)[:100], tr, command.strip(), json.dumps(args or []), url.strip(), cred_slug, json.dumps(allowed_tools or []), 1 if enabled else 0),
            )
            self.store.conn.commit()
        return {"ok": True, "server": self.get_server(sid), "summary": f"Registered `{tr}` MCP server `{sid}`."}

    def propose_server(
        self, server_id: str, transport: str, label: str = "", command: str = "",
        args: Optional[List[str]] = None, url: str = "", credential_ref: str = "",
        allowed_tools: Optional[List[str]] = None, actor: str = "companion",
    ) -> Dict[str, Any]:
        """Stage a discovered `stdio` or `http` MCP server + symbolic Vault placeholder into `ApprovalQueue` (`actor='companion'` safe)."""
        sid, tr, v_err = self._validate_mcp_server_input(server_id, label, transport, command, url)
        if v_err:
            return {"ok": False, "error": v_err}
        cred_slug = normalize_secret_name(credential_ref) if credential_ref else ""
        if cred_slug:
            get_vault_manager(store=self.store).declare_placeholder(cred_slug, cred_type="bearer_token", description=f"Proposed credential for MCP server `{sid}`", allow_list=[f"mcp:{sid}"], actor=actor)
        payload = {"server_id": sid, "transport": tr, "label": label.strip() or sid, "command": command.strip(), "args": list(args or []), "url": url.strip(), "credential_ref": cred_slug, "allowed_tools": list(allowed_tools or [])}
        _allowed, apr = get_approval_queue(self.store).check_or_enqueue("mcp_server", "register_mcp_server", sid, f"Register `{tr}` MCP server `{label.strip() or sid}` (`{url.strip() or command.strip()}`, credential_ref=`{cred_slug or 'none'}`).", payload=payload, requester=actor, confirmed=False, always_require_in_autonomous=True)
        return {"ok": True, "awaiting_approval": True, "approval_id": apr["approval_id"], "server_id": sid, "credential_ref": cred_slug, "summary": f"Staged `{tr}` MCP server `{sid}` for 1-click user approval (`{apr['approval_id']}`)."}

    def get_server(self, server_id: str) -> Optional[Dict[str, Any]]:
        cols = ["server_id", "label", "transport", "command", "args_json", "url", "credential_ref", "allowed_tools_json", "discovered_tools_json", "tools_fingerprint", "approved_fingerprint", "session_id", "enabled", "updated_at"]
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM mcp_servers WHERE server_id = ?", (_slug(server_id),))
        if not rows:
            return None
        r = rows[0]
        r["args"] = json.loads(r.pop("args_json") or "[]")
        r["allowed_tools"] = json.loads(r.pop("allowed_tools_json") or "[]")
        r["discovered_tools"] = json.loads(r.pop("discovered_tools_json") or "[]")
        r["enabled"] = bool(r["enabled"])
        r["is_approved"] = bool(r["tools_fingerprint"] and r["tools_fingerprint"] == r["approved_fingerprint"])
        return r

    def list_servers(self) -> List[Dict[str, Any]]:
        ids = [str(r[0]) for r in self.store._fetch_rows("SELECT server_id FROM mcp_servers ORDER BY server_id ASC")]
        return [s for sid in ids if (s := self.get_server(sid)) is not None]

    def remove_server(self, server_id: str, actor: str = "user") -> Dict[str, Any]:
        if actor.strip().lower() not in ("user", "cli", "web") or is_in_agent_tool_turn():
            return {"ok": False, "error": "MCP GUARD REJECTED: Only the user can remove outbound MCP servers."}
        sid = _slug(server_id)
        close_all_stdio_mcp_sessions(sid)
        with self.store._lock:
            cur = self.store.conn.execute("DELETE FROM mcp_servers WHERE server_id = ?", (sid,))
            self.store.conn.commit()
        return {"ok": bool(cur.rowcount), "server_id": sid, "summary": f"Removed MCP server `{sid}`."} if cur.rowcount else {"ok": False, "error": f"MCP server `{sid}` not found."}

    def _send_rpc(self, srv: Dict[str, Any], rpc_req: Dict[str, Any], timeout: float = 20.0) -> Tuple[bool, Dict[str, Any], str]:
        if self._transport_fn is not None:
            return True, self._transport_fn(srv, rpc_req), ""
        vault = get_vault_manager(store=self.store)
        secret_val = ""
        if srv["credential_ref"]:
            ok, val_or_err = vault.resolve_for_call(srv["credential_ref"], f"mcp:{srv['server_id']}")
            if not ok:
                return False, {}, val_or_err
            secret_val = val_or_err

        if srv["transport"] == "http":
            ssrf_err = check_url_ssrf(srv["url"], allow_private_lan=True)
            if ssrf_err:
                return False, {}, ssrf_err
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            if secret_val:
                headers["Authorization"] = f"Bearer {secret_val}"
            if srv.get("session_id"):
                headers["Mcp-Session-Id"] = str(srv["session_id"])
            try:
                req = urllib.request.Request(srv["url"], data=json.dumps(rpc_req).encode("utf-8"), method="POST", headers=headers)
                with urlopen_safe(req, timeout=timeout, allow_private_lan=True, allow_redirects=False) as resp:
                    new_sid = resp.headers.get("Mcp-Session-Id") or ""
                    if new_sid and new_sid != srv.get("session_id"):
                        with self.store._lock:
                            self.store.conn.execute("UPDATE mcp_servers SET session_id = ? WHERE server_id = ?", (new_sid[:120], srv["server_id"]))
                            self.store.conn.commit()
                    raw = resp.read(524_288).decode("utf-8", errors="replace")
                return True, _parse_http_mcp_body(raw, expected_id=rpc_req.get("id")), ""
            except Exception as exc:
                return False, {}, vault.scrub_output(f"MCP HTTP error: {type(exc).__name__}: {exc}", extra_secrets=[secret_val] if secret_val else None)

        env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "HOME": os.environ.get("HOME", os.environ.get("USERPROFILE", ""))}
        for win_k in ("PATHEXT", "COMSPEC", "SYSTEMDRIVE", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
            if os.environ.get(win_k):
                env[win_k] = os.environ[win_k]
        if secret_val:
            env["MCP_AUTH_TOKEN"] = secret_val
        raw_cmd, raw_args = str(srv["command"]).strip(), [str(x) for x in srv.get("args", [])]
        argv = [t.strip('"') for t in shlex.split(raw_cmd, posix=(os.name != "nt"))] if (not raw_args and (" " in raw_cmd or '"' in raw_cmd)) else [raw_cmd, *raw_args]
        sec_hash = hashlib.sha256(secret_val.encode("utf-8")).hexdigest()[:12] if secret_val else ""
        pool_key = (str(srv["server_id"]), raw_cmd, tuple(raw_args), sec_hash)

        for attempt in range(2):
            try:
                with _STDIO_POOL_LOCK:
                    sess = _STDIO_SESSIONS.get(pool_key)
                    reused = sess is not None and sess.alive
                    if sess is None or not sess.alive:
                        if sess is not None:
                            sess.close()
                        sess = _StdioMcpSession(argv, env)
                        _STDIO_SESSIONS[pool_key] = sess
                call_timeout = min(3.0, timeout) if (attempt == 0 and reused) else timeout
                ok_s, resp_s, err_s = sess.call(rpc_req, timeout=call_timeout)
                if ok_s:
                    return True, resp_s, ""
                # One-shot or wedged pooled stdio servers: close dead/stalled session and retry once cleanly
                with _STDIO_POOL_LOCK:
                    dead = _STDIO_SESSIONS.pop(pool_key, None)
                if dead is not None:
                    dead.close()
                if attempt == 0 and (reused or "exited" in err_s):
                    continue
                return False, {}, vault.scrub_output(err_s[:300], extra_secrets=[secret_val] if secret_val else None)
            except Exception as exc:
                with _STDIO_POOL_LOCK:
                    dead = _STDIO_SESSIONS.pop(pool_key, None)
                if dead is not None:
                    dead.close()
                return False, {}, vault.scrub_output(f"MCP stdio error: {type(exc).__name__}: {exc}", extra_secrets=[secret_val] if secret_val else None)
        return False, {}, "MCP stdio call failed after session retry."

    def discover_server(self, server_id: str, actor: str = "user") -> Dict[str, Any]:
        """Run `initialize` + `tools/list`, apply `allowed_tools` narrowing, and record SHA-256 `tools_fingerprint`."""
        srv = self.get_server(server_id)
        if not srv:
            return {"ok": False, "error": f"MCP server `{server_id}` not found."}
        if srv["transport"] == "http":
            self._send_rpc(srv, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "companion", "version": __version__}}})
            srv = self.get_server(server_id) or srv
        ok, resp, err = self._send_rpc(srv, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        if not ok:
            return {"ok": False, "error": err}
        raw_tools = list((resp.get("result") or {}).get("tools") or [])
        allow_set = {str(x) for x in srv.get("allowed_tools", []) if str(x)}
        filtered = [t for t in raw_tools if not allow_set or str(t.get("name")) in allow_set]
        fp = _compute_tools_fingerprint(filtered)
        is_human = actor.strip().lower() in ("user", "cli", "web") and not is_in_agent_tool_turn()
        approved_fp = fp if is_human else srv.get("approved_fingerprint", "")
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE mcp_servers SET discovered_tools_json = ?, tools_fingerprint = ?, approved_fingerprint = ?, updated_at = CURRENT_TIMESTAMP WHERE server_id = ?",
                (json.dumps(filtered), fp, approved_fp, srv["server_id"]),
            )
            self.store.conn.commit()
        return {"ok": True, "server_id": srv["server_id"], "tools": filtered, "tools_fingerprint": fp, "fingerprint": fp, "is_approved": fp == approved_fp, "summary": f"Discovered {len(filtered)} tool(s) on `{srv['server_id']}` (fingerprint=`{fp}`, approved={fp == approved_fp})."}

    def approve_server(self, server_id: str, actor: str = "user", expected_fingerprint: str = "") -> Dict[str, Any]:
        if actor.strip().lower() not in ("user", "cli", "web") or is_in_agent_tool_turn():
            return {"ok": False, "error": "MCP GUARD REJECTED: Only the user can approve an MCP server's tool fingerprint."}
        srv = self.get_server(server_id)
        if not srv or not srv["tools_fingerprint"]:
            return {"ok": False, "error": f"Run `discover` on MCP server `{server_id}` before approving."}
        if expected_fingerprint and expected_fingerprint.strip() != srv["tools_fingerprint"]:
            return {"ok": False, "error": f"MCP FINGERPRINT MISMATCH: expected `{expected_fingerprint.strip()}`, current discovered fingerprint is `{srv['tools_fingerprint']}`."}
        with self.store._lock:
            self.store.conn.execute("UPDATE mcp_servers SET approved_fingerprint = ?, updated_at = CURRENT_TIMESTAMP WHERE server_id = ?", (srv["tools_fingerprint"], srv["server_id"]))
            self.store.conn.commit()
        return {"ok": True, "server_id": srv["server_id"], "approved_fingerprint": srv["tools_fingerprint"], "summary": f"Approved MCP server `{srv['server_id']}` tool fingerprint `{srv['tools_fingerprint']}`."}

    def list_approved_openai_tools(self) -> List[Dict[str, Any]]:
        """Expose enabled + fingerprint-approved MCP server tools natively as `mcp__<server_id>__<tool_name>`."""
        schemas: List[Dict[str, Any]] = []
        for srv in self.list_servers():
            if not srv.get("enabled") or not srv.get("is_approved"):
                continue
            sid = re.sub(r"[^a-zA-Z0-9_-]", "_", str(srv["server_id"]))
            for t in srv.get("discovered_tools") or []:
                tname = str(t.get("name") or "").strip()
                if not tname:
                    continue
                safe_t = re.sub(r"[^a-zA-Z0-9_-]", "_", tname)
                in_schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {"type": "object", "properties": {}}
                params = {"type": "object", "properties": dict(in_schema.get("properties") or {}), **({"required": list(in_schema["required"])} if isinstance(in_schema.get("required"), list) else {})}
                schemas.append({"type": "function", "function": {"name": f"mcp__{sid}__{safe_t}", "description": f"[MCP `{srv['server_id']}`] {t.get('description') or tname}", "parameters": params}})
        return schemas

    def call_mcp_tool(
        self, server_id: str, tool_name: str, arguments: Optional[Dict[str, Any]] = None,
        actor: str = "companion", confirmed: bool = False,
    ) -> Dict[str, Any]:
        """Invoke a tool on an outbound MCP server enforcing `Discovery != Authorization` fingerprint approval."""
        srv = self.get_server(server_id)
        if not srv or not srv["enabled"]:
            return {"ok": False, "error": f"Enabled MCP server `{server_id}` not found."}
        if not srv["tools_fingerprint"]:
            disc = self.discover_server(srv["server_id"], actor=actor)
            if not disc.get("ok"):
                return disc
            srv = self.get_server(server_id) or srv

        by_name = {str(t.get("name")): t for t in srv.get("discovered_tools", [])}
        if tool_name not in by_name:
            return {"ok": False, "error": f"Tool `{tool_name}` is not in the discovered/allowed tool list for `{srv['server_id']}` ({sorted(by_name)})."}

        is_human = actor.strip().lower() in ("user", "cli", "web") and not is_in_agent_tool_turn()
        if not is_human and srv["approved_fingerprint"] != srv["tools_fingerprint"]:
            allowed, apr = get_approval_queue(self.store).check_or_enqueue(
                category="mcp_server", action_name="approve_mcp_tools", target=srv["server_id"],
                summary=f"Approve MCP server `{srv['label']}` (`{srv['server_id']}`) tool list fingerprint `{srv['tools_fingerprint']}` ({sorted(by_name)}).",
                payload={"server_id": srv["server_id"], "fingerprint": srv["tools_fingerprint"]},
                requester=actor, confirmed=confirmed and is_human, always_require_in_autonomous=True,
            )
            if not allowed:
                return {"ok": False, "awaiting_approval": True, "approval_id": apr["approval_id"], "error": f"MCP SERVER APPROVAL REQUIRED (`{apr['approval_id']}`): Server `{srv['server_id']}` tool fingerprint `{srv['tools_fingerprint']}` awaits approval."}
            with self.store._lock:
                self.store.conn.execute("UPDATE mcp_servers SET approved_fingerprint = ? WHERE server_id = ?", (srv["tools_fingerprint"], srv["server_id"]))
                self.store.conn.commit()

        t_spec = by_name[tool_name]
        destructive = bool((t_spec.get("annotations") or {}).get("destructiveHint") or t_spec.get("destructiveHint"))
        if not is_human and (get_trust_mode(store=self.store) == "strict" or destructive):
            allowed_t, apr_t = get_approval_queue(self.store).check_or_enqueue(
                category="mcp_tool", action_name="call_mcp_tool", target=f"{srv['server_id']}:{tool_name}",
                summary=f"Invoke MCP tool `{srv['server_id']}.{tool_name}` ({'destructive' if destructive else 'strict trust'}).",
                payload={"server_id": srv["server_id"], "tool_name": tool_name, "arguments": arguments or {}},
                requester=actor, confirmed=confirmed and is_human, always_require_in_autonomous=destructive,
            )
            if not allowed_t:
                return {"ok": False, "awaiting_approval": True, "approval_id": apr_t["approval_id"], "error": f"MCP TOOL APPROVAL REQUIRED (`{apr_t['approval_id']}`): `{srv['server_id']}.{tool_name}` awaits approval."}

        ok, resp, err = self._send_rpc(srv, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments or {}}})
        if not ok:
            return {"ok": False, "error": err}
        if "error" in resp:
            return {"ok": False, "error": f"Remote MCP error: {resp['error']}"}
        res_obj = resp.get("result") or {}
        blocks = res_obj.get("content") or []
        txt = "\n".join(str(b.get("text") or b) for b in blocks) if isinstance(blocks, list) and blocks else json.dumps(res_obj)
        clean_out = get_vault_manager(store=self.store).scrub_output(txt)[: settings.out_limit]
        return {"ok": not bool(res_obj.get("isError")), "output": clean_out, "raw_result": res_obj}

    def configure_hosted_mcp(
        self, enabled: bool, token_ref: str = "hosted-mcp-token",
        allow_wiki_read: bool = False, actor: str = "user", confirmed: bool = False,
    ) -> Dict[str, Any]:
        """Enable or disable Inbound Hosted MCP ('The Order Counter' — off by default, protected by Vault token)."""
        tok_slug = normalize_secret_name(token_ref or "hosted-mcp-token") or "hosted-mcp-token"
        is_human = actor.strip().lower() in ("user", "cli", "web") and not is_in_agent_tool_turn()
        if enabled and not is_human:
            allowed, apr = get_approval_queue(self.store).check_or_enqueue(
                category="mcp_server", action_name="enable_hosted_mcp", target="hosted_mcp",
                summary=f"Enable inbound Hosted MCP 'Order Counter' protected by Vault token `{tok_slug}`.",
                payload={"token_ref": tok_slug, "allow_wiki_read": allow_wiki_read},
                requester=actor, confirmed=confirmed and is_human, always_require_in_autonomous=True,
            )
            if not allowed:
                return {"ok": False, "awaiting_approval": True, "approval_id": apr["approval_id"], "error": f"Approval `{apr['approval_id']}` required to enable inbound Hosted MCP."}
        get_vault_manager(store=self.store).declare_placeholder(tok_slug, cred_type="bearer_token", description="Inbound Hosted MCP Order Counter token", allow_list=["hosted_mcp"], actor=actor)
        self.store.set_user_profile("hosted_mcp_enabled", "on" if enabled else "off")
        self.store.set_user_profile("hosted_mcp_token_ref", tok_slug)
        self.store.set_user_profile("hosted_mcp_allow_wiki_read", "on" if allow_wiki_read else "off")
        return {"ok": True, "enabled": enabled, "token_ref": tok_slug, "hosted_mcp_token_ref": tok_slug, "allow_wiki_read": allow_wiki_read, "summary": f"Hosted MCP Order Counter={'ON' if enabled else 'OFF'} (token_ref=`{tok_slug}`, wiki_read={allow_wiki_read})."}

    def verify_hosted_mcp_auth(self, auth_header_or_token: str) -> Tuple[bool, str]:
        prof = self.store.get_user_profile()
        if prof.get("hosted_mcp_enabled", "off") != "on":
            return False, "Hosted MCP Order Counter is disabled (`hosted_mcp_enabled=off`)."
        tok_ref = prof.get("hosted_mcp_token_ref") or "hosted-mcp-token"
        ok, expected = get_vault_manager(store=self.store).resolve_for_call(tok_ref, "hosted_mcp")
        if not ok:
            return False, f"Hosted MCP token `{tok_ref}` is not configured in Vault."
        supplied = (auth_header_or_token or "").strip()
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        if not supplied or not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            return False, "Invalid or missing Hosted MCP Bearer token."
        return True, ""

    def handle_hosted_mcp_jsonrpc(
        self, payload: Dict[str, Any], auth_token: str = "", run_jobs_in_background: bool = True,
    ) -> Tuple[int, Dict[str, Any]]:
        """Process an inbound MCP JSON-RPC 2.0 request against the strictly bounded 'Order Counter' toolset."""
        from companion.jobs import get_job_manager
        from companion.wiki import get_wiki_engine

        ok_auth, auth_err = self.verify_hosted_mcp_auth(auth_token)
        req_id = payload.get("id")
        if not ok_auth:
            return (403 if "disabled" in auth_err else 401), {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": auth_err}}

        method = str(payload.get("method") or "")
        if "id" not in payload or req_id is None:
            if method.startswith("notifications/"):
                return 202, {}
        prof = self.store.get_user_profile()
        wiki_ok = prof.get("hosted_mcp_allow_wiki_read", "off") == "on"
        exposed = list(HOSTED_MCP_SAFE_TOOLS) + (list(_WIKI_READ_MCP_TOOLS) if wiki_ok else [])

        if method == "initialize":
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "companion-order-counter", "version": __version__}}}
        if method in ("notifications/initialized", "ping"):
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"tools": exposed}}
        if method != "tools/call":
            return 400, {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method `{method}` not supported."}}

        params = payload.get("params") or {}
        tname = str(params.get("name") or "")
        targs = params.get("arguments") or {}
        jm = get_job_manager(store=self.store, **({"chat_fn": self._chat_fn} if self._chat_fn else {}))
        if tname == "get_peer_identity":
            out_data: Any = build_local_peer_identity(store=self.store)
        elif tname == "get_system_health":
            jobs = jm.list_jobs(limit=30)
            out_data = {"ok": True, "trust_mode": get_trust_mode(store=self.store), "queued_or_running_jobs": sum(1 for j in jobs if j["status"] in ("queued", "running")), "waiting_approvals": len(get_approval_queue(self.store).list_requests(status="waiting"))}
        elif tname == "dispatch_companion_job":
            out_data = jm.dispatch_job(goal=str(targs.get("goal") or ""), title=str(targs.get("title") or "Federated Partner Job"), kind="mcp_inbound", starter="federated", tier=str(targs.get("tier") or "fast"), run_in_background=run_jobs_in_background)
        elif tname == "check_job_status":
            row = jm.get_job(str(targs.get("job_id") or ""))
            out_data = {"ok": bool(row), "job_id": row["job_id"], "status": row["status"], "approval_id": row.get("approval_id", "")} if row else {"ok": False, "error": "Job not found."}
        elif tname == "get_job_result":
            row = jm.get_job(str(targs.get("job_id") or ""))
            out_data = {"ok": bool(row), "job_id": row["job_id"], "status": row["status"], "envelope_summary": row.get("envelope_summary", ""), "overflow_wiki_path": row.get("overflow_wiki_path", ""), "error": row.get("error", "")} if row else {"ok": False, "error": "Job not found."}
        elif wiki_ok and tname == "search_wiki":
            out_data = {"ok": True, "hits": get_wiki_engine(store=self.store).search_notes(str(targs.get("query") or ""), limit=int(targs.get("limit") or 8))}
        elif wiki_ok and tname == "read_wiki_note":
            out_data = get_wiki_engine(store=self.store).read_note(str(targs.get("path") or ""))
        else:
            return 403, {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Tool `{tname}` is not exposed on the Hosted MCP Order Counter."}}
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(out_data)}], "isError": not bool(out_data.get("ok", True))}}

    def register_partner(self, partner_id: str, url: str, label: str = "", credential_ref: str = "", enabled: bool = True, actor: str = "user") -> Dict[str, Any]:
        """Pair with a remote Federated Companion instance URL (`http://<host>:<port>/api/mcp`) + Vault token."""
        if actor.strip().lower() not in ("user", "cli", "web") or is_in_agent_tool_turn():
            return {"ok": False, "error": "MCP GUARD REJECTED: Only the user can register or modify federated partners."}
        pid = _slug(partner_id or label)
        clean_url = (url or "").strip()
        if not pid or not clean_url.startswith(("http://", "https://")):
            return {"ok": False, "error": "`partner_id` and `http(s)://.../api/mcp` `url` are required."}
        ssrf_err = check_url_ssrf(clean_url, allow_private_lan=True)
        if ssrf_err:
            return {"ok": False, "error": ssrf_err}
        cred_slug = normalize_secret_name(credential_ref) if credential_ref else ""
        if cred_slug:
            get_vault_manager(store=self.store).declare_placeholder(
                cred_slug, cred_type="bearer_token", description=f"Federated partner token for `{pid}`", allow_list=[f"partner:{pid}"], actor=actor,
            )
        with self.store._lock:
            self.store.conn.execute(
                """INSERT INTO federated_partners (partner_id, label, url, credential_ref, enabled, updated_at)
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(partner_id) DO UPDATE SET label=excluded.label, url=excluded.url, credential_ref=excluded.credential_ref, enabled=excluded.enabled, updated_at=CURRENT_TIMESTAMP""",
                (pid, (label.strip() or pid)[:100], clean_url, cred_slug, 1 if enabled else 0),
            )
            self.store.conn.commit()
        return {"ok": True, "partner": self.get_partner(pid), "summary": f"Registered federated partner `{pid}` -> `{clean_url}`."}

    def get_partner(self, partner_id: str) -> Optional[Dict[str, Any]]:
        cols = ["partner_id", "label", "url", "credential_ref", "enabled", "online", "latency_ms", "peer_profile_json", "health_json", "last_seen_at", "updated_at"]
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM federated_partners WHERE partner_id = ?", (_slug(partner_id),))
        if not rows:
            return None
        r = rows[0]
        r["enabled"], r["online"] = bool(r["enabled"]), bool(r["online"])
        r["peer_profile"] = json.loads(r.pop("peer_profile_json") or "{}")
        r["health"] = json.loads(r.pop("health_json") or "{}")
        return r

    def list_partners(self) -> List[Dict[str, Any]]:
        ids = [str(r[0]) for r in self.store._fetch_rows("SELECT partner_id FROM federated_partners ORDER BY partner_id ASC")]
        return [p for pid in ids if (p := self.get_partner(pid)) is not None]

    def remove_partner(self, partner_id: str, actor: str = "user") -> Dict[str, Any]:
        if actor.strip().lower() not in ("user", "cli", "web") or is_in_agent_tool_turn():
            return {"ok": False, "error": "MCP GUARD REJECTED: Only the user can remove federated partners."}
        pid = _slug(partner_id)
        with self.store._lock:
            cur = self.store.conn.execute("DELETE FROM federated_partners WHERE partner_id = ?", (pid,))
            self.store.conn.commit()
        return {"ok": bool(cur.rowcount), "partner_id": pid, "summary": f"Removed federated partner `{pid}`."} if cur.rowcount else {"ok": False, "error": f"Partner `{pid}` not found."}

    def _call_partner_tool(self, partner: Dict[str, Any], tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Tuple[bool, Dict[str, Any], str]:
        vault = get_vault_manager(store=self.store)
        tok = ""
        if partner["credential_ref"]:
            ok, tok_or_err = vault.resolve_for_call(partner["credential_ref"], f"partner:{partner['partner_id']}")
            if not ok:
                return False, {}, tok_or_err
            tok = tok_or_err
        rpc_payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments or {}}}
        if self._partner_rpc_fn is not None:
            resp = self._partner_rpc_fn(partner, rpc_payload, tok)
        else:
            ssrf_err = check_url_ssrf(partner["url"], allow_private_lan=True)
            if ssrf_err:
                return False, {}, ssrf_err
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
            try:
                req = urllib.request.Request(partner["url"], data=json.dumps(rpc_payload).encode("utf-8"), method="POST", headers=headers)
                with urlopen_safe(req, timeout=12.0, allow_private_lan=True, allow_redirects=False) as http_resp:
                    resp = _parse_http_mcp_body(http_resp.read(262_144).decode("utf-8", errors="replace"), expected_id=1)
            except urllib.error.HTTPError as exc:
                return False, {}, f"Partner HTTP {exc.code}"
            except Exception as exc:
                return False, {}, vault.scrub_output(f"{type(exc).__name__}: {exc}", extra_secrets=[tok] if tok else None)
        if "error" in resp:
            return False, {}, str((resp.get("error") or {}).get("message") or resp["error"])
        blocks = (resp.get("result") or {}).get("content") or [{}]
        raw_txt = str(blocks[0].get("text") or "{}")
        return True, json.loads(raw_txt), ""

    def handshake_partner(self, partner_id: str) -> Dict[str, Any]:
        """Exchange live Peer Identity & Host Hardware Profile with a paired Federated Companion."""
        p = self.get_partner(partner_id)
        if not p:
            return {"ok": False, "error": f"Federated partner `{partner_id}` not found."}
        t0 = time.perf_counter()
        ok, peer_prof, err = self._call_partner_tool(p, "get_peer_identity", {})
        ms = round((time.perf_counter() - t0) * 1000.0, 1)
        if not ok:
            with self.store._lock:
                self.store.conn.execute("UPDATE federated_partners SET online = 0, latency_ms = ?, updated_at = CURRENT_TIMESTAMP WHERE partner_id = ?", (ms, p["partner_id"]))
                self.store.conn.commit()
            return {"ok": False, "partner_id": p["partner_id"], "online": False, "error": err}
        _, health, _ = self._call_partner_tool(p, "get_system_health", {})
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE federated_partners SET online = 1, latency_ms = ?, peer_profile_json = ?, health_json = ?, last_seen_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE partner_id = ?",
                (ms, json.dumps(peer_prof), json.dumps(health or {}), p["partner_id"]),
            )
            self.store.conn.commit()
        return {"ok": True, "partner_id": p["partner_id"], "online": True, "latency_ms": ms, "peer_profile": peer_prof, "health": health, "summary": f"Connected to partner `{peer_prof.get('companion_name', p['label'])}` on `{peer_prof.get('hostname')}` ({peer_prof.get('os_platform')}, GPU: {peer_prof.get('gpu_summary')}, {ms}ms)."}

    def delegate_to_partner(self, partner_id: str, goal: str, title: str = "", tier: str = "fast") -> Dict[str, Any]:
        p = self.get_partner(partner_id)
        if not p or not p["enabled"]:
            return {"ok": False, "error": f"Enabled federated partner `{partner_id}` not found."}
        ok, data, err = self._call_partner_tool(p, "dispatch_companion_job", {"goal": goal, "title": title or goal[:80], "tier": tier})
        return {**data, "partner_id": p["partner_id"]} if ok else {"ok": False, "error": err}

    def check_partner_job(self, partner_id: str, remote_job_id: str) -> Dict[str, Any]:
        p = self.get_partner(partner_id)
        if not p:
            return {"ok": False, "error": f"Federated partner `{partner_id}` not found."}
        ok, data, err = self._call_partner_tool(p, "get_job_result", {"job_id": remote_job_id})
        return {**data, "partner_id": p["partner_id"]} if ok else {"ok": False, "error": err}


def format_connected_partners_hud(store: Optional[MemoryStore] = None) -> str:
    """Render a compact `[CONNECTED FEDERATED PARTNERS]` prompt block so Companion actively knows its remote peers."""
    partners = MCPHub(store=store).list_partners()
    enabled = [p for p in partners if p.get("enabled")]
    if not enabled:
        return ""
    lines = ["[CONNECTED FEDERATED PARTNERS (paired remote Companion instances reachable via `mcp_manage`)]"]
    for p in enabled:
        prof = p.get("peer_profile") or {}
        state = f"ONLINE ({p['latency_ms']}ms)" if p.get("online") else "OFFLINE/UNPROBED"
        cname = prof.get("companion_name") or p["label"]
        host_str = f"{prof.get('hostname', '?')} | {prof.get('os_platform', 'unknown OS')} | GPU={prof.get('gpu_summary', 'unknown')} | RAM={prof.get('ram_gb', '?')}GB | model={prof.get('fast_model', '?')}" if prof else p["url"]
        lines.append(f"  - `{p['partner_id']}` ({cname}) [{state}]: {host_str}")
    return "\n".join(lines)


def harvest_mcp_capability_pack(
    server_id: str,
    pack_name: Optional[str] = None,
    overwrite: bool = True,
    store: Optional[MemoryStore] = None,
    root: Optional[Path] = None,
    chat_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Introspect an approved MCP server's `discovered_tools` into a `data/skills/<pack>/` Capability Pack."""
    from companion.workflows import stage_and_promote_skill

    srv = MCPHub(store=store).get_server(server_id)
    if not srv:
        return {"ok": False, "error": f"MCP server `{server_id}` not found."}
    if not srv["is_approved"] or not srv["discovered_tools"]:
        return {"ok": False, "error": f"MCP server `{server_id}` must be discovered and user-approved before harvesting a Capability Pack."}
    pack = re.sub(r"[^a-z0-9-]+", "-", (pack_name or f"mcp-{srv['server_id']}").strip().lower()).strip("-")[:48]
    tools, first_tool, dag_name = list(srv["discovered_tools"]), str(srv["discovered_tools"][0].get("name") or "ping"), f"{pack}-pipeline"
    ref_lines = [f"# MCP Tool Reference — `{srv['label']}` (`{srv['server_id']}`)", ""] + [
        f"## `{t.get('name')}`\n- **Description**: {t.get('description') or 'MCP tool.'}\n- **Input Schema**: `{json.dumps(t.get('inputSchema') or {})}`\n" for t in tools
    ]

    when_to_use = f"Use this pack whenever orchestrating `{srv['server_id']}` MCP tools ({', '.join(str(t.get('name')) for t in tools[:8])}) deterministically."
    playbook_notes = ""
    if chat_fn is not None or ("unittest" not in sys.modules and os.environ.get("COMPANION_TEST_MODE") != "1"):
        llm_call = chat_fn or chat
        try:
            with tier_context("fast"):
                resp = llm_call(
                    [
                        {"role": "system", "content": "Write concise operational guidance for an MCP Capability Pack (`when_to_use` 1-2 sentences, `best_practices` 3 bullet points). Respond ONLY with JSON: {\"when_to_use\":\"...\",\"best_practices\":[\"...\"]}"},
                        {"role": "user", "content": f"MCP Server: {srv['label']} ({srv['server_id']})\nTools: {json.dumps(tools[:12])}"},
                    ],
                    None,
                    "none",
                )
            txt, thk, _ = normalize_assistant_message((resp.get("choices") or [{}])[0].get("message") or {})
            raw_j = txt if "{" in txt else thk
            s_i, e_i = raw_j.find("{"), raw_j.rfind("}")
            if s_i != -1 and e_i > s_i:
                parsed = json.loads(raw_j[s_i : e_i + 1])
                if isinstance(parsed, dict):
                    if str(parsed.get("when_to_use") or "").strip():
                        when_to_use = str(parsed["when_to_use"]).strip()
                    if isinstance(parsed.get("best_practices"), list) and parsed["best_practices"]:
                        playbook_notes = "\n## Best Practices\n" + "\n".join(f"- {str(b).strip()}" for b in parsed["best_practices"][:4] if str(b).strip()) + "\n"
        except Exception:
            pass

    skill_md = (
        f"---\nname: {pack}\ndescription: Harvested MCP capability pack for {srv['label']} ({len(tools)} approved tool(s)).\nrequired_tools: [call_mcp, ops_manage]\n---\n\n"
        f"# {srv['label']} Capability Pack (`{pack}`)\n\n## When to Use This Skill\n{when_to_use}\n\n"
        f"## Step-by-Step Procedure\n1. Read `references/mcp-tools.md` to inspect exact JSON input schemas.\n"
        f"2. Run `ops_manage(domain='workflows', action='run_dag', params={{'dag': '{dag_name}', 'params': {{...}}}})` for deterministic multi-step execution.\n"
        f"3. Or invoke `call_mcp(server_id='{srv['server_id']}', tool_name=..., arguments=...)`.\n{playbook_notes}"
    )
    helper_py = (
        '"""Self-tested schema helper for harvested MCP capability pack."""\n\nimport json\nimport sys\n\n'
        f"SERVER_ID = {srv['server_id']!r}\nTOOLS = json.loads({json.dumps(tools)!r})\n\n\n"
        'def main() -> int:\n    if "--self-test" in sys.argv:\n        assert SERVER_ID and isinstance(TOOLS, list) and len(TOOLS) >= 1\n'
        '        print(json.dumps({"ok": True, "server_id": SERVER_ID, "tool_count": len(TOOLS)}))\n        return 0\n'
        '    print(json.dumps({"server_id": SERVER_ID, "tools": [t.get("name") for t in TOOLS]}))\n    return 0\n\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
    )
    wf_spec = {
        "name": dag_name,
        "description": f"Deterministic declarative MCP workflow for {srv['label']}.",
        "nodes": [
            {
                "id": "invoke_mcp",
                "type": "mcp_step",
                "config": {
                    "server_id": srv["server_id"],
                    "tool_name": first_tool,
                    "args": {},
                },
                "output_schema": {"ok": "bool"},
            },
            {
                "id": "verify_schema",
                "type": "run_script",
                "depends_on": ["invoke_mcp"],
                "config": {"script": "scripts/mcp_client_helper.py"},
                "output_schema": {"server_id": "str", "tools": "list"},
            },
        ],
    }
    res = stage_and_promote_skill(
        name=pack,
        skill_md=skill_md,
        scripts={"mcp_client_helper.py": helper_py},
        dags={f"{dag_name}.workflow.json": json.dumps(wf_spec, indent=2)},
        references={"mcp-tools.md": "\n".join(ref_lines)},
        overwrite=overwrite,
        root=root,
        scope="user",
    )
    hint = f" Harvested workflow `{dag_name}` (`workflows/{dag_name}.workflow.json`)." if res.get("promoted") else ""
    return {**res, "ok": bool(res.get("promoted")), "pack_name": pack, "dag_name": dag_name, "summary": str(res.get("summary") or "") + hint}


def get_mcp_hub(
    store: Optional[MemoryStore] = None,
    transport_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]] = None,
    partner_rpc_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any], str], Dict[str, Any]]] = None,
) -> MCPHub:
    return MCPHub(store=store, transport_fn=transport_fn, partner_rpc_fn=partner_rpc_fn)
