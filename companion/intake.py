"""
Readiness-Aware Intake, Uncompromised Model Tier Router & HTTP Smoke Probe
(`companion/intake.py` — A-Frame Application Layer).

Provides:
1. Domain-neutral `assess_turn_readiness` & `classify_turn_intent` (`CHAT_DIRECT`,
   `DEEP_CONSULT`, `GUIDED_CHANGE`, `AUTONOMOUS_BUILD`) without domain-specific hardcoding.
2. Uncompromised `select_turn_step_tier`: routes `DEEP_CONSULT`, `DESIGN`, `PLAN`, `RECOVER`,
   and `consecutive_errors >= 2` to `reasoning` on BOTH multi-endpoint and single-endpoint
   hardware, while keeping escalated `reasoning` sticky within a turn on shared endpoints
   to prevent mid-turn model-swap ping-ponging.
3. Ephemeral stdlib `run_http_smoke_probe` for clean zero-zombie endpoint verification.
"""

import importlib
import re
import threading
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from companion.config import settings
from companion.disciplines import ActionRiskTier, classify_action_risk
from companion.grounding import EpistemicGrounding, build_epistemic_grounding
from companion.streaming import get_active_wire_images
from companion.telemetry import telemetry

__all__ = [
    "IntentDecision",
    "PHASE_TIERS",
    "ReadinessAssessment",
    "assess_turn_readiness",
    "classify_turn_intent",
    "resolve_workflow_phase_tier",
    "run_http_smoke_probe",
    "select_turn_step_tier",
]

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

_CONSULT_CUE_RE = re.compile(
    r"\b(?:360\s+(?:architecture|review|audit)|architecture\s+review|design\s+review|compare\s+tradeoffs|"
    r"trade-?offs?\s+between|evaluate\s+the\s+core|root-?cause\s+analysis|strategic\s+options|"
    r"what\s+is\s+fundamentally\s+missing|pros\s+and\s+cons|design\s+a\s+strategy|how\s+should\s+we\s+architect|"
    r"evaluate\s+options|open-?ended\s+design)\b",
    re.I,
)
_CONSULT_GUARD_RE = re.compile(
    r"^\s*(?:can\s+you\s+review|before\s+we\s+do\s+that|what\s+do\s+you\s+think|explain\s+why|how\s+does|help\s+me\s+think)\b",
    re.I,
)
_BUILD_VERB_RE = re.compile(
    r"\b(?:build|create|implement|develop|wire\s+up|architect\s+and\s+build|upgrade\s+all|automate|assemble|author|scaffold)\b",
    re.I,
)
_MULTI_SCOPE_RE = re.compile(
    r"\b(?:end-to-end|full-?stack|web\s+(?:ui|front\s*-?\s*end|interface|dashboard|server)|"
    r"unit\s+tests?\s+and\s+specs?|all\s+\d+\s+upgrades|multi-?phase|rest\s+api|complete\s+application|"
    r"multi-?step\s+(?:pipeline|workflow|system)|with\s+verification\s+and\s+tests|reusable\s+skill\s+pack|"
    r"across\s+multiple\s+(?:files|modules)|from\s+scratch\s+with\s+tests|plus\s+automated\s+tests)\b",
    re.I,
)
_EXPLICIT_FILE_DELIVERABLE_RE = re.compile(
    r"\b[a-zA-Z0-9_./-]+\.(?:py|json|toml|md|ya?ml|html?|js|ts|css|sql)\b",
    re.I,
)
_GUIDED_EDIT_RE = re.compile(
    r"\b(?:edit|fix|patch|update|refactor|rename|configure|generate|validate|add\s+a\s+(?:helper|function|test|method|script|check))\b",
    re.I,
)
_AMBIGUITY_CUE_RE = re.compile(
    r"\b(?:not\s+sure\s+(?:how|what|which)|whatever\s+you\s+think|figure\s+out\s+what\s+we\s+need|"
    r"ask\s+me\s+questions\s+first|align\s+with\s+me\s+first|help\s+me\s+scope)\b",
    re.I,
)
_HIGH_BLAST_CUE_RE = re.compile(
    r"\b(?:production\s+cutover|migrate\s+live|push\s+to\s+remote|overwrite\s+external|delete\s+all\s+records|deploy\s+live)\b",
    re.I,
)
_PERSONAL_COACH_RE = re.compile(
    r"\b(?:flashcards?|quiz\s+me|tutor(?:\s+me)?|teach\s+me|study\s+(?:course|plan|guide|session|notes?)|"
    r"syllabus|spaced\s+repetition|active\s+recall|my\s+(?:sprint|week|calendar|backlog|board|chores?)|"
    r"weekly\s+sprint|slice\s+an?\s+unblocker|unblocker|procrastinat\w*|appointment)\b",
    re.I,
)
_CODE_DELIVERABLE_CUE_RE = re.compile(
    r"\b(?:unit\s+tests?|specs?|rest\s+api|web\s+(?:ui|front\s*-?\s*end|interface|dashboard|server)|full-?stack)\b",
    re.I,
)
_ARTIFACT_EXT_RE = re.compile(r"\.(?:py|json|toml|md|ya?ml|html?|js|ts|css|sql|sh|ps1|bat|txt|csv|ini|cfg|conf|xml)$", re.I)
_GENERIC_TECH_TERMS = frozenset({
    "adr", "api", "ast", "back-end", "cli", "command-line", "cross-project", "css", "csv", "dag",
    "domain-neutral", "end-to-end", "front-end", "full-stack", "html", "http", "https", "in-memory",
    "json", "key-value", "multi-phase", "multi-step", "open-ended", "read-only", "real-time", "rest",
    "sdd", "self-test", "single-file", "sql", "sqlite", "state-machine", "toml", "url", "utf", "utf-8",
    "xml", "yaml", "zero-dependency",
})


