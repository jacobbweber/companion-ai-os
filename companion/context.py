"""Dynamic System Prompt Builder, Multi-Project Workspace Manager & Context Facade (A-Frame Application Layer)."""

import json
import platform
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from companion.archive_and_approvals import get_trust_mode
from companion.compaction import (
    KeyTokens, build_lossless_phase_handoff, compact_messages_if_needed as _compact_messages_impl,
    estimate_messages_chars, estimate_messages_tokens, extract_key_tokens, format_context_budget_status,
    normalize_compaction_text, prune_stale_tool_outputs, verify_compaction_summary,
)
from companion.config import MODEL_TIERS, SELF_PROJECT_ID, settings, slugify_project_id
from companion.education_engine import format_education_hud
from companion.grounding import build_epistemic_grounding, build_workspace_radar, discover_skills
from companion.llm import format_model_tiers_status
from companion.mcp_hub import format_connected_partners_hud
from companion.memory_store import MemoryStore, get_memory_store
from companion.project_graph import format_project_graph_hud
from companion.workflow_graph import WorkflowEngine, workflow_engine

__all__ = [
    "DEFAULT_SOUL_AND_USER", "KeyTokens", "VALID_TONES", "bootstrap_identity", "build_lossless_phase_handoff",
    "build_system_prompt", "compact_messages_if_needed", "discover_skills", "estimate_messages_chars",
    "estimate_messages_tokens", "execute_identity_action", "extract_key_tokens", "format_context_budget_status",
    "format_identity_summary", "format_skills_catalog", "is_identity_bootstrapped", "manage_active_project",
    "manage_companion_tone", "manage_model_tier", "normalize_compaction_text", "prune_stale_tool_outputs",
    "run_first_boot_onboarding", "sync_identity_md", "verify_compaction_summary",
]

DEFAULT_SOUL_AND_USER: Dict[str, str] = {
    "companion_name": "Companion",
    "companion_soul": "Warm, grounded, loyal thinking partner and autonomous lead engineer who owns technical complexity and communicates in plain, honest English.",
    "user_name": "Partner",
    "user_profile": "Thoughtful human partner who values clear explanations, well-reasoned recommendations, and reliable outcomes.",
}
VALID_TONES: Dict[str, str] = {
    "concise": "Terse, bullet-first executive answers with zero fluff.",
    "balanced": "Warm, plain-English explanations with concise action summaries.",
    "elaborate": "Detailed walkthroughs with architectural reasoning, tradeoffs, and examples.",
}


def _resolve_tone(prof: Dict[str, str]) -> str:
    t = prof.get("companion_tone", "balanced").strip().lower()
    return t if t in VALID_TONES else "balanced"


def manage_companion_tone(tone: Optional[str] = None, store: Optional[MemoryStore] = None) -> str:
    """Inspect or set Companion's communication tone (`concise`, `balanced`, or `elaborate`)."""
    mem, req = (store or get_memory_store()), (tone or "").strip().lower()
    if req:
        if req not in VALID_TONES:
            return f"Invalid tone '{tone}'. Valid options: {', '.join(VALID_TONES)}."
        mem.set_user_profile("companion_tone", req)
        sync_identity_md(store=mem)
        mem.sync_memory_md()
    curr = _resolve_tone(mem.get_user_profile())
    return f"Communication Tone: `{curr}` — {VALID_TONES[curr]} (Options: `/tone concise|balanced|elaborate`)"


def manage_model_tier(tier_or_action: Optional[str] = None, store: Optional[MemoryStore] = None) -> str:
    """Inspect, ping (`check`), or switch Companion's active model tier (`auto`, `fast`, `coding`, `reasoning`, `audio`, `image`, `video`)."""
    mem, req = (store or get_memory_store()), (tier_or_action or "").strip().lower()
    curr = mem.get_user_profile().get("active_model_tier", "auto").strip().lower() or "auto"
    if req in ("", "status", "check"):
        return format_model_tiers_status(active_mode=curr, check_health=(req == "check"))
    valid_modes = ("auto", *MODEL_TIERS)
    if req not in valid_modes:
        return f"Invalid model tier '{tier_or_action}'. Valid options: {', '.join(valid_modes)}, status, check."
    mem.set_user_profile("active_model_tier", req)
    mem.sync_memory_md()
    return f"Active model routing mode set to `{req}`.\n\n" + format_model_tiers_status(active_mode=req)


