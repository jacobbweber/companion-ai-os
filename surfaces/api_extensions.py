"""
Headless Web API Extensions, Multimodal Attachments & Hosted MCP Route Adapter
(`surfaces/api_extensions.py` — A-Frame Presentation Layer).

Wires Companion 3.0's Shared Core Action Bus (`/api/actions`, `/api/actions/<name>`),
Forever Chat Multimodal File Attachments (`/api/attachments`, `/api/attachments/<id>/raw`),
and Token-Authenticated Inbound Hosted MCP Order Counter (`/api/mcp`) into `CompanionWebRequestHandler`.
"""

from typing import Any, Dict, Optional, Tuple

from companion.agile_boards import MASTER_SPRINT_BOARD_ID, get_agile_manager
from companion.archive_and_approvals import get_approval_queue, get_trust_mode, peek_session_nudges
from companion.attachments import (
    get_attachment_record,
    ingest_attachment,
    prepare_turn_attachments,
    read_attachment_raw,
)
from companion.background_jobs import get_job_manager
from companion.config import settings
from companion.core_actions import execute_core_action, list_core_actions
from companion.education_engine import get_education_engine
from companion.hobbies import HobbyEngine, WebIdleHobbyWatcher, describe_operating_quadrant
from companion.mcp_hub import get_mcp_hub
from companion.memory_store import MemoryStore
from companion.model_registry import get_model_matrix_snapshot
from companion.streaming import wire_attachment_context
from companion.wiki_engine import get_wiki_engine
from companion.wiki_maintenance import read_wiki_asset_raw

__all__ = [
    "create_session_idle_watcher",
    "dispatch_chat_or_job_with_attachments",
    "enrich_web_status",
    "handle_extended_get",
    "handle_extended_post",
    "handle_extended_raw_get",
]


def create_session_idle_watcher(sess: Any, poll_interval_sec: float = 15.0) -> WebIdleHobbyWatcher:
    """Create a `WebIdleHobbyWatcher` bound to `sess.store` that yields immediately to foreground chat or jobs."""

    def _is_web_busy() -> bool:
        if bool(getattr(sess, "busy", False)) or bool(getattr(sess, "active_job_id", "")):
            return True
        jobs = get_job_manager(store=sess.store).list_jobs(limit=15)
        return any(j.get("status") == "running" for j in jobs)

    return WebIdleHobbyWatcher(is_busy_fn=_is_web_busy, engine=HobbyEngine(store=sess.store), poll_interval_sec=poll_interval_sec)


def enrich_web_status(store: MemoryStore) -> Dict[str, Any]:
    """Return Companion 3.0 Harvest status fields merged into `GET /api/status`."""
    prof = store.get_user_profile()
    jobs = get_job_manager(store=store).list_jobs(limit=25)
    active_bg = [j for j in jobs if j["status"] in ("queued", "running", "waiting_approval")]
    partners = get_mcp_hub(store=store).list_partners()
    nudges = peek_session_nudges(session_id="web_ui", store=store) + peek_session_nudges(session_id="main", store=store)
    agile_view = get_agile_manager(store=store).get_board_view(MASTER_SPRINT_BOARD_ID)
    edu_prog = get_education_engine(store=store).get_progress_summary()
    ac = edu_prog.get("active_course")
    sp = agile_view.get("active_sprint")
    m_snap = get_model_matrix_snapshot(store=store)
    hobby_on, self_dev_on = store.is_hobby_mode_enabled(), store.is_self_dev_mode_enabled()
    unseen_hobbies = store.get_hobby_journal(unseen_only=True, mark_seen=False, limit=20)
    waiting_approvals = get_approval_queue(store=store).list_requests(status="waiting")
    return {
        "onboarded": prof.get("onboarded", "false").strip().lower() in ("true", "1", "yes"),
        "companion_tone": prof.get("companion_tone", "balanced").strip().lower() or "balanced",
        "companion_soul": prof.get("companion_soul", ""),
        "user_profile": prof.get("user_profile", ""),
        "data_dir": str(settings.effective_data_dir),
        "trust_mode": get_trust_mode(store=store),
        "privacy_mode": settings.effective_privacy_mode(),
        "self_dev_mode": "on" if self_dev_on else "off",
        "self_dev_enabled": self_dev_on,
        "self_dev_locked": settings.is_self_dev_locked_by_env(),
        "self_dev_lock_reason": settings.self_dev_lock_reason(),
        "operating_quadrant": describe_operating_quadrant(hobby_on, self_dev_on),
        "unseen_hobbies_count": len(unseen_hobbies),
        "provider_count": len(m_snap["providers"]),
        "pending_approvals_count": len(waiting_approvals),
        "pending_approvals": waiting_approvals[:5],
        "background_jobs_active": len(active_bg),
        "hosted_mcp_enabled": prof.get("hosted_mcp_enabled", "off") == "on",
        "session_nudges": nudges[:8],
        "agile_summary": {
            "active_sprint_title": sp["title"] if sp else "",
            "open_cards": sum(1 for c in agile_view["cards"] if c["status"] != "done"),
            "aging_count": len(agile_view["aging_cards"]),
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


def handle_extended_raw_get(
    route: str, store: MemoryStore,
) -> Optional[Tuple[int, str, bytes, Tuple[Tuple[str, str], ...]]]:
    """Serve sandboxed raw attachment & Wiki diagram bytes (`GET /api/attachments/<id>/raw`, `GET /api/wiki/assets/<name>`) with strict CSP."""
    csp = ("Content-Security-Policy", "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'")
    if route.startswith("/api/wiki/assets/"):
        asset_name = route[len("/api/wiki/assets/") :].strip("/")
        wk_res = read_wiki_asset_raw(get_wiki_engine(store=store), asset_name)
        if wk_res is None:
            return 404, "application/json; charset=utf-8", b'{"ok":false,"error":"Wiki asset not found"}', ()
        content_type, data, fname = wk_res
        return 200, content_type, data, (csp, ("Content-Disposition", f'inline; filename="{fname}"'))
    if route.startswith("/api/attachments/") and route.endswith("/raw"):
        att_id = route[len("/api/attachments/") : -len("/raw")].strip("/")
        raw_res = read_attachment_raw(att_id, store=store)
        if raw_res is None:
            return 404, "application/json; charset=utf-8", b'{"ok":false,"error":"Attachment not found"}', ()
        content_type, data, fname = raw_res
        return 200, content_type, data, (csp, ("Content-Disposition", f'inline; filename="{fname}"'))
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
    """Handle `POST /api/mcp`, `POST /api/attachments`, and `POST /api/actions/<name>`."""
    if route == "/api/mcp":
        auth_hdr = str(headers.get("Authorization") or "")
        return get_mcp_hub(store=store).handle_hosted_mcp_jsonrpc(body, auth_token=auth_hdr, run_jobs_in_background=True)
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


def dispatch_chat_or_job_with_attachments(
    sess: Any, body: Dict[str, Any], is_async: bool,
) -> Tuple[int, Dict[str, Any]]:
    """Prepare optional file/image attachments and invoke `submit_async_chat` or `execute_chat`."""
    msg = str(body.get("message") or "")
    mode = str(body.get("mode") or "chat")
    raw_ids = body.get("attachment_ids")
    att_ids = [str(x) for x in raw_ids] if isinstance(raw_ids, list) else None
    prep = prepare_turn_attachments(msg, att_ids, store=sess.store)
    with wire_attachment_context(prep.wire_doc_context, prep.wire_image_uris):
        if is_async:
            return sess.submit_async_chat(prep.display_message, mode=mode)
        return sess.execute_chat(prep.display_message, mode=mode)