def _filter_external_unknown_targets(clean_text: str, raw_unknowns: List[str]) -> Tuple[str, ...]:
    """Retain genuine unverified system/component identifiers while ignoring new artifact filenames and plain prose."""
    first_word = clean_text.split()[0].strip(".,:;!?()[]{}\"'") if clean_text.split() else ""
    filtered: List[str] = []
    for tok in raw_unknowns:
        norm = tok.strip(".,:;!?()[]{}\"'")
        low = norm.lower().strip("./-")
        if not low or low in _GENERIC_TECH_TERMS or _ARTIFACT_EXT_RE.search(norm):
            continue
        has_internal_cap_or_digit = any(c.isupper() for c in norm[1:]) or any(c.isdigit() for c in norm)
        has_struct_sep = any(ch in norm for ch in ("_", "/", ".")) or ("-" in norm and low not in _GENERIC_TECH_TERMS)
        is_mid_sentence_proper = norm[0].isupper() and norm != first_word
        if (has_internal_cap_or_digit or has_struct_sep or is_mid_sentence_proper) and norm not in filtered:
            filtered.append(norm)
    return tuple(filtered)


@dataclass(frozen=True)
class ReadinessAssessment:
    """Pre-execution readiness & blast-radius evaluation for an incoming user objective."""

    status: str
    reasons: Tuple[str, ...] = ()
    unknown_terms: Tuple[str, ...] = ()
    risk_tier: ActionRiskTier = ActionRiskTier.R0_READ_ONLY


@dataclass(frozen=True)
class IntentDecision:
    """Step-0 deterministic turn intent and readiness classification result."""

    mode: str
    suggested_tier: str
    reason: str
    readiness: str = "READY"
    ambiguity_notes: Tuple[str, ...] = ()
    needs_blueprint: bool = False


