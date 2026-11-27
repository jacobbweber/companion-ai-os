"""
Isolated Git Worktrees, Secret-Shielded SDLC Operations, Preview Servers, Parallel Subagents & PR Delivery
(`companion/worktrees.py`).

Consolidates `sdlc_tools.py`, `worktree_runner.py`, and Blind-Vault GitHub PR REST helpers into a single
cohesive module with zero reverse imports into `skills/`:
1. `discover_repo_sdlc_context` & `probe_repo_workspace`: stack & `.agents/rules` discovery.
2. `tool_git_manage`: local branches, parallel `git worktree` checkouts, secret/DB staging shield,
   protected-branch & `--force` blocks, `delivery_policy` enforcement, and Blind-Vault `GIT_SSH_COMMAND`/`GIT_ASKPASS`.
3. `PreviewProcessManager` (`tool_dev_preview`): free-port allocation, secret-scrubbed temp env/DB,
   OS process-group lifecycle, loopback HTTP probes, base64 vision critique, and guaranteed teardown.
4. `upsert_pull_request`, `inspect_check_runs`, `merge_pull_request`: pure-stdlib Blind-Vault GitHub REST client.
5. `dispatch_work_item_agent`, `dispatch_parallel_ready_items`, `merge_worktree_into_base`,
   `push_worktree_branch_and_pr`, `request_worktree_changes`, `discard_worktree`, `run_parallel_subagents`.
"""

import atexit
import base64
import collections
import contextlib
import contextvars
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Tuple

from companion.approvals import get_approval_queue
from companion.attachments import ingest_attachment, inspect_image_bytes
from companion.config import is_in_agent_tool_turn, settings, workspace_context
from companion.jobs import BackgroundJobManager, get_job_manager
from companion.llm import chat, normalize_assistant_message, tier_context
from companion.memory_store import MemoryStore, get_memory_store, memory_store_context
from companion.projects import get_project_graph
from companion.streaming import (
    CancelScope,
    cancel_scope_context,
    check_cancelled,
    get_active_cancel_scope,
    get_active_stream_sink,
    wire_attachment_context,
)
from companion.tools import bool_arg, check_command_safety, check_path_safety, int_arg, str_arg
from companion.vault import VaultManager, _lock_windows_owner_dacl, get_vault_manager

__all__ = [
    "PreviewInstance",
    "PreviewProcessManager",
    "SDLC_SLASH_MAP",
    "WORKTREE_CODE_TOOLS",
    "_current_branch",
    "_run_git_cmd",
    "check_git_stage_safety",
    "collect_worktree_deliverable_receipt",
    "discard_worktree",
    "discover_repo_sdlc_context",
    "dispatch_parallel_ready_items",
    "dispatch_work_item_agent",
    "execute_agile_worktree_subaction",
    "format_parallel_subagents_tool",
    "init_greenfield_repo",
    "inspect_check_runs",
    "inspect_sdlc_repo_state",
    "is_ephemeral_cache_path",
    "merge_pull_request",
    "merge_worktree_into_base",
    "parse_unified_git_diff",
    "preview_manager",
    "preview_turn_cleanup_scope",
    "probe_repo_workspace",
    "push_worktree_branch_and_pr",
    "request_worktree_changes",
    "resolve_sdlc_slash_turn",
    "run_parallel_subagents",
    "tool_dev_preview",
    "tool_git_manage",
    "upsert_pull_request",
    "validate_external_repo_path",
]

_PROTECTED_BRANCHES = frozenset({"main", "master", "production", "release"})
_EPHEMERAL_CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
_EPHEMERAL_CACHE_EXTS = frozenset({".pyc", ".pyo", ".pyd"})
_BLOCKED_STAGE_NAMES = frozenset({".env", "secrets.env", "id_rsa", "id_ed25519", "id_ecdsa"})
_BLOCKED_STAGE_EXTS = frozenset({
    ".pem", ".key", ".p12", ".pfx", ".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3",
})
_BLOCKED_STAGE_DIRS = frozenset({"node_modules", ".companion_backups", ".ssh", ".ssh_tmp"})
_STRIP_PREVIEW_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASS", "_PAT", "_CREDENTIAL")
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

WORKTREE_CODE_TOOLS: Tuple[str, ...] = (
    "read_file", "write_file", "edit_file", "list_dir", "search",
    "run_python", "run_shell", "git_manage", "dev_preview",
    "workflow_manage", "memory_manage", "wiki_manage",
)
_PERSONAL_WORKER_TOOLS: Tuple[str, ...] = (
    "read_file", "write_file", "edit_file", "list_dir", "search",
    "web_fetch", "web_search", "wiki_manage", "memory_manage",
    "agile_manage", "calendar_manage",
)
_DIFF_CAP_CHARS = 40_000
_ATT_ID_RE = re.compile(r"\batt_[A-Za-z0-9_]+\b")
_IMG_PATH_RE = re.compile(r"([A-Za-z0-9_./:\\-]+?\.(?:png|jpg|jpeg|webp))", re.I)


# ---------------------------------------------------------------------------
# 1. Secret-Shielded Git, Worktrees & Blind-Vault Git Auth (`tool_git_manage`)
# ---------------------------------------------------------------------------
def _slugify_branch_part(raw: str) -> str:
    return re.sub(r"[^a-z0-9._/-]+", "-", (raw or "").strip().lower()).strip("-/")[:72] or "task"


def _active_project_record() -> Dict[str, Any]:
    ws = settings.get_workspace()
    try:
        pg = get_project_graph(get_memory_store())
        for cand in pg.list_projects():
            if cand.get("workspace_path") and Path(str(cand["workspace_path"])).resolve() == ws.project_root.resolve():
                return cand
        if ws.project_id and not ws.is_self_project:
            return pg.get_project(ws.project_id)
        return pg.get_project()
    except Exception:
        return {"project_id": ws.project_id, "delivery_policy": "local_only", "git_credential_ref": ""}


SDLC_SLASH_MAP: Dict[str, Tuple[int, str, str]] = {
    "sdlc-next": (0, "auto", "Auto-Router"),
    "sdlc-1-product": (1, "sdlc-product", "Stage 1: Product Management"),
    "sdlc-product": (1, "sdlc-product", "Stage 1: Product Management"),
    "sdlc-2-architecture": (2, "sdlc-architecture", "Stage 2: System Architecture & ADRs"),
    "sdlc-architecture": (2, "sdlc-architecture", "Stage 2: System Architecture & ADRs"),
    "sdlc-3-backlog": (3, "sdlc-backlog", "Stage 3: Delivery & Backlog Planning"),
    "sdlc-backlog": (3, "sdlc-backlog", "Stage 3: Delivery & Backlog Planning"),
    "sdlc-4-design": (4, "sdlc-design", "Stage 4: Design Engineering & Hardening Pass"),
    "sdlc-design": (4, "sdlc-design", "Stage 4: Design Engineering & Hardening Pass"),
    "sdlc-5-implement": (5, "sdlc-implement", "Stage 5: Software Implementation"),
    "sdlc-implement": (5, "sdlc-implement", "Stage 5: Software Implementation"),
    "sdlc-6-verify": (6, "sdlc-verify", "Stage 6: Quality & Verification Engineering"),
    "sdlc-verify": (6, "sdlc-verify", "Stage 6: Quality & Verification Engineering"),
    "sdlc-7-operate": (7, "sdlc-operate", "Stage 7: Operations & Reliability Engineering"),
    "sdlc-operate": (7, "sdlc-operate", "Stage 7: Operations & Reliability Engineering"),
    "sdlc-change": (8, "sdlc-change", "Day-2: Change Management & Ripple Triage"),
}
_SDLC_STAGE_META: Dict[int, Tuple[str, str]] = {
    1: ("sdlc-product", "Stage 1: Product Management"),
    2: ("sdlc-architecture", "Stage 2: System Architecture & ADRs"),
    3: ("sdlc-backlog", "Stage 3: Delivery & Backlog Planning"),
    4: ("sdlc-design", "Stage 4: Design Engineering & Hardening Pass"),
    5: ("sdlc-implement", "Stage 5: Software Implementation"),
    6: ("sdlc-verify", "Stage 6: Quality & Verification Engineering"),
    7: ("sdlc-operate", "Stage 7: Operations & Reliability Engineering"),
    8: ("sdlc-change", "Day-2: Change Management & Ripple Triage"),
}
_SDLC_CODE_EXTS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rs", ".go", ".cs",
    ".java", ".kt", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".rb", ".php", ".ps1", ".psm1",
})
_SDLC_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".companion_backups", ".worktrees",
})
_TEMPLATE_BRACKET_RE = re.compile(
    r"\[(?:Product\s*/\s*System\s+Name|System\s+Name|Human-Readable\s+Name|Write\s+1\s+crisp|"
    r"Describe\s+the\s+concrete|Selected\s+Technology|Selected\s+Host|Concrete\s+target|"
    r"Exact\s+native\s+file/folder\s+tree|Copy-paste\s+query)\b[^\]]*\]",
    re.I,
)
_STUB_MARKER_RE = re.compile(r"(?<![A-Za-z0-9_-])(?:TODO|TBD|FIXME)(?:\s*:|\b)")
_DEFAULT_GREENFIELD_GITIGNORE = (
    "__pycache__/\n*.py[cod]\n.venv/\nvenv/\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n"
    "node_modules/\ndist/\nbuild/\n.env\n.env.*\n!.env.example\n!.env.sample\n!.env.template\n"
    "secrets.env\n*.pem\n*.key\n*.db\n*.db-wal\n*.db-shm\n*.sqlite*\n.DS_Store\nThumbs.db\n.companion_backups/\n"
)


def validate_external_repo_path(target: Path) -> Optional[str]:
    """Mechanically block repository initialization or binding inside Companion's `app/`, `data/`, `$HOME`, or `/`."""
    try:
        resolved = Path(target).expanduser().resolve()
    except Exception as exc:
        return f"SAFETY GUARD REJECTED: invalid repository path `{target}` ({exc})."
    self_res = settings.root.resolve()
    data_res = settings.effective_data_dir.resolve()
    raw_data = (self_res / "data").resolve()
    if resolved == self_res or resolved.is_relative_to(self_res):
        return (
            f"SAFETY GUARD REJECTED: Repository path `{resolved}` cannot live inside Companion's immutable `app/` "
            f"runtime (`{self_res}`). Create or attach repositories under `{settings.projects_dir}`."
        )
    if resolved == data_res or resolved.is_relative_to(data_res) or resolved == raw_data or resolved.is_relative_to(raw_data):
        return (
            f"SAFETY GUARD REJECTED: Repository path `{resolved}` cannot live inside Companion's runtime `data/` "
            f"directory (`{data_res}`). Create or attach repositories under `{settings.projects_dir}`."
        )
    anchor_res = Path(resolved.anchor).resolve() if resolved.anchor else Path("/")
    if resolved == anchor_res or resolved == Path.home().resolve():
        return f"SAFETY GUARD REJECTED: Repository path `{resolved}` cannot be the filesystem root or home directory directly."
    return None


def _extract_layout_source_paths(design_text: str) -> List[str]:
    m = re.search(r"(?im)^#{2,3}\s*(?:1\.?\s*)?Project\s*&\s*Module\s+File\s+Layout\b[^\n]*\n(.*?)(?=^#{2,3}\s|\Z)", design_text, re.S)
    scope = m.group(1) if m else ""
    if not scope:
        return []
    found: List[str] = []
    for tok in re.findall(r"(?:src|app|lib|pkg|cmd|internal|backend|server|client)/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+", scope):
        clean = tok.strip("`'\".,;:()[]")
        low = clean.lower()
        if Path(clean).suffix.lower() in _SDLC_CODE_EXTS and not any(k in low for k in ("test_", "_test.", ".test.", ".spec.", "/tests/", "/test/")):
            if clean not in found:
                found.append(clean)
    return found


def _scan_repo_code_and_tests(repo_root: Path) -> Tuple[List[str], List[str]]:
    sources: List[str] = []
    tests: List[str] = []
    if not repo_root.is_dir():
        return sources, tests
    for cur_root, dirs, files in os.walk(repo_root):
        dirs[:] = sorted(d for d in dirs if d not in _SDLC_SKIP_DIRS and not d.startswith("."))
        rel_dir = Path(cur_root).resolve().relative_to(repo_root.resolve()).as_posix()
        top_seg = "" if rel_dir == "." else rel_dir.split("/", 1)[0].lower()
        if top_seg in ("docs", ".github", ".agents"):
            continue
        for fn in sorted(files):
            ext = Path(fn).suffix.lower()
            if ext not in _SDLC_CODE_EXTS:
                continue
            rel_f = fn if rel_dir == "." else f"{rel_dir}/{fn}"
            low_f, low_name = rel_f.lower(), fn.lower()
            is_test = (
                top_seg in ("tests", "test", "__tests__", "spec")
                or "/tests/" in f"/{low_f}"
                or "/__tests__/" in f"/{low_f}"
                or low_name.startswith("test_")
                or any(s in low_name for s in ("_test.", ".test.", ".spec."))
            )
            if is_test:
                tests.append(rel_f)
            elif low_name not in ("conftest.py", "setup.py"):
                sources.append(rel_f)
    return sources, tests


