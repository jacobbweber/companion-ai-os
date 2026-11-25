"""
Unified Frontier Tool Registry, Workspace Execution Sandbox & Safety Guards (`companion/tools.py`).

Consolidates:
1. `<20` orthogonal OpenAI tool schemas (`TOOLS` = 18 schemas) with phase-scoped `AllowedTools` gating,
   plus backward-compatible `TOOL_IMPLS` aliases (`run_shell`, `run_python`, `workflow_manage`, etc.).
2. `ExecutionTarget` protocol + `LocalWorkspaceTarget` subprocess/PYTHONPATH sandbox & Blind Vault output scrubbing.
3. Mechanical `WorkspaceContext` path confinement (`check_path_safety`), destructive command guard
   (`check_command_safety`), `.py`/`.json` syntax guard (`lint_single_file_edit`), indent-shifted edit recovery,
   and `LoopGuard` stagnation detection.
4. Automatic scratch spillover (`spill_tool_output` -> `data/scratch/tool_<ts>_<label>.log`) so outputs `>8,000`
   chars never silently lose middle/tail evidence or bloat active context.
"""

import ast
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Optional, Protocol, Tuple

from companion.approvals import (
    ActionRiskTier,
    classify_action_risk,
    format_forever_search_results,
    get_approval_queue,
    search_forever_history,
)
from companion.config import SKIP_DIRS, agent_tool_turn_context, is_in_agent_tool_turn, settings
from companion.context import get_knowledge_port, manage_active_project
from companion.llm_wire import check_url_ssrf, is_local_or_lan_endpoint, urlopen_safe
from companion.memory_store import MemoryStore, get_memory_store
from companion.streaming import check_cancelled, emit_stream_event, get_active_cancel_scope
from companion.telemetry import telemetry
from companion.vault import (
    CheckpointManager,
    checkpoint_manager,
    get_active_checkpoint_manager,
    get_self_checkpoint_manager,
    get_vault_manager,
    run_system_doctor,
    run_unit_tests,
    validate_python_syntax,
)

__all__ = [
    "ALL_TOOL_NAMES",
    "CheckpointManager",
    "ExecutionTarget",
    "LocalWorkspaceTarget",
    "LoopGuard",
    "PHASE_ALLOWED_TOOLS",
    "TOOLS",
    "TOOL_IMPLS",
    "bool_arg",
    "check_command_safety",
    "check_path_safety",
    "check_phase_tool_permission",
    "checkpoint_manager",
    "current_workflow_phase",
    "dispatch",
    "dispatch_harvest_slash",
    "extract_python_symbol",
    "filter_tools_for_phase",
    "get_active_checkpoint_manager",
    "get_execution_target",
    "int_arg",
    "is_r3_action_approved",
    "lint_single_file_edit",
    "normalize_windows_shell_command",
    "phase_tool_context",
    "r3_approval_context",
    "register_execution_target",
    "resolve",
    "run_system_doctor",
    "run_unit_tests",
    "spill_tool_output",
    "str_arg",
    "summarize_tool_step",
    "tool_agile_manage",
    "tool_calendar_manage",
    "tool_call_mcp",
    "tool_checkpoint_manage",
    "tool_delegate_subagent",
    "tool_dev_preview",
    "tool_edit_file",
    "tool_education_manage",
    "tool_endpoint_manage",
    "tool_git_manage",
    "tool_job_manage",
    "tool_list_dir",
    "tool_mcp_manage",
    "tool_memory_manage",
    "tool_ops_manage",
    "tool_project_manage",
    "tool_read_file",
    "tool_routine_manage",
    "tool_run_command",
    "tool_run_python",
    "tool_run_shell",
    "tool_search",
    "tool_system_doctor",
    "tool_task_manage",
    "tool_telemetry_manage",
    "tool_vault_manage",
    "tool_web_fetch",
    "tool_web_search",
    "tool_wiki_manage",
    "tool_workflow_manage",
    "tool_write_file",
    "try_indent_shifted_replace",
    "validate_python_syntax",
]


# ---------------------------------------------------------------------------
# 1. Argument Coercion & Phase ContextVars
# ---------------------------------------------------------------------------
_ACTIVE_PHASE: ContextVar[str] = ContextVar("companion_active_phase", default="")
_R3_STEP_APPROVED: ContextVar[bool] = ContextVar("companion_r3_step_approved", default=False)


def str_arg(args: Dict[str, Any], key: str, default: str = "", aliases: Tuple[str, ...] = ()) -> str:
    for k in (key, *aliases):
        if args.get(k) is not None and str(args.get(k)).strip() != "":
            return str(args.get(k)).strip()
    return "" if args.get(key) == "" else default


def int_arg(args: Dict[str, Any], key: str, default: int) -> int:
    try:
        return default if args.get(key) in (None, "") else int(args[key])
    except (ValueError, TypeError):
        return default


def bool_arg(args: Dict[str, Any], key: str, default: bool = False) -> bool:
    val = args.get(key)
    return default if val is None else (val.strip().lower() in ("true", "1", "yes", "on") if isinstance(val, str) else bool(val))


def _dict_arg(args: Dict[str, Any], *keys: str) -> Optional[Dict[str, Any]]:
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


def current_workflow_phase() -> str:
    """Return the active phase-scoped tool gate name (or `''` when ungated)."""
    return _ACTIVE_PHASE.get()


@contextmanager
def phase_tool_context(phase: Optional[str]) -> Iterator[str]:
    """Bind a mechanical workflow phase (`DISCOVER`, `DESIGN`, `PLAN`, `IMPLEMENT`, etc.) to the current thread/turn."""
    clean = (phase or "").strip().upper()
    token = _ACTIVE_PHASE.set(clean)
    try:
        yield clean
    finally:
        _ACTIVE_PHASE.reset(token)


def is_r3_action_approved() -> bool:
    """Return `True` when executing inside a user-approved `R3` workflow step context."""
    return bool(_R3_STEP_APPROVED.get())


@contextmanager
def r3_approval_context(approved: bool = True) -> Iterator[bool]:
    """Temporarily authorize `R3` tool execution strictly for the duration of an approved step."""
    token = _R3_STEP_APPROVED.set(bool(approved))
    try:
        yield bool(approved)
    finally:
        _R3_STEP_APPROVED.reset(token)


# ---------------------------------------------------------------------------
# 2. Path/Command Safety Guards, Edit Recovery & LoopGuard
# ---------------------------------------------------------------------------
_DESTRUCTIVE_CMD_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:^|[;&|]\s*)(?:sudo\s+)?rm\s+(?:-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*|--recursive)\s+['\"]?(?:/|\*|~|\.\.(?:/|\\|\s|$))", re.I), "destructive recursive rm on root/home/parent"),
    (re.compile(r"\bgit\s+push\b[^\n;&|]*(?:--force(?!-with-lease)|-f\b)", re.I), "force-pushing git history (`git push --force`)"),
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.I), "destructive `git reset --hard` (use checkpoint rollback instead)"),
    (re.compile(r"\bgit\s+clean\s+-[a-zA-Z]*f", re.I), "destructive `git clean -f` untracked file wipe"),
    (re.compile(r"\b(?:mkfs(?:\.[a-z0-9]+)?\b|format\s+[a-zA-Z]:|Format-Volume\b)", re.I), "disk/volume formatting command"),
    (re.compile(r"\bdd\s+[^\n;&|]*\bof=/dev/", re.I), "raw block device write (`dd of=/dev/...`)"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.I), "shell fork bomb"),
    (re.compile(r"\b(?:shutdown|reboot|poweroff|halt|Stop-Computer|Restart-Computer)\b", re.I), "system power/reboot command"),
    (re.compile(r"\b(?:del|erase|rd|rmdir)\s+[^\n;&|]*/s[^\n;&|]*/q\s+['\"]?(?:[a-zA-Z]:\\|\*|\\)", re.I), "destructive Windows drive/wildcard wipe"),
    (re.compile(r"\b(?:Remove-Item|ri|rm|del|rd|rmdir)\b[^\n;&|]*(?:-Recurse|-r\b)[^\n;&|]*(?:[a-zA-Z]:\\|/(?:\s|$|[a-zA-Z])|~|\.\.(?:/|\\|\s|$)|\*(?:\s|$))", re.I), "destructive recursive PowerShell item wipe on root/home/parent/wildcard"),
    (re.compile(r"\b(?:DROP\s+TABLE|TRUNCATE\s+TABLE)\b", re.I), "destructive SQL table drop/truncate"),
    (re.compile(r"(?:UPDATE\s+user_profile|INSERT\s+[^\n;]*user_profile|REPLACE\s+INTO\s+user_profile)\b[^\n;]*(?:trust_mode|immutable_app|immutable_core|self_dev_mode)|(?:trust_mode|immutable_app|immutable_core|self_dev_mode)[^\n;]*(?:UPDATE\s+user_profile|INSERT\s+INTO\s+user_profile)", re.I), "privilege-escalation mutation of protected `user_profile` governance keys"),
    (re.compile(r"\b(?:pkill|killall)\b[^\n;&|]*\b(?:systemd|init|launchd|sshd)\b", re.I), "signaling OS/session init or SSH supervisor daemon"),
    (re.compile(r"\bsystemctl\b[^\n;&|]*\b(?:stop|kill|disable|mask)\b[^\n;&|]*\bcompanion(?:\.service)?\b", re.I), "stopping or disabling Companion's own service unit"),
    (re.compile(r"(?:secrets\.env|\.comp_ssh_|\.secrets_|\.ssh_tmp|(?:^|[\s'\"/\\])\.env(?!\.(?:example|sample|template))\b)", re.I), "reading or mutating Blind Vault / `.env` secret stores from subprocess"),
)
_TRAILING_OR_TRUE_RE = re.compile(r"\s*\|\|\s*(?:true|:\s*|exit\s+0)\s*$", re.I)
_PY_SELF_KILL_RE = re.compile(r"\bos\.kill(?:pg)?\s*\(\s*(?:(-?\d+)|(os\.getp?pid\s*\(\s*\)))")
_SCHEMA_TYPO_RE = re.compile(r"(?:is required\b|unknown [a-z_]+ action|JSON parse error|must be (?:one of|a non-empty)|unexpected keyword)", re.I)
_KILL_FAMILY_RE = re.compile(r"\b(?:kill|pkill|killall|taskkill|Stop-Process)\b", re.I)


def check_command_safety(command: str) -> Optional[str]:
    """Inspect a shell command or Python snippet and reject destructive, secret-reading, or self-terminating patterns."""
    cmd = (command or "").strip()
    if not cmd:
        return None
    for rx, reason in _DESTRUCTIVE_CMD_PATTERNS:
        if rx.search(cmd):
            return f"SAFETY GUARD REJECTED command: blocked {reason}.\nCommand was NOT executed. Use safe, scoped project operations."
    protected = {-1, 0, 1, os.getpid(), *([os.getppid()] if os.getppid() > 0 else [])}
    for m in re.finditer(r"\bkill\b(?P<rest>[^\n;&|]+)", cmd, re.I):
        args_part = re.sub(r"^(?:\s+(?:-[sSnN]\s+\S+|--signal(?:=|\s+)\S+|-[a-zA-Z0-9]+))?(?:\s+--)?", "", m.group("rest"))
        if any(int(tok) in protected for tok in re.findall(r"(?<!\S)-?\d+(?!\S)", args_part)):
            return "SAFETY GUARD REJECTED command: blocked signaling PID 1, process group, or Companion's own process/supervisor tree."
    if any(int(p) in protected for p in re.findall(r"(?:/pid|-id)\s+(-?\d+)", cmd, re.I)):
        return "SAFETY GUARD REJECTED command: blocked signaling PID 1 or Companion's own process/supervisor tree."
    for pm in _PY_SELF_KILL_RE.finditer(cmd):
        if pm.group(2) or (pm.group(1) and int(pm.group(1)) in protected):
            return "SAFETY GUARD REJECTED command: blocked `os.kill`/`os.killpg` targeting PID 1 or Companion's own process/supervisor tree."
    return None


def normalize_windows_shell_command(command: str) -> Tuple[str, bool]:
    """Normalize POSIX `/dev/null` redirections and trailing `|| true` guards on Windows."""
    cmd, ignore_err = (command or "").strip(), False
    if os.name == "nt" and cmd:
        match = _TRAILING_OR_TRUE_RE.search(cmd)
        if match:
            cmd, ignore_err = cmd[: match.start()].strip(), True
        cmd = re.sub(r"2>\s*/dev/null\b", "2>nul", cmd)
        cmd = re.sub(r"(?<![0-9])>\s*/dev/null\b", ">nul", cmd)
    return cmd, ignore_err


