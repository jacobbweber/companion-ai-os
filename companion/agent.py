"""
Executive Agent Loop & Interactive Session Controller (A-Frame Application Layer).

Features:
- Step-0 Adaptive Intent Routing (`CHAT_DIRECT`, `DEEP_CONSULT`, `GUIDED_CHANGE`, `AUTONOMOUS_BUILD`)
- Rolling 1-step `<think>` working scratchpad & `<tool_call>` rescue across tool steps
- Mechanical phase-scoped `AllowedTools` filtering & multi-project `WorkspaceContext` HUD
- LoopGuard protection, 2-stage lossless compaction, and grounded budget synthesis
"""

import contextvars
import json
import time
from typing import Any, Callable, Dict, List, Optional

from companion.archive_and_approvals import consume_session_nudges, session_id_context
from companion.autonomy import run_autonomous_goal, run_autonomous_goal_slice
from companion.blueprint import (approve_blueprint_and_build, classify_blueprint_reply, create_or_revise_blueprint, handle_blueprint_conversation_turn, is_awaiting_blueprint_approval)
from companion.config import agent_tool_turn_context, settings
from companion.context import (build_system_prompt, compact_messages_if_needed, estimate_messages_chars, estimate_messages_tokens, format_context_budget_status, format_identity_summary, format_skills_catalog, manage_active_project, manage_companion_tone, manage_model_tier, run_first_boot_onboarding)
from companion.core_actions import HARVEST_SLASH_VERBS, handle_harvest_slash
from companion.dag_runtime import register_dag_runtime_bindings
from companion.education_engine import get_education_engine
from companion.eval_harness import run_live_flight_eval
from companion.hygiene import analyze_workspace_hygiene
from companion.intake import select_turn_step_tier, triage_turn
from companion.llm import chat, current_tier_name, normalize_assistant_message, recover_empty_synthesis, tier_context
from companion.mcp_hub import get_mcp_hub
from companion.memory_store import MemoryStore, get_memory_store, memory_store_context
from companion.project_graph import get_project_graph
from companion.routines import execute_autonomy_mode_action, format_morning_briefing
from companion.safety import LoopGuard, run_system_doctor
from companion.sdd import verify_sdd
from companion.streaming import emit_stream_event
from companion.telemetry import telemetry
from companion.tools import current_workflow_phase, dispatch, filter_tools_for_phase, phase_tool_context
from companion.wiki_curation import pending_curation_nudge
from companion.workflow_graph import workflow_engine


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


def _execute_tool_calls(
    tool_calls: List[Dict[str, Any]], messages: List[Dict[str, Any]],
    loop_guard: LoopGuard, mem: MemoryStore, session_tag: Optional[str], verbose: bool,
) -> Optional[bool]:
    """Dispatch each tool call in a turn with LoopGuard, SQLite logging, and doctor status tracking."""
    doctor_ok: Optional[bool] = None
    for tc in tool_calls:
        fn_spec = tc.get("function") or {}
        name, raw_args = fn_spec.get("name", "unknown"), fn_spec.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError:
            args = {}
        if verbose:
            _safe_print(f"  [tool] {name}({json.dumps(args)})")

        intervention = loop_guard.check_before_call(name, args)
        if intervention is not None:
            if telemetry.active_trace_id:
                telemetry.log("WARN", "safety", f"LoopGuard blocked repeated tool call `{name}`")
            result = intervention
        else:
            with agent_tool_turn_context():
                result = dispatch(name, args)
            if name == "system_doctor":
                doctor_ok = "HEALTHY (PASS)" in result
            recovery_hint = loop_guard.record_result(name, result)
            if recovery_hint:
                if telemetry.active_trace_id:
                    telemetry.log("WARN", "safety", f"LoopGuard injected recovery hint after errors in `{name}`")
                result = f"{result}\n{recovery_hint}"
        if verbose:
            preview = result[:120].replace("\n", " ")
            _safe_print(f"  [->] {preview}{'...' if len(result) > 120 else ''}")
        tc_id = tc.get("id")
        messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})
        mem.log_message("tool", f"{name}({json.dumps(args)}) -> {result[:settings.out_limit]}", tool_call_id=tc_id, session_tag=session_tag)
    return doctor_ok


def _resolve_effective_phase(mem: MemoryStore, intent_mode: str = "") -> str:
    explicit = current_workflow_phase()
    if explicit:
        return explicit
    if _SUBAGENT_DEPTH.get() == 0:
        active_wf = mem.get_active_workflow()
        if active_wf and active_wf["current_state"] in ("DISCOVER", "DESIGN", "PLAN", "VERIFY", "REFLECT"):
            return str(active_wf["current_state"])
        if intent_mode == "DEEP_CONSULT":
            return "DISCOVER"
    return ""