def assess_turn_readiness(
    user_text: str,
    grounding: Optional[EpistemicGrounding] = None,
    store: Optional[Any] = None,
    root: Optional[Any] = None,
) -> ReadinessAssessment:
    """Evaluate whether a prompt is ready for direct execution or warrants alignment/reasoning first."""
    clean = (user_text or "").strip()
    if not clean or clean.startswith(("/", ".")):
        return ReadinessAssessment("READY")
    eff_grounding = grounding or (build_epistemic_grounding(clean, store=store, root=root) if (store is not None or root is not None) else None)
    reasons: List[str] = []
    risk = ActionRiskTier.R1_LOCAL_REVERSIBLE if (_BUILD_VERB_RE.search(clean) or _GUIDED_EDIT_RE.search(clean)) else ActionRiskTier.R0_READ_ONLY
    if _HIGH_BLAST_CUE_RE.search(clean) or classify_action_risk("run_shell", {"command": clean}) == ActionRiskTier.R3_HIGH_IMPACT_MUTATION:
        risk = ActionRiskTier.R3_HIGH_IMPACT_MUTATION
        reasons.append("high-impact external/stateful operation detected (`R3`)")
    if _AMBIGUITY_CUE_RE.search(clean):
        reasons.append("explicit alignment or scoping request detected")
    unknowns = _filter_external_unknown_targets(clean, list(eff_grounding.unknown_terms)) if eff_grounding else ()
    if len(unknowns) >= 2 and (_BUILD_VERB_RE.search(clean) or _GUIDED_EDIT_RE.search(clean)):
        reasons.append(f"multiple unverified domain targets ({', '.join(unknowns[:3])})")
    status = "NEEDS_ALIGNMENT" if reasons else "READY"
    return ReadinessAssessment(status=status, reasons=tuple(reasons), unknown_terms=unknowns, risk_tier=risk)


def _resolve_active_media_tier() -> Optional[str]:
    uris = get_active_wire_images()
    if not uris:
        return None
    slot = "video" if any(u.startswith("data:video/") for u in uris) else ("audio" if any(u.startswith("data:audio/") for u in uris) else "image")
    return slot if settings.resolve_tier(slot).is_dedicated else "reasoning"


