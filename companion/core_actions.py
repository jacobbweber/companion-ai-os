"""Shared Core Action Bus (`companion/core_actions.py` — A-Frame Application Layer).
Canonical `CORE_ACTIONS` registry with `actor` provenance (`user`, `cli`, `web`, `companion`),
1-sentence error normalization, and Vault scrubbing across CLI, REPL, Web API, and LLM tools.
"""

import json
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from companion.archive_and_approvals import (
    TRUST_MODES, archive_aged_conversations, current_session_id, delete_prompt_template, dismiss_session_nudges,
    format_forever_search_results, get_approval_queue, get_trust_mode, list_prompt_templates, render_prompt_template,
    save_prompt_template, search_forever_history, set_trust_mode,
)
from companion.background_jobs import get_job_manager
from companion.calendar_engine import execute_calendar_action, parse_calendar_slash
from companion.config import is_in_agent_tool_turn, settings
from companion.context import execute_identity_action
from companion.dag_runtime import resume_dag_run
from companion.education_engine import execute_education_action, parse_education_slash
from companion.endpoints import get_endpoint_manager
from companion.mcp_hub import format_connected_partners_hud, get_mcp_hub, harvest_mcp_capability_pack
from companion.memory_store import MemoryStore, get_memory_store, memory_store_context
from companion.model_registry import execute_model_registry_action, parse_providers_slash
from companion.project_graph import get_project_graph
from companion.routines import (
    cleanup_sandbox_clone, create_sandbox_clone, diff_sandbox_clone, execute_autonomy_mode_action,
    get_routine_manager, list_sandbox_clones, parse_selfdev_slash, verify_sandbox_clone,
)
from companion.vault import create_snapshot_backup, get_vault_manager, list_snapshot_backups, restore_snapshot_backup
from companion.wiki_curation import commit_curation_plan, inspect_graduation, list_curation_history, revert_curation, validate_curation_plan
from companion.wiki_engine import get_wiki_engine
from companion.wiki_maintenance import execute_wiki_maintenance_subaction
from companion.wiki_templates import get_template, list_templates, load_tag_authority, save_template

__all__ = ["CORE_ACTIONS", "HARVEST_SLASH_VERBS", "ActionSpec", "execute_core_action", "handle_harvest_slash", "list_core_actions"]


@dataclass(frozen=True)
class ActionSpec:
    """Declarative metadata + handler for a unified Core Action."""

    name: str
    domain: str
    description: str
    user_only: bool
    handler: Callable[[Dict[str, Any], str, MemoryStore], Dict[str, Any]]


def _as_bool(val: Any, default: bool = False) -> bool:
    return val if isinstance(val, bool) else (default if val is None else str(val).strip().lower() in ("1", "true", "yes", "on"))


def _human_confirmed(p: Dict[str, Any], actor: str) -> bool:
    """Only honor `confirmed=True` when invoked directly by a human (`user`, `cli`, `web`) outside an LLM tool turn."""
    return _as_bool(p.get("confirmed")) and actor.strip().lower() in ("user", "cli", "web") and not is_in_agent_tool_turn()


def _as_list(val: Any) -> Optional[List[str]]:
    return None if val is None else ([str(x).strip() for x in val if str(x).strip()] if isinstance(val, list) else [s.strip() for s in str(val).split(",") if s.strip()])


def _act_trust(p: Dict[str, Any], actor: str, mem: MemoryStore) -> Dict[str, Any]:
    sub = str(p.get("subaction") or ("set" if p.get("mode") else "get")).lower()
    if sub == "set":
        return set_trust_mode(str(p.get("mode") or ""), actor=actor, store=mem)
    cur = get_trust_mode(store=mem)
    return {"ok": True, "trust_mode": cur, "modes": TRUST_MODES, "summary": f"Active Trust Mode: `{cur}` — {TRUST_MODES[cur]}"}


def _apply_wiki_approved(act: str, payload: Dict[str, Any], mem: MemoryStore) -> Optional[str]:
    wk = get_wiki_engine(store=mem)
    if act == "graduate_note" and payload.get("from") and payload.get("to"):
        to_parts = [x for x in str(payload["to"]).split("/") if x]
        dom, top = (to_parts[1] if len(to_parts) >= 3 else "general"), (to_parts[2] if len(to_parts) >= 4 else "")
        slug = to_parts[-1].removesuffix(".md") if to_parts else ""
        r = wk.graduate_note(str(payload["from"]), domain=dom, topic=top, new_slug=slug, actor="user", confirmed=True, template=str(payload.get("template") or ""), summary=str(payload.get("summary") or ""), ontology=_as_list(payload.get("ontology")))
        return r.get("summary") if r.get("ok") else None
    if act == "commit_curation" and payload.get("curation_id"):
        r = commit_curation_plan(wk, str(payload["curation_id"]), actor="user", confirmed=True)
        return r.get("summary") if r.get("ok") else None
    if act == "revert_curation" and payload.get("tx_id"):
        r = revert_curation(wk, str(payload["tx_id"]), actor="user", confirmed=True)
        return r.get("summary") if r.get("ok") else None
    if act == "edit_user_note" and payload.get("path") and "content" in payload:
        r = wk.save_or_update_note(str(payload["path"]), str(payload["content"]), title=str(payload.get("title") or ""), tags=_as_list(payload.get("tags")), actor="companion", confirmed=True, summary=str(payload.get("summary") or ""), template=str(payload.get("template") or ""), ontology=_as_list(payload.get("ontology")))
        return r.get("summary") if r.get("ok") else None
    if act == "trash_note" and payload.get("path"):
        r = wk.trash_note(str(payload["path"]), actor="user", confirmed=True)
        return r.get("summary") if r.get("ok") else None
    return None