def _load_known_projects(mem: MemoryStore) -> Dict[str, str]:
    raw = mem.get_user_profile().get("known_projects_json", "{}")
    try:
        parsed = json.loads(raw) if raw else {}
        return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _persist_workspace_selection(mem: MemoryStore, pid: str, proot: Path, known: Dict[str, str]) -> None:
    if pid != SELF_PROJECT_ID:
        known[pid] = str(proot)
    mem.set_user_profile("active_project_id", pid)
    mem.set_user_profile("active_project_path", str(proot))
    mem.set_user_profile("known_projects_json", json.dumps(known, sort_keys=True))
    mem.sync_memory_md()


def _resolve_external_target(raw_target: str, known: Dict[str, str]) -> Path:
    clean, slug = raw_target.strip().strip("'\""), slugify_project_id(raw_target.strip().strip("'\""))
    if clean in known:
        return Path(known[clean]).resolve()
    if slug in known and "/" not in clean and "\\" not in clean and ":" not in clean:
        return Path(known[slug]).resolve()
    cand = Path(clean)
    return cand.resolve() if cand.is_absolute() else (settings.root.parent / clean).resolve()


def manage_active_project(arg: Optional[str] = None, store: Optional[MemoryStore] = None) -> str:
    """Manage multi-project `WorkspaceContext` (`status`, `list`, `self`, `init <name|path>`, `switch <name|path>`)."""
    mem = store or get_memory_store()
    known, raw = _load_known_projects(mem), (arg or "").strip()
    parts = raw.split(None, 1)
    verb, rest = (parts[0].lower() if parts else "status"), (parts[1].strip() if len(parts) > 1 else "")

    if verb in ("status", "current", ""):
        ws, sd_on = settings.get_workspace(), mem.is_self_dev_mode_enabled()
        lock = (("UNLOCKED (Self-Dev Mode ON — Core Self-Evolution Active)" if sd_on else "READ-ONLY LOCKED (Self-Dev Mode OFF per ADR-019 — `data/` writable; enable via `/selfdev on`)") if ws.is_self_project else f"READ-ONLY LOCKED (`{ws.self_root}` protected per ADR-007)")
        return f"=== Active WorkspaceContext ===\nProject ID    : `{ws.project_id}`\nProject Root  : {ws.project_root}\nCompanion Core: {lock}\n(Use `/project list`, `/project init <name|path>`, `/project switch <name|path>`, `/project self`, or `/selfdev on|off`.)"
    if verb == "list":
        ws = settings.get_workspace()
        lines = ["=== Registered Workspaces ===", f"  * `{SELF_PROJECT_ID}` -> {settings.root.resolve()}{' [ACTIVE]' if ws.is_self_project else ''}"]
        for k, v in sorted(known.items()):
            lines.append(f"  * `{k}` -> {v}{' [ACTIVE]' if (not ws.is_self_project and ws.project_id == k) else ''}")
        return "\n".join(lines)
    if verb in ("self", SELF_PROJECT_ID, "reset", "home"):
        ws = settings.reset_workspace()
        _persist_workspace_selection(mem, ws.project_id, ws.project_root, known)
        core_state = "Companion core unlocked (`Self-Dev Mode = ON`)." if mem.is_self_dev_mode_enabled() else "Companion core remains READ-ONLY (`Self-Dev Mode = OFF`; `data/` writable)."
        return f"Switched active WorkspaceContext back to `{ws.project_id}` ({ws.project_root}). {core_state}"
    if verb in ("init", "create", "new"):
        if not rest:
            return "error: project name or path is required for `init` (e.g., `/project init my-app`)."
        target = _resolve_external_target(rest, known)
        if target != settings.root.resolve() and target.is_relative_to(settings.root.resolve()):
            return f"error: external project `{target}` must live outside Companion core (`{settings.root.resolve()}`)."
        target.mkdir(parents=True, exist_ok=True)
        (target / "tests").mkdir(exist_ok=True)
        readme = target / "README.md"
        if not readme.exists():
            readme.write_text(f"# {target.name}\n\nManaged by Companion AI OS (`ADR-007` Workspace Isolation).\n", encoding="utf-8")
        ws = settings.set_workspace(target)
        _persist_workspace_selection(mem, ws.project_id, ws.project_root, known)
        return f"Initialized & activated project `{ws.project_id}` at `{ws.project_root}` (`{ws.self_root}` locked READ-ONLY)."

    target_str = rest if verb == "switch" else raw
    if not target_str:
        return "error: target project name or path is required for `switch`."
    if target_str.lower() in ("self", SELF_PROJECT_ID):
        return manage_active_project("self", store=mem)
    target = _resolve_external_target(target_str, known)
    if target != settings.root.resolve() and target.is_relative_to(settings.root.resolve()):
        return f"error: external project `{target}` must live outside Companion core (`{settings.root.resolve()}`)."
    if not target.is_dir():
        return f"error: project directory not found: `{target}` (use `/project init {target_str}` to create it)."
    ws = settings.set_workspace(target)
    _persist_workspace_selection(mem, ws.project_id, ws.project_root, known)
    return f"Switched active WorkspaceContext to `{ws.project_id}` at `{ws.project_root}` (`{ws.self_root}` locked READ-ONLY)."


