"""
Unified Single-Agent Frontier Harness (`companion/agent.py`).

Consolidates:
1. Canonical Single Primary ReAct Loop (`_run_step_loop`, `run_agent_turn`, `run_worker_step_loop`, `run_subagent`)
   with frozen `messages[0]` KV-cache prefix, `<turn_context>` wire scope, strict JSON tool-arg validation,
   `LoopGuard` stagnation recovery, and mechanical foreground/background `ApprovalQueue` pause.
2. Fast deterministic blast-radius & tier routing (`triage_turn`, `classify_turn_intent`, `select_turn_step_tier`).
3. Interactive `/plan` -> `/approve` Blueprint steering (`create_or_revise_blueprint`, `approve_blueprint_and_build`,
   `handle_blueprint_conversation_turn`, `is_awaiting_blueprint_approval`) and `/goal` autonomous verification
   (`run_autonomous_goal`, `run_autonomous_goal_slice`, `WorkflowEngine`).
"""

import concurrent.futures
import contextlib
import contextvars
import hashlib
import importlib
import inspect
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from companion.approvals import ActionRiskTier, consume_session_nudges, session_id_context
from companion.config import (
    GENERAL_TRACKED_EXTENSIONS,
    agent_tool_turn_context,
    execution_origin_context,
    settings,
    walk_pruned_files,
)
from companion.context import (
    EpistemicGrounding,
    build_epistemic_grounding,
    build_lossless_phase_handoff,
    build_system_prompt,
    build_turn_context,
    classify_task_autoskip,
    compact_messages_if_needed,
    consolidate_turn_memory,
    estimate_messages_chars,
    estimate_messages_tokens,
    format_context_budget_status,
    format_identity_summary,
    format_skills_catalog,
    format_task_carry_forward,
     get_knowledge_port,
    manage_active_project,
    manage_companion_tone,
    manage_model_tier,
    run_first_boot_onboarding,
    should_compact,
)
from companion.workflows import DAGRuntimeBindings, discover_dags, execute_dag_by_name, register_dag_runtime_bindings, resume_dag_run
from companion.education_engine import get_education_engine
from companion.llm import chat, current_tier_name, model_gate, normalize_assistant_message, recover_empty_synthesis, tier_context
from companion.integrations import get_mcp_hub
from companion.memory_store import MemoryStore, get_memory_store, has_memory_store_override, memory_store_context
from companion.projects import get_project_graph, sync_workflow_to_project_graph
from companion.worktrees import preview_turn_cleanup_scope
from companion.workflows import distill_workflow_reflection
from companion.streaming import (
    check_cancelled,
    clean_subagent_wire_context,
    emit_stream_event,
    get_active_wire_doc_context,
    get_active_wire_images,
    turn_context_scope,
    wire_attachment_context,
)
from companion.telemetry import telemetry
from companion.tools import (
    CheckpointManager,
    LoopGuard,
    TOOLS,
    current_workflow_phase,
    dispatch,
    filter_tools_for_phase,
    get_active_checkpoint_manager,
    phase_tool_context,
    r3_approval_context,
    run_system_doctor,
    run_unit_tests,
    summarize_tool_step,
    validate_python_syntax,
)
from companion.wiki_curation import pending_curation_nudge
from companion.workflows import ExecutorBindings, GoalPlan, GoalStep, VerificationSpec, build_goal_plan_from_tasks, evaluate_step_verification, is_node_output_passed as _is_node_output_passed

__all__ = [
    "ALLOWED_TRANSITIONS",
    "IntentDecision",
    "PHASE_TIERS",
    "ReadinessAssessment",
    "SLASH_HELP_TEXT",
    "TRIAGE_ROUTES",
    "TransitionResult",
    "TriageDecision",
    "WORKFLOW_TEMPLATES",
    "WorkflowEngine",
    "WorkflowState",
    "_evaluate_hard_verification",
    "_execute_implement_tasks",
    "_is_node_output_passed",
    "approve_blueprint_and_build",
    "assess_turn_readiness",
    "check_in",
    "classify_blueprint_reply",
    "classify_turn_intent",
    "create_or_revise_blueprint",
    "ensure_dag_runtime_bound",
    "format_blueprint_card",
    "format_dags_catalog",
    "handle_blueprint_conversation_turn",
    "handle_slash_command",
    "is_awaiting_blueprint_approval",
    "is_pure_blueprint_question",
    "match_verified_dag",
    "resolve_goal_engines",
    "resolve_workflow_phase_tier",
    "run_agent_turn",
    "run_autonomous_goal",
    "run_autonomous_goal_slice",
    "run_http_smoke_probe",
    "run_phase_turn",
    "run_subagent",
    "run_worker_step_loop",
    "scan_placeholder_violations",
    "select_turn_step_tier",
    "triage_turn",
    "workflow_engine",
]

_SUBAGENT_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("companion_subagent_depth", default=0)


# ---------------------------------------------------------------------------
# 1. Cognitive Triage, Blast-Radius Readiness & Step Tier Selection
# ---------------------------------------------------------------------------
TRIAGE_ROUTES: Tuple[str, ...] = ("DIRECT_TURN", "EXECUTE_VERIFIED_DAG", "GOAL_ALIGN_PLAN", "ADVANCE_ACTIVE_GOAL")
PHASE_TIERS: Dict[str, str] = {
    "INTAKE": "fast", "DISCOVER": "fast", "DESIGN": "reasoning", "PLAN": "reasoning",
    "IMPLEMENT": "fast", "VERIFY": "fast", "RECOVER": "reasoning", "REFLECT": "fast",
}
_ARTIFACT_PATH_RE = re.compile(r"\b[a-zA-Z0-9_./-]+\.(?:py|json|toml|md|ya?ml|html?|js|ts|css|sql|sh|ps1)\b", re.I)
_NUMBERED_SPEC_RE = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*])\s+\S+", re.M)
_GENERIC_TECH_TERMS = frozenset({
    "adr", "api", "ast", "back-end", "cli", "command-line", "cross-project", "css", "csv", "dag",
    "domain-neutral", "end-to-end", "front-end", "full-stack", "html", "http", "https", "in-memory",
    "json", "key-value", "mcp", "multi-phase", "multi-step", "open-ended", "read-only", "real-time",
    "rest", "sdd", "self-test", "single-file", "sql", "sqlite", "state-machine", "toml", "url",
    "utf", "utf-8", "xml", "yaml", "zero-dependency",
})
_COMPANION_ID_PREFIXES = ("wi_", "crd_", "prj_", "ms_", "ep_", "tb_", "sp_", "apr_", "bgj_", "sbx_", "rt_", "dagrun_", "edu_", "evt_", "cur_", "ctx_")
_COMPANION_SDK_SYMBOLS = frozenset({"dagspec", "dagnode", "triggerpolicy", "verificationspec", "goalplan", "goalstep", "memorystore", "skillforge"})
_DESTRUCTIVE_VERBS = frozenset({"cutover", "migrate", "overwrite", "wipe", "destroy", "drop", "purge"})
_PRODUCTION_NOUNS = frozenset({"production", "prod", "live", "remote", "cluster", "database"})
_MUTATION_VERBS = frozenset({
    "build", "create", "implement", "develop", "refactor", "rewrite", "edit", "fix",
    "patch", "update", "generate", "scaffold", "wire", "automate", "assemble", "author",
    "configure", "setup", "deploy", "install", "sync",
})
_ADVANCE_VERBS = frozenset({"advance", "continue", "proceed", "resume", "tackle"})
_GOAL_NOUNS = frozenset({"goal", "slice", "milestone", "project", "work"})
_PERSONAL_ASSISTANT_NOUNS = frozenset({
    "course", "courses", "flashcard", "flashcards", "study", "quiz", "calendar", "appointment",
    "reminder", "task", "tasks", "card", "cards", "board", "milestone", "milestones", "note", "notes", "inbox", "wiki", "event", "events",
})
_PERSONAL_ACTION_VERBS = frozenset({"add", "create", "save", "schedule", "capture", "curate", "log", "move", "update", "delete", "remove"})


@dataclass(frozen=True)
class ReadinessAssessment:
    status: str
    reasons: Tuple[str, ...] = ()
    unknown_terms: Tuple[str, ...] = ()
    risk_tier: ActionRiskTier = ActionRiskTier.R0_READ_ONLY


@dataclass(frozen=True)
class TriageDecision:
    mode: str
    suggested_tier: str
    reason: str
    readiness: str = "READY"
    ambiguity_notes: Tuple[str, ...] = ()
    needs_blueprint: bool = False
    route: str = "DIRECT_TURN"
    matched_dag: str = ""
    target_project_id: str = ""
    risk_tier: str = "R0"


IntentDecision = TriageDecision


def _tokenize_words(text: str) -> List[str]:
    return [w.lower() for w in re.findall(r"[A-Za-z0-9_-]+", text or "")]


def _filter_external_unknown_targets(clean_text: str, raw_unknowns: List[str]) -> Tuple[str, ...]:
    first_word = clean_text.split()[0].strip(".,:;!?()[]{}\"'`") if clean_text.split() else ""
    filtered: List[str] = []
    for tok in raw_unknowns:
        norm = tok.strip(".,:;!?()[]{}\"'`")
        low = norm.lower().strip("./-")
        if not low or low in _GENERIC_TECH_TERMS or low in _COMPANION_SDK_SYMBOLS:
            continue
        if low.startswith(_COMPANION_ID_PREFIXES) or norm.isupper() or norm == norm.lower():
            continue
        if "." in norm and _ARTIFACT_PATH_RE.search(norm):
            continue
        has_internal_cap_or_digit = any(c.isupper() for c in norm[1:]) or any(c.isdigit() for c in norm)
        is_mid_sentence_proper = norm[0].isupper() and norm != first_word and len(norm) >= 4
        if (has_internal_cap_or_digit or is_mid_sentence_proper) and norm not in filtered:
            filtered.append(norm)
    return tuple(filtered)


def match_verified_dag(user_text: str, root: Optional[Path] = None) -> str:
    clean = (user_text or "").strip().lower()
    if not clean or clean.endswith("?"):
        return ""
    tokens = set(_tokenize_words(clean))
    run_cues = {"run", "execute", "trigger", "invoke", "launch", "audit", "verify", "diagnose", "check"}
    if not (tokens & run_cues):
        return ""
    best_pos, best_name = -1, ""
    for d in discover_dags(root=root):
        name = str(d.get("name") or "").lower()
        alt = name.replace("-", " ")
        if name and (f"dag {name}" in clean or f"workflow {name}" in clean or f"`{name}`" in clean or (len(name) >= 6 and (name in tokens or alt in clean) and bool({"dag", "workflow"} & tokens))):
            pos = max(clean.rfind(name), clean.rfind(alt))
            if pos >= best_pos:
                best_pos, best_name = pos, str(d["name"])
    return best_name


def assess_turn_readiness(
    user_text: str, grounding: Optional[EpistemicGrounding] = None, store: Optional[Any] = None, root: Optional[Any] = None,
) -> ReadinessAssessment:
    clean = (user_text or "").strip()
    if not clean or clean.startswith(("/", ".")):
        return ReadinessAssessment("READY")
    eff_grounding = grounding or (build_epistemic_grounding(clean, store=store, root=root) if (store is not None or root is not None) else None)
    words = set(_tokenize_words(clean))
    reasons: List[str] = []
    has_mutation = bool(words & _MUTATION_VERBS)
    risk = ActionRiskTier.R1_LOCAL_REVERSIBLE if has_mutation else ActionRiskTier.R0_READ_ONLY
    if bool((words & _DESTRUCTIVE_VERBS) and (words & _PRODUCTION_NOUNS)) and "```" not in clean:
        risk = ActionRiskTier.R3_HIGH_IMPACT_MUTATION
        reasons.append("high-impact stateful or external operation detected (`R3`)")
    low = clean.lower()
    if any(p in low for p in ("align with me first", "ask me questions first", "help me scope", "not sure how", "not sure which", "before we build")):
        reasons.append("explicit alignment or scoping request")
    unknowns = _filter_external_unknown_targets(clean, list(eff_grounding.unknown_terms)) if eff_grounding else ()
    if len(unknowns) >= 2 and has_mutation and not (words & _PERSONAL_ASSISTANT_NOUNS):
        reasons.append(f"multiple unverified external targets ({', '.join(unknowns[:3])})")
    return ReadinessAssessment("NEEDS_ALIGNMENT" if reasons else "READY", tuple(reasons), unknowns, risk)


def _resolve_active_media_tier() -> Optional[str]:
    uris = get_active_wire_images()
    if not uris:
        return None
    slot = "video" if any(u.startswith("data:video/") for u in uris) else ("audio" if any(u.startswith("data:audio/") for u in uris) else "image")
    return slot if settings.resolve_tier(slot).is_dedicated else "reasoning"


def _triage_via_llm(
    clean: str, readiness: ReadinessAssessment, risk_code: str, eff_root: Path,
    triage_llm_fn: Callable[[str, Dict[str, Any]], Dict[str, Any]],
) -> Optional[TriageDecision]:
    dags = [str(d["name"]) for d in discover_dags(root=eff_root)]
    try:
        raw = triage_llm_fn(clean, {"risk_tier": risk_code, "readiness": readiness.status, "reasons": list(readiness.reasons), "available_dags": dags})
    except Exception:
        return None
    route = str((raw or {}).get("route") or "").strip().upper()
    dag = str((raw or {}).get("matched_dag") or "").strip()
    if route not in TRIAGE_ROUTES or (route == "EXECUTE_VERIFIED_DAG" and dag not in dags):
        return None
    tier = str(raw.get("tier") or "fast").strip().lower()
    if tier not in ("fast", "coding", "reasoning"):
        tier = "fast"
    needs_bp = bool(raw.get("needs_blueprint", False)) or readiness.risk_tier == ActionRiskTier.R3_HIGH_IMPACT_MUTATION
    mode_map = {
        "DIRECT_TURN": "DEEP_CONSULT" if tier == "reasoning" else "CHAT_DIRECT",
        "EXECUTE_VERIFIED_DAG": "GUIDED_CHANGE",
        "GOAL_ALIGN_PLAN": "DEEP_CONSULT" if needs_bp else "AUTONOMOUS_BUILD",
        "ADVANCE_ACTIVE_GOAL": "GUIDED_CHANGE",
    }
    return TriageDecision(
        mode=mode_map[route], suggested_tier="reasoning" if needs_bp else tier,
        reason=str(raw.get("reason") or "structured cognitive triage")[:160],
        readiness=readiness.status, ambiguity_notes=readiness.reasons,
        needs_blueprint=needs_bp and route == "GOAL_ALIGN_PLAN", route=route, matched_dag=dag, risk_tier=risk_code,
    )