def _apply_approved_request(req: Dict[str, Any], mem: MemoryStore) -> Optional[str]:
    """Immediately execute structured pending actions (`wiki_edit`, `vault_scope`, `endpoint_*`, `mcp_server`, `dag_gate`, `routine_enable`) upon user approval."""
    cat, act = str(req.get("category") or ""), str(req.get("action_name") or "")
    try:
        payload = json.loads(req.get("payload_json") or "{}")
    except Exception:
        payload = {}
    if cat == "wiki_edit":
        return _apply_wiki_approved(act, payload, mem)
    sec_name = str(payload.get("name") or payload.get("secret") or "")
    if cat == "vault_scope" and act == "grant_scope" and sec_name and payload.get("caller_scope"):
        r = get_vault_manager(store=mem).grant_scope(sec_name, str(payload["caller_scope"]), actor="user", confirmed=True)
        return r.get("summary") if r.get("ok") else None
    if cat == "endpoint_register" and act == "register_endpoint" and payload.get("endpoint_id"):
        r = get_endpoint_manager(store=mem).register_endpoint(
            endpoint_id=str(payload["endpoint_id"]), protocol=str(payload.get("protocol") or ""), host=str(payload.get("host") or ""),
            label=str(payload.get("label") or ""), port=int(payload.get("port") or 0), username=str(payload.get("username") or ""),
            auth_mode=str(payload.get("auth_mode") or ""), credential_ref=str(payload.get("credential_ref") or ""),
            auth_header=str(payload.get("auth_header") or "X-API-Key"), os_hint=str(payload.get("os_hint") or ""),
            verify_tls=_as_bool(payload.get("verify_tls"), True), tags=_as_list(payload.get("tags")), actor="user", confirmed=True,
        )
        return r.get("summary") if r.get("ok") else None
    if cat == "endpoint_exec" and payload.get("endpoint_id"):
        em, eid = get_endpoint_manager(store=mem), str(payload["endpoint_id"])
        if act == "ssh_exec" and payload.get("command"):
            r = em.ssh_exec(eid, str(payload["command"]), actor="user", confirmed=True)
            return str(r.get("output") or r.get("summary") or "SSH command executed.") if r.get("ok") else None
        if act == "winrm_exec" and payload.get("command"):
            r = em.winrm_exec(eid, str(payload["command"]), actor="user", confirmed=True)
            return str(r.get("output") or r.get("summary") or "WinRM command executed.") if r.get("ok") else None
        if act.startswith("http_"):
            verb = str(payload.get("method") or act.removeprefix("http_").upper())
            r = em.api_request(eid, method=verb, path=str(payload.get("path") or "/"), json_body=payload.get("json_body") if isinstance(payload.get("json_body"), dict) else None, actor="user", confirmed=True)
            return f"HTTP {r.get('status_code')}" if r.get("ok") else None
    if cat == "mcp_server":
        hub = get_mcp_hub(store=mem)
        if act == "register_mcp_server" and payload.get("server_id"):
            sid = str(payload["server_id"])
            r = hub.register_server(
                server_id=sid, transport=str(payload.get("transport") or "stdio"),
                label=str(payload.get("label") or ""), command=str(payload.get("command") or ""),
                args=_as_list(payload.get("args")), url=str(payload.get("url") or ""),
                credential_ref=str(payload.get("credential_ref") or ""), allowed_tools=_as_list(payload.get("allowed_tools")),
                actor="user", confirmed=True,
            )
            disc = hub.discover_server(sid, actor="user") if r.get("ok") else {}
            return (disc.get("summary") if disc.get("ok") else r.get("summary")) if r.get("ok") else None
        if act == "approve_mcp_tools" and payload.get("server_id"):
            r = hub.approve_server(str(payload["server_id"]), actor="user")
            return r.get("summary") if r.get("ok") else None
        if act == "enable_hosted_mcp":
            r = hub.configure_hosted_mcp(True, token_ref=str(payload.get("token_ref") or "hosted-mcp-token"), allow_wiki_read=_as_bool(payload.get("allow_wiki_read")), actor="user", confirmed=True)
            return r.get("summary") if r.get("ok") else None
    if cat == "dag_gate" and act in ("approve_dag_node", "resume_dag_run") and payload.get("run_id"):
        r = resume_dag_run(str(payload["run_id"]), approved_node_id=str(payload.get("node_id") or ""), store=mem)
        return str(r.get("summary") or f"Resumed DAG `{payload['run_id']}`.") if r.get("ok") or r.get("status") == "waiting_approval" else None
    if cat == "routine_enable" and act == "enable_routine" and payload.get("routine_id"):
        r = get_routine_manager(store=mem).create_or_update_routine(
            routine_id=str(payload["routine_id"]), schedule_expr=str(payload.get("schedule") or ""),
            target_kind=str(payload.get("target_kind") or ""), title=str(payload.get("title") or ""),
            target_name=str(payload.get("target_name") or ""), inputs=payload.get("inputs") if isinstance(payload.get("inputs"), dict) else None,
            trigger_policy=payload.get("trigger_policy") if isinstance(payload.get("trigger_policy"), dict) else None,
            enabled=True, catch_up=_as_bool(payload.get("catch_up")), actor="user", confirmed=True,
        )
        return r.get("summary") if r.get("ok") else None
    return None


def _act_approvals(p: Dict[str, Any], actor: str, mem: MemoryStore) -> Dict[str, Any]:
    aq = get_approval_queue(store=mem)
    sub = str(p.get("subaction") or "list").lower()
    if sub in ("dismiss_nudges", "clear_nudges", "mark_nudges_seen"):
        raw_ids = p.get("nudge_ids") if isinstance(p.get("nudge_ids"), list) else ([p["nudge_id"]] if p.get("nudge_id") is not None else None)
        return dismiss_session_nudges(nudge_ids=[int(x) for x in raw_ids] if raw_ids else None, category=str(p.get("category") or ""), store=mem)
    if sub in ("approve", "deny", "reject", "resolve", "decide"):
        dec = str(p.get("decision") or ("approve" if sub == "approve" else "deny"))
        res = aq.resolve(str(p.get("approval_id") or ""), decision=dec, actor=actor, note=str(p.get("note") or ""))
        if res.get("ok") and dec.strip().lower() in ("approve", "approved", "yes", "allow"):
            if _as_bool(p.get("apply_action"), True):
                applied_msg = _apply_approved_request(res.get("approval") or {}, mem)
                if applied_msg:
                    aq.mark_executed(str((res.get("approval") or {}).get("approval_id") or p.get("approval_id") or ""))
                    res["applied_summary"] = applied_msg
                    res["summary"] = f"{res['summary']} -> Executed: {applied_msg}"
            if _as_bool(p.get("resume_job"), True):
                for j in get_job_manager(store=mem).list_jobs(status="waiting_approval", limit=20):
                    if j.get("approval_id") == p.get("approval_id"):
                        get_job_manager(store=mem).resume_job(j["job_id"])
        return res
    st = str(p.get("status") or "waiting")
    return {"ok": True, "approvals": aq.list_requests(status=st), "summary": aq.format_board(status=st)}


def _act_archive(p: Dict[str, Any], _actor: str, mem: MemoryStore) -> Dict[str, Any]:
    sub = str(p.get("subaction") or "search").lower()
    if sub == "run":
        return archive_aged_conversations(store=mem, older_than_days=int(p.get("older_than_days") or 90), keep_min_hot=int(p.get("keep_min_hot", 80)))
    q = str(p.get("query") or "")
    hits = search_forever_history(query=q, store=mem, limit=int(p.get("limit") or 15), time_window=p.get("time_window"))
    return {"ok": True, "hits": hits, "summary": format_forever_search_results(hits, query=q)}


