"""
Companion OS State, Governance & Orchestration Tool Handlers (A-Frame Application Layer).

Extracted from `companion/tools.py` so workspace I/O tools and OS state/governance tools
each maintain clean single-responsibility focus and generous `<465L` headroom.
"""

import json
from typing import Any, Dict

from companion.archive_and_approvals import format_forever_search_results, search_forever_history
from companion.autonomy import run_autonomous_goal, run_http_smoke_probe
from companion.blueprint import approve_blueprint_and_build, create_or_revise_blueprint
from companion.config import settings
from companion.context import manage_active_project
from companion.dag_runtime import resume_dag_run
from companion.grounding import get_execution_target, get_knowledge_port
from companion.hygiene import analyze_workspace_hygiene
from companion.memory_store import get_memory_store
from companion.safety import get_active_checkpoint_manager, run_system_doctor
from companion.sdd import CANONICAL_SPEC_FILES, sync_decisions_md, verify_sdd
from companion.skill_forge import check_make_vs_borrow, run_skill_script, stage_and_promote_skill
from companion.telemetry import telemetry
from companion.tool_schemas import bool_arg, current_workflow_phase, int_arg, str_arg
from companion.workflow_graph import workflow_engine


def tool_memory_manage(args: Dict[str, Any]) -> str:
    """Manage persistent SQLite memory, ADRs, user profile, notes, and MEMORY.md sync."""
    action, store = str_arg(args, "action", "get_notes").lower(), get_memory_store()
    if action == "add_note":
        note = str_arg(args, "content") or str_arg(args, "note")
        if not note:
            return "error: content/note is required for add_note"
        nid = store.add_system_note(note, category=str_arg(args, "category", "general") or "general")
        store.sync_memory_md()
        return f"Saved system note #{nid} and synchronized MEMORY.md."
    if action == "get_notes":
        notes = store.get_system_notes(category=str_arg(args, "category") or None, limit=int_arg(args, "limit", 20))
        return "\n".join(f"- [{c} | {ts}] {n}" for n, c, ts in notes) if notes else "No system notes found."
    if action == "add_adr":
        if settings.get_workspace().is_self_project and not settings.is_self_dev_enabled():
            return "SAFETY GUARD REJECTED: `add_adr` modifies `specs/DECISIONS.md` and requires `Self-Dev Mode = ON` (`/selfdev on`)."
        title, decision = str_arg(args, "title"), str_arg(args, "decision")
        if not title or not decision:
            return "error: title and decision are required for add_adr"
        aid = store.add_adr(
            title=title,
            context=str_arg(args, "context", "Architectural evolution") or "Architectural evolution",
            decision=decision,
            consequences=str_arg(args, "consequences", "Clean modular evolution") or "Clean modular evolution",
        )
        store.sync_memory_md()
        sync_decisions_md(store=store)
        return f"Recorded ADR #{aid} ('{title}') and synced MEMORY.md & specs/DECISIONS.md."
    if action == "get_adrs":
        adrs = store.get_adrs()
        return "\n".join(f"#{a['id']} {a['title']} [{a['status']}]: {a['decision']}" for a in adrs) if adrs else "No ADRs."
    if action == "set_profile":
        k, v = str_arg(args, "key"), str_arg(args, "value")
        if not k or not v:
            return "error: key and value are required"
        try:
            store.set_user_profile(k, v)
        except PermissionError as exc:
            return str(exc)
        store.sync_memory_md()
        return f"Updated user profile '{k}'."
    if action == "get_profile":
        return "\n".join(f"- {k}: {v}" for k, v in store.get_user_profile().items())
    if action == "upsert_fact":
        k, v = str_arg(args, "key"), (str_arg(args, "value") or str_arg(args, "content"))
        if not k or not v:
            return "error: key and value are required for upsert_fact"
        try:
            conf = float(args.get("confidence", 1.0)) if args.get("confidence") is not None else 1.0
        except (TypeError, ValueError):
            conf = 1.0
        pid = settings.get_workspace().project_id
        return get_knowledge_port(store).propose_update(key=k, value=v, project_id=pid, source=str_arg(args, "source", "agent") or "agent", confidence=conf)
    if action in ("query_facts", "read_fact"):
        port, raw_q, raw_k, lim = get_knowledge_port(store), str_arg(args, "query"), str_arg(args, "key"), int_arg(args, "limit", 15)
        pid = settings.get_workspace().project_id
        exact = port.read_entry(raw_k, project_id=pid) if raw_k and not raw_q else None
        entries = [exact] if exact else (port.search(raw_q or raw_k, project_id=pid, limit=lim) if (raw_q or raw_k) else port.list_index(project_id=pid, limit=lim))
        return "\n".join(f"- `{e.key}` [{e.source}]: {e.content}" for e in entries) if entries else "No grounded facts found."
    if action in ("search_history", "search"):
        q, tw = str_arg(args, "query"), str_arg(args, "time_window") or None
        if not q and not tw:
            return "error: query or time_window is required for search_history"
        hits = search_forever_history(q, store=store, limit=int_arg(args, "limit", 15), time_window=tw, include_cold=True)
        return format_forever_search_results(hits, query=q)
    if action == "recent_history":
        msgs = store.get_recent_messages(limit=int_arg(args, "limit", 15))
        return "\n".join(f"[{ts} {role}]: {c[:300]}" for role, c, ts in reversed(msgs)) if msgs else "No messages."
    if action == "sync":
        md_path = store.sync_memory_md()
        if settings.get_workspace().is_self_project and not settings.is_self_dev_enabled():
            return f"Synchronized SQLite memory to {md_path} (`specs/DECISIONS.md` remains read-only while Self-Dev Mode is OFF)."
        return f"Synchronized SQLite memory to {md_path} and {sync_decisions_md(store=store)}."
    return f"error: unknown memory_manage action '{action}'."


