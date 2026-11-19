"""
Universal Endpoint Registry & Multi-Protocol Connector Engine (`companion/endpoints.py` — Application Layer).

Implements Pillar 3 of the Companion 3.0 Harvest Blueprint:
1. Symbolic-Only Endpoint Registry (`remote_endpoints` SQLite table) storing `credential_ref` names
   pointing to the Blind Credential Vault — never raw passwords, SSH keys, or tokens.
2. Three first-class protocol drivers with Just-In-Time Vault injection & output scrubbing:
   - `ssh`      : OpenSSH / key-file remote command execution, safe file reads, and Linux/GPU host profiling.
   - `winrm`    : Windows WinRM / PowerShell Remoting (`Invoke-Command` over WSMan `5985`/`5986`) with env-isolated creds.
   - `http_api` : Authenticated REST / Homelab HTTP client (`Bearer`, `Basic`, custom API-key headers) with metadata SSRF guard.
"""

import base64
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from companion.archive_and_approvals import get_approval_queue, get_trust_mode
from companion.config import is_in_agent_tool_turn, settings
from companion.llm_wire import check_url_ssrf, urlopen_safe
from companion.memory_store import MemoryStore, get_memory_store
from companion.safety import check_command_safety
from companion.vault import _lock_windows_owner_dacl, get_vault_manager, normalize_secret_name

__all__ = [
    "VALID_AUTH_MODES",
    "VALID_PROTOCOLS",
    "EndpointManager",
    "get_endpoint_manager",
]

VALID_PROTOCOLS = ("ssh", "winrm", "http_api")
VALID_AUTH_MODES = ("none", "ssh_key", "ssh_agent", "basic_auth", "bearer_token", "api_key_header")
_DEFAULT_PORTS = {"ssh": 22, "winrm": 5985, "http_api": 80}
_BLOCKED_META_HOSTS = {"metadata.google.internal", "metadata", "instance-data", "169.254.169.254"}
_SECRET_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASS")
_READONLY_SINGLE_CMDS = frozenset({
    "uname", "hostname", "uptime", "df", "free", "ps", "ls", "cat", "head",
    "tail", "grep", "rg", "find", "stat", "journalctl", "nvidia-smi", "ss",
})
_READONLY_TWO_WORD_CMDS = frozenset({"systemctl status", "docker ps", "docker logs", "ip addr", "ip route"})
_SAFE_SED_READ_RE = re.compile(r"^sed\s+-n\s+'1,\d+p'\s+--\s+\S+$")


def _parse_user_host_port(raw_host: str, raw_user: str, raw_port: int) -> Tuple[str, str, int]:
    """Extract optional `user@host:port` syntax when registering SSH/WinRM endpoints."""
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