def triage_turn(
    user_text: str, grounding: Optional[EpistemicGrounding] = None, store: Optional[Any] = None,
    root: Optional[Any] = None, triage_llm_fn: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
) -> TriageDecision:
    clean = (user_text or "").strip()
    if not clean or clean.startswith(("/", ".", "[AUTONOMOUS WORKFLOW PHASE:", "[ISOLATED SUBAGENT WORKER TASK]")):
        return TriageDecision("CHAT_DIRECT", "fast", "slash or internal system framing", route="DIRECT_TURN")
    m_tier = _resolve_active_media_tier()
    if m_tier:
        return TriageDecision("CHAT_DIRECT", m_tier, f"multimodal {m_tier} attachment turn", route="DIRECT_TURN")

    eff_root = Path(root).resolve() if root else settings.active_project_root
    readiness = assess_turn_readiness(clean, grounding=grounding, store=store, root=eff_root)
    risk_code = readiness.risk_tier.name.split("_", 1)[0]
    dag_hit = match_verified_dag(clean, root=eff_root)
    if dag_hit and readiness.risk_tier != ActionRiskTier.R3_HIGH_IMPACT_MUTATION:
        return TriageDecision("GUIDED_CHANGE", "fast", f"matched verified workflow `{dag_hit}`", readiness.status, readiness.reasons, route="EXECUTE_VERIFIED_DAG", matched_dag=dag_hit, risk_tier=risk_code)

    words_list = _tokenize_words(clean)
    word_set = set(words_list)
    if store is not None and (word_set & _ADVANCE_VERBS) and (word_set & _GOAL_NOUNS) and len(words_list) <= 14:
        ready_items = get_project_graph(store).next_ready_companion_items(limit=1)
        if ready_items:
            pid = str(ready_items[0]["project_id"])
            return TriageDecision("GUIDED_CHANGE", "fast", f"advance ready work item `{ready_items[0]['item_id']}` in `{pid}`", "READY", route="ADVANCE_ACTIVE_GOAL", target_project_id=pid, risk_tier=risk_code)

    if triage_llm_fn is not None:
        llm_dec = _triage_via_llm(clean, readiness, risk_code, eff_root, triage_llm_fn)
        if llm_dec is not None:
            return llm_dec

    has_mutation = bool(word_set & _MUTATION_VERBS)
    consult_cues = {"architecture", "tradeoffs", "tradeoff", "design", "strategy", "compare", "evaluate", "root"}
    is_question = clean.endswith("?") or (bool(words_list) and words_list[0] in {"why", "how", "what", "compare", "explain", "evaluate"})
    artifact_refs = {m.lower() for m in _ARTIFACT_PATH_RE.findall(clean)}
    multi_deliverable = len(artifact_refs) >= 3 or len(_NUMBERED_SPEC_RE.findall(clean)) >= 3 or ("end-to-end" in clean.lower() and bool({"test", "tests"} & word_set) and has_mutation)

    if readiness.status == "NEEDS_ALIGNMENT" and (readiness.risk_tier == ActionRiskTier.R3_HIGH_IMPACT_MUTATION or has_mutation):
        return TriageDecision("GUIDED_CHANGE", "reasoning", "high-impact or underspecified goal routed to Blueprint alignment", readiness.status, readiness.reasons, needs_blueprint=True, route="GOAL_ALIGN_PLAN", risk_tier=risk_code)
    has_personal_write = bool((word_set & _PERSONAL_ACTION_VERBS) and (word_set & _PERSONAL_ASSISTANT_NOUNS)) and not bool(word_set & consult_cues)
    if has_personal_write and readiness.status == "READY" and not multi_deliverable:
        return TriageDecision("GUIDED_CHANGE", "fast", "personal board/calendar/wiki action", readiness.status, readiness.reasons, route="DIRECT_TURN", risk_tier=risk_code)
    if (is_question or (not has_mutation and bool(word_set & consult_cues))) and not multi_deliverable:
        is_deep = len(words_list) >= 14 or bool(word_set & consult_cues)
        return TriageDecision("DEEP_CONSULT" if is_deep else "CHAT_DIRECT", "reasoning" if is_deep else "fast", "analytical consultation" if is_deep else "direct question", readiness.status, readiness.reasons, route="DIRECT_TURN", risk_tier=risk_code)

    tier_hint = "reasoning" if readiness.status == "NEEDS_ALIGNMENT" else "fast"
    if has_mutation and multi_deliverable and len(words_list) >= 8:
        return TriageDecision("AUTONOMOUS_BUILD", tier_hint, "multi-deliverable objective routed to goal execution", readiness.status, readiness.reasons, route="GOAL_ALIGN_PLAN", risk_tier=risk_code)
    if has_mutation and len(words_list) >= 4:
        return TriageDecision("GUIDED_CHANGE", tier_hint, "scoped single-objective task", readiness.status, readiness.reasons, route="DIRECT_TURN", risk_tier=risk_code)
    return TriageDecision("CHAT_DIRECT", tier_hint, "conversational turn", readiness.status, readiness.reasons, route="DIRECT_TURN", risk_tier=risk_code)


def classify_turn_intent(user_text: str, grounding: Optional[EpistemicGrounding] = None, store: Optional[Any] = None, root: Optional[Any] = None) -> TriageDecision:
    return triage_turn(user_text, grounding=grounding, store=store, root=root)


def resolve_workflow_phase_tier(state: Optional[str]) -> str:
    clean = (state or "").strip().upper()
    if clean == "IMPLEMENT" and settings.resolve_tier("coding").is_dedicated:
        return "coding"
    return PHASE_TIERS.get(clean, "fast")


def select_turn_step_tier(
    top_tier: Optional[str], active_wf_state: Optional[str], consecutive_errors: int,
    step: int = 0, intent_mode: str = "CHAT_DIRECT", prior_tier: Optional[str] = None,
    subagent_depth: int = 0, tiers_share_endpoint: Optional[bool] = None,
    suggested_tier: Optional[str] = None, allow_error_escalation: Optional[bool] = None,
) -> Optional[str]:
    _ = tiers_share_endpoint
    m_tier = _resolve_active_media_tier()
    if m_tier:
        return m_tier
    is_auto = not top_tier or top_tier.strip().lower() == "auto"
    can_escalate = is_auto if allow_error_escalation is None else bool(allow_error_escalation)
    if consecutive_errors >= 2 and can_escalate:
        if telemetry.active_trace_id:
            telemetry.log("INFO", "safety", f"Escalating step to `reasoning` tier after {consecutive_errors} consecutive tool errors")
        return "reasoning"
    if step >= 1 and prior_tier:
        return prior_tier
    if subagent_depth == 0 and is_auto:
        if intent_mode == "DEEP_CONSULT" or suggested_tier == "reasoning":
            return "reasoning"
        return resolve_workflow_phase_tier(active_wf_state) if active_wf_state else "fast"
    return top_tier


# ---------------------------------------------------------------------------
# 2. Deterministic Workflow State Machine (`WorkflowEngine`)
# ---------------------------------------------------------------------------
class WorkflowState(str, Enum):
    IDLE = "IDLE"
    INTAKE = "INTAKE"
    DISCOVER = "DISCOVER"
    DESIGN = "DESIGN"
    PLAN = "PLAN"
    IMPLEMENT = "IMPLEMENT"
    VERIFY = "VERIFY"
    RECOVER = "RECOVER"
    REFLECT = "REFLECT"
    COMPLETE = "COMPLETE"
    ABORTED = "ABORTED"


ALLOWED_TRANSITIONS: Dict[WorkflowState, Tuple[WorkflowState, ...]] = {
    WorkflowState.IDLE: (WorkflowState.INTAKE, WorkflowState.DISCOVER, WorkflowState.PLAN),
    WorkflowState.INTAKE: (WorkflowState.DISCOVER, WorkflowState.DESIGN, WorkflowState.PLAN, WorkflowState.RECOVER, WorkflowState.ABORTED),
    WorkflowState.DISCOVER: (WorkflowState.DESIGN, WorkflowState.PLAN, WorkflowState.IMPLEMENT, WorkflowState.INTAKE, WorkflowState.RECOVER, WorkflowState.ABORTED),
    WorkflowState.DESIGN: (WorkflowState.PLAN, WorkflowState.IMPLEMENT, WorkflowState.DISCOVER, WorkflowState.RECOVER, WorkflowState.ABORTED),
    WorkflowState.PLAN: (WorkflowState.IMPLEMENT, WorkflowState.DESIGN, WorkflowState.DISCOVER, WorkflowState.RECOVER, WorkflowState.ABORTED),
    WorkflowState.IMPLEMENT: (WorkflowState.VERIFY, WorkflowState.PLAN, WorkflowState.DISCOVER, WorkflowState.RECOVER, WorkflowState.ABORTED),
    WorkflowState.VERIFY: (WorkflowState.REFLECT, WorkflowState.COMPLETE, WorkflowState.IMPLEMENT, WorkflowState.RECOVER, WorkflowState.PLAN, WorkflowState.ABORTED),
    WorkflowState.RECOVER: (WorkflowState.PLAN, WorkflowState.IMPLEMENT, WorkflowState.DISCOVER, WorkflowState.VERIFY, WorkflowState.ABORTED),
    WorkflowState.REFLECT: (WorkflowState.COMPLETE, WorkflowState.IMPLEMENT, WorkflowState.VERIFY, WorkflowState.RECOVER, WorkflowState.ABORTED),
    WorkflowState.COMPLETE: (WorkflowState.IDLE,),
    WorkflowState.ABORTED: (WorkflowState.IDLE,),
}

_FULL_PATH = [
    WorkflowState.INTAKE, WorkflowState.DISCOVER, WorkflowState.DESIGN, WorkflowState.PLAN,
    WorkflowState.IMPLEMENT, WorkflowState.VERIFY, WorkflowState.REFLECT, WorkflowState.COMPLETE,
]

WORKFLOW_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "self_evolution": {"name": "Self-Evolution & Capability Upgrade", "initial_state": WorkflowState.INTAKE, "recommended_path": _FULL_PATH, "description": "Full lifecycle for evolving architecture, tools, or prompts with checkpoint safety."},
    "engineering_feature": {"name": "Feature & Deliverable Execution", "initial_state": WorkflowState.INTAKE, "recommended_path": _FULL_PATH, "description": "End-to-end discovery, design, planning, implementation, and evidence-verified delivery workflow."},
    "bug_fix": {"name": "Deterministic Bug Diagnosis & Fix", "initial_state": WorkflowState.DISCOVER, "recommended_path": [WorkflowState.DISCOVER, WorkflowState.PLAN, WorkflowState.IMPLEMENT, WorkflowState.VERIFY, WorkflowState.REFLECT, WorkflowState.COMPLETE], "description": "Root-cause reproduction, targeted fix, regression test, and verification."},
    "architecture_refactor": {"name": "Architecture & Modular Refactoring", "initial_state": WorkflowState.DISCOVER, "recommended_path": _FULL_PATH[1:], "description": "Structural refactoring with mandatory checkpointing and test verification."},
}


@dataclass
class TransitionResult:
    success: bool
    workflow_id: Optional[int]
    from_state: str
    to_state: str
    message: str
    gate_details: Optional[str] = None
    checkpoint_id: Optional[str] = None


def format_dags_catalog(root: Optional[Path] = None) -> str:
    dags = discover_dags(root=root)
    if not dags:
        return "No verified workflows found."
    lines = [f"=== Verified Workflows ({len(dags)}) ==="]
    for d in dags:
        lines.append(f"  * `{d['name']}` (pack: `{d['skill']}`) — {d['description']}\n    Nodes: {' -> '.join(d['nodes'])} | Path: `{d['path']}`")
    return "\n".join(lines)


