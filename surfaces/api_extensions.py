"""
Headless Web API Extensions, Multimodal Attachments & Hosted MCP Route Adapter
(`surfaces/api_extensions.py` — A-Frame Presentation Layer).

Wires Companion 3.0's Shared Core Action Bus (`/api/actions`, `/api/actions/<name>`),
Forever Chat Multimodal File Attachments (`/api/attachments`, `/api/attachments/<id>/raw`),
and Token-Authenticated Inbound Hosted MCP Order Counter (`/api/mcp`) into `CompanionWebRequestHandler`.
"""

import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from companion.approvals import get_approval_queue, get_trust_mode, peek_session_nudges
from companion.attachments import (
    get_attachment_record,
    ingest_attachment,
    prepare_turn_attachments,
    read_attachment_raw,
    rehydrate_recent_turn_images,
)
from companion.jobs import get_job_manager
from companion.vault import get_active_checkpoint_manager
from companion.config import settings
from companion.core_actions import execute_core_action, list_core_actions
from companion.education_engine import get_education_engine
from companion.llm import active_swap_status
from companion.integrations import get_mcp_hub
from companion.memory_store import MemoryStore
from companion.model_registry import get_model_matrix_snapshot
from companion.projects import get_project_graph
from companion.routines import IdleAutonomyWatcher, describe_operating_quadrant
from companion.streaming import wire_attachment_context
from companion.telemetry import TelemetryEngine, read_ro_rows, telemetry
from companion.wiki import get_wiki_engine
from companion.wiki import read_wiki_asset_raw
from companion.agent import WorkflowEngine

__all__ = [
    "build_history_payload",
    "build_reload_command",
    "build_telemetry_payload",
    "build_web_status_payload",
    "cancel_web_or_bg_job",
    "commit_cancelled_turn",
    "create_session_idle_watcher",
    "dispatch_chat_or_job_with_attachments",
    "enrich_web_status",
    "handle_extended_get",
    "handle_extended_post",
    "handle_extended_raw_get",
    "load_or_create_csrf_token",
    "record_coalesced_job_event",
    "register_service_reload_handler",
    "request_service_reload",
    "spawn_companion_reload",
    "wait_for_port_release",
]

_RELOAD_HANDLER: Optional[Callable[[], None]] = None


def register_service_reload_handler(handler: Optional[Callable[[], None]]) -> None:
    """Register the active Tray or Web server shutdown-and-reload callback."""
    global _RELOAD_HANDLER
    _RELOAD_HANDLER = handler


def build_reload_command(port: Optional[int] = None) -> List[str]:
    """Construct the exact Python command line to relaunch this Companion service cleanly."""
    has_py_entry = bool(sys.argv and Path(sys.argv[0]).suffix.lower() == ".py" and Path(sys.argv[0]).exists())
    script = Path(sys.argv[0]).resolve() if has_py_entry else (settings.root / "harness.py").resolve()
    raw_args = list(sys.argv[1:]) if has_py_entry else ["--web"]
    if any(a in ("--tray", "/tray") for a in raw_args) and "--no-browser" not in raw_args:
        raw_args.append("--no-browser")
    if port is not None:
        p_str = str(int(port))
        replaced = False
        cleaned: List[str] = []
        idx = 0
        while idx < len(raw_args):
            tok = raw_args[idx]
            if tok == "--port" and idx + 1 < len(raw_args):
                cleaned.extend(["--port", p_str])
                replaced = True
                idx += 2
            elif tok.isdigit() and not replaced:
                cleaned.append(p_str)
                replaced = True
                idx += 1
            else:
                cleaned.append(tok)
                idx += 1
        if not replaced:
            cleaned.extend(["--port", p_str])
        raw_args = cleaned
    return [sys.executable, str(script), *raw_args]


def wait_for_port_release(host: str, port: int, timeout: float = 6.0) -> bool:
    """Wait until `port` is bindable so a reloading child process reclaims the exact same port."""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    deadline = time.monotonic() + max(0.2, float(timeout))
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((probe_host, int(port)))
                return True
            except OSError:
                time.sleep(0.15)
    return False


