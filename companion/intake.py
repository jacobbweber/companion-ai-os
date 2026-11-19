"""
Cognitive Triage Gate, Uncompromised Model Tier Router & HTTP Smoke Probe
(`companion/intake.py` — A-Frame Application Layer).

Replaces brittle keyword-spotting regexes with a 2-stage Cognitive Triage Gate:
1. `<5ms` Deterministic Grounding, Blast-Radius (`R0..R3`), Verified Python `DAGSpec` Matching,
   and Active Project & Goal Graph Readiness Scan.
2. Schema-constrained JSON Cognitive Triage (when an optional fast triage model callable is bound)
   paired with a deterministic structural classifier routing across:
   - `DIRECT_TURN` (`CHAT_DIRECT`, `GUIDED_CHANGE`, `DEEP_CONSULT`)
   - `EXECUTE_VERIFIED_DAG` (`matched_dag` bound to a verified `skills/*/dags/*.py` graph)
   - `GOAL_ALIGN_PLAN` (`DISCOVER -> DESIGN -> PLAN` Blueprint or autonomous build)
   - `ADVANCE_ACTIVE_GOAL` (next ready `companion`/`collaborative` slice on `ProjectGraphManager`)
"""

import contextlib
import importlib
import inspect
import re
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from companion.config import settings
from companion.dag_runtime import discover_dags
from companion.disciplines import ActionRiskTier
from companion.grounding import EpistemicGrounding, build_epistemic_grounding
from companion.memory_store import MemoryStore, has_memory_store_override
from companion.project_graph import get_project_graph
from companion.streaming import get_active_wire_images
from companion.telemetry import telemetry

__all__ = [
    "IntentDecision",
    "PHASE_TIERS",
    "ReadinessAssessment",
    "TRIAGE_ROUTES",
    "TriageDecision",
    "assess_turn_readiness",
    "classify_turn_intent",
    "match_verified_dag",
    "resolve_workflow_phase_tier",
    "run_http_smoke_probe",
    "select_turn_step_tier",
    "triage_turn",
]

TRIAGE_ROUTES: Tuple[str, ...] = (
    "DIRECT_TURN",
    "EXECUTE_VERIFIED_DAG",
    "GOAL_ALIGN_PLAN",
    "ADVANCE_ACTIVE_GOAL",
)

PHASE_TIERS: Dict[str, str] = {
    "INTAKE": "fast",
    "DISCOVER": "fast",
    "DESIGN": "reasoning",
    "PLAN": "reasoning",
    "IMPLEMENT": "fast",
    "VERIFY": "fast",
    "RECOVER": "reasoning",
    "REFLECT": "fast",
}

_ARTIFACT_PATH_RE = re.compile(
    r"\b[a-zA-Z0-9_./-]+\.(?:py|json|toml|md|ya?ml|html?|js|ts|css|sql|sh|ps1)\b",
    re.I,
)
_NUMBERED_SPEC_RE = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*])\s+\S+", re.M)
_GENERIC_TECH_TERMS = frozenset({
    "adr", "api", "ast", "back-end", "cli", "command-line", "cross-project", "css", "csv", "dag",
    "domain-neutral", "end-to-end", "front-end", "full-stack", "html", "http", "https", "in-memory",
    "json", "key-value", "mcp", "multi-phase", "multi-step", "open-ended", "read-only", "real-time",
    "rest", "sdd", "self-test", "single-file", "sql", "sqlite", "state-machine", "toml", "url",
    "utf", "utf-8", "xml", "yaml", "zero-dependency",
})

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
    "reminder", "task", "tasks", "card", "cards", "board", "milestone", "milestones", "note", "notes", "inbox",
})


def _tokenize_words(text: str) -> List[str]:
    return [w.lower() for w in re.findall(r"[A-Za-z0-9_-]+", text or "")]


_COMPANION_ID_PREFIXES = (
    "wi_", "crd_", "prj_", "ms_", "ep_", "tb_", "sp_", "apr_", "bgj_", "sbx_", "rt_", "dagrun_", "edu_", "evt_", "cur_", "ctx_",
)
_COMPANION_SDK_SYMBOLS = frozenset({
    "dagspec", "dagnode", "triggerpolicy", "verificationspec", "goalplan", "goalstep", "memorystore", "skillforge",
})


def _filter_external_unknown_targets(clean_text: str, raw_unknowns: List[str]) -> Tuple[str, ...]:
    """Retain genuine unverified external system/component identifiers while ignoring Companion IDs, slugs, and code literals."""
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


@dataclass(frozen=True)
class ReadinessAssessment:
    """Deterministic `<5ms` blast-radius and epistemic grounding evaluation."""

    status: str
    reasons: Tuple[str, ...] = ()
    unknown_terms: Tuple[str, ...] = ()
    risk_tier: ActionRiskTier = ActionRiskTier.R0_READ_ONLY