def _act_prompts(p: Dict[str, Any], _actor: str, mem: MemoryStore) -> Dict[str, Any]:
    sub = str(p.get("subaction") or "list").lower()
    if sub == "save":
        return save_prompt_template(str(p.get("name") or ""), str(p.get("template") or ""), description=str(p.get("description") or ""), category=str(p.get("category") or "general"), store=mem)
    if sub == "render":
        return render_prompt_template(str(p.get("name") or ""), variables=p.get("variables") if isinstance(p.get("variables"), dict) else {}, store=mem)
    if sub == "delete":
        return delete_prompt_template(str(p.get("name") or ""), store=mem)
    items = list_prompt_templates(category=p.get("category"), store=mem)
    lines = ["=== Saved Prompts Library ==="] + ([f"  * `{i['name']}` [{i['category']}] — {i['description'] or i['template'][:80]}" for i in items] or ["  (No saved prompt templates yet.)"])
    return {"ok": True, "prompts": items, "summary": "\n".join(lines)}


def _act_jobs(p: Dict[str, Any], _actor: str, mem: MemoryStore) -> Dict[str, Any]:
    jm = get_job_manager(store=mem)
    sub = str(p.get("subaction") or "list").lower()
    if sub == "dispatch":
        return jm.dispatch_job(
            goal=str(p.get("goal") or ""), title=str(p.get("title") or ""), kind=str(p.get("kind") or "worker"),
            starter=str(p.get("starter") or "chat"), session_id=str(p.get("session_id") or current_session_id() or "main"),
            tier=str(p.get("tier") or "fast"), allowed_tools=_as_list(p.get("allowed_tools")),
            max_iters=int(p.get("max_iters") or 8), timeout_sec=int(p.get("timeout_sec") or 600),
            run_in_background=_as_bool(p.get("run_in_background"), True),
            dag_name=str(p.get("dag_name") or p.get("recipe_name") or ""),
            dag_params=p.get("dag_params") if isinstance(p.get("dag_params"), dict) else (p.get("recipe_params") if isinstance(p.get("recipe_params"), dict) else None),
        )
    if sub in ("status", "get", "result"):
        row = jm.get_job(str(p.get("job_id") or ""))
        return {"ok": False, "error": f"Background job `{p.get('job_id')}` not found."} if not row else {"ok": True, "job": row, "summary": f"`{row['job_id']}` [{row['status'].upper()}] {row['title']}\n{row.get('envelope_summary') or row.get('partial_output') or row.get('error') or '(in progress)'}"}
    if sub == "cancel":
        return jm.cancel_job(str(p.get("job_id") or ""))
    if sub == "resume":
        return jm.resume_job(str(p.get("job_id") or ""), run_in_background=_as_bool(p.get("run_in_background"), True))
    lim = int(p.get("limit") or 15)
    return {"ok": True, "jobs": jm.list_jobs(status=str(p.get("status") or ""), limit=lim), "summary": jm.format_departures_board(limit=lim)}


def _act_wiki_templates_and_curation(wk: Any, sub: str, p: Dict[str, Any], actor: str, mem: MemoryStore) -> Optional[Dict[str, Any]]:
    if sub == "list_templates":
        tpls = list_templates(wk.root)
        return {"ok": True, "templates": tpls, "summary": "\n".join([f"=== Governed Wiki Templates ({len(tpls)}) ==="] + [f"  * `{t['slug']}` [{t['document_type']}] sections={t['required_sections']} — {t['when_to_use']}" for t in tpls])}
    if sub == "read_template":
        res = get_template(wk.root, str(p.get("template") or p.get("slug") or p.get("path") or ""))
        if res.get("ok"):
            t = res["template"]
            res["summary"] = f"Template `{t['slug']}` ({t['title']}) — required_sections={t['required_sections']}\nWhen to use: {t['when_to_use']}\n\n{t['skeleton']}"
        return res
    if sub == "save_template":
        return save_template(wk.root, str(p.get("slug") or p.get("template") or ""), str(p.get("title") or ""), str(p.get("document_type") or "reference"), str(p.get("when_to_use") or ""), _as_list(p.get("required_sections")) or [], str(p.get("content") or p.get("skeleton") or ""))
    if sub == "tag_authority":
        auth = load_tag_authority(wk.root)
        return {**auth, "summary": f"Tag & Ontology Authority: domains={auth['domains']}, ontology={auth['ontology']}"}
    if sub == "inspect_graduation":
        return inspect_graduation(wk, p.get("inbox_paths") or p.get("path") or "")
    if sub == "validate_curation":
        return validate_curation_plan(wk, str(p.get("curation_id") or ""), str(p.get("strategy") or "CREATE_NEW"), outputs=p.get("outputs") or p.get("outputs_json"), rationale=str(p.get("rationale") or ""), allow_condense=_as_bool(p.get("allow_condense")), fallback_fields=p)
    if sub == "commit_curation":
        return commit_curation_plan(wk, str(p.get("curation_id") or ""), actor=actor, confirmed=_human_confirmed(p, actor), plan_override=p.get("plan_override") if isinstance(p.get("plan_override"), dict) else None)
    if sub == "curation_history":
        return list_curation_history(wk, limit=int(p.get("limit") or 25))
    if sub == "revert_curation":
        return revert_curation(wk, str(p.get("tx_id") or p.get("path") or ""), actor=actor, confirmed=_human_confirmed(p, actor))
    if sub == "create_proxy_note":
        return wk.create_proxy_note(asset_path=str(p.get("asset_path") or p.get("path") or ""), title=str(p.get("title") or ""), executive_summary=str(p.get("summary") or p.get("content") or ""), highlights=str(p.get("highlights") or ""), mime_type=str(p.get("mime_type") or ""), size_label=str(p.get("size_label") or ""), sha256=str(p.get("sha256") or ""), tags=_as_list(p.get("tags")), ontology=_as_list(p.get("ontology")), related_links=_as_list(p.get("related")), actor=actor)
    if sub == "deep_curate_job":
        target_p = str(p.get("path") or "").strip() or "all staged notes in `01_inbox/`"
        goal = f"Deliberately curate {target_p} into `02_knowledge/` using `inspect_graduation`, `validate_curation`, and `commit_curation`."
        return get_job_manager(store=mem).dispatch_job(goal=goal, title=f"Deep Wiki Curation: {target_p[:48]}", kind="worker", starter="wiki_studio", session_id=str(p.get("session_id") or current_session_id() or "main"), tier="reasoning", allowed_tools=["wiki_manage"], max_iters=16, run_in_background=_as_bool(p.get("run_in_background"), True))
    return None