def _is_readonly_remote_command(command: str, protocol: str) -> bool:
    """Strict allowlist for read-only SSH commands (`sudo`, `kill`, `sed -i`, `ufw`, `scp`, pipes/redirects require approval)."""
    if protocol != "ssh":
        return False
    clean = (command or "").strip()
    if not clean or any(ch in clean for ch in (";", "|", "&", ">", "<", "`", "\n", "\r")) or "$(" in clean:
        return False
    if _SAFE_SED_READ_RE.match(clean):
        return True
    try:
        toks = shlex.split(clean)
    except ValueError:
        return False
    if not toks:
        return False
    cmd0 = Path(toks[0]).name.lower()
    if cmd0 == "find" and any(t in ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fls") for t in toks[1:]):
        return False
    if cmd0 in _READONLY_SINGLE_CMDS:
        return True
    if len(toks) >= 2 and f"{cmd0} {toks[1].lower()}" in _READONLY_TWO_WORD_CMDS:
        return True
    return False

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
            return {
                "ok": False,
                "error": "VAULT GUARD REJECTED: `credential_ref` must be a symbolic Vault name (like `node-ssh`), NEVER a raw key or token.",
            }

        eff_auth = (auth_mode or ("ssh_key" if proto == "ssh" and credential_ref else ("ssh_agent" if proto == "ssh" else ("basic_auth" if proto == "winrm" else ("bearer_token" if credential_ref else "none"))))).strip().lower()
        if eff_auth not in VALID_AUTH_MODES:
            return {"ok": False, "error": f"Invalid `auth_mode` '{eff_auth}'. Valid: {', '.join(VALID_AUTH_MODES)}."}
        parsed_url = urllib.parse.urlsplit(clean_host) if "://" in clean_host else None
        url_port = int(parsed_url.port) if (parsed_url and parsed_url.port) else 0
        eff_port = int(port) if int(port or 0) > 0 else (url_port or (443 if proto == "http_api" and clean_host.lower().startswith("https://") else _DEFAULT_PORTS[proto]))
        cred_slug = normalize_secret_name(credential_ref) if credential_ref else ""

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
        if cred_slug:
            get_vault_manager(store=self.store).declare_placeholder(
                cred_slug, cred_type="api_key", description=f"Proposed credential for endpoint `{eid}`", allow_list=[f"endpoint:{eid}"], actor=actor,
            )
        payload = {
            "endpoint_id": eid, "protocol": proto, "host": clean_host, "label": label.strip() or eid,
            "port": int(port or 0), "username": clean_user, "auth_mode": auth_mode.strip(),
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
        allowed, apr = get_approval_queue(self.store).check_or_enqueue(
            category="endpoint_exec",
            action_name=f"{ep['protocol']}_exec",
            target=ep["endpoint_id"],
            summary=f"Execute `{command[:100]}` on remote `{ep['protocol']}` host `{ep['label']}` (`{ep['host']}`).",
            payload={"endpoint_id": ep["endpoint_id"], "command": command},
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
        """Execute a safety-checked command over SSH using JIT Vault key injection or SSH agent."""
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
            return gate_block
        ssh_bin = shutil.which("ssh")
        if not ssh_bin:
            return {"ok": False, "error": "OpenSSH `ssh` client binary not found on PATH."}

        vault = get_vault_manager(store=self.store)
        tmp_key_path = ""
        extra_secrets: List[str] = []
        try:
            ssh_args = [
                ssh_bin, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
                "-o", "StrictHostKeyChecking=accept-new", "-p", str(eff_port),
            ]
            if ep["auth_mode"] == "ssh_key" and ep["credential_ref"]:
                ok, val_or_err = vault.resolve_for_call(ep["credential_ref"], f"endpoint:{ep['endpoint_id']}", expected_types=("ssh_key",))
                if not ok:
                    return {"ok": False, "error": val_or_err}
                extra_secrets.append(val_or_err)
                ssh_tmp_dir = settings.effective_data_dir / ".ssh_tmp"
                ssh_tmp_dir.mkdir(parents=True, exist_ok=True)
                _lock_windows_owner_dacl(ssh_tmp_dir)
                fd, tmp_key_path = tempfile.mkstemp(prefix=".comp_ssh_", suffix=".key", dir=str(ssh_tmp_dir))
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as kf:
                    kf.write(val_or_err if val_or_err.endswith("\n") else val_or_err + "\n")
                try:
                    os.chmod(tmp_key_path, 0o600)
                except OSError:
                    pass
                _lock_windows_owner_dacl(Path(tmp_key_path))
                ssh_args.extend(["-i", tmp_key_path])

            dest = f"{user_s}@{host_s}" if user_s else host_s
            proc = subprocess.run([*ssh_args, "--", dest, command], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=max(5, int(timeout)))
            out = vault.scrub_output((proc.stdout or "").strip(), extra_secrets=extra_secrets)
            err = vault.scrub_output((proc.stderr or "").strip(), extra_secrets=extra_secrets)
            combined = (out + (f"\n[stderr]\n{err}" if err else "")).strip() or "(no output)"
            return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": combined[: settings.out_limit], "error": "" if proc.returncode == 0 else combined[:400]}
        except Exception as exc:
            return {"ok": False, "error": vault.scrub_output(f"SSH error: {type(exc).__name__}: {exc}", extra_secrets=extra_secrets)}
        finally:
            if tmp_key_path and os.path.exists(tmp_key_path):
                try:
                    os.unlink(tmp_key_path)
                except OSError:
                    pass

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
        req_headers: Dict[str, str] = {"User-Agent": "companion-os/3.0", **(headers or {})}
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