def _select_step_tier(
    top_tier: Optional[str], mem: MemoryStore, consecutive_errors: int,
    step: int = 0, intent_mode: str = "CHAT_DIRECT", prior_tier: Optional[str] = None,
    suggested_tier: Optional[str] = None, allow_error_escalation: Optional[bool] = None,
) -> Optional[str]:
    """Select mechanical model tier via `companion.intake.select_turn_step_tier` across multi- or single-GPU boxes."""
    sub_depth = _SUBAGENT_DEPTH.get()
    active_wf = mem.get_active_workflow() if sub_depth == 0 else None
    wf_state = str(active_wf["current_state"]) if active_wf else None
    return select_turn_step_tier(
        top_tier, wf_state, consecutive_errors,
        step=step, intent_mode=intent_mode, prior_tier=prior_tier,
        subagent_depth=sub_depth, suggested_tier=suggested_tier,
        allow_error_escalation=allow_error_escalation,
    )


def _synthesize_turn_budget_exit(
    messages: List[Dict[str, Any]], chat_fn: Callable[..., Dict[str, Any]],
    mem: MemoryStore, session_tag: Optional[str], tid: Optional[str], doctor_pass: Optional[bool],
) -> str:
    """Perform a grounded, anti-hallucination final synthesis when `MAX_TOOL_ITERS` is reached."""
    active_wf, pending_cnt = mem.get_active_workflow(), len(mem.get_pending_tasks())
    wf_state = active_wf["current_state"] if active_wf else "IDLE"
    doc_badge = "PASS" if doctor_pass is True else ("FAIL" if doctor_pass is False else "NOT_RUN")
    synth_text = (
        "Tool iteration budget reached. Do NOT call any more tools.\n"
        f"GROUND TRUTH: workflow_state=`{wf_state}`, pending_tasks={pending_cnt}, system_doctor_in_this_turn=`{doc_badge}`.\n"
        "STRICT HONESTY RULE: Do NOT claim tests, hygiene, or system_doctor passed unless `system_doctor_in_this_turn` is `PASS`. "
        "Provide an honest summary of completed steps, what remains unverified, and the exact next step."
    )
    closing_msgs = (
        messages + [{"role": "assistant", "content": "I have completed the tool steps for this turn."}, {"role": "user", "content": synth_text}]
        if messages and messages[-1].get("role") == "tool"
        else messages + [{"role": "system", "content": synth_text}]
    )
    try:
        final_data = chat_fn(closing_msgs, None, "none")
        final_msg, _, _ = normalize_assistant_message(final_data["choices"][0]["message"])
        if not final_msg.strip():
            final_msg = recover_empty_synthesis(closing_msgs, chat_fn)
        if final_msg.strip():
            messages.append({"role": "assistant", "content": final_msg})
            mem.log_message("assistant", final_msg, session_tag=session_tag)
            if tid:
                telemetry.end_trace(tid, status="ok")
            return final_msg
    except Exception:
        pass
    fallback = f"I reached the tool step limit for this turn (workflow=`{wf_state}`, pending_tasks={pending_cnt}, system_doctor=`{doc_badge}`). Progress is saved to SQLite memory; send `/goal resume` to finish verification."
    messages.append({"role": "assistant", "content": fallback})
    mem.log_message("assistant", fallback, session_tag=session_tag)
    if tid:
        telemetry.end_trace(tid, status="ok")
    return fallback


def _extract_turn_widget_fence(messages: List[Dict[str, Any]], start_idx: int) -> str:
    """Return the latest fenced `companion-flashcards` widget emitted by tools in this turn, if any."""
    latest = ""
    for msg in messages[max(0, start_idx):]:
        if msg.get("role") != "tool":
            continue
        raw = str(msg.get("content") or "")
        marker = "```companion-flashcards"
        if marker in raw:
            sub = raw[raw.rfind(marker):]
            end = sub.find("```", len(marker))
            if end != -1:
                latest = sub[: end + 3].strip()
    return latest