def spawn_companion_reload(port: Optional[int] = None) -> List[str]:
    """Spawn or in-place `execv` a fresh Companion process inheriting `COMPANION_DATA_DIR` and `COMPANION_RELOADING=1`."""
    cmd = build_reload_command(port=port)
    env = dict(os.environ)
    env["COMPANION_RELOADING"] = "1"
    env.setdefault("COMPANION_DATA_DIR", str(settings.effective_data_dir))
    is_supervised = bool(os.environ.get("INVOCATION_ID") or os.environ.get("COMPANION_SUPERVISED") == "1" or Path("/.dockerenv").exists())
    if is_supervised and os.name != "nt":
        telemetry.log("INFO", "web_ui", f"In-place supervised execv reload: {' '.join(cmd)}")
        os.environ.update(env)
        os.execv(sys.executable, cmd)
        return cmd
    flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0
    windowless = sys.stdout is None or Path(sys.executable).name.lower().startswith("pythonw")
    if os.name == "nt" and windowless:
        flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    std_kw = (
        {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if windowless
        else {}
    )
    subprocess.Popen(cmd, cwd=str(settings.root), env=env, close_fds=True, creationflags=flags, **std_kw)
    telemetry.log("INFO", "web_ui", f"Spawned reloaded Companion service: {' '.join(cmd)}")
    return cmd


def request_service_reload() -> Dict[str, Any]:
    """Schedule an asynchronous reload (~250ms delay) so the HTTP 200 response flushes first."""
    handler = _RELOAD_HANDLER
    if handler is None:
        return {"ok": False, "error": "Service reload is only available in live `--tray` or `--web` server mode."}
    threading.Timer(0.25, handler).start()
    return {"ok": True, "reloading": True, "summary": "Reloading Companion service with latest code..."}


def create_session_idle_watcher(sess: Any, poll_interval_sec: float = 15.0) -> IdleAutonomyWatcher:
    """Create an `IdleAutonomyWatcher` bound to `sess.store` that yields immediately to foreground chat or jobs."""

    def _is_web_busy() -> bool:
        if bool(getattr(sess, "busy", False)) or bool(getattr(sess, "active_job_id", "")):
            return True
        jobs = get_job_manager(store=sess.store).list_jobs(limit=15)
        return any(j.get("status") == "running" for j in jobs)

    return IdleAutonomyWatcher(is_busy_fn=_is_web_busy, store=sess.store, poll_interval_sec=poll_interval_sec)


def enrich_web_status(store: MemoryStore) -> Dict[str, Any]:
    """Return Companion 3.5 status fields merged into `GET /api/status`."""
    prof = store.get_user_profile()
    jobs = get_job_manager(store=store).list_jobs(limit=25)
    active_bg = [j for j in jobs if j["status"] in ("queued", "running", "waiting_approval")]
    partners = get_mcp_hub(store=store).list_partners()
    nudges = peek_session_nudges(session_id="web_ui", store=store) + peek_session_nudges(session_id="main", store=store)
    pg = get_project_graph(store=store)
    agile_view = pg.get_board_projection()
    all_open_items = [c for c in pg.list_items() if c["column_key"] != "done"]
    all_blocked_items = [c for c in all_open_items if c["column_key"] == "blocked"]
    edu_prog = get_education_engine(store=store).get_progress_summary()
    ac = edu_prog.get("active_course")
    sp = agile_view.get("active_sprint")
    m_snap = get_model_matrix_snapshot(store=store)
    bg_on = store.is_background_autonomy_enabled()
    unseen_journal = store.get_autonomy_journal(unseen_only=True, mark_seen=False, limit=20)
    waiting_approvals = get_approval_queue(store=store).list_requests(status="waiting")
    return {
        "onboarded": prof.get("onboarded", "false").strip().lower() in ("true", "1", "yes"),
        "companion_tone": prof.get("companion_tone", "balanced").strip().lower() or "balanced",
        "companion_soul": prof.get("companion_soul", ""),
        "user_profile": prof.get("user_profile", ""),
        "data_dir": str(settings.effective_data_dir),
        "trust_mode": get_trust_mode(store=store),
        "privacy_mode": settings.effective_privacy_mode(),
        "background_autonomy": "on" if bg_on else "off",
        "immutable_app": settings.immutable_app,
        "operating_quadrant": describe_operating_quadrant(bg_on),
        "unseen_autonomy_count": len(unseen_journal),
        "provider_count": len(m_snap["providers"]),
        "pending_approvals_count": len(waiting_approvals),
        "pending_approvals": waiting_approvals[:5],
        "background_jobs_active": len(active_bg),
        "hosted_mcp_enabled": prof.get("hosted_mcp_enabled", "off") == "on",
        "session_nudges": nudges[:8],
        "focus_project_id": agile_view["project"]["project_id"],
        "agile_summary": {
            "active_sprint_title": sp["title"] if sp else "",
            "open_cards": len(all_open_items),
            "blocked_count": len(all_blocked_items),
            "aging_count": len(all_blocked_items),
        },
        "education_summary": {
            "active_course_id": ac["course_id"] if ac else "",
            "active_course_title": ac["title"] if ac else "",
            "mastery_pct": ac["mastery_pct"] if ac else 0.0,
            "due_total": edu_prog["due_total"],
            "course_count": edu_prog["course_count"],
        },
        "connected_partners": [
            {
                "partner_id": p["partner_id"],
                "label": p["label"],
                "online": p["online"],
                "latency_ms": p["latency_ms"],
                "peer_profile": p["peer_profile"],
            }
            for p in partners
            if p.get("enabled")
        ],
    }


_SAFE_INLINE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"})


def _safe_disposition(content_type: str, fname: str) -> Tuple[str, str]:
    safe_fn = re.sub(r"[^A-Za-z0-9._-]+", "_", (fname or "asset")).strip("._")[:120] or "asset"
    base_mime = (content_type or "").split(";", 1)[0].strip().lower()
    disp = "inline" if base_mime in _SAFE_INLINE_MIMES and not safe_fn.lower().endswith((".svg", ".html", ".htm", ".xml")) else "attachment"
    return ("Content-Disposition", f'{disp}; filename="{safe_fn}"')


def handle_extended_raw_get(
    route: str, store: MemoryStore,
) -> Optional[Tuple[int, str, bytes, Tuple[Tuple[str, str], ...]]]:
    """Serve sandboxed raw attachment & Wiki diagram bytes (`GET /api/attachments/<id>/raw`, `GET /api/wiki/assets/<name>`) with strict CSP."""
    csp = ("Content-Security-Policy", "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox")
    if route.startswith("/api/wiki/assets/"):
        asset_name = route[len("/api/wiki/assets/") :].strip("/")
        wk_res = read_wiki_asset_raw(get_wiki_engine(store=store), asset_name)
        if wk_res is None:
            return 404, "application/json; charset=utf-8", b'{"ok":false,"error":"Wiki asset not found"}', ()
        content_type, data, fname = wk_res
        return 200, content_type, data, (csp, _safe_disposition(content_type, fname))
    if route.startswith("/api/attachments/") and route.endswith("/raw"):
        att_id = route[len("/api/attachments/") : -len("/raw")].strip("/")
        raw_res = read_attachment_raw(att_id, store=store)
        if raw_res is None:
            return 404, "application/json; charset=utf-8", b'{"ok":false,"error":"Attachment not found"}', ()
        content_type, data, fname = raw_res
        return 200, content_type, data, (csp, _safe_disposition(content_type, fname))
    return None


def handle_extended_get(route: str, store: MemoryStore) -> Optional[Tuple[int, Dict[str, Any]]]:
    """Handle `GET /api/actions`, `GET /api/mcp`, and `GET /api/attachments/<id>`."""
    if route == "/api/actions":
        return 200, {"ok": True, "actions": list_core_actions()}
    if route == "/api/mcp":
        enabled = store.get_user_profile().get("hosted_mcp_enabled", "off") == "on"
        return 200, {"ok": True, "service": "companion-order-counter", "enabled": enabled, "transport": "streamable-http-jsonrpc"}
    if route.startswith("/api/attachments/") and not route.endswith("/raw"):
        att_id = route[len("/api/attachments/") :].strip("/")
        rec = get_attachment_record(att_id, store=store)
        return (200, {"ok": True, "attachment": rec}) if rec else (404, {"ok": False, "error": "Attachment not found"})
    return None


def handle_extended_post(
    route: str, body: Dict[str, Any], headers: Any, store: MemoryStore,
) -> Optional[Tuple[int, Dict[str, Any]]]:
    """Handle `POST /api/mcp`, `POST /api/reload`, `POST /api/attachments`, and `POST /api/actions/<name>`."""
    if route == "/api/mcp":
        auth_hdr = str(headers.get("Authorization") or "")
        return get_mcp_hub(store=store).handle_hosted_mcp_jsonrpc(body, auth_token=auth_hdr, run_jobs_in_background=True)
    if route == "/api/reload":
        if bool(body.get("dry_run")):
            return 200, {"ok": True, "reloading": False, "dry_run": True, "command": build_reload_command()}
        res_rel = request_service_reload()
        return (200 if res_rel.get("ok") else 409), res_rel
    if route == "/api/attachments":
        res = ingest_attachment(
            filename=str(body.get("filename") or ""),
            data_base64=str(body.get("data_base64") or ""),
            mime_type=str(body.get("mime_type") or ""),
            store=store,
        )
        return (200 if res.get("ok") else 400), res
    if route.startswith("/api/actions/"):
        act_name = route[len("/api/actions/") :].strip("/")
        res = execute_core_action(act_name, body, actor="web", store=store)
        return (200 if res.get("ok") else 400), res
    return None


def build_web_status_payload(sess: Any) -> Dict[str, Any]:
    """Build non-blocking live status JSON for `GET /api/status`."""
    prof = {str(k): str(v) for k, v in read_ro_rows(sess.db_path, "SELECT key, value FROM user_profile")}
    wf_engine = WorkflowEngine(store=sess.store)
    active_wf = sess.store.get_active_workflow()
    tasks = [{"id": tid, "description": desc, "status": st, "priority": prio} for tid, desc, st, prio in sess.store.get_pending_tasks()[:10]]
    backlog = [{"id": bid, "description": desc, "category": cat, "status": st} for bid, desc, cat, st in sess.store.get_backlog(status="pending")[:10]]
    ws = settings.get_workspace()
    f_cfg, r_cfg = settings.resolve_tier("fast"), settings.resolve_tier("reasoning")
    busy_sec = int(max(0.0, time.monotonic() - sess._busy_started)) if sess.busy else 0
    try:
        savepoints = get_active_checkpoint_manager().list_git_savepoints(limit=5)
    except Exception:
        savepoints = []
    with sess._jobs_lock:
        active_job = sess._serialize_job(sess._jobs[sess.active_job_id], include_live_spans=True) if sess.active_job_id in sess._jobs else None
    return {
        "ok": True, "csrf_token": sess.csrf_token,
        "companion_name": prof.get("companion_name") or settings.companion_name or "Companion",
        "user_name": prof.get("user_name") or settings.user_name or "Partner",
        "tone": prof.get("companion_tone", "balanced"), "active_model_tier": prof.get("active_model_tier", "auto"),
        "fast_model": f_cfg.model, "reasoning_model": r_cfg.model,
        "workspace": {"project_id": ws.project_id, "project_root": str(ws.project_root), "is_self_project": ws.is_self_project},
        "busy": sess.busy, "active_action": sess.active_action, "busy_seconds": busy_sec,
        "model_swap": active_swap_status(), "active_job": active_job, "lan_url": sess.lan_url, "workflow": active_wf,
        "awaiting_approval": bool((active_wf or {}).get("metadata", {}).get("awaiting_plan_approval") and (active_wf or {}).get("current_state") == "PLAN"),
        "workflow_hud": wf_engine.render_hud(), "pending_tasks": tasks, "pending_backlog": backlog, "recent_savepoints": savepoints,
        **enrich_web_status(sess.store),
    }


def build_telemetry_payload(db_path: Path, limit: int = 12, trace_id: str = "") -> Dict[str, Any]:
    """Build waterfall trace, span, log, and KPI JSON payload for `GET /api/telemetry`."""
    tel = telemetry if db_path == settings.db_path.resolve() else TelemetryEngine(db_path=db_path)
    try:
        kpis = tel.get_kpis()
    finally:
        if tel is not telemetry:
            tel.close()
    cap = max(1, min(int(limit), 50))
    tr_rows = read_ro_rows(db_path, "SELECT trace_id, trace_type, name, status, duration_ms, span_count, error_count, created_at FROM telemetry_traces ORDER BY rowid DESC LIMIT ?", (cap,))
    traces = [{"trace_id": str(tid), "trace_type": str(tt), "name": str(nm), "status": str(st), "duration_ms": round(float(dur or 0.0), 1), "span_count": int(sc or 0), "error_count": int(ec or 0), "created_at": str(ts)} for tid, tt, nm, st, dur, sc, ec, ts in tr_rows]
    chosen_tid = trace_id.strip() or (traces[0]["trace_id"] if traces else "")
    sp_rows = read_ro_rows(db_path, "SELECT id, span_type, name, status, offset_ms, duration_ms, detail FROM telemetry_spans WHERE trace_id = ? ORDER BY id ASC", (chosen_tid,)) if chosen_tid else []
    spans = [{"id": int(sid), "span_type": str(st), "name": str(nm), "status": str(stat), "offset_ms": round(float(off or 0.0), 1), "duration_ms": round(float(dur or 0.0), 1), "detail": str(det)} for sid, st, nm, stat, off, dur, det in sp_rows]
    log_rows = read_ro_rows(db_path, "SELECT id, level, component, trace_id, message, created_at FROM telemetry_logs ORDER BY id DESC LIMIT 30")
    logs = [{"id": int(lid), "level": str(lv), "component": str(cp), "trace_id": str(tid), "message": str(msg), "created_at": str(ts)} for lid, lv, cp, tid, msg, ts in log_rows]
    return {"ok": True, "kpis": kpis, "selected_trace_id": chosen_tid, "traces": traces, "spans": spans, "logs": logs}


def commit_cancelled_turn(
    sess: Any, pre_msgs: List[Dict[str, Any]], clean_msg: str, mode: str, awaiting_approval: bool,
) -> Tuple[int, Dict[str, Any]]:
    """Commit partial streamed text + `*(Stopped by user.)*` cleanly so SQLite and `sess.messages` stay balanced."""
    partial_txt = ""
    with sess._jobs_lock:
        if sess.active_job_id and sess.active_job_id in sess._jobs:
            partial_txt = str(sess._jobs[sess.active_job_id].get("stream_text") or "").rstrip()
    stopped_reply = f"{partial_txt}\n\n*(Stopped by user.)*" if partial_txt else "*(Stopped by user.)*"
    sess.messages[:] = pre_msgs
    sess.messages.extend([{"role": "user", "content": clean_msg}, {"role": "assistant", "content": stopped_reply}])
    tag = "web_plan" if mode == "plan" else ("web_goal" if mode == "goal" else "web_ui")
    last_ua = read_ro_rows(
        sess.db_path,
        "SELECT role FROM conversations WHERE role IN ('user', 'assistant') AND COALESCE(session_tag, '') NOT LIKE 'subagent:%' AND COALESCE(session_tag, '') NOT LIKE 'autonomy:%' ORDER BY id DESC LIMIT 1",
    )
    if not last_ua or str(last_ua[0][0]) != "user":
        sess.store.log_message("user", clean_msg, session_tag=tag)
    sess.store.log_message("assistant", stopped_reply, session_tag=tag)
    return 200, {"ok": True, "cancelled": True, "role": "assistant", "mode": mode, "awaiting_approval": awaiting_approval, "response": stopped_reply}


def cancel_web_or_bg_job(sess: Any, raw_job_id: str = "") -> Tuple[int, Dict[str, Any]]:
    """Abort an active foreground turn (`job_...` / `active`) or background worker (`bgj_...`) in `<100ms`."""
    target_id = (raw_job_id or "").strip()
    if target_id.startswith("bgj_"):
        res = get_job_manager(store=sess.store).cancel_job(target_id)
        return (200 if res.get("ok") else 404), res
    return sess.cancel_active_turn(target_id)


def load_or_create_csrf_token(db_path: Path) -> str:
    """Persist CSRF token in `data/.web_csrf_secret` (`0o600`) so restarts never break open tabs."""
    secret_file = db_path.parent / ".web_csrf_secret"
    try:
        if secret_file.is_file():
            tok = secret_file.read_text(encoding="utf-8").strip()
            if len(tok) >= 20:
                return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(tok, encoding="utf-8")
        if os.name != "nt":
            os.chmod(secret_file, 0o600)
    except OSError:
        pass
    return tok


def build_history_payload(db_path: Path, limit: int = 60, include_tools: bool = False, before_id: int = 0) -> Dict[str, Any]:
    """Return Forever Chat history hydrated with persisted `meta_json` (`thinking`, `tool_steps`, `duration_ms`, `model`)."""
    cap = max(1, min(int(limit), 200))
    clauses = ["COALESCE(session_tag, '') NOT LIKE 'subagent:%'", "COALESCE(session_tag, '') NOT LIKE 'autonomy:%'"]
    if not include_tools:
        clauses.append("role IN ('user', 'assistant')")
    if before_id > 0:
        clauses.append("id < ?")
    where_sql = f"WHERE {' AND '.join(clauses)} "
    rows = read_ro_rows(
        db_path,
        f"SELECT id, role, content, timestamp, COALESCE(session_tag, ''), COALESCE(meta_json, '{{}}') FROM conversations {where_sql}ORDER BY id DESC LIMIT ?",
        (*((int(before_id),) if before_id > 0 else ()), cap + 1),
    )
    items: List[Dict[str, Any]] = []
    for cid, role, content, ts, tag, raw_meta in reversed(rows[:cap]):
        try:
            meta = json.loads(str(raw_meta or "{}"))
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        entry: Dict[str, Any] = {"id": int(cid), "role": str(role), "content": str(content), "timestamp": str(ts), "session_tag": str(tag)}
        if meta.get("thinking"):
            entry["thinking"] = str(meta["thinking"])
        if isinstance(meta.get("tool_steps"), list) and meta["tool_steps"]:
            entry["tool_steps"] = meta["tool_steps"]
        if meta.get("duration_ms"):
            entry["duration_ms"] = meta["duration_ms"]
        if meta.get("model"):
            entry["model"] = str(meta["model"])
        items.append(entry)
    return {"ok": True, "count": len(items), "has_more": len(rows) > cap, "messages": items}


def record_coalesced_job_event(job: Dict[str, Any], ev: Dict[str, Any]) -> None:
    """Update live job stream snapshot and coalesce consecutive delta events so long turns never drop tail events."""
    etype = str(ev.get("type") or "")
    if etype == "text_delta":
        job["stream_text"] = job.get("stream_text", "") + str(ev.get("delta") or "")
    elif etype == "thinking_delta":
        job["stream_thinking"] = job.get("stream_thinking", "") + str(ev.get("delta") or "")
    elif etype == "orphan_think_closed":
        moved = str(ev.get("thinking") or job.get("stream_text") or "")
        job["stream_thinking"] = (job.get("stream_thinking", "") + moved).strip()
        job["stream_text"] = ""
    elif etype == "phase":
        job["active_phase"] = str(ev.get("phase") or "")
    elif etype == "tool_start":
        job["active_tool"] = str(ev.get("summary") or ev.get("tool") or "")
    elif etype == "tool_end":
        job["active_tool"] = ""
        steps = job.setdefault("tool_steps", [])
        steps.append({"tool": str(ev.get("tool") or ""), "summary": str(ev.get("summary") or ev.get("tool") or ""), "ok": bool(ev.get("ok", True)), "duration_ms": ev.get("duration_ms", 0)})
    events: List[Dict[str, Any]] = job["events"]
    if etype in ("text_delta", "thinking_delta") and events and events[-1].get("type") == etype and (len(events) >= 256 or len(str(events[-1].get("delta") or "")) < 64):
        events[-1]["delta"] = str(events[-1].get("delta") or "") + str(ev.get("delta") or "")
    elif len(events) < 8000:
        events.append(dict(ev))


def dispatch_chat_or_job_with_attachments(
    sess: Any, body: Dict[str, Any], is_async: bool,
) -> Tuple[int, Dict[str, Any]]:
    """Prepare optional file/image attachments and invoke `submit_async_chat` or `execute_chat`."""
    msg = str(body.get("message") or "")
    mode = str(body.get("mode") or "chat")
    focus_pid = str(body.get("project_id") or "").strip()
    raw_ids = body.get("attachment_ids")
    att_ids = [str(x) for x in raw_ids] if isinstance(raw_ids, list) else None
    prep = prepare_turn_attachments(msg, att_ids, store=sess.store)
    eff_imgs = prep.wire_image_uris or rehydrate_recent_turn_images(getattr(sess, "messages", []), user_message=msg, store=sess.store, max_images=2)
    with wire_attachment_context(prep.wire_doc_context, eff_imgs):
        if is_async:
            code, res = sess.submit_async_chat(prep.display_message, mode=mode, focus_project_id=focus_pid, followup_doc_note=prep.followup_doc_note)
        else:
            code, res = sess.execute_chat(prep.display_message, mode=mode, focus_project_id=focus_pid, followup_doc_note=prep.followup_doc_note)
    if code < 400 and isinstance(res, dict):
        res.setdefault("display_message", prep.display_message)
    return code, res
