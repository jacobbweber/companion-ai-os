"""
Companion 3.0 Harvest LLM Tool Adapters (`companion/harvest_tools.py` — A-Frame Application Layer).

Thin `actor="companion"` adapters routing Companion's 6 Harvest Pillar tools through `CORE_ACTIONS`:
1. `tool_job_manage`      — Forever-Chat Background Job Dispatcher & Departures Board
2. `tool_wiki_manage`     — Co-Owned 4-Folder Knowledge Wiki Warehouse & 2D Mind Map
3. `tool_vault_manage`    — Blind Zero-Reveal Credential Vault Metadata & Scope Requests
4. `tool_endpoint_manage` — Universal SSH, Windows WinRM & Authenticated HTTP API Connector
5. `tool_mcp_manage`      — Outbound MCP Client, Hosted Order Counter Status & Federated Partners
6. `tool_routine_manage`  — Respectful Routines, Sandbox Clones, Saved Prompts & Approval Board
"""

import json
import shlex
from typing import Any, Dict, List, Optional

from companion.calendar_engine import parse_calendar_slash
from companion.core_actions import execute_core_action
from companion.education_engine import parse_education_slash
from companion.memory_store import MemoryStore
from companion.model_registry import parse_providers_slash

__all__ = [
    "dispatch_harvest_slash",
    "tool_agile_manage",
    "tool_calendar_manage",
    "tool_education_manage",
    "tool_endpoint_manage",
    "tool_job_manage",
    "tool_mcp_manage",
    "tool_routine_manage",
    "tool_vault_manage",
    "tool_wiki_manage",
]

_BLIND_VAULT_ALLOWED_SUBACTIONS = {"list", "check", "check_secret", "metadata", "list_missing", "declare", "request_scope", "grant_scope", "audit"}


def _format_tool_result(res: Dict[str, Any]) -> str:
    if res.get("awaiting_approval"):
        return f"AWAITING_USER_APPROVAL (`{res.get('approval_id', '')}`): {res.get('error') or res.get('summary') or 'Awaiting user approval.'}"
    if not res.get("ok", False):
        return f"error: {res.get('error') or res.get('summary') or 'Operation failed.'}"
    if res.get("markdown_block"):
        return (
            f"{res.get('summary', '')}\n\n"
            "[INTERNAL NOTE FOR ASSISTANT — DO NOT ASK THE USER TO COPY ANYTHING: "
            "Companion's Chat UI automatically renders the fenced block below as a live interactive widget in your reply.]\n"
            f"{res['markdown_block']}"
        ).strip()
    if "output" in res and res.get("summary"):
        return f"{res['summary']}\n{res['output']}"
    if "output" in res:
        return str(res["output"])
    if "body" in res and "status_code" in res:
        return f"[HTTP {res['status_code']}]\n{res['body']}"
    if res.get("summary"):
        return str(res["summary"])
    return json.dumps(res, indent=2, default=str)


def _clean_agent_params(args: Dict[str, Any], subaction: str) -> Dict[str, Any]:
    params = {k: v for k, v in args.items() if k != "confirmed"}
    params["subaction"] = subaction
    return params


def tool_job_manage(args: Dict[str, Any]) -> str:
    """Dispatch, inspect, cancel, or resume background jobs to keep the primary Forever Chat clean."""
    return _format_tool_result(execute_core_action("jobs", _clean_agent_params(args, str(args.get("action") or "list").strip().lower()), actor="companion"))


def tool_wiki_manage(args: Dict[str, Any]) -> str:
    """Read, search, capture into `01_inbox/`, update, graduate, or export the 2D graph of the Knowledge Wiki."""
    return _format_tool_result(execute_core_action("wiki", _clean_agent_params(args, str(args.get("action") or "list").strip().lower()), actor="companion"))


def tool_vault_manage(args: Dict[str, Any]) -> str:
    """Zero-reveal blind Vault metadata inspector (`list`, `check_secret`, `list_missing`, `declare`, `request_scope`)."""
    sub = str(args.get("action") or "list").strip().lower()
    if sub not in _BLIND_VAULT_ALLOWED_SUBACTIONS:
        return (
            f"error: VAULT ZERO-REVEAL POLICY: Companion tool `vault_manage` only supports symbolic metadata actions "
            f"{sorted(_BLIND_VAULT_ALLOWED_SUBACTIONS)} — raw secret values can never be read, set, or deleted by Companion."
        )
    return _format_tool_result(execute_core_action("vault", _clean_agent_params(args, sub), actor="companion"))


def tool_endpoint_manage(args: Dict[str, Any]) -> str:
    """Register, test, profile, and run bounded commands/requests across `ssh`, `winrm`, and `http_api` endpoints."""
    return _format_tool_result(execute_core_action("endpoints", _clean_agent_params(args, str(args.get("action") or "list").strip().lower()), actor="companion"))


