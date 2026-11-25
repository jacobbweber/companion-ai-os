"""100% Python Stdlib Web Server (`surfaces/web_server.py` — A-Frame Presentation Layer).
Exposes Companion AI OS over `ThreadingHTTPServer` (`127.0.0.1:<port>` default, token-gated on LAN) with CSRF, SSE streaming, and WAL status APIs.
"""

import contextlib, contextvars, copy, io, ipaddress, json, os, secrets, socket, sys, threading, time, urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from companion.agent import handle_slash_command, run_agent_turn
from companion.agent import run_autonomous_goal
from companion.agent import create_or_revise_blueprint, is_awaiting_blueprint_approval
from companion.config import settings
from companion.context import build_system_prompt, manage_active_project, manage_companion_tone, manage_model_tier
from companion.llm import chat, model_gate
from companion.memory_store import MemoryStore, get_memory_store, memory_store_context
from companion.projects import get_project_graph
from companion.tools import run_system_doctor
from companion.streaming import CancelScope, cancel_scope_context, get_active_cancel_scope, stream_event_context
from companion.telemetry import notify_db_closed, read_ro_rows, telemetry
from surfaces.api_extensions import (
    build_history_payload, build_telemetry_payload, build_web_status_payload, cancel_web_or_bg_job, commit_cancelled_turn,
    create_session_idle_watcher, dispatch_chat_or_job_with_attachments, handle_extended_get, handle_extended_post,
    handle_extended_raw_get, load_or_create_csrf_token, record_coalesced_job_event, register_service_reload_handler,
    spawn_companion_reload, wait_for_port_release,
)
from surfaces.web_assets import read_static_asset, render_web_index_html

_CARRIER_NAT_NET = ipaddress.ip_network("100.64.0.0/10")
_LAN_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain", ".ts.net")
_LOCAL_HOSTNAME = socket.gethostname().strip().lower()
_READONLY_SUBACTIONS = frozenset({
    "list", "status", "get", "read", "search", "graph", "metadata", "check",
    "check_secret", "list_missing", "audit", "window", "progress", "board_view", "matrix", "probe_repo",
})