def sync_identity_md(store: Optional[MemoryStore] = None, path: Optional[Path] = None) -> Path:
    """Write the compact Soul & User Identity grounding card to IDENTITY.md."""
    mem = store or get_memory_store()
    prof = mem.get_user_profile()
    c_name = prof.get("companion_name") or settings.companion_name or DEFAULT_SOUL_AND_USER["companion_name"]
    c_soul = prof.get("companion_soul") or settings.companion_soul or DEFAULT_SOUL_AND_USER["companion_soul"]
    c_tone = _resolve_tone(prof)
    u_name = prof.get("user_name") or settings.user_name or DEFAULT_SOUL_AND_USER["user_name"]
    u_prof = prof.get("user_profile") or settings.user_profile or DEFAULT_SOUL_AND_USER["user_profile"]
    default_id = settings.identity_md_path if mem.db_path.resolve() == settings.db_path.resolve() else (mem.db_path.parent / "IDENTITY.md")
    out_path = Path(path or default_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Companion AI OS — Soul & User Identity (`IDENTITY.md`)\n\n## 1. Companion Soul\n"
        f"- **Name**: {c_name}\n- **Soul & Ethos**: {c_soul}\n- **Communication Tone**: {c_tone} ({VALID_TONES[c_tone]})\n\n"
        f"## 2. User Identity\n- **Name**: {u_name}\n- **Profile & Style**: {u_prof}\n"
    )
    out_path.write_text(content, encoding="utf-8", newline="\n")
    return out_path


def format_identity_summary(store: Optional[MemoryStore] = None) -> str:
    """Return a concise summary of the Companion Soul and User Identity grounding cards."""
    mem = store or get_memory_store()
    prof = mem.get_user_profile()
    c_name, c_soul = prof.get("companion_name") or DEFAULT_SOUL_AND_USER["companion_name"], prof.get("companion_soul") or DEFAULT_SOUL_AND_USER["companion_soul"]
    c_tone, onboarded = _resolve_tone(prof), prof.get("onboarded", "false").lower() in ("true", "1", "yes")
    u_name, u_prof = prof.get("user_name") or DEFAULT_SOUL_AND_USER["user_name"], prof.get("user_profile") or DEFAULT_SOUL_AND_USER["user_profile"]
    return (
        f"=== Companion Soul & User Identity (Bootstrapped: {'YES' if onboarded else 'PENDING FIRST BOOT'}) ===\n"
        f"Companion Name : {c_name}\nCompanion Soul : {c_soul}\nActive Tone    : {c_tone} ({VALID_TONES[c_tone]})\n"
        f"User Name      : {u_name}\nUser Profile   : {u_prof}\n"
        "(Run `/onboard`, `/tone [concise|balanced|elaborate]`, or set COMPANION_NAME in `.env` to customize.)"
    )