def check_path_safety(path: Path, write: bool = False, allowed_roots: Optional[List[Path]] = None) -> Optional[str]:
    """Enforce mechanical WorkspaceContext path confinement and protect core/sensitive files."""
    try:
        resolved = path.resolve()
    except Exception as exc:
        return f"SAFETY GUARD REJECTED path: invalid path `{path}` ({exc})."

    tmp_root = Path(tempfile.gettempdir()).resolve()
    eff_data = settings.effective_data_dir.resolve()
    if allowed_roots is not None:
        roots = [r.resolve() for r in allowed_roots]
    else:
        ws = settings.get_workspace()
        if ws.is_self_project:
            roots = [ws.self_root, eff_data, *([settings.data_dir.resolve()] if settings.data_dir else []), tmp_root]
        elif write:
            if resolved.is_relative_to(ws.self_root) and not resolved.is_relative_to(ws.project_root) and not resolved.is_relative_to(eff_data):
                return (
                    f"SAFETY GUARD REJECTED path: Companion's installed `app/` runtime (`{ws.self_root}`) is permanently IMMUTABLE / READ-ONLY "
                    f"while working on external project `{ws.project_id}` (`{ws.project_root}`)."
                )
            roots = [ws.project_root, settings.domains_dir.resolve(), settings.user_skills_dir.resolve(), settings.scratch_dir.resolve(), tmp_root]
        else:
            roots = [ws.project_root, eff_data, (ws.self_root / "skills").resolve(), tmp_root]

    if not any(resolved.is_relative_to(r) for r in roots):
        return f"SAFETY GUARD REJECTED path: `{resolved}` is outside the active WorkspaceContext root (`{roots[0]}`). Cross-directory traversal is blocked."
    parts_lower = [pt.lower() for pt in resolved.parts]
    fname_lower = resolved.name.lower()
    if ".agents" in parts_lower and write:
        return f"SAFETY GUARD REJECTED path: `.agents/` builder directory (`{resolved}`) is off-limits to runtime tool writes."
    if ".ssh" in parts_lower or ".ssh_tmp" in parts_lower or fname_lower.startswith((".comp_ssh_", ".secrets_")):
        return f"SAFETY GUARD REJECTED path: access to SSH/Vault temporary credentials (`{resolved}`) is blocked."
    is_env_secret = (
        fname_lower in (".env", "secrets.env")
        or resolved == settings.secrets_env_path.resolve()
        or (fname_lower.startswith(".env.") and fname_lower not in (".env.example", ".env.sample", ".env.template"))
    )
    if is_env_secret:
        return (
            f"SAFETY GUARD REJECTED path: direct overwrite of secret file `{resolved.name}` is blocked."
            if write
            else f"SAFETY GUARD REJECTED path: reading secret file `{resolved.name}` via tools is blocked (inspect `.env.example` instead)."
        )
    if write and ".git" in parts_lower:
        return f"SAFETY GUARD REJECTED path: direct modification of `.git` internals (`{resolved}`) is blocked."
    if write and allowed_roots is None:
        wiki_roots = [(eff_data / "wiki").resolve(), (settings.root / "data" / "wiki").resolve()]
        if any(resolved.is_relative_to(wr) for wr in wiki_roots):
            return (
                f"SAFETY GUARD REJECTED path: direct `write_file`/`edit_file` inside `{resolved.parent}` is blocked. "
                "Use `wiki_manage` so Knowledge Wiki co-ownership and curation rules are enforced."
            )
        ws_chk = settings.get_workspace()
        if ws_chk.is_self_project and resolved.is_relative_to(ws_chk.self_root):
            data_roots = [eff_data]
            if not settings.immutable_app or settings.data_dir is None:
                data_roots.append((ws_chk.self_root / "data").resolve())
            if not any(resolved.is_relative_to(dr) for dr in data_roots):
                return (
                    f"SAFETY GUARD REJECTED path: Companion's installed `app/` runtime (`{ws_chk.self_root}`) is permanently IMMUTABLE. "
                    "Write inside `data/` (`data/domains/`, `data/scratch/`, `data/skills/`) or focus an external Project clone (`workspace_path`)."
                )
    return None


def lint_single_file_edit(
    path: Path, content: str, op_name: str = "write_file", governed_root: Optional[Path] = None,
) -> Tuple[Optional[str], str]:
    """Validate syntax on `.py` and `.json` file edits without imposing Companion-repo policing on user files."""
    _ = governed_root
    resolved = path.resolve()
    sfx = resolved.suffix.lower()
    if sfx == ".py":
        valid, syn_err = validate_python_syntax(content, filename=str(resolved))
        if not valid:
            verb_note = "File was NOT modified. Fix syntax and retry." if op_name == "write_file" else "Edit was NOT applied. Adjust new_str and retry."
            return f"SYNTAX GUARD REJECTED {op_name} on {path}:\n  {syn_err}\n{verb_note}", ""
    elif sfx == ".json":
        try:
            json.loads(content)
        except Exception as exc:
            return f"SYNTAX GUARD REJECTED {op_name} on {path}: Invalid JSON ({exc}).", ""
    return None, ""


def _try_blank_normalized_replace(t_lines: List[str], o_lines: List[str], n_lines: List[str]) -> Optional[str]:
    nz_o = [ln for ln in o_lines if ln.strip()]
    if len(nz_o) < 2:
        return None
    nz_t = [(idx, ln) for idx, ln in enumerate(t_lines) if ln.strip()]
    if len(nz_o) > len(nz_t):
        return None
    spans: List[Tuple[int, int]] = []
    for pos in range(len(nz_t) - len(nz_o) + 1):
        if all(nz_t[pos + k][1] == nz_o[k] for k in range(len(nz_o))):
            if all((nz_t[pos + k + 1][0] - nz_t[pos + k][0]) <= 5 for k in range(len(nz_o) - 1)):
                spans.append((nz_t[pos][0], nz_t[pos + len(nz_o) - 1][0]))
    if len(spans) != 1:
        return None
    s_i, e_i = spans[0]
    return "\n".join(t_lines[:s_i] + n_lines + t_lines[e_i + 1 :])


def try_indent_shifted_replace(text: str, old: str, new: str) -> Optional[str]:
    """If old matches a unique block in text shifted by uniform leading spaces or minor blank-line count, apply new."""
    t_lines, o_lines, n_lines = text.split("\n"), old.split("\n"), new.split("\n")
    if not o_lines or len(o_lines) > len(t_lines) + 4:
        return None
    matches: List[Tuple[int, int]] = []
    for i in range(max(0, len(t_lines) - len(o_lines) + 1)):
        if any(t_lines[i + k].strip() != o_lines[k].strip() for k in range(len(o_lines))):
            continue
        rest_deltas = {
            (len(o_lines[k]) - len(o_lines[k].lstrip(" "))) - (len(t_lines[i + k]) - len(t_lines[i + k].lstrip(" ")))
            for k in range(1, len(o_lines)) if t_lines[i + k].strip()
        }
        d0 = (len(o_lines[0]) - len(o_lines[0].lstrip(" "))) - (len(t_lines[i]) - len(t_lines[i].lstrip(" ")))
        all_deltas = rest_deltas | ({d0} if t_lines[i].strip() else set())
        if len(all_deltas) == 1:
            matches.append((i, next(iter(all_deltas))))
        elif len(rest_deltas) == 1 and o_lines[0] == o_lines[0].lstrip(" "):
            matches.append((i, next(iter(rest_deltas))))
    if len(matches) != 1:
        return _try_blank_normalized_replace(t_lines, o_lines, n_lines)
    idx, delta = matches[0]
    t0_indent = len(t_lines[idx]) - len(t_lines[idx].lstrip(" "))
    adj_new: List[str] = []
    for k, ln in enumerate(n_lines):
        if not ln.strip():
            adj_new.append("")
        elif k == 0 and o_lines[0] == o_lines[0].lstrip(" ") and ln == ln.lstrip(" "):
            adj_new.append((" " * t0_indent) + ln)
        else:
            adj_new.append((" " * max(0, (len(ln) - len(ln.lstrip(" "))) - delta)) + ln.lstrip(" "))
    return "\n".join(t_lines[:idx] + adj_new + t_lines[idx + len(o_lines) :])


@dataclass
class LoopGuard:
    """Detects and breaks infinite tool loops, stagnation, and repeated error cascades."""

    max_identical_calls: int = 2
    max_consecutive_errors: int = 2
    call_counts: Counter = field(default_factory=Counter)
    family_counts: Counter = field(default_factory=Counter)
    last_signature: Optional[str] = None
    consecutive_identical: int = 0
    consecutive_errors: int = 0

    def _signature(self, name: str, args: Dict[str, Any]) -> str:
        try:
            canonical = json.dumps(args, sort_keys=True)
        except Exception:
            canonical = str(args)
        return f"{name}:{canonical}"

    def check_before_call(self, name: str, args: Dict[str, Any]) -> Optional[str]:
        if name in ("run_shell", "run_command") and _KILL_FAMILY_RE.search(str(args.get("command") or "")):
            self.family_counts["process_kill"] += 1
            if self.family_counts["process_kill"] > 2:
                self.consecutive_errors = max(self.consecutive_errors, self.max_consecutive_errors)
                return (
                    "LOOP GUARD INTERVENTION: Repeated process-kill attempts detected in this turn — a supervisor daemon is likely respawning the process. "
                    "Inspect `systemctl status <pid>` or `docker ps` instead of killing respawned PIDs."
                )
        sig = self._signature(name, args)
        self.call_counts[sig] += 1
        self.consecutive_identical = (self.consecutive_identical + 1) if sig == self.last_signature else 1
        self.last_signature = sig
        if self.consecutive_identical > self.max_identical_calls or self.call_counts[sig] > self.max_identical_calls + 1:
            return (
                f"LOOP GUARD INTERVENTION: You have called `{name}` with identical arguments {self.call_counts[sig]} times in this turn. "
                "Adjust parameters or answer with current findings."
            )
        return None

    def record_result(self, name: str, result: str) -> Optional[str]:
        is_err = result.startswith((
            "error:", "error in ", "Tool argument JSON parse error", "SYNTAX GUARD REJECTED", "SAFETY GUARD REJECTED",
            "PHASE TOOL GATE REJECTED", "RISK GATE REJECTED", "SKILL FORGE REJECTED", "WIKI AUTHOR GUARD REJECTED",
            "PRIVILEGE GUARD REJECTED",
        )) or bool(re.match(r"^exit=(?!0\b)-?\d+\n", result))
        self.consecutive_errors = (self.consecutive_errors + 1) if is_err else 0
        if self.consecutive_errors >= self.max_consecutive_errors:
            return f"\n[SELF-RECOVERY HINT: {self.consecutive_errors} consecutive tool errors detected. Pause and inspect the root cause before retrying.]"
        if self.consecutive_errors == 1 and _SCHEMA_TYPO_RE.search(result):
            return f"\n[Parameter hint for `{name}`: check parameter names/types against the tool schema and retry cleanly.]"
        return None


# ---------------------------------------------------------------------------
# 3. Scratch Spillover & Workspace ExecutionTarget Seam
# ---------------------------------------------------------------------------
def spill_tool_output(text: str, label: str = "output", limit: Optional[int] = None) -> str:
    """Spill tool outputs exceeding `limit` (`settings.out_limit` = 8,000 chars) to `data/scratch/`."""
    cap = limit or settings.out_limit
    if len(text) <= cap:
        return text
    clean_lbl = re.sub(r"[^a-zA-Z0-9_-]+", "_", label or "output").strip("_")[:32] or "output"
    scratch = settings.scratch_dir
    spill_note = f"[truncated at {cap:,} of {len(text):,} chars]"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        spill_path = scratch / f"tool_{int(time.time() * 1000)}_{clean_lbl}.log"
        spill_path.write_text(text, encoding="utf-8")
        spill_note = f"[Full output ({len(text):,} chars) saved to {spill_path} — inspect specific ranges with `read_file` or `search`.]"
    except OSError:
        pass
    head_n = max(1200, int(cap * 0.6))
    tail_n = max(800, cap - head_n - 260)
    return f"{text[:head_n]}\n\n... {spill_note} ...\n\n{text[-tail_n:]}"


class ExecutionTarget(Protocol):
    """Execution seam enabling local workspaces today and isolated sandboxes/endpoints tomorrow."""

    @property
    def project_root(self) -> Path: ...
    @property
    def is_self_project(self) -> bool: ...
    def exec_env_and_cwd(self) -> Tuple[Dict[str, str], str]: ...
    def read_text(self, path: Path) -> str: ...
    def write_text(self, path: Path, content: str, backup: bool = True) -> None: ...
    def list_dir(self, path: Path) -> List[str]: ...
    def exec_command(
        self, cmd_or_args: Any, shell: bool = False, timeout: Optional[int] = None,
        extra_env: Optional[Dict[str, str]] = None, cwd_override: Optional[str] = None,
    ) -> Tuple[int, str, str, Optional[str]]: ...
    def run_python_code(self, code: str, timeout: Optional[int] = None) -> str: ...
    def create_checkpoint(self, label: str = "auto") -> Dict[str, Any]: ...
    def list_checkpoints(self) -> List[Dict[str, Any]]: ...
    def rollback_checkpoint(self, checkpoint_id: Optional[str] = None, reason: str = "", force: bool = False) -> Dict[str, Any]: ...


_STRIP_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD", "_CREDENTIAL")