class WorkflowEngine:
    """Persistent, gate-enforced State Machine & Declarative Workflow Engine."""

    def __init__(self, store: Optional[MemoryStore] = None, cp_manager: Optional[CheckpointManager] = None, root: Optional[Path] = None):
        self._store = store
        self._cp_manager = cp_manager
        self._root = root.resolve() if root else None
        self._tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None
        self._subagent_fn: Optional[Callable[..., str]] = None

    @property
    def store(self) -> MemoryStore:
        return self._store or get_memory_store()

    @property
    def cp_manager(self) -> CheckpointManager:
        if self._cp_manager is not None:
            return self._cp_manager
        if self._root is not None:
            self._cp_manager = CheckpointManager(root=self._root, backup_dir=self._root / ".companion_backups", store=self._store)
            return self._cp_manager
        mem = self.store
        if mem.db_path.resolve() != settings.db_path.resolve():
            iso_root = mem.db_path.parent
            cp = CheckpointManager(root=iso_root, backup_dir=iso_root / ".companion_backups", store=mem)
            if self._store is not None:
                self._cp_manager = cp
            return cp
        return get_active_checkpoint_manager()

    @property
    def root(self) -> Path:
        return self._root or self.cp_manager.root

    def bind_executors(self, tool_fn: Callable[[str, Dict[str, Any]], str], subagent_fn: Optional[Callable[..., str]] = None, llm_fn: Optional[Callable[[str, str, str], str]] = None) -> None:
        self._tool_fn, self._subagent_fn = tool_fn, subagent_fn
        ExecutorBindings.bind(tool_fn, subagent_fn)
        register_dag_runtime_bindings(tool_fn=tool_fn, llm_fn=llm_fn)

    def start_workflow(self, goal: str, workflow_type: str = "engineering_feature", acceptance_criteria: str = "", create_initial_checkpoint: bool = True) -> TransitionResult:
        workflow_type = workflow_type if workflow_type in WORKFLOW_TEMPLATES else "engineering_feature"
        initial_state: WorkflowState = WORKFLOW_TEMPLATES[workflow_type]["initial_state"]
        cp_id = self.cp_manager.create_checkpoint(label=f"wf_{workflow_type}")["checkpoint_id"] if create_initial_checkpoint else ""
        wf_id = self.store.create_workflow_run(workflow_type=workflow_type, goal=goal, initial_state=initial_state.value, acceptance_criteria=acceptance_criteria, checkpoint_id=cp_id, metadata={"template": workflow_type, "verified": False})
        self.store.sync_memory_md()
        return TransitionResult(True, wf_id, WorkflowState.IDLE.value, initial_state.value, f"Workflow #{wf_id} ({workflow_type}) started in state `{initial_state.value}`.", checkpoint_id=cp_id or None)

    def _run_verification_gate(self) -> Tuple[bool, str]:
        py_files = [p for p in walk_pruned_files(self.root) if p.suffix.lower() == ".py"]
        bad_syntax = [f"{p.name}: {err}" for p in py_files if not (res := validate_python_syntax(p.read_text(encoding="utf-8", errors="replace"), str(p)))[0] and (err := res[1])]
        if bad_syntax:
            return False, f"Syntax gate failed on {len(bad_syntax)} file(s): {'; '.join(bad_syntax)}"
        test_res = run_unit_tests(self.root)
        if not test_res["passed"]:
            return False, f"Unit test gate failed (exit={test_res.get('returncode')}):\n{test_res.get('output', '')}"
        return True, f"Verification passed ({len(py_files)} Python file(s) checked; tests passed)."

    def transition(self, to_state_str: str, summary: str, acceptance_criteria: Optional[str] = None, run_tests_on_verify: bool = True) -> TransitionResult:
        active = self.store.get_active_workflow()
        if not active:
            return TransitionResult(False, None, WorkflowState.IDLE.value, to_state_str, "error: No active workflow. Call workflow_manage(action='start', goal='...') first.")
        wf_id, raw_from = int(active["id"]), str(active["current_state"])
        if raw_from not in WorkflowState._value2member_map_:
            return TransitionResult(False, wf_id, raw_from, to_state_str, f"Active workflow #{wf_id} (`{active.get('workflow_type')}`) is managed automatically by the DAG runtime.")
        from_state = WorkflowState(raw_from)
        try:
            target_state = WorkflowState(to_state_str.strip().upper())
        except ValueError:
            return TransitionResult(False, wf_id, from_state.value, to_state_str, f"error: Unknown workflow state '{to_state_str}'. Valid states: {[s.value for s in WorkflowState]}")
        allowed = ALLOWED_TRANSITIONS.get(from_state, ())
        if target_state not in allowed:
            return TransitionResult(False, wf_id, from_state.value, target_state.value, f"GATE BLOCKED: Transition `{from_state.value} -> {target_state.value}` is not permitted. Allowed transitions from `{from_state.value}`: {[s.value for s in allowed]}.")

        cp_id = active.get("checkpoint_id") or ""
        gate_details: Optional[str] = None
        meta_updates: Dict[str, Any] = {}
        if target_state == WorkflowState.IMPLEMENT and not cp_id:
            cp_id = self.cp_manager.create_checkpoint(label=f"wf{wf_id}_pre_implement")["checkpoint_id"]
            gate_details = f"Created automatic pre-implementation safety checkpoint `{cp_id}`."
        if target_state in (WorkflowState.VERIFY, WorkflowState.REFLECT, WorkflowState.COMPLETE) and run_tests_on_verify:
            passed, details = self._run_verification_gate()
            gate_details = details
            meta_updates.update({"verified": passed, "last_verification": details})
            if not passed:
                self.store.update_workflow_state(wf_id, from_state.value, WorkflowState.RECOVER.value, f"Auto-routed to RECOVER due to verification gate failure: {details[:300]}", status="active", checkpoint_id=cp_id, verification_passed=False, metadata_updates=meta_updates, acceptance_criteria=acceptance_criteria)
                self.store.sync_memory_md()
                return TransitionResult(False, wf_id, from_state.value, WorkflowState.RECOVER.value, f"VERIFICATION GATE FAILED: Cannot transition to `{target_state.value}` while syntax or unit tests fail. Automatically transitioned workflow #{wf_id} to `RECOVER`.\nDiagnostics:\n{details}", gate_details=details, checkpoint_id=cp_id)

        new_status = "completed" if target_state == WorkflowState.COMPLETE else ("aborted" if target_state == WorkflowState.ABORTED else "active")
        self.store.update_workflow_state(wf_id, from_state.value, target_state.value, summary, status=new_status, checkpoint_id=cp_id, verification_passed=meta_updates.get("verified", True), metadata_updates=meta_updates, acceptance_criteria=acceptance_criteria)
        self.store.sync_memory_md()
        return TransitionResult(True, wf_id, from_state.value, target_state.value, f"Workflow #{wf_id} transitioned `{from_state.value} -> {target_state.value}`. {gate_details or ''}".strip(), gate_details=gate_details, checkpoint_id=cp_id or None)

    def recover(self, reason: str, rollback: bool = False) -> TransitionResult:
        active = self.store.get_active_workflow()
        if not active:
            if rollback:
                rb = self.cp_manager.rollback_checkpoint()
                return TransitionResult(bool(rb.get("restored")), None, WorkflowState.IDLE.value, WorkflowState.IDLE.value, f"Emergency rollback executed: {rb}")
            return TransitionResult(False, None, WorkflowState.IDLE.value, WorkflowState.RECOVER.value, "No active workflow to recover.")
        wf_id, from_state, cp_id = int(active["id"]), active["current_state"], active.get("checkpoint_id") or ""
        rb_msg = ""
        if rollback and cp_id:
            rb = self.cp_manager.rollback_checkpoint(cp_id)
            rb_msg = f" Rolled back {rb.get('restored_count', 0)} files from checkpoint `{cp_id}`."
        self.store.add_system_note(f"Workflow #{wf_id} recovery triggered from {from_state}: {reason}.{rb_msg}", category="recovery")
        self.store.update_workflow_state(wf_id, from_state, WorkflowState.RECOVER.value, f"Recovery: {reason}.{rb_msg}".strip(), status="active", verification_passed=False)
        self.store.sync_memory_md()
        return TransitionResult(True, wf_id, from_state, WorkflowState.RECOVER.value, f"Workflow #{wf_id} entered `RECOVER` state.{rb_msg} Inspect the issue, adjust your plan, and transition to `PLAN` or `IMPLEMENT`.", checkpoint_id=cp_id or None)

    def render_hud(self) -> str:
        active = self.store.get_active_workflow()
        if not active:
            return (
                "State Machine: `IDLE` (No active software engineering workflow).\n"
                "  - Multi-step builds: `/plan <objective>` or `/goal <objective>`.\n"
                "  - Declarative workflows: `ops_manage(domain='workflows', action='run_dag', target='<name>')`.\n"
                "  - Project & Goal Graph items: `agile_manage` (`companion` | `human` | `collaborative`)."
            )
        wf_id, wf_type, raw_state = active["id"], active["workflow_type"], str(active["current_state"])
        if raw_state not in WorkflowState._value2member_map_:
            return f"Active Workflow #{wf_id} (`{wf_type}`): {active['goal']}\n  Current Node: `{raw_state}`"
        curr = WorkflowState(raw_state)
        tmpl = WORKFLOW_TEMPLATES.get(wf_type, WORKFLOW_TEMPLATES["engineering_feature"])
        graph_parts = [f"**[{n.value}]**" if n == curr else n.value for n in tmpl["recommended_path"]]
        if curr == WorkflowState.RECOVER:
            graph_parts.append("**[RECOVER]**")
        allowed = [s.value for s in ALLOWED_TRANSITIONS.get(curr, ())]
        lines = [
            f"Active Workflow #{wf_id} (`{wf_type}`): {active['goal']}",
            f"  Graph Progress: {' -> '.join(graph_parts)}",
            f"  Current State : `{curr.value}` | Allowed Next States: {allowed}",
        ]
        if active.get("acceptance_criteria"):
            lines.append(f"  Acceptance Criteria: {active['acceptance_criteria']}")
        if active.get("checkpoint_id"):
            lines.append(f"  Safety Checkpoint  : `{active['checkpoint_id']}`")
        return "\n".join(lines)

    def discover_dags(self) -> List[Dict[str, Any]]:
        return discover_dags(root=self.root)

    def format_dags_catalog(self) -> str:
        return format_dags_catalog(root=self.root)

    def run_dag(
        self, dag_name: str, params: Optional[Dict[str, Any]] = None,
        tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None,
        llm_fn: Optional[Callable[[str, str, str], str]] = None,
    ) -> Dict[str, Any]:
        tool_cb, _ = ExecutorBindings.resolve(tool_fn or self._tool_fn, self._subagent_fn)
        bindings = DAGRuntimeBindings(tool_fn=tool_cb, llm_fn=llm_fn) if (tool_cb or llm_fn) else None
        return execute_dag_by_name(dag_name, params=params, store=self.store, bindings=bindings, root=self.root)

    def resume_dag(self, run_id: str) -> Dict[str, Any]:
        return resume_dag_run(run_id, store=self.store)


workflow_engine = WorkflowEngine()


# ---------------------------------------------------------------------------
# 3. Single Primary ReAct Loop (`_run_step_loop`, `run_agent_turn`, `run_subagent`)
# ---------------------------------------------------------------------------
def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


def _parse_strict_tool_args(raw_args: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
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
) -> Tuple[Optional[bool], Optional[str], bool]:
    """Dispatch tool calls with strict JSON validation, LoopGuard, and mechanical approval-queue halting."""
    doctor_ok: Optional[bool] = None
    for idx, tc in enumerate(tool_calls):
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
        elif allowed_tool_names is not None and (name == "job_manage" or (name == "ops_manage" and str(args.get("domain") or "").lower() == "jobs") or name not in allowed_tool_names):
            result = "error: recursive `job_manage` is disabled inside background workers" if name in ("job_manage", "ops_manage") else f"error: tool `{name}` is not permitted for this worker"
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
                if name == "system_doctor" or (name == "ops_manage" and str(args.get("domain") or "").lower() == "doctor"):
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

        is_approval_gate = "AWAITING_USER_APPROVAL" in result or (result.startswith("RISK GATE REJECTED (R3)") and "`apr_" in result)
        if is_approval_gate:
            result = (
                f"{result}\n\n[ACTION QUEUED FOR USER APPROVAL — Tool execution is mechanically paused for this turn; "
                "explain the queued approval request clearly to the user.]"
            )
        tc_id = tc.get("id")
        messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})
        if log_to_conversations:
            mem.log_message("tool", f"{name}({json.dumps(args or {})}) -> {result[:settings.out_limit]}", tool_call_id=tc_id, session_tag=session_tag)
        if on_tool_result is not None:
            pause_id = on_tool_result(name, result)
            if pause_id:
                for rem_tc in tool_calls[idx + 1 :]:
                    messages.append({"role": "tool", "tool_call_id": rem_tc.get("id"), "content": "Skipped: worker paused awaiting user approval."})
                return doctor_ok, pause_id, False
        elif is_approval_gate:
            m_apr = re.search(r"apr_[a-zA-Z0-9_]+", result)
            for rem_tc in tool_calls[idx + 1 :]:
                messages.append({"role": "tool", "tool_call_id": rem_tc.get("id"), "content": "Skipped: turn halted awaiting user approval on prior action."})
            return doctor_ok, None, (m_apr.group(0) if m_apr else "pending")
    return doctor_ok, None, ""


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


def _resolve_effective_phase(mem: MemoryStore) -> str:
    explicit = current_workflow_phase()
    if explicit:
        return explicit
    if _SUBAGENT_DEPTH.get() == 0:
        active_wf = mem.get_active_workflow()
        if active_wf and active_wf["current_state"] in ("DISCOVER", "DESIGN", "PLAN", "VERIFY", "REFLECT"):
            return str(active_wf["current_state"])
    return ""