def bootstrap_identity(companion_name: str = "", companion_soul: str = "", user_name: str = "", user_profile: str = "", store: Optional[MemoryStore] = None) -> str:
    """Persist compact Companion Soul and User Identity cards (capped to prevent context bloat)."""
    mem = store or get_memory_store()
    prof = mem.get_user_profile()
    c_name = (companion_name.strip() or prof.get("companion_name") or DEFAULT_SOUL_AND_USER["companion_name"])[:60]
    c_soul = (companion_soul.strip() or prof.get("companion_soul") or DEFAULT_SOUL_AND_USER["companion_soul"])[:300]
    u_name = (user_name.strip() or prof.get("user_name") or DEFAULT_SOUL_AND_USER["user_name"])[:60]
    u_prof = (user_profile.strip() or prof.get("user_profile") or DEFAULT_SOUL_AND_USER["user_profile"])[:300]
    for k, v in (("companion_name", c_name), ("companion_soul", c_soul), ("user_name", u_name), ("user_profile", u_prof), ("onboarded", "true")):
        mem.set_user_profile(k, v)
    sync_identity_md(store=mem)
    mem.sync_memory_md()
    return format_identity_summary(store=mem)


def is_identity_bootstrapped(store: Optional[MemoryStore] = None) -> bool:
    """Check if first-boot identity bootstrap is complete (or auto-bootstrap from .env once if unbootstrapped)."""
    mem = store or get_memory_store()
    prof = mem.get_user_profile()
    onboarded = prof.get("onboarded", "false").strip().lower() in ("true", "1", "yes")
    if not onboarded and any((settings.companion_name, settings.companion_soul, settings.user_name, settings.user_profile)):
        bootstrap_identity(
            prof.get("companion_name") or settings.companion_name,
            prof.get("companion_soul") or settings.companion_soul,
            prof.get("user_name") or settings.user_name,
            prof.get("user_profile") or settings.user_profile,
            store=mem,
        )
        return True
    default_id = settings.identity_md_path if mem.db_path.resolve() == settings.db_path.resolve() else (mem.db_path.parent / "IDENTITY.md")
    if not default_id.is_file():
        sync_identity_md(store=mem, path=default_id)
    return onboarded


def _build_identity_snapshot(mem: MemoryStore, summary: Optional[str] = None) -> Dict[str, Any]:
    prof, ws = mem.get_user_profile(), settings.get_workspace()
    tone, onboarded = _resolve_tone(prof), prof.get("onboarded", "false").strip().lower() in ("true", "1", "yes")
    return {
        "ok": True, "onboarded": onboarded, "companion_tone": tone, "valid_tones": VALID_TONES,
        "companion_name": prof.get("companion_name") or DEFAULT_SOUL_AND_USER["companion_name"],
        "companion_soul": prof.get("companion_soul") or DEFAULT_SOUL_AND_USER["companion_soul"],
        "user_name": prof.get("user_name") or DEFAULT_SOUL_AND_USER["user_name"],
        "user_profile": prof.get("user_profile") or DEFAULT_SOUL_AND_USER["user_profile"],
        "role": prof.get("role") or "", "communication_style": prof.get("communication_style") or "",
        "workspace": {"project_id": ws.project_id, "project_root": str(ws.project_root), "is_self_project": ws.is_self_project, "self_root": str(ws.self_root), "known_projects": _load_known_projects(mem)},
        "paths": {"data_dir": str(settings.effective_data_dir), "db_path": str(mem.db_path), "identity_md_path": str(settings.identity_md_path), "memory_md_path": str(settings.memory_md_path), "user_skills_dir": str(settings.user_skills_dir)},
        "summary": summary or format_identity_summary(store=mem),
    }