def classify_turn_intent(
    user_text: str,
    grounding: Optional[EpistemicGrounding] = None,
    store: Optional[Any] = None,
    root: Optional[Any] = None,
) -> IntentDecision:
    """Classify an incoming user prompt into `CHAT_DIRECT`, `DEEP_CONSULT`, `GUIDED_CHANGE`, or `AUTONOMOUS_BUILD`."""
    clean = (user_text or "").strip()
    words = clean.split()
    if not clean or clean.startswith(("/", ".", "[AUTONOMOUS WORKFLOW PHASE:", "[ISOLATED SUBAGENT WORKER TASK]")):
        return IntentDecision("CHAT_DIRECT", "fast", "slash or internal system framing")
    m_tier = _resolve_active_media_tier()
    if m_tier:
        return IntentDecision("CHAT_DIRECT", m_tier, f"multimodal {m_tier} attachment turn")

    readiness = assess_turn_readiness(clean, grounding=grounding, store=store, root=root)
    has_consult = bool(_CONSULT_CUE_RE.search(clean))
    has_consult_guard = bool(_CONSULT_GUARD_RE.search(clean))
    has_build_verb = bool(_BUILD_VERB_RE.search(clean))
    has_action_verb = bool(has_build_verb or _GUIDED_EDIT_RE.search(clean))
    distinct_files = {m.lower() for m in _EXPLICIT_FILE_DELIVERABLE_RE.findall(clean)}
    has_multi_scope = bool(_MULTI_SCOPE_RE.search(clean) or (has_build_verb and len(distinct_files) >= 3))
    is_personal_coach = bool(_PERSONAL_COACH_RE.search(clean) and not distinct_files and not _CODE_DELIVERABLE_CUE_RE.search(clean))
    if is_personal_coach and readiness.risk_tier != ActionRiskTier.R3_HIGH_IMPACT_MUTATION:
        coach_tier = "reasoning" if (_AMBIGUITY_CUE_RE.search(clean) or has_consult) else "fast"
        return IntentDecision("GUIDED_CHANGE", coach_tier, "conversational agile, calendar, or education coaching turn", "READY", ())

    if readiness.status == "NEEDS_ALIGNMENT" and (_AMBIGUITY_CUE_RE.search(clean) or readiness.risk_tier == ActionRiskTier.R3_HIGH_IMPACT_MUTATION):
        return IntentDecision(
            "DEEP_CONSULT",
            "reasoning",
            "underspecified or high-impact goal requiring reasoning alignment",
            readiness.status,
            readiness.reasons,
            needs_blueprint=has_action_verb,
        )
    if has_consult and (has_consult_guard or not has_multi_scope):
        return IntentDecision("DEEP_CONSULT", "reasoning", "architectural review or multi-option tradeoff consultation", readiness.status, readiness.reasons)
    tier_hint = "reasoning" if readiness.status == "NEEDS_ALIGNMENT" else "fast"
    if has_build_verb and has_multi_scope and not has_consult_guard and (len(words) >= 8 or len(distinct_files) >= 3):
        return IntentDecision("AUTONOMOUS_BUILD", tier_hint, "multi-deliverable end-to-end objective", readiness.status, readiness.reasons)
    if has_action_verb and len(words) >= 4:
        return IntentDecision("GUIDED_CHANGE", tier_hint, "scoped modification, validation, or capability task", readiness.status, readiness.reasons)
    return IntentDecision("CHAT_DIRECT", tier_hint, "conversational turn or quick status query", readiness.status, readiness.reasons)


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
    """Select the right model tier for the cognitive job without downgrading `reasoning` on shared GPUs."""
    m_tier = _resolve_active_media_tier()
    if m_tier:
        return m_tier
    is_auto = not top_tier or top_tier.strip().lower() == "auto"
    can_escalate = is_auto if allow_error_escalation is None else bool(allow_error_escalation)
    shared = settings.tiers_share_endpoint("fast", "reasoning") if tiers_share_endpoint is None else bool(tiers_share_endpoint)
    if consecutive_errors >= 2 and can_escalate:
        if telemetry.active_trace_id:
            telemetry.log("INFO", "safety", f"Escalating step to `reasoning` tier after {consecutive_errors} consecutive tool errors")
        return "reasoning"
    if shared and step >= 1 and prior_tier == "reasoning" and can_escalate:
        return "reasoning"
    if subagent_depth == 0 and is_auto:
        if step == 0 and (intent_mode == "DEEP_CONSULT" or suggested_tier == "reasoning"):
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
    try:
        mod = importlib.import_module(module_name)
        factory = getattr(mod, factory_name)
        server, _ = factory(host="127.0.0.1", port=0)
    except Exception as exc:
        msg = f"HTTP smoke probe failed to initialize `{module_name}.{factory_name}`: {type(exc).__name__}: {exc}"
        return {"passed": False, "port": 0, "results": [], "summary": msg}

    port = int(server.server_address[1])
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    results: List[Dict[str, Any]] = []
    all_ok = True
    try:
        for ep in probe_paths:
            clean_ep = ep if ep.startswith("/") else f"/{ep}"
            url = f"http://127.0.0.1:{port}{clean_ep}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "companion-smoke-probe/2.3"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
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

    badge = "PASS" if all_ok else "FAIL"
    lines = [f"=== HTTP Smoke Probe (`{module_name}` @ 127.0.0.1:{port}): {badge} ==="]
    for r in results:
        err_note = f" ({r['error']})" if r.get("error") else ""
        lines.append(f"  [{'PASS' if r['passed'] else 'FAIL'}] GET {r['endpoint']} -> HTTP {r['status']} ({r['bytes']} bytes){err_note}")
    return {"passed": all_ok, "port": port, "results": results, "summary": "\n".join(lines)}