def _synthesize_turn_budget_exit(
    messages: List[Dict[str, Any]], chat_fn: Callable[..., Dict[str, Any]],
    mem: MemoryStore, session_tag: Optional[str], tid: Optional[str], doctor_pass: Optional[bool],
    log_to_conversations: bool = True, custom_notice: str = "",
) -> str:
    active_wf, pending_cnt = mem.get_active_workflow(), len(mem.get_pending_tasks())
    wf_state = active_wf["current_state"] if active_wf else "IDLE"
    doc_badge = "PASS" if doctor_pass is True else ("FAIL" if doctor_pass is False else "NOT_RUN")
    synth_text = custom_notice or (
        "Tool iteration budget reached. Do NOT call any more tools.\n"
        f"GROUND TRUTH: workflow_state=`{wf_state}`, pending_tasks={pending_cnt}, system_doctor_in_this_turn=`{doc_badge}`.\n"
        "STRICT HONESTY RULE: Do NOT claim tests or system_doctor passed unless `system_doctor_in_this_turn` is `PASS`. "
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
    fallback = f"I reached the tool step limit for this turn (workflow=`{wf_state}`, pending_tasks={pending_cnt}, system_doctor=`{doc_badge}`). Progress is saved to SQLite memory."
    messages.append({"role": "assistant", "content": fallback})
    if log_to_conversations:
        mem.log_message("assistant", fallback, session_tag=session_tag)
    if tid:
        telemetry.end_trace(tid, status="ok")
    return fallback


def _extract_turn_widget_fence(messages: List[Dict[str, Any]], start_idx: int) -> str:
    latest = ""
    for msg in messages[max(0, start_idx) :]:
        if msg.get("role") != "tool":
            continue
        raw = str(msg.get("content") or "")
        marker = "```companion-flashcards"
        if marker in raw:
            sub = raw[raw.rfind(marker) :]
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
    """Canonical Single-Agent ReAct loop keeping `messages[0]` frozen across steps."""
    loop_guard, last_doctor_pass, rolling_scratchpad, prior_step_tier = LoopGuard(), None, "", None
    turn_t0, turn_thoughts, turn_tools = time.perf_counter(), [], []
    turn_iso, cur_nudges, turn_start_idx = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), 0, max(1, len(messages) - 1)
    force_final_text = False
    paused_approval_id = ""
    with turn_context_scope(turn_ctx):
        for step in range(iter_limit):
            check_cancelled()
            if cancel_check is not None and cancel_check():
                return "cancelled", "", "Cancelled by user."
            if deadline_mono is not None and time.monotonic() > deadline_mono:
                return "timed_out", "", "Timed out."
            remaining = iter_limit - step
            if _SUBAGENT_DEPTH.get() > 0 and remaining == 1 and step >= 2 and not force_final_text:
                break
            if step >= 1 and messages and messages[-1].get("role") == "tool" and not force_final_text:
                notes = ([f"[PRIOR STEP WORKING SCRATCHPAD] (Prior step context — the tool call above ALREADY executed; proceed to the next step without repeating it):\n{rolling_scratchpad}"] if rolling_scratchpad else []) + (
                    [f"[ITERATION BUDGET NOTICE: Only {remaining} tool iteration(s) remain in this turn. Complete any required final step and provide your clear final response.]"] if remaining in (1, 2) else []
                )
                if notes:
                    messages[-1]["content"] = str(messages[-1].get("content", "")) + "\n\n" + "\n\n".join(notes)

            eff_phase = _resolve_effective_phase(mem)
            sub_depth = _SUBAGENT_DEPTH.get()
            active_wf = mem.get_active_workflow() if sub_depth == 0 else None
            step_tier = select_turn_step_tier(
                top_tier, str(active_wf["current_state"]) if active_wf else None, loop_guard.consecutive_errors,
                step=step, intent_mode=intent_mode, prior_tier=prior_step_tier, subagent_depth=sub_depth,
                suggested_tier=suggested_tier, allow_error_escalation=allow_error_escalation,
            )
            prior_step_tier = step_tier
            if not current_workflow_phase():
                step_model_lbl = settings.resolve_tier(step_tier).model
                phase_lbl = eff_phase or intent_mode
                step_tag = f"{phase_lbl} · Step {step + 1} ({step_model_lbl})" if step > 0 else f"{phase_lbl} ({step_model_lbl})"
                emit_stream_event({"type": "phase", "phase": step_tag, "tier": step_tier or "fast"})
            tag_s = session_tag or ""
            if force_final_text or tag_s.startswith("autonomy:design:"):
                step_tools = None
            elif tag_s.startswith("autonomy:plan:"):
                plan_schemas = [t for t in filter_tools_for_phase(eff_phase) if t["function"]["name"] in ("read_file", "list_dir")]
                plan_schemas.append({
                    "type": "function",
                    "function": {
                        "name": "task_manage",
                        "description": "Add atomic build tasks (`add_task`) for the execution plan.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["add_task", "list_tasks"]},
                                "description": {"type": "string", "description": "Atomic task description."},
                                "priority": {"type": "integer", "description": "Priority 1..5."},
                                "acceptance_criteria": {"type": "string", "description": "Testable verification criteria."},
                            },
                            "required": ["action"],
                        },
                    },
                })
                step_tools = plan_schemas
            elif allowed_tool_names is not None:
                alias_parents = {
                    *(["ops_manage"] if allowed_tool_names & {"endpoint_manage", "vault_manage", "routine_manage", "workflow_manage", "checkpoint_manage", "project_manage", "telemetry_manage", "system_doctor"} else []),
                    *(["call_mcp"] if "mcp_manage" in allowed_tool_names else []),
                    *(["run_command"] if allowed_tool_names & {"run_shell", "run_python"} else []),
                    *(["delegate_subagent"] if "subagent" in allowed_tool_names else []),
                }
                eff_schema_names = allowed_tool_names | alias_parents
                step_tools = [t for t in TOOLS if t["function"]["name"] in eff_schema_names]
            else:
                step_tools = filter_tools_for_phase(eff_phase)

            with tier_context(step_tier), phase_tool_context(eff_phase):
                data = chat_fn(messages, step_tools if step_tools else None, "none" if not step_tools else "auto")
            last_tokens = int((data.get("usage") or {}).get("total_tokens") or 0) if isinstance(data.get("usage"), dict) else 0
            raw_asst = data["choices"][0]["message"]
            clean_text, thinking_text, tool_calls = normalize_assistant_message(raw_asst)
            dropped_forced_tools = bool(force_final_text and tool_calls)
            if force_final_text:
                tool_calls = []
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
                if force_final_text and (dropped_forced_tools or "approv" not in final_text.lower()):
                    apr_lbl = f" (`{paused_approval_id}`)" if paused_approval_id and paused_approval_id != "pending" else ""
                    apr_note = f"*(Action queued for approval{apr_lbl} — click **Approve** above and I will continue automatically.)*"
                    final_text = f"{final_text.rstrip()}\n\n{apr_note}".strip() if not dropped_forced_tools else apr_note
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
                doc_res, pause_id, fg_approval_halt = _execute_tool_calls(
                    tool_calls, messages, loop_guard, mem, session_tag, verbose,
                    allowed_tool_names=allowed_tool_names, dispatch_fn=dispatch_fn, on_tool_result=on_tool_result,
                    log_to_conversations=log_to_conversations, tool_steps_out=turn_tools,
                )
            if doc_res is not None:
                last_doctor_pass = doc_res
            if pause_id:
                return "waiting_approval", "", pause_id
            if fg_approval_halt:
                paused_approval_id = str(fg_approval_halt)
                if step + 1 >= iter_limit:
                    return "done", _synthesize_turn_budget_exit(
                        messages, chat_fn, mem, session_tag, tid, last_doctor_pass, log_to_conversations=log_to_conversations,
                        custom_notice="An action was queued in ApprovalQueue for user confirmation. Summarize the pending approval request clearly for the user without calling tools.",
                    ), ""
                force_final_text = True
                continue
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
    vis_summary = (summary or "").strip() or ("Completed workflow execution." if ok_flag else "⚠️ Workflow execution stopped without a summary.")
    messages.append({"role": "assistant", "content": vis_summary})
    if log_conv:
        mem.log_message("assistant", vis_summary, session_tag=session_tag)
    if tid:
        telemetry.end_trace(tid, status="ok" if ok_flag else "error")
    return vis_summary


def run_agent_turn(
    messages: List[Dict[str, Any]], max_iters: Optional[int] = None, chat_fn: Callable[..., Dict[str, Any]] = chat,
    store: Optional[MemoryStore] = None, session_tag: Optional[str] = None, verbose: bool = True, auto_promote: Optional[bool] = None,
) -> str:
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


def run_subagent(
    goal: str, skill_name: Optional[str] = None, max_iters: int = 8, chat_fn: Callable[..., Dict[str, Any]] = chat,
    store: Optional[MemoryStore] = None, verbose: bool = True, tier: Optional[str] = None,
    inherit_attachments: bool = False,
) -> str:
    """Spawn an isolated worker loop on the requested model tier (`fast`, `coding`, `reasoning`, `image`)."""
    clean_goal = (goal or "").strip()
    if not clean_goal:
        return "error: goal is required for run_subagent"
    sub_depth = _SUBAGENT_DEPTH.get()
    if sub_depth >= 2:
        return "error: maximum subagent nesting depth (2) reached; execute steps directly."

    mem = store or get_memory_store()
    parent_imgs = list(get_active_wire_images()) if inherit_attachments else []
    parent_doc = get_active_wire_doc_context() if inherit_attachments else ""
    eff_tier_name = (tier or ("image" if parent_imgs and settings.resolve_tier("image").is_dedicated else current_tier_name()) or "fast").strip().lower()
    tier_cfg = settings.resolve_tier(eff_tier_name)
    clean_skill = (skill_name or "").strip().lower()
    skill_candidates = [
        settings.domains_dir / clean_skill / "CONTEXT.md",
        settings.user_skills_dir / clean_skill / "SKILL.md",
        settings.skills_dir / clean_skill / "SKILL.md",
    ] if clean_skill else []
    skill_path = next((p for p in skill_candidates if p.is_file()), None)
    rel_skill = (
        str(skill_path.resolve().relative_to(settings.root.resolve())).replace("\\", "/")
        if skill_path and skill_path.resolve().is_relative_to(settings.root.resolve())
        else (skill_path.name if skill_path else "")
    )
    skill_block = f"\n\n[PRE-LOADED TIER-2 SKILL RUNBOOK (`{rel_skill}`)]\n{skill_path.read_text(encoding='utf-8', errors='replace').strip()}" if skill_path else ""
    eff_iters = max(1, min(int(max_iters or 8), settings.max_iters))
    worker_prompt = (
        f"[ISOLATED SUBAGENT WORKER TASK]\nGoal: {clean_goal}\n"
        f"Model Tier: `{tier_cfg.tier}` (`{tier_cfg.model}`) | Tool Budget: max {eff_iters} tool steps\nSkill Focus: `{clean_skill or 'general-worker'}`\n"
        "Instructions: Execute the goal autonomously in this isolated context window. Keep file/tool exploration surgical, "
        f"do not repeat completed tool calls, and return a concise (<200 words) self-contained summary.{skill_block}"
    )
    tag, parent_tid, t0 = f"subagent:{clean_skill or 'worker'}:{tier_cfg.tier}", telemetry.active_trace_id, time.perf_counter()
    depth_tok = _SUBAGENT_DEPTH.set(sub_depth + 1)
    trace_tok = telemetry._active_trace_var.set(parent_tid)
    try:
        wire_cm = (turn_context_scope("") if (parent_imgs or parent_doc) else clean_subagent_wire_context())
        img_cm = (wire_attachment_context(doc_context=parent_doc, image_data_uris=parent_imgs) if (parent_imgs or parent_doc) else contextlib.nullcontext())
        with wire_cm, img_cm, execution_origin_context("subagent"), tier_context(tier_cfg.tier):
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


# ---------------------------------------------------------------------------
# 4. `/plan` Blueprint Steering & `/goal` Autonomous Driver
# ---------------------------------------------------------------------------
_APPROVE_CLAUSE = (
    r"(?:yes|yep|sure|ok|okay|approve|approved|looks\s+(?:good|great|solid)|"
    r"go\s+ahead(?:\s+and\s+build(?:\s+it)?)?|proceed(?:\s+with\s+(?:the\s+)?(?:plan|build))?|"
    r"ship\s+it|build\s+it|green\s*light|let'?s\s+(?:do|build)\s+it|lgtm)"
)
_APPROVE_REPLY_RE = re.compile(rf"^\s*{_APPROVE_CLAUSE}(?:[,;!\s]+{_APPROVE_CLAUSE})*\s*[!.]*\s*$", re.I)
_ABORT_REPLY_RE = re.compile(r"^\s*(?:cancel|abort|discard|scrap|stop)\s+(?:the\s+)?(?:plan|blueprint|workflow|build)\s*[!.]*\s*$", re.I)
_DIRECTIVE_REVISION_RE = re.compile(r"\b(?:use|bind|set|change|update|add|remove|drop|switch|instead|make\s+it|revise|include|replace|target|port\s+\d+|rename|split)\b", re.I)
_ANALYTICAL_QUESTION_START_RE = re.compile(r"^\s*(?:why|how|what|where|when|which|who|does|do|did|is|are|will|would\s+this|would\s+that|would\s+it|explain)\b", re.I)
_POLITE_DIRECTIVE_QUESTION_RE = re.compile(r"^\s*(?:(?:can|could|should|would|shall|may|let'?s)\s+(?:you|we)\s+(?:please\s+|instead\s+)?|why\s+not\s+|what\s+if\s+we\s+)(?:use|bind|set|change|update|add|remove|drop|switch|make|revise|include|replace|target|rename|split)\b", re.I)
_EMBEDDED_DIRECTIVE_RE = re.compile(r"(?:;\s*|\.\s+)(?:please\s+|can\s+(?:you|we)\s+|could\s+(?:you|we)\s+|let'?s\s+)?(?:use|change|update|add|remove|switch|make|revise|replace)\b", re.I)
_PLAN_DAG_HINT = (
    ' (Optional: include a fenced ```json {"steps": [{"step_id": "s1", "title": "...", "depends_on": [], '
    '"risk_tier": "R1", "verification": {"kind": "evidence_file", "target": "..."}}]} ``` block when dependencies or evidence gates apply.)'
)


def is_pure_blueprint_question(user_text: str) -> bool:
    clean = (user_text or "").strip()
    if not clean.endswith("?"):
        return False
    if _POLITE_DIRECTIVE_QUESTION_RE.search(clean) or _EMBEDDED_DIRECTIVE_RE.search(clean):
        return False
    if _ANALYTICAL_QUESTION_START_RE.search(clean):
        return True
    return not _DIRECTIVE_REVISION_RE.search(clean)


def is_awaiting_blueprint_approval(store: Optional[MemoryStore] = None) -> bool:
    mem = store or get_memory_store()
    active = mem.get_active_workflow()
    if not active or str(active.get("current_state")) != "PLAN":
        return False
    return bool((active.get("metadata") or {}).get("awaiting_plan_approval"))


def classify_blueprint_reply(user_text: str) -> Optional[str]:
    clean = (user_text or "").strip()
    if not clean or clean.startswith(("/", ".")):
        return None
    if _APPROVE_REPLY_RE.match(clean):
        return "APPROVE"
    if _ABORT_REPLY_RE.match(clean):
        return "ABORT"
    return None


def format_blueprint_card(
    wf_id: Optional[int], goal: str, cp_id: str, design_handoff: str, plan_handoff: str,
    pending_tasks: List[Tuple[int, str, str, int]], revised: bool = False,
    goal_dag: Optional[Dict[str, Any]] = None, pause_reason: str = "",
) -> str:
    heading = "Revised Blueprint Ready for Your Review" if revised else "Blueprint Ready for Your Review"
    dag_steps = GoalPlan.from_dict(goal_dag).steps if isinstance(goal_dag, dict) and goal_dag.get("steps") else []
    if pending_tasks:
        task_lines = [f"  {idx}. **[Task #{tid} | P{prio}]** {desc}" for idx, (tid, desc, _st, prio) in enumerate(pending_tasks, 1)]
        gated = [f"`{s.step_id}` ({s.risk_tier}/{s.verification.kind})" for s in dag_steps if s.risk_tier == "R3" or s.verification.kind == "human_gate"]
        if gated:
            task_lines.append(f"  * Approval Gates in Plan: {', '.join(gated)}")
    elif dag_steps:
        task_lines = [f"  {idx}. **[Step `{s.step_id}` | {s.risk_tier} | `{s.verification.kind}`]** {s.title}" for idx, s in enumerate(dag_steps, 1)]
    else:
        task_lines = ["  1. Implement & verify requested deliverable against acceptance gates."]
    gate_banner = f"**Active Approval Gate**: {pause_reason}\n" if pause_reason else ""
    return (
        f"=== {heading} (Workflow #{wf_id} | Checkpoint `{cp_id or 'ready'}`) ===\n"
        f"**Goal**: {goal}\n{gate_banner}"
        "**Safety Lock**: File-writing tools remain **LOCKED** (`PLAN` phase) until you approve.\n\n"
        f"### 1. Proposed Architecture & Approach\n{(design_handoff or '').strip()[:1400]}\n\n"
        f"### 2. Execution Summary\n{(plan_handoff or '').strip()[:1600]}\n\n"
        "### 3. Step-by-Step Build Checklist\n" + "\n".join(task_lines)
        + "\n\n### Your Steering Options\n"
        "- **Approve & Build**: Reply `approve` or run `/approve` (or click **Approve & Build** in the Web UI)\n"
        "- **Clarify or Tweak**: Reply in plain English with answers/adjustments or run `/plan revise <feedback>`\n"
        "- **Discard Plan**: Run `/workflow abort`"
    )


def _snapshot_file_hashes(root: Path) -> Dict[str, str]:
    hashes: Dict[str, str] = {}
    for item in walk_pruned_files(root):
        if item.suffix.lower() in GENERAL_TRACKED_EXTENSIONS:
            rel = str(item.relative_to(root)).replace("\\", "/")
            try:
                hashes[rel] = hashlib.sha256(item.read_bytes()).hexdigest()
            except Exception:
                continue
    return hashes