def _act_wiki(p: Dict[str, Any], actor: str, mem: MemoryStore) -> Dict[str, Any]:
    wk = get_wiki_engine(store=mem)
    sub = str(p.get("subaction") or "list").lower()
    if cur_res := _act_wiki_templates_and_curation(wk, sub, p, actor, mem):
        return cur_res
    if maint_res := execute_wiki_maintenance_subaction(wk, sub, p, actor):
        return maint_res
    if sub == "capture_inbox":
        return wk.capture_inbox(title=str(p.get("title") or ""), content=str(p.get("content") or ""), tags=_as_list(p.get("tags")), source=str(p.get("source") or actor), slug=str(p.get("slug") or ""), actor=actor, template=str(p.get("template") or ""), summary=str(p.get("summary") or ""), ontology=_as_list(p.get("ontology")), domain_hint=str(p.get("domain") or ""))
    if sub in ("read", "read_note"):
        res = wk.read_note(str(p.get("path") or ""))
        if res.get("ok"):
            res["note_summary"] = str(res.get("note_summary") or res.get("summary") or "")
            res["summary"] = f"`{res['path']}` (`author: {res['author']}`, tpl=`{res.get('template', '')}`, hash=`{res['content_hash']}`, ontology={res.get('ontology', [])}, backlinks={res['backlinks']})\n\n{res['raw']}"
        return res
    if sub in ("save", "update", "save_note"):
        return wk.save_or_update_note(rel_path=str(p.get("path") or ""), content=str(p.get("content") or ""), title=str(p.get("title") or ""), tags=_as_list(p.get("tags")), actor=actor, expected_hash=str(p.get("expected_hash") or ""), confirmed=_human_confirmed(p, actor), summary=str(p.get("summary") or ""), template=str(p.get("template") or ""), ontology=_as_list(p.get("ontology")), status=str(p.get("status") or ""))
    if sub == "graduate":
        return wk.graduate_note(inbox_path=str(p.get("path") or ""), domain=str(p.get("domain") or ""), topic=str(p.get("topic") or ""), new_slug=str(p.get("new_slug") or ""), actor=actor, confirmed=_human_confirmed(p, actor), template=str(p.get("template") or ""), summary=str(p.get("summary") or ""), ontology=_as_list(p.get("ontology")))
    if sub == "trash":
        return wk.trash_note(rel_path=str(p.get("path") or ""), actor=actor, confirmed=_human_confirmed(p, actor))
    if sub == "restore":
        return wk.restore_note(str(p.get("path") or p.get("trash_id") or ""))
    if sub == "list_trash":
        mf = wk.ensure_warehouse() / "trash" / "trash_manifest.json"
        entries = json.loads(mf.read_text(encoding="utf-8")) if mf.is_file() else {}
        items = [{"trash_id": tid, **info} for tid, info in reversed(list(entries.items()))]
        return {"ok": True, "trash": items, "summary": f"Trash holds {len(items)} restorable note(s)."}
    if sub == "search":
        hits = wk.search_notes(str(p.get("query") or ""), limit=int(p.get("limit") or 10), include_archive=_as_bool(p.get("include_archive")), include_ontology_bridges=_as_bool(p.get("include_ontology_bridges"), True))
        lines = [f"Wiki Search (`{p.get('query')}`): {len(hits)} hit(s)"] + [
            f"  * `{h['rel_path']}` [{'ONTOLOGY BRIDGE via ' + ','.join(h.get('shared_ontology', [])) if h.get('match_kind') == 'ontology_bridge' else 'direct'} | author={h['author']}] {h['title']} — {h['excerpt'][:110]}"
            for h in hits
        ]
        return {"ok": True, "hits": hits, "summary": "\n".join(lines)}
    if sub == "graph":
        g = wk.export_mindmap_graph()
        g["summary"] = f"Wiki 2D Graph: {g['stats']['note_count']} notes, {g['stats']['edge_count']} wikilinks, {g['stats']['ontology_edge_count']} ontology bridges, domains={g['domains']}, orphans={len(g['orphan_notes'])}."
        return g
    if sub == "reindex":
        return wk.rebuild_index()
    if sub == "export_chat":
        return wk.export_chat_to_wiki(title=str(p.get("title") or "Chat Export"), limit=int(p.get("limit") or 12), tags=_as_list(p.get("tags")), actor=actor)
    notes = wk.list_notes(folder=str(p.get("folder") or p.get("path") or ""), limit=int(p.get("limit") or 40))
    lines = ["=== Co-Owned Knowledge Wiki Warehouse ==="] + ([f"  * `{n['rel_path']}` [tpl={n.get('template') or '-'} | author={n['author']} | #{','.join(n['tags'])}] {n['title']}" for n in notes] or ["  (Warehouse is empty — capture notes into `01_inbox/`.)"])
    return {"ok": True, "notes": notes, "summary": "\n".join(lines)}


def _act_vault(p: Dict[str, Any], actor: str, mem: MemoryStore) -> Dict[str, Any]:
    vm = get_vault_manager(store=mem)
    sub = str(p.get("subaction") or "list").lower()
    if sub == "set":
        return vm.set_secret(name=str(p.get("name") or ""), raw_value=str(p.get("value") or ""), cred_type=str(p.get("cred_type") or "api_key"), description=str(p.get("description") or ""), allow_list=_as_list(p.get("allow_list")), expires_at=str(p.get("expires_at") or ""), actor=actor)
    if sub == "delete":
        return vm.delete_secret(name=str(p.get("name") or ""), remove_metadata=_as_bool(p.get("remove_metadata")), actor=actor)
    if sub == "declare":
        return vm.declare_placeholder(name=str(p.get("name") or ""), cred_type=str(p.get("cred_type") or "api_key"), description=str(p.get("description") or ""), allow_list=_as_list(p.get("allow_list")), actor=actor)
    if sub in ("check", "check_secret", "metadata"):
        meta = vm.get_metadata(str(p.get("name") or ""))
        return {"ok": True, "exists": False, "is_set": False, "summary": f"Vault credential `{p.get('name')}` is not registered (`is_set=False`)."} if not meta else {"ok": True, "exists": True, "secret": meta, "summary": f"Vault `{meta['name']}`: is_set={meta['is_set']}, type={meta['cred_type']}, scopes={meta['allow_list']}."}
    if sub == "list_missing":
        miss = vm.list_missing_secrets()
        return {"ok": True, "missing": miss, "summary": "Missing Vault secrets: " + (", ".join(f"`{m['name']}` ({m['needed_by']})" for m in miss) or "none")}
    if sub in ("grant_scope", "request_scope"):
        return vm.grant_scope(name=str(p.get("name") or ""), caller_scope=str(p.get("caller_scope") or ""), actor=actor, confirmed=_human_confirmed(p, actor))
    if sub == "audit":
        logs = vm.get_audit_log(limit=int(p.get("limit") or 20))
        lines = ["=== Vault Zero-Reveal Audit Log ==="] + [f"  * [{r['created_at']}] `{r['secret_name']}` -> {r['outcome']} ({r['caller_scope']}) {r['detail']}" for r in logs]
        return {"ok": True, "audit": logs, "summary": "\n".join(lines)}
    return {"ok": True, "secrets": vm.list_secrets(), "missing": vm.list_missing_secrets(), "summary": vm.format_status()}