class LocalWorkspaceTarget:
    """Default `ExecutionTarget` executing inside the active `WorkspaceContext` with strict `PYTHONPATH` isolation."""

    @property
    def project_root(self) -> Path:
        return settings.get_workspace().project_root

    @property
    def is_self_project(self) -> bool:
        return settings.get_workspace().is_self_project

    def _cp_mgr(self) -> CheckpointManager:
        return get_active_checkpoint_manager()

    def exec_env_and_cwd(self) -> Tuple[Dict[str, str], str]:
        ws = settings.get_workspace()
        clean_env = {
            k: v for k, v in os.environ.items()
            if k.upper() != "COMPANION_WEB_TOKEN"
            and not k.upper().endswith(_STRIP_ENV_SUFFIXES)
            and not k.upper().startswith("COMPANION_SECRET")
        }
        return {**clean_env, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONPATH": str(ws.project_root)}, str(ws.project_root)

    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace")

    def write_text(self, path: Path, content: str, backup: bool = True) -> None:
        cp_mgr = self._cp_mgr()
        existed_before = path.is_file()
        before_bytes = path.read_bytes() if existed_before else None
        if backup and existed_before:
            cp_mgr.backup_file(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        cp_mgr.record_file_touch(path, existed_before, before_bytes, path.read_bytes())

    def list_dir(self, path: Path) -> List[str]:
        return [e.name + "/" if e.is_dir() else e.name for e in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))]

    def _snapshot_priv_db(self, store: MemoryStore) -> Tuple[str, List[Tuple[str, str, str, str]]]:
        prof = store.get_user_profile()
        approvals: List[Tuple[str, str, str, str]] = []
        with store._lock:
            has_apr = store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='approval_requests'").fetchone()
            if has_apr:
                approvals = [(str(r[0]), str(r[1]), str(r[2] or ""), str(r[3] or "")) for r in store.conn.execute("SELECT approval_id, status, decided_by, decided_at FROM approval_requests").fetchall()]
        return prof.get("trust_mode", "balanced"), approvals

    def _guard_priv_db(self, store: MemoryStore, pre_state: Tuple[str, List[Tuple[str, str, str, str]]]) -> Optional[str]:
        pre_tm, pre_apr = pre_state
        prof = store.get_user_profile()
        violated: List[str] = []
        with store._lock:
            if prof.get("trust_mode", "balanced") != pre_tm:
                store.conn.execute("INSERT OR REPLACE INTO user_profile (key, value, updated_at) VALUES ('trust_mode', ?, CURRENT_TIMESTAMP)", (pre_tm,))
                violated.append("trust_mode")
            has_apr = store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='approval_requests'").fetchone()
            if has_apr:
                cur_apr = [(str(r[0]), str(r[1]), str(r[2] or ""), str(r[3] or "")) for r in store.conn.execute("SELECT approval_id, status, decided_by, decided_at FROM approval_requests").fetchall()]
                if cur_apr != pre_apr:
                    pre_ids = {r[0] for r in pre_apr}
                    store.conn.execute(f"DELETE FROM approval_requests WHERE approval_id NOT IN ({','.join('?' for _ in pre_ids)})" if pre_ids else "DELETE FROM approval_requests", tuple(pre_ids))
                    for aid, st, db_by, d_at in pre_apr:
                        store.conn.execute("UPDATE approval_requests SET status = ?, decided_by = ?, decided_at = ? WHERE approval_id = ?", (st, db_by, d_at or None, aid))
                    violated.append("approval_requests")
            if violated:
                store.conn.commit()
        if not violated:
            return None
        return f"SAFETY GUARD REJECTED subprocess privilege escalation: direct SQLite modification of `{', '.join(violated)}` was blocked and reverted (`ADR-019`)."

    def exec_command(
        self, cmd_or_args: Any, shell: bool = False, timeout: Optional[int] = None,
        extra_env: Optional[Dict[str, str]] = None, cwd_override: Optional[str] = None,
    ) -> Tuple[int, str, str, Optional[str]]:
        check_cancelled()
        active_cp, self_cp, store = self._cp_mgr(), get_self_checkpoint_manager(), get_memory_store()
        pre_py_stats = active_cp.snapshot_py_stats() if not self.is_self_project else None
        pre_priv = self._snapshot_priv_db(store)
        ro_snap = self_cp.snapshot_readonly_tree()
        env, cwd = self.exec_env_and_cwd()
        if extra_env:
            env = {**env, **extra_env}
        eff_timeout = timeout or settings.shell_timeout
        scope = get_active_cancel_scope()
        proc = subprocess.Popen(
            cmd_or_args, shell=shell, cwd=cwd_override or cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            if scope is not None:
                with scope.track_popen(proc):
                    out_raw, err_raw = proc.communicate(timeout=eff_timeout)
            else:
                out_raw, err_raw = proc.communicate(timeout=eff_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            if pre_py_stats is not None:
                active_cp.guard_py_stats_after_exec(pre_py_stats)
            self._guard_priv_db(store, pre_priv)
            self_cp.enforce_readonly_tree(ro_snap)
            return -1, "", "", f"timed out after {eff_timeout}s"
        finally:
            if scope is not None and scope.is_cancelled:
                if pre_py_stats is not None:
                    active_cp.guard_py_stats_after_exec(pre_py_stats)
                self._guard_priv_db(store, pre_priv)
                self_cp.enforce_readonly_tree(ro_snap)
        check_cancelled()
        guard_err = self._guard_priv_db(store, pre_priv) or (
            active_cp.guard_py_stats_after_exec(pre_py_stats) if pre_py_stats is not None else None
        ) or self_cp.enforce_readonly_tree(ro_snap)
        vault = get_vault_manager(store=store)
        return proc.returncode, vault.scrub_output(out_raw or ""), vault.scrub_output(err_raw or ""), guard_err

    def run_python_code(self, code: str, timeout: Optional[int] = None) -> str:
        if not (code or "").strip():
            return "error: empty Python code"
        ok_syn, syn_err = validate_python_syntax(code, filename="<run_python>")
        if not ok_syn:
            return f"error: Python syntax error in code: {syn_err}"
        rc, stdout, stderr, guard_err = self.exec_command([sys.executable, "-c", code], shell=False, timeout=timeout)
        if rc == -1 and guard_err:
            return f"error: Python execution {guard_err}"
        if guard_err:
            return f"exit=1\n{guard_err}"
        out = (stdout + (("\n[stderr]\n" + stderr) if stderr else "")).strip() or "(no output)"
        return f"exit={rc}\n{spill_tool_output(out, label='run_python')}"

    def create_checkpoint(self, label: str = "auto") -> Dict[str, Any]:
        return self._cp_mgr().create_checkpoint(label=label)

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        return self._cp_mgr().list_checkpoints()

    def rollback_checkpoint(self, checkpoint_id: Optional[str] = None, reason: str = "", force: bool = False) -> Dict[str, Any]:
        return self._cp_mgr().rollback_checkpoint(checkpoint_id=checkpoint_id, reason=reason, force=force)


_CUSTOM_EXECUTION_TARGET: Optional[ExecutionTarget] = None
_DEFAULT_LOCAL_TARGET = LocalWorkspaceTarget()


def register_execution_target(target: Optional[ExecutionTarget]) -> None:
    """Register or clear a custom `ExecutionTarget` (e.g. container/remote sandbox manager)."""
    global _CUSTOM_EXECUTION_TARGET
    _CUSTOM_EXECUTION_TARGET = target


def get_execution_target() -> ExecutionTarget:
    """Return the active `ExecutionTarget` (`LocalWorkspaceTarget` by default)."""
    return _CUSTOM_EXECUTION_TARGET if _CUSTOM_EXECUTION_TARGET is not None else _DEFAULT_LOCAL_TARGET


# ---------------------------------------------------------------------------
# 4. Orthogonal Frontier Tool Schemas (`<20` Schemas = 18 Total)
# ---------------------------------------------------------------------------
def _p(ptype: str, desc: str, enum: Optional[List[str]] = None, items_type: Optional[str] = None) -> Dict[str, Any]:
    spec: Dict[str, Any] = {"type": ptype, "description": desc}
    if enum:
        spec["enum"] = enum
    if items_type:
        spec["items"] = {"type": items_type}
    return spec


def _tool(name: str, desc: str, props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {"type": "object", "properties": props, "required": required}}}


TOOLS: List[Dict[str, Any]] = [
    _tool("list_dir", "List files and subdirectories inside a directory in the active WorkspaceContext.", {
        "path": _p("string", "Relative or absolute directory path (defaults to `.`)."),
    }, []),
    _tool("read_file", "Read numbered lines or an exact Python AST symbol (`func`, `Class`, `Class.method`) from a file.", {
        "path": _p("string", "Relative or absolute file path inside the active WorkspaceContext or `data/`."),
        "start_line": _p("integer", "1-based first line to read (optional; ignored when `symbol` is set)."),
        "end_line": _p("integer", "1-based last line to read inclusive (optional; ignored when `symbol` is set)."),
        "symbol": _p("string", "Optional Python AST symbol (`func_name`, `ClassName`, or `ClassName.method`) to slice."),
    }, ["path"]),
    _tool("write_file", "Create or overwrite a file (auto-backups & validates `.py`/`.json` syntax).", {
        "path": _p("string", "Relative or absolute path of the file to write in `data/` or the active project."),
        "content": _p("string", "Complete UTF-8 text content to write to the file."),
    }, ["path", "content"]),
    _tool("edit_file", "Replace an EXACT unique string in an existing file (auto-backups & validates syntax).", {
        "path": _p("string", "Relative or absolute path of the existing file to modify."),
        "old_str": _p("string", "Exact unique existing block of text (including indentation) to replace."),
        "new_str": _p("string", "Replacement text block that drops cleanly in place of `old_str`."),
    }, ["path", "old_str", "new_str"]),
    _tool("search", "Search text across workspace files by case-insensitive substring or regular expression.", {
        "pattern": _p("string", "Substring or regex pattern to search for."),
        "path": _p("string", "Directory or file path to scope the search (defaults to `.`)."),
        "is_regex": _p("boolean", "Set true to treat `pattern` as a Python regular expression."),
        "file_glob": _p("string", "Optional filename glob filter such as `*.py`, `*.ts`, `*.csv`, or `*.md`."),
    }, ["pattern"]),
    _tool("run_command", "Execute a safe shell command (`command`) or in-process Python 3 script (`python_code`); outputs >8,000 chars automatically spill to `data/scratch/`.", {
        "command": _p("string", "Shell command line to run in the active workspace (omit when passing `python_code`)."),
        "python_code": _p("string", "Optional Python 3 snippet to run directly via `sys.executable -c` (preferred for CSV/SQLite/JSON processing)."),
    }, []),
    _tool("web_search", "Search the public web or configured local SearXNG endpoint (gated by `local_only` privacy posture).", {
        "query": _p("string", "Search query keywords or natural-language question to look up."),
        "max_results": _p("integer", "Maximum number of search results to return (1-10, default 5)."),
    }, ["query"]),
    _tool("web_fetch", "Fetch an HTTP/HTTPS URL with SSRF protection, 512 KB cap, HTML-to-Markdown conversion, and scratch spillover.", {
        "url": _p("string", "Full `http://` or `https://` URL to fetch."),
        "raw": _p("boolean", "Set true to return raw HTML instead of readable Markdown."),
    }, ["url"]),
    _tool("delegate_subagent", "Spawn an isolated subagent worker (`goal`) or up to 4 parallel worktree workers (`tasks`) so heavy research/coding stays out of main chat context.", {
        "goal": _p("string", "Focused objective for a single isolated subagent worker."),
        "skill": _p("string", "Optional runtime skill or domain pack name to preload into the worker."),
        "tier": _p("string", "Model tier for the worker.", enum=["fast", "coding", "reasoning", "image"]),
        "max_iters": _p("integer", "Maximum tool steps for the worker (default 8)."),
        "inherit_attachments": _p("boolean", "Set true to pass active turn image/attachment context to the subagent."),
        "tasks": {"type": "array", "description": "Optional array of 1-4 `{goal, skill, tier, worktree_branch}` tasks for parallel execution.", "items": {"type": "object"}},
        "max_concurrency": _p("integer", "Maximum concurrent workers (1-4, default 3) when `tasks` is set."),
    }, []),
    _tool("call_mcp", "Discover, propose, invoke, or harvest outbound MCP servers and Federated Companion Partners.", {
        "action": _p("string", "MCP Hub, Harvester, or Partner action.", enum=["list", "propose_server", "register_server", "discover", "call_tool", "harvest_skill", "list_partners", "register_partner", "handshake_partner", "delegate_partner", "check_partner_job"]),
        "server_id": _p("string", "Outbound MCP server slug for `propose_server`, `register_server`, `discover`, `call_tool`, or `harvest_skill`."),
        "pack_name": _p("string", "Optional target Capability Pack slug in `data/skills/` when `action='harvest_skill'`."),
        "transport": _p("string", "Outbound MCP transport (`stdio` or `http`).", enum=["stdio", "http"]),
        "command": _p("string", "Executable command for `stdio` MCP server registration."),
        "url": _p("string", "Remote HTTP MCP endpoint URL (`http://.../api/mcp`) for servers or partners."),
        "credential_ref": _p("string", "Symbolic Blind Vault token reference."),
        "tool_name": _p("string", "Discovered MCP tool name when `action='call_tool'`."),
        "arguments": _p("object", "Key-value JSON arguments passed to `call_tool`."),
        "partner_id": _p("string", "Paired Federated Companion ID."),
        "goal": _p("string", "Task goal for `delegate_partner`."),
        "job_id": _p("string", "Remote background job ID for `check_partner_job`."),
    }, ["action"]),
    _tool("memory_manage", "Manage persistent SQLite memory, KnowledgePort facts, notes, user profile, Forever-Chat history search, and `MEMORY.md` sync.", {
        "action": _p("string", "Memory or KnowledgePort operation.", enum=["add_note", "get_notes", "add_adr", "get_adrs", "set_profile", "get_profile", "upsert_fact", "query_facts", "search", "search_history", "recent_history", "sync"]),
        "content": _p("string", "System note or fact content."),
        "category": _p("string", "Note category (`architecture`, `lesson`, `general`)."),
        "key": _p("string", "Profile or KnowledgePort fact key (`domain:topic`)."),
        "value": _p("string", "Profile or KnowledgePort fact value."),
        "source": _p("string", "Fact provenance tag (`user`, `verified`, `agent`)."),
        "confidence": _p("number", "Confidence score `0.0..1.0` for `upsert_fact`."),
        "query": _p("string", "Search query across `query_facts` or Forever-Chat history."),
        "time_window": _p("string", "Optional relative window (`-7 days`, `-24 hours`)."),
        "limit": _p("integer", "Maximum rows to return (default 15)."),
    }, ["action"]),
    _tool("wiki_manage", "Operate the Co-Owned 4-Folder Knowledge Wiki (`01_inbox`..`04_archive`), governed templates, Deliberate Curation, search, freshness verification, and 2D Mind Map.", {
        "action": _p("string", "Wiki operation.", enum=["list", "tree", "read", "search", "capture_inbox", "save", "curate", "graduate", "inspect_graduation", "validate_curation", "commit_curation", "curation_history", "revert_curation", "list_templates", "read_template", "save_template", "tag_authority", "create_proxy_note", "deep_curate_job", "diff", "consolidate_ontology", "verify_note", "list_stale", "stage_turn", "trash", "list_trash", "restore", "graph", "reindex", "export_chat"]),
        "path": _p("string", "Relative Wiki note path (`01_inbox/...` or `02_knowledge/<domain>/...`)."),
        "title": _p("string", "Human-readable note or template title."),
        "summary": _p("string", "1-2 sentence executive summary for `02_knowledge/` frontmatter."),
        "template": _p("string", "Governed template slug (`standard-note`, `zettelkasten-atomic`, `sop-runbook`, `adr-decision`, etc.)."),
        "content": _p("string", "Complete Markdown note body matching required sections."),
        "body": _p("string", "Alias for `content`."),
        "query": _p("string", "Search query (supports `tag:...`, `domain:...`, `ontology:...`, free-text)."),
        "domain": _p("string", "Top-level domain folder under `02_knowledge/`."),
        "topic": _p("string", "Optional second-level topic folder."),
        "tags": _p("string", "Comma-separated kebab-case tags."),
        "ontology": _p("string", "Comma-separated ontology bridge concepts."),
        "curation_id": _p("string", "Active curation session ID (`cur_...`)."),
        "strategy": _p("string", "Graduation strategy.", enum=["CREATE_NEW", "MERGE_AND_REFINE", "SPLIT_MULTI", "KEEP_SEPARATE"]),
        "outputs_json": _p("string", "JSON array string of curated output note objects for `validate_curation`."),
        "rationale": _p("string", "Explicit rationale when `strategy='KEEP_SEPARATE'`."),
        "allow_condense": _p("boolean", "Allow intentional condensation during `MERGE_AND_REFINE`."),
        "tx_id": _p("string", "Transaction ID (`ctx_...`) for `revert_curation`."),
        "asset_path": _p("string", "Source binary/PDF/image asset path for `create_proxy_note`."),
        "highlights": _p("string", "Extracted highlights for `create_proxy_note`."),
        "expected_hash": _p("string", "Optional 16-char SHA-256 `content_hash` for optimistic concurrency."),
    }, ["action"]),
    _tool("agile_manage", "Operate the Universal Project & Goal Graph (`projects`, `milestones`, `timeboxes`, `work_items`, Kanban columns, 15m unblocker slicing, and parallel worktree cards).", {
        "action": _p("string", "Board/Goal operation.", enum=["board_view", "list_items", "roadmap", "list_boards", "probe_repo", "create_project", "update_project", "save_board", "list_epics", "create_milestone", "save_epic", "delete_epic", "seed_dikw", "list_sprints", "create_timebox", "start_sprint", "rollover_sprint", "capacity", "create_item", "update_item", "save_card", "move_card", "delete_item", "delete_card", "slice_unblocker", "start_item", "run_parallel_ready", "merge_item", "push_item_pr", "request_changes", "discard_worktree"]),
        "project_id": _p("string", "Project ID or board slug (`life-home`, `work-career`, `prj_...`)."),
        "board_id": _p("string", "Board slug alias (`all-active-sprint` or project ID)."),
        "epic_id": _p("string", "Milestone/Epic ID (`ms_...` or `ep_...`)."),
        "card_id": _p("string", "Work-item ID (`wi_...` or `crd_...`)."),
        "title": _p("string", "Title for project, milestone, sprint, or work item."),
        "description": _p("string", "Detailed notes, acceptance criteria, or context."),
        "owner": _p("string", "Execution owner.", enum=["companion", "human", "collaborative"]),
        "stage_kind": _p("string", "DIKW stage.", enum=["OBSERVE_DATA", "CURATE_DIKW", "FORGE_CAPABILITY", "DELIVER_AND_VERIFY"]),
        "depends_on": _p("array", "Upstream work-item IDs.", items_type="string"),
        "dag_name": _p("string", "Optional verified workflow name bound to this item."),
        "checklist": _p("array", "Optional checklist subtask strings.", items_type="string"),
        "status": _p("string", "Column status.", enum=["backlog", "sprint_todo", "ready", "in_progress", "blocked", "verify", "done"]),
        "horizon": _p("string", "Planning horizon.", enum=["now", "next", "later", "someday"]),
        "effort_size": _p("string", "Effort slot size.", enum=["quick", "half_day", "full_day", "multi_day"]),
        "day_slot": _p("string", "Day-of-week slot.", enum=["weekday", "weekend", "any"]),
        "priority": _p("integer", "Priority 1 (highest) to 5 (lowest)."),
        "planned_date": _p("string", "Planned execution date (`YYYY-MM-DD`)."),
        "due_date": _p("string", "Hard due date (`YYYY-MM-DD`)."),
        "blocked_reason": _p("string", "Reason blocking a `blocked` work item."),
        "kind": _p("string", "Project category.", enum=["life", "work", "project", "software", "general"]),
        "workspace_path": _p("string", "Optional local Git repository directory bound to this project."),
        "delivery_policy": _p("string", "Git delivery posture.", enum=["local_only", "remote_branch_pr", "ask_me"]),
    }, ["action"]),
    _tool("calendar_manage", "Inspect or schedule across the Unified Calendar (appointments, focus blocks, sprint task dates, deadlines, routines, and due SRS reviews).", {
        "action": _p("string", "Calendar sub-action.", enum=["window", "today", "week", "month", "list_events", "add_event", "delete_event"]),
        "title": _p("string", "Event title for `add_event`."),
        "start_at": _p("string", "Start date/time (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`)."),
        "end_at": _p("string", "Optional end date/time."),
        "kind": _p("string", "Event category.", enum=["appointment", "focus_block", "milestone", "reminder"]),
        "all_day": _p("boolean", "Set true for all-day entries."),
        "start_date": _p("string", "Window start date (`YYYY-MM-DD`)."),
        "days": _p("integer", "Number of days in `window` (default 35)."),
        "event_id": _p("string", "Event ID (`evt_...`) for `delete_event`."),
        "notes": _p("string", "Optional location or preparation notes."),
    }, ["action"]),
    _tool("education_manage", "Operate courses, pedagogical stages (`priming`..`mastered`), spaced-repetition grading, AI flashcard/quiz generation, Wiki study notes, and inline flashcard decks.", {
        "action": _p("string", "Education sub-action.", enum=["progress", "list_courses", "save_course", "set_active", "advance_stage", "upsert_items", "generate_items", "grade_item", "due", "flashcard_deck", "curate_note"]),
        "course_id": _p("string", "Course slug (defaults to active course)."),
        "title": _p("string", "Course title (`save_course`) or study module title (`curate_note`)."),
        "domain": _p("string", "Subject domain slug."),
        "goal_summary": _p("string", "Learning objective summary for `save_course`."),
        "modules": _p("array", "Ordered list of syllabus module titles.", items_type="string"),
        "module_slug": _p("string", "Target module slug."),
        "stage": _p("string", "Pedagogical stage.", enum=["priming", "dual_coding", "elaboration", "retrieval", "mastered"]),
        "items": {"type": "array", "description": "Batch array of `{prompt, answer, topic, explanation}` objects for `upsert_items`.", "items": {"type": "object"}},
        "prompt": _p("string", "Single flashcard front / question."),
        "answer": _p("string", "Single flashcard back / answer."),
        "topic": _p("string", "Topic focus for `generate_items` or `flashcard_deck`."),
        "count": _p("integer", "Number of flashcards/questions to generate (`generate_items`, default 5)."),
        "item_id": _p("string", "Study item ID (`edu_...`) for `grade_item`."),
        "passed": _p("boolean", "True if recalled accurately (`grade_item`)."),
        "miss_reason": _p("string", "Diagnostic note when `passed=False`."),
        "core_idea": _p("string", "Atomic principle summary for `curate_note`."),
        "mechanism_markdown": _p("string", "Detailed Markdown explanation for `curate_note`."),
        "mermaid_diagram": _p("string", "Optional Mermaid diagram body for `curate_note`."),
        "limit": _p("integer", "Max items for `due` or `flashcard_deck`."),
    }, ["action"]),
    _tool("ops_manage", "Manage background jobs, scheduled/idle routines, Blind Credential Vault metadata, SSH/WinRM/HTTP endpoints, Capability Packs/JSON workflows, workspace focus, checkpoints, telemetry, and runtime health.", {
        "domain": _p("string", "Operational subsystem to manage.", enum=["jobs", "routines", "vault", "endpoints", "workflows", "projects", "checkpoints", "telemetry", "doctor"]),
        "action": _p("string", "Subsystem sub-action (e.g. `list`, `dispatch`, `create`, `propose`, `test`, `inspect_host`, `ssh_exec`, `ssh_read_file`, `winrm_exec`, `api_request`, `run_dag`, `resume_dag`, `bootstrap_skill`, `run_script`, `check_secret`, `status`, `switch`)."),
        "target": _p("string", "Target identifier (`job_id`, `routine_id`, `endpoint_id`, `dag`, `skill`, `name`, or project path)."),
        "params": _p("object", "Additional subsystem parameters dictionary (e.g. `{goal, title, mode, schedule_expr, command, skill_md, scripts, workflows}`)."),
    }, ["domain"]),
    _tool("git_manage", "Execute safe local Git, parallel `git worktree`, secret-shielded commit, and Blind-Vault-authenticated remote operations governed by project `delivery_policy`.", {
        "action": _p("string", "Git or worktree sub-action.", enum=["status", "diff", "log", "branch", "worktree_add", "worktree_list", "worktree_remove", "commit", "fetch", "pull", "push"]),
        "repo_path": _p("string", "Optional repository or worktree path."),
        "branch": _p("string", "Feature/fix branch name."),
        "slug": _p("string", "Short kebab-case slug for `worktree_add`."),
        "worktree_path": _p("string", "Optional worktree checkout path."),
        "message": _p("string", "Descriptive commit message for `commit`."),
        "paths": _p("array", "Explicit relative file paths to stage on `commit`.", items_type="string"),
        "staged": _p("boolean", "Inspect `--cached` diff."),
        "path": _p("string", "Optional relative path filter for `diff`."),
        "remote": _p("string", "Git remote name (default `origin`)."),
        "credential_ref": _p("string", "Optional symbolic Blind Vault slug."),
        "limit": _p("integer", "Recent commit count for `log` (default 10)."),
    }, ["action"]),
    _tool("dev_preview", "Manage isolated local preview servers (`127.0.0.1:<port>`) with scrubbed temp env/DB, ring-buffer logs, HTTP probes, vision critique, and guaranteed teardown.", {
        "action": _p("string", "Preview server sub-action.", enum=["start", "status", "list", "logs", "http_probe", "vision_critique", "stop", "stop_all"]),
        "preview_id": _p("string", "Preview instance ID (`prev_...`)."),
        "cwd": _p("string", "Optional working directory for `start`."),
        "command": _p("string", "Optional startup command override (supports `{port}`)."),
        "port": _p("integer", "Preferred loopback port (0 allocates free port)."),
        "health_path": _p("string", "Readiness URL path to poll on `start`."),
        "timeout": _p("integer", "Readiness timeout seconds (default 20)."),
        "tail": _p("integer", "Recent stdout/stderr lines for `logs` (default 60)."),
        "path": _p("string", "Loopback URL path for `http_probe`."),
        "method": _p("string", "HTTP method (`GET` or `POST`)."),
        "screenshot_paths": _p("array", "Captured PNG file paths for `vision_critique`.", items_type="string"),
        "prompt": _p("string", "Optional visual QA prompt for `vision_critique`."),
    }, ["action"]),
]

ALL_TOOL_NAMES: FrozenSet[str] = frozenset(t["function"]["name"] for t in TOOLS) | frozenset({
    "run_shell", "run_python", "workflow_manage", "checkpoint_manage", "project_manage",
    "task_manage", "telemetry_manage", "system_doctor", "job_manage", "vault_manage",
    "endpoint_manage", "mcp_manage", "routine_manage",
})
_READ_ORIENT_TOOLS: Tuple[str, ...] = (
    "list_dir", "read_file", "search", "web_search", "web_fetch", "call_mcp", "memory_manage",
    "wiki_manage", "agile_manage", "calendar_manage", "education_manage", "ops_manage",
    "git_manage", "dev_preview",
    # Legacy alias names permitted in read-only phases (fine-grained sub-actions still gated below)
    "task_manage", "workflow_manage", "checkpoint_manage", "telemetry_manage", "project_manage",
    "system_doctor", "job_manage", "vault_manage", "endpoint_manage", "mcp_manage", "routine_manage",
)
_REPO_INTERNAL_TOOLS: FrozenSet[str] = frozenset({"checkpoint_manage", "task_manage", "system_doctor", "git_manage", "dev_preview"})
_CODE_MARKERS: FrozenSet[str] = frozenset({".git", "pyproject.toml", "package.json", "cargo.toml", "go.mod", "makefile", "requirements.txt"})
_CODE_EXTS: FrozenSet[str] = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".rs", ".go", ".cs", ".csproj", ".sln", ".java", ".c", ".cpp", ".sh", ".ps1"})

