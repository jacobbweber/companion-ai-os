"""Federated MCP Hub: Outbound MCP Client, Inbound "Order Counter" Server & Partner-Aware Federation (`companion/mcp_hub.py`)."""

import contextlib, ctypes, hashlib, json, os, platform, re, secrets, shlex, shutil, socket, subprocess, threading, time, urllib.error, urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from companion import __version__
from companion.approvals import get_approval_queue, get_trust_mode
from companion.jobs import get_job_manager
from companion.config import is_in_agent_tool_turn, settings
from companion.llm_wire import check_url_ssrf, urlopen_safe
from companion.memory_store import MemoryStore, get_memory_store
from companion.workflows import stage_and_promote_skill
from companion.vault import get_vault_manager, normalize_secret_name
from companion.wiki import get_wiki_engine

__all__ = ["HOSTED_MCP_SAFE_TOOLS", "MCPHub", "build_local_peer_identity", "format_connected_partners_hud", "get_mcp_hub", "harvest_mcp_capability_pack"]

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

    def __init__(self, store: Optional[MemoryStore] = None, transport_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]] = None, partner_rpc_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any], str], Dict[str, Any]]] = None, chat_fn: Optional[Callable[..., Dict[str, Any]]] = None):
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
        init_line = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "companion", "version": __version__}}})
        notif_line = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        stdin_payload = "\n".join([init_line, notif_line, json.dumps(rpc_req)]) + "\n"
        proc: Optional[subprocess.Popen[str]] = None
        try:
            raw_cmd, raw_args = str(srv["command"]).strip(), list(srv.get("args", []))
            argv = [t.strip('"') for t in shlex.split(raw_cmd, posix=(os.name != "nt"))] if (not raw_args and (" " in raw_cmd or '"' in raw_cmd)) else [raw_cmd, *raw_args]
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8", errors="replace")
            target_id, found_box = rpc_req.get("id"), [None, "", ""]

            def _pump_stdio() -> None:
                assert proc is not None
                if proc.stdin:
                    with contextlib.suppress(Exception):
                        proc.stdin.write(stdin_payload)
                        proc.stdin.flush()
                if proc.stdout:
                    for raw_ln in proc.stdout:
                        s = raw_ln.strip()
                        if s.startswith("{"):
                            with contextlib.suppress(Exception):
                                obj = json.loads(s)
                                found_box[0] = obj
                                if target_id is None or obj.get("id") == target_id:
                                    found_box[1] = "matched"
                                    return
                if proc.stderr:
                    with contextlib.suppress(Exception):
                        found_box[2] = proc.stderr.read(4096)

            worker = threading.Thread(target=_pump_stdio, daemon=True)
            worker.start()
            worker.join(timeout=max(1.0, float(timeout)))
            if found_box[1] == "matched" and isinstance(found_box[0], dict):
                return True, found_box[0], ""
            if not worker.is_alive() and target_id is None and isinstance(found_box[0], dict):
                return True, found_box[0], ""
            err_raw = "Timed out waiting for stdio MCP response" if worker.is_alive() else (found_box[2] or "Missing matching JSON-RPC response ID in stdio MCP output")
            return False, {}, vault.scrub_output(err_raw[:300], extra_secrets=[secret_val] if secret_val else None)
        except Exception as exc:
            return False, {}, vault.scrub_output(f"MCP stdio error: {type(exc).__name__}: {exc}", extra_secrets=[secret_val] if secret_val else None)
        finally:
            if proc is not None:
                with contextlib.suppress(Exception):
                    if proc.stdin:
                        proc.stdin.close()
                with contextlib.suppress(Exception):
                    proc.terminate()
                    proc.wait(timeout=0.5)
                with contextlib.suppress(Exception):
                    proc.kill()
                    proc.wait(timeout=0.5)
                for pipe in (proc.stdout, proc.stderr):
                    with contextlib.suppress(Exception):
                        if pipe:
                            pipe.close()

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

    # --- Inbound Hosted MCP Server ("The Order Counter") ---

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

    # --- Partner-Aware Federation (Paired Companion Instances, e.g., Workstation <-> Lab GPU Node) ---

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
        blocks = ((resp.get("result") or {}).get("content") or [{}])
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


def harvest_mcp_capability_pack(server_id: str, pack_name: Optional[str] = None, overwrite: bool = True, store: Optional[MemoryStore] = None, root: Optional[Path] = None) -> Dict[str, Any]:
    """Introspect an approved MCP server's `discovered_tools` into a complete `data/skills/<pack>/` Capability Pack."""
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
    skill_md = (
        f"---\nname: {pack}\ndescription: Harvested MCP capability pack for {srv['label']} ({len(tools)} approved tool(s)).\nrequired_tools: [mcp_manage, workflow_manage]\n---\n\n"
        f"# {srv['label']} Capability Pack (`{pack}`)\n\n## When to Use This Skill\nUse this pack whenever orchestrating `{srv['server_id']}` MCP tools ({', '.join(str(t.get('name')) for t in tools[:8])}) deterministically.\n\n"
        f"## Step-by-Step Procedure\n1. Read `references/mcp-tools.md` to inspect exact JSON input schemas.\n2. Run `workflow_manage(action='run_dag', dag='{dag_name}', params=...)` for deterministic multi-step execution.\n3. Or invoke `mcp_manage(action='call_tool', server_id='{srv['server_id']}', tool_name=..., arguments=...)`.\n"
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
    res = stage_and_promote_skill(name=pack, skill_md=skill_md, scripts={"mcp_client_helper.py": helper_py}, dags={f"{dag_name}.workflow.json": json.dumps(wf_spec, indent=2)}, references={"mcp-tools.md": "\n".join(ref_lines)}, overwrite=overwrite, root=root, scope="user")
    hint = f" Harvested DAG `{dag_name}` (run via `workflow_manage(action='run_dag', dag='{dag_name}', params=...)` or `mcp_manage(action='call_tool', server_id='{srv['server_id']}', tool_name='{first_tool}', arguments=...)`)." if res.get("promoted") else ""
    return {**res, "ok": bool(res.get("promoted")), "pack_name": pack, "dag_name": dag_name, "summary": str(res.get("summary") or "") + hint}


def get_mcp_hub(
    store: Optional[MemoryStore] = None,
    transport_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]] = None,
    partner_rpc_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any], str], Dict[str, Any]]] = None,
) -> MCPHub:
    return MCPHub(store=store, transport_fn=transport_fn, partner_rpc_fn=partner_rpc_fn)