def _act_backup(p: Dict[str, Any], actor: str, mem: MemoryStore) -> Dict[str, Any]:
    sub = str(p.get("subaction") or "list").lower()
    if sub == "create":
        return create_snapshot_backup(label=str(p.get("label") or "manual"), store=mem)
    if sub == "restore":
        return restore_snapshot_backup(snapshot_id=str(p.get("snapshot_id") or ""), actor=actor, store=mem)
    snaps = list_snapshot_backups()
    lines = ["=== Secret-Free Snapshot Backups (`secrets.env` Excluded) ==="] + ([f"  * `{s['snapshot_id']}` ({s['created_at']}) wiki_files={s['wiki_files']} declared_secrets={len(s.get('declared_secrets', []))}" for s in snaps] or ["  (No snapshot backups created yet.)"])
    return {"ok": True, "snapshots": snaps, "summary": "\n".join(lines)}


def _act_endpoints(p: Dict[str, Any], actor: str, mem: MemoryStore) -> Dict[str, Any]:
    em = get_endpoint_manager(store=mem)
    sub = str(p.get("subaction") or "list").lower()
    if sub == "register":
        return em.register_endpoint(endpoint_id=str(p.get("endpoint_id") or ""), protocol=str(p.get("protocol") or ""), host=str(p.get("host") or ""), label=str(p.get("label") or ""), port=int(p.get("port") or 0), username=str(p.get("username") or ""), auth_mode=str(p.get("auth_mode") or ""), credential_ref=str(p.get("credential_ref") or ""), auth_header=str(p.get("auth_header") or "X-API-Key"), os_hint=str(p.get("os_hint") or ""), verify_tls=_as_bool(p.get("verify_tls"), True), tags=_as_list(p.get("tags")), actor=actor, confirmed=_human_confirmed(p, actor))
    if sub in ("propose", "propose_endpoint"):
        return em.propose_endpoint(endpoint_id=str(p.get("endpoint_id") or ""), protocol=str(p.get("protocol") or ""), host=str(p.get("host") or ""), label=str(p.get("label") or ""), port=int(p.get("port") or 0), username=str(p.get("username") or ""), auth_mode=str(p.get("auth_mode") or ""), credential_ref=str(p.get("credential_ref") or ""), auth_header=str(p.get("auth_header") or "X-API-Key"), os_hint=str(p.get("os_hint") or ""), verify_tls=_as_bool(p.get("verify_tls"), True), tags=_as_list(p.get("tags")), actor=actor)
    if sub == "remove":
        return em.remove_endpoint(str(p.get("endpoint_id") or ""), actor=actor)
    if sub == "test":
        return em.test_endpoint(str(p.get("endpoint_id") or ""), timeout=float(p.get("timeout") or 5.0))
    if sub == "inspect_host":
        return em.inspect_host(str(p.get("endpoint_id") or ""), actor=actor, confirmed=_human_confirmed(p, actor))
    if sub == "ssh_exec":
        return em.ssh_exec(str(p.get("endpoint_id") or ""), str(p.get("command") or ""), timeout=int(p.get("timeout") or 30), actor=actor, confirmed=_human_confirmed(p, actor))
    if sub == "ssh_read_file":
        return em.ssh_read_file(str(p.get("endpoint_id") or ""), str(p.get("remote_path") or ""), max_lines=int(p.get("max_lines") or 200), actor=actor, confirmed=_human_confirmed(p, actor))
    if sub == "winrm_exec":
        return em.winrm_exec(str(p.get("endpoint_id") or ""), str(p.get("command") or ""), timeout=int(p.get("timeout") or 30), actor=actor, confirmed=_human_confirmed(p, actor))
    if sub == "api_request":
        body = p.get("json_body")
        if isinstance(body, str) and body.strip():
            body = json.loads(body)
        return em.api_request(str(p.get("endpoint_id") or ""), method=str(p.get("method") or "GET"), path=str(p.get("path") or "/"), json_body=body if isinstance(body, dict) else None, actor=actor, confirmed=_human_confirmed(p, actor))
    return {"ok": True, "endpoints": em.list_endpoints(protocol=str(p.get("protocol") or "")), "summary": em.format_status()}


def _act_mcp(p: Dict[str, Any], actor: str, mem: MemoryStore) -> Dict[str, Any]:
    hub = get_mcp_hub(store=mem)
    sub = str(p.get("subaction") or "list").lower()
    if sub == "register_server":
        return hub.register_server(server_id=str(p.get("server_id") or ""), transport=str(p.get("transport") or "stdio"), label=str(p.get("label") or ""), command=str(p.get("command") or ""), args=_as_list(p.get("args")), url=str(p.get("url") or ""), credential_ref=str(p.get("credential_ref") or ""), allowed_tools=_as_list(p.get("allowed_tools")), actor=actor, confirmed=_human_confirmed(p, actor))
    if sub in ("propose", "propose_server"):
        return hub.propose_server(server_id=str(p.get("server_id") or ""), transport=str(p.get("transport") or "stdio"), label=str(p.get("label") or ""), command=str(p.get("command") or ""), args=_as_list(p.get("args")), url=str(p.get("url") or ""), credential_ref=str(p.get("credential_ref") or ""), allowed_tools=_as_list(p.get("allowed_tools")), actor=actor)
    if sub in ("harvest_skill", "harvest_pack"):
        return harvest_mcp_capability_pack(server_id=str(p.get("server_id") or ""), pack_name=str(p.get("pack_name") or "") or None, overwrite=_as_bool(p.get("overwrite"), True), store=mem)
    if sub == "discover":
        return hub.discover_server(str(p.get("server_id") or ""), actor=actor)
    if sub == "approve_server":
        return hub.approve_server(str(p.get("server_id") or ""), actor=actor)
    if sub == "remove_server":
        return hub.remove_server(str(p.get("server_id") or ""), actor=actor)
    if sub == "call_tool":
        args_in = p.get("arguments")
        if isinstance(args_in, str) and args_in.strip():
            args_in = json.loads(args_in)
        return hub.call_mcp_tool(str(p.get("server_id") or ""), str(p.get("tool_name") or ""), arguments=args_in if isinstance(args_in, dict) else {}, actor=actor, confirmed=_human_confirmed(p, actor))
    if sub == "configure_hosted":
        return hub.configure_hosted_mcp(enabled=_as_bool(p.get("enabled"), True), token_ref=str(p.get("token_ref") or "hosted-mcp-token"), allow_wiki_read=_as_bool(p.get("allow_wiki_read")), actor=actor, confirmed=_human_confirmed(p, actor))
    if sub == "register_partner":
        return hub.register_partner(partner_id=str(p.get("partner_id") or ""), url=str(p.get("url") or ""), label=str(p.get("label") or ""), credential_ref=str(p.get("credential_ref") or ""), enabled=_as_bool(p.get("enabled"), True), actor=actor)
    if sub == "remove_partner":
        return hub.remove_partner(str(p.get("partner_id") or ""), actor=actor)
    if sub == "handshake_partner":
        return hub.handshake_partner(str(p.get("partner_id") or ""))
    if sub == "delegate_partner":
        return hub.delegate_to_partner(str(p.get("partner_id") or ""), goal=str(p.get("goal") or ""), title=str(p.get("title") or ""), tier=str(p.get("tier") or "fast"))
    if sub == "check_partner_job":
        return hub.check_partner_job(str(p.get("partner_id") or ""), str(p.get("job_id") or ""))
    if sub == "list_partners":
        pts = hub.list_partners()
        hud = format_connected_partners_hud(store=mem) or "=== Federated Partners ===\n  (No federated partners registered yet.)"
        return {"ok": True, "partners": pts, "summary": hud}
    srvs = hub.list_servers()
    prof = mem.get_user_profile()
    lines = [f"=== Federated MCP Hub (Inbound Order Counter: `{prof.get('hosted_mcp_enabled', 'off').upper()}`) ==="]
    lines.extend([f"  * `{s['server_id']}` [{s['transport']} | tools={len(s['discovered_tools'])} | approved={s['is_approved']}]" for s in srvs] or ["  (No outbound MCP servers registered.)"])
    if p_hud := format_connected_partners_hud(store=mem):
        lines.append("\n" + p_hud)
    return {"ok": True, "servers": srvs, "partners": hub.list_partners(), "hosted_mcp_enabled": prof.get("hosted_mcp_enabled", "off") == "on", "summary": "\n".join(lines)}