PHASE_ALLOWED_TOOLS: Dict[str, FrozenSet[str]] = {
    "INTAKE": frozenset(_READ_ORIENT_TOOLS),
    "DISCOVER": frozenset(_READ_ORIENT_TOOLS),
    "DESIGN": frozenset(_READ_ORIENT_TOOLS),
    "PLAN": frozenset(_READ_ORIENT_TOOLS),
    "IMPLEMENT": ALL_TOOL_NAMES,
    "RECOVER": ALL_TOOL_NAMES,
    "VERIFY": frozenset((*_READ_ORIENT_TOOLS, "run_command", "run_python", "run_shell")),
    "REFLECT": frozenset(_READ_ORIENT_TOOLS),
}

_PHASE_LOCKED_ACTIONS: Dict[str, FrozenSet[str]] = {
    "checkpoint_manage": frozenset({"rollback", "git_save"}),
    "memory_manage": frozenset({"add_note", "set_profile", "add_adr", "upsert_fact", "sync"}),
    "workflow_manage": frozenset({"start", "transition", "abort", "complete", "recover", "autonomy", "blueprint", "approve", "run_dag", "resume_dag", "run_recipe", "bootstrap_skill", "promote_skill", "run_script", "init_domain_pack", "ensure_domain_pack", "create_domain_pack"}),
    "job_manage": frozenset({"dispatch", "cancel", "resume"}),
    "wiki_manage": frozenset({"capture_inbox", "save", "curate", "graduate", "commit_curation", "revert_curation", "save_template", "create_proxy_note", "deep_curate_job", "consolidate_ontology", "verify_note", "stage_turn", "trash", "restore", "export_chat"}),
    "vault_manage": frozenset({"declare", "request_scope"}),
    "endpoint_manage": frozenset({"propose", "propose_endpoint", "register", "create", "add", "set", "update", "upsert", "remove", "ssh_exec", "winrm_exec", "api_request"}),
    "mcp_manage": frozenset({"propose_server", "register_server", "call_tool", "harvest_skill", "register_partner", "delegate_partner"}),
    "call_mcp": frozenset({"propose_server", "register_server", "call_tool", "harvest_skill", "register_partner", "delegate_partner"}),
    "routine_manage": frozenset({"create", "enable", "disable", "delete", "tick", "run_now", "autonomy_on", "autonomy_off", "prompt_save"}),
    "agile_manage": frozenset({"create_project", "update_project", "save_board", "create_milestone", "save_epic", "delete_epic", "seed_dikw", "create_timebox", "start_sprint", "rollover_sprint", "create_item", "create_card", "update_item", "update_card", "save_card", "move_card", "delete_item", "delete_card", "slice_unblocker", "start_item", "start_card", "dispatch_item", "run_parallel_ready", "run_ready", "merge_item", "merge_local", "push_item_pr", "push_and_pr", "request_changes", "discard_worktree"}),
    "calendar_manage": frozenset({"add_event", "delete_event"}),
    "education_manage": frozenset({"save_course", "set_active", "advance_stage", "upsert_items", "generate_items", "grade_item", "curate_note"}),
    "git_manage": frozenset({"branch", "worktree_add", "worktree_remove", "commit", "fetch", "pull", "push"}),
    "dev_preview": frozenset({"start", "vision_critique", "stop", "stop_all"}),
}
_PHASE_ACTION_EXCEPTIONS: Dict[str, FrozenSet[Tuple[str, str]]] = {
    "VERIFY": frozenset({("workflow_manage", "run_script"), ("dev_preview", "start"), ("dev_preview", "vision_critique"), ("dev_preview", "stop"), ("dev_preview", "stop_all")}),
    "REFLECT": frozenset({("memory_manage", "upsert_fact"), ("memory_manage", "add_note"), ("wiki_manage", "capture_inbox")}),
}