def tool_mcp_manage(args: Dict[str, Any]) -> str:
    """Discover/call outbound MCP servers and handshake/delegate tasks to paired Federated Companion Partners."""
    return _format_tool_result(execute_core_action("mcp", _clean_agent_params(args, str(args.get("action") or "list").strip().lower()), actor="companion"))


def tool_routine_manage(args: Dict[str, Any]) -> str:
    """Manage Unified `TriggerPolicy` Routines, Background Autonomy, saved prompts, and pending approvals."""
    sub = str(args.get("action") or "list").strip().lower()
    if sub in ("autonomy_status", "autonomy_on", "autonomy_off", "morning"):
        auto_sub = sub.removeprefix("autonomy_")
        return _format_tool_result(execute_core_action("autonomy", _clean_agent_params(args, auto_sub), actor="companion"))
    if sub.startswith("prompt_"):
        return _format_tool_result(execute_core_action("prompts", _clean_agent_params(args, sub.removeprefix("prompt_")), actor="companion"))
    if sub == "list_approvals":
        return _format_tool_result(execute_core_action("approvals", {"subaction": "list", "status": "waiting"}, actor="companion"))
    return _format_tool_result(execute_core_action("routines", _clean_agent_params(args, sub), actor="companion"))


def tool_agile_manage(args: Dict[str, Any]) -> str:
    """Operate Multi-Board Kanban, Master Active Sprint, Capacity Slots, Epics, and 15m Unblocker Slicing."""
    return _format_tool_result(execute_core_action("agile", _clean_agent_params(args, str(args.get("action") or "board_view").strip().lower()), actor="companion"))


def tool_calendar_manage(args: Dict[str, Any]) -> str:
    """Operate Unified Calendar timeline across appointments, active sprints, task dates, routines, and SRS study reviews."""
    return _format_tool_result(execute_core_action("calendar", _clean_agent_params(args, str(args.get("action") or "window").strip().lower()), actor="companion"))


def tool_education_manage(args: Dict[str, Any]) -> str:
    """Operate Headless Education courses, pedagogical stages, spaced repetition grading, and inline Chat flashcard decks."""
    return _format_tool_result(execute_core_action("education", _clean_agent_params(args, str(args.get("action") or "progress").strip().lower()), actor="companion"))


def _parse_agile_slash(verb: str, parts: List[str]) -> Dict[str, Any]:
    if verb == "/boards":
        return {"subaction": "list_boards"}
    if verb == "/sprint":
        if parts and parts[0].lower() == "create" and len(parts) >= 2:
            return {"subaction": "create_sprint", "title": " ".join(parts[1:])}
        return {"subaction": "board_view"}
    if parts and parts[0].lower() == "add" and len(parts) >= 2:
        return {"subaction": "create_card", "title": " ".join(parts[1:])}
    return {"subaction": "roadmap"} if (parts and parts[0].lower() == "roadmap") else {"subaction": "board_view", "project_id": parts[0] if parts else ""}