def execute_identity_action(params: Optional[Dict[str, Any]] = None, actor: str = "user", store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """Execute Shared Core Action `identity` (`get`, `update`, `project_self`, `project_switch`, `project_init`)."""
    p, mem = dict(params or {}), (store or get_memory_store())
    sub = str(p.get("subaction") or p.get("action") or "get").strip().lower()
    if sub in ("get", "status"):
        is_identity_bootstrapped(store=mem)
        return _build_identity_snapshot(mem)
    if actor.strip().lower() not in ("user", "cli", "web"):
        return {"ok": False, "error": f"SECURITY GUARD: `identity.{sub}` requires direct user/CLI/Web authorization."}
    if sub in ("update", "save", "bootstrap"):
        tone_in = str(p.get("companion_tone") or "").strip().lower()
        if tone_in and tone_in not in VALID_TONES:
            return {"ok": False, "error": f"Invalid tone `{tone_in}`. Valid: {', '.join(VALID_TONES)}."}
        if tone_in:
            mem.set_user_profile("companion_tone", tone_in)
        for field in ("role", "communication_style"):
            if field in p and str(p.get(field) or "").strip():
                mem.set_user_profile(field, str(p[field]).strip()[:300])
        bootstrap_identity(str(p.get("companion_name") or ""), str(p.get("companion_soul") or ""), str(p.get("user_name") or ""), str(p.get("user_profile") or ""), store=mem)
        snap = _build_identity_snapshot(mem)
        snap["summary"] = f"Saved Identity & Profile (`{snap['companion_name']}` <-> `{snap['user_name']}`, tone=`{snap['companion_tone']}`)."
        return snap
    if sub in ("project_self", "project_switch", "project_init"):
        target = str(p.get("target") or p.get("path") or "").strip()
        cmd = "self" if sub == "project_self" else f"{'init' if sub == 'project_init' else 'switch'} {target}".strip()
        msg = manage_active_project(cmd, store=mem)
        return {"ok": False, "error": msg} if msg.lower().startswith("error:") else _build_identity_snapshot(mem, summary=msg)
    return {"ok": False, "error": f"Unknown `identity` subaction `{sub}`."}


def run_first_boot_onboarding(store: Optional[MemoryStore] = None, input_fn: Any = input) -> str:
    """Interactive 4-question first-boot wizard to ground Companion's Soul and User Identity."""
    mem = store or get_memory_store()
    if input_fn is input and (sys.stdin is None or not sys.stdin.isatty()):
        return format_identity_summary(store=mem) + "\n\n(Interactive `/onboard` requires a terminal TTY. Run `python harness.py` in a terminal, set `COMPANION_NAME`/`COMPANION_USER_NAME` in `.env`, or use `/tone`.)"
    prof = mem.get_user_profile()
    d_cname = prof.get("companion_name") or DEFAULT_SOUL_AND_USER["companion_name"]
    d_csoul = prof.get("companion_soul") or DEFAULT_SOUL_AND_USER["companion_soul"]
    d_uname = prof.get("user_name") or DEFAULT_SOUL_AND_USER["user_name"]
    d_uprof = prof.get("user_profile") or DEFAULT_SOUL_AND_USER["user_profile"]
    print("\n=== Welcome! First-Boot Soul & User Identity Setup ===\nLet's take 30 seconds to orient your Companion (press Enter on any question to keep the default).\n")
    c_name = input_fn(f"1. Companion's Name [{d_cname}]: ").strip() or d_cname
    c_soul = input_fn(f"2. Companion's Soul / Personality (1-2 short sentences)\n   [{d_csoul}]: ").strip() or d_csoul
    u_name = input_fn(f"3. Your Name [{d_uname}]: ").strip() or d_uname
    u_prof = input_fn(f"4. Your Profile & Communication Preference (1-2 short sentences)\n   [{d_uprof}]: ").strip() or d_uprof
    summary = bootstrap_identity(c_name, c_soul, u_name, u_prof, store=mem)
    print("\nSetup complete! Grounding saved to SQLite and `IDENTITY.md`.\n")
    return summary


def format_skills_catalog(skill_name: Optional[str] = None, root: Optional[Path] = None) -> str:
    """Format the Tier-1 skills registry or return the full Tier-2 SKILL.md runbook + Tier-3 resource map."""
    target_root = (root or settings.root).resolve()
    skills = discover_skills(target_root)
    if skill_name and skill_name.strip():
        query = skill_name.strip().lower()
        for s in skills:
            if s["name"].lower() == query or query in s["name"].lower():
                cand_p = Path(s["path"])
                full_p = cand_p if cand_p.is_absolute() else (target_root / cand_p)
                if full_p.is_file():
                    res_lines = [f"- `{k}`: {s[k]}" for k in ("references", "dags", "scripts") if s.get(k)]
                    res_block = ("\n## Bundled Tier-3 Resources\n" + "\n".join(res_lines) + "\n") if res_lines else ""
                    return f"# Skill Runbook: `{s['name']}` ({s['path']})\n\n" + full_p.read_text(encoding="utf-8", errors="replace") + res_block
        return f"Skill '{skill_name}' not found. Available skills: {', '.join(s['name'] for s in skills)}"

    lines = [
        f"=== Companion Progressive-Disclosure Skill Registry ({len(skills)} Runbooks in `skills/`) ===",
        "Architecture: Tools (`companion/tools.py`) are L1 atomic primitives; Skills (`skills/<name>/`) are L2/L3 3-Tier Capability Packs (`SKILL.md`, `references/`, `dags/`, `scripts/`).",
        "",
    ]
    for s in skills:
        req = f"  [Tools: {s['required_tools']}]" if s.get("required_tools") else ""
        tier3 = [f"{k}=[{s[k]}]" for k in ("references", "dags", "scripts") if s.get(k)]
        t3_str = f"\n    Tier-3 Resources: {' | '.join(tier3)}" if tier3 else ""
        lines.append(f"  * `{s['name']}` (`{s['path']}`)\n    {s['description']}{req}{t3_str}")
    lines.append("\n(Use `/skills <name>` or `read_file` on a skill's `SKILL.md` path to view its full procedural runbook.)")
    return "\n".join(lines)


def _format_identity_hud_block(prof: Dict[str, str]) -> str:
    """Format the top `[COMPANION SOUL & USER IDENTITY]` section of the System Prompt."""
    c_name = prof.get("companion_name") or DEFAULT_SOUL_AND_USER["companion_name"]
    c_soul = prof.get("companion_soul") or DEFAULT_SOUL_AND_USER["companion_soul"]
    c_tone = _resolve_tone(prof)
    u_name = prof.get("user_name") or DEFAULT_SOUL_AND_USER["user_name"]
    u_prof = prof.get("user_profile") or DEFAULT_SOUL_AND_USER["user_profile"]
    return (
        f"[COMPANION SOUL & USER IDENTITY]\n"
        f"- Your Name : {c_name}\n"
        f"- Your Soul : {c_soul}\n"
        f"- Active Tone: {c_tone} ({VALID_TONES[c_tone]})\n"
        f"- User Name : {u_name}\n"
        f"- User Style: {u_prof}"
    )


def _format_host_runtime_hud() -> str:
    """Return dynamic stdlib host/OS/arch/Python runtime line for `LIVE SYSTEM HUD`."""
    return f"host=`{socket.gethostname()}` | os=`{platform.system()} {platform.release()} ({platform.machine()})` | python=`{platform.python_version()}`"


def build_system_prompt(store: Optional[MemoryStore] = None, wf_engine: Optional[WorkflowEngine] = None, user_query: str = "") -> str:
    """Construct the live, state-aware System Prompt with WorkspaceContext, Epistemic Grounding, and Workspace Radar."""
    mem, wf = (store or get_memory_store()), (wf_engine or workflow_engine)
    is_identity_bootstrapped(store=mem)
    default_md = settings.memory_md_path if mem.db_path.resolve() == settings.db_path.resolve() else (mem.db_path.parent / "MEMORY.md")
    if not default_md.is_file():
        mem.sync_memory_md(target_path=default_md)
    prof, ws, self_dev_on = mem.get_user_profile(), settings.get_workspace(), mem.is_self_dev_mode_enabled()
    identity_text, host_hud = _format_identity_hud_block(prof), _format_host_runtime_hud()
    ws_lock = (
        ("Self-Evolution Mode (`companion-self` | `Self-Dev Mode = ON` — edit with SDD & `system_doctor` discipline)" if self_dev_on else "DAILY-DRIVER STABLE MODE (`companion-self` | `Self-Dev Mode = OFF` per ADR-019 — core repo is MECHANICALLY READ-ONLY; `data/` & external `/project` workspaces are 100% writable; only human `/selfdev on` or Web System Studio can unlock)")
        if ws.is_self_project
        else f"EXTERNAL PROJECT MODE (`{ws.self_root}` is MECHANICALLY LOCKED READ-ONLY per ADR-007)"
    )
    repo_radar, hud, bg_on = build_workspace_radar(ws.project_root), wf.render_hud(), mem.is_background_autonomy_enabled()
    self_dev_lock = f" [{settings.self_dev_lock_reason()}]" if settings.is_self_dev_locked_by_env() else ""
    trust_mode, partners_hud = get_trust_mode(store=mem), format_connected_partners_hud(store=mem)
    partners_section = f"\n{partners_hud}\n" if partners_hud else ""
    agile_hud, edu_hud = format_project_graph_hud(store=mem), format_education_hud(store=mem)
    active_mode = prof.get("active_model_tier", "auto").strip().lower() or "auto"
    fast_cfg, code_cfg, reas_cfg = settings.resolve_tier("fast"), settings.resolve_tier("coding"), settings.resolve_tier("reasoning")
    code_hud = f" | coding=`{code_cfg.model}` (temp={code_cfg.temperature})" if code_cfg.is_dedicated else ""
    pending_tasks, backlog_items = mem.get_pending_tasks()[:6], mem.get_backlog(status="pending")[:6]
    notes, skills = mem.get_system_notes(limit=5), discover_skills(settings.root)
    grounding = build_epistemic_grounding(user_query, store=mem, root=ws.project_root, skills=skills, top_k=6)
    shown_skills = grounding.top_skills if grounding.top_skills else skills[:6]
    extra_skills = [s["name"] for s in skills if s["name"] not in {sk["name"] for sk in shown_skills}]

    tasks_str = "\n".join(f"  - Task #{tid} [P{pr} | {st}]: {desc}" for tid, desc, st, pr in pending_tasks) if pending_tasks else "  - (No pending immediate tasks)"
    backlog_str = "\n".join(f"  - Backlog #{bid} [{cat}]: {desc}" for bid, desc, cat, _ in backlog_items) if backlog_items else "  - (No pending backlog items)"
    notes_str = "\n".join(f"  - [{cat}] {n}" for n, cat, _ in notes) if notes else "  - Always use `run_python` for executing Python snippets on Windows."
    skills_str = "\n".join(f"  - `{s['name']}` (`{s['path']}`): {s['description']}" for s in shown_skills) if shown_skills else "  - (No skills found in `skills/`)"
    if extra_skills:
        skills_str += f"\n  - Additional available skills ({len(extra_skills)}): {', '.join(f'`{n}`' for n in extra_skills[:18])}"

    return f"""{identity_text}

===========================================================================
LIVE SYSTEM HUD (Auto-Refreshed from SQLite State Store)
===========================================================================
Host Runtime   : {host_hud}
Active Project : `{ws.project_id}` -> {ws.project_root}
Workspace Guard: {ws_lock}
Companion Core : {settings.root} (Entry: `harness.py` | Specs: `specs/` | Skills: `skills/` | Data: `{settings.effective_data_dir}` | Wiki: `{settings.wiki_dir}`)
Memory & DB    : {settings.memory_md_path} | {mem.db_path}
Trust Mode     : `{trust_mode}` (Blind Vault Zero-Reveal Active | Approvals Queue: `/approvals`)
Model Routing  : mode=`{active_mode}` | privacy=`{settings.effective_privacy_mode()}` | fast=`{fast_cfg.model}` (temp={fast_cfg.temperature}){code_hud} | reasoning=`{reas_cfg.model}` (temp={reas_cfg.temperature})
Operating Modes: Self-Dev=`{'ON (Core Editable)' if self_dev_on else 'OFF (Core Read-Only)'}`{self_dev_lock} | Background Autonomy=`{'ON (Personal+Self-Dev Idle)' if (bg_on and self_dev_on) else ('ON (Personal & Goal Slices Idle)' if bg_on else 'OFF')}`
{partners_section}
[MULTI-TIER MODEL ROUTING & OODA LOOP (ALWAYS-ON RULES)]
1. Organic Deep Thinking (`tier="reasoning"`): When the User asks a strategic, architectural, or multi-option tradeoff question, call `workflow_manage(action="subagent", tier="reasoning", skill="deep-thinking", goal="...")` in 1 hop.
2. OODA Troubleshooting (`Observe -> Orient -> Decide -> Act`): When diagnosing bugs, test failures, or system issues, follow `skills/ooda-troubleshooting/SKILL.md` or run `workflow_manage(action="run_dag", dag="ooda-diagnose")`.
3. Second Opinion When Stuck: Any time 2 consecutive tool calls or verification checks fail, consult `tier="reasoning"` before making another edit.

[STATE MACHINE WORKFLOW STATUS]
{hud}

[ACTIVE TASKS]
{tasks_str}

[TOP PENDING BACKLOG]
{backlog_str}

{agile_hud}

{edu_hud}

[RECENT SYSTEM NOTES & LESSONS]
{notes_str}
{grounding.prompt_block}
[CODEBASE AST & LINE-BUDGET RADAR (`{ws.project_id}`)]
{repo_radar}

[AVAILABLE SKILLS — PROGRESSIVE DISCLOSURE RUNBOOKS (L2)]
Tools (`companion/tools.py`) are L1 atomic execution primitives; Skills (`skills/<name>/SKILL.md`) are L2 declarative runbooks:
{skills_str}

[EXECUTION REMINDERS]
- Partnership Rule: Never ask the User to run internal tools or write code — execute tools autonomously and communicate in warm, plain executive English.
- Surgical Reads & Inline Guard: Use `read_file(path="...", symbol="ClassName.method")` to inspect exact AST definitions without wasting context. `write_file`/`edit_file` run an instant micro-linter (blocking `>700L` modules, `>100L` functions, upward A-Frame imports, and external packages).
- Practice SDD: keep `specs/PRODUCT.md`, `specs/ARCHITECTURE.md`, `specs/CONTRACTS.md`, and `specs/DECISIONS.md` aligned with Companion core changes (`sdd_manage` / `system_doctor`).
- Switch or initialize isolated external projects via `project_manage(action="init"|"switch"|"self"|"list")` or `/project`.
- Wiki Warehouse notes (`01_inbox/`, `02_knowledge/`, `03_resources/`, `04_archive/`) live in the Wiki Warehouse, not the repo root — always use `wiki_manage` directly (never `list_dir`/`read_file` on `01_inbox` or `02_knowledge`). Stage rough captures into `01_inbox/` first; when curating/graduating to `02_knowledge/`, prioritize deliberate multi-turn quality (`inspect_graduation` -> `read` every `must_read` overlap -> pick one of the 11 governed templates & absorb into sections -> `validate_curation` -> `commit_curation`).
- On Windows, ALWAYS use `run_python` to run Python code (never use bash `<< EOF` in `run_shell`).
"""


def compact_messages_if_needed(
    messages: List[Dict[str, Any]],
    max_chars: Optional[int] = None,
    keep_recent: Optional[int] = None,
    store: Optional[MemoryStore] = None,
    force: bool = False,
    last_usage_tokens: int = 0,
    summarizer_fn: Optional[Any] = None,
    tier: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Adaptive 2-stage compaction with live `build_system_prompt` fallback."""
    mem = store or get_memory_store()
    return _compact_messages_impl(
        messages=messages,
        max_chars=max_chars,
        keep_recent=keep_recent,
        store=mem,
        force=force,
        last_usage_tokens=last_usage_tokens,
        summarizer_fn=summarizer_fn,
        tier=tier,
        system_prompt_fn=lambda: build_system_prompt(store=mem),
    )
