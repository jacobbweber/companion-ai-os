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
from typing import Any, Dict

from companion.core_actions import execute_core_action

__all__ = [
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
        return f"{res.get('summary', '')}\n\nInclude this fenced block verbatim in your reply to render the interactive deck:\n{res['markdown_block']}".strip()
    if "output" in res and res.get("summary"):
        return f"{res['summary']}\n{res['output']}"
    if "output" in res:
        return str(res["output"])
    if "body" in res and "status_code" in res:
        return f"[HTTP {res['status_code']}]\n{res['body']}"
    if res.get("summary"):
        return str(res["summary"])
    return json.dumps(res, indent=2, default=str)


def tool_job_manage(args: Dict[str, Any]) -> str:
    """Dispatch, inspect, cancel, or resume background jobs to keep the primary Forever Chat clean."""
    params = dict(args)
    params["subaction"] = str(args.get("action") or "list").strip().lower()
    return _format_tool_result(execute_core_action("jobs", params, actor="companion"))


def tool_wiki_manage(args: Dict[str, Any]) -> str:
    """Read, search, capture into `01_inbox/`, update, graduate, or export the 2D graph of the Knowledge Wiki."""
    params = dict(args)
    params["subaction"] = str(args.get("action") or "list").strip().lower()
    return _format_tool_result(execute_core_action("wiki", params, actor="companion"))


def tool_vault_manage(args: Dict[str, Any]) -> str:
    """Zero-reveal blind Vault metadata inspector (`list`, `check_secret`, `list_missing`, `declare`, `request_scope`)."""
    sub = str(args.get("action") or "list").strip().lower()
    if sub not in _BLIND_VAULT_ALLOWED_SUBACTIONS:
        return (
            f"error: VAULT ZERO-REVEAL POLICY: Companion tool `vault_manage` only supports symbolic metadata actions "
            f"{sorted(_BLIND_VAULT_ALLOWED_SUBACTIONS)} — raw secret values can never be read, set, or deleted by Companion."
        )
    params = dict(args)
    params["subaction"] = sub
    return _format_tool_result(execute_core_action("vault", params, actor="companion"))


def tool_endpoint_manage(args: Dict[str, Any]) -> str:
    """Register, test, profile, and run bounded commands/requests across `ssh`, `winrm`, and `http_api` endpoints."""
    params = dict(args)
    params["subaction"] = str(args.get("action") or "list").strip().lower()
    return _format_tool_result(execute_core_action("endpoints", params, actor="companion"))


def tool_mcp_manage(args: Dict[str, Any]) -> str:
    """Discover/call outbound MCP servers and handshake/delegate tasks to paired Federated Companion Partners."""
    params = dict(args)
    params["subaction"] = str(args.get("action") or "list").strip().lower()
    return _format_tool_result(execute_core_action("mcp", params, actor="companion"))


def tool_routine_manage(args: Dict[str, Any]) -> str:
    """Manage Respectful Routines, isolated self-dev sandbox clones, saved prompt templates, and pending approvals."""
    sub = str(args.get("action") or "list").strip().lower()
    params = dict(args)
    if sub.startswith("sandbox_"):
        params["subaction"] = sub.removeprefix("sandbox_")
        return _format_tool_result(execute_core_action("sandbox", params, actor="companion"))
    if sub.startswith("prompt_"):
        params["subaction"] = sub.removeprefix("prompt_")
        return _format_tool_result(execute_core_action("prompts", params, actor="companion"))
    if sub == "list_approvals":
        return _format_tool_result(execute_core_action("approvals", {"subaction": "list", "status": "waiting"}, actor="companion"))
    params["subaction"] = sub
    return _format_tool_result(execute_core_action("routines", params, actor="companion"))


def tool_agile_manage(args: Dict[str, Any]) -> str:
    """Operate Multi-Board Kanban, Master Active Sprint, Capacity Slots, Epics, and 15m Unblocker Slicing."""
    params = dict(args)
    params["subaction"] = str(args.get("action") or "board_view").strip().lower()
    return _format_tool_result(execute_core_action("agile", params, actor="companion"))


def tool_calendar_manage(args: Dict[str, Any]) -> str:
    """Operate Unified Calendar timeline across appointments, active sprints, task dates, routines, and SRS study reviews."""
    params = dict(args)
    params["subaction"] = str(args.get("action") or "window").strip().lower()
    return _format_tool_result(execute_core_action("calendar", params, actor="companion"))


def tool_education_manage(args: Dict[str, Any]) -> str:
    """Operate Headless Education courses, pedagogical stages, spaced repetition grading, and inline Chat flashcard decks."""
    params = dict(args)
    params["subaction"] = str(args.get("action") or "progress").strip().lower()
    return _format_tool_result(execute_core_action("education", params, actor="companion"))