def _changed_paths_since(root: Path, baseline: Dict[str, str]) -> List[Path]:
    current = _snapshot_file_hashes(root)
    return [root / rel for rel, sha in current.items() if baseline.get(rel) != sha]


def scan_placeholder_violations(paths: List[Path], root: Optional[Path] = None) -> List[str]:
    target_root = (root or settings.active_project_root).resolve()
    violations: List[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            rel = str(path.resolve().relative_to(target_root)).replace("\\", "/")
        except ValueError:
            rel = path.name
        if path.suffix.lower() == ".py":
            ok_syn, syn_err = validate_python_syntax(path.read_text(encoding="utf-8", errors="replace"), filename=rel)
            if not ok_syn:
                violations.append(f"{rel}: {syn_err}")
    return violations


def resolve_goal_engines(
    store: Optional[MemoryStore] = None, cp_mgr: Optional[CheckpointManager] = None, root: Optional[Path] = None,
) -> Tuple[MemoryStore, Path, CheckpointManager, WorkflowEngine]:
    mem, target_root = store or get_memory_store(), (root or settings.active_project_root).resolve()
    if cp_mgr is not None:
        eff_cp = cp_mgr
    elif target_root == settings.active_project_root.resolve() and mem.db_path.resolve() != settings.db_path.resolve() and settings.get_workspace().is_self_project:
        eff_cp = CheckpointManager(root=target_root, backup_dir=mem.db_path.parent / ".companion_backups", store=mem)
    elif target_root == settings.active_project_root.resolve():
        eff_cp = get_active_checkpoint_manager()
    else:
        eff_cp = CheckpointManager(root=target_root, backup_dir=target_root / ".companion_backups", store=mem)
    wf = WorkflowEngine(store=mem, cp_manager=eff_cp, root=target_root) if (store is not None or cp_mgr is not None or root is not None or not settings.get_workspace().is_self_project) else workflow_engine
    return mem, target_root, eff_cp, wf


def run_phase_turn(
    phase: str, prompt: str, max_iters: int, chat_fn: Callable[..., Dict[str, Any]], mem: MemoryStore, verbose: bool,
) -> str:
    tier = resolve_workflow_phase_tier(phase)
    emit_stream_event({"type": "phase", "phase": phase, "tier": tier})
    phase_msgs: List[Dict[str, Any]] = [{"role": "user", "content": f"[AUTONOMOUS WORKFLOW PHASE: {phase} | TIER: {tier}]\n{prompt}"}]
    with tier_context(tier), phase_tool_context(phase):
        return run_agent_turn(phase_msgs, max_iters=max_iters, chat_fn=chat_fn, store=mem, session_tag=f"autonomy:{phase.lower()}:{tier}", verbose=verbose, auto_promote=False)


def _verify_task_completion(root: Path, changed_paths: List[Path], run_doctor_tests: bool) -> Tuple[bool, str]:
    stubs = scan_placeholder_violations(changed_paths, root=root)
    if stubs:
        return False, f"Syntax violation: {'; '.join(stubs[:3])}"
    struct_ok, struct_msg = evaluate_step_verification(VerificationSpec("assertion"), root, "ok", changed_paths)
    if not struct_ok:
        return False, struct_msg
    if run_doctor_tests:
        for p in changed_paths:
            if p.name.startswith("test_") and p.suffix.lower() == ".py" and p.is_file():
                tres = run_unit_tests(root=root, timeout=30, pattern=p.name)
                if not tres.get("passed"):
                    return False, f"Unit test `{p.name}` failed:\n{str(tres.get('output', ''))[-300:]}"
    return True, f"Verified ({len(changed_paths)} file(s) touched; syntax & contract checks clean)"


def _evaluate_hard_verification(
    root: Path, baseline_hashes: Dict[str, str], run_doctor_tests: bool, goal: str = "", goal_plan: Optional[GoalPlan] = None,
) -> Tuple[bool, str]:
    changed = _changed_paths_since(root, baseline_hashes)
    stub_issues = scan_placeholder_violations(changed, root=root)
    struct_ok, struct_msg = evaluate_step_verification(VerificationSpec("assertion"), root, "ok", changed)
    if not struct_ok:
        stub_issues.append(struct_msg)
    if goal_plan is not None:
        for st in goal_plan.steps:
            if st.verification.kind in ("evidence_file", "check_command", "script_validator", "domain_validator"):
                ev_ok, ev_msg = evaluate_step_verification(st.verification, root, "ok", changed)
                if ev_ok:
                    st.status = "completed"
                else:
                    stub_issues.append(f"Step `{st.step_id}` verification failed: {ev_msg}")
            elif st.status != "completed":
                stub_issues.append(f"Step `{st.step_id}` ({st.title}) incomplete (`{st.status}`): {st.evidence or 'step did not pass verification'}")

    all_files = [p for p in walk_pruned_files(root) if p.name != ".gitignore"]
    has_src = len(all_files) > 0
    has_py = any(p.suffix.lower() == ".py" for p in all_files)
    py_errs = scan_placeholder_violations([p for p in all_files if p.suffix.lower() == ".py"], root=root)
    syn_ok = len(py_errs) == 0
    t_res = run_unit_tests(root=root) if run_doctor_tests else {"passed": True, "ran": 1, "output": "Tests skipped."}
    low_goal = goal.lower()
    needs_tests = run_doctor_tests and ("test" in low_goal) and (has_py or any(k in low_goal for k in ("unittest", "unit test", "test_", "pytest")))
    tests_ok = bool(t_res.get("passed")) and (not needs_tests or int(t_res.get("ran", 0)) >= 1)
    doc_ok = (has_src or not run_doctor_tests) and syn_ok and tests_ok
    extra = "" if has_src else "\nFAIL: Zero source files or artifacts created in workspace — write the requested files with `write_file`."
    if needs_tests and int(t_res.get("ran", 0)) == 0:
        extra += "\nFAIL: Goal requires unit tests, but 0 unit tests were found/executed in `tests/`."
    doc_summary = f"Workspace Gate (`{root}`): artifacts={'PASS' if has_src else 'FAIL'}, syntax={'PASS' if syn_ok else 'FAIL'}, tests={'PASS' if tests_ok else 'FAIL'}\n{t_res.get('output', '')}{extra}"
    passed = (len(stub_issues) == 0) and doc_ok
    parts = (["Verification / Contract Violations:\n" + "\n".join(f"  - {v}" for v in stub_issues)] if stub_issues else []) + [doc_summary]
    return passed, "\n\n".join(parts)


def _sync_switched_workspace(
    pinned_root: bool, pinned_cp: bool, prev_root: Path, eff_cp: CheckpointManager, wf: WorkflowEngine,
    wf_id: Optional[int], cp_id: str, baseline_hashes: Dict[str, str], goal_plan: GoalPlan, mem: MemoryStore,
) -> Tuple[Path, CheckpointManager, WorkflowEngine, str, Dict[str, str]]:
    ws = settings.get_workspace()
    active_root = ws.project_root.resolve()
    if pinned_root or active_root == prev_root:
        return prev_root, eff_cp, wf, cp_id, baseline_hashes
    new_cp = eff_cp if pinned_cp else CheckpointManager(root=active_root, backup_dir=ws.backup_dir, store=mem)
    new_wf = WorkflowEngine(store=mem, cp_manager=new_cp, root=active_root)
    if wf_id is not None:
        tids = [int(s.task_id) for s in goal_plan.steps if s.task_id]
        mem.rebind_workflow_project(wf_id, ws.project_id, task_ids=tids, checkpoint_id="")
        mem.update_workflow_metadata(wf_id, {"baseline_hashes": {}})
    return active_root, new_cp, new_wf, "", {}


def _execute_single_dag_step(
    step: GoalStep, idx: int, goal: str, design_handoff: str, plan_handoff: str,
    target_root: Path, built_rels: List[str], notes: List[str], run_doctor_tests: bool,
    max_phase_iters: int, chat_fn: Callable[..., Dict[str, Any]], mem: MemoryStore, verbose: bool,
    pinned_root: bool = False,
) -> Tuple[bool, str, Path]:
    tid = step.task_id or idx
    pure_verify, already_built = classify_task_autoskip(step.title, built_rels, target_root)
    if pure_verify and step.verification.kind == "assertion":
        ok_pre, v_pre = _verify_task_completion(target_root, [], False)
        t_res = run_unit_tests(root=target_root, timeout=30) if run_doctor_tests else {"passed": True, "ran": 0}
        if ok_pre and bool(t_res.get("passed")):
            auto_msg = f"Auto-verified in Python ({v_pre}; ran={t_res.get('ran', 0)} tests PASS)"
            if step.task_id:
                mem.update_task(step.task_id, status="completed", verification_result=auto_msg)
            return True, auto_msg, target_root

    pre_hashes = _snapshot_file_hashes(target_root)
    carry_block = format_task_carry_forward(target_root, built_rels, notes)
    fast_notice = "\n[FAST-PATH NOTICE: Target file(s) for this task already exist on disk from a prior step — inspect briefly and conclude if satisfied.]" if already_built else ""
    impl_prompt = (
        f"Overall Goal: {goal}\n\n{carry_block}Lossless Architecture Design Handoff:\n{design_handoff}\n\n"
        f"Lossless Plan Handoff:\n{plan_handoff}\n\nExecute Task #{tid}: {step.title}{fast_notice}"
    )
    out = run_phase_turn("IMPLEMENT", impl_prompt, min(max_phase_iters, 4) if already_built else max_phase_iters, chat_fn, mem, verbose)
    if not pinned_root and settings.active_project_root.resolve() != target_root:
        target_root = settings.active_project_root.resolve()
        pre_hashes = {}
        built_rels.clear()
    changed = _changed_paths_since(target_root, pre_hashes)
    for ch_p in changed:
        rel_s = str(ch_p.resolve().relative_to(target_root)).replace("\\", "/") if ch_p.resolve().is_relative_to(target_root) else ch_p.name
        if rel_s not in built_rels:
            built_rels.append(rel_s)
    ok_task, v_detail = _verify_task_completion(target_root, changed, run_doctor_tests)
    ok_spec, spec_msg = evaluate_step_verification(step.verification, target_root, out, changed)
    ok_all = ok_task and ok_spec
    combined = f"{v_detail}; {spec_msg}" if ok_all else (v_detail if not ok_task else spec_msg)
    if step.task_id:
        mem.update_task(step.task_id, status="completed" if ok_all else "in_progress", verification_result=f"{combined} | {out[:160]}" if ok_all else f"NEEDS_FIX: {combined}")
    return ok_all, combined, target_root


def _execute_implement_tasks(
    goal: str, design_handoff: str, plan_handoff: str, wf_id: Optional[int], meta: Dict[str, Any],
    target_root: Path, run_doctor_tests: bool, max_phase_iters: int,
    chat_fn: Callable[..., Dict[str, Any]], wf: WorkflowEngine, mem: MemoryStore, verbose: bool,
    pinned_root: bool = False, pinned_cp: bool = False, eff_cp: Optional[CheckpointManager] = None,
    cp_id: str = "", baseline_hashes: Optional[Dict[str, str]] = None, pause_only_human_gate: bool = False,
) -> Tuple[List[str], GoalPlan, str, Path, CheckpointManager, WorkflowEngine, str, Dict[str, str]]:
    raw_dag = meta.get("goal_dag")
    has_saved_dag = isinstance(raw_dag, dict) and bool(raw_dag.get("steps"))
    pending = mem.get_pending_tasks()
    if not pending and not has_saved_dag:
        tid = mem.add_task(f"Implement and verify: {goal[:120]}", priority=3, acceptance_criteria="Passes verification gate with zero stubs.", workflow_id=wf_id)
        pending = [(tid, f"Implement and verify: {goal[:120]}", "pending", 3)]

    goal_plan = GoalPlan.from_dict(raw_dag) if has_saved_dag else build_goal_plan_from_tasks(goal, pending, plan_handoff)
    if not goal_plan.steps:
        goal_plan = build_goal_plan_from_tasks(goal, pending, plan_handoff)

    cur_cp = eff_cp or get_active_checkpoint_manager()
    cur_hashes = baseline_hashes if baseline_hashes is not None else {}
    notes: List[str] = []
    completed_ids: List[int] = list(meta.get("completed_task_ids") or [])
    built_rels: List[str] = list(meta.get("built_rels") or [])
    approved_r3 = list(meta.get("approved_r3_steps") or [])
    legacy_r3 = bool(meta.get("r3_approved")) and not has_saved_dag

    for idx in range(1, min(len(goal_plan.steps), 25) + 1):
        ready = goal_plan.ready_steps()
        if not ready:
            break
        r0_batch = [s for s in ready if s.risk_tier == "R0" and not goal_plan.requires_pause(s, r3_approved=legacy_r3, approved_r3_steps=approved_r3)[0]][:3]
        if len(r0_batch) > 1:
            def _run_r0(st_item: Tuple[int, GoalStep]) -> Tuple[GoalStep, bool, str]:
                s_offset, s_obj = st_item
                ok_s, msg_s, _ = _execute_single_dag_step(s_obj, idx + s_offset, goal, design_handoff, plan_handoff, target_root, list(built_rels), list(notes), run_doctor_tests, max_phase_iters, chat_fn, mem, verbose, pinned_root=True)
                return s_obj, ok_s, msg_s

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(r0_batch)) as pool:
                futs = [pool.submit(contextvars.copy_context().run, _run_r0, (off, s_obj)) for off, s_obj in enumerate(r0_batch)]
                results = [f.result() for f in futs]
            for s_obj, ok_s, msg_s in results:
                s_obj.status = "completed" if ok_s else "blocked"
                s_obj.evidence = msg_s[:240]
                if ok_s and s_obj.task_id and s_obj.task_id not in completed_ids:
                    completed_ids.append(s_obj.task_id)
                notes.append(f"Step `{s_obj.step_id}` [{'VERIFIED' if ok_s else 'NEEDS_FIX'}]: {s_obj.title}" + ("" if ok_s else f" ({msg_s})"))
            if wf_id is not None:
                mem.update_workflow_metadata(wf_id, {"goal_dag": goal_plan.to_dict(), "completed_task_ids": completed_ids, "built_rels": built_rels})
            continue

        step = ready[0]
        if wf_id is not None:
            sync_workflow_to_project_graph(mem, wf_id, goal, "IMPLEMENT")
        must_pause, pause_reason = goal_plan.requires_pause(step, r3_approved=legacy_r3, approved_r3_steps=approved_r3)
        if must_pause:
            if pause_only_human_gate and step.verification.kind != "human_gate":
                break
            if wf_id is not None:
                mem.update_workflow_metadata(wf_id, {"goal_dag": goal_plan.to_dict(), "built_rels": built_rels, "awaiting_plan_approval": True, "pause_reason": pause_reason})
            wf.transition("PLAN", f"Paused at plan approval gate: {pause_reason[:120]}", run_tests_on_verify=False)
            return notes, goal_plan, pause_reason, target_root, cur_cp, wf, cp_id, cur_hashes

        prev_root = target_root
        step_r3_ok = legacy_r3 or (step.step_id in set(approved_r3))
        with r3_approval_context(step_r3_ok):
            ok_step, ev_msg, _ = _execute_single_dag_step(step, idx, goal, design_handoff, plan_handoff, target_root, built_rels, notes, run_doctor_tests, max_phase_iters, chat_fn, mem, verbose, pinned_root=pinned_root)
        target_root, cur_cp, wf, cp_id, cur_hashes = _sync_switched_workspace(pinned_root, pinned_cp, prev_root, cur_cp, wf, wf_id, cp_id, cur_hashes, goal_plan, mem)
        step.status = "completed" if ok_step else "blocked"
        step.evidence = ev_msg[:240]
        if ok_step and step.task_id and step.task_id not in completed_ids:
            completed_ids.append(step.task_id)
        tag = "AUTO-VERIFIED" if "Auto-verified" in ev_msg else ("VERIFIED" if ok_step else "NEEDS_FIX")
        notes.append(f"Step `{step.step_id}` [{tag}]: {step.title}" + ("" if ok_step else f" ({ev_msg})"))
        if wf_id is not None:
            mem.update_workflow_metadata(wf_id, {"goal_dag": goal_plan.to_dict(), "completed_task_ids": completed_ids, "built_rels": built_rels})
    return notes, goal_plan, "", target_root, cur_cp, wf, cp_id, cur_hashes