def tool_task_manage(args: Dict[str, Any]) -> str:
    """Create, update, and query tasks and engineering backlog items in SQLite."""
    action, store = str_arg(args, "action", "list_all").lower(), get_memory_store()
    if action == "add_task":
        desc = str_arg(args, "description")
        if not desc:
            return "error: description is required"
        tid = store.add_task(desc, priority=int_arg(args, "priority", 5), acceptance_criteria=str_arg(args, "acceptance_criteria") or None)
        store.sync_memory_md()
        return f"Created Task #{tid}: {desc}"
    if action == "update_task":
        tid = int_arg(args, "task_id", 0)
        if tid <= 0:
            return "error: valid task_id is required"
        ok = store.update_task(tid, status=str_arg(args, "status", "completed") or "completed", verification_result=str_arg(args, "verification_result") or None)
        store.sync_memory_md()
        return f"Updated Task #{tid}" if ok else f"error: Task #{tid} not found"
    if action == "list_tasks":
        tasks = store.get_all_tasks(status_filter=str_arg(args, "status") or None)
        return "\n".join(f"Task #{t['id']} [P{t['priority']} | {t['status']}]: {t['description']}" for t in tasks) if tasks else "No tasks."
    if action == "add_backlog":
        desc = str_arg(args, "description")
        if not desc:
            return "error: description is required"
        bid = store.add_backlog_item(
            desc,
            category=str_arg(args, "category", "architecture") or "architecture",
            status=str_arg(args, "status", "pending") or "pending",
            estimated_effort=str_arg(args, "estimated_effort") or None,
            priority=int_arg(args, "priority", 5),
        )
        store.sync_memory_md()
        return f"Added Backlog Item #{bid}: {desc}"
    if action == "update_backlog":
        bid, prio = int_arg(args, "backlog_id", 0), int_arg(args, "priority", -1)
        if bid <= 0:
            return "error: valid backlog_id is required"
        ok = store.update_backlog_item(
            bid,
            status=str_arg(args, "status", "completed") or "completed",
            actual_effort=str_arg(args, "actual_effort") or None,
            priority=prio if prio >= 0 else None,
        )
        store.sync_memory_md()
        return f"Updated Backlog #{bid}" if ok else f"error: Backlog #{bid} not found"
    if action in ("list_backlog", "list_all"):
        items = store.get_backlog(category=str_arg(args, "category") or None, status=str_arg(args, "status") or None)
        tasks = store.get_pending_tasks()
        lines = ["=== Pending Tasks ===", *([f"  Task #{tid} [P{pr} | {st}]: {d}" for tid, d, st, pr in tasks] if tasks else ["  (none)"]), "\n=== Engineering Backlog ==="]
        lines.extend([f"  Backlog #{bid} [{c} | {st}]: {d}" for bid, d, c, st in items] if items else ["  (empty)"])
        return "\n".join(lines)
    return f"error: unknown task_manage action '{action}'."