def _inspect_choice_b_increments(repo_root: Path) -> Tuple[List[str], List[Dict[str, Any]]]:
    feat_root = repo_root / "docs" / "features"
    if not feat_root.is_dir():
        return [], []
    overviews = [p.relative_to(repo_root).as_posix() for p in sorted(feat_root.glob("**/00-feature-overview.md")) if p.is_file()]
    increments: List[Dict[str, Any]] = []
    for inc_p in sorted(feat_root.glob("**/inc-*.md")):
        if not inc_p.is_file():
            continue
        txt = inc_p.read_text(encoding="utf-8", errors="ignore")
        shipped = bool(
            re.search(r"(?im)^\s*(?:[-*]\s*)?\*{0,2}status\*{0,2}\s*:\s*`?\*{0,2}shipped\b", txt)
            or re.search(r"(?i)\bSTATUS\s*:\s*SHIPPED\b", txt)
        )
        m_des = re.search(r"(?im)^(#{2,3})\s*(?:Stage\s*4\s*:?\s*)?Engineering\s+Design([^\n]*)$", txt)
        if m_des is None or "pending" in m_des.group(2).lower():
            design_pending = True
            design_body = ""
        else:
            rest = txt[m_des.end() :]
            nxt_h = re.search(r"(?m)^#{1,2}\s+\S+", rest)
            design_body = (rest[: nxt_h.start()] if nxt_h else rest).strip()
            design_pending = len(design_body) < 40 or design_body.lower().strip("() ") in ("pending", "tbd", "todo")
        planned = [] if design_pending else _extract_layout_source_paths(design_body or txt)
        missing_src = [sp for sp in planned if not (repo_root / sp).is_file()]
        increments.append({
            "path": inc_p.relative_to(repo_root).as_posix(),
            "design_pending": design_pending,
            "shipped": shipped,
            "planned_sources": planned,
            "missing_sources": missing_src,
        })
    return overviews, increments


def _scan_sdlc_placeholders(repo_root: Path, pending_inc_paths: set[str]) -> List[str]:
    candidates: List[Path] = []
    for top_doc in ("product.md", "architecture.md", "adr.md"):
        if (repo_root / top_doc).is_file():
            candidates.append(repo_root / top_doc)
    if (repo_root / "docs").is_dir():
        candidates.extend(sorted((repo_root / "docs").glob("**/*.md")))
    violations: List[str] = []
    for md_p in candidates:
        rel = md_p.relative_to(repo_root).as_posix()
        txt = md_p.read_text(encoding="utf-8", errors="ignore")
        if rel in pending_inc_paths:
            txt = re.sub(r"(?ims)^#{2,3}\s*Stage\s*4[^\n]*\bPending\b.*$", "", txt)
        prose = re.sub(r"```.*?```", "", txt, flags=re.S)
        for idx, ln in enumerate(prose.splitlines(), 1):
            if _TEMPLATE_BRACKET_RE.search(ln) or _STUB_MARKER_RE.search(ln):
                violations.append(f"{rel}:{idx}: {ln.strip()[:120]}")
                if len(violations) >= 20:
                    return violations
    return violations


def inspect_sdlc_repo_state(repo_root: Path) -> Dict[str, Any]:
    """Deterministically inspect SDLC artifacts, Greenfield vs Brownfield posture, Choice B Increments, and hygiene."""
    root = Path(repo_root).expanduser().resolve()
    has_product = (root / "product.md").is_file()
    has_root_arch = (root / "architecture.md").is_file()
    host_arch_docs = [d for d in ("AGENTS.md", "CLAUDE.md", "docs/architecture.md", "docs/project-layout.md") if (root / d).is_file()]
    adr_rels = (["adr.md"] if (root / "adr.md").is_file() else []) + (
        [p.relative_to(root).as_posix() for p in sorted((root / "docs" / "adr").glob("*.md")) if p.is_file()]
        if (root / "docs" / "adr").is_dir() else []
    )
    has_adrs = len(adr_rels) > 0
    has_choice_a_backlog = (root / "docs" / "features.md").is_file() and (root / "docs" / "user-stories.md").is_file()
    overviews, increments = _inspect_choice_b_increments(root)
    has_choice_b_backlog = len(increments) > 0
    backlog_mode = "choice_b" if (has_choice_b_backlog or overviews) else ("choice_a" if has_choice_a_backlog else "none")

    eng_design_p = root / "docs" / "engineering-design.md"
    eng_design_txt = eng_design_p.read_text(encoding="utf-8", errors="ignore") if eng_design_p.is_file() else ""
    has_choice_a_design = bool(eng_design_txt.strip() and len(eng_design_txt.strip()) >= 40)
    choice_a_missing_src = [sp for sp in _extract_layout_source_paths(eng_design_txt) if not (root / sp).is_file()] if has_choice_a_design else []

    sources, tests = _scan_repo_code_and_tests(root)
    has_test_strategy = (root / "docs" / "testing-strategy.md").is_file()
    has_runbook = (root / "docs" / "operations-runbook.md").is_file()
    is_brownfield = (not has_product) and (not has_root_arch) and (bool(host_arch_docs) or (len(sources) >= 1 and backlog_mode == "none"))
    repo_kind = "brownfield" if is_brownfield else "greenfield"

    active_inc = ""
    lifecycle_complete = False
    if repo_kind == "greenfield" and not has_product:
        stage = 1
    elif (repo_kind == "greenfield" and (not has_root_arch or not has_adrs)) or (repo_kind == "brownfield" and not has_adrs and not has_choice_b_backlog and not has_choice_a_backlog):
        stage = 2
    elif not has_choice_a_backlog and not has_choice_b_backlog:
        stage = 3
    elif has_choice_b_backlog:
        unshipped = [inc for inc in increments if not inc["shipped"]]
        if unshipped:
            cur_inc = unshipped[0]
            active_inc = str(cur_inc["path"])
            if cur_inc["design_pending"]:
                stage = 4
            elif cur_inc["missing_sources"] or (len(sources) == 0):
                stage = 5
            else:
                stage = 6
        elif not has_runbook:
            stage = 7
        else:
            stage, lifecycle_complete = 8, True
    else:
        if not has_choice_a_design:
            stage = 4
        elif choice_a_missing_src or len(sources) == 0:
            stage = 5
        elif not has_test_strategy or len(tests) == 0:
            stage = 6
        elif not has_runbook:
            stage = 7
        else:
            stage, lifecycle_complete = 8, True

    skill_name, stage_lbl = _SDLC_STAGE_META[stage]
    upstream: List[str] = []
    for cand in ("product.md", "architecture.md", *host_arch_docs, *adr_rels[:6]):
        if (root / cand).is_file() and cand not in upstream:
            upstream.append(cand)
    if has_choice_a_backlog:
        upstream.extend(["docs/features.md", "docs/user-stories.md"])
    if overviews:
        upstream.extend(overviews[:3])
    if active_inc and active_inc not in upstream:
        upstream.append(active_inc)
    elif has_choice_a_design:
        upstream.append("docs/engineering-design.md")
    if has_test_strategy:
        upstream.append("docs/testing-strategy.md")
    if has_runbook:
        upstream.append("docs/operations-runbook.md")

    pending_set = {str(i["path"]) for i in increments if i["design_pending"] and not i["shipped"]}
    violations = _scan_sdlc_placeholders(root, pending_set)
    inc_note = f" | target_increment=`{active_inc}`" if active_inc else ""
    return {
        "ok": True,
        "repo_root": str(root),
        "is_git_repo": (root / ".git").exists(),
        "repo_kind": repo_kind,
        "backlog_mode": backlog_mode,
        "detected_stage": 7 if lifecycle_complete else stage,
        "stage_label": stage_lbl,
        "recommended_skill": skill_name,
        "active_increment": active_inc,
        "upstream_artifacts": upstream,
        "increments": increments,
        "source_file_count": len(sources),
        "test_file_count": len(tests),
        "placeholder_violations": violations,
        "hygiene_clean": len(violations) == 0,
        "lifecycle_complete": lifecycle_complete,
        "summary": (
            f"SDLC Status (`{root.name}` | `{repo_kind}`): Next=`{stage_lbl}` (`{skill_name}`){inc_note} | "
            f"upstream={len(upstream)} | sources={len(sources)} | tests={len(tests)} | hygiene={'CLEAN' if not violations else f'{len(violations)} issue(s)'}"
        ),
    }


def resolve_sdlc_slash_turn(user_query: str, repo_root: Path, is_self_project: bool = False) -> Optional[Dict[str, Any]]:
    """Resolve `/sdlc-*` slash commands into deterministic stage, collaboration mode, and preloaded `SKILL.md`."""
    clean = (user_query or "").strip()
    if not clean.startswith("/"):
        return None
    parts = clean[1:].split(None, 1)
    if not parts:
        return None
    verb = parts[0].lower()
    if verb not in SDLC_SLASH_MAP:
        return None
    arg_text = parts[1].strip() if len(parts) > 1 else ""
    collab_mode = "Express" if re.search(r"\bexpress\b", arg_text, re.I) else "Guided Co-Design"
    state = inspect_sdlc_repo_state(repo_root)
    req_stage, req_skill, req_lbl = SDLC_SLASH_MAP[verb]
    if verb == "sdlc-next":
        skill_name = str(state["recommended_skill"])
        stage_num = 8 if skill_name == "sdlc-change" else int(state["detected_stage"])
        stage_lbl = str(state["stage_label"])
    else:
        skill_name, stage_num, stage_lbl = req_skill, req_stage, req_lbl

    skill_candidates = [settings.user_skills_dir / skill_name / "SKILL.md", settings.skills_dir / skill_name / "SKILL.md"]
    skill_file = next((p for p in skill_candidates if p.is_file()), None)
    skill_body = skill_file.read_text(encoding="utf-8", errors="replace").strip() if skill_file else ""
    self_guard = (
        "\nCRITICAL WORKSPACE GUARD: Active workspace is currently `companion-self` (Companion's immutable `app/` runtime). "
        "Do NOT write `product.md` or project files into `app/` or `scratch/`. First initialize and focus a new external Git repository "
        f"under `{settings.projects_dir}` via `agile_manage(action='create_project', kind='software', init_git=True, title='...')` "
        "(or ask the user which project to focus) before writing repository artifacts!"
        if is_self_project
        else ""
    )
    stop_rule = (
        "MODE RULE (`Guided Co-Design`): Execute ONLY the next narrated Co-Design Stop in chat (starting at Stop 1 when entering a stage). "
        "Propose concrete, domain-grounded drafts from knowns, explain WHY and what real bug/trap it prevents in plain English, and pause for the user's gut-check. "
        "For Stages 1-4 and Day-2 Change Triage, do NOT write final stage files to disk until the final stop is confirmed."
        if collab_mode == "Guided Co-Design"
        else "MODE RULE (`Express`): Read the stage's bundled reference template via `read_file('skills/"
        + skill_name
        + "/references/...')` if applicable, write the complete zero-placeholder artifact(s) to disk immediately, and present the Checkpoint Review Card."
    )
    directive = (
        f"[SDLC ROUTER DIRECTIVE — `/{verb}`]\n"
        f"- Detected Repo Posture : `{state['repo_kind']}` (`{repo_root}` | backlog=`{state['backlog_mode']}`)\n"
        f"- Active Stage & Skill  : `{stage_lbl}` (`skills/{skill_name}/SKILL.md`)"
        + (f" | Target Increment: `{state['active_increment']}`" if state.get("active_increment") else "")
        + f"\n- Collaboration Mode    : `{collab_mode}`\n"
        f"- Locked Upstream Files : {', '.join(f'`{u}`' for u in state['upstream_artifacts']) if state['upstream_artifacts'] else '(none yet — greenfield start)'}\n"
        f"- User Notes / Argument : {arg_text or '(none)'}\n"
        "1. Open your response with one crisp sentence announcing the detected Stage/Skill and Collaboration Mode.\n"
        "2. Read locked upstream files from disk first so you inherit full cross-session context.\n"
        f"3. {stop_rule}{self_guard}"
    )
    if skill_body:
        directive += f"\n\n[PRE-LOADED SKILL RUNBOOK (`skills/{skill_name}/SKILL.md`)]\n{skill_body}"
    suggested_tier = "coding" if (stage_num in (5, 6) and settings.resolve_tier("coding").is_dedicated) else "reasoning"
    return {
        "verb": verb,
        "arg": arg_text,
        "mode": collab_mode,
        "stage": stage_num,
        "stage_label": stage_lbl,
        "skill": skill_name,
        "suggested_tier": suggested_tier,
        "repo_state": state,
        "prompt_block": directive,
    }


def init_greenfield_repo(
    target_path: Any = "",
    title: str = "",
    goal_summary: str = "",
    default_branch: str = "main",
) -> Dict[str, Any]:
    """Idempotently initialize an external Git repository on `main` with `.gitignore`, `README.md`, and initial commit."""
    raw = str(target_path or "").strip().strip("'\"")
    if not raw:
        slug = _slugify_part(title or "new-project", fallback="new-project", max_len=48)
        target = (settings.projects_dir / slug).resolve()
    else:
        cand = Path(raw).expanduser()
        target = cand.resolve() if (cand.is_absolute() or raw.startswith("~")) else (settings.projects_dir / cand).resolve()
    bound_err = validate_external_repo_path(target)
    if bound_err:
        return {"ok": False, "error": bound_err}
    if shutil.which("git") is None:
        return {"ok": False, "error": "error: git executable not found on PATH"}

    target.mkdir(parents=True, exist_ok=True)
    created_repo = False
    if not (target / ".git").exists():
        rc_i, out_i = _run_git_cmd(target, ["init", "-b", default_branch])
        if rc_i != 0:
            rc_i, out_i = _run_git_cmd(target, ["init"])
            if rc_i != 0:
                return {"ok": False, "error": f"error: git init failed: {out_i}"}
            _run_git_cmd(target, ["symbolic-ref", "HEAD", f"refs/heads/{default_branch}"])
        created_repo = True

    created_commit = False
    if _run_git_cmd(target, ["rev-parse", "--verify", "HEAD"])[0] != 0:
        gi_path = target / ".gitignore"
        if not gi_path.exists():
            gi_path.write_text(_DEFAULT_GREENFIELD_GITIGNORE, encoding="utf-8", newline="\n")
        readme_path = target / "README.md"
        if not readme_path.exists():
            heading = (title or target.name).strip()
            summary_blk = f"\n{goal_summary.strip()}\n" if (goal_summary or "").strip() else "\n"
            readme_path.write_text(f"# {heading}\n{summary_blk}", encoding="utf-8", newline="\n")
        _run_git_cmd(target, ["symbolic-ref", "HEAD", f"refs/heads/{default_branch}"])
        stage_rels: List[str] = [f for f in (".gitignore", "README.md") if (target / f).is_file()]
        rc_st, st_out = _run_git_cmd(target, ["status", "--porcelain=v1"])
        for ln in (st_out.splitlines() if rc_st == 0 else []):
            m_porc = re.match(r"^[MADRCU?! ]{1,2}\s+(.+)$", ln)
            if m_porc:
                entry = m_porc.group(1).split(" -> ")[-1].strip().strip('"')
                if entry and entry not in stage_rels and not is_ephemeral_cache_path(entry) and check_git_stage_safety(target, [entry]) is None:
                    stage_rels.append(entry)
        rc_add, out_add = _run_git_cmd(target, ["add", "--", *stage_rels])
        if rc_add != 0:
            return {"ok": False, "error": f"error: git add failed during repo init: {out_add}"}
        rc_c, out_c = _run_git_cmd(target, ["commit", "-m", "chore: initial repository bootstrap"])
        if rc_c != 0 and any(k in out_c.lower() for k in ("user.email", "user.name", "please tell me who you are", "auto-detect email")):
            _run_git_cmd(target, ["config", "user.name", "Companion AI OS"])
            _run_git_cmd(target, ["config", "user.email", "companion@localhost"])
            rc_c, out_c = _run_git_cmd(target, ["commit", "-m", "chore: initial repository bootstrap"])
        if rc_c != 0:
            return {"ok": False, "error": f"error: initial git commit failed: {out_c}"}
        created_commit = True

    branch = _current_branch(target)
    if branch == "HEAD":
        branch = default_branch
    _, sha_out = _run_git_cmd(target, ["rev-parse", "--short", "HEAD"])
    is_idempotent = (not created_repo) and (not created_commit)
    lbl = "Verified existing Git repository" if is_idempotent else "Initialized greenfield Git repository"
    return {
        "ok": True,
        "repo_root": str(target),
        "branch": branch,
        "commit_sha": sha_out.strip(),
        "created_repo": created_repo,
        "initial_commit_created": created_commit,
        "idempotent": is_idempotent,
        "summary": f"{lbl} at `{target}` on `{branch}` (`{sha_out.strip()}`).",
    }