def _ensure_wf_state(wf: WorkflowEngine, mem: MemoryStore, target_state: str, summary: str) -> None:
    active = mem.get_active_workflow()
    if active and str(active.get("current_state")) == target_state:
        return
    wf.transition(target_state, summary, run_tests_on_verify=False)


def _prepare_workflow_start_or_resume(
    clean_goal: str, workflow_type: str, resume: bool, wf: WorkflowEngine, mem: MemoryStore,
) -> Tuple[Optional[int], str, str, Dict[str, Any], Optional[Dict[str, Any]]]:
    if not resume:
        start_res = wf.start_workflow(goal=clean_goal, workflow_type=workflow_type, acceptance_criteria="All verification gates pass with zero placeholder stubs.")
        return start_res.workflow_id, start_res.checkpoint_id or "", clean_goal, {}, None
    saved = mem.get_resumable_workflow()
    if not saved:
        return None, "", "", {}, {"status": "error", "passed": False, "summary": "No interrupted or active autonomous workflow found to resume."}
    wf_id, cp_id, meta = int(saved["id"]), str(saved.get("checkpoint_id") or ""), dict(saved.get("metadata") or {})
    restored_goal = str(saved.get("goal") or clean_goal)
    if meta.get("last_revision") and str(meta["last_revision"]) not in restored_goal:
        restored_goal = f"{restored_goal}\nRevision requirement: {meta['last_revision']}"
    curr = str(saved.get("current_state") or "INTAKE")
    if saved.get("status") != "active" or curr in ("ABORTED", "COMPLETE", "IDLE"):
        reentry = "PLAN" if meta.get("plan_handoff") else ("DESIGN" if meta.get("design_handoff") else ("DISCOVER" if meta.get("discovery_handoff") else "INTAKE"))
        mem.update_workflow_state(wf_id, curr, reentry, "Resumed interrupted autonomous workflow", status="active")
    return wf_id, cp_id, restored_goal, meta, None