def _run_step_loop(
    messages: List[Dict[str, Any]], iter_limit: int, user_text: str, intent_mode: str, top_tier: Optional[str],
    chat_fn: Callable[..., Dict[str, Any]], mem: MemoryStore, session_tag: Optional[str], tid: Optional[str], verbose: bool,
    suggested_tier: Optional[str] = None, allow_error_escalation: Optional[bool] = None, nudge_banner: str = "",
) -> str:
    """Execute the step-by-step tool loop with rolling `<think>` scratchpad and phase-scoped tool gates."""
    loop_guard, last_doctor_pass, rolling_scratchpad, prior_step_tier = LoopGuard(), None, "", None
    turn_iso, cur_nudges, turn_start_idx = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), 0, len(messages)
    for step in range(iter_limit):
        sys_prompt = build_system_prompt(store=mem, user_query=user_text) + (f"\n\n{nudge_banner}\n" if nudge_banner else "")
        if step >= 1 and rolling_scratchpad:
            sys_prompt += f"\n[PRIOR STEP WORKING SCRATCHPAD]\n{rolling_scratchpad}\n"
        if step >= 2 and user_text != "turn":
            sys_prompt += f"\n[ACTIVE TURN OBJECTIVE]\nStay focused on the user's current request: {user_text[:280]}\n"
        messages[0]["content"] = sys_prompt
        remaining = iter_limit - step
        if _SUBAGENT_DEPTH.get() > 0 and remaining == 1 and step >= 2:
            break
        if remaining in (1, 2, 4) and step > 0 and messages and messages[-1].get("role") == "tool":
            messages[-1]["content"] = str(messages[-1].get("content", "")) + (
                f"\n\n[ITERATION BUDGET NOTICE: Only {remaining} tool iteration(s) remain in this turn. "
                "Do NOT start new multi-step file edits or add unused imports. Finish your current check and provide your clear final response.]"
            )
        eff_phase = _resolve_effective_phase(mem, intent_mode=intent_mode)
        step_tier = _select_step_tier(
            top_tier, mem, loop_guard.consecutive_errors, step=step, intent_mode=intent_mode,
            prior_tier=prior_step_tier, suggested_tier=suggested_tier, allow_error_escalation=allow_error_escalation,
        )
        prior_step_tier = step_tier
        if step == 0 and not current_workflow_phase():
            emit_stream_event({"type": "phase", "phase": eff_phase or intent_mode, "tier": step_tier or "fast"})
        if (session_tag or "").startswith("autonomy:design:"):
            step_tools = None
        elif (session_tag or "").startswith("autonomy:plan:"):
            step_tools = [t for t in filter_tools_for_phase(eff_phase) if t["function"]["name"] in ("task_manage", "read_file", "list_dir")]
        else:
            step_tools = filter_tools_for_phase(eff_phase)
        with tier_context(step_tier), phase_tool_context(eff_phase):
            data = chat_fn(messages, step_tools, "none" if step_tools is None else "auto")
        last_tokens = int((data.get("usage") or {}).get("total_tokens") or 0) if isinstance(data.get("usage"), dict) else 0
        raw_asst = data["choices"][0]["message"]
        clean_text, thinking_text, tool_calls = normalize_assistant_message(raw_asst)
        if thinking_text:
            rolling_scratchpad = thinking_text[-900:].strip()
        if not tool_calls:
            cur_nudge = pending_curation_nudge(mem, turn_iso, clean_text) if (step_tools and step + 1 < iter_limit and cur_nudges < 2) else None
            if cur_nudge:
                cur_nudges += 1
                messages.extend([{"role": "assistant", "content": clean_text or "Continuing curation."}, {"role": "user", "content": cur_nudge}])
                continue
            with tier_context(step_tier):
                final_text = clean_text if clean_text.strip() else recover_empty_synthesis(messages, chat_fn, prior_thinking=thinking_text)
            widget_fence = _extract_turn_widget_fence(messages, turn_start_idx)
            if widget_fence and "```companion-flashcards" not in final_text:
                final_text = f"{final_text.rstrip()}\n\n{widget_fence}".strip()
            messages.append({"role": "assistant", "content": final_text})
            mem.log_message("assistant", final_text, session_tag=session_tag)
            if tid:
                telemetry.end_trace(tid, status="ok")
            return final_text
        messages.append({"role": "assistant", "content": clean_text, "tool_calls": tool_calls, **({"_provider_meta": raw_asst["_provider_meta"]} if "_provider_meta" in raw_asst else {})})
        with phase_tool_context(eff_phase):
            doc_res = _execute_tool_calls(tool_calls, messages, loop_guard, mem, session_tag, verbose)
        if doc_res is not None:
            last_doctor_pass = doc_res
        compact_messages_if_needed(messages, store=mem, last_usage_tokens=last_tokens, tier=top_tier)
    return _synthesize_turn_budget_exit(messages, chat_fn, mem, session_tag, tid, last_doctor_pass)