def _act_partners(p: Dict[str, Any], actor: str, mem: MemoryStore) -> Dict[str, Any]:
    alias = {
        "register": "register_partner", "add": "register_partner", "register_partner": "register_partner",
        "remove": "remove_partner", "delete": "remove_partner", "remove_partner": "remove_partner",
        "handshake": "handshake_partner", "ping": "handshake_partner", "handshake_partner": "handshake_partner",
        "delegate": "delegate_partner", "delegate_partner": "delegate_partner",
        "check_job": "check_partner_job", "status": "check_partner_job", "result": "check_partner_job", "check_partner_job": "check_partner_job",
        "list": "list_partners", "list_partners": "list_partners",
    }
    sub = alias.get(str(p.get("subaction") or "list").lower(), "list_partners")
    return _act_mcp({**p, "subaction": sub}, actor, mem)


def _act_routines(p: Dict[str, Any], actor: str, mem: MemoryStore) -> Dict[str, Any]:
    rm = get_routine_manager(store=mem)
    sub = str(p.get("subaction") or "list").lower()
    if sub in ("create", "save", "update"):
        tp_in = dict(p.get("trigger_policy") or {}) if isinstance(p.get("trigger_policy"), dict) else {}
        for k in ("mode", "schedule_expr", "idle_min_seconds", "allowed_window", "cooldown_seconds", "precheck_name", "requires_self_dev"):
            if p.get(k) is not None and k not in tp_in:
                tp_in[k] = p[k]
        return rm.create_or_update_routine(
            routine_id=str(p.get("routine_id") or ""), schedule_expr=str(p.get("schedule_expr") or ""),
            target_kind=str(p.get("target_kind") or "dag"), title=str(p.get("title") or ""),
            target_name=str(p.get("target_name") or ""), inputs=p.get("inputs") if isinstance(p.get("inputs"), dict) else None,
            trigger_policy=tp_in or None, enabled=_as_bool(p.get("enabled"), True), catch_up=_as_bool(p.get("catch_up"), False),
            actor=actor, confirmed=_human_confirmed(p, actor),
        )
    if sub in ("enable", "disable"):
        return rm.set_routine_enabled(str(p.get("routine_id") or ""), enabled=(sub == "enable"))
    if sub in ("run", "run_now"):
        return rm.run_routine_now(str(p.get("routine_id") or ""), run_in_background=_as_bool(p.get("run_in_background"), False))
    if sub == "delete":
        return rm.delete_routine(str(p.get("routine_id") or ""))
    if sub == "tick":
        return rm.tick_due_routines(run_in_background=_as_bool(p.get("run_in_background"), True))
    items = rm.list_routines(mode=str(p.get("mode") or "") or None)
    lines = ["=== Unified Routines (`manual` | `scheduled` | `on_idle`) ==="] + (
        [f"  * `{r['routine_id']}` [{'ON' if r['enabled'] else 'OFF'} | `{r['trigger_policy']['mode']}` {r['schedule_expr'] or r['trigger_policy'].get('allowed_window') or 'anytime'} -> {r['target_kind']}] status={r['last_status']}" for r in items]
        or ["  (No routines configured.)"]
    )
    return {"ok": True, "routines": items, "summary": "\n".join(lines)}


def _act_sandbox(p: Dict[str, Any], _actor: str, _mem: MemoryStore) -> Dict[str, Any]:
    sub = str(p.get("subaction") or "list").lower()
    if sub == "create":
        if not settings.is_self_dev_enabled():
            return {"ok": False, "error": "SAFETY GUARD REJECTED: Self-development sandbox clone creation requires `Self-Dev Mode = ON` (`/selfdev on` or Web System Studio)."}
        return create_sandbox_clone(label=str(p.get("label") or "experiment"))
    if sub == "verify":
        return verify_sandbox_clone(str(p.get("sandbox_id") or ""), run_tests=_as_bool(p.get("run_tests"), False))
    if sub == "diff":
        return diff_sandbox_clone(str(p.get("sandbox_id") or ""))
    if sub == "cleanup":
        return cleanup_sandbox_clone(str(p.get("sandbox_id") or ""))
    items = list_sandbox_clones()
    lines = ["=== Isolated Self-Development Sandbox Clones ==="] + ([f"  * `{s['sandbox_id']}` ({s['copied_files']} files) -> `{s['path']}`" for s in items] or ["  (No active sandbox clones.)"])
    return {"ok": True, "sandboxes": items, "summary": "\n".join(lines)}