def _dict_arg(args: Dict[str, Any], *keys: str) -> Dict[str, Any] | None:
    for k in keys:
        val = args.get(k)
        if isinstance(val, dict):
            return val
        if isinstance(val, str) and val.strip().startswith("{"):
            raw = val.strip()
            for cand in (raw, raw + "}"):
                try:
                    parsed = json.loads(cand, strict=False)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    continue
    return None


def _bad_json_dict_err(args: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        val = args.get(k)
        if isinstance(val, str) and val.strip() and _dict_arg({k: val}, k) is None:
            return f"error: parameter `{k}` must be a dict object (could not parse string as JSON object)."
    return ""


def _handle_skill_forge_action(action: str, args: Dict[str, Any]) -> str:
    """Handle `check_skill`, `bootstrap_skill`/`promote_skill`, and `run_script` actions for `tool_workflow_manage`."""
    if action == "check_skill":
        q = str_arg(args, "goal") or str_arg(args, "skill")
        return "error: goal or skill query is required for check_skill" if not q else str(check_make_vs_borrow(q)["summary"])
    if action in ("bootstrap_skill", "promote_skill"):
        jerr = _bad_json_dict_err(args, "dags", "workflows", "scripts", "references")
        if jerr:
            return jerr
        raw_dags = dict(_dict_arg(args, "dags", "workflows") or {})
        raw_scripts = dict(_dict_arg(args, "scripts") or {})
        for fname in list(raw_scripts):
            src = str(raw_scripts[fname])
            if "DAGSpec(" in src and "--self-test" not in src:
                raw_dags.setdefault(fname, raw_scripts.pop(fname))
        default_scope = "core" if settings.is_self_dev_enabled() else "user"
        eff_scope = str_arg(args, "scope", default_scope) or default_scope
        return str(
            stage_and_promote_skill(
                name=str_arg(args, "skill"),
                skill_md=str(args.get("skill_md") or ""),
                scripts=raw_scripts or None,
                dags=raw_dags or None,
                references=_dict_arg(args, "references"),
                overwrite=bool_arg(args, "overwrite", False),
                scope=eff_scope,
            )["summary"]
        )
    raw_sa = args.get("script_args")
    return run_skill_script(
        skill_name=str_arg(args, "skill"),
        script_name=str_arg(args, "script"),
        script_args=[str(x) for x in raw_sa] if isinstance(raw_sa, list) else None,
    )


def _handle_workflow_state_action(action: str, args: Dict[str, Any]) -> str:
    """Handle `start`, `transition`, `abort`, `complete`, `recover`, and `status` actions for `workflow_manage`."""
    if action == "start":
        goal = str_arg(args, "goal")
        if not goal:
            return "error: goal is required to start a workflow"
        wf_type = str_arg(args, "workflow_type", "engineering_feature") or "engineering_feature"
        if settings.get_workspace().is_self_project and not settings.is_self_dev_enabled():
            return (
                f"SAFETY GUARD REJECTED: `workflow_manage(action='start', workflow_type='{wf_type}')` is strictly for software/codebase engineering builds "
                "and Companion core is read-only while `Self-Dev Mode = OFF` (`/selfdev on`). "
                "For the user's personal, home, or career long-running goals/projects, use `agile_manage(action='save_epic')` and `agile_manage(action='save_card', epic_id=...)` on Boards instead "
                "(or switch to an external coding workspace via `project_manage`)."
            )
        res = workflow_engine.start_workflow(
            goal=goal,
            workflow_type=wf_type,
            acceptance_criteria=str_arg(args, "acceptance_criteria"),
        )
        return f"{res.message}\n\n{workflow_engine.render_hud()}"
    if action in ("transition", "abort", "complete"):
        to_state = "ABORTED" if action == "abort" else ("COMPLETE" if action == "complete" else str_arg(args, "to_state"))
        if not to_state:
            return "error: to_state is required"
        res = workflow_engine.transition(
            to_state_str=to_state,
            summary=str_arg(args, "summary") or f"Transition to {to_state}",
            acceptance_criteria=str_arg(args, "acceptance_criteria") or None,
        )
        return f"{res.message}\n\n{workflow_engine.render_hud()}"
    if action == "recover":
        res = workflow_engine.recover(reason=str_arg(args, "reason", "Recovery triggered") or "Recovery triggered", rollback=bool_arg(args, "rollback", False))
        return f"{res.message}\n\n{workflow_engine.render_hud()}"
    hud, active = workflow_engine.render_hud(), get_memory_store().get_active_workflow()
    return hud + (
        "\n\nTransition History:\n"
        + "\n".join(f"  {h['created_at']}: {h['from_state']} -> {h['to_state']} ({h['summary']})" for h in get_memory_store().get_workflow_transitions(int(active["id"])))
        if active
        else ""
    )


def tool_workflow_manage(args: Dict[str, Any]) -> str:
    """Drive the deterministic State Machine, Isolated Subagent Workers, and Code-Controlled Python DAGs."""
    action = str_arg(args, "action", "status").lower()
    if current_workflow_phase() and action in ("start", "transition", "abort", "complete", "recover", "autonomy", "blueprint", "approve"):
        return (
            f"PHASE TOOL GATE REJECTED `workflow_manage(action='{action}')`: the outer autonomous driver already manages "
            f"workflow state (`{current_workflow_phase()}`). Finish your current task directly and stop calling tools."
        )
    if action in ("start", "transition", "abort", "complete", "recover", "status"):
        return _handle_workflow_state_action(action, args)
    if action == "subagent":
        goal = str_arg(args, "goal")
        if not goal:
            return "error: goal is required for subagent dispatch"
        from companion.agent import run_subagent

        skill = (str_arg(args, "skill") or str_arg(args, "skill_name")) or None
        tier = str_arg(args, "tier") or None
        return f"=== Subagent Worker Result (`{skill or 'worker'}` | tier=`{tier or 'fast'}`) ===\n{run_subagent(goal=goal, skill_name=skill, max_iters=int_arg(args, 'max_iters', 8), verbose=False, tier=tier)}"
    if action in ("list_dags", "list_recipes"):
        return workflow_engine.format_dags_catalog()
    if action in ("check_skill", "bootstrap_skill", "promote_skill", "run_script"):
        return _handle_skill_forge_action(action, args)
    if action in ("run_dag", "run_recipe"):
        dag_name = str_arg(args, "dag") or str_arg(args, "recipe") or str_arg(args, "recipe_name") or str_arg(args, "goal")
        if not dag_name:
            return "error: dag is required for run_dag"
        from companion.agent import ensure_dag_runtime_bound

        ensure_dag_runtime_bound()
        raw_params = _dict_arg(args, "params", "recipe_inputs", "inputs") or {}
        res = workflow_engine.run_dag(dag_name, params=raw_params)
        return str(res.get("summary") or json.dumps(res, indent=2, default=str))
    if action == "resume_dag":
        run_id = str_arg(args, "run_id") or str_arg(args, "goal")
        if not run_id:
            return "error: run_id is required for resume_dag"
        from companion.agent import ensure_dag_runtime_bound

        ensure_dag_runtime_bound()
        res = resume_dag_run(run_id, store=get_memory_store())
        return str(res.get("summary") or json.dumps(res, indent=2, default=str))
    if action == "blueprint":
        return str(create_or_revise_blueprint(str_arg(args, "goal") or "status", workflow_type=str_arg(args, "workflow_type", "engineering_feature") or "engineering_feature", max_phase_iters=int_arg(args, "max_iters", 10), verbose=False)["summary"])
    if action == "approve":
        return str(approve_blueprint_and_build(max_phase_iters=int_arg(args, "max_iters", 10), verbose=False)["summary"])
    if action == "autonomy":
        goal = str_arg(args, "goal")
        if not goal:
            return "error: goal is required for workflow_manage(action='autonomy')"
        return str(
            run_autonomous_goal(
                goal=goal,
                workflow_type=str_arg(args, "workflow_type", "engineering_feature") or "engineering_feature",
                max_phase_iters=int_arg(args, "max_iters", 10),
                verbose=False,
            )["summary"]
        )
    if action == "http_smoke":
        raw_eps = args.get("endpoints")
        return str(
            run_http_smoke_probe(
                module_name=str_arg(args, "module", "surfaces.web_server") or "surfaces.web_server",
                endpoints=[str(e) for e in raw_eps] if isinstance(raw_eps, list) and raw_eps else None,
            )["summary"]
        )
    return f"error: unknown workflow_manage action '{action}'."


def tool_checkpoint_manage(args: Dict[str, Any]) -> str:
    """Create, list, or rollback full workspace checkpoints or local Git save-points in the active project."""
    action, active_cp, target = str_arg(args, "action", "list").lower(), get_active_checkpoint_manager(), get_execution_target()
    if action == "create":
        m = target.create_checkpoint(label=str_arg(args, "label", "manual") or "manual")
        return f"Created workspace checkpoint `{m['checkpoint_id']}` ({m['file_count']} files)."
    if action == "list":
        cps = target.list_checkpoints()
        return "\n".join(f"- {c['checkpoint_id']} (label='{c['label']}', files={c['file_count']})" for c in cps) if cps else "No checkpoints."
    if action == "rollback":
        res = target.rollback_checkpoint(checkpoint_id=str_arg(args, "checkpoint_id") or None)
        return f"Rolled back to `{res['checkpoint_id']}` ({res['restored_count']} files)." if res.get("restored") else f"error: {res.get('error')}"
    if action == "git_save":
        res = active_cp.create_git_savepoint(message=str_arg(args, "label", "Manual save-point") or "Manual save-point", init_if_needed=not settings.get_workspace().is_self_project)
        return str(res.get("summary", ""))
    if action == "git_log":
        logs = active_cp.list_git_savepoints(limit=10)
        return "\n".join(f"- `{e['commit']}` ({e['date']}): {e['subject']}" for e in logs) if logs else "No Git commits found in active workspace."
    return f"error: unknown checkpoint_manage action '{action}'."


def tool_sdd_manage(args: Dict[str, Any]) -> str:
    """Verify Spec-Driven Development alignment, AST Dead-Code Hygiene, or read/sync specs."""
    action = str_arg(args, "action", "verify").lower()
    if action in ("verify", "status"):
        return f"{verify_sdd().format_summary()}\n\n{analyze_workspace_hygiene().format_summary()}"
    if action == "sync":
        if settings.get_workspace().is_self_project and not settings.is_self_dev_enabled():
            return "SAFETY GUARD REJECTED: `sdd_manage(action='sync')` writes `specs/DECISIONS.md` and requires `Self-Dev Mode = ON` (`/selfdev on`)."
        store = get_memory_store()
        return f"Synchronized & compacted ADRs to {sync_decisions_md(store=store)} and memory to {store.sync_memory_md()}."
    if action in ("add_adr", "get_adrs"):
        return tool_memory_manage(args)
    if action == "read_spec":
        name = f"{(str_arg(args, 'spec_name', 'CONTRACTS.md') or 'CONTRACTS.md').upper().removesuffix('.MD')}.md"
        if name not in CANONICAL_SPEC_FILES:
            return f"error: spec_name must be one of {CANONICAL_SPEC_FILES}"
        p = settings.specs_dir / name
        if not p.is_file():
            return f"error: file not found: {p}"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return f"# {p}  (lines 1-{len(lines)} of {len(lines)})\n" + "\n".join(f"{i:5d}|{ln}" for i, ln in enumerate(lines, 1))
    return f"error: unknown sdd_manage action '{action}'. Valid: verify, status, sync, read_spec, add_adr, get_adrs"


def tool_project_manage(args: Dict[str, Any]) -> str:
    """Manage active multi-project WorkspaceContext (`status`, `current`, `list`, `init`, `switch`, `self`)."""
    raw_act = str_arg(args, "action")
    target = str_arg(args, "target") or str_arg(args, "path") or str_arg(args, "name")
    action = (raw_act or ("switch" if target else "status")).lower()
    phase = current_workflow_phase()
    if phase and phase not in ("IMPLEMENT", "RECOVER") and action not in ("status", "current", "list"):
        return f"PHASE TOOL GATE REJECTED `project_manage(action='{action}')`: cannot switch WorkspaceContext during `{phase}` phase."
    if action in ("status", "current", "list"):
        return manage_active_project(action, store=get_memory_store())
    return manage_active_project(f"{action} {target}".strip() if target else action, store=get_memory_store())


def tool_system_doctor(args: Dict[str, Any]) -> str:
    return str(run_system_doctor(run_tests=bool_arg(args, "run_tests", True))["summary"])


def tool_telemetry_manage(args: Dict[str, Any]) -> str:
    return telemetry.handle_tool(args)