def detect_lan_ip() -> str:
    """Detect the host machine's primary LAN IPv4 address without sending packets."""
    for probe_target in (("192.168.1.1", 80), ("10.255.255.255", 1)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(probe_target)
            ip = str(sock.getsockname()[0])
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            sock.close()
    try:
        host_ip = socket.gethostbyname(socket.gethostname())
        if host_ip and not host_ip.startswith("127."):
            return host_ip
    except OSError:
        pass
    return "127.0.0.1"


def is_allowed_lan_host(host_header: str) -> bool:
    """Allow loopback, RFC1918/Tailscale LAN IPs, and verified local mDNS/machine hostnames; block DNS rebinding."""
    raw = (host_header or "").strip().lower()
    if not raw:
        return False
    host = (urllib.parse.urlsplit(f"http://{raw}").hostname or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "0.0.0.0", "::1") or (bool(_LOCAL_HOSTNAME) and host == _LOCAL_HOSTNAME) or host.endswith(_LAN_SUFFIXES):
        return True
    try:
        raw_ip = ipaddress.ip_address(host.strip("[]"))
        ip_obj = getattr(raw_ip, "ipv4_mapped", None) or raw_ip
        if ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_unspecified:
            return False
        return bool(ip_obj.is_loopback or ip_obj.is_private or (ip_obj.version == 4 and ip_obj in _CARRIER_NAT_NET))
    except ValueError:
        return False


def _is_loopback_client(client_ip: str) -> bool:
    raw = (client_ip or "").strip()
    if not raw:
        return False
    try:
        ip_obj = ipaddress.ip_address(raw.split("%")[0].strip("[]"))
        return bool((getattr(ip_obj, "ipv4_mapped", None) or ip_obj).is_loopback)
    except ValueError:
        return raw in ("localhost", "127.0.0.1", "::1")


def verify_lan_client_auth(headers: Any, client_ip: str, qs: Optional[Dict[str, List[str]]] = None) -> Tuple[bool, str]:
    """Allow direct loopback clients; honor `X-Forwarded-For` from loopback reverse proxies; require `COMPANION_WEB_TOKEN` for LAN."""
    eff_ip = client_ip
    if _is_loopback_client(client_ip):
        xff = str(headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if xff:
            eff_ip = xff
    if _is_loopback_client(eff_ip):
        return True, ""
    expected = settings.web_token.strip()
    if not expected:
        return False, ""
    auth_hdr = str(headers.get("Authorization") or "").strip()
    if auth_hdr.lower().startswith("bearer "):
        cand = auth_hdr[7:].strip()
        if cand and secrets.compare_digest(cand.encode("utf-8"), expected.encode("utf-8")):
            return True, ""
    raw_cookie = str(headers.get("Cookie") or "")
    if raw_cookie:
        jar = SimpleCookie()
        try:
            jar.load(raw_cookie)
            morsel = jar.get("companion_token")
            if morsel and morsel.value and secrets.compare_digest(morsel.value.encode("utf-8"), expected.encode("utf-8")):
                return True, ""
        except Exception:
            pass
    q_tok = str(((qs or {}).get("token") or [""])[0]).strip()
    if q_tok and secrets.compare_digest(q_tok.encode("utf-8"), expected.encode("utf-8")):
        sec_attr = "; Secure" if str(headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower() == "https" else ""
        return True, f"companion_token={q_tok}; HttpOnly; SameSite=Lax; Max-Age=2592000; Path=/{sec_attr}"
    return False, ""


def verify_post_request_security(headers: Any, expected_csrf: str) -> Optional[str]:
    """Enforce JSON Content-Type, Origin/Sec-Fetch-Site checks, and unconditional `X-Companion-CSRF` on all POSTs."""
    if "application/json" not in str(headers.get("Content-Type") or "").lower():
        return "Browser security guard: Content-Type must be application/json"
    sec_site = str(headers.get("Sec-Fetch-Site") or "").strip().lower()
    if sec_site and sec_site not in ("same-origin", "none"):
        return f"Cross-site request blocked (Sec-Fetch-Site={sec_site})"
    origin = str(headers.get("Origin") or "").strip()
    host_hdr = str(headers.get("Host") or "").strip()
    xf_host = str(headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
    allowed_hosts = {h.lower() for h in (host_hdr, xf_host) if h and is_allowed_lan_host(h)}
    if origin:
        if origin.lower() == "null":
            return "Cross-origin null Origin blocked"
        parsed_orig = urllib.parse.urlsplit(origin)
        if not parsed_orig.netloc or not is_allowed_lan_host(parsed_orig.netloc):
            return f"Cross-origin request blocked ({origin})"
        if (host_hdr or xf_host) and parsed_orig.netloc.lower() not in allowed_hosts:
            return f"Origin mismatch ({parsed_orig.netloc} != {host_hdr})"
    sent_token = str(headers.get("X-Companion-CSRF") or "").strip()
    if not sent_token or not secrets.compare_digest(sent_token, expected_csrf):
        return "csrf_mismatch"
    return None


def _read_rows(db_path: Path, sql: str, params: Tuple[Any, ...] = ()) -> List[Tuple[Any, ...]]:
    """Execute a thread-local `?mode=ro` WAL read query against SQLite so status polling never churns connections."""
    return read_ro_rows(db_path, sql, params)


class CompanionWebSession:
    """Thread-safe controller bridging HTTP requests, SSE streams, and async mobile jobs to Companion's engine."""

    def __init__(self, store: Optional[MemoryStore] = None, chat_fn: Callable[..., Dict[str, Any]] = chat, lan_url: str = "http://127.0.0.1:8000") -> None:
        self.store = store or get_memory_store()
        self.db_path = self.store.db_path.resolve()
        self.chat_fn = chat_fn
        self.lan_url = lan_url
        self.csrf_token = load_or_create_csrf_token(self.db_path)
        self._turn_lock = threading.Lock()
        self._jobs_lock = threading.Lock()
        self._stream_cond = threading.Condition(self._jobs_lock)
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._active_cancel_scope: Optional[CancelScope] = None
        self.active_job_id, self.busy, self.active_action, self._busy_started = "", False, "", 0.0
        self.idle_watcher = create_session_idle_watcher(self)
        self.messages: List[Dict[str, Any]] = [{"role": "system", "content": build_system_prompt(store=self.store)}]
        cut = _read_rows(self.db_path, "SELECT COALESCE(MAX(id), 0) FROM conversations WHERE session_tag = 'focus_marker'")
        rows = _read_rows(
            self.db_path,
            "SELECT role, content FROM (SELECT id, role, content FROM conversations WHERE id > ? AND role IN ('user', 'assistant') "
            "AND COALESCE(session_tag, '') NOT IN ('focus_marker', 'compact_marker') "
            "AND COALESCE(session_tag, '') NOT LIKE 'subagent:%' AND COALESCE(session_tag, '') NOT LIKE 'autonomy:%' "
            "ORDER BY id DESC LIMIT 12) ORDER BY id ASC",
            (int(cut[0][0]) if cut else 0,),
        )
        self.messages.extend({"role": str(r), "content": str(c)} for r, c in rows)

    def _serialize_job(self, job: Dict[str, Any], include_live_spans: bool = False) -> Dict[str, Any]:
        elapsed = job.get("elapsed_seconds")
        if elapsed is None:
            elapsed = round(max(0.0, time.monotonic() - float(job.get("started_mono", time.monotonic()))), 1)
        pid = settings.get_workspace().project_id
        wf_rows = _read_rows(self.db_path, "SELECT current_state FROM workflow_runs WHERE status = 'active' AND project_id = ? ORDER BY id DESC LIMIT 1", (pid,))
        out: Dict[str, Any] = {
            "job_id": job["job_id"], "mode": job["mode"], "message": job["message"], "status": job["status"],
            "elapsed_seconds": elapsed, "created_at": job["created_at"], "workflow_state": str(wf_rows[0][0]) if wf_rows else "IDLE",
            "active_phase": job.get("active_phase", ""), "active_tool": job.get("active_tool", ""),
            "stream_text": job.get("stream_text", ""), "stream_thinking": job.get("stream_thinking", ""),
            "tool_steps": list(job.get("tool_steps") or []), "event_cursor": len(job.get("events") or []),
            "passed": job.get("passed"), "awaiting_approval": job.get("awaiting_approval", False),
            "response": job.get("response", ""), "error": job.get("error", ""),
        }
        if include_live_spans:
            sp_rows = _read_rows(self.db_path, "SELECT span_type, name, status, duration_ms, detail FROM telemetry_spans ORDER BY id DESC LIMIT 6")
            out["recent_spans"] = [{"span_type": str(st), "name": str(nm), "status": str(stat), "duration_ms": round(float(dur or 0.0), 1), "detail": str(det)} for st, nm, stat, dur, det in reversed(sp_rows)]
        return out

    def get_status_payload(self) -> Dict[str, Any]:
        """Build non-blocking live status JSON for `GET /api/status`."""
        return build_web_status_payload(self)

    def get_history_payload(self, limit: int = 60, include_tools: bool = False, before_id: int = 0) -> Dict[str, Any]:
        return build_history_payload(self.db_path, limit=limit, include_tools=include_tools, before_id=before_id)

    def get_search_payload(self, query: str = "", limit: int = 20) -> Dict[str, Any]:
        rows = self.store.search_conversations(query=query, limit=min(max(1, limit), 100))
        return {"ok": True, "query": query, "count": len(rows), "results": [{"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3]} for r in rows]}

    def get_telemetry_payload(self, limit: int = 12, trace_id: str = "") -> Dict[str, Any]:
        return build_telemetry_payload(self.db_path, limit=limit, trace_id=trace_id)

    def cancel_active_turn(self, job_id: str = "") -> Tuple[int, Dict[str, Any]]:
        clean_id = (job_id or "").strip()
        with self._jobs_lock:
            target = (self._jobs.get(self.active_job_id) if clean_id in ("", "active") else self._jobs.get(clean_id)) if (clean_id or self.active_job_id) else None
            scope = (target.get("cancel_scope") if target else None) or self._active_cancel_scope
            eff_id = str(target["job_id"]) if target else (self.active_job_id or clean_id or "active")
        if scope is None and target is None and not self.busy:
            return (404, {"ok": False, "error": f"Unknown job_id: {clean_id}"}) if clean_id not in ("", "active") else (200, {"ok": True, "cancelled": False, "status": "idle"})
        if scope is not None:
            scope.cancel()
        return 200, {"ok": True, "cancelled": True, "job_id": eff_id, "status": "cancelled"}

    def execute_chat(
        self, message: str, mode: str = "chat", cancel_scope: Optional[CancelScope] = None,
        focus_project_id: str = "", followup_doc_note: str = "",
    ) -> Tuple[int, Dict[str, Any]]:
        """Execute a foreground chat message, `/slash` command, or multi-phase autonomous goal."""
        clean_msg = (message or "").strip()
        if not clean_msg:
            return 400, {"ok": False, "error": "message is required"}
        if not self._turn_lock.acquire(timeout=2.0):
            return 409, {"ok": False, "error": "Companion is currently busy processing an active turn. Please wait.", "active_job_id": self.active_job_id}
        scope = cancel_scope or get_active_cancel_scope() or CancelScope()
        self._active_cancel_scope = scope
        self.busy, self._busy_started = True, time.monotonic()
        self.active_action = "blueprint plan" if mode == "plan" else ("autonomous goal" if mode == "goal" else (clean_msg.split()[0] if clean_msg.startswith("/") else "chat turn"))
        if focus_project_id:
            get_project_graph(store=self.store).set_focus_project(focus_project_id)
        pre_msgs = copy.deepcopy(self.messages)
        try:
            with cancel_scope_context(scope), memory_store_context(self.store):
                try:
                    slash_out = handle_slash_command(clean_msg, self.messages, store=self.store, chat_fn=self.chat_fn, interactive=False)
                    if slash_out is not None:
                        low_cmd = clean_msg.lower()
                        m_tag = "focus_marker" if low_cmd in ("/reset", "/clear") else ("compact_marker" if low_cmd.startswith("/compact") and "status" not in low_cmd else "")
                        if m_tag:
                            marker = "Fresh working focus — earlier messages remain saved & searchable." if m_tag == "focus_marker" else "Context compacted into long-term memory — earlier messages remain saved & searchable."
                            self.store.log_message("assistant", marker, session_tag=m_tag)
                            return 200, {"ok": True, "role": "assistant", "mode": "slash", "session_tag": m_tag, "response": marker}
                        self.store.log_message("user", clean_msg, session_tag="web_slash")
                        self.store.log_message("assistant", slash_out, session_tag="web_slash")
                        return 200, {"ok": True, "role": "assistant", "mode": "slash", "response": slash_out}
                    if mode in ("plan", "goal") and not is_awaiting_blueprint_approval(self.store):
                        tag, label = ("web_plan", "[Plan First]") if mode == "plan" else ("web_goal", "[Goal Mode]")
                        self.store.log_message("user", f"{label} {clean_msg}", session_tag=tag)
                        res = create_or_revise_blueprint(clean_msg, chat_fn=self.chat_fn, store=self.store, verbose=False) if mode == "plan" else run_autonomous_goal(clean_msg, chat_fn=self.chat_fn, store=self.store, verbose=False)
                        reply = str(res.get("summary", ""))
                        self.store.log_message("assistant", reply, session_tag=tag)
                        u_hist = f"{clean_msg}\n\n{followup_doc_note}".strip() if followup_doc_note else clean_msg
                        self.messages.extend([{"role": "user", "content": u_hist}, {"role": "assistant", "content": reply}])
                        return 200, {"ok": True, "role": "assistant", "mode": mode, "passed": res.get("passed", False), "awaiting_approval": res.get("awaiting_approval", False), "response": reply}
                except InterruptedError:
                    return commit_cancelled_turn(self, pre_msgs, clean_msg, mode, is_awaiting_blueprint_approval(self.store))
                except Exception as exc:
                    err_text = f"{mode.title()} error: {type(exc).__name__}: {exc}"
                    telemetry.log("ERROR", "web_ui", err_text)
                    return 500, {"ok": False, "error": err_text}
                u_idx = len(self.messages)
                self.messages.append({"role": "user", "content": clean_msg})
                try:
                    with model_gate.foreground_turn():
                        reply = run_agent_turn(self.messages, chat_fn=self.chat_fn, store=self.store, session_tag="web_ui", verbose=False)
                    if followup_doc_note and u_idx < len(self.messages) and self.messages[u_idx].get("role") == "user":
                        self.messages[u_idx]["content"] = f"{self.messages[u_idx]['content']}\n\n{followup_doc_note}".strip()
                    return 200, {"ok": True, "role": "assistant", "mode": "chat", "awaiting_approval": is_awaiting_blueprint_approval(self.store), "response": reply}
                except InterruptedError:
                    return commit_cancelled_turn(self, pre_msgs, clean_msg, mode, is_awaiting_blueprint_approval(self.store))
                except Exception as exc:
                    self.messages[:] = pre_msgs
                    err_text = f"Turn error: {type(exc).__name__}: {exc}"
                    telemetry.log("ERROR", "web_ui", err_text)
                    return 500, {"ok": False, "error": err_text}
        finally:
            self._active_cancel_scope = None
            self.busy, self.active_action = False, ""
            self._turn_lock.release()

    def _record_job_stream_event(self, job: Dict[str, Any], ev: Dict[str, Any]) -> None:
        with self._jobs_lock:
            record_coalesced_job_event(job, ev)
            self._stream_cond.notify_all()

    def submit_async_chat(
        self, message: str, mode: str = "chat", focus_project_id: str = "", followup_doc_note: str = "",
    ) -> Tuple[int, Dict[str, Any]]:
        """Launch a disconnect-proof background turn/goal job (`202 Accepted`) with live SSE event streaming."""
        clean_msg = (message or "").strip()
        if not clean_msg:
            return 400, {"ok": False, "error": "message is required"}
        if clean_msg.startswith(("/", ".")) and not clean_msg.lower().startswith(("/goal ", "/plan ", "/blueprint ", "/approve", "/eval", "/subagent ", "/doctor")):
            return self.execute_chat(clean_msg, mode=mode, focus_project_id=focus_project_id, followup_doc_note=followup_doc_note)
        scope = CancelScope()
        with self._jobs_lock:
            if self.busy or (self.active_job_id and self._jobs.get(self.active_job_id, {}).get("status") == "running"):
                return 409, {"ok": False, "error": "Companion is currently busy processing an active job.", "active_job_id": self.active_job_id}
            job_id = f"job_{int(time.time() * 1000)}_{os.urandom(3).hex()}"
            job: Dict[str, Any] = {
                "job_id": job_id, "message": clean_msg, "mode": mode, "status": "running", "cancel_scope": scope,
                "started_mono": time.monotonic(), "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "passed": None, "awaiting_approval": False, "response": "", "error": "", "elapsed_seconds": None,
                "stream_text": "", "stream_thinking": "", "active_phase": "", "active_tool": "", "tool_steps": [], "events": [],
            }
            self._jobs[job_id] = job
            if len(self._jobs) > 40:
                oldest = next(iter(self._jobs))
                if oldest != job_id:
                    self._jobs.pop(oldest, None)
            self.active_job_id = job_id

        def _bg_job_runner() -> None:
            try:
                with stream_event_context(lambda ev: self._record_job_stream_event(job, ev)):
                    code, payload = self.execute_chat(clean_msg, mode=mode, cancel_scope=scope, focus_project_id=focus_project_id, followup_doc_note=followup_doc_note)
            except Exception as exc:
                code, payload = 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            with self._jobs_lock:
                job["elapsed_seconds"] = round(max(0.0, time.monotonic() - float(job["started_mono"])), 2)
                if payload.get("cancelled"):
                    job.update({"status": "cancelled", "response": str(payload.get("response") or "*(Stopped by user.)*"), "awaiting_approval": bool(payload.get("awaiting_approval"))})
                elif code < 400 and payload.get("ok"):
                    job.update({"status": "completed", "response": str(payload.get("response") or ""), "passed": payload.get("passed"), "awaiting_approval": bool(payload.get("awaiting_approval"))})
                else:
                    job.update({"status": "error", "error": str(payload.get("error") or f"HTTP {code}")})
                job["events"].append({"type": "done", "status": job["status"], "cancelled": bool(payload.get("cancelled")), "response": job["response"], "error": job["error"], "passed": job["passed"], "awaiting_approval": job["awaiting_approval"]})
                if self.active_job_id == job_id:
                    self.active_job_id = ""
                self._stream_cond.notify_all()

        ctx = contextvars.copy_context()
        threading.Thread(target=lambda: ctx.run(_bg_job_runner), daemon=True, name=f"companion-{job_id}").start()
        return 202, {"ok": True, "accepted": True, "job_id": job_id, "status": "running", "mode": mode}

    def wait_job_events(self, job_id: str, cursor: int, timeout: float = 10.0) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], int]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job:
                return None, [], cursor
            if cursor >= len(job["events"]) and job["status"] == "running":
                self._stream_cond.wait(timeout=timeout)
            events = list(job["events"][cursor:])
            return dict(job), events, cursor + len(events)

    def get_job_payload(self, job_id: str = "") -> Tuple[int, Dict[str, Any]]:
        clean_id = (job_id or "").strip()
        with self._jobs_lock:
            if not clean_id:
                return 200, {"ok": True, "active_job_id": self.active_job_id, "jobs": [self._serialize_job(j) for j in list(self._jobs.values())[-15:]]}
            job = self._jobs.get(clean_id)
            if not job:
                return 404, {"ok": False, "error": f"Unknown job_id: {clean_id}"}
            return 200, {"ok": True, "job": self._serialize_job(dict(job), include_live_spans=True)}

    def update_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        notes: List[str] = []
        if payload.get("tier"):
            notes.append(manage_model_tier(str(payload["tier"]), store=self.store))
        if payload.get("tone"):
            notes.append(manage_companion_tone(str(payload["tone"]), store=self.store))
        if payload.get("project"):
            notes.append(manage_active_project(str(payload["project"]), store=self.store))
        if payload.get("focus_project_id"):
            fp = get_project_graph(store=self.store).set_focus_project(str(payload["focus_project_id"]))
            notes.append(f"Focused Goal/Project set to `{fp['title']}` (`{fp['project_id']}`).")
        if notes:
            fresh_sys = build_system_prompt(store=self.store)
            with self._turn_lock:
                if self.messages and self.messages[0].get("role") == "system":
                    self.messages[0]["content"] = fresh_sys
        return {"ok": True, "message": "\n".join(notes) or "No settings changed."}


def _parse_int_qs(qs: Dict[str, List[str]], key: str, default: int) -> Optional[int]:
    raw = str((qs.get(key) or [str(default)])[0] or default).strip()
    try:
        return int(raw)
    except ValueError:
        return None


class CompanionHTTPServer(ThreadingHTTPServer):
    """Threaded stdlib HTTP server with daemon worker threads and fast socket reuse."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: Tuple[str, int], handler_cls: type, session: CompanionWebSession) -> None:
        super().__init__(server_address, handler_cls)
        self.session = session

    def server_close(self) -> None:
        self.session.idle_watcher.stop()
        notify_db_closed(self.session.db_path)
        super().server_close()


class CompanionWebRequestHandler(BaseHTTPRequestHandler):
    """HTTP/1.1 handler serving Companion's zero-login LAN Web UI, SSE streams, and JSON REST APIs."""

    server: CompanionHTTPServer

    def setup(self) -> None:
        if hasattr(self.request, "settimeout"):
            self.request.settimeout(30.0)
        if hasattr(self.request, "setsockopt"):
            with contextlib.suppress(OSError):
                self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        super().setup()

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_security_headers(self) -> None:
        for hdr, val in (("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer")):
            self.send_header(hdr, val)

    def _send_bytes(self, status: int, content_type: str, raw: bytes, extra_headers: Tuple[Tuple[str, str], ...] = ()) -> None:
        with contextlib.suppress(ConnectionAbortedError, BrokenPipeError, ConnectionResetError, OSError):
            self.send_response(status)
            for hdr, val in (("Content-Type", content_type), ("Content-Length", str(len(raw))), ("Cache-Control", "no-store"), *extra_headers):
                self.send_header(hdr, val)
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(raw)

    def _send_json(self, status: int, data: Dict[str, Any]) -> None:
        self._send_bytes(status, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _check_host_guard(self) -> bool:
        host_hdr = str(self.headers.get("Host") or "").strip()
        xf_host = str(self.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
        if is_allowed_lan_host(host_hdr) and (not xf_host or is_allowed_lan_host(xf_host)):
            return True
        self._send_json(403, {"ok": False, "error": "DNS rebinding guard: non-LAN Host header rejected"})
        return False

    def _read_json_body(self, max_bytes: int = 1_048_576) -> Tuple[Optional[int], Dict[str, Any]]:
        raw_cl = self.headers.get("Content-Length")
        if raw_cl is None or str(raw_cl).strip() == "":
            return None, {}
        try:
            length = int(str(raw_cl).strip())
        except ValueError:
            return 400, {"ok": False, "error": "Invalid Content-Length header"}
        if length < 0:
            return 400, {"ok": False, "error": "Negative Content-Length header"}
        if length == 0:
            return None, {}
        if length > max_bytes:
            return 413, {"ok": False, "error": f"Request payload exceeds {max_bytes} byte limit"}
        try:
            parsed = json.loads(self.rfile.read(length).decode("utf-8", errors="replace"))
            return (None, parsed) if isinstance(parsed, dict) else (400, {"ok": False, "error": "JSON body must be an object"})
        except json.JSONDecodeError:
            return 400, {"ok": False, "error": "Malformed JSON body"}

    def _stream_job_sse(self, job_id: str, qs: Optional[Dict[str, List[str]]] = None) -> None:
        sess = self.server.session
        if sess.wait_job_events(job_id, 0, timeout=0.0)[0] is None:
            self._send_json(404, {"ok": False, "error": f"Unknown job_id: {job_id}"})
            return
        self.send_response(200)
        for hdr, val in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-cache, no-store"), ("Connection", "close"), ("X-Accel-Buffering", "no")):
            self.send_header(hdr, val)
        self._send_security_headers()
        self.end_headers()
        raw_cur = str(((qs or {}).get("cursor") or [self.headers.get("Last-Event-ID") or "0"])[0] or "0").strip()
        cursor = max(0, int(raw_cur)) if raw_cur.isdigit() else 0
        try:
            while True:
                base_cur = cursor
                job_now, batch, cursor = sess.wait_job_events(job_id, cursor, timeout=10.0)
                if job_now is None:
                    return
                if not batch and job_now.get("status") == "running":
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                frames = batch or ([{"type": "done", "status": job_now.get("status"), "response": job_now.get("response", ""), "error": job_now.get("error", "")}] if job_now.get("status") != "running" else [])
                for offset, ev in enumerate(frames, 1):
                    self.wfile.write(f"id: {base_cur + offset}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    if ev.get("type") == "done":
                        return
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_OPTIONS(self) -> None:
        self._send_json(403, {"ok": False, "error": "CORS preflight disabled on private LAN Companion server"})

    def do_GET(self) -> None:
        if not self._check_host_guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        route, qs, sess = parsed.path, urllib.parse.parse_qs(parsed.query), self.server.session
        client_ip = str(self.client_address[0]) if self.client_address else ""
        auth_ok, set_cookie = verify_lan_client_auth(self.headers, client_ip, qs=qs)
        extra_cookie = (("Set-Cookie", set_cookie),) if set_cookie else ()
        if route == "/api/health":
            payload: Dict[str, Any] = {"ok": True, "service": "companion-web-ui", "lan_url": sess.lan_url}
            if auth_ok:
                payload["csrf_token"] = sess.csrf_token
            else:
                payload["auth_required"] = True
            self._send_bytes(200, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"), extra_cookie)
            return
        if not auth_ok and route != "/api/mcp":
            self._send_json(401, {"ok": False, "auth_required": True, "error": "LAN access requires a valid COMPANION_WEB_TOKEN (Bearer header, cookie, or ?token=)."})
            return
        raw_ext = handle_extended_raw_get(route, sess.store)
        if raw_ext is not None:
            self._send_bytes(raw_ext[0], raw_ext[1], raw_ext[2], (*raw_ext[3], *extra_cookie))
            return
        ext = handle_extended_get(route, sess.store)
        if ext is not None:
            self._send_json(ext[0], ext[1])
            return
        if route in ("/", "/index.html"):
            self._send_bytes(200, "text/html; charset=utf-8", render_web_index_html(csrf_token=sess.csrf_token).encode("utf-8"), extra_cookie)
        elif route.startswith("/static/"):
            asset = read_static_asset(route.split("/static/", 1)[1])
            self._send_bytes(200, asset[0], asset[1], extra_cookie) if asset is not None else self._send_json(404, {"ok": False, "error": f"Unknown static asset: {route}"})
        elif route == "/favicon.ico":
            self._send_bytes(204, "image/x-icon", b"")
        elif route == "/api/status":
            self._send_json(200, sess.get_status_payload())
        elif route.startswith("/api/jobs/") and route.endswith("/stream"):
            self._stream_job_sse(route[len("/api/jobs/") : -len("/stream")].strip("/"), qs=qs)
        elif route == "/api/jobs" or route.startswith("/api/jobs/"):
            self._send_json(*sess.get_job_payload(route.split("/api/jobs/", 1)[1] if route.startswith("/api/jobs/") else ""))
        elif route == "/api/history":
            lim, bef = _parse_int_qs(qs, "limit", 60), _parse_int_qs(qs, "before_id", 0)
            if lim is None or bef is None:
                self._send_json(400, {"ok": False, "error": "Invalid integer `limit` or `before_id` parameter"})
                return
            hist_payload = sess.get_history_payload(limit=lim, include_tools=(qs.get("include_tools") or ["0"])[0] in ("1", "true", "yes"), before_id=max(0, bef))
            self._send_json(200 if hist_payload.get("ok", True) else 500, hist_payload)
        elif route == "/api/telemetry":
            lim = _parse_int_qs(qs, "limit", 12)
            if lim is None:
                self._send_json(400, {"ok": False, "error": "Invalid integer `limit` parameter"})
                return
            self._send_json(200, sess.get_telemetry_payload(limit=lim, trace_id=str((qs.get("trace_id") or [""])[0])))
        elif route == "/api/search":
            lim = _parse_int_qs(qs, "limit", 20)
            if lim is None:
                self._send_json(400, {"ok": False, "error": "Invalid integer `limit` parameter"})
                return
            self._send_json(200, sess.get_search_payload(query=str((qs.get("q") or [""])[0]), limit=lim))
        else:
            self._send_json(404, {"ok": False, "error": f"Unknown endpoint: {route}"})

    def do_POST(self) -> None:
        if not self._check_host_guard():
            return
        route = urllib.parse.urlparse(self.path).path
        sess = self.server.session
        client_ip = str(self.client_address[0]) if self.client_address else ""
        if route != "/api/mcp":
            auth_ok, _ = verify_lan_client_auth(self.headers, client_ip)
            if not auth_ok:
                self._send_json(401, {"ok": False, "error": "LAN access requires a valid COMPANION_WEB_TOKEN."})
                return
            sec_err = verify_post_request_security(self.headers, sess.csrf_token)
            if sec_err:
                self._send_json(403, {"ok": False, "error": sec_err})
                return
        body_err, body = self._read_json_body(28_311_552 if route == "/api/attachments" else 1_048_576)
        if body_err is not None:
            self._send_json(body_err, body)
            return
        if route == "/api/mcp":
            ext_mcp = handle_extended_post(route, body, self.headers, sess.store)
            if ext_mcp is None:
                self._send_json(404, {"ok": False})
            elif ext_mcp[0] == 202 and not ext_mcp[1]:
                self._send_bytes(202, "application/json; charset=utf-8", b"")
            else:
                self._send_json(ext_mcp[0], ext_mcp[1])
            return
        sub = str(body.get("subaction") or "").strip().lower()
        if not (route.startswith("/api/actions/") and sub in _READONLY_SUBACTIONS):
            sess.idle_watcher.record_human_activity()
        ext_act = handle_extended_post(route, body, self.headers, sess.store)
        if ext_act is not None:
            if route in ("/api/actions/autonomy", "/api/actions/identity", "/api/actions/trust", "/api/actions/models"):
                fresh_sys = build_system_prompt(store=sess.store)
                with sess._turn_lock:
                    if sess.messages and sess.messages[0].get("role") == "system":
                        sess.messages[0]["content"] = fresh_sys
            self._send_json(ext_act[0], ext_act[1])
            return
        if route == "/api/jobs/cancel" or (route.startswith("/api/jobs/") and route.endswith("/cancel")):
            jid = "" if route == "/api/jobs/cancel" else route[len("/api/jobs/") : -len("/cancel")].strip("/")
            self._send_json(*cancel_web_or_bg_job(sess, jid or str(body.get("job_id") or "")))
        elif route in ("/api/chat", "/api/jobs"):
            self._send_json(*dispatch_chat_or_job_with_attachments(sess, body, route == "/api/jobs" or bool(body.get("async"))))
        elif route == "/api/settings":
            self._send_json(200, sess.update_settings(body))
        elif route == "/api/doctor":
            self._send_json(200, run_system_doctor(run_tests=bool(body.get("run_tests", True))))
        else:
            self._send_json(404, {"ok": False, "error": f"Unknown POST endpoint: {route}"})


def create_web_server(host: Optional[str] = None, port: int = 8000, store: Optional[MemoryStore] = None, chat_fn: Callable[..., Dict[str, Any]] = chat) -> Tuple[CompanionHTTPServer, CompanionWebSession]:
    """Instantiate a bound `CompanionHTTPServer` and `CompanionWebSession` (supports `port=0` for tests)."""
    eff_host = (host or settings.web_host).strip() or "127.0.0.1"
    lan_ip = detect_lan_ip()
    sess = CompanionWebSession(store=store, chat_fn=chat_fn, lan_url=f"http://{lan_ip}:{port}")
    srv = CompanionHTTPServer((eff_host, int(port)), CompanionWebRequestHandler, session=sess)
    sess.lan_url = f"http://{lan_ip}:{int(srv.server_address[1])}"
    return srv, sess


class _InMemorySocket:
    """Minimal socket shim allowing `CompanionWebRequestHandler` execution when OS loopback sockets are sandboxed."""

    def __init__(self, raw_request: bytes) -> None:
        self._rbuf, self.wbuf = io.BytesIO(raw_request), io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self._rbuf if "r" in mode else self.wbuf

    def sendall(self, data: bytes) -> None:
        self.wbuf.write(data)


def dispatch_raw_http(
    server: CompanionHTTPServer, method: str = "GET", path: str = "/",
    body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None, client_ip: str = "127.0.0.1",
) -> Tuple[int, str]:
    """Execute a real HTTP/1.1 request through `CompanionWebRequestHandler` in-memory and return `(status, body_str)`."""
    payload = body or b""
    req_headers: Dict[str, str] = {"Host": "127.0.0.1", "Connection": "close", **({"Content-Type": "application/json", "Content-Length": str(len(payload))} if payload else {}), **(headers or {})}
    hdr_lines = [f"{method.upper()} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in req_headers.items()]
    sock = _InMemorySocket(("\r\n".join(hdr_lines) + "\r\n\r\n").encode("utf-8") + payload)
    CompanionWebRequestHandler(sock, (client_ip, 0), server)
    head_bytes, _, body_bytes = sock.wbuf.getvalue().partition(b"\r\n\r\n")
    parts = (head_bytes.decode("iso-8859-1", errors="replace").splitlines()[0] if head_bytes else "HTTP/1.1 500").split()
    return (int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 500), body_bytes.decode("utf-8", errors="replace")


def start_web_server(host: Optional[str] = None, port: Optional[int] = None) -> None:
    """Launch Companion's Web UI server (`127.0.0.1` default) and block until interrupted (supports live `/api/reload`)."""
    eff_host = (host or settings.web_host).strip() or "127.0.0.1"
    eff_port = int(port if port is not None else int(os.environ.get("COMPANION_WEB_PORT", "8000")))
    if os.environ.pop("COMPANION_RELOADING", "") == "1":
        wait_for_port_release(eff_host, eff_port, timeout=6.0)
    try:
        srv, sess = create_web_server(host=eff_host, port=eff_port)
    except OSError as exc:
        from surfaces.windows_tray import format_port_conflict_error

        print(f"error: {format_port_conflict_error(eff_host, eff_port, exc)}", file=sys.stderr)
        sys.exit(1)
    bound_port, do_reload = int(srv.server_address[1]), [False]
    register_service_reload_handler(lambda: (do_reload.__setitem__(0, True), threading.Thread(target=srv.shutdown, daemon=True).start()) and None)
    telemetry.log("INFO", "web_ui", f"Started Companion Web UI on {eff_host}:{bound_port} ({sess.lan_url})")
    lan_note = f"  Mobile / LAN URL  : {sess.lan_url} (COMPANION_WEB_TOKEN required)\n" if eff_host not in ("127.0.0.1", "localhost", "::1") else ""
    print(f"=== Companion AI OS — Web UI Active ===\n  Local Desktop URL : http://127.0.0.1:{bound_port}\n{lan_note}  Press Ctrl+C to stop the Web UI server.\n")
    sys.stdout.flush()
    sess.idle_watcher.start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Companion Web UI server...")
    finally:
        register_service_reload_handler(None)
        sess.idle_watcher.stop()
        srv.shutdown()
        srv.server_close()
    if do_reload[0]:
        spawn_companion_reload(port=bound_port)


if __name__ == "__main__":
    start_web_server(port=int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None)