def _workspace_hidden_tools(eff_phase: str) -> FrozenSet[str]:
    ws = settings.get_workspace()
    if ws.is_self_project:
        return frozenset() if eff_phase else _REPO_INTERNAL_TOOLS
    root = ws.project_root
    if eff_phase or any((root / m).exists() for m in _CODE_MARKERS):
        return frozenset({"system_doctor"})
    try:
        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            if any(Path(fn).suffix.lower() in _CODE_EXTS or fn.lower() in _CODE_MARKERS for fn in files):
                return frozenset({"system_doctor"})
            if Path(cur) != root:
                dirs[:] = []
    except OSError:
        pass
    return _REPO_INTERNAL_TOOLS


def filter_tools_for_phase(phase: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return `<20` tool schemas permitted in `phase`, scoping repo-internal tools and appending approved MCP schemas."""
    eff = (phase if phase is not None else current_workflow_phase()).strip().upper()
    allowed, hidden = PHASE_ALLOWED_TOOLS.get(eff), _workspace_hidden_tools(eff)
    base = [t for t in TOOLS if t["function"]["name"] not in hidden and (not allowed or t["function"]["name"] in allowed)]
    if not allowed or eff in ("IMPLEMENT", "RECOVER"):
        try:
            from companion.integrations import get_mcp_hub

            base.extend(get_mcp_hub().list_approved_openai_tools())
        except Exception:
            pass
    return base


def check_phase_tool_permission(tool_name: str, phase: Optional[str] = None, args: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Return a mechanical rejection string if `(tool_name, action)` is disallowed in `phase`."""
    eff = (phase if phase is not None else current_workflow_phase()).strip().upper()
    allowed = PHASE_ALLOWED_TOOLS.get(eff)
    if not allowed:
        return None
    if tool_name not in allowed:
        return (
            f"PHASE TOOL GATE REJECTED `{tool_name}` during `{eff}` phase: "
            f"file-mutating or execution tools outside `{sorted(allowed)}` are mechanically locked in `{eff}`. "
            "Complete discovery/design/planning first, or transition workflow to `IMPLEMENT` before modifying files."
        )
    if args is not None and eff in ("INTAKE", "DISCOVER", "DESIGN", "PLAN", "VERIFY", "REFLECT"):
        if tool_name == "ops_manage":
            dom = str_arg(args, "domain").lower()
            act = str_arg(args, "action", "list").lower()
            mapped = {
                "jobs": "job_manage", "routines": "routine_manage", "vault": "vault_manage",
                "endpoints": "endpoint_manage", "workflows": "workflow_manage",
                "projects": "project_manage", "checkpoints": "checkpoint_manage",
            }.get(dom, "")
            if mapped:
                return check_phase_tool_permission(mapped, phase=eff, args={**(args.get("params") or {}), "action": act, "target": args.get("target")})
            return None
        act = str(args.get("action") or "").strip().lower()
        if tool_name == "project_manage":
            has_tgt = any(str(args.get(k) or "").strip() for k in ("target", "path", "name"))
            if act not in ("", "status", "current", "list") or (not act and has_tgt):
                rej_act = act or "switch"
                return f"PHASE TOOL GATE REJECTED `project_manage(action='{rej_act}')` during `{eff}` phase: mutating or state-overriding sub-action `{rej_act}` is mechanically locked in `{eff}`."
            return None
        locked = _PHASE_LOCKED_ACTIONS.get(tool_name)
        allowed_pair = (tool_name, act) in _PHASE_ACTION_EXCEPTIONS.get(eff, frozenset())
        if locked and act in locked and not allowed_pair:
            return f"PHASE TOOL GATE REJECTED `{tool_name}(action='{act}')` during `{eff}` phase: mutating or state-overriding sub-action `{act}` is mechanically locked in `{eff}`."
    return None


# ---------------------------------------------------------------------------
# 5. Primitive File, Search, Shell/Python & Web Tool Implementations
# ---------------------------------------------------------------------------
def extract_python_symbol(source: str, symbol: str, rel_path: str = "<file>") -> Tuple[Optional[Tuple[int, int, str]], List[str]]:
    """Locate a Python `class`, `def`, or `Class.method` AST span `(start_line, end_line, kind)`."""
    target = (symbol or "").strip()
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return None, []
    available: List[str] = []
    exact_match: Optional[Tuple[int, int, str]] = None
    fallback_match: Optional[Tuple[int, int, str]] = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            available.append(node.name)
            if node.name == target and exact_match is None:
                start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
                exact_match = (start, getattr(node, "end_lineno", node.lineno), "function")
        elif isinstance(node, ast.ClassDef):
            available.append(node.name)
            if node.name == target and exact_match is None:
                start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
                exact_match = (start, getattr(node, "end_lineno", node.lineno), "class")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qual = f"{node.name}.{sub.name}"
                    available.append(qual)
                    start = sub.decorator_list[0].lineno if sub.decorator_list else sub.lineno
                    end = getattr(sub, "end_lineno", sub.lineno)
                    if target == qual and exact_match is None:
                        exact_match = (start, end, "method")
                    elif sub.name == target and fallback_match is None:
                        fallback_match = (start, end, f"method `{qual}`")
    return (exact_match or fallback_match), available


_WIKI_DIRS = ("01_inbox", "02_knowledge", "03_resources", "04_archive")
_SHARED_DATA_PREFIXES = ("data/domains/", "data/skills/", "data/scratch/", "data/wiki/", "data/attachments/")


def resolve(p: str, for_write: bool = False) -> Path:
    """Resolve `p` against active `WorkspaceContext` while mapping Companion `data/` packs cleanly."""
    cand = Path(p)
    if cand.is_absolute():
        return cand
    ws, norm = settings.get_workspace(), p.replace("\\", "/").strip("/")
    if norm in _WIKI_DIRS or norm.startswith(tuple(f"{d}/" for d in _WIKI_DIRS)):
        if ws.is_self_project or not (ws.project_root / p).exists():
            return settings.wiki_dir / norm
    if norm == "data/CONTEXT.md" or norm.startswith(_SHARED_DATA_PREFIXES):
        if ws.is_self_project or not (ws.project_root / p).exists():
            return settings.effective_data_dir / norm[5:]
    if ws.is_self_project and (norm == "data" or norm.startswith("data/")):
        return (settings.effective_data_dir / norm[5:]) if norm.startswith("data/") and norm[5:] else settings.effective_data_dir
    if norm == "scratch" or norm.startswith("scratch/"):
        if ws.is_self_project or not (ws.project_root / p).exists():
            return (settings.scratch_dir / norm[8:]) if norm.startswith("scratch/") and norm[8:] else settings.scratch_dir
    if for_write and ws.is_self_project and not norm.startswith(("companion/", "surfaces/", "frontend/", "skills/", "tests/", ".agents/", ".env", "secrets.env")) and norm not in ("README.md", "harness.py", ".gitignore", "Dockerfile", "docker-compose.yml"):
        return settings.scratch_dir / norm
    if not ws.is_self_project:
        rname = ws.project_root.name.lower()
        if norm.lower() == rname and not (ws.project_root / p).exists():
            return ws.project_root
        if norm.lower().startswith(f"{rname}/") and not (ws.project_root / p).exists():
            sub = norm[len(rname) + 1 :]
            if sub and ((ws.project_root / sub).exists() or (for_write and (ws.project_root / sub).parent.exists())):
                return ws.project_root / sub
        if not for_write and not (ws.project_root / p).exists() and norm.startswith("skills/") and (ws.self_root / p).exists():
            return ws.self_root / p
    return ws.project_root / p


def _closest_line_hint(text: str, old: str) -> str:
    first_probe = next((ln.strip() for ln in old.splitlines() if ln.strip()), "")
    if not first_probe:
        return ""
    file_lines = text.splitlines()
    for idx, fline in enumerate(file_lines):
        if first_probe in fline:
            lo, hi = max(0, idx - 1), min(len(file_lines), idx + len(old.splitlines()) + 2)
            return f"\nExact file content around lines {lo + 1}-{hi} (copy exact indentation):\n```text\n" + "\n".join(file_lines[lo:hi]) + "\n```"
    return ""


_READ_MAX_LINES = 800
_READ_MAX_BYTES = 40_000


def tool_read_file(args: Dict[str, Any]) -> str:
    path_str = str_arg(args, "path", aliases=("file_path", "filepath", "file"))
    if not path_str:
        return "error: path is required"
    p = resolve(path_str, for_write=False)
    path_err = check_path_safety(p, write=False)
    if path_err:
        return path_err
    if not p.is_file():
        return f"error: file not found: {p}"
    raw_text = get_execution_target().read_text(p)
    lines = raw_text.splitlines()
    if not lines:
        return f"# {p}  (empty file)"
    symbol = str_arg(args, "symbol")
    if symbol and p.suffix.lower() == ".py":
        span, avail = extract_python_symbol(raw_text, symbol, rel_path=p.name)
        if span is None:
            return f"error: symbol `{symbol}` not found in {p}. Available symbols: {', '.join(avail[:25]) or '(no top-level symbols)'}"
        s_ln, e_ln, kind = span
        return f"# {p}  (symbol `{symbol}` [{kind}], lines {s_ln}-{e_ln} of {len(lines)})\n" + "\n".join(f"{i:5d}|{lines[i - 1]}" for i in range(s_ln, min(e_ln, len(lines)) + 1))
    start = max(1, int_arg(args, "start_line", 1) or 1)
    req_end = int_arg(args, "end_line", 0) or len(lines)
    if start > min(len(lines), req_end):
        return "error: start_line must be <= end_line"
    capped_end = min(len(lines), req_end, start + _READ_MAX_LINES - 1)
    selected: List[str] = []
    byte_used, actual_end = 0, start - 1
    for i in range(start, capped_end + 1):
        row = f"{i:5d}|{lines[i - 1]}"
        row_bytes = len(row.encode("utf-8", errors="replace")) + 1
        if selected and byte_used + row_bytes > _READ_MAX_BYTES:
            break
        selected.append(row)
        byte_used += row_bytes
        actual_end = i
    trunc_note = f"\n[Showing lines {start}-{actual_end} of {len(lines)}; pass start_line={actual_end + 1} to continue]" if actual_end < min(len(lines), req_end) else ""
    return f"# {p}  (lines {start}-{actual_end} of {len(lines)})\n" + "\n".join(selected) + trunc_note


def tool_write_file(args: Dict[str, Any]) -> str:
    path_str = str_arg(args, "path", aliases=("file_path", "filepath", "file"))
    raw_c = next((args[k] for k in ("content", "text", "body") if args.get(k) is not None), None)
    if not path_str or raw_c is None:
        return "error: path is required" if not path_str else "error: content is required"
    p, norm_content = resolve(path_str, for_write=True), str(raw_c).replace("\r\n", "\n")
    path_err = check_path_safety(p, write=True)
    if path_err:
        return path_err
    if p.is_dir():
        return f"error: target path is an existing directory, not a file: {p}"
    hard_err, lint_warn = lint_single_file_edit(p, norm_content, op_name="write_file", governed_root=settings.active_project_root)
    if hard_err:
        return hard_err
    get_execution_target().write_text(p, norm_content, backup=True)
    return f"wrote {len(norm_content)} chars to {p}{lint_warn}"


def tool_edit_file(args: Dict[str, Any]) -> str:
    path_str = str_arg(args, "path", aliases=("file_path", "filepath", "file"))
    if not path_str:
        return "error: path is required"
    p = resolve(path_str, for_write=True)
    path_err = check_path_safety(p, write=True)
    if path_err:
        return path_err
    if not p.is_file():
        return f"error: file not found: {p}"
    old_raw = next((args[k] for k in ("old_str", "old_string", "old_text", "target") if args.get(k) is not None), None)
    new_raw = next((args[k] for k in ("new_str", "new_string", "new_text", "replacement") if args.get(k) is not None), None)
    if not isinstance(old_raw, str) or not old_raw or new_raw is None:
        return "error: old_str must be a non-empty string" if not isinstance(old_raw, str) or not old_raw else "error: new_str is required"
    target = get_execution_target()
    old, new = old_raw.replace("\r\n", "\n"), str(new_raw).replace("\r\n", "\n")
    text = target.read_text(p).replace("\r\n", "\n")
    n = text.count(old)
    if n == 0:
        shifted = try_indent_shifted_replace(text, old, new)
        if shifted is None:
            return f"error: old_str not found in file (must match EXACTLY, including whitespace){_closest_line_hint(text, old)}"
        updated_text = shifted
    elif n > 1:
        return f"error: old_str matches {n} times - include more surrounding context so it is unique"
    else:
        if (
            old == old.lstrip(" ")
            and new.startswith(("def ", "async def "))
            and ("(self" in new.splitlines()[0] or "(cls" in new.splitlines()[0])
            and len(new.splitlines()) > 1
            and new.splitlines()[1].startswith("        ")
        ):
            new = "    " + new
        updated_text = text.replace(old, new, 1)
    if p.suffix.lower() == ".py" and not validate_python_syntax(updated_text, filename=str(p))[0] and n == 1 and "\n" in new:
        n_lns = new.split("\n")
        for s in (-2, 2, -4, 4):
            cand = text.replace(
                old,
                "\n".join([n_lns[0]] + [("" if not ln.strip() else " " * max(0, len(ln) - len(ln.lstrip(" ")) + s) + ln.lstrip(" ")) for ln in n_lns[1:]]),
                1,
            )
            if validate_python_syntax(cand, filename=str(p))[0]:
                updated_text = cand
                break
    hard_err, lint_warn = lint_single_file_edit(p, updated_text, op_name="edit_file", governed_root=settings.active_project_root)
    if hard_err:
        return hard_err
    target.write_text(p, updated_text, backup=True)
    return f"edited {p} (replaced 1 occurrence){lint_warn}"


def tool_list_dir(args: Dict[str, Any]) -> str:
    p = resolve(str_arg(args, "path", ".", aliases=("dir_path", "directory", "dir")) or ".", for_write=False)
    path_err = check_path_safety(p, write=False)
    if path_err:
        return path_err
    if not p.is_dir():
        return f"error: directory not found: {p}"
    names = get_execution_target().list_dir(p)
    return f"{p}/  (empty)" if not names else f"{p}/\n" + "\n".join(names)


_BINARY_EXTS = {".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3", ".pyc", ".pyo", ".pyd", ".exe", ".dll", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip"}


def tool_search(args: Dict[str, Any]) -> str:
    pattern, file_glob = str_arg(args, "pattern", aliases=("query", "search_term")), str_arg(args, "file_glob", aliases=("glob", "include")).lower()
    if not pattern:
        return "error: pattern is required"
    base = resolve(str_arg(args, "path", ".", aliases=("dir_path", "directory")) or ".", for_write=False)
    path_err = check_path_safety(base, write=False)
    if path_err:
        return path_err
    try:
        rx = re.compile(pattern) if bool_arg(args, "is_regex", False) else None
    except re.error as e:
        return f"error: invalid regex: {e}"
    in_static = "static" in base.parts and "surfaces" in base.parts
    needle, matches, files = pattern.lower(), [], ([base] if base.is_file() else [])
    if base.is_dir():
        for root, dirs, fnames in os.walk(base):
            root_p = Path(root)
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and (in_static or not (d == "static" and root_p.name == "surfaces"))]
            files.extend(root_p / fn for fn in fnames if Path(fn).suffix.lower() not in _BINARY_EXTS and fn.lower() not in (".env", "secrets.env") and (not file_glob or fnmatch.fnmatch(fn.lower(), file_glob)))
    elif not base.is_file():
        return f"error: path not found: {base}"
    target = get_execution_target()
    for f in files:
        if len(matches) >= settings.search_limit:
            break
        try:
            for i, line in enumerate(target.read_text(f).splitlines(), 1):
                if rx.search(line) if rx else (needle in line.lower()):
                    matches.append(f"{f}:{i}: {line.strip()[:200]}")
                    if len(matches) >= settings.search_limit:
                        break
        except Exception:
            continue
    return "no matches" if not matches else f"{len(matches)} match(es)\n" + "\n".join(matches)


def tool_run_python(args: Dict[str, Any]) -> str:
    """Execute Python code directly in the active `ExecutionTarget` with UTF-8 encoding and spillover."""
    code = str(next((args[k] for k in ("code", "python_code", "script", "source") if args.get(k) is not None), "") or "")
    cmd_err = check_command_safety(code)
    if cmd_err:
        return cmd_err
    return get_execution_target().run_python_code(code, timeout=settings.shell_timeout)


_HEREDOC_RE = re.compile(r"python(?:3)?\s+<<-?\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\1\b", re.DOTALL)
_MULTILINE_PY_C_RE = re.compile(r'^(?:cd\s+[^\n&]+&&\s*)?python(?:3)?\s+-c\s+(["\'])(.*)\1\s*$', re.DOTALL)
_HEAD_TAIL_PIPE_RE = re.compile(r"\s*\|\s*(head|tail)(?:\s+-n\s*|\s+-)?(\d+)?\s*$", re.IGNORECASE)


def tool_run_shell(args: Dict[str, Any]) -> str:
    cmd = str(next((args[k] for k in ("command", "cmd", "shell_command") if args.get(k) is not None), "") or "")
    if not cmd.strip():
        return "error: empty command"
    cmd_err = check_command_safety(cmd)
    if cmd_err:
        return cmd_err
    heredoc_match = _HEREDOC_RE.search(cmd)
    if heredoc_match:
        return tool_run_python({"code": heredoc_match.group(2)})
    py_c_match = _MULTILINE_PY_C_RE.match(cmd.strip()) if "\n" in cmd.strip() else None
    if py_c_match:
        return tool_run_python({"code": py_c_match.group(2)})
    cmd, ignore_err = normalize_windows_shell_command(cmd)
    pipe_op, pipe_n, pm = None, 10, (_HEAD_TAIL_PIPE_RE.search(cmd) if os.name == "nt" else None)
    if pm:
        pipe_op, pipe_n, cmd = pm.group(1).lower(), (int(pm.group(2)) if pm.group(2) else 10), cmd[: pm.start()].strip()
    extra_env = {"COMPANION_MAX_ITERS": "4"} if "harness.py" in cmd else None
    rc_raw, stdout_str, stderr_str, guard_err = get_execution_target().exec_command(
        cmd, shell=True, timeout=settings.shell_timeout, extra_env=extra_env
    )
    if rc_raw == -1 and guard_err:
        return f"error: command {guard_err}: {cmd}"
    if guard_err:
        return f"exit=1\n{guard_err}"
    if pipe_op and stdout_str:
        lines = stdout_str.splitlines()
        stdout_str = "\n".join(lines[:pipe_n] if pipe_op == "head" else lines[-pipe_n:])
    rc = 0 if ignore_err else rc_raw
    out = (stdout_str + (("\n[stderr]\n" + stderr_str) if stderr_str and not ignore_err else "")).strip() or "(no output)"
    return f"exit={rc}\n{spill_tool_output(out, label='run_command')}"


def tool_run_command(args: Dict[str, Any]) -> str:
    """Unified command runner accepting either `python_code` (`code`) or shell `command`."""
    if any(args.get(k) not in (None, "") for k in ("python_code", "code", "script", "source")):
        return tool_run_python(args)
    return tool_run_shell(args)


_BLOCKED_FETCH_PORTS = {21, 22, 23, 25, 135, 139, 445, 3306, 3389, 5432, 6379, 11211, 27017}
_BLOCKED_METADATA_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}
_MAX_WEB_FETCH_BYTES = 524_288


