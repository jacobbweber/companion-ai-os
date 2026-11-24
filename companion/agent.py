"""
Executive Agent Loop & Interactive Session Controller (A-Frame Application Layer).

Features:
- Unified canonical `_run_step_loop` across `Chat`, `Plan First`, `Goal`, and `BackgroundJobManager` workers
- Frozen `~700`-token `messages[0]` prefix + `<turn_context>` wire scope for 100% KV-cache reuse across steps
- `<100ms` socket/subprocess `CancelScope` interruption, strict JSON tool-arg validation, and LoopGuard protection
"""

import contextlib
import contextvars
import json
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from companion.archive_and_approvals import consume_session_nudges, session_id_context
from companion.autonomy import run_autonomous_goal, run_autonomous_goal_slice
from companion.blueprint import (
    approve_blueprint_and_build, classify_blueprint_reply, create_or_revise_blueprint,
    handle_blueprint_conversation_turn, is_awaiting_blueprint_approval,
)
from companion.config import agent_tool_turn_context, execution_origin_context, settings
from companion.context import (
    build_system_prompt, build_turn_context, compact_messages_if_needed, consolidate_turn_memory,
    estimate_messages_chars, estimate_messages_tokens, format_context_budget_status, format_identity_summary,
    format_skills_catalog, manage_active_project, manage_companion_tone, manage_model_tier,
    run_first_boot_onboarding, should_compact,
)
from companion.core_actions import HARVEST_SLASH_VERBS, handle_harvest_slash
from companion.dag_runtime import register_dag_runtime_bindings
from companion.education_engine import get_education_engine
from companion.intake import select_turn_step_tier, triage_turn
from companion.llm import chat, current_tier_name, normalize_assistant_message, recover_empty_synthesis, tier_context
from companion.mcp_hub import get_mcp_hub
from companion.memory_store import MemoryStore, get_memory_store, memory_store_context
from companion.os_tools import summarize_tool_step
from companion.project_graph import get_project_graph
from companion.routines import execute_autonomy_mode_action, format_morning_briefing
from companion.safety import LoopGuard, run_system_doctor
from companion.sdlc_tools import preview_turn_cleanup_scope
from companion.streaming import check_cancelled, clean_subagent_wire_context, emit_stream_event, turn_context_scope
from companion.telemetry import telemetry
from companion.tools import TOOLS, current_workflow_phase, dispatch, filter_tools_for_phase, phase_tool_context
from companion.wiki_curation import pending_curation_nudge
from companion.workflow_graph import workflow_engine


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