def discover_repo_sdlc_context(root: Path, project: Optional[Dict[str, Any]] = None) -> str:
    """Discover repo-local engineering rules, stack commands, SDLC stage state, and Co-Design operating laws."""
    resolved = root.resolve()
    if not resolved.is_dir():
        return ""
    proj = project or _active_project_record()
    proj_id = str(proj.get("project_id") or "")
    proj_ws = str(proj.get("workspace_path") or "").strip()
    proj_kind = str(proj.get("kind") or "").strip().lower()
    if resolved == settings.root.resolve() and proj_id and proj_id != "companion-self" and not proj_ws and proj_kind != "software":
        return ""
    rule_notes: List[str] = []
    rules_dir = resolved / ".agents" / "rules"
    if rules_dir.is_dir():
        for rf in sorted(rules_dir.glob("*.md"))[:5]:
            head = next((ln.lstrip("# ").strip() for ln in rf.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()), rf.stem)
            rule_notes.append(f"`.agents/rules/{rf.name}` ({head[:64]})")
    for doc_name in ("AGENTS.md", "CLAUDE.md"):
        dp = resolved / doc_name
        if dp.is_file():
            head = next((ln.lstrip("# ").strip() for ln in dp.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()), doc_name)
            rule_notes.append(f"`{doc_name}` ({head[:64]})")

    stacks: List[str] = []
    if (resolved / "companion").is_dir() and (resolved / "harness.py").is_file():
        stacks.append("companion-python (`python harness.py --doctor`, preview: `python harness.py --web --port {port} --no-browser`)")
    elif (resolved / "pyproject.toml").is_file() or (resolved / "tests").is_dir() or (resolved / "requirements.txt").is_file():
        stacks.append("python (`python -m unittest discover -s tests -p \"test_*.py\"`)")
    if (resolved / "package.json").is_file():
        stacks.append("node (`npm test`, `npm run build`)")
    elif (resolved / "frontend" / "package.json").is_file():
        stacks.append("frontend-node (`npm --prefix frontend test`, `npm --prefix frontend run build`)")
    if (resolved / "Cargo.toml").is_file():
        stacks.append("rust (`cargo test`)")
    if (resolved / "go.mod").is_file():
        stacks.append("go (`go test ./...`)")
    if list(resolved.glob("*.sln")) or list(resolved.glob("*.csproj")):
        stacks.append("dotnet (`dotnet test`)")
    if (resolved / "Makefile").is_file():
        stacks.append("make (`Makefile`)")

    is_software_ws = (
        (resolved / ".git").exists()
        or str(proj.get("kind") or "").lower() == "software"
        or bool(stacks)
        or bool(rule_notes)
        or (resolved / "product.md").is_file()
        or (resolved / "architecture.md").is_file()
        or (resolved / "docs").is_dir()
    )
    if not is_software_ws:
        return ""

    sdlc_state = inspect_sdlc_repo_state(resolved)
    parts = [f"delivery_policy=`{proj.get('delivery_policy', 'local_only')}`"]
    if (resolved / ".git").exists():
        parts.append(f"branch=`{_current_branch(resolved)}`")
    if proj.get("git_credential_ref"):
        parts.append(f"git_auth=`vault://{proj['git_credential_ref']}`")
    if proj.get("test_command"):
        parts.append(f"test_cmd=`{proj['test_command']}`")
    parts.append(f"sdlc_posture=`{sdlc_state['repo_kind']}`")
    parts.append(f"next_sdlc_stage=`{sdlc_state['stage_label']}` (`{sdlc_state['recommended_skill']}`)")
    if sdlc_state.get("active_increment"):
        parts.append(f"active_increment=`{sdlc_state['active_increment']}`")
    if stacks:
        parts.append("stack=" + "; ".join(stacks))
    if rule_notes:
        parts.append("repo_rules=" + ", ".join(rule_notes))
    laws = (
        "sdlc_operating_rules: Default=`Guided Co-Design` (2-4 narrated stops in chat: propose concrete drafts first, "
        "explain WHY & what bug/trap it prevents, pause for gut-check; switch to single-pass `Express` only if user says 'express'). "
        "5 Core Laws: (1) Senior judgment (0-2 questions max with `(Recommended: Option A)`, never quiz on standard norms); "
        "(2) Grounded design (no speculative frameworks; Entry/Core/Adapter = logical roles in native stack idioms); "
        "(3) Greenfield (`product.md` -> `architecture.md`) vs Brownfield (existing `AGENTS.md`/`docs/architecture.md` -> start at `docs/adr/` + `docs/features/F0X/`); "
        "(4) Disk artifacts = cross-session memory (keep teaching strictly in chat; zero `TODO`/`TBD` stubs in repo files); "
        "(5) 75-90% Simplified Technical English keeping 100% of exact types, paths, flags, and failure codes."
    )
    return f"<repo_sdlc_context>\nsdlc_repo_context: {' | '.join(parts)}\n{laws}\n</repo_sdlc_context>"


def _resolve_repo_arg(raw_repo: str) -> Path:
    active = settings.active_project_root.resolve()
    clean = (raw_repo or "").strip()
    if not clean or clean in (".", "./") or clean.replace("\\", "/").strip("/") == active.name:
        return active
    p = Path(clean)
    return p.resolve() if p.is_absolute() else (active / p).resolve()


def _is_allowed_sdlc_repo_dir(target: Path, mutating: bool) -> Optional[str]:
    resolved, self_root = target.resolve(), settings.root.resolve()
    if mutating and resolved == self_root:
        return f"SAFETY GUARD REJECTED: Companion's installed `app/` directory (`{self_root}`) is permanently IMMUTABLE. Run mutating Git/SDLC operations inside an external Project clone or worktree (`workspace_path`)."
    ws = settings.get_workspace()
    allowed = [ws.project_root.resolve(), Path(tempfile.gettempdir()).resolve()]
    wt_base = (ws.project_root.resolve().parent / ".worktrees").resolve()
    if wt_base != self_root:
        allowed.append(wt_base)
    if not ws.is_self_project and resolved.is_relative_to(self_root) and not resolved.is_relative_to(ws.project_root.resolve()):
        return f"SAFETY GUARD REJECTED: `{resolved}` is inside Companion's immutable `app/` runtime."
    if not any(resolved.is_relative_to(a) for a in allowed) and resolved != self_root:
        return f"SAFETY GUARD REJECTED: Repository path `{resolved}` is outside the active WorkspaceContext (`{ws.project_root}`)."
    return None


def is_ephemeral_cache_path(raw_rel: str) -> bool:
    p = Path((raw_rel or "").replace("\\", "/").strip("/"))
    return bool(({pt.lower() for pt in p.parts} & _EPHEMERAL_CACHE_DIRS) or p.suffix.lower() in _EPHEMERAL_CACHE_EXTS)


def check_git_stage_safety(repo_root: Path, rel_paths: List[str]) -> Optional[str]:
    """Mechanically block staging `.env`, `secrets.env`, private keys, SQLite DBs/WALs, or `node_modules/`."""
    for raw_rel in rel_paths:
        norm = raw_rel.replace("\\", "/").strip("/")
        if not norm:
            continue
        p = Path(norm)
        fname = p.name.lower()
        parts_low = {pt.lower() for pt in p.parts}
        if parts_low & _BLOCKED_STAGE_DIRS:
            return f"SAFETY GUARD REJECTED staging `{norm}`: directory in {sorted(parts_low & _BLOCKED_STAGE_DIRS)} must never be committed."
        is_secret_env = fname in _BLOCKED_STAGE_NAMES or (
            fname.startswith(".env.") and fname not in (".env.example", ".env.sample", ".env.template")
        )
        if is_secret_env or fname.startswith(("id_rsa", "id_ed25519")) or p.suffix.lower() in _BLOCKED_STAGE_EXTS:
            return f"SAFETY GUARD REJECTED staging `{norm}`: secret, key, or database file staging is mechanically blocked."
        full = (repo_root / norm).resolve()
        if full.is_file() and full.stat().st_size <= 512_000:
            head = full.read_text(encoding="utf-8", errors="ignore")[:8192]
            if "-----BEGIN " in head and "PRIVATE KEY-----" in head:
                return f"SAFETY GUARD REJECTED staging `{norm}`: detected PEM private key material inside file."
    return None