def dispatch_harvest_slash(verb: str, arg: str, store: Optional[MemoryStore] = None) -> str:
    """Unified CLI/REPL slash handler routing `/jobs`, `/wiki`, `/vault`, `/endpoints`, `/mcp`, `/partners`, etc."""
    v, parts = verb.strip().lower(), (shlex.split(arg) if arg.strip() else [])
    sub, rest = (parts[0].lower() if parts else "list"), parts[1:]
    act_name = v.lstrip("/")
    if v == "/trust":
        p: Dict[str, Any] = {"subaction": "set", "mode": sub} if parts else {"subaction": "get"}
    elif v == "/approvals":
        p = {"subaction": sub, "approval_id": rest[0] if rest else "", "note": " ".join(rest[1:])} if sub in ("approve", "deny") else {"subaction": "list", "status": sub if parts else "waiting"}
    elif v == "/jobs":
        p = {"subaction": "dispatch", "goal": " ".join(rest)} if sub == "dispatch" else ({"subaction": sub, "job_id": rest[0] if rest else ""} if sub in ("status", "cancel", "resume") else {"subaction": "list", "status": sub if parts and sub != "list" else ""})
    elif v == "/wiki":
        if sub in ("read", "trash", "restore", "inspect", "verify", "verify_note", "diff"):
            mapped = "inspect_graduation" if sub == "inspect" else ("verify_note" if sub == "verify" else sub)
            p = {"subaction": mapped, "path": rest[0] if rest else "", "compare_to": rest[1] if len(rest) > 1 else ""}
        elif sub in ("stale", "list_stale", "consolidate", "consolidate_ontology"):
            p = {"subaction": "list_stale" if sub in ("stale", "list_stale") else "consolidate_ontology", "aliases": " ".join(rest)}
        elif sub in ("templates", "list_templates"):
            p = {"subaction": "read_template", "template": rest[0]} if rest else {"subaction": "list_templates"}
        elif sub in ("history", "curation_history"):
            p = {"subaction": "curation_history"}
        elif sub in ("revert", "revert_curation"):
            p = {"subaction": "revert_curation", "tx_id": rest[0] if rest else ""}
        elif sub == "search":
            p = {"subaction": "search", "query": " ".join(rest)}
        elif sub == "capture":
            p = {"subaction": "capture_inbox", "title": rest[0] if rest else "Quick Note", "content": " ".join(rest[1:]) if len(rest) > 1 else (rest[0] if rest else "")}
        elif sub == "graduate":
            p = {"subaction": "graduate", "path": rest[0] if rest else "", "domain": rest[1] if len(rest) > 1 else "general", "topic": rest[2] if len(rest) > 2 else ""}
        else:
            p = {"subaction": sub if sub in ("graph", "reindex", "export_chat", "tag_authority", "list_trash", "list") else "list", "folder": sub if sub.startswith("0") else ""}
    elif v == "/vault":
        if sub == "set" and len(rest) >= 3:
            p = {"subaction": "set", "name": rest[0], "cred_type": rest[1], "value": " ".join(rest[2:])}
        elif sub in ("delete", "check") and rest:
            p = {"subaction": sub, "name": rest[0]}
        elif sub == "grant" and len(rest) >= 2:
            p = {"subaction": "grant_scope", "name": rest[0], "caller_scope": rest[1]}
        else:
            p = {"subaction": sub if sub in ("list", "list_missing", "audit") else "list"}
    elif v == "/backup":
        p = {"subaction": "create", "label": rest[0] if rest else "manual"} if sub == "create" else ({"subaction": "restore", "snapshot_id": rest[0] if rest else ""} if sub == "restore" else {"subaction": "list"})
    elif v == "/endpoints":
        p = {"subaction": "register", "endpoint_id": rest[0], "protocol": rest[1], "host": rest[2], "credential_ref": rest[3] if len(rest) > 3 else ""} if sub == "add" and len(rest) >= 3 else ({"subaction": sub, "endpoint_id": rest[0]} if sub in ("test", "inspect_host", "remove") and rest else {"subaction": "list"})
    elif v in ("/mcp", "/partners"):
        act_name = "mcp"
        if v == "/partners":
            p = {"subaction": "register_partner", "partner_id": rest[0], "url": rest[1], "credential_ref": rest[2] if len(rest) > 2 else ""} if sub == "add" and len(rest) >= 2 else ({"subaction": "handshake_partner", "partner_id": rest[0]} if sub in ("handshake", "ping") and rest else {"subaction": "list_partners"})
        elif sub in ("discover", "approve_server", "remove_server", "harvest_skill") and rest:
            p = {"subaction": sub, "server_id": rest[0]}
        elif sub == "hosted":
            p = {"subaction": "configure_hosted", "enabled": (rest[0].lower() == "on") if rest else True, "token_ref": rest[1] if len(rest) > 1 else "hosted-mcp-token"}
        else:
            p = {"subaction": "list"}
    elif v == "/routines":
        p = {"subaction": "tick"} if sub == "tick" else ({"subaction": "run_now", "routine_id": rest[0]} if sub == "run" and rest else ({"subaction": "delete", "routine_id": rest[0]} if sub == "delete" and rest else {"subaction": "list"}))
    elif v == "/prompts":
        p = {"subaction": "save", "name": rest[0], "template": " ".join(rest[1:])} if sub == "save" and len(rest) >= 2 else ({"subaction": "delete", "name": rest[0]} if sub == "delete" and rest else {"subaction": "list"})
    elif v == "/archive":
        p = {"subaction": "run", "older_than_days": int(rest[0]) if rest else 90} if sub == "run" else {"subaction": "search", "query": arg}
    elif v in ("/board", "/boards", "/sprint"):
        act_name, p = "agile", _parse_agile_slash(v, parts)
    elif v in ("/calendar", "/study", "/providers"):
        slash_map = {"/calendar": ("calendar", parse_calendar_slash), "/study": ("education", parse_education_slash), "/providers": ("models", parse_providers_slash)}
        act_name, fn = slash_map[v]
        p = fn(parts)
    else:
        act_name, p = "identity", {"subaction": "get"}
    res = execute_core_action(act_name, p, actor="cli", store=store)
    if res.get("markdown_block"):
        return f"{res.get('summary', '')}\n\n{res['markdown_block']}".strip()
    if res.get("summary") and res.get("output"):
        return f"{res['summary']}\n{res['output']}"
    return str(res.get("summary") or res.get("output") or res.get("error") or json.dumps(res, indent=2))