def run_agent_turn(
    messages: List[Dict[str, Any]], max_iters: Optional[int] = None, chat_fn: Callable[..., Dict[str, Any]] = chat,
    store: Optional[MemoryStore] = None, session_tag: Optional[str] = None, verbose: bool = True, auto_promote: Optional[bool] = None,
) -> str:
    """Execute one complete user turn with Step-0 intent routing, scratchpad continuity, and safety guards."""
    mem, iter_limit = store or get_memory_store(), max_iters or settings.max_iters
    sub_depth = _SUBAGENT_DEPTH.get()
    is_top = sub_depth == 0 and not (session_tag or "").startswith(("autonomy:", "subagent:"))
    nudge_banner = consume_session_nudges(session_id=session_tag or "main", store=mem) if is_top else ""
    fresh_system = build_system_prompt(store=mem) + (f"\n\n{nudge_banner}\n" if nudge_banner else "")
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = fresh_system
    else:
        messages.insert(0, {"role": "system", "content": fresh_system})

    is_user_msg = len(messages) >= 2 and messages[-1].get("role") == "user"
    user_text = str(messages[-1].get("content", "")) if is_user_msg else "turn"
    if is_user_msg:
        mem.log_message("user", user_text, session_tag=session_tag)
    awaiting_bp = is_top and is_awaiting_blueprint_approval(mem)
    bp_action = classify_blueprint_reply(user_text) if awaiting_bp else None
    intent = triage_turn(user_text, store=mem, root=settings.active_project_root) if is_top else triage_turn("")
    can_orchestrate = is_top and not awaiting_bp and (auto_promote is True or (auto_promote is None and max_iters is None and mem.get_active_workflow() is None))
    code_build_allowed = (not settings.get_workspace().is_self_project) or settings.is_self_dev_enabled() or intent.risk_tier == "R3"
    should_blueprint = can_orchestrate and intent.needs_blueprint and code_build_allowed
    should_promote = can_orchestrate and not should_blueprint and intent.mode == "AUTONOMOUS_BUILD" and code_build_allowed
    tid = telemetry.start_trace(trace_type=session_tag or "user_turn", name=user_text[:100]) if mem.db_path.resolve() == settings.db_path.resolve() else None
    if awaiting_bp and is_user_msg and not user_text.strip().startswith(("/", ".")):
        try:
            if bp_action == "APPROVE":
                res = approve_blueprint_and_build(chat_fn=chat_fn, store=mem, verbose=verbose)
            elif bp_action == "ABORT":
                res = {"passed": True, "summary": dispatch("workflow_manage", {"action": "abort", "summary": "Blueprint cancelled by user"})}
            else:
                res = handle_blueprint_conversation_turn(user_text, chat_fn=chat_fn, store=mem, verbose=verbose)
            summary = str(res["summary"])
            messages.append({"role": "assistant", "content": summary})
            mem.log_message("assistant", summary, session_tag=session_tag)
            telemetry.end_trace(tid, status="ok" if res.get("passed") else "error")
            return summary
        except Exception:
            telemetry.end_trace(tid, status="error")
            raise
    if can_orchestrate and intent.route in ("EXECUTE_VERIFIED_DAG", "ADVANCE_ACTIVE_GOAL"):
        try:
            if intent.route == "EXECUTE_VERIFIED_DAG":
                dag_res = workflow_engine.run_dag(intent.matched_dag, params={"goal": user_text})
                summary, ok_flag = str(dag_res.get("summary") or ""), bool(dag_res.get("ok") or dag_res.get("status") == "waiting_approval")
            else:
                sl_res = run_autonomous_goal_slice(project_id=intent.target_project_id or None, chat_fn=chat_fn, store=mem, verbose=verbose)
                summary, ok_flag = str(sl_res.get("summary") or ""), bool(sl_res.get("ok"))
            messages.append({"role": "assistant", "content": summary})
            mem.log_message("assistant", summary, session_tag=session_tag)
            telemetry.end_trace(tid, status="ok" if ok_flag else "error")
            return summary
        except Exception:
            telemetry.end_trace(tid, status="error")
            raise
    if should_blueprint or should_promote:
        try:
            if tid:
                telemetry.log("INFO", "autonomy", f"Step-0 Intent Router promoted turn to {'BLUEPRINT_ALIGN' if should_blueprint else 'AUTONOMOUS_BUILD'}: {user_text[:90]}")
            res = create_or_revise_blueprint(user_text, chat_fn=chat_fn, store=mem, verbose=verbose) if should_blueprint else run_autonomous_goal(user_text, chat_fn=chat_fn, store=mem, verbose=verbose)
            summary = str(res["summary"])
            messages.append({"role": "assistant", "content": summary})
            mem.log_message("assistant", summary, session_tag=session_tag)
            telemetry.end_trace(tid, status="ok" if res.get("passed") else "error")
            return summary
        except Exception:
            telemetry.end_trace(tid, status="error")
            raise

    automated_tag = (session_tag or "").startswith(("autonomy:", "subagent:"))
    phase_tier = session_tag.split(":")[2] if automated_tag and (session_tag or "").count(":") >= 2 else None
    top_tier = phase_tier or (mem.get_user_profile().get("active_model_tier", "auto") if sub_depth == 0 else current_tier_name())
    can_escalate = (not top_tier or top_tier.strip().lower() == "auto") or automated_tag or sub_depth > 0
    compact_messages_if_needed(messages, store=mem, tier=top_tier)
    try:
        with tier_context(top_tier), memory_store_context(mem), session_id_context(session_tag or "main"):
            return _run_step_loop(
                messages, iter_limit, user_text, intent.mode, top_tier, chat_fn, mem, session_tag, tid, verbose,
                suggested_tier=intent.suggested_tier, allow_error_escalation=can_escalate, nudge_banner=nudge_banner,
            )
    except Exception:
        telemetry.end_trace(tid, status="error")
        raise


_SUBAGENT_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("companion_subagent_depth", default=0)