def _parse_strict_tool_args(raw_args: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse tool arguments strictly as a JSON `dict`; return `(args, error_msg)` without silent `{}` coercion."""
    if raw_args is None or raw_args == "":
        return {}, None
    if isinstance(raw_args, dict):
        return dict(raw_args), None
    if not isinstance(raw_args, str):
        return None, f"Tool argument JSON parse error at char 0 — pass a valid JSON object, got {type(raw_args).__name__}."
    stripped = raw_args.strip()
    if not stripped:
        return {}, None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as jerr:
        return None, f"Tool argument JSON parse error at char {jerr.pos} — pass a valid JSON object ({jerr.msg})."
    if not isinstance(parsed, dict):
        return None, f"Tool argument JSON parse error at char 0 — pass a valid JSON object, got {type(parsed).__name__}."
    return parsed, None


def _execute_tool_calls(
    tool_calls: List[Dict[str, Any]], messages: List[Dict[str, Any]],
    loop_guard: LoopGuard, mem: MemoryStore, session_tag: Optional[str], verbose: bool,
    allowed_tool_names: Optional[Set[str]] = None,
    dispatch_fn: Callable[[str, Dict[str, Any]], str] = dispatch,
    on_tool_result: Optional[Callable[[str, str], Optional[str]]] = None,
    log_to_conversations: bool = True,
    tool_steps_out: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[bool], Optional[str]]:
    """Dispatch each tool call with strict JSON arg validation, LoopGuard, and optional worker pause hook."""
    doctor_ok: Optional[bool] = None
    for tc in tool_calls:
        check_cancelled()
        t_step0 = time.perf_counter()
        fn_spec = tc.get("function") or {}
        name, raw_args = str(fn_spec.get("name") or "unknown"), fn_spec.get("arguments")
        args, parse_err = _parse_strict_tool_args(raw_args)
        if parse_err is not None or args is None:
            result = parse_err or "Tool argument JSON parse error at char 0 — pass a valid JSON object."
            hint = loop_guard.record_result(name, result)
            if hint:
                result = f"{result}\n{hint}"
        elif allowed_tool_names is not None and (name == "job_manage" or name not in allowed_tool_names):
            result = "error: recursive `job_manage` is disabled inside background workers" if name == "job_manage" else f"error: tool `{name}` is not permitted for this worker"
            loop_guard.record_result(name, result)
        else:
            if verbose:
                _safe_print(f"  [tool] {name}({json.dumps(args)})")
            intervention = loop_guard.check_before_call(name, args)
            if intervention is not None:
                if telemetry.active_trace_id:
                    telemetry.log("WARN", "safety", f"LoopGuard blocked repeated tool call `{name}`")
                result = intervention
            else:
                with agent_tool_turn_context():
                    result = dispatch_fn(name, args)
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
        if tool_steps_out is not None:
            step_ok = not result.startswith(("error:", "exit=1", "SAFETY GUARD", "RISK GATE", "PHASE TOOL GATE", "Tool argument JSON"))
            tool_steps_out.append({"tool": name, "summary": summarize_tool_step(name, args or {}), "ok": step_ok, "duration_ms": round((time.perf_counter() - t_step0) * 1000.0, 1)})
        if "AWAITING_USER_APPROVAL" in result or (result.startswith("RISK GATE REJECTED (R3)") and "`apr_" in result):
            result = (
                f"{result}\n\n[ACTION QUEUED FOR USER APPROVAL — Do NOT retry, bypass, or work around this action with other tools; "
                "summarize the queued approval request calmly for the user and conclude your turn immediately.]"
            )
        tc_id = tc.get("id")
        messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})
        if log_to_conversations:
            mem.log_message("tool", f"{name}({json.dumps(args or {})}) -> {result[:settings.out_limit]}", tool_call_id=tc_id, session_tag=session_tag)
        if on_tool_result is not None:
            pause_id = on_tool_result(name, result)
            if pause_id:
                return doctor_ok, pause_id
    return doctor_ok, None


def _resolve_effective_phase(mem: MemoryStore) -> str:
    explicit = current_workflow_phase()
    if explicit:
        return explicit
    if _SUBAGENT_DEPTH.get() == 0:
        active_wf = mem.get_active_workflow()
        if active_wf and active_wf["current_state"] in ("DISCOVER", "DESIGN", "PLAN", "VERIFY", "REFLECT"):
            return str(active_wf["current_state"])
    return ""


def _select_step_tier(
    top_tier: Optional[str], mem: MemoryStore, consecutive_errors: int,
    step: int = 0, intent_mode: str = "CHAT_DIRECT", prior_tier: Optional[str] = None,
    suggested_tier: Optional[str] = None, allow_error_escalation: Optional[bool] = None,
) -> Optional[str]:
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
    log_to_conversations: bool = True,
) -> str:
    """Perform a grounded final synthesis when `MAX_TOOL_ITERS` is reached (always ending with `role='user'`)."""
    active_wf, pending_cnt = mem.get_active_workflow(), len(mem.get_pending_tasks())
    wf_state = active_wf["current_state"] if active_wf else "IDLE"
    doc_badge = "PASS" if doctor_pass is True else ("FAIL" if doctor_pass is False else "NOT_RUN")
    synth_text = (
        "Tool iteration budget reached. Do NOT call any more tools.\n"
        f"GROUND TRUTH: workflow_state=`{wf_state}`, pending_tasks={pending_cnt}, system_doctor_in_this_turn=`{doc_badge}`.\n"
        "STRICT HONESTY RULE: Do NOT claim tests, hygiene, or system_doctor passed unless `system_doctor_in_this_turn` is `PASS`. "
        "Provide an honest summary of completed steps, what remains unverified, and the exact next step."
    )
    if messages and messages[-1].get("role") == "tool":
        closing_msgs = messages + [{"role": "assistant", "content": "I have completed the tool steps for this turn."}, {"role": "user", "content": synth_text}]
    elif messages and messages[-1].get("role") == "user":
        closing_msgs = messages + [{"role": "assistant", "content": "Understood."}, {"role": "user", "content": synth_text}]
    else:
        closing_msgs = messages + [{"role": "user", "content": synth_text}]
    try:
        final_data = chat_fn(closing_msgs, None, "none")
        final_msg, _, _ = normalize_assistant_message(final_data["choices"][0]["message"])
        if not final_msg.strip():
            final_msg = recover_empty_synthesis(closing_msgs, chat_fn)
        if final_msg.strip():
            messages.append({"role": "assistant", "content": final_msg})
            if log_to_conversations:
                mem.log_message("assistant", final_msg, session_tag=session_tag)
            if tid:
                telemetry.end_trace(tid, status="ok")
            return final_msg
    except InterruptedError:
        raise
    except Exception:
        pass
    fallback = f"I reached the tool step limit for this turn (workflow=`{wf_state}`, pending_tasks={pending_cnt}, system_doctor=`{doc_badge}`). Progress is saved to SQLite memory; send `/goal resume` to finish verification."
    messages.append({"role": "assistant", "content": fallback})
    if log_to_conversations:
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
    suggested_tier: Optional[str] = None, allow_error_escalation: Optional[bool] = None, turn_ctx: str = "",
    allowed_tool_names: Optional[Set[str]] = None, dispatch_fn: Callable[[str, Dict[str, Any]], str] = dispatch,
    cancel_check: Optional[Callable[[], bool]] = None, deadline_mono: Optional[float] = None,
    on_tool_result: Optional[Callable[[str, str], Optional[str]]] = None, log_to_conversations: bool = True,
) -> Tuple[str, str, str]:
    """Canonical step loop returning `(status, output_text, meta_id_or_err)` while keeping `messages[0]` frozen."""
    loop_guard, last_doctor_pass, rolling_scratchpad, prior_step_tier = LoopGuard(), None, "", None
    turn_t0, turn_thoughts, turn_tools = time.perf_counter(), [], []
    turn_iso, cur_nudges, turn_start_idx = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), 0, max(1, len(messages) - 1)
    with turn_context_scope(turn_ctx):
        for step in range(iter_limit):
            check_cancelled()
            if cancel_check is not None and cancel_check():
                return "cancelled", "", "Cancelled by user."
            if deadline_mono is not None and time.monotonic() > deadline_mono:
                return "timed_out", "", "Timed out."
            remaining = iter_limit - step
            if _SUBAGENT_DEPTH.get() > 0 and remaining == 1 and step >= 2:
                break
            if step >= 1 and messages and messages[-1].get("role") == "tool":
                notes = ([f"[PRIOR STEP WORKING SCRATCHPAD] (Prior step context — the tool call above ALREADY executed; proceed to the next step without repeating it):\n{rolling_scratchpad}"] if rolling_scratchpad else []) + (
                    [f"[ITERATION BUDGET NOTICE: Only {remaining} tool iteration(s) remain in this turn. Complete any required final step (such as git commit) and provide your clear final response.]"] if remaining in (1, 2) else []
                )
                if notes:
                    messages[-1]["content"] = str(messages[-1].get("content", "")) + "\n\n" + "\n\n".join(notes)

            eff_phase = _resolve_effective_phase(mem)
            step_tier = _select_step_tier(
                top_tier, mem, loop_guard.consecutive_errors, step=step, intent_mode=intent_mode,
                prior_tier=prior_step_tier, suggested_tier=suggested_tier, allow_error_escalation=allow_error_escalation,
            )
            prior_step_tier = step_tier
            if step == 0 and not current_workflow_phase():
                emit_stream_event({"type": "phase", "phase": eff_phase or intent_mode, "tier": step_tier or "fast"})
            tag_s = session_tag or ""
            step_tools = (
                None if tag_s.startswith("autonomy:design:")
                else [t for t in filter_tools_for_phase(eff_phase) if t["function"]["name"] in ("task_manage", "read_file", "list_dir")] if tag_s.startswith("autonomy:plan:")
                else [t for t in TOOLS if t["function"]["name"] in allowed_tool_names] if allowed_tool_names is not None
                else filter_tools_for_phase(eff_phase)
            )
            with tier_context(step_tier), phase_tool_context(eff_phase):
                data = chat_fn(messages, step_tools if step_tools else None, "none" if not step_tools else "auto")
            last_tokens = int((data.get("usage") or {}).get("total_tokens") or 0) if isinstance(data.get("usage"), dict) else 0
            raw_asst = data["choices"][0]["message"]
            clean_text, thinking_text, tool_calls = normalize_assistant_message(raw_asst)
            if thinking_text:
                rolling_scratchpad = thinking_text[-900:].strip()
                turn_thoughts.append(thinking_text.strip())
            if not tool_calls:
                allow_wiki = bool(step_tools and step + 1 < iter_limit and cur_nudges < 2 and (allowed_tool_names is None or "wiki_manage" in allowed_tool_names))
                cur_nudge = pending_curation_nudge(mem, turn_iso, clean_text, require_intent_phrase=(allowed_tool_names is None)) if allow_wiki else None
                if cur_nudge:
                    cur_nudges += 1
                    messages.extend([{"role": "assistant", "content": clean_text or "Continuing curation."}, {"role": "user", "content": cur_nudge}])
                    continue
                with tier_context(step_tier):
                    final_text = clean_text if clean_text.strip() else recover_empty_synthesis(messages, chat_fn, prior_thinking=thinking_text)
                wfence = _extract_turn_widget_fence(messages, turn_start_idx)
                if wfence and "```companion-flashcards" not in final_text:
                    final_text = f"{final_text.rstrip()}\n\n{wfence}".strip()
                messages.append({"role": "assistant", "content": final_text})
                if log_to_conversations:
                    t_meta = {"thinking": "\n\n".join(turn_thoughts)[-4000:], "tool_steps": turn_tools, "duration_ms": round((time.perf_counter() - turn_t0) * 1000.0, 1), "model": settings.resolve_tier(step_tier).model}
                    mem.log_message("assistant", final_text, session_tag=session_tag, meta=t_meta)
                if tid:
                    telemetry.end_trace(tid, status="ok")
                return "done", final_text, ""

            messages.append({"role": "assistant", "content": clean_text, "tool_calls": tool_calls, **({"_provider_meta": raw_asst["_provider_meta"]} if "_provider_meta" in raw_asst else {})})
            with phase_tool_context(eff_phase):
                doc_res, pause_id = _execute_tool_calls(
                    tool_calls, messages, loop_guard, mem, session_tag, verbose,
                    allowed_tool_names=allowed_tool_names, dispatch_fn=dispatch_fn, on_tool_result=on_tool_result, log_to_conversations=log_to_conversations, tool_steps_out=turn_tools,
                )
            if doc_res is not None:
                last_doctor_pass = doc_res
            if pause_id:
                return "waiting_approval", "", pause_id
            if should_compact(messages, tier=step_tier or top_tier, last_usage_tokens=last_tokens, tools=step_tools):
                compact_messages_if_needed(messages, store=mem, last_usage_tokens=last_tokens, tier=step_tier or top_tier, tools=step_tools, active_turn_start_idx=turn_start_idx)
        return "done", _synthesize_turn_budget_exit(messages, chat_fn, mem, session_tag, tid, last_doctor_pass, log_to_conversations=log_to_conversations), ""


def run_worker_step_loop(
    system_prompt: str, user_brief: str, max_iters: int, tier: str,
    allowed_tool_names: Set[str], chat_fn: Callable[..., Dict[str, Any]], mem: MemoryStore,
    dispatch_fn: Callable[[str, Dict[str, Any]], str] = dispatch,
    cancel_check: Optional[Callable[[], bool]] = None, deadline_mono: Optional[float] = None,
    on_tool_result: Optional[Callable[[str, str], Optional[str]]] = None,
) -> Tuple[str, str, str]:
    """Execute a Zero-Identity background worker on the canonical `_run_step_loop`."""
    msgs: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_brief}]
    return _run_step_loop(
        msgs, max(1, int(max_iters)), user_brief, "GUIDED_CHANGE", tier, chat_fn, mem,
        session_tag="background_worker", tid=None, verbose=False, allow_error_escalation=True,
        allowed_tool_names=allowed_tool_names, dispatch_fn=dispatch_fn,
        cancel_check=cancel_check, deadline_mono=deadline_mono,
        on_tool_result=on_tool_result, log_to_conversations=False,
    )


def _finish_orchestrated_turn(
    summary: str, ok_flag: bool, messages: List[Dict[str, Any]], mem: MemoryStore,
    session_tag: Optional[str], tid: Optional[str], log_conv: bool,
) -> str:
    messages.append({"role": "assistant", "content": summary})
    if log_conv:
        mem.log_message("assistant", summary, session_tag=session_tag)
    if tid:
        telemetry.end_trace(tid, status="ok" if ok_flag else "error")
    return summary


def run_agent_turn(
    messages: List[Dict[str, Any]], max_iters: Optional[int] = None, chat_fn: Callable[..., Dict[str, Any]] = chat,
    store: Optional[MemoryStore] = None, session_tag: Optional[str] = None, verbose: bool = True, auto_promote: Optional[bool] = None,
) -> str:
    """Execute one complete user turn with frozen KV-cache prefix, `<turn_context>`, and Cooperative `CancelScope`."""
    mem, sub_depth = store or get_memory_store(), _SUBAGENT_DEPTH.get()
    automated_tag = (session_tag or "").startswith(("autonomy:", "subagent:"))
    is_top, log_conv = (sub_depth == 0 and not automated_tag), (not automated_tag)
    iter_limit = max_iters or (15 if is_top else settings.max_iters)
    nudge_banner = consume_session_nudges(session_id=session_tag or "main", store=mem) if is_top else ""

    static_system = build_system_prompt(store=mem)
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": static_system})
    elif messages[0].get("content") != static_system and is_top:
        messages[0]["content"] = static_system

    is_user_msg = len(messages) >= 2 and messages[-1].get("role") == "user"
    user_text = str(messages[-1].get("content", "")) if is_user_msg else "turn"
    if is_user_msg and log_conv:
        mem.log_message("user", user_text, session_tag=session_tag)

    awaiting_bp = is_top and is_awaiting_blueprint_approval(mem)
    bp_action = classify_blueprint_reply(user_text) if awaiting_bp else None
    intent = triage_turn(user_text, store=mem, root=settings.active_project_root) if is_top else triage_turn("")
    can_orchestrate = is_top and not awaiting_bp and (auto_promote is True or (auto_promote is None and max_iters is None and mem.get_active_workflow() is None))
    has_ext_ws = (not settings.get_workspace().is_self_project) or bool(get_project_graph(store=mem).get_project().get("workspace_path"))
    code_build_allowed = has_ext_ws or intent.risk_tier == "R3"
    should_blueprint = is_top and not awaiting_bp and auto_promote is True and intent.needs_blueprint and code_build_allowed
    should_promote = is_top and not awaiting_bp and auto_promote is True and not should_blueprint and intent.mode == "AUTONOMOUS_BUILD" and code_build_allowed

    prev_trace_tok = telemetry._active_trace_var.set(telemetry.active_trace_id)
    tid = telemetry.start_trace(trace_type=session_tag or "user_turn", name=user_text[:100]) if mem.db_path.resolve() == settings.db_path.resolve() else None
    try:
        with (execution_origin_context("foreground_turn") if is_top else contextlib.nullcontext()):
            if awaiting_bp and is_user_msg and not user_text.strip().startswith(("/", ".")):
                res = (
                    approve_blueprint_and_build(chat_fn=chat_fn, store=mem, verbose=verbose) if bp_action == "APPROVE"
                    else {"passed": True, "summary": dispatch("workflow_manage", {"action": "abort", "summary": "Blueprint cancelled by user"})} if bp_action == "ABORT"
                    else handle_blueprint_conversation_turn(user_text, chat_fn=chat_fn, store=mem, verbose=verbose)
                )
                return _finish_orchestrated_turn(str(res["summary"]), bool(res.get("passed")), messages, mem, session_tag, tid, log_conv)
            if can_orchestrate and intent.route in ("EXECUTE_VERIFIED_DAG", "ADVANCE_ACTIVE_GOAL"):
                res_o = (
                    workflow_engine.run_dag(intent.matched_dag, params={"goal": user_text}) if intent.route == "EXECUTE_VERIFIED_DAG"
                    else run_autonomous_goal_slice(project_id=intent.target_project_id or None, chat_fn=chat_fn, store=mem, verbose=verbose)
                )
                return _finish_orchestrated_turn(str(res_o.get("summary") or ""), bool(res_o.get("ok") or res_o.get("status") == "waiting_approval"), messages, mem, session_tag, tid, log_conv)
            if should_blueprint or should_promote:
                res = create_or_revise_blueprint(user_text, chat_fn=chat_fn, store=mem, verbose=verbose) if should_blueprint else run_autonomous_goal(user_text, chat_fn=chat_fn, store=mem, verbose=verbose)
                return _finish_orchestrated_turn(str(res["summary"]), bool(res.get("passed")), messages, mem, session_tag, tid, log_conv)

            phase_tier = session_tag.split(":")[2] if automated_tag and (session_tag or "").count(":") >= 2 else None
            top_tier = phase_tier or (mem.get_user_profile().get("active_model_tier", "auto") if sub_depth == 0 else current_tier_name())
            can_escalate = (not top_tier or top_tier.strip().lower() == "auto") or automated_tag or sub_depth > 0
            if should_compact(messages, tier=top_tier):
                compact_messages_if_needed(messages, store=mem, tier=top_tier, active_turn_start_idx=max(1, len(messages) - 1))
            with get_project_graph(store=mem).project_workspace_scope(), preview_turn_cleanup_scope(), tier_context(top_tier), memory_store_context(mem), session_id_context(session_tag or "main"):
                turn_ctx = build_turn_context(store=mem, user_query=user_text if is_top else "", nudge_banner=nudge_banner)
                _st, reply, _meta = _run_step_loop(
                    messages, iter_limit, user_text, intent.mode, top_tier, chat_fn, mem, session_tag, tid, verbose,
                    suggested_tier=intent.suggested_tier, allow_error_escalation=can_escalate, turn_ctx=turn_ctx, log_to_conversations=log_conv,
                )
            if is_top and is_user_msg:
                consolidate_turn_memory(user_text, reply, store=mem)
            return reply
    except BaseException:
        if tid:
            telemetry.end_trace(tid, status="error")
        raise
    finally:
        telemetry._active_trace_var.reset(prev_trace_tok)


_SUBAGENT_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("companion_subagent_depth", default=0)


def run_subagent(
    goal: str, skill_name: Optional[str] = None, max_iters: int = 8, chat_fn: Callable[..., Dict[str, Any]] = chat,
    store: Optional[MemoryStore] = None, verbose: bool = True, tier: Optional[str] = None,
) -> str:
    """Spawn an isolated worker loop on the requested model tier (`fast` or `reasoning`) in a clean wire context."""
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
        "Instructions: Execute the goal autonomously in this isolated context window. Keep file/tool exploration surgical (1-2 targeted reads), "
        f"do not repeat completed tool calls, and return a concise (<200 words) self-contained summary.{skill_block}"
    )
    tag, parent_tid, t0 = f"subagent:{clean_skill or 'worker'}:{tier_cfg.tier}", telemetry.active_trace_id, time.perf_counter()
    depth_tok = _SUBAGENT_DEPTH.set(sub_depth + 1)
    trace_tok = telemetry._active_trace_var.set(parent_tid)
    try:
        with clean_subagent_wire_context(), execution_origin_context("subagent"), tier_context(tier_cfg.tier):
            summary = run_agent_turn([{"role": "user", "content": worker_prompt}], max_iters=eff_iters, chat_fn=chat_fn, store=mem, session_tag=tag, verbose=verbose, auto_promote=False)
        if parent_tid:
            telemetry.record_span("subagent_call", tag, (time.perf_counter() - t0) * 1000.0, status="ok", detail=f"tier={tier_cfg.tier} {summary[:120].replace(chr(10), ' ')}", trace_id=parent_tid)
        return summary
    except Exception as exc:
        if parent_tid:
            telemetry.record_span("subagent_call", tag, (time.perf_counter() - t0) * 1000.0, status="error", detail=str(exc)[:140], trace_id=parent_tid)
        raise
    finally:
        telemetry._active_trace_var.reset(trace_tok)
        _SUBAGENT_DEPTH.reset(depth_tok)


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
    spr_line = f"`{spr['title']}` ({spr['start_date']}..{spr['end_date']})" if spr else "(None active)"
    edu_line = f"`{ac['title']}` ({ac['mastery_pct']}% mastery, {edu['due_total']} due)" if ac else f"(None active, {edu['due_total']} due)"
    return (
        f"=== Proactive Check-In ===\nActive Project  : `{ws.project_id}` ({ws.project_root})\n"
        f"Autonomy Posture: Background Autonomy={bg_lbl} | Installed App=IMMUTABLE\n"
        f"Workflow State  : `{wf['current_state'] if wf else 'IDLE'}`\n"
        f"Active Timebox  : {spr_line}\n"
        f"Study Course    : {edu_line}\n"
        f"Pending Tasks   : {len(mem.get_pending_tasks()[:6])} active | Pending Backlog: {len(mem.get_backlog(status='pending')[:6])}\n"
        f"Recent Notes    : {len(mem.get_system_notes(limit=5))} notes saved\n"
        "All systems operational. Ready for your next request."
    )


SLASH_HELP_TEXT = (
    "=== Companion AI OS Commands ===\n"
    "  /status | /checkin      — Live system status, WorkspaceContext, workflow HUD, tasks & proactive check-in\n"
    "  /project [init|switch..]— Manage isolated workspaces (`/project list`, `init <name>`, `switch <name>`, `self`)\n"
    "  /model & /providers     — View, ping (`check`), switch model tiers, or inspect multi-provider & Vault matrix\n"
    "  /identity | /tone | /onboard — Soul & User Identity cards, tone (`concise|balanced|elaborate`), First-Boot setup\n"
    "  /telemetry | /traces | /logs — Live KPI scorecard, Waterfall execution traces & structured app logs\n"
    "  /doctor | /specs | /hygiene  — 7-stage System Doctor, 4-File Canonical SDD sync & AST Dead-Code scan\n"
    "  /autonomy | /morning   — Background Autonomy controls & Morning Briefing\n"
    "  /skills [name]          — List Progressive-Disclosure `SKILL.md` runbooks or read a skill\n"
    "  /plan <obj> | /approve  — Draft/revise read-only Blueprint (`DISCOVER -> DESIGN -> PLAN`) & approve build\n"
    "  /goal <obj|resume>      — Execute or resume a multi-phase autonomous SDD build with doctor gates\n"
    "  /eval | /subagent       — Run Live Local-Model Flight Eval or spawn an isolated worker\n"
    "  /workflow | /backlog    — Show State-Machine Workflow graph, run a verified DAG, or list tasks/backlog\n"
    "  /memory | /compact      — Sync/display MEMORY.md or view/trigger 2-stage context compaction\n"
    "  /jobs | /wiki | /vault  — Background Job Departures Board, Knowledge Wiki Warehouse & Blind Credential Vault\n"
    "  /endpoints | /mcp | /partners — SSH/WinRM/HTTP Registry, Outbound MCP Client & Federated Partners\n"
    "  /routines | /approvals | /trust | /backup — Routines, ApprovalQueue, Trust Mode & Secret-Free Backups\n"
    "  /board | /sprint | /calendar | /study — Multi-Board Kanban, Weekly Sprints, Spatial Calendar & Socratic Tutor\n"
    "  /checkpoint | /rollback | /reset | exit — Checkpoints, Git save-points, rollback, clear window, or exit"
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
        return f"=== Companion AI OS v3.5 Status ===\nProject    : `{ws.project_id}` ({ws.project_root})\nRouting    : mode=`{mode}` | fast=`{f_cfg.model}` (temp={f_cfg.temperature}){code_seg} | reasoning=`{r_cfg.model}` (temp={r_cfg.temperature})\nPosture    : Background Autonomy={'ON' if mem.is_background_autonomy_enabled() else 'OFF'} | Installed App=IMMUTABLE (Idle: {settings.idle_minutes}m | Cooldown: {settings.hobby_cooldown_minutes}m)\nTimebox/Edu: Timebox={spr_s} | Study={edu_s}\n\n{hud}\n\nPending Tasks: {len(tasks)} | Pending Backlog: {len(backlog)}"
    if verb in ("doctor", "doc"):
        return str(run_system_doctor(run_tests=True)["summary"])
    if verb in ("plan", "blueprint"):
        return str(create_or_revise_blueprint(arg or "status", chat_fn=chat_fn, store=mem)["summary"])
    if verb == "approve":
        return str(approve_blueprint_and_build(chat_fn=chat_fn, store=mem)["summary"])
    if verb == "goal":
        return "Usage: `/goal <feature or engineering objective>` (or `/goal resume`)" if not arg else str(run_autonomous_goal(arg, chat_fn=chat_fn, store=mem)["summary"])
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
        if verb in ("trust", "partners", "mcp", "board", "boards", "sprint", "study", "providers") and messages and messages[0].get("role") == "system":
            messages[0]["content"] = build_system_prompt(store=mem)
        return out
    return f"Unknown slash command '/{verb}'. Type `/help` to see all available commands." if (cleaned.startswith("/") and "/" not in verb and "\\" not in verb) else None