@contextlib.contextmanager
def _git_auth_env(cred_ref: str, project_id: str) -> Iterator[Tuple[Optional[Dict[str, str]], Optional[str]]]:
    """Create ephemeral zero-reveal `GIT_SSH_COMMAND` or `GIT_ASKPASS` helpers from Blind Vault and clean up."""
    base_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    clean_ref = (cred_ref or "").strip()
    if not clean_ref:
        yield base_env, None
        return
    vault = get_vault_manager()
    ok, secret_or_err = vault.resolve_for_call(clean_ref, caller_scope=f"git:{project_id}")
    if not ok:
        yield None, f"SAFETY GUARD REJECTED Git auth: {secret_or_err}"
        return
    meta = vault.get_metadata(clean_ref) or {}
    tmp_dir = Path(tempfile.mkdtemp(prefix=".comp_ssh_git_"))
    try:
        if meta.get("cred_type") == "ssh_key" or "-----BEGIN " in secret_or_err:
            key_file = tmp_dir / "id_git"
            key_file.write_text(secret_or_err.rstrip() + "\n", encoding="utf-8", newline="\n")
            with contextlib.suppress(OSError):
                os.chmod(key_file, 0o600)
            _lock_windows_owner_dacl(key_file)
            base_env["GIT_SSH_COMMAND"] = f'ssh -i "{key_file.as_posix()}" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes'
        else:
            tok_file = tmp_dir / ".secrets_git_pat"
            tok_file.write_text(secret_or_err.strip(), encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(tok_file, 0o600)
            _lock_windows_owner_dacl(tok_file)
            py_helper = tmp_dir / "askpass_helper.py"
            py_helper.write_text(
                "import os, pathlib, sys\n"
                "prompt = (sys.argv[1] if len(sys.argv) > 1 else '').lower()\n"
                "if 'username' in prompt:\n"
                "    print(os.environ.get('COMPANION_GIT_USER', 'x-access-token'))\n"
                "else:\n"
                "    print(pathlib.Path(os.environ['COMPANION_GIT_TOK_FILE']).read_text(encoding='utf-8').strip())\n",
                encoding="utf-8",
            )
            if os.name == "nt":
                wrapper = tmp_dir / "git_askpass.cmd"
                wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{py_helper}" %*\r\n', encoding="utf-8")
            else:
                wrapper = tmp_dir / "git_askpass.sh"
                wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{py_helper}" "$@"\n', encoding="utf-8", newline="\n")
                os.chmod(wrapper, 0o700)
            base_env["COMPANION_GIT_TOK_FILE"] = str(tok_file)
            base_env["GIT_ASKPASS"] = str(wrapper)
        yield base_env, None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_git_cmd(repo_root: Path, git_args: List[str], env: Optional[Dict[str, str]] = None, timeout: int = 45) -> Tuple[int, str]:
    if shutil.which("git") is None:
        return -1, "error: git executable not found on PATH"
    run_env = env or {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    proc = subprocess.run(
        ["git", *git_args], cwd=str(repo_root), env=run_env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    vault = get_vault_manager()
    combined = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    return proc.returncode, vault.scrub_output(combined)


def _current_branch(repo_root: Path) -> str:
    rc, out = _run_git_cmd(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    return out.strip() if rc == 0 and out.strip() else "HEAD"


def _handle_git_worktree(action: str, repo_root: Path, args: Dict[str, Any]) -> str:
    if action == "worktree_list":
        rc, out = _run_git_cmd(repo_root, ["worktree", "list"])
        return out if rc == 0 else f"error: {out}"
    if action == "worktree_remove":
        wt_raw = str_arg(args, "worktree_path") or str_arg(args, "path")
        if not wt_raw:
            return "error: worktree_path is required for worktree_remove"
        wt_p = Path(wt_raw).resolve()
        if wt_p == repo_root.resolve() or wt_p == settings.root.resolve():
            return "SAFETY GUARD REJECTED: refusing to remove primary repository root."
        rc, out = _run_git_cmd(repo_root, ["worktree", "remove", "--force", str(wt_p)])
        _run_git_cmd(repo_root, ["worktree", "prune"])
        return f"Removed worktree `{wt_p}`." if rc == 0 else f"error: {out}"
    raw_br = str_arg(args, "branch", aliases=("branch_name", "target_branch")) or f"feat/{_slugify_branch_part(str_arg(args, 'slug') or 'slice')}"
    if raw_br.lower() in _PROTECTED_BRANCHES:
        return f"SAFETY GUARD REJECTED: create worktrees on a feature branch (`feat/...`), not `{raw_br}`."
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw_br.lower()).strip("-")[:48]
    wt_raw = str_arg(args, "worktree_path", aliases=("path", "dir"))
    wt_dir = Path(wt_raw).resolve() if wt_raw else (repo_root.parent / ".worktrees" / repo_root.name / slug).resolve()
    if wt_dir.is_relative_to(settings.root.resolve()):
        return "SAFETY GUARD REJECTED: worktree_path must live outside Companion's installed `app/` directory."
    if (wt_dir / ".git").exists():
        return json.dumps({"ok": True, "reused": True, "branch": _current_branch(wt_dir), "worktree_path": str(wt_dir), "summary": f"Reusing existing worktree `{wt_dir}` on branch `{_current_branch(wt_dir)}`."})
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    chk_rc, _ = _run_git_cmd(repo_root, ["show-ref", "--verify", f"refs/heads/{raw_br}"])
    wt_args = ["worktree", "add", str(wt_dir), raw_br] if chk_rc == 0 else ["worktree", "add", "-b", raw_br, str(wt_dir)]
    rc, out = _run_git_cmd(repo_root, wt_args)
    if rc != 0:
        return f"error: git worktree add failed: {out}"
    return json.dumps({"ok": True, "branch": raw_br, "worktree_path": str(wt_dir), "summary": f"Created isolated worktree `{wt_dir}` on branch `{raw_br}`."})


def _handle_git_commit(repo_root: Path, args: Dict[str, Any]) -> str:
    cur_br = _current_branch(repo_root)
    allow_prot = bool_arg(args, "allow_protected_branch", False) and not is_in_agent_tool_turn()
    if cur_br.lower() in _PROTECTED_BRANCHES and not allow_prot:
        return f"SAFETY GUARD REJECTED: direct commits onto protected branch `{cur_br}` are blocked. Create/switch to a feature branch first via `git_manage(action='branch', branch='feat/<slug>')`."
    msg = " ".join((str_arg(args, "message", aliases=("commit_message", "msg", "summary")) or "").splitlines()).strip()
    if not msg:
        return "error: commit `message` is required"
    raw_paths = next((args[k] for k in ("paths", "files", "file_paths") if args.get(k) is not None), None)
    if isinstance(raw_paths, str) and raw_paths.strip():
        raw_paths = json.loads(raw_paths) if raw_paths.strip().startswith("[") else [p.strip() for p in raw_paths.split(",") if p.strip()]
    cand_rels: List[str] = [str(p).strip() for p in raw_paths if str(p).strip() and not is_ephemeral_cache_path(str(p))] if isinstance(raw_paths, list) and raw_paths else []
    if not cand_rels:
        rc_st, st_out = _run_git_cmd(repo_root, ["status", "--porcelain=v1"])
        if rc_st != 0:
            return f"error: {st_out}"
        for ln in st_out.splitlines():
            m_porc = re.match(r"^[MADRCU?! ]{1,2}\s+(.+)$", ln)
            if m_porc:
                entry = m_porc.group(1).split(" -> ")[-1].strip().strip('"')
                if entry and not is_ephemeral_cache_path(entry):
                    cand_rels.append(entry)
    if not cand_rels:
        return "Working tree is clean; nothing to commit."
    stage_err = check_git_stage_safety(repo_root, cand_rels)
    if stage_err:
        return stage_err
    rc_add, out_add = _run_git_cmd(repo_root, ["add", "--", *cand_rels])
    if rc_add != 0:
        return f"error: git add failed: {out_add}"
    rc_cached, cached_names = _run_git_cmd(repo_root, ["diff", "--cached", "--name-only"])
    staged = [ln.strip() for ln in cached_names.splitlines() if ln.strip()] if rc_cached == 0 else []
    if not staged:
        return "No changes staged for commit."
    staged_err = check_git_stage_safety(repo_root, staged)
    if staged_err:
        _run_git_cmd(repo_root, ["reset", "HEAD"])
        return staged_err
    rc_c, out_c = _run_git_cmd(repo_root, ["commit", "-m", msg])
    if rc_c != 0 and any(k in out_c for k in ("user.email", "user.name", "Please tell me who you are")):
        _run_git_cmd(repo_root, ["config", "user.name", "Companion AI OS"])
        _run_git_cmd(repo_root, ["config", "user.email", "companion@local"])
        rc_c, out_c = _run_git_cmd(repo_root, ["commit", "-m", msg])
    if rc_c != 0:
        return f"error: git commit failed: {out_c}"
    _, sha = _run_git_cmd(repo_root, ["rev-parse", "--short", "HEAD"])
    return f"Committed `{sha.strip()}` on `{cur_br}` ({len(staged)} file(s)): {msg}"


def _handle_git_remote(action: str, repo_root: Path, args: Dict[str, Any]) -> str:
    if bool_arg(args, "force", False) or any(f in str_arg(args, "branch") for f in ("--force", "-f")):
        return "SAFETY GUARD REJECTED: `--force` git operations are mechanically blocked."
    proj = _active_project_record()
    policy = str(proj.get("delivery_policy") or "local_only").strip().lower()
    remote = str_arg(args, "remote", "origin") or "origin"
    cur_br = str_arg(args, "branch") or _current_branch(repo_root)
    if action == "push":
        if policy == "local_only":
            return (
                "SAFETY GUARD REJECTED: Active project `delivery_policy` is `local_only` — remote `git push` is disabled. "
                "Stop cleanly at your verified local feature branch commit."
            )
        if cur_br.lower() in _PROTECTED_BRANCHES:
            return f"SAFETY GUARD REJECTED: pushing directly to protected branch `{cur_br}` is blocked; push a feature branch (`feat/...`) instead."
        human_confirmed = bool_arg(args, "confirmed", False) and not is_in_agent_tool_turn()
        if policy == "ask_me" and not human_confirmed:
            allowed, apr = get_approval_queue(get_memory_store()).check_or_enqueue(
                category="git_push", action_name="git_push", target=f"{remote}/{cur_br}",
                summary=f"Push branch `{cur_br}` to `{remote}` in `{repo_root.name}` (`delivery_policy='ask_me'`).",
                payload={"repo_path": str(repo_root), "remote": remote, "branch": cur_br, "project_id": str(proj.get("project_id") or "")},
                requester="companion", confirmed=False, always_require_in_autonomous=True,
            )
            if not allowed:
                return f"AWAITING_USER_APPROVAL (`{apr['approval_id']}`): Pushing `{cur_br}` to `{remote}` awaits user approval (`delivery_policy='ask_me'`)."
    cred_ref = str_arg(args, "credential_ref") or str(proj.get("git_credential_ref") or "")
    with _git_auth_env(cred_ref, str(proj.get("project_id") or "default")) as (auth_env, auth_err):
        if auth_err or auth_env is None:
            return auth_err or "error: failed to prepare Git auth environment"
        git_cmd = ["fetch", remote] if action == "fetch" else (["pull", "--ff-only", remote, cur_br] if action == "pull" else ["push", "-u", remote, cur_br])
        rc, out = _run_git_cmd(repo_root, git_cmd, env=auth_env, timeout=90)
    return f"git {action} `{remote}/{cur_br}` succeeded.\n{out}".strip() if rc == 0 else f"error: git {action} failed: {out}"


def tool_git_manage(args: Dict[str, Any]) -> str:
    """Execute safe local & Blind-Vault-authenticated Git/worktree operations governed by `delivery_policy`."""
    action = str_arg(args, "action", "status").lower()
    raw_repo = str_arg(args, "repo_path", aliases=("cwd", "repo", "workdir"))
    if action == "init":
        if raw_repo and not Path(raw_repo).expanduser().is_absolute() and not raw_repo.startswith("~") and settings.get_workspace().is_self_project:
            init_target = (settings.projects_dir / raw_repo).resolve()
        else:
            init_target = _resolve_repo_arg(raw_repo)
        res = init_greenfield_repo(init_target, title=str_arg(args, "title", aliases=("name", "slug")), default_branch=str_arg(args, "branch", "main") or "main")
        return str(res["summary"]) if res.get("ok") else str(res.get("error") or "error: git init failed")
    repo_root = _resolve_repo_arg(raw_repo)
    mutating = action not in ("status", "diff", "log", "worktree_list")
    dir_err = _is_allowed_sdlc_repo_dir(repo_root, mutating=mutating)
    if dir_err:
        return dir_err
    if not (repo_root / ".git").exists():
        return f"error: `{repo_root}` is not a Git repository"

    if action == "status":
        br = _current_branch(repo_root)
        _, st = _run_git_cmd(repo_root, ["status", "--short", "--branch"])
        dp = _active_project_record().get("delivery_policy", "local_only")
        return f"Repo: `{repo_root}` | branch=`{br}` | delivery_policy=`{dp}`\n{st or '(clean)'}"
    if action == "diff":
        d_args = ["diff", "--cached"] if bool_arg(args, "staged", False) else ["diff"]
        if str_arg(args, "path", aliases=("file_path",)):
            d_args.extend(["--", str_arg(args, "path", aliases=("file_path",))])
        rc, out = _run_git_cmd(repo_root, d_args)
        return (out[: settings.out_limit] or "(no diff)") if rc == 0 else f"error: {out}"
    if action == "log":
        lim = max(1, min(int_arg(args, "limit", 10), 50))
        rc, out = _run_git_cmd(repo_root, ["log", f"-n{lim}", "--oneline", "--decorate"])
        return (out or "(no commits)") if rc == 0 else f"error: {out}"
    if action == "branch":
        raw_sl = str_arg(args, "slug", aliases=("name",))
        br = str_arg(args, "branch", aliases=("branch_name", "target_branch")) or (raw_sl if "/" in raw_sl else (f"feat/{_slugify_branch_part(raw_sl)}" if raw_sl else ""))
        if not br:
            rc, out = _run_git_cmd(repo_root, ["branch", "--list"])
            return out if rc == 0 else f"error: {out}"
        if "--force" in br or "-f" in br.split():
            return "SAFETY GUARD REJECTED: force branch overwrite is blocked."
        chk_rc, _ = _run_git_cmd(repo_root, ["show-ref", "--verify", f"refs/heads/{br}"])
        b_cmd = ["checkout", br] if chk_rc == 0 else ["checkout", "-b", br]
        rc, out = _run_git_cmd(repo_root, b_cmd)
        return f"Switched `{repo_root.name}` to branch `{br}`." if rc == 0 else f"error: {out}"
    if action in ("worktree_add", "worktree_list", "worktree_remove"):
        return _handle_git_worktree(action, repo_root, args)
    if action == "commit":
        return _handle_git_commit(repo_root, args)
    if action in ("fetch", "pull", "push"):
        return _handle_git_remote(action, repo_root, args)
    return f"error: unknown git_manage action `{action}`."


# ---------------------------------------------------------------------------
# 2. Managed Local Preview Servers & Vision Critique (`tool_dev_preview`)
# ---------------------------------------------------------------------------
@dataclass
class PreviewInstance:
    """Managed local preview server instance with OS process group, isolated temp data dir, and log ring buffer."""

    preview_id: str
    cwd: Path
    command: Any
    port: int
    url: str
    health_path: str
    temp_dir: Path
    proc: subprocess.Popen[str]
    started_at: float = field(default_factory=time.monotonic)
    turn_scope_id: int = 0
    logs: Deque[str] = field(default_factory=lambda: collections.deque(maxlen=400))


def _allocate_loopback_port(preferred: int = 0) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", preferred if preferred > 0 else 0))
        return int(s.getsockname()[1])


def _scrubbed_preview_env(port: int, temp_dir: Path, cwd: Path) -> Dict[str, str]:
    data_dir = temp_dir / "preview_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    clean = {
        k: v for k, v in os.environ.items()
        if k.upper() not in ("COMPANION_WEB_TOKEN", "COMPANION_SECRETS_PATH")
        and not k.upper().endswith(_STRIP_PREVIEW_SUFFIXES) and not k.upper().startswith("COMPANION_SECRET")
    }
    return {
        **clean, "PORT": str(port), "HOST": "127.0.0.1", "COMPANION_WEB_HOST": "127.0.0.1",
        "COMPANION_DATA_DIR": str(data_dir), "COMPANION_IMMUTABLE_APP": "1", "PYTHONPATH": str(cwd),
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1",
    }


def _auto_detect_preview_command(cwd: Path, port: int) -> Tuple[Any, bool, str]:
    override = str(_active_project_record().get("preview_command") or "").strip()
    if override:
        return override.replace("{port}", str(port)), True, "/"
    if (cwd / "harness.py").is_file() and (cwd / "surfaces" / "web_server.py").is_file():
        return [sys.executable, "harness.py", "--web", "--host", "127.0.0.1", "--port", str(port), "--no-browser"], False, "/api/health"
    if (cwd / "package.json").is_file():
        return f"npm run dev -- --host 127.0.0.1 --port {port}", True, "/"
    if (cwd / "app.py").is_file():
        return [sys.executable, "app.py"], False, "/"
    return [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"], False, "/"


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        with contextlib.suppress(Exception):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL, capture_output=True, timeout=6, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        time.sleep(0.15)
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=3)


class PreviewProcessManager:
    """Thread-safe lifecycle manager for local `dev_preview` servers with guaranteed process-group teardown."""

    def __init__(self) -> None:
        self._lock, self._instances, self._turn_counter = threading.RLock(), {}, 0

    def begin_turn_scope(self) -> int:
        with self._lock:
            self._turn_counter += 1
            return self._turn_counter

    def _spawn_log_readers(self, inst: PreviewInstance) -> None:
        def _drain(stream: Any, pfx: str) -> None:
            if stream is not None:
                try:
                    with contextlib.suppress(Exception):
                        for ln in iter(stream.readline, ""):
                            inst.logs.append(f"[{pfx}] {ln.rstrip()[:500]}")
                finally:
                    with contextlib.suppress(Exception):
                        stream.close()

        threading.Thread(target=_drain, args=(inst.proc.stdout, "out"), daemon=True).start()
        threading.Thread(target=_drain, args=(inst.proc.stderr, "err"), daemon=True).start()

    def _wait_until_ready(self, inst: PreviewInstance, timeout_s: float) -> Tuple[bool, str]:
        deadline, last_err = time.monotonic() + max(1.0, timeout_s), "not yet listening"
        probe_url = f"{inst.url}{inst.health_path if inst.health_path.startswith('/') else '/' + inst.health_path}"
        while time.monotonic() < deadline:
            if inst.proc.poll() is not None:
                return False, f"exited early with code {inst.proc.returncode}"
            try:
                with _NO_PROXY_OPENER.open(urllib.request.Request(probe_url, headers={"Host": f"127.0.0.1:{inst.port}"}), timeout=1.5) as resp:
                    if 200 <= resp.status < 500:
                        return True, f"HTTP {resp.status} on {inst.health_path}"
            except urllib.error.HTTPError as herr:
                if herr.code < 500:
                    return True, f"HTTP {herr.code} on {inst.health_path}"
                last_err = f"HTTP {herr.code}"
            except Exception as exc:
                last_err = str(exc)
                with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", inst.port), timeout=0.4):
                    if not inst.health_path or inst.health_path == "/":
                        return True, "TCP port open"
            time.sleep(0.2)
        return False, f"readiness timeout ({last_err})"

    def start(
        self, cwd: Optional[Path] = None, command: str = "", port: int = 0,
        health_path: str = "", startup_timeout: float = 20.0, turn_scope_id: int = 0,
    ) -> Dict[str, Any]:
        target_cwd = (cwd or settings.active_project_root).resolve()
        if dir_err := _is_allowed_sdlc_repo_dir(target_cwd, mutating=False):
            return {"ok": False, "error": dir_err}
        if command.strip() and (cmd_err := check_command_safety(command)):
            return {"ok": False, "error": cmd_err}
        alloc_port = _allocate_loopback_port(port)
        if command.strip():
            expanded, eff_health = command.replace("{port}", str(alloc_port)), (health_path or "/")
            use_shell = any(op in expanded for op in ("|", "&", ";", ">", "<", "\n"))
            cmd_spec = expanded if use_shell else [t.strip('"') for t in shlex.split(expanded, posix=(os.name != "nt"))]
        else:
            cmd_spec, use_shell, auto_health = _auto_detect_preview_command(target_cwd, alloc_port)
            eff_health = health_path or auto_health
        temp_dir = Path(tempfile.mkdtemp(prefix="companion_preview_"))
        popen_kw: Dict[str, Any] = {
            "cwd": str(target_cwd), "env": _scrubbed_preview_env(alloc_port, temp_dir, target_cwd),
            "shell": use_shell, "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE, "text": True, "encoding": "utf-8", "errors": "replace",
        }
        if os.name == "nt":
            popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            popen_kw["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd_spec, **popen_kw)
        except Exception as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return {"ok": False, "error": f"Failed to start preview server: {exc}"}
        pid_slug = f"prev_{uuid.uuid4().hex[:8]}"
        inst = PreviewInstance(pid_slug, target_cwd, cmd_spec, alloc_port, f"http://127.0.0.1:{alloc_port}", eff_health, temp_dir, proc, turn_scope_id=turn_scope_id)
        self._spawn_log_readers(inst)
        if (scope := get_active_cancel_scope()) is not None and scope.is_cancelled:
            _terminate_process_tree(proc)
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise InterruptedError("Preview start cancelled.")
        ready, detail = self._wait_until_ready(inst, startup_timeout)
        if not ready:
            tail = "\n".join(list(inst.logs)[-25:])
            _terminate_process_tree(proc)
            shutil.rmtree(temp_dir, ignore_errors=True)
            return {"ok": False, "error": f"Preview `{pid_slug}` failed readiness ({detail}).\nLogs:\n{tail}"}
        with self._lock:
            self._instances[pid_slug] = inst
        return {"ok": True, "preview_id": pid_slug, "url": inst.url, "port": inst.port, "pid": proc.pid, "ready": True, "detail": detail, "logs": list(inst.logs)[-15:], "summary": f"Preview `{pid_slug}` ready at `{inst.url}` (PID {proc.pid}, {detail})."}

    def get_logs(self, preview_id: str = "", tail: int = 60) -> Dict[str, Any]:
        with self._lock:
            inst = self._instances.get(preview_id) if preview_id else next(iter(self._instances.values()), None)
        if inst is None:
            return {"ok": False, "error": f"Preview `{preview_id or 'active'}` not found."}
        vault = get_vault_manager()
        lines = [vault.scrub_output(ln) for ln in list(inst.logs)[-max(1, min(tail, 400)) :]]
        return {"ok": True, "preview_id": inst.preview_id, "alive": inst.proc.poll() is None, "logs": lines, "output": "\n".join(lines) or "(no logs)"}

    def http_probe(self, preview_id: str = "", path: str = "/", method: str = "GET") -> Dict[str, Any]:
        with self._lock:
            inst = self._instances.get(preview_id) if preview_id else next(iter(self._instances.values()), None)
        if inst is None:
            return {"ok": False, "error": f"Preview `{preview_id or 'active'}` not found."}
        target_url = f"{inst.url}{path if path.startswith('/') else '/' + path}"
        t0 = time.perf_counter()
        try:
            with _NO_PROXY_OPENER.open(urllib.request.Request(target_url, method=method.upper(), headers={"Host": f"127.0.0.1:{inst.port}"}), timeout=10) as resp:
                body = get_vault_manager().scrub_output(resp.read(32_768).decode("utf-8", errors="replace"))
                ms = round((time.perf_counter() - t0) * 1000.0, 1)
                return {"ok": 200 <= resp.status < 400, "status_code": resp.status, "latency_ms": ms, "body": body[:4000], "summary": f"[HTTP {resp.status} in {ms}ms] {target_url}"}
        except urllib.error.HTTPError as herr:
            return {"ok": False, "status_code": herr.code, "error": f"HTTP {herr.code} on {target_url}: {herr.read(4096).decode('utf-8', errors='replace')[:400]}"}
        except Exception as exc:
            return {"ok": False, "error": f"Probe failed on {target_url}: {exc}"}

    def stop(self, preview_id: str = "") -> Dict[str, Any]:
        with self._lock:
            key = preview_id or (next(iter(self._instances)) if self._instances else "")
            inst = self._instances.pop(key, None) if key else None
        if inst is None:
            return {"ok": False, "error": f"Preview `{preview_id or 'active'}` not found."}
        _terminate_process_tree(inst.proc)
        shutil.rmtree(inst.temp_dir, ignore_errors=True)
        return {"ok": True, "preview_id": inst.preview_id, "stopped_pid": inst.proc.pid, "alive": inst.proc.poll() is None, "summary": f"Stopped preview `{inst.preview_id}` (PID {inst.proc.pid}) and cleaned temp state."}

    def stop_turn_previews(self, turn_scope_id: Optional[int] = None) -> List[str]:
        with self._lock:
            ids = [pid for pid, inst in self._instances.items() if turn_scope_id is None or inst.turn_scope_id == turn_scope_id]
        return [pid for pid in ids if self.stop(pid).get("ok")]

    def stop_all(self) -> Dict[str, Any]:
        stopped = self.stop_turn_previews(turn_scope_id=None)
        return {"ok": True, "stopped": stopped, "summary": f"Stopped {len(stopped)} preview server(s)."}

    def list_instances(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._instances.values())
        return [{"preview_id": i.preview_id, "url": i.url, "port": i.port, "pid": i.proc.pid, "alive": i.proc.poll() is None, "cwd": str(i.cwd), "uptime_s": round(time.monotonic() - i.started_at, 1)} for i in items]


preview_manager = PreviewProcessManager()
atexit.register(preview_manager.stop_all)
_ACTIVE_TURN_PREVIEW_SCOPE: threading.local = threading.local()


@contextlib.contextmanager
def preview_turn_cleanup_scope() -> Iterator[int]:
    """Guarantee every `dev_preview` server started during an agent turn is torn down on turn exit or `■ Stop`."""
    sid = preview_manager.begin_turn_scope()
    prev = getattr(_ACTIVE_TURN_PREVIEW_SCOPE, "sid", 0)
    _ACTIVE_TURN_PREVIEW_SCOPE.sid = sid
    try:
        yield sid
    finally:
        _ACTIVE_TURN_PREVIEW_SCOPE.sid = prev
        preview_manager.stop_turn_previews(turn_scope_id=sid)


def _critique_screenshots(raw_paths: Any, prompt: str) -> str:
    paths = [Path(str(x)).resolve() for x in (raw_paths if isinstance(raw_paths, list) else []) if str(x).strip()]
    if not paths:
        return "error: `screenshot_paths` array is required for `vision_critique`"
    uris: List[str] = []
    ingested: List[str] = []
    for p in paths[:4]:
        p_err = check_path_safety(p, write=False)
        if p_err:
            return p_err
        if not p.is_file():
            return f"error: screenshot not found: {p}"
        raw = p.read_bytes()
        info = inspect_image_bytes(raw)
        if info is None:
            return f"error: invalid image file: {p}"
        mime, w, h = info
        b64_img = base64.b64encode(raw).decode("ascii")
        att = ingest_attachment(p.name, b64_img, mime_type=mime, store=get_memory_store())
        aid = (att.get("attachment") or {}).get("id", "") if att.get("ok") else ""
        ingested.append(f"`{p.name}` ({w}x{h}" + (f", attachment=`{aid}`" if aid else "") + ")")
        uris.append(f"data:{mime};base64,{b64_img}")
    ask = prompt.strip() or "Inspect these Desktop and Mobile UI screenshots for layout overlap, broken contrast, missing controls, or visible error banners."
    try:
        with wire_attachment_context(image_data_uris=uris), tier_context("image"):
            resp = chat([{"role": "system", "content": "You are a precise UI QA visual inspector."}, {"role": "user", "content": ask}], None, "none")
        txt, _, _ = normalize_assistant_message((resp.get("choices") or [{}])[0].get("message") or {})
        return f"Verified {len(uris)} screenshot(s): {', '.join(ingested)}\n{txt.strip()}"
    except Exception as exc:
        return f"Verified {len(uris)} screenshot(s) ({', '.join(ingested)}); vision model critique unavailable ({exc})."


def tool_dev_preview(args: Dict[str, Any]) -> str:
    """Start, inspect logs, HTTP-probe, visually critique, or tear down isolated local `dev_preview` servers."""
    action = str_arg(args, "action", "status").lower()
    if action == "start":
        cwd_str = str_arg(args, "cwd", aliases=("repo_path", "workdir"))
        sid = int(getattr(_ACTIVE_TURN_PREVIEW_SCOPE, "sid", 0) or 0)
        res = preview_manager.start(
            cwd=_resolve_repo_arg(cwd_str) if cwd_str else None, command=str_arg(args, "command", aliases=("cmd",)),
            port=int_arg(args, "port", 0), health_path=str_arg(args, "health_path", aliases=("ready_path", "path")),
            startup_timeout=float(int_arg(args, "timeout", 20)), turn_scope_id=sid,
        )
        return json.dumps(res) if res.get("ok") else f"error: {res.get('error')}"
    if action in ("status", "list"):
        return json.dumps({"ok": True, "previews": preview_manager.list_instances()})
    if action == "logs":
        res = preview_manager.get_logs(str_arg(args, "preview_id"), tail=int_arg(args, "tail", 60))
        return str(res["output"]) if res.get("ok") else f"error: {res.get('error')}"
    if action == "http_probe":
        res = preview_manager.http_probe(str_arg(args, "preview_id"), path=str_arg(args, "path", "/"), method=str_arg(args, "method", "GET"))
        return f"{res['summary']}\n{res.get('body', '')}".strip() if res.get("ok") else f"error: {res.get('error')}"
    if action == "vision_critique":
        return _critique_screenshots(args.get("screenshot_paths"), str_arg(args, "prompt"))
    if action == "stop":
        res = preview_manager.stop(str_arg(args, "preview_id"))
        return str(res["summary"]) if res.get("ok") else f"error: {res.get('error')}"
    if action == "stop_all":
        return str(preview_manager.stop_all()["summary"])
    return f"error: unknown dev_preview action `{action}`."


# ---------------------------------------------------------------------------
# 3. Blind-Vault GitHub Pull Request & Actions CI Client (Zero Skill Coupling)
# ---------------------------------------------------------------------------
def _github_request(
    method: str,
    api_base: str,
    path: str,
    token: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Any:
    url = api_base.rstrip("/") + "/" + path.lstrip("/")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Companion-SDLC/4.1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    opener = _NO_PROXY_OPENER if host in ("127.0.0.1", "localhost", "::1") else urllib.request.build_opener()
    with opener.open(req, timeout=20.0) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {"ok": True}


def _resolve_github_token(credential_ref: str, vault: Optional[VaultManager] = None) -> Tuple[str, VaultManager]:
    vm = vault or get_vault_manager()
    ok, tok_or_err = vm.resolve_for_call(credential_ref, caller_scope="skill:github-workflow")
    if not ok:
        raise RuntimeError(tok_or_err)
    return tok_or_err, vm


def upsert_pull_request(
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str,
    credential_ref: str,
    *,
    api_base: str = "https://api.github.com",
    vault: Optional[VaultManager] = None,
) -> Dict[str, Any]:
    """Idempotently create or update an open GitHub Pull Request for `head -> base`."""
    if "/" not in repo or head in ("main", "master"):
        return {"ok": False, "error": f"Invalid repo `{repo}` or protected head branch `{head}`."}
    owner = repo.split("/", 1)[0]
    token, vm = _resolve_github_token(credential_ref, vault)
    try:
        q = urllib.parse.urlencode({"head": f"{owner}:{head}", "base": base, "state": "open"})
        existing = _github_request("GET", api_base, f"/repos/{repo}/pulls?{q}", token)
        if isinstance(existing, list) and existing:
            pr_num = int(existing[0]["number"])
            pr = _github_request("PATCH", api_base, f"/repos/{repo}/pulls/{pr_num}", token, {"title": title, "body": body})
            action = "updated"
        else:
            pr = _github_request("POST", api_base, f"/repos/{repo}/pulls", token, {"title": title, "body": body, "head": head, "base": base})
            action = "created"
        out = {"ok": True, "action": action, "number": pr.get("number"), "html_url": pr.get("html_url", ""), "state": pr.get("state", "open")}
        return json.loads(vm.scrub_output(json.dumps(out)))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:400]
        return {"ok": False, "error": vm.scrub_output(f"GitHub HTTP {exc.code}: {err_body}")}
    except Exception as exc:
        return {"ok": False, "error": vm.scrub_output(f"{type(exc).__name__}: {exc}")}


def inspect_check_runs(
    repo: str,
    git_ref: str,
    credential_ref: str,
    *,
    api_base: str = "https://api.github.com",
    vault: Optional[VaultManager] = None,
) -> Dict[str, Any]:
    """Inspect GitHub Actions check-runs for `git_ref`."""
    token, vm = _resolve_github_token(credential_ref, vault)
    try:
        res = _github_request("GET", api_base, f"/repos/{repo}/commits/{git_ref}/check-runs", token)
        runs = res.get("check_runs") or []
        passed = [r["name"] for r in runs if r.get("conclusion") in ("success", "neutral", "skipped")]
        failed = [{"name": r.get("name"), "url": r.get("html_url", "")} for r in runs if r.get("conclusion") in ("failure", "timed_out", "cancelled", "action_required")]
        pending = [r["name"] for r in runs if r.get("status") != "completed"]
        out = {"ok": len(failed) == 0, "ref": git_ref, "total": len(runs), "passed": passed, "failed": failed, "pending": pending}
        return json.loads(vm.scrub_output(json.dumps(out)))
    except Exception as exc:
        return {"ok": False, "error": vm.scrub_output(f"{type(exc).__name__}: {exc}")}


def merge_pull_request(
    repo: str,
    pr_number: int,
    credential_ref: str,
    *,
    merge_method: str = "squash",
    approved: bool = False,
    api_base: str = "https://api.github.com",
    vault: Optional[VaultManager] = None,
) -> Dict[str, Any]:
    """Merge an approved Pull Request after human gate confirmation."""
    if not approved:
        return {"ok": False, "error": "PR merge requires explicit human gate approval (`--approved`)."}
    token, vm = _resolve_github_token(credential_ref, vault)
    try:
        res = _github_request(
            "PUT", api_base, f"/repos/{repo}/pulls/{int(pr_number)}/merge", token,
            {"merge_method": merge_method if merge_method in ("squash", "merge", "rebase") else "squash"},
        )
        out = {"ok": bool(res.get("merged", True)), "sha": res.get("sha", ""), "message": res.get("message", "Merged")}
        return json.loads(vm.scrub_output(json.dumps(out)))
    except Exception as exc:
        return {"ok": False, "error": vm.scrub_output(f"{type(exc).__name__}: {exc}")}


# ---------------------------------------------------------------------------
# 4. Isolated Git Worktree Card Orchestrator & Parallel Subagents
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slugify_part(raw: str, fallback: str = "slice", max_len: int = 36) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")[:max_len].strip("-") or fallback


def _scrub_remote_url(raw_url: str) -> str:
    clean = (raw_url or "").strip()
    return re.sub(r"://[^/@\s]+@", "://", clean) if "://" in clean else clean


def _detect_stack_and_rules(root: Path) -> Tuple[str, List[str]]:
    stacks: List[str] = []
    if any((root / m).exists() for m in ("pyproject.toml", "requirements.txt", "harness.py", "tests")):
        stacks.append("Python")
    if (root / "package.json").is_file() or (root / "frontend" / "package.json").is_file():
        stacks.append("Frontend" if (root / "frontend" / "package.json").is_file() else "Node")
    if (root / "Cargo.toml").is_file():
        stacks.append("Rust")
    if (root / "go.mod").is_file():
        stacks.append("Go")
    if list(root.glob("*.sln")) or list(root.glob("*.csproj")):
        stacks.append(".NET")
    rules = [f".agents/rules/{f.name}" for f in sorted((root / ".agents" / "rules").glob("*.md"))[:8]] if (root / ".agents" / "rules").is_dir() else []
    rules.extend(doc for doc in ("AGENTS.md", "CLAUDE.md") if (root / doc).is_file())
    return (" + ".join(stacks) if stacks else "General"), rules


def probe_repo_workspace(path_str: str) -> Dict[str, Any]:
    """Inspect a candidate repository path without mutating anything (powers UI `Check Repo`)."""
    raw = (path_str or "").strip()
    proj_dir = str(settings.projects_dir)
    if not raw:
        return {"ok": False, "exists": False, "is_git_repo": False, "projects_dir": proj_dir, "error": "Repository path is required."}
    cand = Path(raw).expanduser()
    target = cand.resolve() if (cand.is_absolute() or raw.startswith("~")) else (settings.projects_dir / cand).resolve()
    bound_err = validate_external_repo_path(target)
    if bound_err:
        return {
            "ok": False, "exists": target.exists(), "is_git_repo": False, "can_init": False,
            "repo_root": str(target), "projects_dir": proj_dir, "error": bound_err, "summary": bound_err,
        }
    base_res = {"ok": True, "repo_root": str(target), "projects_dir": proj_dir, "branch": "", "clean": True, "changed_count": 0, "remote_url": ""}
    if not target.exists() or not target.is_dir():
        return {**base_res, "exists": False, "is_git_repo": False, "can_init": True, "stack_summary": "", "rules_files": [], "summary": f"Path ready — will initialize new Git repo on main at `{target}`."}
    stack_sum, rules_files = _detect_stack_and_rules(target)
    if not (target / ".git").exists():
        return {**base_res, "exists": True, "is_git_repo": False, "can_init": True, "stack_summary": stack_sum, "rules_files": rules_files, "summary": f"Folder exists (not a Git repository) · {stack_sum}"}
    branch = _current_branch(target)
    if branch == "HEAD":
        rc_sym, sym_out = _run_git_cmd(target, ["symbolic-ref", "--short", "HEAD"])
        branch = sym_out.strip() if rc_sym == 0 and sym_out.strip() else "main"
    rc_st, st_out = _run_git_cmd(target, ["status", "--porcelain=v1"])
    changed = [ln for ln in (st_out.splitlines() if rc_st == 0 else []) if ln.strip()]
    rc_rem, rem_out = _run_git_cmd(target, ["remote", "get-url", "origin"])
    remote_url = _scrub_remote_url(rem_out.splitlines()[0]) if rc_rem == 0 and rem_out.strip() else ""
    rules_note = f" · {len(rules_files)} rules file{'s' if len(rules_files) != 1 else ''}" if rules_files else ""
    clean_lbl = "Clean" if not changed else f"{len(changed)} changed"
    return {
        "ok": True, "exists": True, "is_git_repo": True, "can_init": True, "repo_root": str(target), "projects_dir": proj_dir,
        "branch": branch, "clean": not changed, "changed_count": len(changed), "remote_url": remote_url,
        "stack_summary": stack_sum, "rules_files": rules_files,
        "summary": f"Git repo on {branch} · {clean_lbl} · {stack_sum}{rules_note}",
    }


def _detect_base_branch(repo_root: Path) -> str:
    cur = _current_branch(repo_root)
    if cur not in ("HEAD", "") and not cur.startswith(("feat/", "fix/", "wt/")):
        return cur
    for cand in ("main", "master"):
        if _run_git_cmd(repo_root, ["show-ref", "--verify", f"refs/heads/{cand}"])[0] == 0:
            return cand
    return cur if cur not in ("HEAD", "") else "main"


def parse_unified_git_diff(unified_diff: str, base_label: str = "main", head_label: str = "HEAD") -> Dict[str, Any]:
    """Convert unified git diff text into `WikiDiffPayload` format for `<DiffView>` rendering."""
    files: List[Dict[str, Any]] = []
    cur_file: Optional[Dict[str, Any]] = None
    old_ln, new_ln, total_add, total_del = 1, 1, 0, 0
    hunk_re = re.compile(r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@")
    for raw_ln in (unified_diff or "").splitlines():
        if raw_ln.startswith("diff --git "):
            if cur_file is not None:
                files.append(cur_file)
            parts = raw_ln.split()
            rel = parts[-1].removeprefix("b/") if len(parts) >= 4 else "file"
            cur_file = {"title": rel, "old_label": f"{base_label}:{rel}", "new_label": f"{head_label}:{rel}", "additions": 0, "deletions": 0, "identical": False, "unified_diff": "", "rows": []}
        elif cur_file is None or raw_ln.startswith(("index ", "--- ", "+++ ", "new file ", "deleted file ")):
            continue
        elif hm := hunk_re.match(raw_ln):
            old_ln, new_ln = int(hm.group(1)), int(hm.group(2))
            cur_file["rows"].append({"kind": "hunk", "old_ln": None, "new_ln": None, "old_text": raw_ln, "new_text": raw_ln})
        elif raw_ln.startswith("+"):
            cur_file["additions"] += 1
            total_add += 1
            cur_file["rows"].append({"kind": "add", "old_ln": None, "new_ln": new_ln, "old_text": "", "new_text": raw_ln[1:]})
            new_ln += 1
        elif raw_ln.startswith("-"):
            cur_file["deletions"] += 1
            total_del += 1
            cur_file["rows"].append({"kind": "del", "old_ln": old_ln, "new_ln": None, "old_text": raw_ln[1:], "new_text": ""})
            old_ln += 1
        elif raw_ln.startswith(" "):
            cur_file["rows"].append({"kind": "context", "old_ln": old_ln, "new_ln": new_ln, "old_text": raw_ln[1:], "new_text": raw_ln[1:]})
            old_ln, new_ln = old_ln + 1, new_ln + 1
    if cur_file is not None:
        files.append(cur_file)
    return {"ok": True, "mode": "worktree_diff", "files": files, "total_additions": total_add, "total_deletions": total_del, "unified_diff": unified_diff, "summary": f"{len(files)} file(s) changed (+{total_add} / -{total_del})"}


def _extract_test_and_screenshots(raw_text: str, worktree_path: Optional[Path], store: MemoryStore) -> Tuple[str, List[str]]:
    test_lines = [ln.strip() for ln in (raw_text or "").splitlines() if any(k in ln for k in ("Ran ", "PASSED", "FAILED", "OK", "Test Files", "Tests ", "exit=0", "HEALTHY (PASS)"))]
    seen_ids = list(dict.fromkeys(_ATT_ID_RE.findall(raw_text or "")))
    for raw_img in _IMG_PATH_RE.findall(raw_text or "")[:4]:
        cand = Path(raw_img) if Path(raw_img).is_absolute() or worktree_path is None else (worktree_path / raw_img).resolve()
        if cand.is_file() and cand.stat().st_size <= 10_000_000:
            b64 = base64.b64encode(cand.read_bytes()).decode("ascii")
            aid = str((ingest_attachment(cand.name, b64, store=store).get("attachment") or {}).get("id") or "")
            if aid and aid not in seen_ids:
                seen_ids.append(aid)
    return ("\n".join(test_lines[-8:])[:800] if test_lines else ""), seen_ids[:6]


def _autocommit_clean_worktree_edits(worktree_path: Path, project_id: str, item_id: str, title: str, store: MemoryStore) -> None:
    rc_st, st_out = _run_git_cmd(worktree_path, ["status", "--porcelain=v1"])
    cand_rels = [
        entry for ln in (st_out.splitlines() if rc_st == 0 else [])
        if (m := re.match(r"^[MADRCU?! ]{1,2}\s+(.+)$", ln))
        and (entry := m.group(1).split(" -> ")[-1].strip().strip('"'))
        and not is_ephemeral_cache_path(entry)
        and check_git_stage_safety(worktree_path, [entry]) is None
    ]
    if cand_rels:
        with workspace_context(worktree_path, project_id), memory_store_context(store):
            tool_git_manage({"action": "commit", "repo_path": str(worktree_path), "paths": cand_rels, "message": f"feat({item_id}): {title[:64]}"})


def collect_worktree_deliverable_receipt(
    base_repo: Path, worktree_path: Path, branch_name: str,
    item: Dict[str, Any], job_row: Dict[str, Any], store: MemoryStore,
) -> Dict[str, Any]:
    """Auto-commit clean edits on `branch_name` and build the `deliverable_json` diff & screenshot receipt."""
    for p in preview_manager.list_instances():
        with contextlib.suppress(Exception):
            if Path(str(p.get("cwd") or "")).resolve() == worktree_path.resolve():
                preview_manager.stop(str(p["preview_id"]))
    base_branch = _detect_base_branch(base_repo)
    _autocommit_clean_worktree_edits(worktree_path, str(item.get("project_id") or ""), item["item_id"], item["title"], store)
    _, sha_out = _run_git_cmd(worktree_path, ["rev-parse", "--short", "HEAD"])
    _, names_out = _run_git_cmd(worktree_path, ["diff", "--name-only", f"{base_branch}...HEAD"])
    _, stat_out = _run_git_cmd(worktree_path, ["diff", "--stat", f"{base_branch}...HEAD"])
    _, diff_out = _run_git_cmd(worktree_path, ["diff", f"{base_branch}...HEAD"])
    rc_rem, rem_out = _run_git_cmd(base_repo, ["remote", "get-url", "origin"])
    remote_url = _scrub_remote_url(rem_out.splitlines()[0]) if rc_rem == 0 and rem_out.strip() else ""
    changed_files = [ln.strip() for ln in names_out.splitlines() if ln.strip()]
    capped_diff = (diff_out or "")[:_DIFF_CAP_CHARS]
    worker_txt = str(job_row.get("full_output") or job_row.get("envelope_summary") or job_row.get("partial_output") or "").strip()
    test_sum, shot_ids = _extract_test_and_screenshots(f"{worker_txt}\n{job_row.get('partial_output') or ''}", worktree_path, store)
    prev_deliv = dict(item.get("deliverable") or {})
    return {
        **prev_deliv, "kind": "code_worktree", "worker_status": str(job_row.get("status") or "done"),
        "branch_name": branch_name, "base_branch": base_branch, "worktree_path": str(worktree_path),
        "commit_sha": sha_out.strip(), "changed_files": changed_files, "diff_stat": stat_out.strip(),
        "unified_diff": capped_diff, "diff_payload": parse_unified_git_diff(capped_diff, base_label=base_branch, head_label=branch_name),
        "summary": worker_txt[:3000] or str(job_row.get("error") or ""), "test_summary": test_sum,
        "screenshot_ids": shot_ids or list(prev_deliv.get("screenshot_ids") or []),
        "job_id": str(job_row.get("job_id") or ""), "has_remote": bool(remote_url), "remote_url": remote_url,
        "error": str(job_row.get("error") or ""), "updated_at": _now_iso(),
    }


def _build_card_worker_goal(
    proj: Dict[str, Any], item: Dict[str, Any], is_code_repo: bool,
    worktree_path: Optional[Path], branch_name: str, feedback: str,
) -> str:
    fb_blk = f"\n\n[HUMAN REVIEW REVISION FEEDBACK]\n{feedback.strip()}\nAddress every point above on this existing branch and re-verify." if feedback.strip() else ""
    crit_blk = f"\nAcceptance Criteria: {item['acceptance_criteria']}" if item.get("acceptance_criteria") else ""
    desc_blk = f"\nDetails:\n{item['description']}" if item.get("description") else ""
    if not is_code_repo or worktree_path is None:
        return f"Execute Board Card `{item['item_id']}` in Project `{proj['title']}`:\nTitle: {item['title']}{desc_blk}{crit_blk}{fb_blk}\n\nProduce a clear, actionable Markdown deliverable summary."
    repo_ctx = discover_repo_sdlc_context(worktree_path, proj)
    return (
        f"[ISOLATED GIT WORKTREE CARD EXECUTION]\nProject: `{proj['title']}` (`delivery_policy={proj.get('delivery_policy', 'local_only')}`)\n"
        f"Card `{item['item_id']}`: {item['title']}{desc_blk}{crit_blk}\nIsolated Worktree Root: `{worktree_path}` (branch `{branch_name}`)\n{repo_ctx}{fb_blk}\n\n"
        "Execution Rules:\n1. Work strictly inside your isolated worktree root (`read_file`, `edit_file`, `write_file`, `run_python`, `run_shell`).\n"
        "2. Never switch to `main`/`master` or push to remotes unless explicitly instructed.\n"
        "3. Run relevant unit tests (and `dev_preview` + `browser-dogfood` if changing Web UI) before finishing.\n"
        "4. Stage and commit your verified changes cleanly onto this feature branch via `git_manage(action='commit', ...)` and summarize test results."
    )


def _prepare_item_worktree(proj: Dict[str, Any], item: Dict[str, Any], base_repo: Path) -> Tuple[Optional[Path], str, str]:
    if _run_git_cmd(base_repo, ["rev-parse", "--verify", "HEAD"])[0] != 0:
        return None, "", f"Repository `{base_repo}` has no initial commit yet — commit at least once on `main` first."
    slug = _slugify_part(item["title"], fallback="card", max_len=28)
    short_id = re.sub(r"[^a-z0-9]+", "", item["item_id"].lower())[-6:] or "wi"
    existing_wt, existing_br = str(item.get("worktree_path") or "").strip(), str(item.get("branch_name") or "").strip()
    branch_name = existing_br or f"feat/{slug}-{short_id}"
    wt_dir = (
        Path(existing_wt).resolve()
        if existing_wt and Path(existing_wt).resolve() != settings.root.resolve()
        else (base_repo.parent / ".worktrees" / _slugify_part(proj["project_id"], "proj", 32) / f"{short_id}-{slug}").resolve()
    )
    if (wt_dir / ".git").exists():
        return wt_dir, _current_branch(wt_dir) or branch_name, ""
    with workspace_context(base_repo, proj["project_id"]):
        wt_out = tool_git_manage({"action": "worktree_add", "repo_path": str(base_repo), "branch": branch_name, "worktree_path": str(wt_dir)})
    return (None, "", wt_out) if wt_out.startswith(("error:", "SAFETY GUARD REJECTED")) else (wt_dir, branch_name, "")


def dispatch_work_item_agent(
    item_id: str, project_id: str = "", feedback: str = "", run_in_background: bool = True,
    store: Optional[MemoryStore] = None, chat_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None, job_mgr: Optional[BackgroundJobManager] = None,
) -> Dict[str, Any]:
    """Start (or iterate on) a Board `WorkItem` inside an isolated git worktree or Personal OS worker."""
    mem = store or get_memory_store()
    pg = get_project_graph(mem)
    item = pg.get_item(item_id)
    proj = pg.get_project(project_id or item["project_id"])
    jm = job_mgr or BackgroundJobManager(store=mem, chat_fn=chat_fn or chat, tool_fn=tool_fn)
    if item.get("active_job_id"):
        cur_job = jm.get_job(str(item["active_job_id"]))
        if cur_job and cur_job.get("status") in ("queued", "running"):
            return {"ok": False, "error": f"Card `{item['item_id']}` already has an active worker (`{cur_job['job_id']}`)."}

    ws_str = str(proj.get("workspace_path") or "").strip()
    base_repo = Path(ws_str).resolve() if ws_str else None
    is_code_repo = bool(base_repo and base_repo.is_dir() and (base_repo / ".git").exists())
    wt_dir, branch_name = (None, "")
    if is_code_repo and base_repo is not None:
        wt_dir, branch_name, wt_err = _prepare_item_worktree(proj, item, base_repo)
        if wt_err or wt_dir is None:
            return {"ok": False, "error": wt_err or "Failed to prepare git worktree."}

    job_id = f"bgj_{int(time.time() * 1000)}_{os.urandom(3).hex()}"
    prev_deliv = dict(item.get("deliverable") or {})
    if feedback.strip():
        prev_deliv["revision_history"] = [*list(prev_deliv.get("revision_history") or []), {"feedback": feedback.strip(), "requested_at": _now_iso()}]
    pg.update_item(item["item_id"], status="in_progress", active_job_id=job_id, worktree_path=str(wt_dir) if wt_dir else "", branch_name=branch_name, deliverable=prev_deliv)
    goal_txt = _build_card_worker_goal(proj, item, is_code_repo, wt_dir, branch_name, feedback)

    def _on_done(done_job: Dict[str, Any]) -> None:
        st_j = str(done_job.get("status") or "done")
        fresh_item = pg.get_item(item["item_id"])
        if st_j == "cancelled":
            pg.update_item(item["item_id"], status="verify" if (fresh_item.get("deliverable") or {}).get("unified_diff") else "ready", active_job_id="")
            return
        try:
            if is_code_repo and base_repo is not None and wt_dir is not None:
                receipt = collect_worktree_deliverable_receipt(base_repo, wt_dir, branch_name, fresh_item, done_job, mem)
            else:
                out_s = str(done_job.get("full_output") or done_job.get("envelope_summary") or done_job.get("error") or "").strip()
                receipt = {**dict(fresh_item.get("deliverable") or {}), "kind": "personal_deliverable", "worker_status": st_j, "summary": out_s[:4000], "job_id": job_id, "error": str(done_job.get("error") or ""), "updated_at": _now_iso()}
        except Exception as exc:
            receipt = {**dict(fresh_item.get("deliverable") or {}), "worker_status": "failed", "summary": str(done_job.get("full_output") or ""), "error": f"{type(exc).__name__}: {exc}", "updated_at": _now_iso()}
        next_st = "verify" if st_j == "done" or receipt.get("changed_files") else "ready"
        pg.update_item(item["item_id"], status=next_st, active_job_id="", verification_result=str(receipt.get("summary") or "")[:500], deliverable=receipt)

    exec_root = wt_dir if (is_code_repo and wt_dir is not None) else settings.scratch_dir
    exec_root.mkdir(parents=True, exist_ok=True)
    with workspace_context(exec_root, proj["project_id"]), memory_store_context(mem):
        j_res = jm.dispatch_job(
            goal=goal_txt, title=f"[{branch_name or 'Card'}] {item['title'][:72]}", kind="worker", starter="worktree",
            session_id="board_studio", tier="coding" if is_code_repo else "fast",
            allowed_tools=list(WORKTREE_CODE_TOOLS if is_code_repo else _PERSONAL_WORKER_TOOLS),
            max_iters=20 if is_code_repo else 10, timeout_sec=900,
            run_in_background=run_in_background, on_complete=_on_done, job_id_override=job_id,
        )
    updated_item = pg.get_item(item["item_id"])
    return {
        "ok": bool(j_res.get("ok", True)), "job_id": job_id, "branch_name": branch_name,
        "worktree_path": str(wt_dir) if wt_dir else "", "item": updated_item, "card": updated_item,
        "summary": f"Started `{item['title']}`" + (f" in worktree `{branch_name}` (`{job_id}`)." if branch_name else f" (`{job_id}`)."),
    }


def dispatch_parallel_ready_items(
    project_id: str = "", max_workers: int = 0, run_in_background: bool = True,
    store: Optional[MemoryStore] = None, chat_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None, job_mgr: Optional[BackgroundJobManager] = None,
) -> Dict[str, Any]:
    """Fan out up to `project.max_parallel_workers` unblocked `Ready` Companion cards in isolated worktrees."""
    mem = store or get_memory_store()
    pg = get_project_graph(mem)
    proj = pg.get_project(project_id)
    jm = job_mgr or BackgroundJobManager(store=mem, chat_fn=chat_fn or chat, tool_fn=tool_fn)
    cap = max(1, min(4, int(max_workers or proj.get("max_parallel_workers") or 2)))
    active_running = sum(
        1 for it in pg.list_items(project_id=proj["project_id"])
        if it["status"] == "in_progress" and it.get("active_job_id")
        and (jm.get_job(str(it["active_job_id"])) or {}).get("status") in ("queued", "running")
    )
    slots = max(0, cap - active_running)
    if slots == 0:
        return {"ok": False, "dispatched": [], "error": f"Already running {active_running}/{cap} parallel agents on `{proj['title']}`."}
    ready = pg.next_ready_companion_items(proj["project_id"], limit=slots)
    if not ready:
        return {"ok": False, "dispatched": [], "error": f"No unblocked Ready Companion items in `{proj['title']}`."}

    if run_in_background:
        results = [dispatch_work_item_agent(it["item_id"], project_id=proj["project_id"], run_in_background=True, store=mem, chat_fn=chat_fn, tool_fn=tool_fn, job_mgr=jm) for it in ready]
    else:
        with ThreadPoolExecutor(max_workers=len(ready)) as pool:
            futs = [pool.submit(lambda t=it: contextvars.Context().run(lambda: dispatch_work_item_agent(t["item_id"], project_id=proj["project_id"], run_in_background=False, store=mem, chat_fn=chat_fn, tool_fn=tool_fn, job_mgr=jm))) for it in ready]
            results = [f.result() for f in futs]
    ok_runs = [r for r in results if r.get("ok")]
    return {
        "ok": len(ok_runs) > 0, "count": len(ok_runs), "dispatched": results,
        "items": [r["item"] for r in ok_runs if "item" in r],
        "summary": f"Dispatched {len(ok_runs)} parallel agent(s) across isolated worktrees on `{proj['title']}`.",
    }


def merge_worktree_into_base(item_id: str, store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """Merge a verified worktree branch cleanly into local base (`main`/`master`), clean up worktree, and unblock dependents."""
    mem = store or get_memory_store()
    pg = get_project_graph(mem)
    item = pg.get_item(item_id)
    proj = pg.get_project(item["project_id"])
    ws_str = str(proj.get("workspace_path") or "").strip()
    branch = str(item.get("branch_name") or (item.get("deliverable") or {}).get("branch_name") or "").strip()
    if not ws_str or not branch:
        pre_blk = {it["item_id"] for it in pg.list_items(project_id=proj["project_id"]) if it["column_key"] == "blocked"}
        done_item = pg.update_item(item["item_id"], status="done", active_job_id="")
        unblocked = [it["item_id"] for it in pg.list_items(project_id=proj["project_id"]) if it["item_id"] in pre_blk and it["column_key"] == "ready"]
        return {"ok": True, "item": done_item, "card": done_item, "unblocked_ids": unblocked, "summary": f"Completed `{item['title']}`."}

    base_repo = Path(ws_str).resolve()
    if not (base_repo / ".git").exists() or base_repo == settings.root.resolve():
        return {"ok": False, "error": f"Base repository `{base_repo}` is invalid or immutable."}
    wt_str = str(item.get("worktree_path") or "").strip()
    if wt_str and (Path(wt_str) / ".git").exists():
        _autocommit_clean_worktree_edits(Path(wt_str).resolve(), proj["project_id"], item["item_id"], item["title"], mem)

    rc_st, st_out = _run_git_cmd(base_repo, ["status", "--porcelain=v1"])
    if rc_st != 0 or any(ln.strip() for ln in st_out.splitlines()):
        return {"ok": False, "error": f"Base repository `{base_repo}` has uncommitted changes; commit or stash them before merging `{branch}`."}
    base_branch = str((item.get("deliverable") or {}).get("base_branch") or _detect_base_branch(base_repo))
    if _current_branch(base_repo) != base_branch:
        rc_co, out_co = _run_git_cmd(base_repo, ["checkout", base_branch])
        if rc_co != 0:
            return {"ok": False, "error": f"Could not checkout `{base_branch}` in `{base_repo}`: {out_co}"}

    merge_msg = f"Merge {branch}: {item['title'][:64]}"
    rc_m, out_m = _run_git_cmd(base_repo, ["merge", "--no-ff", "-m", merge_msg, branch])
    if rc_m != 0 and any(k in out_m for k in ("user.email", "user.name", "Please tell me who you are")):
        _run_git_cmd(base_repo, ["config", "user.name", "Companion AI OS"])
        _run_git_cmd(base_repo, ["config", "user.email", "companion@local"])
        rc_m, out_m = _run_git_cmd(base_repo, ["merge", "--no-ff", "-m", merge_msg, branch])
    if rc_m != 0:
        _run_git_cmd(base_repo, ["merge", "--abort"])
        return {"ok": False, "error": f"Merge conflict merging `{branch}` into `{base_branch}`: {out_m[:320]}"}

    _, merged_sha = _run_git_cmd(base_repo, ["rev-parse", "--short", "HEAD"])
    if wt_str and Path(wt_str).resolve() not in (base_repo, settings.root.resolve()):
        _run_git_cmd(base_repo, ["worktree", "remove", "--force", str(Path(wt_str).resolve())])
        _run_git_cmd(base_repo, ["worktree", "prune"])
        shutil.rmtree(Path(wt_str).resolve(), ignore_errors=True)
    _run_git_cmd(base_repo, ["branch", "-d", branch])

    deliv = {**dict(item.get("deliverable") or {}), "merged": True, "merged_commit": merged_sha.strip(), "merged_into": base_branch, "merged_at": _now_iso()}
    pre_blocked = {it["item_id"] for it in pg.list_items(project_id=proj["project_id"]) if it["column_key"] == "blocked"}
    done_item = pg.update_item(item["item_id"], status="done", active_job_id="", worktree_path="", deliverable=deliv)
    now_ready = [it["item_id"] for it in pg.list_items(project_id=proj["project_id"]) if it["item_id"] in pre_blocked and it["column_key"] == "ready"]
    unblk_note = f" (unblocked {len(now_ready)} dependent card(s))" if now_ready else ""
    return {
        "ok": True, "merged_commit": merged_sha.strip(), "base_branch": base_branch,
        "unblocked_ids": now_ready, "item": done_item, "card": done_item,
        "summary": f"Merged `{branch}` into `{base_branch}` (`{merged_sha.strip()}`) and completed `{item['title']}`{unblk_note}.",
    }


def _parse_github_owner_repo(remote_url: str) -> str:
    m = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", (remote_url or "").strip())
    return m.group(1) if m else ""


def push_worktree_branch_and_pr(
    item_id: str, store: Optional[MemoryStore] = None, api_base: str = "https://api.github.com",
) -> Dict[str, Any]:
    """Push verified worktree branch using Blind Vault auth and open/update a GitHub PR natively."""
    mem = store or get_memory_store()
    pg = get_project_graph(mem)
    item = pg.get_item(item_id)
    proj = pg.get_project(item["project_id"])
    if str(proj.get("delivery_policy") or "local_only") == "local_only":
        return {"ok": False, "error": "Delivery Policy is `Local Only` — switch to `Ask Before Push` or `Remote Branch + PR` in Project Settings first."}
    ws_str, branch = str(proj.get("workspace_path") or "").strip(), str(item.get("branch_name") or "").strip()
    if not ws_str or not branch:
        return {"ok": False, "error": "Card has no bound repository or feature branch to push."}
    base_repo = Path(ws_str).resolve()
    wt_str = str(item.get("worktree_path") or "").strip()
    git_dir = Path(wt_str).resolve() if wt_str and (Path(wt_str) / ".git").exists() else base_repo
    cred_ref = str(proj.get("git_credential_ref") or "").strip()
    with workspace_context(base_repo, proj["project_id"]), memory_store_context(mem):
        push_out = tool_git_manage({"action": "push", "repo_path": str(git_dir), "branch": branch, "credential_ref": cred_ref, "confirmed": not is_in_agent_tool_turn()})
    if push_out.startswith(("error:", "SAFETY GUARD REJECTED", "AWAITING_USER_APPROVAL")):
        return {"ok": False, "error": push_out}
    deliv = {**dict(item.get("deliverable") or {}), "pushed": True}
    rc_rem, rem_out = _run_git_cmd(base_repo, ["remote", "get-url", "origin"])
    gh_repo = _parse_github_owner_repo(rem_out.splitlines()[0] if rc_rem == 0 and rem_out.strip() else "")
    pr_note = ""
    if gh_repo and cred_ref:
        base_br = str(deliv.get("base_branch") or _detect_base_branch(base_repo))
        with memory_store_context(mem):
            pr_res = upsert_pull_request(gh_repo, branch, base_br, item["title"], str(deliv.get("summary") or item["description"]), cred_ref, api_base=api_base)
        if pr_res.get("ok"):
            deliv["pr_url"], deliv["pr_number"] = str(pr_res.get("html_url") or ""), pr_res.get("number")
            pr_note = f" and {pr_res.get('action', 'opened')} PR #{pr_res.get('number')} ({deliv['pr_url']})"
    upd_item = pg.update_item(item["item_id"], deliverable=deliv)
    return {"ok": True, "item": upd_item, "card": upd_item, "pr_url": deliv.get("pr_url", ""), "summary": f"Pushed `{branch}` to `origin`{pr_note}."}


def request_worktree_changes(
    item_id: str, feedback: str, run_in_background: bool = True,
    store: Optional[MemoryStore] = None, chat_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None, job_mgr: Optional[BackgroundJobManager] = None,
) -> Dict[str, Any]:
    """Keep the existing worktree & feature branch intact and dispatch a revision pass with human feedback."""
    clean_fb = (feedback or "").strip()
    if not clean_fb:
        return {"ok": False, "error": "Please enter revision notes before sending the card back to Companion."}
    return dispatch_work_item_agent(item_id=item_id, feedback=clean_fb, run_in_background=run_in_background, store=store, chat_fn=chat_fn, tool_fn=tool_fn, job_mgr=job_mgr)


def discard_worktree(item_id: str, store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """Cancel any running worker, remove the isolated git worktree, and reset the card to `ready`."""
    mem = store or get_memory_store()
    pg = get_project_graph(mem)
    item = pg.get_item(item_id)
    proj = pg.get_project(item["project_id"])
    if item.get("active_job_id"):
        get_job_manager(store=mem).cancel_job(str(item["active_job_id"]))
    ws_str, wt_str = str(proj.get("workspace_path") or "").strip(), str(item.get("worktree_path") or "").strip()
    if ws_str and wt_str:
        base_repo, wt_res = Path(ws_str).resolve(), Path(wt_str).resolve()
        if (base_repo / ".git").exists() and wt_res not in (base_repo, settings.root.resolve()):
            _run_git_cmd(base_repo, ["worktree", "remove", "--force", str(wt_res)])
            _run_git_cmd(base_repo, ["worktree", "prune"])
            shutil.rmtree(wt_res, ignore_errors=True)
    upd = pg.update_item(item["item_id"], status="ready", active_job_id="", worktree_path="", branch_name="")
    return {"ok": True, "item": upd, "card": upd, "summary": f"Discarded worktree and reset `{item['title']}` to Ready."}


def _run_one_subagent_slot(
    idx: int, total: int, spec: Dict[str, Any], max_iters: int, mem: MemoryStore,
    chat_fn: Optional[Callable[..., Dict[str, Any]]], parent_scope: Optional[CancelScope], parent_sink: Any, ws: Any,
) -> Dict[str, Any]:
    from companion.agent import run_subagent

    check_cancelled()
    goal_s = str(spec["goal"]).strip()
    skill_s = str(spec.get("skill") or spec.get("skill_name") or "").strip() or None
    tier_s = str(spec.get("tier") or "fast").strip() or "fast"
    wt_branch = str(spec.get("worktree_branch") or "").strip()
    child_scope = parent_scope.spawn_child() if parent_scope is not None else CancelScope()
    wt_created, t0 = "", time.perf_counter()
    if parent_sink is not None:
        with contextlib.suppress(Exception):
            parent_sink({"type": "subagent_progress", "index": idx, "total": total, "status": "running", "goal": goal_s[:90], "tier": tier_s})
    try:
        with cancel_scope_context(child_scope), memory_store_context(mem):
            target_root = ws.project_root
            if wt_branch and (ws.project_root / ".git").exists() and ws.project_root.resolve() != settings.root.resolve():
                wt_dir = (ws.project_root.parent / ".worktrees" / ws.project_id / _slugify_part(wt_branch, f"sub-{idx}")).resolve()
                wt_out = tool_git_manage({"action": "worktree_add", "repo_path": str(ws.project_root), "branch": wt_branch, "worktree_path": str(wt_dir)})
                if not wt_out.startswith(("error:", "SAFETY GUARD")):
                    target_root, wt_created = wt_dir, str(wt_dir)
            with workspace_context(target_root, ws.project_id):
                out = run_subagent(goal=goal_s, skill_name=skill_s, max_iters=int(spec.get("max_iters") or max_iters or 8), chat_fn=chat_fn or chat, store=mem, verbose=False, tier=tier_s)
        if parent_sink is not None:
            with contextlib.suppress(Exception):
                parent_sink({"type": "subagent_progress", "index": idx, "total": total, "status": "done", "goal": goal_s[:90], "tier": tier_s})
        return {"index": idx, "ok": True, "goal": goal_s, "skill": skill_s or "worker", "tier": tier_s, "worktree_branch": wt_branch, "worktree_path": wt_created, "duration_ms": round((time.perf_counter() - t0) * 1000.0, 1), "output": out}
    except InterruptedError:
        if parent_sink is not None:
            with contextlib.suppress(Exception):
                parent_sink({"type": "subagent_progress", "index": idx, "total": total, "status": "cancelled", "goal": goal_s[:90], "tier": tier_s})
        raise
    except Exception as exc:
        return {"index": idx, "ok": False, "goal": goal_s, "skill": skill_s or "worker", "tier": tier_s, "error": f"{type(exc).__name__}: {exc}", "output": ""}
    finally:
        if parent_scope is not None:
            parent_scope.unregister_child(child_scope)


def run_parallel_subagents(
    tasks: List[Dict[str, Any]], max_concurrency: int = 3, max_iters: int = 8,
    store: Optional[MemoryStore] = None, chat_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Fan out `1..4` concurrent subagents with parent-linked child `CancelScope`s (`<50ms` abort) and live SSE progress."""
    valid = [dict(t) for t in (tasks or []) if isinstance(t, dict) and str(t.get("goal") or "").strip()][:4]
    if not valid:
        return {"ok": False, "results": [], "error": "Provide 1-4 `{goal, skill?, tier?, worktree_branch?}` task dicts for `parallel_subagents`."}
    mem = store or get_memory_store()
    eff_conc = max(1, min(4, int(max_concurrency or 3), len(valid)))
    parent_scope, parent_sink, ws = get_active_cancel_scope(), get_active_stream_sink(), settings.get_workspace()
    results: List[Dict[str, Any]] = []
    pool = ThreadPoolExecutor(max_workers=eff_conc)
    futs = []
    try:
        for i, sp in enumerate(valid, 1):
            futs.append(pool.submit(contextvars.copy_context().run, _run_one_subagent_slot, i, len(valid), sp, max_iters, mem, chat_fn, parent_scope, parent_sink, ws))
        for f in futs:
            while True:
                check_cancelled()
                try:
                    results.append(f.result(timeout=0.02))
                    break
                except FuturesTimeout:
                    continue
    except InterruptedError:
        for f in futs:
            f.cancel()
        if parent_scope is not None:
            parent_scope.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=False)

    blocks = [
        f"### Subagent {r['index']}/{len(results)} (`{r['skill']}` | tier=`{r['tier']}`"
        + (f" | wt=`{r['worktree_branch']}`" if r.get("worktree_branch") else "")
        + f")\nGoal: {r['goal']}\n" + (r["output"] if r.get("ok") else f"ERROR: {r.get('error')}")
        for r in results
    ]
    return {
        "ok": all(r.get("ok") for r in results), "count": len(results), "results": results,
        "summary": f"=== Parallel Subagents Synthesis ({len(results)} workers, max_concurrency={eff_conc}) ===\n\n" + "\n\n".join(blocks),
    }


def format_parallel_subagents_tool(args: Dict[str, Any]) -> str:
    raw_tasks = next((args[k] for k in ("tasks", "subagents", "items") if args.get(k) is not None), None)
    if isinstance(raw_tasks, str) and raw_tasks.strip().startswith("["):
        with contextlib.suppress(Exception):
            raw_tasks = json.loads(raw_tasks)
    if not isinstance(raw_tasks, list):
        return "error: `tasks` array of `{goal, skill?, tier?, worktree_branch?}` objects is required for `parallel_subagents`"
    res = run_parallel_subagents(tasks=raw_tasks, max_concurrency=int_arg(args, "max_concurrency", 3), max_iters=int_arg(args, "max_iters", 8))
    return str(res["summary"]) if res.get("results") else f"error: {res.get('error')}"


def execute_agile_worktree_subaction(
    sub: str, pid: str, p: Dict[str, Any], _actor: str, mem: MemoryStore,
    job_mgr_fn: Callable[..., BackgroundJobManager] = get_job_manager,
) -> Optional[Dict[str, Any]]:
    """Handle Studio repo probe, parallel worktree dispatch, and 1-click deliverable subactions for `agile`."""
    if sub == "probe_repo":
        return probe_repo_workspace(str(p.get("workspace_path") or p.get("path") or ""))
    ref = str(p.get("item_id") or p.get("card_id") or "").strip()
    bg = p.get("run_in_background")
    run_bg = True if bg is None else (bg if isinstance(bg, bool) else str(bg).strip().lower() in ("1", "true", "yes", "on"))
    if sub in ("start_item", "start_card", "dispatch_item"):
        return dispatch_work_item_agent(ref, project_id=pid, feedback=str(p.get("feedback") or ""), run_in_background=run_bg, store=mem, job_mgr=job_mgr_fn(store=mem))
    if sub in ("run_parallel_ready", "run_ready"):
        return dispatch_parallel_ready_items(project_id=pid, max_workers=int(p.get("max_workers") or 0), run_in_background=run_bg, store=mem, job_mgr=job_mgr_fn(store=mem))
    if sub in ("merge_item", "merge_local"):
        return merge_worktree_into_base(ref, store=mem)
    if sub in ("push_item_pr", "push_and_pr"):
        return push_worktree_branch_and_pr(ref, store=mem)
    if sub == "request_changes":
        return request_worktree_changes(ref, feedback=str(p.get("feedback") or p.get("note") or ""), run_in_background=run_bg, store=mem, job_mgr=job_mgr_fn(store=mem))
    if sub == "discard_worktree":
        return discard_worktree(ref, store=mem)
    return None