def run_subagent(
    goal: str, skill_name: Optional[str] = None, max_iters: int = 8, chat_fn: Callable[..., Dict[str, Any]] = chat,
    store: Optional[MemoryStore] = None, verbose: bool = True, tier: Optional[str] = None,
) -> str:
    """Spawn an isolated worker loop on the requested model tier (`fast` or `reasoning`) and return its synthesis."""
    clean_goal = (goal or "").strip()
    if not clean_goal:
        return "error: goal is required for run_subagent"
    sub_depth = _SUBAGENT_DEPTH.get()
    if sub_depth >= 2:
        return "error: maximum subagent nesting depth (2) reached; execute steps directly."

    mem = store or get_memory_store()
    tier_cfg = settings.resolve_tier((tier or current_tier_name() or "fast").strip().lower())
    clean_skill = (skill_name or "").strip().lower()
    skill_path = next((d / clean_skill / "SKILL.md" for d in (settings.user_skills_dir, settings.skills_dir) if (d / clean_skill / "SKILL.md").is_file()), None) if clean_skill else None
    skill_block = f"\n\n[PRE-LOADED TIER-2 SKILL RUNBOOK (`skills/{clean_skill}/SKILL.md`)]\n{skill_path.read_text(encoding='utf-8', errors='replace').strip()}" if skill_path and skill_path.is_file() else ""
    eff_iters = max(1, min(int(max_iters or 8), settings.max_iters))
    worker_prompt = (
        f"[ISOLATED SUBAGENT WORKER TASK]\nGoal: {clean_goal}\n"
        f"Model Tier: `{tier_cfg.tier}` (`{tier_cfg.model}`) | Tool Budget: max {eff_iters} tool steps\nSkill Focus: `{clean_skill or 'general-worker'}`\n"
        "Instructions: Execute the goal autonomously in this isolated context window. Keep file/tool exploration surgical (1-3 targeted reads) "
        f"and return a concise, self-contained executive & technical summary.{skill_block}"
    )
    tag, parent_tid, t0 = f"subagent:{clean_skill or 'worker'}:{tier_cfg.tier}", telemetry.active_trace_id, time.perf_counter()
    depth_tok = _SUBAGENT_DEPTH.set(sub_depth + 1)
    try:
        with tier_context(tier_cfg.tier):
            summary = run_agent_turn([{"role": "user", "content": worker_prompt}], max_iters=eff_iters, chat_fn=chat_fn, store=mem, session_tag=tag, verbose=verbose, auto_promote=False)
        if parent_tid:
            telemetry.record_span("subagent_call", tag, (time.perf_counter() - t0) * 1000.0, status="ok", detail=f"tier={tier_cfg.tier} {summary[:120].replace(chr(10), ' ')}", trace_id=parent_tid)
        return summary
    except Exception as exc:
        if parent_tid:
            telemetry.record_span("subagent_call", tag, (time.perf_counter() - t0) * 1000.0, status="error", detail=str(exc)[:140], trace_id=parent_tid)
        raise
    finally:
        _SUBAGENT_DEPTH.reset(depth_tok)
        if parent_tid:
            telemetry.active_trace_id = parent_tid


def _run_bounded_dag_llm(prompt: str, system_prompt: str = "", tier: str = "fast") -> str:
    """Execute a single zero-tool bounded LLM synthesis step for `dag_runtime`."""
    msgs = [
        {"role": "system", "content": system_prompt or "Produce a concise, grounded output without calling tools."},
        {"role": "user", "content": prompt},
    ]
    with tier_context((tier or "fast").strip().lower()):
        data = chat(msgs, None, "none")
    clean, _, _ = normalize_assistant_message((data.get("choices") or [{}])[0].get("message") or {})
    return clean.strip()


def ensure_dag_runtime_bound() -> None:
    """Ensure `workflow_engine` and `dag_runtime` executor bindings are registered."""
    workflow_engine.bind_executors(
        tool_fn=dispatch,
        subagent_fn=lambda g, s=None, m=6: run_subagent(goal=g, skill_name=s, max_iters=m, verbose=False),
        llm_fn=_run_bounded_dag_llm,
    )
    register_dag_runtime_bindings(
        tool_fn=dispatch,
        mcp_fn=lambda srv, tn, a: get_mcp_hub().call_mcp_tool(srv, tn, arguments=a, actor="companion"),
        llm_fn=_run_bounded_dag_llm,
    )


ensure_dag_runtime_bound()


def check_in(store: Optional[MemoryStore] = None) -> str:
    """Provide a friendly proactive check-in summary of Companion's current state."""
    mem, ws = store or get_memory_store(), settings.get_workspace()
    wf, spr, edu = mem.get_active_workflow(), get_project_graph(store=mem).get_active_timebox(), get_education_engine(store=mem).get_progress_summary()
    ac = edu.get("active_course")
    bg_lbl = "ON" if mem.is_background_autonomy_enabled() else "OFF"
    sd_lbl = "ON" if mem.is_self_dev_mode_enabled() else "OFF (Core Read-Only)"
    spr_line = f"`{spr['title']}` ({spr['start_date']}..{spr['end_date']})" if spr else "(None active)"
    edu_line = f"`{ac['title']}` ({ac['mastery_pct']}% mastery, {edu['due_total']} due)" if ac else f"(None active, {edu['due_total']} due)"
    return (
        f"=== Proactive Check-In ===\nActive Project  : `{ws.project_id}` ({ws.project_root})\n"
        f"Autonomy Levers : Background Autonomy={bg_lbl} | Self-Dev Mode={sd_lbl}\n"
        f"Workflow State  : `{wf['current_state'] if wf else 'IDLE'}`\n"
        f"Active Timebox  : {spr_line}\n"
        f"Study Course    : {edu_line}\n"
        f"Pending Tasks   : {len(mem.get_pending_tasks()[:6])} active | Pending Backlog: {len(mem.get_backlog(status='pending')[:6])}\n"
        f"Recent Notes    : {len(mem.get_system_notes(limit=5))} notes saved\n"
        "All systems operational. Ready for your next request."
    )