@dataclass(frozen=True)
class TriageDecision:
    """Cognitive Triage decision unifying `route` (`TRIAGE_ROUTES`) with turn `mode` and tier metadata."""

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


def match_verified_dag(user_text: str, root: Optional[Path] = None) -> str:
    """Return a verified Python `DAGSpec` name when `user_text` explicitly requests running that DAG."""
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
        if name and (f"dag {name}" in clean or f"`{name}`" in clean or (len(name) >= 6 and (name in tokens or alt in clean) and "dag" in tokens)):
            pos = max(clean.rfind(name), clean.rfind(alt))
            if pos >= best_pos:
                best_pos, best_name = pos, str(d["name"])
    return best_name


def assess_turn_readiness(
    user_text: str,
    grounding: Optional[EpistemicGrounding] = None,
    store: Optional[Any] = None,
    root: Optional[Any] = None,
) -> ReadinessAssessment:
    """Evaluate `<5ms` blast-radius (`R0..R3`) and epistemic grounding before routing a turn."""
    clean = (user_text or "").strip()
    if not clean or clean.startswith(("/", ".")):
        return ReadinessAssessment("READY")

    eff_grounding = grounding or (build_epistemic_grounding(clean, store=store, root=root) if (store is not None or root is not None) else None)
    words = set(_tokenize_words(clean))
    reasons: List[str] = []
    has_mutation = bool(words & _MUTATION_VERBS)
    risk = ActionRiskTier.R1_LOCAL_REVERSIBLE if has_mutation else ActionRiskTier.R0_READ_ONLY

    high_blast = bool((words & _DESTRUCTIVE_VERBS) and (words & _PRODUCTION_NOUNS)) and "```" not in clean
    if high_blast:
        risk = ActionRiskTier.R3_HIGH_IMPACT_MUTATION
        reasons.append("high-impact stateful or external operation detected (`R3`)")

    low = clean.lower()
    if any(p in low for p in ("align with me first", "ask me questions first", "help me scope", "not sure how", "not sure which", "before we build")):
        reasons.append("explicit alignment or scoping request")

    unknowns = _filter_external_unknown_targets(clean, list(eff_grounding.unknown_terms)) if eff_grounding else ()
    if len(unknowns) >= 2 and has_mutation and not (words & _PERSONAL_ASSISTANT_NOUNS):
        reasons.append(f"multiple unverified external targets ({', '.join(unknowns[:3])})")

    return ReadinessAssessment(
        status="NEEDS_ALIGNMENT" if reasons else "READY",
        reasons=tuple(reasons),
        unknown_terms=unknowns,
        risk_tier=risk,
    )


def _resolve_active_media_tier() -> Optional[str]:
    uris = get_active_wire_images()
    if not uris:
        return None
    slot = "video" if any(u.startswith("data:video/") for u in uris) else ("audio" if any(u.startswith("data:audio/") for u in uris) else "image")
    return slot if settings.resolve_tier(slot).is_dedicated else "reasoning"


def _parse_llm_triage_json(raw: Dict[str, Any], valid_dags: List[str]) -> Optional[Dict[str, Any]]:
    """Validate structured JSON output from an optional fast Cognitive Triage callable."""
    route = str(raw.get("route") or "").strip().upper()
    if route not in TRIAGE_ROUTES:
        return None
    dag = str(raw.get("matched_dag") or "").strip()
    if route == "EXECUTE_VERIFIED_DAG" and dag not in valid_dags:
        return None
    tier = str(raw.get("tier") or "fast").strip().lower()
    if tier not in ("fast", "coding", "reasoning"):
        tier = "fast"
    return {
        "route": route,
        "tier": tier,
        "needs_blueprint": bool(raw.get("needs_blueprint", False)),
        "matched_dag": dag,
        "reason": str(raw.get("reason") or "structured cognitive triage")[:160],
    }