def _act_agile(p: Dict[str, Any], _actor: str, mem: MemoryStore) -> Dict[str, Any]:
    """Project `agile` CoreActions cleanly over the Universal Project & Goal Graph (`ProjectGraphManager`)."""
    mgr = get_project_graph(store=mem)
    sub = str(p.get("subaction") or p.get("action") or "board_view").strip().lower()
    pid = str(p.get("project_id") or p.get("board_id") or "").strip()
    if pid == "all-active-sprint":
        pid = ""
    if sub in ("create_board", "save_board", "create_project", "upsert_project"):
        proj = mgr.create_project(
            title=str(p.get("title") or p.get("name") or "New Goal"), goal_summary=str(p.get("goal_summary") or p.get("description") or ""),
            slug=str(p.get("slug") or "") or None, kind=str(p.get("kind") or "goal"),
            trigger_policy=p.get("trigger_policy") if isinstance(p.get("trigger_policy"), dict) else None,
            project_id=pid or None,
        )
        return {"ok": True, "project": proj, "board": proj, "summary": f"Saved project/goal `{proj['title']}` (`{proj['project_id']}`)."}
    if sub == "update_project":
        proj = mgr.update_project(pid or mgr.resolve_project_id(), **{k: v for k, v in p.items() if k in ("title", "goal_summary", "status", "wiki_hub_path", "trigger_policy", "metadata")})
        return {"ok": True, "project": proj, "summary": f"Updated project `{proj['project_id']}`."}
    if sub in ("create_card", "save_card", "add_card", "create_item"):
        ref = str(p.get("item_id") or p.get("card_id") or "").strip()
        if ref:
            it = mgr.update_item(ref, **{k: v for k, v in p.items() if k not in ("subaction", "action", "item_id", "card_id")})
        else:
            it = mgr.create_item(
                project_id=pid or None, title=str(p.get("title") or ""), description=str(p.get("description") or ""),
                owner=str(p.get("owner") or "companion"), stage_kind=str(p.get("stage_kind") or "DELIVER_AND_VERIFY"),
                status=str(p.get("status") or p.get("column_key") or "ready"), priority=int(p.get("priority") or 3),
                depends_on=_as_list(p.get("depends_on")), milestone_id=str(p.get("milestone_id") or p.get("epic_id") or ""),
                timebox_id=str(p.get("timebox_id") or p.get("sprint_id") or ""),
                verification=p.get("verification") if isinstance(p.get("verification"), dict) else None,
                dag_name=str(p.get("dag_name") or ""), acceptance_criteria=str(p.get("acceptance_criteria") or ""),
                estimate_minutes=int(p.get("estimate_minutes") or 30), planned_date=str(p.get("planned_date") or ""),
                due_date=str(p.get("due_date") or ""), tags=_as_list(p.get("tags")),
            )
        return {"ok": True, "card": it, "item": it, "summary": f"Saved work item `{it['item_id']}` (`{it['owner']}` | `{it['status']}`): {it['title']}"}
    if sub in ("move_card", "update_card", "update_item"):
        ref = str(p.get("item_id") or p.get("card_id") or "").strip()
        it = mgr.update_item(ref, **{k: v for k, v in p.items() if k not in ("subaction", "action", "item_id", "card_id")})
        return {"ok": True, "card": it, "item": it, "summary": f"Updated `{it['item_id']}` -> `{it['status']}`."}
    if sub in ("delete_card", "delete_item"):
        ref = str(p.get("item_id") or p.get("card_id") or "").strip()
        ok = mgr.delete_item(ref)
        return {"ok": ok, "item_id": ref, "summary": f"Deleted `{ref}`."}
    if sub in ("create_epic", "save_epic", "create_milestone"):
        ms = mgr.create_milestone(pid or mgr.resolve_project_id(), str(p.get("title") or ""), description=str(p.get("description") or ""), stage_kind=str(p.get("stage_kind") or "DELIVER_AND_VERIFY"), target_date=str(p.get("target_date") or ""))
        return {"ok": True, "epic": ms, "milestone": ms, "summary": f"Created milestone `{ms['title']}` (`{ms['milestone_id']}`)."}
    if sub == "seed_dikw":
        seeded = mgr.seed_dikw_goal_milestones(pid or mgr.resolve_project_id())
        return {"ok": True, "milestones": seeded, "summary": f"Seeded 4 DIKW prerequisite milestones: {seeded}"}
    if sub in ("create_sprint", "save_sprint", "create_timebox"):
        tb = mgr.create_timebox(pid or None, str(p.get("title") or "Weekly Timebox"), goal=str(p.get("goal") or ""), start_date=str(p.get("start_date") or ""), end_date=str(p.get("end_date") or ""))
        if _as_bool(p.get("activate"), True):
            tb = mgr.update_timebox_status(tb["timebox_id"], "active")
        return {"ok": True, "sprint": tb, "timebox": tb, "summary": f"Created timebox `{tb['title']}` (`{tb['start_date']}..{tb['end_date']}`)."}
    if sub in ("start_sprint", "complete_sprint", "update_timebox"):
        st = "active" if sub == "start_sprint" else ("completed" if sub == "complete_sprint" else str(p.get("status") or "active"))
        tb = mgr.update_timebox_status(str(p.get("timebox_id") or p.get("sprint_id") or ""), st, retro_notes=str(p.get("retro_notes") or ""))
        return {"ok": True, "sprint": tb, "timebox": tb, "summary": f"Timebox `{tb['timebox_id']}` -> `{tb['status']}`."}
    if sub == "roadmap":
        rmap = mgr.get_roadmap_projection(pid or None)
        return {"ok": True, **rmap, "summary": f"Roadmap `{rmap['project']['title']}`: {len(rmap['milestones'])} milestone(s)."}
    if sub == "list_boards":
        projs = mgr.list_projects()
        return {"ok": True, "boards": projs, "projects": projs, "summary": "Projects & Goals: " + ", ".join(f"`{x['slug']}` ({x['kind']})" for x in projs)}
    view = mgr.get_board_projection(pid or None, timebox_id=str(p.get("sprint_id") or p.get("timebox_id") or "") or None, owner_filter=str(p.get("owner") or "") or None)
    cnt, rows = view["counts"], [f"  - `{it['item_id']}` [`{it['owner']}` | `{it['status']}`]: {it['title']}" for it in view.get("items", [])[:15]]
    return {
        "ok": True, **view, "boards": mgr.list_projects(), "aging_cards": [],
        "summary": f"Project `{view['project']['title']}`: total={cnt['total']}, companion_ready={cnt['companion_ready']}, human_pending={cnt['human_pending']}, blocked={cnt['blocked']}, done={cnt['done']}." + ("\n" + "\n".join(rows) if rows else ""),
    }


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