SLASH_HELP_TEXT = (
    "=== Companion AI OS Commands ===\n"
    "  /status                 — Show live system status, WorkspaceContext, Background Autonomy, workflow HUD & tasks\n"
    "  /project [init|switch..]— Manage isolated workspaces (`/project list`, `init <name>`, `switch <name>`, `self`)\n"
    "  /model & /providers     — View, ping (`check`), switch model tiers, or inspect multi-provider & Vault matrix\n"
    "  /identity               — View Companion Soul & User Identity grounding cards\n"
    "  /tone [concise|...]     — View or set communication tone (`concise`, `balanced`, `elaborate`)\n"
    "  /onboard                — Run the 4-question First-Boot Soul & User Identity setup\n"
    "  /checkin                — Friendly executive check-in summary\n"
    "  /telemetry              — View live KPI & Observability Scorecard\n"
    "  /traces [id]            — View Waterfall execution traces for turns & routines\n"
    "  /logs [level|component] — View structured application logs (DEBUG, INFO, WARN, ERROR)\n"
    "  /doctor                 — Run 7-stage System Doctor (Config, Syntax, Arch, SDD, Hygiene, DB, Tests)\n"
    "  /autonomy & /selfdev    — Background Autonomy (`/autonomy on|off|run`) & Human-Only Self-Dev Mode (`/selfdev on|off|status`)\n"
    "  /morning                — View Morning Briefing of overnight background routines & goal slices\n"
    "  /specs                  — Verify 4-File Canonical SDD alignment & list spec status\n"
    "  /skills [name]          — List Progressive-Disclosure `SKILL.md` runbooks or read a skill\n"
    "  /plan <obj|revise ...>  — Draft or revise a read-only Blueprint (`DISCOVER -> DESIGN -> PLAN`) before coding\n"
    "  /approve                — Approve the pending Blueprint and execute `IMPLEMENT -> VERIFY -> COMPLETE`\n"
    "  /goal <obj|resume>      — Execute or resume a multi-phase autonomous SDD build with doctor gates\n"
    "  /eval [quick|full|all]  — Run Live Local-Model Flight Eval (`quick`, `full`, `general`, `all`, `compare`)\n"
    "  /subagent <goal>        — Spawn an isolated worker (`--tier reasoning` & `--skill <name>` optional)\n"
    "  /hygiene                — Run AST Dead-Code, Unused Import & Simplicity Budget scan\n"
    "  /workflow [dags|run]    — Show State-Machine Workflow graph or run a verified Python DAG\n"
    "  /backlog                — List all tasks and prioritized backlog items\n"
    "  /memory                 — Sync and display MEMORY.md\n"
    "  /compact [status|now]   — View context watermark status or trigger 2-stage compaction\n"
    "  /jobs [dispatch|status] —Forever-Chat Background Job Dispatcher & Departures Board\n"
    "  /wiki [list|read|graph] — Co-Owned 4-Folder Knowledge Wiki Warehouse & 2D Mind Map\n"
    "  /vault [list|set|audit] — Zero-Reveal Blind Credential Vault (`secrets.env` + scopes)\n"
    "  /endpoints [add|test]   — Universal SSH, Windows WinRM & Authenticated HTTP API Registry\n"
    "  /mcp & /partners        — Outbound MCP Client, Hosted Order Counter & Federated Partners\n"
    "  /routines & /approvals  — Hardware-Gated Routines, Trust Mode (`/trust`), Backups (`/backup`) & Sandbox (`/sandbox`)\n"
    "  /board & /sprint        — Universal Multi-Board Kanban, Weekly Sprint Slots & 15m Unblocker (`/boards`, `/sprint`)\n"
    "  /calendar               — Unified Multi-Source Calendar (Events, Active Sprint, Deadlines, Routines & SRS)\n"
    "  /study                  — Conversational Socratic Courses, Mastery Progress & Inline Flashcards (`/study flashcards`)\n"
    "  /checkpoint [git|log..] — Create/list workspace checkpoints or local Git save-points (`/checkpoint git`)\n"
    "  /rollback [id]          — Rollback workspace to a checkpoint\n"
    "  /reset                  — Clear active chat window (SQLite history is preserved)\n"
    "  exit / quit             — Exit the interactive REPL"
)


def _run_manual_compact(messages: List[Dict[str, Any]], store: MemoryStore) -> str:
    before_tok, before_chars = estimate_messages_tokens(messages), estimate_messages_chars(messages)
    ctid = telemetry.start_trace("compaction", "Manual /compact") if telemetry.active_trace_id is None else None
    compact_messages_if_needed(messages, store=store, force=True)
    if ctid:
        telemetry.end_trace(ctid, status="ok")
    after_tok, after_chars = estimate_messages_tokens(messages), estimate_messages_chars(messages)
    return f"Compaction complete (~{before_tok:,} -> ~{after_tok:,} tokens | {before_chars:,} -> {after_chars:,} chars).\n\n{format_context_budget_status(messages)}"