class _HtmlToMarkdownParser(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
    _BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "main", "aside", "tr", "blockquote", "br", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip, self._in_pre, self._href = 0, False, ""
        self._link_buf: List[str] = []
        self._out: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._out.append(f"\n\n{'#' * int(t[1])} ")
        elif t == "li":
            self._out.append("\n- ")
        elif t == "pre":
            self._in_pre = True
            self._out.append("\n```\n")
        elif t == "code" and not self._in_pre:
            self._out.append("`")
        elif t == "a":
            href = dict(attrs).get("href") or ""
            if href and not href.lower().startswith("javascript:"):
                self._href, self._link_buf = href, []
        elif t in self._BLOCK_TAGS:
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if t in ("h1", "h2", "h3", "h4", "h5", "h6") or t in self._BLOCK_TAGS:
            self._out.append("\n")
        elif t == "pre":
            self._in_pre = False
            self._out.append("\n```\n")
        elif t == "code" and not self._in_pre:
            self._out.append("`")
        elif t == "a" and self._href:
            lbl = "".join(self._link_buf).strip()
            self._out.append(f"[{lbl}]({self._href})" if lbl and lbl != self._href else (lbl or self._href))
            self._href, self._link_buf = "", []

    def handle_data(self, data: str) -> None:
        if not self._skip and data:
            cleaned = data if self._in_pre else re.sub(r"[ \t\r\n]+", " ", data)
            (self._link_buf if self._href else self._out).append(cleaned)

    def get_markdown(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "\n".join(ln.rstrip() for ln in "".join(self._out).splitlines())).strip()


class _DdgHtmlSearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[Dict[str, str]] = []
        self._mode, self._cur_href = "", ""
        self._buf: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        ad = dict(attrs)
        cls = set((ad.get("class") or "").split())
        if tag.lower() == "a" and "result__a" in cls:
            href = str(ad.get("href") or "").strip()
            if "uddg=" in href:
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
                href = (qs.get("uddg") or [href])[0]
            self._mode, self._cur_href, self._buf = "title", href, []
        elif "result__snippet" in cls and self.results:
            self._mode, self._buf = "snippet", []

    def handle_endtag(self, tag: str) -> None:
        if self._mode == "title" and tag.lower() == "a":
            title = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if title and self._cur_href.startswith(("http://", "https://")):
                self.results.append({"title": title, "url": self._cur_href, "snippet": ""})
            self._mode, self._buf, self._cur_href = "", [], ""
        elif self._mode == "snippet" and tag.lower() in ("a", "div", "span", "td"):
            if self.results and not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self._mode, self._buf = "", []

    def handle_data(self, data: str) -> None:
        if self._mode and data:
            self._buf.append(data)


def _validate_web_fetch_url(url: str) -> Optional[str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "error: valid http:// or https:// url is required"
    host = parsed.hostname.strip().lower()
    if host in _BLOCKED_METADATA_HOSTS:
        return f"SAFETY GUARD REJECTED: cloud metadata host blocked (`{host}`)"
    return check_url_ssrf(url, allow_private_lan=False, blocked_ports=tuple(_BLOCKED_FETCH_PORTS))


def tool_web_fetch(args: Dict[str, Any]) -> str:
    url = str_arg(args, "url", aliases=("uri", "href"))
    url_err = _validate_web_fetch_url(url)
    if url_err:
        return url_err
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "companion-os/4.2"})
        with urlopen_safe(req, timeout=30, allow_private_lan=False, allow_redirects=True, blocked_ports=tuple(_BLOCKED_FETCH_PORTS)) as resp:
            ctype, raw = (resp.headers.get("Content-Type") or "").lower(), resp.read(_MAX_WEB_FETCH_BYTES + 1)
    except Exception as e:
        if "SAFETY GUARD REJECTED" in str(e):
            return str(e)
        return f"error: {type(e).__name__}: {e}"
    byte_capped, text = len(raw) > _MAX_WEB_FETCH_BYTES, raw[:_MAX_WEB_FETCH_BYTES].decode("utf-8", "replace")
    if "application/json" in ctype:
        try:
            text = json.dumps(json.loads(text), indent=2)
        except Exception:
            pass
    elif not bool_arg(args, "raw", False) and ("text/html" in ctype or text.lstrip()[:120].lower().startswith(("<!doctype html", "<html"))):
        parser = _HtmlToMarkdownParser()
        try:
            parser.feed(text)
            text = parser.get_markdown() or text
        except Exception:
            pass
    spilled = spill_tool_output(text, label="web_fetch")
    if byte_capped and len(text) <= settings.out_limit:
        return spilled + "\n... [stream capped at 512 KB]"
    return spilled