def _run_planning_phases(
    clean_goal: str, wf_id: Optional[int], meta: Dict[str, Any], target_root: Path,
    max_phase_iters: int, chat_fn: Callable[..., Dict[str, Any]], wf: WorkflowEngine, mem: MemoryStore, verbose: bool,
) -> Tuple[str, str, str]:
    half = max(4, max_phase_iters // 2)
    disc = str(meta.get("discovery_handoff") or "")
    if not disc:
        _ensure_wf_state(wf, mem, "DISCOVER", "Autonomous DISCOVER phase started")
        raw = run_phase_turn("DISCOVER", f"Goal: {clean_goal}\nInspect relevant workspace files, configs, and tests. Do NOT edit files; return a grounded impact summary with exact file paths.", half, chat_fn, mem, verbose)
        disc = build_lossless_phase_handoff("DISCOVER", raw, max_chars=5000)
        if wf_id is not None:
            mem.update_workflow_metadata(wf_id, {"discovery_handoff": disc})

    design = str(meta.get("design_handoff") or "")
    if not design:
        _ensure_wf_state(wf, mem, "DESIGN", "Discovery complete; entering reasoning DESIGN phase")
        raw = run_phase_turn("DESIGN", f"Goal: {clean_goal}\nLossless Discovery Findings:\n{disc}\n\nKeep internal <think> reasoning brief (<200 words). Produce a concise technical design (<400 words; clean modular design, explicit verification evidence) directly from the Discovery Findings.", 1, chat_fn, mem, verbose)
        design = build_lossless_phase_handoff("DESIGN", raw, max_chars=5000)
        if wf_id is not None:
            mem.update_workflow_metadata(wf_id, {"design_handoff": design})

    plan = str(meta.get("plan_handoff") or "")
    if not plan:
        _ensure_wf_state(wf, mem, "PLAN", "Design complete; breaking down atomic tasks")
        raw = run_phase_turn("PLAN", f"Goal: {clean_goal}\nLossless Design:\n{design}\n\nKeep internal <think> reasoning brief (<200 words). Batch 1-4 atomic `task_manage(action='add_task', ...)` calls in a single step, then summarize the execution plan concisely in plain English. File writes are locked in PLAN.{_PLAN_DAG_HINT}", min(half, 3), chat_fn, mem, verbose)
        plan = build_lossless_phase_handoff("PLAN", raw, max_chars=4500)
        dag = build_goal_plan_from_tasks(clean_goal, mem.get_pending_tasks(), plan)
        meta["goal_dag"] = dag.to_dict()
        if wf_id is not None:
            mem.update_workflow_metadata(wf_id, {"plan_handoff": plan, "goal_dag": meta["goal_dag"]})
    get_project_graph(mem).seed_dikw_goal_milestones(settings.get_workspace().project_id)
    if wf_id is not None:
        sync_workflow_to_project_graph(mem, wf_id, clean_goal, "PLAN")
    return disc, design, plan


def _reconcile_recovered_steps(goal_plan: GoalPlan, root: Path, rec_out: str, baseline_hashes: Dict[str, str], mem: MemoryStore) -> None:
    changed = _changed_paths_since(root, baseline_hashes)
    for st in goal_plan.steps:
        if st.status == "blocked" and st.verification.kind != "human_gate":
            ev_ok, ev_msg = evaluate_step_verification(st.verification, root, rec_out, changed)
            if ev_ok:
                st.status = "completed"
                st.evidence = ev_msg[:240]
                if st.task_id:
                    mem.update_task(st.task_id, status="completed", verification_result=f"Recovered: {ev_msg[:180]}")


def _finalize_autonomous_goal(
    passed: bool, clean_goal: str, wf_id: Optional[int], cp_id: str, recoveries_used: int, max_recoveries: int,
    impl_count: int, gate_report: str, elapsed_s: float, rollback_on_failure: bool, target_root: Path,
    baseline_hashes: Dict[str, str], mem: MemoryStore, eff_cp: CheckpointManager, wf: WorkflowEngine, auto_git_commit: bool = False,
) -> Dict[str, Any]:
    if not passed:
        rb_note = ""
        if rollback_on_failure and cp_id:
            rb = eff_cp.rollback_checkpoint(cp_id, reason=f"Autonomous goal failed verification: {clean_goal[:100]}")
            rb_note = f" Workspace rolled back to `{cp_id}` ({rb.get('restored_count', 0)} files restored)."
        wf.transition("ABORTED", f"Verification failed after {recoveries_used} recovery attempt(s).{rb_note}", run_tests_on_verify=False)
        telemetry.log("WARN", "autonomy", f"Autonomous goal aborted ({elapsed_s}s): {clean_goal[:100]}")
        summary = f"=== Autonomous Goal Execution: FAILED ({elapsed_s}s) ===\nGoal: {clean_goal}\nRecoveries Attempted: {recoveries_used}/{max_recoveries}.{rb_note}\n\nGround-Truth Verification Report:\n{gate_report}"
        return {"status": "FAILED", "passed": False, "workflow_id": wf_id, "checkpoint_id": cp_id, "recoveries": recoveries_used, "summary": summary}

    wf.transition("REFLECT", "All verification gates and contract checks passed", run_tests_on_verify=False)
    for ip_task in mem.get_all_tasks(status_filter="in_progress"):
        mem.update_task(int(ip_task["id"]), status="completed", verification_result=f"Verified by Workflow #{wf_id} final verification gate")
    distill_workflow_reflection(clean_goal, wf_id, _changed_paths_since(target_root, baseline_hashes), target_root, mem)
    if target_root == settings.root.resolve():
        mem.sync_memory_md()
    git_res = eff_cp.create_git_savepoint(f"Complete Workflow #{wf_id}: {clean_goal[:72]}", init_if_needed=False) if auto_git_commit else {}
    git_line = f"\nGit Save-Point: {git_res['summary']}" if git_res.get("summary") else ""
    wf.transition("COMPLETE", f"Autonomous goal verified and completed in {elapsed_s}s", run_tests_on_verify=False)
    if wf_id is not None:
        sync_workflow_to_project_graph(mem, wf_id, clean_goal, "COMPLETE")
    telemetry.log("INFO", "autonomy", f"Autonomous goal completed ({elapsed_s}s): {clean_goal[:100]}")
    summary = f"=== Autonomous Goal Execution: COMPLETE ({elapsed_s}s) ===\nGoal: {clean_goal}\nWorkflow #{wf_id} | Checkpoint: `{cp_id}` | Recoveries Used: {recoveries_used}\nTasks Processed: {impl_count}{git_line}\n\n{gate_report}"
    return {"status": "COMPLETE", "passed": True, "workflow_id": wf_id, "checkpoint_id": cp_id, "git_commit": git_res.get("commit", ""), "recoveries": recoveries_used, "summary": summary}


def run_autonomous_goal(
    goal: str, workflow_type: str = "engineering_feature", max_phase_iters: int = 10, max_recoveries: int = 2,
    chat_fn: Callable[..., Dict[str, Any]] = chat, store: Optional[MemoryStore] = None, cp_mgr: Optional[CheckpointManager] = None,
    root: Optional[Path] = None, run_doctor_tests: bool = True, rollback_on_failure: bool = True,
    verbose: bool = True, resume: bool = False, pause_at_plan: bool = False, auto_git_commit: Optional[bool] = None,
) -> Dict[str, Any]:
    raw_goal = (goal or "").strip()
    is_resume = resume or raw_goal.lower() in ("resume", "continue", "approve")
    if not raw_goal and not is_resume:
        return {"status": "error", "passed": False, "summary": "error: goal is required for autonomous execution."}

    pinned_root, pinned_cp = (root is not None), (cp_mgr is not None)
    mem, target_root, eff_cp, wf = resolve_goal_engines(store, cp_mgr, root)
    t0 = time.perf_counter()

    with model_gate.foreground_turn():
        wf_id, cp_id, clean_goal, meta, resume_err = _prepare_workflow_start_or_resume(raw_goal, workflow_type, is_resume, wf, mem)
        if resume_err is not None:
            return resume_err
        saved_hashes = meta.get("baseline_hashes")
        if is_resume and isinstance(saved_hashes, dict):
            baseline_hashes = {str(k): str(v) for k, v in saved_hashes.items()}
        else:
            baseline_hashes = _snapshot_file_hashes(target_root)
            if wf_id is not None:
                mem.update_workflow_metadata(wf_id, {"baseline_hashes": baseline_hashes})

        _, design_handoff, plan_handoff = _run_planning_phases(clean_goal, wf_id, meta, target_root, max_phase_iters, chat_fn, wf, mem, verbose)
        if pause_at_plan:
            if wf_id is not None:
                mem.update_workflow_metadata(wf_id, {"awaiting_plan_approval": True})
            return {"status": "AWAITING_APPROVAL", "passed": True, "awaiting_approval": True, "workflow_id": wf_id, "checkpoint_id": cp_id, "goal": clean_goal, "design_handoff": design_handoff, "plan_handoff": plan_handoff, "summary": plan_handoff}

        if wf_id is not None and meta.get("awaiting_plan_approval"):
            mem.update_workflow_metadata(wf_id, {"awaiting_plan_approval": False})
        _ensure_wf_state(wf, mem, "IMPLEMENT", "Plan locked; executing build steps")
        impl_notes, goal_plan, pause_reason, target_root, eff_cp, wf, cp_id, baseline_hashes = _execute_implement_tasks(
            clean_goal, design_handoff, plan_handoff, wf_id, meta, target_root, run_doctor_tests, max_phase_iters,
            chat_fn, wf, mem, verbose, pinned_root=pinned_root, pinned_cp=pinned_cp, eff_cp=eff_cp, cp_id=cp_id, baseline_hashes=baseline_hashes,
        )
        if pause_reason:
            return {
                "status": "AWAITING_APPROVAL", "passed": True, "awaiting_approval": True,
                "workflow_id": wf_id, "checkpoint_id": cp_id, "goal": clean_goal, "pause_reason": pause_reason,
                "summary": f"=== Workflow Paused for User Approval (`PLAN`) ===\n{pause_reason}\nReply `approve` or run `/approve` to authorize and continue.",
            }

        _ensure_wf_state(wf, mem, "VERIFY", "Implementation steps finished; running hard verification gate")
        if wf_id is not None:
            sync_workflow_to_project_graph(mem, wf_id, clean_goal, "VERIFY")
        passed, gate_report = _evaluate_hard_verification(target_root, baseline_hashes, run_doctor_tests, clean_goal, goal_plan)
        recoveries_used = 0
        while not passed and recoveries_used < max_recoveries:
            recoveries_used += 1
            wf.recover(reason=f"Hard verification failed (attempt {recoveries_used}): {gate_report[:240]}", rollback=False)
            rec_report = build_lossless_phase_handoff("VERIFY_FAILURE", gate_report, max_chars=4500)
            prev_rec_root = target_root
            rec_out = run_phase_turn("RECOVER", f"Goal: {clean_goal}\nHard Verification Gate FAILED (Recovery {recoveries_used}/{max_recoveries}):\n{rec_report}\n\nFix all failing checks and missing artifacts directly.", max_phase_iters, chat_fn, mem, verbose)
            target_root, eff_cp, wf, cp_id, baseline_hashes = _sync_switched_workspace(pinned_root, pinned_cp, prev_rec_root, eff_cp, wf, wf_id, cp_id, baseline_hashes, goal_plan, mem)
            _reconcile_recovered_steps(goal_plan, target_root, rec_out, baseline_hashes, mem)
            nxt = goal_plan.ready_steps()
            if nxt:
                meta["goal_dag"] = goal_plan.to_dict()
                _ensure_wf_state(wf, mem, "IMPLEMENT", f"Executing unlocked steps after RECOVER {recoveries_used}")
                more, goal_plan, rec_pause, target_root, eff_cp, wf, cp_id, baseline_hashes = _execute_implement_tasks(
                    clean_goal, design_handoff, plan_handoff, wf_id, meta, target_root, run_doctor_tests, max_phase_iters,
                    chat_fn, wf, mem, verbose, pinned_root=pinned_root, pinned_cp=pinned_cp, eff_cp=eff_cp, cp_id=cp_id,
                    baseline_hashes=baseline_hashes, pause_only_human_gate=True,
                )
                impl_notes.extend(more)
                if rec_pause:
                    return {
                        "status": "AWAITING_APPROVAL", "passed": True, "awaiting_approval": True,
                        "workflow_id": wf_id, "checkpoint_id": cp_id, "goal": clean_goal, "pause_reason": rec_pause,
                        "summary": f"=== Workflow Paused for User Approval (`PLAN`) ===\n{rec_pause}\nReply `approve` or run `/approve` to authorize and continue.",
                    }
            if wf_id is not None:
                mem.update_workflow_metadata(wf_id, {"goal_dag": goal_plan.to_dict()})
            _ensure_wf_state(wf, mem, "VERIFY", f"Re-verifying after RECOVER attempt {recoveries_used}")
            passed, gate_report = _evaluate_hard_verification(target_root, baseline_hashes, run_doctor_tests, clean_goal, goal_plan)

        eff_git = (settings.auto_git_commit and chat_fn is chat) if auto_git_commit is None else bool(auto_git_commit)
        return _finalize_autonomous_goal(
            passed, clean_goal, wf_id, cp_id, recoveries_used, max_recoveries,
            len(impl_notes), gate_report, round(time.perf_counter() - t0, 1),
            rollback_on_failure, target_root, baseline_hashes, mem, eff_cp, wf, auto_git_commit=eff_git,
        )


def _revise_existing_blueprint(
    feedback: str, max_phase_iters: int, chat_fn: Callable[..., Dict[str, Any]],
    mem: MemoryStore, cp_mgr: Optional[CheckpointManager], root: Optional[Path], verbose: bool,
) -> Dict[str, Any]:
    active = mem.get_active_workflow()
    if not active:
        return {"status": "error", "passed": False, "awaiting_approval": False, "summary": "No active Blueprint workflow found to revise. Start one with `/plan <objective>`."}
    wf_id, goal, cp_id = int(active["id"]), str(active.get("goal") or ""), str(active.get("checkpoint_id") or "")
    meta = dict(active.get("metadata") or {})
    disc_handoff, prev_design = str(meta.get("discovery_handoff") or ""), str(meta.get("design_handoff") or "")
    alignment_notes = [*list(meta.get("alignment_notes") or []), feedback]
    combined_feedback = "; ".join(alignment_notes)
    _, _, _, wf = resolve_goal_engines(mem, cp_mgr, root)

    for t in mem.get_all_tasks(status_filter="pending"):
        if t.get("workflow_id") == wf_id:
            mem.update_task(int(t["id"]), status="cancelled", verification_result="Superseded by Blueprint revision")

    half = max(4, max_phase_iters // 2)
    with model_gate.foreground_turn():
        mem.update_workflow_state(wf_id, str(active.get("current_state") or "PLAN"), "DESIGN", f"Revising Blueprint design per feedback: {feedback[:80]}", status="active")
        design_raw = run_phase_turn(
            "DESIGN",
            f"Goal: {goal}\nAccumulated User Alignment & Feedback: {combined_feedback}\nPrior Design:\n{prev_design}\nDiscovery Context:\n{disc_handoff}\n\n"
            "Keep internal <think> reasoning brief (<200 words). Update the technical design concisely (<400 words) to incorporate all user alignment details.",
            1, chat_fn, mem, verbose,
        )
        design_handoff = build_lossless_phase_handoff("DESIGN", design_raw, max_chars=5000)
        wf.transition("PLAN", "Revised design ready; updating atomic task plan", run_tests_on_verify=False)
        plan_raw = run_phase_turn(
            "PLAN",
            f"Goal: {goal}\nAccumulated User Alignment & Feedback: {combined_feedback}\nRevised Design:\n{design_handoff}\n\n"
            f"Keep internal <think> reasoning brief (<200 words). Batch 1-4 updated atomic `task_manage(action='add_task', ...)` calls in a single step, then explain the updated plan concisely in plain English.{_PLAN_DAG_HINT}",
            min(half, 3), chat_fn, mem, verbose,
        )
        plan_handoff = build_lossless_phase_handoff("PLAN", plan_raw, max_chars=4500)
        if not mem.get_pending_tasks():
            mem.add_task(f"Implement and verify: {goal[:140]} (Alignment: {combined_feedback[:140]})", priority=1, acceptance_criteria="Passes verification gate with zero stubs.", workflow_id=wf_id)
        dag_dict = build_goal_plan_from_tasks(goal, mem.get_pending_tasks(), plan_handoff).to_dict()
        mem.update_workflow_metadata(wf_id, {
            "design_handoff": design_handoff, "plan_handoff": plan_handoff, "goal_dag": dag_dict,
            "awaiting_plan_approval": True, "pause_reason": "", "last_revision": combined_feedback, "alignment_notes": alignment_notes,
        })
        sync_workflow_to_project_graph(mem, wf_id, goal, "PLAN")

    card = format_blueprint_card(wf_id, goal, cp_id, design_handoff, plan_handoff, mem.get_pending_tasks(), revised=True, goal_dag=dag_dict)
    telemetry.log("INFO", "blueprint", f"Revised Blueprint for Workflow #{wf_id}: {feedback[:80]}")
    return {"status": "AWAITING_APPROVAL", "passed": True, "awaiting_approval": True, "workflow_id": wf_id, "checkpoint_id": cp_id, "summary": card}


def handle_blueprint_conversation_turn(
    user_text: str, max_phase_iters: int = 10, chat_fn: Callable[..., Dict[str, Any]] = chat,
    store: Optional[MemoryStore] = None, cp_mgr: Optional[CheckpointManager] = None,
    root: Optional[Path] = None, verbose: bool = True,
) -> Dict[str, Any]:
    mem = store or get_memory_store()
    clean = (user_text or "").strip()
    active = mem.get_active_workflow()
    wf_id = int(active["id"]) if active else None
    meta = dict((active or {}).get("metadata") or {})
    if active and is_pure_blueprint_question(clean):
        with model_gate.foreground_turn():
            ans = run_phase_turn(
                "PLAN",
                f"Active Blueprint Goal: {active.get('goal', '')}\nCurrent Design:\n{meta.get('design_handoff', '')}\n"
                f"Current Plan:\n{meta.get('plan_handoff', '')}\n\nUser Question: {clean}\n"
                "Answer the user's question directly and concisely in plain English without modifying tasks or files. Remind them they can reply `approve` or suggest adjustments.",
                3, chat_fn, mem, verbose,
            )
        return {"status": "AWAITING_APPROVAL", "passed": True, "awaiting_approval": True, "workflow_id": wf_id, "summary": ans}
    notes = [*list(meta.get("alignment_notes") or []), clean]
    get_knowledge_port(mem).propose_update(
        key=f"blueprint:{wf_id or 'active'}:alignment",
        value="; ".join(notes)[:800],
        project_id=settings.get_workspace().project_id,
        source="user",
        confidence=1.0,
    )
    return _revise_existing_blueprint(clean, max_phase_iters, chat_fn, mem, cp_mgr, root, verbose)


def create_or_revise_blueprint(
    goal_or_feedback: str, workflow_type: str = "engineering_feature", revise: bool = False,
    max_phase_iters: int = 10, chat_fn: Callable[..., Dict[str, Any]] = chat,
    store: Optional[MemoryStore] = None, cp_mgr: Optional[CheckpointManager] = None,
    root: Optional[Path] = None, verbose: bool = True,
) -> Dict[str, Any]:
    mem = store or get_memory_store()
    clean = (goal_or_feedback or "").strip()
    if clean.lower() == "status":
        active = mem.get_active_workflow()
        if not active or not (active.get("metadata") or {}).get("awaiting_plan_approval"):
            return {"status": "IDLE", "passed": True, "awaiting_approval": False, "summary": "No pending Blueprint awaiting approval. Start one with `/plan <objective>`."}
        meta = active.get("metadata") or {}
        card = format_blueprint_card(
            int(active["id"]), str(active.get("goal") or ""), str(active.get("checkpoint_id") or ""),
            str(meta.get("design_handoff") or ""), str(meta.get("plan_handoff") or ""), mem.get_pending_tasks(),
            goal_dag=meta.get("goal_dag"), pause_reason=str(meta.get("pause_reason") or ""),
        )
        return {"status": "AWAITING_APPROVAL", "passed": True, "awaiting_approval": True, "workflow_id": int(active["id"]), "summary": card}

    if clean.lower().startswith("revise "):
        revise = True
        clean = clean[7:].strip()
    if not clean:
        return {"status": "error", "passed": False, "awaiting_approval": False, "summary": "Usage: `/plan <objective>` or `/plan revise <feedback>` or `/approve`."}
    if revise:
        return _revise_existing_blueprint(clean, max_phase_iters, chat_fn, mem, cp_mgr, root, verbose)

    res = run_autonomous_goal(goal=clean, workflow_type=workflow_type, max_phase_iters=max_phase_iters, chat_fn=chat_fn, store=mem, cp_mgr=cp_mgr, root=root, verbose=verbose, pause_at_plan=True)
    if not res.get("awaiting_approval"):
        return res
    saved_wf = mem.get_active_workflow()
    saved_dag = (saved_wf.get("metadata") or {}).get("goal_dag") if saved_wf else None
    card = format_blueprint_card(res.get("workflow_id"), str(res.get("goal") or clean), str(res.get("checkpoint_id") or ""), str(res.get("design_handoff") or ""), str(res.get("plan_handoff") or ""), mem.get_pending_tasks(), goal_dag=saved_dag)
    telemetry.log("INFO", "blueprint", f"Drafted Blueprint for Workflow #{res.get('workflow_id')}: {clean[:80]}")
    return {**res, "summary": card}


def approve_blueprint_and_build(
    max_phase_iters: int = 10, max_recoveries: int = 2, chat_fn: Callable[..., Dict[str, Any]] = chat,
    store: Optional[MemoryStore] = None, cp_mgr: Optional[CheckpointManager] = None,
    root: Optional[Path] = None, run_doctor_tests: bool = True, rollback_on_failure: bool = True,
    verbose: bool = True, auto_git_commit: Optional[bool] = None,
) -> Dict[str, Any]:
    mem = store or get_memory_store()
    saved = mem.get_resumable_workflow()
    if not saved:
        return {"status": "error", "passed": False, "summary": "No pending Blueprint or interrupted workflow found to approve. Run `/plan <objective>` first."}
    wf_id, meta = int(saved["id"]), dict(saved.get("metadata") or {})
    raw_dag = meta.get("goal_dag")
    has_dag = isinstance(raw_dag, dict) and bool(raw_dag.get("steps"))
    approved_r3 = list(meta.get("approved_r3_steps") or [])
    updates: Dict[str, Any] = {"awaiting_plan_approval": False, "blueprint_approved": True, "pause_reason": ""}
    if not has_dag:
        updates["r3_approved"] = True
    if has_dag:
        dag = GoalPlan.from_dict(raw_dag)
        ready = dag.ready_steps()
        if ready:
            gate_step = ready[0]
            if gate_step.verification.kind == "human_gate":
                gate_step.evidence = "human_approved"
                gate_step.status = "completed"
                if gate_step.task_id:
                    mem.update_task(gate_step.task_id, status="completed", verification_result="Approved by user at human_gate")
                updates["goal_dag"] = dag.to_dict()
            if gate_step.risk_tier == "R3" and gate_step.step_id not in approved_r3:
                approved_r3.append(gate_step.step_id)
        updates["approved_r3_steps"] = approved_r3
    mem.update_workflow_metadata(wf_id, updates)
    telemetry.log("INFO", "blueprint", f"User approved Blueprint #{wf_id}; starting IMPLEMENT -> VERIFY")
    return run_autonomous_goal(
        goal="approve", max_phase_iters=max_phase_iters, max_recoveries=max_recoveries, chat_fn=chat_fn,
        store=mem, cp_mgr=cp_mgr, root=root, run_doctor_tests=run_doctor_tests,
        rollback_on_failure=rollback_on_failure, verbose=verbose, resume=True, pause_at_plan=False, auto_git_commit=auto_git_commit,
    )


def run_autonomous_goal_slice(
    project_id: Optional[str] = None, chat_fn: Callable[..., Dict[str, Any]] = chat,
    store: Optional[MemoryStore] = None, root: Optional[Path] = None, max_iters: int = 6, verbose: bool = False,
) -> Dict[str, Any]:
    mem, target_root, eff_cp, _ = resolve_goal_engines(store, None, root)
    mgr = get_project_graph(mem)
    ready = mgr.next_ready_companion_items(project_id=project_id, limit=1)
    if not ready:
        return {"ok": True, "ran": False, "status": "idle", "summary": "No ready companion work items."}

    item = ready[0]
    pid = str(item["project_id"])
    proj = mgr.get_project(pid)
    cp_id = eff_cp.create_checkpoint(label=f"slice_{item['item_id']}")["checkpoint_id"]
    pre_hashes = _snapshot_file_hashes(target_root)
    mgr.update_item(item["item_id"], status="in_progress", checkpoint_id=cp_id)

    if item.get("dag_name"):
        dag_res = execute_dag_by_name(str(item["dag_name"]), params={"goal": item["title"], "item_id": item["item_id"], "project_id": pid}, store=mem, root=target_root)
        if dag_res.get("status") == "waiting_approval":
            summary = str(dag_res.get("summary") or f"Paused awaiting approval (`{dag_res.get('approval_id')}`).")
            mgr.update_item(item["item_id"], status="waiting_approval", checkpoint_id=cp_id, verification_result=summary[:240])
            mem.log_autonomy_run(pid, summary, entry_kind="goal_slice", status="waiting_approval", details={"item_id": item["item_id"], "approval_id": dag_res.get("approval_id", "")})
            return {"ok": True, "ran": True, "status": "waiting_approval", "project_id": pid, "item_id": item["item_id"], "approval_id": dag_res.get("approval_id", ""), "summary": summary}
        out, exec_ok = str(dag_res.get("summary") or ""), bool(dag_res.get("ok"))
    else:
        prompt = (
            f"Project / Goal: {proj['title']} ({proj['goal_summary']})\nPrerequisite Stage: {item['stage_kind']}\n"
            f"Execute Work Item `{item['item_id']}`: {item['title']}\nDescription: {item['description']}\n"
            f"Acceptance Criteria: {item['acceptance_criteria'] or 'Complete cleanly with grounded verification.'}"
        )
        out, exec_ok = run_phase_turn("IMPLEMENT", prompt, max_iters, chat_fn, mem, verbose), True

    changed = _changed_paths_since(target_root, pre_hashes)
    v_dict = item.get("verification") or {}
    vspec = VerificationSpec.from_dict(v_dict) if v_dict else VerificationSpec("assertion")
    ok_ver, ver_msg = evaluate_step_verification(vspec, target_root, out, changed)
    ok_task, task_msg = _verify_task_completion(target_root, changed, run_doctor_tests=False)
    passed = exec_ok and ok_ver and ok_task
    new_status = "done" if passed else "blocked"
    detail_msg = f"{ver_msg}; {task_msg}" if passed else f"NEEDS_FIX: {ver_msg if not ok_ver else task_msg}"
    updated_item = mgr.update_item(item["item_id"], status=new_status, checkpoint_id=cp_id, verification_result=f"{detail_msg} | {out[:140]}"[:320])
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    mgr.update_project(pid, metadata={"last_slice_at": now_iso, "last_slice_item": item["item_id"]})
    summary = f"Goal slice `{item['item_id']}` ({item['title'][:80]}) -> {new_status.upper()}: {detail_msg[:160]}"
    mem.log_autonomy_run(pid, new_status, summary, checkpoint_id=cp_id, doctor_passed=passed, entry_kind="goal_slice")
    return {"ok": passed, "ran": True, "status": new_status, "project_id": pid, "item": updated_item, "checkpoint_id": cp_id, "summary": summary}


# ---------------------------------------------------------------------------
# 5. HTTP Smoke Probe, Runtime Bindings & CLI/Web Slash Commands
# ---------------------------------------------------------------------------
def run_http_smoke_probe(
    module_name: str = "surfaces.web_server", factory_name: str = "create_web_server",
    endpoints: Optional[List[str]] = None, timeout: float = 5.0,
) -> Dict[str, Any]:
    probe_paths = endpoints or ["/", "/api/health", "/api/status"]
    tmp_ctx = tempfile.TemporaryDirectory()
    iso_store: Optional[MemoryStore] = None
    try:
        mod = importlib.import_module(module_name)
        factory = getattr(mod, factory_name)
        kwargs: Dict[str, Any] = {"host": "127.0.0.1", "port": 0}
        if "store" in inspect.signature(factory).parameters and not has_memory_store_override():
            iso_store = MemoryStore(db_path=Path(tmp_ctx.name) / "smoke.db")
            kwargs["store"] = iso_store
        server, _ = factory(**kwargs)
    except Exception as exc:
        if iso_store is not None:
            iso_store.close()
        tmp_ctx.cleanup()
        return {"passed": False, "port": 0, "results": [], "summary": f"HTTP smoke probe failed to initialize `{module_name}.{factory_name}`: {type(exc).__name__}: {exc}"}

    port = int(server.server_address[1])
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    results: List[Dict[str, Any]] = []
    all_ok = True
    try:
        for ep in probe_paths:
            clean_ep = ep if ep.startswith("/") else f"/{ep}"
            url = f"http://127.0.0.1:{port}{clean_ep}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "companion-smoke-probe/4.2"})
                with direct_opener.open(req, timeout=timeout) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    code = int(resp.status)
                ok = 200 <= code < 300 and len(body.strip()) > 0
                all_ok = all_ok and ok
                results.append({"endpoint": clean_ep, "status": code, "bytes": len(body), "passed": ok})
            except Exception as exc:
                raw_fn = getattr(mod, "dispatch_raw_http", None)
                if callable(raw_fn) and ("10013" in str(exc) or "PermissionError" in str(exc) or "Errno 13" in str(exc)):
                    code, body = raw_fn(server, "GET", clean_ep)
                    ok = 200 <= code < 300 and len(body.strip()) > 0
                    all_ok = all_ok and ok
                    results.append({"endpoint": clean_ep, "status": code, "bytes": len(body), "passed": ok})
                else:
                    all_ok = False
                    results.append({"endpoint": clean_ep, "status": 0, "bytes": 0, "passed": False, "error": str(exc)})
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2.0)
        if iso_store is not None:
            iso_store.close()
        with contextlib.suppress(Exception):
            tmp_ctx.cleanup()

    badge = "PASS" if all_ok else "FAIL"
    lines = [f"=== HTTP Smoke Probe (`{module_name}` @ 127.0.0.1:{port}): {badge} ==="]
    for r in results:
        err_note = f" ({r['error']})" if r.get("error") else ""
        lines.append(f"  [{'PASS' if r['passed'] else 'FAIL'}] GET {r['endpoint']} -> HTTP {r['status']} ({r['bytes']} bytes){err_note}")
    return {"passed": all_ok, "port": port, "results": results, "summary": "\n".join(lines)}


def _run_bounded_dag_llm(prompt: str, system_prompt: str = "", tier: str = "fast") -> str:
    msgs = [
        {"role": "system", "content": system_prompt or "Produce a concise, grounded output without calling tools."},
        {"role": "user", "content": prompt},
    ]
    with tier_context((tier or "fast").strip().lower()):
        data = chat(msgs, None, "none")
    clean, _, _ = normalize_assistant_message((data.get("choices") or [{}])[0].get("message") or {})
    return clean.strip()


def ensure_dag_runtime_bound() -> None:
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


def check_in(store: Optional[MemoryStore] = None, chat_fn: Optional[Callable[..., Dict[str, Any]]] = None) -> str:
    """Provide a grounded proactive check-in summary of Companion's current state."""
    mem, ws = store or get_memory_store(), settings.get_workspace()
    wf, spr, edu = mem.get_active_workflow(), get_project_graph(store=mem).get_active_timebox(), get_education_engine(store=mem).get_progress_summary()
    ac = edu.get("active_course")
    bg_lbl = "ON" if mem.is_background_autonomy_enabled() else "OFF"
    spr_line = f"`{spr['title']}` ({spr['start_date']}..{spr['end_date']})" if spr else "(None active)"
    edu_line = f"`{ac['title']}` ({ac['mastery_pct']}% mastery, {edu['due_total']} due)" if ac else f"(None active, {edu['due_total']} due)"
    base = (
        f"=== Proactive Check-In ===\nActive Project  : `{ws.project_id}` ({ws.project_root})\n"
        f"Autonomy Posture: Background Autonomy={bg_lbl} | Installed App=IMMUTABLE\n"
        f"Workflow State  : `{wf['current_state'] if wf else 'IDLE'}`\n"
        f"Active Timebox  : {spr_line}\n"
        f"Study Course    : {edu_line}\n"
        f"Pending Tasks   : {len(mem.get_pending_tasks()[:6])} active | Pending Backlog: {len(mem.get_backlog(status='pending')[:6])}\n"
        f"Recent Notes    : {len(mem.get_system_notes(limit=5))} notes saved"
    )
    if chat_fn is not None or ("unittest" not in sys.modules and os.environ.get("COMPANION_TEST_MODE") != "1"):
        llm_call = chat_fn or chat
        try:
            ready = [it["title"] for it in get_project_graph(store=mem).next_ready_companion_items(limit=4)]
            with tier_context("fast"):
                resp = llm_call([
                    {"role": "system", "content": "Write a 2-sentence prioritized next-action coaching note grounded strictly in the user's state snapshot."},
                    {"role": "user", "content": f"{base}\nReady items: {ready}"},
                ], None, "none")
            note, _, _ = normalize_assistant_message((resp.get("choices") or [{}])[0].get("message") or {})
            if note.strip():
                return f"{base}\n\n**Priority Brief**: {note.strip()}"
        except Exception:
            pass
    return f"{base}\nAll systems operational. Ready for your next request."


SLASH_HELP_TEXT = (
    "=== Companion AI OS Commands ===\n"
    "  /status | /checkin      — Live system status, WorkspaceContext, workflow HUD, tasks & proactive check-in\n"
    "  /project [init|switch..]— Manage isolated workspaces (`/project list`, `init <name>`, `switch <name>`, `self`)\n"
    "  /model & /providers     — View, ping (`check`), switch model tiers, or inspect multi-provider & Vault matrix\n"
    "  /identity | /tone | /onboard — Soul & User Identity cards, tone (`concise|balanced|elaborate`), First-Boot setup\n"
    "  /telemetry | /traces | /logs — Live KPI scorecard, Waterfall execution traces & structured app logs\n"
    "  /doctor                 — 7-stage Runtime Health Doctor\n"
    "  /autonomy | /morning   — Background Autonomy controls & Morning Briefing\n"
    "  /skills [name]          — List Progressive-Disclosure `SKILL.md` & `data/domains/` packs or read a skill\n"
    "  /plan <obj> | /approve  — Draft/revise read-only Blueprint (`DISCOVER -> DESIGN -> PLAN`) & approve build\n"
    "  /goal <obj|resume>      — Execute or resume a multi-phase autonomous build with verification gates\n"
    "  /subagent               — Spawn an isolated subagent worker\n"
    "  /workflow | /backlog    — Show Workflow graph, run a verified workflow, or list tasks/backlog\n"
    "  /memory | /compact      — Sync/display MEMORY.md or trigger LLM context compaction\n"
    "  /jobs | /wiki | /vault  — Background Job Departures Board, Knowledge Wiki Warehouse & Blind Credential Vault\n"
    "  /endpoints | /mcp | /partners — SSH/WinRM/HTTP Registry, Outbound MCP Client & Federated Partners\n"
    "  /routines | /approvals | /trust | /backup — Routines, ApprovalQueue, Trust Mode & Secret-Free Backups\n"
    "  /board | /sprint | /calendar | /study — Multi-Board Kanban, Weekly Sprints, Spatial Calendar & Study Courses\n"
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
        return dispatch("delegate_subagent", {"skill": skill_arg, "tier": tier_arg, "goal": rest})
    finally:
        if stid:
            telemetry.end_trace(stid, status="ok")


def _dispatch_workflow_slash(arg: str) -> str:
    sub, rest = (arg.split()[0].lower() if arg else "status"), (arg.split(None, 1)[1].strip() if len(arg.split(None, 1)) > 1 else "")
    if sub in ("abort", "cancel", "reset"):
        return dispatch("workflow_manage", {"action": "abort", "summary": "Aborted via CLI slash command"})
    if sub in ("dags", "list_dags", "recipes", "list_recipes", "workflows"):
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


def handle_slash_command(
    cmd_str: str, messages: List[Dict[str, Any]], store: Optional[MemoryStore] = None,
    chat_fn: Callable[..., Dict[str, Any]] = chat, interactive: bool = True,
) -> Optional[str]:
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
        from companion import __version__

        ws, hud, tasks, backlog = settings.get_workspace(), workflow_engine.render_hud(), mem.get_pending_tasks(), mem.get_backlog(status="pending")
        spr, edu = get_project_graph(store=mem).get_active_timebox(), get_education_engine(store=mem).get_progress_summary()
        ac = edu.get("active_course")
        mode, f_cfg, c_cfg, r_cfg = mem.get_user_profile().get("active_model_tier", "auto"), settings.resolve_tier("fast"), settings.resolve_tier("coding"), settings.resolve_tier("reasoning")
        code_seg = f" | coding=`{c_cfg.model}` (temp={c_cfg.temperature})" if c_cfg.is_dedicated else ""
        spr_s = f"`{spr['title']}` ({spr['start_date']}..{spr['end_date']})" if spr else "(none)"
        edu_s = f"`{ac['title']}` ({ac['mastery_pct']}%, {edu['due_total']} due)" if ac else "(none)"
        return f"=== Companion AI OS v{__version__} Status ===\nProject    : `{ws.project_id}` ({ws.project_root})\nRouting    : mode=`{mode}` | fast=`{f_cfg.model}` (temp={f_cfg.temperature}){code_seg} | reasoning=`{r_cfg.model}` (temp={r_cfg.temperature})\nPosture    : Background Autonomy={'ON' if mem.is_background_autonomy_enabled() else 'OFF'} | Installed App=IMMUTABLE (Idle: {settings.idle_minutes}m | Cooldown: {settings.hobby_cooldown_minutes}m)\nTimebox/Edu: Timebox={spr_s} | Study={edu_s}\n\n{hud}\n\nPending Tasks: {len(tasks)} | Pending Backlog: {len(backlog)}"
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
        from companion.routines import execute_autonomy_mode_action

        sub, sub_arg = (arg.split()[0].lower() if arg else "status"), (arg.split(None, 1)[1].strip() if len(arg.split(None, 1)) > 1 else None)
        res_aut = execute_autonomy_mode_action({"subaction": sub, "routine_id": sub_arg}, actor="cli", store=mem)
        return str(res_aut.get("summary") or res_aut.get("error") or "")
    if verb in ("morning", "journal"):
        from companion.routines import format_morning_briefing

        return format_morning_briefing(store=mem, mark_seen=True)
    if verb in ("checkin", "check-in"):
        return check_in(store=mem, chat_fn=chat_fn)
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
    from companion.core_actions import HARVEST_SLASH_VERBS, handle_harvest_slash

    if f"/{verb}" in HARVEST_SLASH_VERBS:
        out = handle_harvest_slash(f"/{verb}", arg, store=mem)
        if verb in ("trust", "partners", "mcp", "board", "boards", "sprint", "study", "providers") and messages and messages[0].get("role") == "system":
            messages[0]["content"] = build_system_prompt(store=mem)
        return out
    return f"Unknown slash command '/{verb}'. Type `/help` to see all available commands." if (cleaned.startswith("/") and "/" not in verb and "\\" not in verb) else None