def _dispatch_subagent_slash(arg: str) -> str:
    if not arg:
        return "Usage: `/subagent [--tier <fast|coding|reasoning>] [--skill <skill-name>] <goal>`"
    skill_arg, tier_arg, rest = None, None, arg.strip()
    while rest.startswith(("--skill ", "--tier ")):
        toks = rest.split(None, 2)
        if toks[0] == "--skill" and len(toks) > 1:
            skill_arg = toks[1]
        elif toks[0] == "--tier" and len(toks) > 1:
            tier_arg = toks[1]
        rest = toks[2] if len(toks) > 2 else ""
    stid = telemetry.start_trace("slash_subagent", f"/subagent {arg[:80]}") if telemetry.active_trace_id is None else None
    try:
        return dispatch("workflow_manage", {"action": "subagent", "skill": skill_arg, "tier": tier_arg, "goal": rest})
    finally:
        if stid:
            telemetry.end_trace(stid, status="ok")


def _dispatch_workflow_slash(arg: str) -> str:
    sub, rest = (arg.split()[0].lower() if arg else "status"), (arg.split(None, 1)[1].strip() if len(arg.split(None, 1)) > 1 else "")
    if sub in ("abort", "cancel", "reset"):
        return dispatch("workflow_manage", {"action": "abort", "summary": "Aborted via CLI slash command"})
    if sub in ("dags", "list_dags", "recipes", "list_recipes"):
        return dispatch("workflow_manage", {"action": "list_dags"})
    if sub in ("run", "run_dag", "run_recipe") and rest:
        r_parts = rest.split(None, 1)
        return dispatch("workflow_manage", {"action": "run_dag", "dag": r_parts[0], "params": {"goal": r_parts[1]} if len(r_parts) > 1 else {}})
    if sub == "resume_dag" and rest:
        return dispatch("workflow_manage", {"action": "resume_dag", "run_id": rest})
    return dispatch("workflow_manage", {"action": "status"})


def _dispatch_checkpoint_slash(arg: str) -> str:
    low = arg.lower().strip()
    if low == "list":
        return dispatch("checkpoint_manage", {"action": "list"})
    if low in ("log", "git_log", "git log"):
        return dispatch("checkpoint_manage", {"action": "git_log"})
    if low == "git" or low.startswith("git "):
        msg = arg.split(None, 1)[1].strip() if " " in arg.strip() else "Manual Git save-point"
        return dispatch("checkpoint_manage", {"action": "git_save", "label": msg})
    lbl = arg.split(None, 1)[1].strip() if low.startswith("create ") else (arg or "user_manual")
    return dispatch("checkpoint_manage", {"action": "create", "label": lbl})