def tool_web_search(args: Dict[str, Any]) -> str:
    query = str_arg(args, "query", aliases=("q", "pattern"))
    if not query:
        return "error: query is required"
    max_res = max(1, min(10, int_arg(args, "max_results", 5)))
    custom_url = os.environ.get("COMPANION_SEARCH_URL", "").strip()
    is_lan_custom = bool(custom_url and is_local_or_lan_endpoint(custom_url))
    if settings.effective_privacy_mode() == "local_only" and not is_lan_custom:
        return (
            "SAFETY GUARD REJECTED (PRIVACY LOCK): `web_search` is blocked while Privacy Posture is `local_only` "
            "(unless `COMPANION_SEARCH_URL` points to a local/LAN SearXNG endpoint). Switch posture to `hybrid` or `cloud_allowed` in Settings -> Models & Tiers."
        )
    try:
        if custom_url:
            sep = "&" if "?" in custom_url else "?"
            url = f"{custom_url}{sep}{urllib.parse.urlencode({'q': query, 'format': 'json'})}"
            req = urllib.request.Request(url, headers={"User-Agent": "companion-os/4.2", "Accept": "application/json"})
            with urlopen_safe(req, timeout=20, allow_private_lan=is_lan_custom, allow_redirects=True) as resp:
                data = json.loads(resp.read(524_288).decode("utf-8", "replace"))
            raw_items = data.get("results") if isinstance(data, dict) else []
            hits = [
                {"title": str(it.get("title") or ""), "url": str(it.get("url") or ""), "snippet": str(it.get("content") or it.get("snippet") or "")}
                for it in (raw_items or [])[:max_res] if isinstance(it, dict) and it.get("url")
            ]
        else:
            url = f"https://html.duckduckgo.com/html/?{urllib.parse.urlencode({'q': query})}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; companion-os/4.2)"})
            with urlopen_safe(req, timeout=20, allow_private_lan=False, allow_redirects=True) as resp:
                html_text = resp.read(524_288).decode("utf-8", "replace")
            parser = _DdgHtmlSearchParser()
            parser.feed(html_text)
            hits = parser.results[:max_res]
    except Exception as exc:
        return str(exc) if "SAFETY GUARD REJECTED" in str(exc) else f"error: web_search failed ({type(exc).__name__}): {exc}"
    if not hits:
        return f"Web Search (`{query}`): no results found."
    rows = [f"{idx}. **{h['title']}** — {h['url']}" + (f"\n   {h['snippet'][:240]}" if h.get("snippet") else "") for idx, h in enumerate(hits, 1)]
    return f"Web Search (`{query}`): {len(hits)} result(s)\n" + "\n".join(rows)


# ---------------------------------------------------------------------------
# 6. Memory, Domain Pillars, Subagent, MCP & Operational Tool Handlers
# ---------------------------------------------------------------------------
_BLIND_VAULT_ALLOWED_SUBACTIONS = {"list", "check", "check_secret", "metadata", "list_missing", "declare", "request_scope", "grant_scope", "audit"}