CORE_ACTIONS: Dict[str, ActionSpec] = {
    "trust": ActionSpec("trust", "governance", "Inspect or set the 3-tier global Trust Mode (`strict`, `balanced`, `autonomous`).", False, _act_trust),
    "autonomy": ActionSpec("autonomy", "governance", "Manage Two-Lever Operating Architecture (`Background Autonomy` x `Self-Dev Mode`), Idle Routines, and Morning Journal.", False, lambda p, a, m: execute_autonomy_mode_action(p, actor=a, store=m)),
    "hobbies": ActionSpec("hobbies", "governance", "Compatibility alias for `autonomy` (`Background Autonomy` x `Self-Dev Mode` & Morning Journal).", False, lambda p, a, m: execute_autonomy_mode_action(p, actor=a, store=m)),
    "approvals": ActionSpec("approvals", "governance", "Inspect or resolve pending human-in-the-loop approval requests.", False, _act_approvals),
    "archive": ActionSpec("archive", "memory", "Search Forever Chat (Hot + Cold Archive) or archive conversations >90d.", False, _act_archive),
    "prompts": ActionSpec("prompts", "memory", "Manage and render reusable templates in the Saved Prompts Library.", False, _act_prompts),
    "jobs": ActionSpec("jobs", "jobs", "Dispatch, inspect, cancel, or resume Forever-Chat background jobs.", False, _act_jobs),
    "wiki": ActionSpec("wiki", "wiki", "Operate the Co-Owned 4-Folder Knowledge Wiki Warehouse & 2D Mind Map.", False, _act_wiki),
    "vault": ActionSpec("vault", "vault", "Manage Blind Credential Vault entries, scope grants, missing-secret checks, and audit logs.", False, _act_vault),
    "backup": ActionSpec("backup", "vault", "Create, list, or restore secret-free SQLite + Wiki snapshot backups.", False, _act_backup),
    "endpoints": ActionSpec("endpoints", "endpoints", "Register, propose, probe, inspect, and execute across SSH, WinRM, and HTTP API endpoints.", False, _act_endpoints),
    "mcp": ActionSpec("mcp", "mcp", "Manage outbound MCP servers, propose/harvest Capability Packs, Hosted MCP, and Federated Partners.", False, _act_mcp),
    "partners": ActionSpec("partners", "mcp", "Register, handshake, inspect, and delegate jobs across Federated Companion Partners.", False, _act_partners),
    "routines": ActionSpec("routines", "routines", "Schedule and execute unified `manual`, `scheduled`, and windowed `on_idle` routines.", False, _act_routines),
    "sandbox": ActionSpec("sandbox", "routines", "Create, Doctor-verify, diff, and clean up isolated self-development clones.", False, _act_sandbox),
    "agile": ActionSpec("agile", "agile", "Operate Universal Project & Goal Graph (`companion` | `human` | `collaborative`), Milestones, Timeboxes, and Kanban/Roadmap views.", False, _act_agile),
    "calendar": ActionSpec("calendar", "calendar", "Operate Unified Calendar timeline merging events, active timeboxes, task dates, routines, and due SRS reviews.", False, lambda p, _a, m: execute_calendar_action(p, store=m)),
    "education": ActionSpec("education", "education", "Operate Headless Education courses, pedagogical stages, spaced repetition, and inline Chat flashcard decks.", False, lambda p, a, m: execute_education_action({**p, "actor": p.get("actor") or a}, store=m)),
    "models": ActionSpec("models", "models", "Manage Multi-Provider LLM Registry, Blind Vault API key bindings, Capability Tier Matrix, and LAN Privacy Lock.", False, execute_model_registry_action),
    "identity": ActionSpec("identity", "governance", "Inspect or customize Companion Soul, User Identity & Profile, Communication Tone, and Active Workspace Project.", False, execute_identity_action),
}

HARVEST_SLASH_VERBS = (
    "/jobs", "/wiki", "/vault", "/endpoints", "/mcp", "/partners", "/routines", "/approvals", "/trust", "/selfdev",
    "/prompts", "/backup", "/archive", "/sandbox", "/board", "/boards", "/sprint", "/calendar", "/study", "/providers", "/identity",
)


def list_core_actions() -> List[Dict[str, Any]]:
    return [{"name": s.name, "domain": s.domain, "description": s.description, "user_only": s.user_only} for s in CORE_ACTIONS.values()]


def execute_core_action(action_name: str, params: Optional[Dict[str, Any]] = None, actor: str = "user", store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """Execute a registered Core Action with uniform 1-sentence error normalization and Vault secret scrubbing."""
    mem = store or get_memory_store()
    spec = CORE_ACTIONS.get((action_name or "").strip().lower())
    if spec is None:
        return {"ok": False, "error": f"Unknown core action `{action_name}` (valid: {', '.join(CORE_ACTIONS)})."}
    if spec.user_only and actor.strip().lower() not in ("user", "cli", "web"):
        return {"ok": False, "error": f"Core action `{spec.name}` is restricted to direct user invocation."}
    try:
        with memory_store_context(mem):
            raw_res = spec.handler(dict(params or {}), actor, mem)
    except Exception as exc:
        raw_res = {"ok": False, "error": f"{spec.name} failed ({type(exc).__name__}): {exc}"}
    vault = get_vault_manager(store=mem)
    scrubbed_json = vault.scrub_output(json.dumps(raw_res, default=str))
    return json.loads(scrubbed_json)


def handle_harvest_slash(verb: str, arg: str, store: Optional[MemoryStore] = None) -> str:
    """Unified CLI/REPL slash handler routing `/jobs`, `/wiki`, `/vault`, `/endpoints`, `/mcp`, `/partners`, etc. through `CORE_ACTIONS`."""
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
            p = {"subaction": sub if sub in ("graph", "reindex", "export_chat", "tag_authority", "list") else "list", "folder": sub if sub.startswith("0") else ""}
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
    elif v in ("/calendar", "/study", "/providers", "/selfdev"):
        slash_map = {"/calendar": ("calendar", parse_calendar_slash), "/study": ("education", parse_education_slash), "/providers": ("models", parse_providers_slash), "/selfdev": ("autonomy", parse_selfdev_slash)}
        act_name, fn = slash_map[v]
        p = fn(parts)
    elif v == "/identity":
        act_name, p = "identity", {"subaction": "get"}
    else:
        act_name = "sandbox"
        p = {"subaction": sub, "sandbox_id": rest[0] if rest else "", "label": rest[0] if rest else "experiment"} if sub in ("create", "verify", "diff", "cleanup") else {"subaction": "list"}
    res = execute_core_action(act_name, p, actor="cli", store=store)
    if res.get("markdown_block"):
        return f"{res.get('summary', '')}\n\n{res['markdown_block']}".strip()
    if res.get("summary") and res.get("output"):
        return f"{res['summary']}\n{res['output']}"
    return str(res.get("summary") or res.get("output") or res.get("error") or json.dumps(res, indent=2))