def handle_slash_command(cmd_str: str, messages: List[Dict[str, Any]], store: Optional[MemoryStore] = None, chat_fn: Callable[..., Dict[str, Any]] = chat, interactive: bool = True) -> Optional[str]:
    """Handle built-in deterministic CLI/REPL/Web slash and dot commands."""
    cleaned = cmd_str.strip()
    if not cleaned.startswith(("/", ".")):
        return None
    parts = cleaned[1:].split(None, 1)
    if not parts:
        return None
    verb, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
    mem = store or get_memory_store()

    if verb in ("help", "h", "?"):
        return SLASH_HELP_TEXT
    if verb in ("reset", "clear"):
        messages[:] = [{"role": "system", "content": build_system_prompt(store=mem)}]
        return "(active conversation context cleared; persistent SQLite memory retained)"
    if verb in ("identity", "soul"):
        return format_identity_summary(store=mem)
    if verb in ("tone", "model", "models", "project", "proj", "workspace"):
        out = manage_active_project(arg or None, store=mem) if verb in ("project", "proj", "workspace") else (manage_companion_tone(arg or None, store=mem) if verb == "tone" else manage_model_tier(arg or None, store=mem))
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = build_system_prompt(store=mem)
        return out
    if verb in ("onboard", "onboarding", "bootstrap"):
        if not interactive:
            return format_identity_summary(store=mem) + "\n\n(Interactive `/onboard` requires a terminal TTY. Run `python harness.py` in a terminal, set `COMPANION_NAME`/`COMPANION_USER_NAME` in `.env`, or use `/tone`.)"
        out = run_first_boot_onboarding(store=mem)
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = build_system_prompt(store=mem)
        return out
    if verb in ("status", "stat"):
        ws, hud, tasks, backlog = settings.get_workspace(), workflow_engine.render_hud(), mem.get_pending_tasks(), mem.get_backlog(status="pending")
        spr, edu = get_project_graph(store=mem).get_active_timebox(), get_education_engine(store=mem).get_progress_summary()
        ac = edu.get("active_course")
        mode, f_cfg, c_cfg, r_cfg = mem.get_user_profile().get("active_model_tier", "auto"), settings.resolve_tier("fast"), settings.resolve_tier("coding"), settings.resolve_tier("reasoning")
        code_seg = f" | coding=`{c_cfg.model}` (temp={c_cfg.temperature})" if c_cfg.is_dedicated else ""
        spr_s = f"`{spr['title']}` ({spr['start_date']}..{spr['end_date']})" if spr else "(none)"
        edu_s = f"`{ac['title']}` ({ac['mastery_pct']}%, {edu['due_total']} due)" if ac else "(none)"
        return f"=== Companion AI OS v3.5 Status ===\nProject    : `{ws.project_id}` ({ws.project_root})\nRouting    : mode=`{mode}` | fast=`{f_cfg.model}` (temp={f_cfg.temperature}){code_seg} | reasoning=`{r_cfg.model}` (temp={r_cfg.temperature})\nTwo-Lever  : Background Autonomy={'ON' if mem.is_background_autonomy_enabled() else 'OFF'} | Self-Dev Mode={'ON' if mem.is_self_dev_mode_enabled() else 'OFF (Core Read-Only)'} (Idle: {settings.idle_minutes}m | Cooldown: {settings.hobby_cooldown_minutes}m)\nTimebox/Edu: Timebox={spr_s} | Study={edu_s}\n\n{hud}\n\nPending Tasks: {len(tasks)} | Pending Backlog: {len(backlog)}"
    if verb in ("doctor", "doc"):
        return str(run_system_doctor(run_tests=True)["summary"])
    if verb in ("plan", "blueprint"):
        return str(create_or_revise_blueprint(arg or "status", chat_fn=chat_fn, store=mem)["summary"])
    if verb == "approve":
        return str(approve_blueprint_and_build(chat_fn=chat_fn, store=mem)["summary"])
    if verb == "eval":
        return str(run_live_flight_eval(suite=arg or "full", chat_fn=chat_fn)["summary"])
    if verb == "goal":
        return "Usage: `/goal <feature or engineering objective>` (or `/goal resume`)" if not arg else str(run_autonomous_goal(arg, chat_fn=chat_fn, store=mem)["summary"])
    if verb in ("specs", "sdd"):
        sub = arg.split()[0].lower() if arg else "verify"
        if sub == "sync":
            return dispatch("sdd_manage", {"action": "sync"})
        return dispatch("sdd_manage", {"action": "read_spec", "spec_name": sub}) if (sub.upper().endswith(".MD") or sub.upper() in ("PRODUCT", "ARCHITECTURE", "CONTRACTS", "DECISIONS")) else verify_sdd().format_summary()
    if verb == "hygiene":
        return analyze_workspace_hygiene().format_summary()
    if verb in ("skills", "skill"):
        return format_skills_catalog(skill_name=arg or None)
    if verb in ("subagent", "worker"):
        return _dispatch_subagent_slash(arg)
    if verb == "autonomy":
        sub, sub_arg = (arg.split()[0].lower() if arg else "status"), (arg.split(None, 1)[1].strip() if len(arg.split(None, 1)) > 1 else None)
        res_aut = execute_autonomy_mode_action({"subaction": sub, "routine_id": sub_arg}, actor="cli", store=mem)
        return str(res_aut.get("summary") or res_aut.get("error") or "")
    if verb in ("morning", "journal"):
        return format_morning_briefing(store=mem, mark_seen=True)
    if verb in ("checkin", "check-in"):
        return check_in(store=mem)
    if verb in ("telemetry", "kpis", "metrics"):
        return telemetry.format_kpi_dashboard()
    if verb in ("traces", "waterfall", "trace"):
        return telemetry.format_waterfall(trace_id=arg) if arg else (telemetry.format_recent_traces() + "\n\n" + telemetry.format_waterfall())
    if verb == "logs":
        return telemetry.format_recent_logs(level=arg if arg.upper() in ("DEBUG", "INFO", "WARN", "ERROR") else None, component=arg if arg and arg.upper() not in ("DEBUG", "INFO", "WARN", "ERROR") else None)
    if verb in ("workflow", "wf"):
        return _dispatch_workflow_slash(arg)
    if verb in ("backlog", "tasks"):
        return dispatch("task_manage", {"action": "list_all"})
    if verb == "memory":
        return mem.sync_memory_md().read_text(encoding="utf-8", errors="replace")
    if verb == "compact":
        return format_context_budget_status(messages) if arg.lower() == "status" else _run_manual_compact(messages, mem)
    if verb in ("checkpoint", "cp"):
        return _dispatch_checkpoint_slash(arg)
    if verb in ("rollback", "rb"):
        return dispatch("checkpoint_manage", {"action": "rollback", "checkpoint_id": arg or None})
    if f"/{verb}" in HARVEST_SLASH_VERBS:
        out = handle_harvest_slash(f"/{verb}", arg, store=mem)
        if verb in ("trust", "selfdev", "partners", "mcp", "board", "boards", "sprint", "study", "providers") and messages and messages[0].get("role") == "system":
            messages[0]["content"] = build_system_prompt(store=mem)
        return out
    return f"Unknown slash command '/{verb}'. Type `/help` to see all available commands." if (cleaned.startswith("/") and "/" not in verb and "\\" not in verb) else None