def _exec_core(domain: str, params: Dict[str, Any], actor: str = "companion", store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    from companion.core_actions import execute_core_action

    return execute_core_action(domain, params, actor=actor, store=store)


def _format_tool_result(res: Dict[str, Any]) -> str:
    if res.get("awaiting_approval"):
        return f"AWAITING_USER_APPROVAL (`{res.get('approval_id', '')}`): {res.get('error') or res.get('summary') or 'Awaiting user approval.'}"
    if not res.get("ok", False):
        return f"error: {res.get('error') or res.get('summary') or 'Operation failed.'}"
    if res.get("markdown_block"):
        return (
            f"{res.get('summary', '')}\n\n"
            "[INTERNAL NOTE FOR ASSISTANT — DO NOT ASK THE USER TO COPY ANYTHING: "
            "Companion's Chat UI automatically renders the fenced block below as a live interactive widget in your reply.]\n"
            f"{res['markdown_block']}"
        ).strip()
    if "output" in res and res.get("summary"):
        return f"{res['summary']}\n{res['output']}"
    if "output" in res:
        return str(res["output"])
    if "body" in res and "status_code" in res:
        return f"[HTTP {res['status_code']}]\n{res['body']}"
    if res.get("summary"):
        return str(res["summary"])
    return json.dumps(res, indent=2, default=str)


def _clean_agent_params(args: Dict[str, Any], subaction: str) -> Dict[str, Any]:
    params = {k: v for k, v in args.items() if k != "confirmed"}
    params["subaction"] = subaction
    return params


def tool_memory_manage(args: Dict[str, Any]) -> str:
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
        if settings.get_workspace().is_self_project:
            return "SAFETY GUARD REJECTED: `add_adr` is disabled inside Companion's immutable `app/` runtime. Focus an external Project clone (`workspace_path`) or save an ADR note in `wiki_manage`."
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
        return f"Recorded ADR #{aid} ('{title}') and synced MEMORY.md."
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
        return f"Synchronized SQLite memory to {md_path}."
    return f"error: unknown memory_manage action '{action}'."


def tool_task_manage(args: Dict[str, Any]) -> str:
    action, store = str_arg(args, "action", "list_all").lower(), get_memory_store()
    if action == "add_task":
        desc = str_arg(args, "description", aliases=("title", "task"))
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
        desc = str_arg(args, "description", aliases=("title", "task"))
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


def tool_job_manage(args: Dict[str, Any]) -> str:
    return _format_tool_result(_exec_core("jobs", _clean_agent_params(args, str(args.get("action") or "list").strip().lower()), actor="companion"))


def tool_wiki_manage(args: Dict[str, Any]) -> str:
    return _format_tool_result(_exec_core("wiki", _clean_agent_params(args, str(args.get("action") or "list").strip().lower()), actor="companion"))


def tool_vault_manage(args: Dict[str, Any]) -> str:
    sub = str(args.get("action") or "list").strip().lower()
    if sub not in _BLIND_VAULT_ALLOWED_SUBACTIONS:
        return (
            f"error: VAULT ZERO-REVEAL POLICY: Companion tool `vault_manage` only supports symbolic metadata actions "
            f"{sorted(_BLIND_VAULT_ALLOWED_SUBACTIONS)} — raw secret values can never be read, set, or deleted by Companion."
        )
    return _format_tool_result(_exec_core("vault", _clean_agent_params(args, sub), actor="companion"))


def tool_endpoint_manage(args: Dict[str, Any]) -> str:
    return _format_tool_result(_exec_core("endpoints", _clean_agent_params(args, str(args.get("action") or "list").strip().lower()), actor="companion"))


def tool_mcp_manage(args: Dict[str, Any]) -> str:
    return _format_tool_result(_exec_core("mcp", _clean_agent_params(args, str(args.get("action") or "list").strip().lower()), actor="companion"))


tool_call_mcp = tool_mcp_manage


def tool_routine_manage(args: Dict[str, Any]) -> str:
    sub = str(args.get("action") or "list").strip().lower()
    if sub in ("autonomy_status", "autonomy_on", "autonomy_off", "morning"):
        return _format_tool_result(_exec_core("autonomy", _clean_agent_params(args, sub.removeprefix("autonomy_")), actor="companion"))
    if sub.startswith("prompt_"):
        return _format_tool_result(_exec_core("prompts", _clean_agent_params(args, sub.removeprefix("prompt_")), actor="companion"))
    if sub == "list_approvals":
        return _format_tool_result(_exec_core("approvals", {"subaction": "list", "status": "waiting"}, actor="companion"))
    return _format_tool_result(_exec_core("routines", _clean_agent_params(args, sub), actor="companion"))


def tool_agile_manage(args: Dict[str, Any]) -> str:
    return _format_tool_result(_exec_core("agile", _clean_agent_params(args, str(args.get("action") or "board_view").strip().lower()), actor="companion"))


def tool_calendar_manage(args: Dict[str, Any]) -> str:
    return _format_tool_result(_exec_core("calendar", _clean_agent_params(args, str(args.get("action") or "window").strip().lower()), actor="companion"))


def tool_education_manage(args: Dict[str, Any]) -> str:
    return _format_tool_result(_exec_core("education", _clean_agent_params(args, str(args.get("action") or "progress").strip().lower()), actor="companion"))


def tool_delegate_subagent(args: Dict[str, Any]) -> str:
    """Spawn a single isolated subagent worker or parallel worktree subagents."""
    if isinstance(args.get("tasks"), list) and args.get("tasks"):
        from companion.worktrees import format_parallel_subagents_tool

        return format_parallel_subagents_tool(args)
    goal = str_arg(args, "goal")
    if not goal:
        return "error: goal (or non-empty tasks list) is required for delegate_subagent"
    from companion.agent import run_subagent

    skill = (str_arg(args, "skill") or str_arg(args, "skill_name")) or None
    tier = str_arg(args, "tier") or None
    inherit_att = bool_arg(args, "inherit_attachments", False)
    out = run_subagent(
        goal=goal,
        skill_name=skill,
        max_iters=int_arg(args, "max_iters", 8),
        verbose=False,
        tier=tier,
        inherit_attachments=inherit_att,
    )
    return f"=== Subagent Worker Result (`{skill or 'worker'}` | tier=`{tier or 'fast'}`) ===\n{out}"


def _handle_skill_forge_action(action: str, args: Dict[str, Any]) -> str:
    from companion.workflows import check_make_vs_borrow, run_skill_script, stage_and_promote_skill

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
        default_scope = "user" if settings.get_workspace().is_self_project else "core"
        eff_scope = str_arg(args, "scope", default_scope) or default_scope
        target_root = None if eff_scope == "user" else settings.active_project_root
        return str(
            stage_and_promote_skill(
                name=str_arg(args, "skill"),
                skill_md=str(args.get("skill_md") or ""),
                scripts=raw_scripts or None,
                dags=raw_dags or None,
                references=_dict_arg(args, "references"),
                overwrite=bool_arg(args, "overwrite", False),
                root=target_root,
                scope=eff_scope,
            )["summary"]
        )
    raw_sa = args.get("script_args") if args.get("script_args") is not None else args.get("args")
    if isinstance(raw_sa, str) and raw_sa.strip().startswith("["):
        try:
            raw_sa = json.loads(raw_sa)
        except Exception:
            pass
    return run_skill_script(
        skill_name=str_arg(args, "skill", aliases=("skill_name",)),
        script_name=str_arg(args, "script", aliases=("script_name",)),
        script_args=[str(x) for x in raw_sa] if isinstance(raw_sa, list) else None,
    )


def _handle_workflow_state_action(action: str, args: Dict[str, Any]) -> str:
    from companion.agent import workflow_engine

    if action == "start":
        goal = str_arg(args, "goal")
        if not goal:
            return "error: goal is required to start a workflow"
        wf_type = str_arg(args, "workflow_type", "engineering_feature") or "engineering_feature"
        if settings.get_workspace().is_self_project:
            return (
                f"SAFETY GUARD REJECTED: `workflow_manage(action='start', workflow_type='{wf_type}')` is strictly for external repository builds "
                "and Companion's installed `app/` runtime is permanently immutable. "
                "For personal/work goals use `agile_manage` on Boards, or bind a Project `workspace_path` to a repository clone."
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
    action = str_arg(args, "action", "status").lower()
    if current_workflow_phase() and action in ("start", "transition", "abort", "complete", "recover", "autonomy", "blueprint", "approve"):
        return (
            f"PHASE TOOL GATE REJECTED `workflow_manage(action='{action}')`: the outer autonomous driver already manages "
            f"workflow state (`{current_workflow_phase()}`). Finish your current task directly and stop calling tools."
        )
    if action in ("start", "transition", "abort", "complete", "recover", "status"):
        return _handle_workflow_state_action(action, args)
    if action in ("subagent", "parallel_subagents"):
        return tool_delegate_subagent(args)
    from companion.agent import (
        approve_blueprint_and_build,
        create_or_revise_blueprint,
        ensure_dag_runtime_bound,
        run_autonomous_goal,
        run_http_smoke_probe,
        workflow_engine,
    )

    if action in ("init_domain_pack", "ensure_domain_pack", "create_domain_pack"):
        from companion.workflows import ensure_domain_pack

        pack_name = str_arg(args, "name") or str_arg(args, "pack") or str_arg(args, "domain") or str_arg(args, "skill_name")
        res = ensure_domain_pack(name=pack_name, description=str_arg(args, "description"))
        return json.dumps(res, indent=2, default=str)
    if action in ("list_dags", "list_recipes"):
        return workflow_engine.format_dags_catalog()
    if action in ("check_skill", "bootstrap_skill", "promote_skill", "run_script"):
        return _handle_skill_forge_action(action, args)
    if action in ("run_dag", "run_recipe"):
        dag_name = str_arg(args, "dag") or str_arg(args, "dag_name") or str_arg(args, "recipe") or str_arg(args, "recipe_name") or str_arg(args, "goal")
        if not dag_name:
            return "error: dag is required for run_dag"
        ensure_dag_runtime_bound()
        raw_params = _dict_arg(args, "params", "recipe_inputs", "inputs") or {}
        res = workflow_engine.run_dag(dag_name, params=raw_params)
        return str(res.get("summary") or json.dumps(res, indent=2, default=str))
    if action == "resume_dag":
        run_id = str_arg(args, "run_id") or str_arg(args, "goal")
        if not run_id:
            return "error: run_id is required for resume_dag"
        ensure_dag_runtime_bound()
        res = workflow_engine.resume_dag(run_id)
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
    action, active_cp, target = str_arg(args, "action", "list").lower(), get_active_checkpoint_manager(), get_execution_target()
    if action == "create":
        m = target.create_checkpoint(label=str_arg(args, "label", "manual", aliases=("message", "name")) or "manual")
        return f"Created workspace checkpoint `{m['checkpoint_id']}` ({m['file_count']} files)."
    if action == "list":
        cps = target.list_checkpoints()
        return "\n".join(f"- {c['checkpoint_id']} (label='{c['label']}', files={c['file_count']})" for c in cps) if cps else "No checkpoints."
    if action == "rollback":
        path_err = check_path_safety(active_cp.root / "__checkpoint_rollback__", write=True)
        if path_err:
            return path_err
        res = target.rollback_checkpoint(checkpoint_id=str_arg(args, "checkpoint_id") or None, force=bool_arg(args, "force", False))
        return f"Rolled back to `{res['checkpoint_id']}` ({res['restored_count']} files)." if res.get("restored") else f"error: {res.get('error')}"
    if action == "git_save":
        res = active_cp.create_git_savepoint(message=str_arg(args, "label", "Manual save-point", aliases=("message", "name")) or "Manual save-point", init_if_needed=bool_arg(args, "init_git", False))
        return str(res.get("summary", ""))
    if action == "git_log":
        logs = active_cp.list_git_savepoints(limit=10)
        return "\n".join(f"- `{e['commit']}` ({e['date']}): {e['subject']}" for e in logs) if logs else "No Git commits found in active workspace."
    return f"error: unknown checkpoint_manage action '{action}'."


def tool_project_manage(args: Dict[str, Any]) -> str:
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


def tool_ops_manage(args: Dict[str, Any]) -> str:
    """Unified operational dispatcher across `jobs`, `routines`, `vault`, `endpoints`, `workflows`, `projects`, `checkpoints`, `telemetry`, `doctor`."""
    dom = str_arg(args, "domain", "jobs").lower()
    sub_params = dict(_dict_arg(args, "params") or {})
    merged: Dict[str, Any] = {**sub_params, **{k: v for k, v in args.items() if k not in ("domain", "params")}}
    tgt = str_arg(args, "target")
    if dom == "jobs":
        if tgt and not merged.get("job_id"):
            merged["job_id"] = tgt
        return tool_job_manage(merged)
    if dom == "routines":
        if tgt and not merged.get("routine_id"):
            merged["routine_id"] = tgt
        return tool_routine_manage(merged)
    if dom == "vault":
        if tgt and not merged.get("name"):
            merged["name"] = tgt
        return tool_vault_manage(merged)
    if dom == "endpoints":
        if tgt and not merged.get("endpoint_id"):
            merged["endpoint_id"] = tgt
        return tool_endpoint_manage(merged)
    if dom == "workflows":
        act = str_arg(merged, "action", "status").lower()
        if tgt:
            if act in ("run_dag", "run_recipe"):
                merged.setdefault("dag", tgt)
            elif act == "resume_dag":
                merged.setdefault("run_id", tgt)
            elif act in ("bootstrap_skill", "promote_skill", "check_skill", "run_script"):
                merged.setdefault("skill", tgt)
            else:
                merged.setdefault("goal", tgt)
        return tool_workflow_manage(merged)
    if dom == "projects":
        return tool_project_manage(merged)
    if dom == "checkpoints":
        if tgt and not merged.get("checkpoint_id"):
            merged["checkpoint_id"] = tgt
        return tool_checkpoint_manage(merged)
    if dom == "telemetry":
        if tgt and not merged.get("trace_id"):
            merged["trace_id"] = tgt
        return tool_telemetry_manage(merged)
    if dom == "doctor":
        return tool_system_doctor(merged)
    return f"error: unknown ops_manage domain '{dom}'."


def tool_git_manage(args: Dict[str, Any]) -> str:
    from companion.worktrees import tool_git_manage as _impl

    return _impl(args)


def tool_dev_preview(args: Dict[str, Any]) -> str:
    from companion.worktrees import tool_dev_preview as _impl

    return _impl(args)


# ---------------------------------------------------------------------------
# 7. CLI Slash Router, Step Summarizer & Central `dispatch()`
# ---------------------------------------------------------------------------
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


def dispatch_harvest_slash(verb: str, arg: str, store: Optional[MemoryStore] = None) -> str:
    """Unified CLI/REPL slash handler routing `/jobs`, `/wiki`, `/vault`, `/endpoints`, `/mcp`, `/partners`, etc."""
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
            p = {"subaction": sub if sub in ("graph", "reindex", "export_chat", "tag_authority", "list_trash", "list") else "list", "folder": sub if sub.startswith("0") else ""}
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
    elif v in ("/calendar", "/study", "/providers"):
        from companion.calendar_engine import parse_calendar_slash
        from companion.education_engine import parse_education_slash
        from companion.model_registry import parse_providers_slash

        slash_map = {"/calendar": ("calendar", parse_calendar_slash), "/study": ("education", parse_education_slash), "/providers": ("models", parse_providers_slash)}
        act_name, fn = slash_map[v]
        p = fn(parts)
    else:
        act_name, p = "identity", {"subaction": "get"}
    res = _exec_core(act_name, p, actor="cli", store=store)
    if res.get("markdown_block"):
        return f"{res.get('summary', '')}\n\n{res['markdown_block']}".strip()
    if res.get("summary") and res.get("output"):
        return f"{res['summary']}\n{res['output']}"
    return str(res.get("summary") or res.get("output") or res.get("error") or json.dumps(res, indent=2))


def summarize_tool_step(name: str, args: Dict[str, Any]) -> str:
    """Format a concise human-readable 1-line label for live SSE and persisted `meta_json` tool pills."""
    a = args or {}
    act, pth = str_arg(a, "action") or str_arg(a, "subaction"), str_arg(a, "path")
    if name == "read_file":
        rng = f" ({a['symbol']})" if a.get("symbol") else (f":{a.get('start_line', 1)}-{a['end_line']}" if a.get("end_line") else "")
        return f"Read {pth or 'file'}{rng}"
    if name in ("write_file", "edit_file"):
        return f"{'Wrote' if name == 'write_file' else 'Edited'} {pth or 'file'}"
    if name in ("search", "web_search"):
        q = str_arg(a, "pattern") or str_arg(a, "query")
        return f"{'Web search' if name == 'web_search' else 'Searched'} \"{q[:48]}\""
    if name in ("run_command", "run_shell", "run_python"):
        if name == "run_python" or a.get("python_code") or a.get("code"):
            return "Executed Python snippet"
        return f"Ran {str_arg(a, 'command')[:52]}"
    if name == "delegate_subagent" or (name == "workflow_manage" and act == "parallel_subagents"):
        raw_t = a.get("tasks")
        if isinstance(raw_t, list) and raw_t:
            return f"Ran {len(raw_t)} parallel subagents"
        return f"Delegated subagent: {str_arg(a, 'goal')[:42]}"
    if name == "wiki_manage":
        tgt = pth or str_arg(a, "title") or str_arg(a, "query") or str_arg(a, "domain")
        verb = "Saved Wiki" if act in ("save", "curate", "capture_inbox") else f"Wiki {act or 'list'}"
        return f"{verb} {tgt[:52]}".strip()
    if name == "ops_manage":
        dom = str_arg(a, "domain", "jobs")
        return f"ops({dom}.{act or 'list'})"
    tgt_any = pth or str_arg(a, "title") or str_arg(a, "endpoint_id") or str_arg(a, "server_id") or str_arg(a, "query")
    return f"{name}({act}{': ' + tgt_any[:36] if tgt_any else ''})" if act else name


TOOL_IMPLS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "list_dir": tool_list_dir,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "search": tool_search,
    "run_command": tool_run_command,
    "web_search": tool_web_search,
    "web_fetch": tool_web_fetch,
    "delegate_subagent": tool_delegate_subagent,
    "call_mcp": tool_call_mcp,
    "memory_manage": tool_memory_manage,
    "wiki_manage": tool_wiki_manage,
    "agile_manage": tool_agile_manage,
    "calendar_manage": tool_calendar_manage,
    "education_manage": tool_education_manage,
    "ops_manage": tool_ops_manage,
    "git_manage": tool_git_manage,
    "dev_preview": tool_dev_preview,
    # Backward-compatible dispatch aliases
    "run_python": tool_run_python,
    "run_shell": tool_run_shell,
    "task_manage": tool_task_manage,
    "workflow_manage": tool_workflow_manage,
    "checkpoint_manage": tool_checkpoint_manage,
    "project_manage": tool_project_manage,
    "system_doctor": tool_system_doctor,
    "telemetry_manage": tool_telemetry_manage,
    "job_manage": tool_job_manage,
    "vault_manage": tool_vault_manage,
    "endpoint_manage": tool_endpoint_manage,
    "mcp_manage": tool_mcp_manage,
    "routine_manage": tool_routine_manage,
}

_GUARD_PREFIXES = (
    "SYNTAX GUARD REJECTED", "SAFETY GUARD REJECTED", "PHASE TOOL GATE REJECTED",
    "RISK GATE REJECTED", "SKILL FORGE REJECTED", "WIKI AUTHOR GUARD REJECTED",
    "PRIVILEGE GUARD REJECTED", "DEPLOYMENT LOCK REJECTED", "exit=1\nSYNTAX GUARD REJECTED",
    "SKILL LINTER REJECTED", "WORKFLOW RECIPE LINTER REJECTED",
)


def _check_r3_risk_gate(name: str, args: Dict[str, Any]) -> Optional[str]:
    eff_name, eff_args = name, args
    if name == "run_command":
        if any(args.get(k) not in (None, "") for k in ("python_code", "code", "script", "source")):
            eff_name, eff_args = "run_python", {"code": args.get("python_code") or args.get("code") or args.get("script") or args.get("source")}
        else:
            eff_name, eff_args = "run_shell", args
    elif name == "call_mcp":
        eff_name = "mcp_manage"
    elif name == "ops_manage":
        dom = str_arg(args, "domain").lower()
        eff_name = {"endpoints": "endpoint_manage", "workflows": "workflow_manage", "checkpoints": "checkpoint_manage"}.get(dom, name)
        eff_args = {**(args.get("params") or {}), **args}

    if classify_action_risk(eff_name, eff_args) != ActionRiskTier.R3_HIGH_IMPACT_MUTATION:
        return None
    if eff_name in ("endpoint_manage", "mcp_manage") and not current_workflow_phase():
        return None
    human_confirmed = bool_arg(args, "confirmed", False) and not is_in_agent_tool_turn()
    if human_confirmed or is_r3_action_approved():
        return None
    store = get_memory_store()
    active = store.get_active_workflow()
    if active:
        try:
            meta = json.loads(active.get("metadata_json") or "{}")
        except Exception:
            meta = {}
        if meta.get("r3_approved") and not meta.get("goal_dag"):
            return None
    target_lbl = str(eff_args.get("command") or eff_args.get("code") or eff_args.get("action") or name)[:120]
    allowed, apr = get_approval_queue(store).check_or_enqueue(
        category="r3_tool", action_name=name, target=target_lbl,
        summary=f"Execute R3 `{name}`: {target_lbl}",
        payload={"tool": name, **{k: v for k, v in args.items() if k != "confirmed"}},
        requester="companion", confirmed=False, always_require_in_autonomous=True,
    )
    if allowed:
        return None
    return (
        f"RISK GATE REJECTED (R3) `{name}` (`{apr['approval_id']}`): high-impact external or system command requires explicit user confirmation. "
        "Align with the user first (or approve the queued R3 request / Blueprint before retrying)."
    )


def _resolve_mcp_tool(name: str) -> Optional[Callable[[Dict[str, Any]], str]]:
    if not name.startswith("mcp__") or name.count("__") < 2:
        return None
    _, srv_id, mcp_tool = name.split("__", 2)

    def _call(a: Dict[str, Any]) -> str:
        from companion.integrations import get_mcp_hub

        r = get_mcp_hub().call_mcp_tool(srv_id, mcp_tool, arguments=a if isinstance(a, dict) else {}, actor="companion")
        if r.get("awaiting_approval"):
            return f"AWAITING_USER_APPROVAL (`{r.get('approval_id', '')}`): {r.get('error') or r.get('summary') or 'Awaiting approval.'}"
        return str(r.get("output") or r.get("summary") or "") if r.get("ok") else f"error: {r.get('error') or 'MCP call failed.'}"

    return _call


def dispatch(name: str, args: Dict[str, Any]) -> str:
    with agent_tool_turn_context():
        fn = TOOL_IMPLS.get(name) or _resolve_mcp_tool(name)
        if fn is None:
            telemetry.log("WARN", "tools", f"Unknown tool requested: {name}")
            return f"error: unknown tool: {name}"
        gate_err = check_phase_tool_permission(name, args=args) or _check_r3_risk_gate(name, args)
        if gate_err:
            telemetry.record_span("tool_call", name, 0.1, status="error", detail=gate_err[:140])
            if telemetry.active_trace_id:
                telemetry.log("WARN", "safety", f"Tool/risk gate blocked `{name}`: {gate_err[:80]}")
            return gate_err
        step_sum = summarize_tool_step(name, args)
        emit_stream_event({"type": "tool_start", "tool": name, "summary": step_sum})
        t0 = time.perf_counter()
        try:
            res = fn(args)
            dur_ms = (time.perf_counter() - t0) * 1000.0
            is_guard = res.startswith(_GUARD_PREFIXES)
            is_err = is_guard or res.startswith(("error:", "exit=1"))
            if is_guard and telemetry.active_trace_id:
                telemetry.log("WARN", "safety", f"Safety/Write Guard blocked operation in `{name}`")
            telemetry.record_span("tool_call", name, dur_ms, status="error" if is_err else "ok", detail=res[:140].replace("\n", " "))
            emit_stream_event({"type": "tool_end", "tool": name, "summary": step_sum, "ok": not is_err, "duration_ms": round(dur_ms, 1)})
            return res
        except InterruptedError:
            emit_stream_event({"type": "tool_end", "tool": name, "summary": step_sum, "ok": False, "duration_ms": round((time.perf_counter() - t0) * 1000.0, 1)})
            raise
        except Exception as e:
            dur_ms = (time.perf_counter() - t0) * 1000.0
            err_msg = f"error in {name}: {type(e).__name__}: {e}"
            telemetry.record_span("tool_call", name, dur_ms, status="error", detail=err_msg[:140])
            telemetry.log("ERROR", "tools", err_msg)
            emit_stream_event({"type": "tool_end", "tool": name, "summary": step_sum, "ok": False, "duration_ms": round(dur_ms, 1)})
            return err_msg