def _triage_via_llm(
    clean: str, readiness: ReadinessAssessment, risk_code: str, eff_root: Path,
    triage_llm_fn: Callable[[str, Dict[str, Any]], Dict[str, Any]],
) -> Optional[TriageDecision]:
    dags = [str(d["name"]) for d in discover_dags(root=eff_root)]
    ctx_payload = {"risk_tier": risk_code, "readiness": readiness.status, "reasons": list(readiness.reasons), "available_dags": dags}
    try:
        parsed = _parse_llm_triage_json(triage_llm_fn(clean, ctx_payload), dags)
    except Exception:
        return None
    if parsed is None:
        return None
    rt = parsed["route"]
    needs_bp = parsed["needs_blueprint"] or readiness.risk_tier == ActionRiskTier.R3_HIGH_IMPACT_MUTATION
    mode_map = {
        "DIRECT_TURN": "DEEP_CONSULT" if parsed["tier"] == "reasoning" else "CHAT_DIRECT",
        "EXECUTE_VERIFIED_DAG": "GUIDED_CHANGE",
        "GOAL_ALIGN_PLAN": "DEEP_CONSULT" if needs_bp else "AUTONOMOUS_BUILD",
        "ADVANCE_ACTIVE_GOAL": "GUIDED_CHANGE",
    }
    return TriageDecision(
        mode=mode_map[rt], suggested_tier="reasoning" if needs_bp else parsed["tier"],
        reason=parsed["reason"], readiness=readiness.status, ambiguity_notes=readiness.reasons,
        needs_blueprint=needs_bp and rt == "GOAL_ALIGN_PLAN", route=rt, matched_dag=parsed["matched_dag"], risk_tier=risk_code,
    )


def triage_turn(
    user_text: str, grounding: Optional[EpistemicGrounding] = None, store: Optional[Any] = None,
    root: Optional[Any] = None, triage_llm_fn: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
) -> TriageDecision:
    """Run the 2-stage Cognitive Triage Gate (`<5ms` grounding + optional JSON triage / structural classifier)."""
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
        return TriageDecision("GUIDED_CHANGE", "fast", f"matched verified Python DAG `{dag_hit}`", readiness.status, readiness.reasons, route="EXECUTE_VERIFIED_DAG", matched_dag=dag_hit, risk_tier=risk_code)

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
    if (is_question or (not has_mutation and bool(word_set & consult_cues))) and not multi_deliverable:
        is_deep = len(words_list) >= 14 or bool(word_set & consult_cues)
        return TriageDecision("DEEP_CONSULT" if is_deep else "CHAT_DIRECT", "reasoning" if is_deep else "fast", "analytical consultation" if is_deep else "direct question", readiness.status, readiness.reasons, route="DIRECT_TURN", risk_tier=risk_code)

    tier_hint = "reasoning" if readiness.status == "NEEDS_ALIGNMENT" else "fast"
    if has_mutation and multi_deliverable and len(words_list) >= 8:
        return TriageDecision("AUTONOMOUS_BUILD", tier_hint, "multi-deliverable objective routed to goal execution", readiness.status, readiness.reasons, route="GOAL_ALIGN_PLAN", risk_tier=risk_code)
    if has_mutation and len(words_list) >= 4:
        return TriageDecision("GUIDED_CHANGE", tier_hint, "scoped single-objective task", readiness.status, readiness.reasons, route="DIRECT_TURN", risk_tier=risk_code)
    return TriageDecision("CHAT_DIRECT", tier_hint, "conversational turn", readiness.status, readiness.reasons, route="DIRECT_TURN", risk_tier=risk_code)


def classify_turn_intent(
    user_text: str,
    grounding: Optional[EpistemicGrounding] = None,
    store: Optional[Any] = None,
    root: Optional[Any] = None,
) -> TriageDecision:
    """Classify turn intent via the Cognitive Triage Gate (`triage_turn`)."""
    return triage_turn(user_text, grounding=grounding, store=store, root=root)


def resolve_workflow_phase_tier(state: Optional[str]) -> str:
    """Return the mechanical model tier (`fast`, `coding`, or `reasoning`) for a given workflow state."""
    clean = (state or "").strip().upper()
    if clean == "IMPLEMENT" and settings.resolve_tier("coding").is_dedicated:
        return "coding"
    return PHASE_TIERS.get(clean, "fast")


def select_turn_step_tier(
    top_tier: Optional[str],
    active_wf_state: Optional[str],
    consecutive_errors: int,
    step: int = 0,
    intent_mode: str = "CHAT_DIRECT",
    prior_tier: Optional[str] = None,
    subagent_depth: int = 0,
    tiers_share_endpoint: Optional[bool] = None,
    suggested_tier: Optional[str] = None,
    allow_error_escalation: Optional[bool] = None,
) -> Optional[str]:
    """Lock one model tier per turn at Step 0 (preserving KV-cache), escalating mid-turn only on `>=2` consecutive errors."""
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


def run_http_smoke_probe(
    module_name: str = "surfaces.web_server",
    factory_name: str = "create_web_server",
    endpoints: Optional[List[str]] = None,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    """Spin up an ephemeral stdlib HTTP server on port 0, probe endpoints, and guarantee clean shutdown."""
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
        msg = f"HTTP smoke probe failed to initialize `{module_name}.{factory_name}`: {type(exc).__name__}: {exc}"
        return {"passed": False, "port": 0, "results": [], "summary": msg}

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
                req = urllib.request.Request(url, headers={"User-Agent": "companion-smoke-probe/3.5"})
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
