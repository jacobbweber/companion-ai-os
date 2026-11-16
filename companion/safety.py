"""
Self-Preservation, Workspace Path Locks, Inline Micro-Linter & 7-Stage System Doctor
(A-Frame Domain Layer).

Guarantees that Companion can self-iterate and build external projects without losing itself,
violating `ADR-007` project boundaries, introducing syntax/hygiene/layer regressions,
or getting trapped in repetitive tool loops.
"""

import ast
import contextlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from companion.checkpoints import CheckpointManager, checkpoint_manager, get_active_checkpoint_manager
from companion.config import settings
from companion.disciplines import (
    FORBIDDEN_EXTERNAL_IMPORTS,
    STUB_PATTERNS,
    analyze_python_source,
    check_module_layer_imports,
    iter_ast_import_targets,
    validate_embedded_scripts,
    verify_repository_syntax,
)
from companion.hygiene import (
    ALLOWED_ROOT_FILES,
    ALLOWED_TOP_DIRS,
    analyze_workspace_hygiene,
    find_semicolon_chains,
    find_unused_imports_in_source,
)

__all__ = [
    "CheckpointManager",
    "LoopGuard",
    "check_command_safety",
    "check_path_safety",
    "checkpoint_manager",
    "get_active_checkpoint_manager",
    "lint_single_file_edit",
    "normalize_windows_shell_command",
    "run_system_doctor",
    "run_unit_tests",
    "try_indent_shifted_replace",
    "validate_python_syntax",
]

_DESTRUCTIVE_CMD_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:^|[;&|]\s*)(?:sudo\s+)?rm\s+(?:-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*|--recursive)\s+['\"]?(?:/|\*|~|\.\.(?:/|\\|\s|$))", re.I), "destructive recursive rm on root/home/parent"),
    (re.compile(r"\bgit\s+push\b[^\n;&|]*(?:--force(?!-with-lease)|-f\b)", re.I), "force-pushing git history (`git push --force`)"),
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.I), "destructive `git reset --hard` (use checkpoint_manage instead)"),
    (re.compile(r"\bgit\s+clean\s+-[a-zA-Z]*f", re.I), "destructive `git clean -f` untracked file wipe"),
    (re.compile(r"\b(?:mkfs(?:\.[a-z0-9]+)?\b|format\s+[a-zA-Z]:|Format-Volume\b)", re.I), "disk/volume formatting command"),
    (re.compile(r"\bdd\s+[^\n;&|]*\bof=/dev/", re.I), "raw block device write (`dd of=/dev/...`)"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.I), "shell fork bomb"),
    (re.compile(r"\b(?:shutdown|reboot|poweroff|halt|Stop-Computer|Restart-Computer)\b", re.I), "system power/reboot command"),
    (re.compile(r"\b(?:del|erase|rd|rmdir)\s+[^\n;&|]*/s[^\n;&|]*/q\s+['\"]?(?:[a-zA-Z]:\\|\*|\\)", re.I), "destructive Windows drive/wildcard wipe"),
    (re.compile(r"\b(?:Remove-Item|ri|rm|del|rd|rmdir)\b[^\n;&|]*(?:-Recurse|-r\b)[^\n;&|]*(?:[a-zA-Z]:\\|/(?:\s|$|[a-zA-Z])|~|\.\.(?:/|\\|\s|$)|\*(?:\s|$))", re.I), "destructive recursive PowerShell item wipe on root/home/parent/wildcard"),
    (re.compile(r"\b(?:DROP\s+TABLE|TRUNCATE\s+TABLE)\b", re.I), "destructive SQL table drop/truncate"),
    (re.compile(r"(?:UPDATE\s+user_profile|INSERT\s+[^\n;]*user_profile|REPLACE\s+INTO\s+user_profile|set_self_dev_mode)\b[^\n;]*self_dev_mode|self_dev_mode[^\n;]*(?:UPDATE\s+user_profile|INSERT\s+INTO\s+user_profile)", re.I), "privilege-escalation mutation of human-only `self_dev_mode`"),
    (re.compile(r"\b(?:pkill|killall)\b[^\n;&|]*\b(?:systemd|init|launchd|sshd)\b", re.I), "signaling OS/session init or SSH supervisor daemon"),
    (re.compile(r"\bsystemctl\b[^\n;&|]*\b(?:stop|kill|disable|mask)\b[^\n;&|]*\bcompanion(?:\.service)?\b", re.I), "stopping or disabling Companion's own service unit"),
)

_TRAILING_OR_TRUE_RE = re.compile(r"\s*\|\|\s*(?:true|:\s*|exit\s+0)\s*$", re.I)
_PY_SELF_KILL_RE = re.compile(r"\bos\.kill(?:pg)?\s*\(\s*(?:(-?\d+)|(os\.getp?pid\s*\(\s*\)))")


def check_command_safety(command: str) -> Optional[str]:
    """Inspect a shell command or Python snippet and reject destructive or self-terminating patterns."""
    cmd = (command or "").strip()
    if not cmd:
        return None
    for rx, reason in _DESTRUCTIVE_CMD_PATTERNS:
        if rx.search(cmd):
            return f"SAFETY GUARD REJECTED command: blocked {reason}.\nCommand was NOT executed. Use safe, scoped project operations or `checkpoint_manage`."
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
    cmd = (command or "").strip()
    ignore_err = False
    if os.name == "nt" and cmd:
        match = _TRAILING_OR_TRUE_RE.search(cmd)
        if match:
            cmd, ignore_err = cmd[: match.start()].strip(), True
        cmd = re.sub(r"2>\s*/dev/null\b", "2>nul", cmd)
        cmd = re.sub(r"(?<![0-9])>\s*/dev/null\b", ">nul", cmd)
    return cmd, ignore_err


def check_path_safety(path: Path, write: bool = False, allowed_roots: Optional[List[Path]] = None) -> Optional[str]:
    """Enforce mechanical WorkspaceContext path confinement (`ADR-007`) and protect core/sensitive files."""
    try:
        resolved = path.resolve()
    except Exception as exc:
        return f"SAFETY GUARD REJECTED path: invalid path `{path}` ({exc})."

    tmp_root = Path(tempfile.gettempdir()).resolve()
    if allowed_roots is not None:
        roots = [r.resolve() for r in allowed_roots]
    else:
        ws = settings.get_workspace()
        if ws.is_self_project:
            roots = [ws.self_root, *([settings.data_dir.resolve()] if settings.data_dir else []), tmp_root]
        elif write:
            if resolved.is_relative_to(ws.self_root) and not resolved.is_relative_to(ws.project_root):
                return (
                    f"SAFETY GUARD REJECTED path: Companion core (`{ws.self_root}`) is mechanically locked READ-ONLY "
                    f"while working on external project `{ws.project_id}` (`{ws.project_root}`). "
                    "Switch back via `/project self` or `project_manage(action='self')` to self-edit."
                )
            roots = [ws.project_root, tmp_root]
        else:
            roots = [
                ws.project_root,
                settings.effective_data_dir.resolve(),
                (ws.self_root / "skills").resolve(),
                (ws.self_root / "specs").resolve(),
                tmp_root,
            ]

    if not any(resolved.is_relative_to(r) for r in roots):
        return (
            f"SAFETY GUARD REJECTED path: `{resolved}` is outside the active WorkspaceContext root "
            f"(`{roots[0]}`). Cross-directory traversal is blocked."
        )
    parts_lower = [pt.lower() for pt in resolved.parts]
    fname_lower = resolved.name.lower()
    if ".ssh" in parts_lower or fname_lower.startswith((".comp_ssh_", ".secrets_")):
        return f"SAFETY GUARD REJECTED path: access to SSH/Vault temporary credentials (`{resolved}`) is blocked."
    is_env_secret = (
        fname_lower in (".env", "secrets.env")
        or resolved == settings.secrets_env_path.resolve()
        or (fname_lower.startswith(".env.") and fname_lower not in (".env.example", ".env.sample", ".env.template"))
    )
    if is_env_secret:
        if write:
            return f"SAFETY GUARD REJECTED path: direct overwrite of secret file `{resolved.name}` is blocked."
        return f"SAFETY GUARD REJECTED path: reading secret file `{resolved.name}` via tools is blocked (inspect `.env.example` instead)."
    if write and ".git" in parts_lower:
        return f"SAFETY GUARD REJECTED path: direct modification of `.git` internals (`{resolved}`) is blocked."
    if write and allowed_roots is None:
        wiki_roots = [(settings.effective_data_dir / "wiki").resolve(), (settings.root / "data" / "wiki").resolve()]
        if any(resolved.is_relative_to(wr) for wr in wiki_roots):
            return (
                f"SAFETY GUARD REJECTED path: direct `write_file`/`edit_file` inside `{resolved.parent}` is blocked. "
                "Use `wiki_manage` so Knowledge Wiki co-ownership and curation rules are enforced."
            )
        ws_chk = settings.get_workspace()
        if ws_chk.is_self_project and not settings.is_self_dev_enabled() and resolved.is_relative_to(ws_chk.self_root):
            data_roots = [(ws_chk.self_root / "data").resolve(), settings.effective_data_dir.resolve()]
            if not any(resolved.is_relative_to(dr) for dr in data_roots):
                return (
                    f"SAFETY GUARD REJECTED path: Companion core (`{ws_chk.self_root}`) is mechanically locked READ-ONLY "
                    "because `Self-Dev Mode` is OFF (`ADR-019`). Enable `/selfdev on` (or toggle `Self-Dev Mode` in "
                    "Web UI `Settings -> General & Autonomy`), or write inside `data/` or an external `/project` workspace."
                )
    return None


def validate_python_syntax(source: str, filename: str = "<string>") -> Tuple[bool, Optional[str]]:
    """Validate that Python source code parses and compiles without SyntaxError."""
    res = analyze_python_source(source, filename=filename)
    return res.valid_syntax, res.syntax_error


def _check_python_governed_rules(tree: ast.Module, lines: List[str], rel: str) -> Optional[str]:
    """Enforce orphan-path, line-budget, ADR-001 stdlib, and A-Frame layer rules on Companion source edits."""
    parts = rel.split("/")
    if len(parts) == 1 and parts[0] not in ALLOWED_ROOT_FILES:
        return f"HYGIENE GUARD REJECTED `{rel}`: root `.py` file not allowed (place inside {sorted(ALLOWED_TOP_DIRS)})."
    if len(parts) > 1 and parts[0] not in (*ALLOWED_TOP_DIRS, "data"):
        return f"HYGIENE GUARD REJECTED `{rel}`: top-level folder `{parts[0]}` not in {sorted(ALLOWED_TOP_DIRS)}."
    if len(lines) > settings.max_module_lines:
        return (
            f"HYGIENE GUARD REJECTED `{rel}`: module is {len(lines)} lines (> budget {settings.max_module_lines}). "
            "Extract a cohesive helper module instead."
        )
    fn_cap = settings.max_function_lines
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_len = getattr(node, "end_lineno", node.lineno) - node.lineno + 1
            if fn_len > fn_cap:
                return f"HYGIENE GUARD REJECTED `{rel}:{node.lineno}`: `{node.name}()` is {fn_len} lines (> budget {fn_cap}). Extract a helper."
            if node.args.args and node.args.args[0].arg in ("self", "cls"):
                return f"HYGIENE GUARD REJECTED `{rel}:{node.lineno}`: `{node.name}({node.args.args[0].arg})` is mis-indented at module top-level."
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn_len = getattr(sub, "end_lineno", sub.lineno) - sub.lineno + 1
                    if fn_len > fn_cap:
                        return f"HYGIENE GUARD REJECTED `{rel}:{sub.lineno}`: `{node.name}.{sub.name}()` is {fn_len} lines (> budget {fn_cap})."

    if parts[0] not in ("tests", "skills", "data"):
        semi = find_semicolon_chains(tree, rel)
        if semi:
            return f"HYGIENE GUARD REJECTED `{semi[0]}`: write one statement per line (do not chain statements with `;`)."
        ring0_local = {"companion", "surfaces", "configs", "memory", "harness", "tests", "__future__", "msvcrt", "winreg", "winsound", "_winapi"}
        stdlib_lower = {m.lower() for m in getattr(sys, "stdlib_module_names", ())}
        for target, lineno in iter_ast_import_targets(tree):
            top_pkg = target.split(".")[0].lower()
            if top_pkg in FORBIDDEN_EXTERNAL_IMPORTS or (stdlib_lower and top_pkg not in stdlib_lower and top_pkg not in ring0_local):
                return f"HYGIENE GUARD REJECTED `{rel}:{lineno}`: third-party import `{top_pkg}` violates ADR-001 stdlib-only rule in Ring-0 core."
        layer_errs = check_module_layer_imports(tree, rel)
        if layer_errs:
            src_mod, src_layer, tgt_mod, tgt_layer, _line = layer_errs[0]
            return f"ARCHITECTURE GUARD REJECTED `{rel}`: upward layer import `{src_mod}` ({src_layer.value}) -> `{tgt_mod}` ({tgt_layer.value})."
    return None


def lint_single_file_edit(
    path: Path,
    content: str,
    op_name: str = "write_file",
    governed_root: Optional[Path] = None,
) -> Tuple[Optional[str], str]:
    """Run `<5ms` inline post-edit micro-linting; return `(hard_reject_error, inline_warning_note)`."""
    from companion.sdd import validate_skill_or_recipe_path

    resolved = path.resolve()
    skill_err = validate_skill_or_recipe_path(resolved, content)
    if skill_err:
        return skill_err, ""

    gov_root = (governed_root or settings.root).resolve()
    is_governed = (
        resolved.is_relative_to(gov_root)
        and (gov_root / "companion").is_dir()
        and ".companion_backups" not in resolved.parts
    )
    rel = str(resolved.relative_to(gov_root)).replace("\\", "/") if resolved.is_relative_to(gov_root) else resolved.name
    warnings: List[str] = []

    if resolved.suffix.lower() == ".py":
        valid, syn_err = validate_python_syntax(content, filename=str(resolved))
        if not valid:
            verb_note = "File was NOT modified. Fix syntax and retry." if op_name == "write_file" else "Edit was NOT applied. Adjust new_str and retry."
            return f"SYNTAX GUARD REJECTED {op_name} on {path}:\n  {syn_err}\n{verb_note}", ""
        tree = ast.parse(content, filename=rel)
        lines = content.splitlines()
        if is_governed:
            gov_err = _check_python_governed_rules(tree, lines, rel)
            if gov_err:
                return f"{gov_err}\nOperation was NOT applied.", ""
        unused = find_unused_imports_in_source(tree, rel)
        if unused:
            warnings.append(f"Unused import(s): {'; '.join(unused)} — remove before finishing so System Doctor Stage 5 passes.")
        if len(lines) >= int(settings.max_module_lines * 0.9):
            warnings.append(f"Module headroom low: {len(lines)}/{settings.max_module_lines} lines.")
        long_lines = [idx for idx, ln in enumerate(lines, 1) if len(ln) > 160 and not ln.lstrip().startswith(("#", '"', "'"))]
        if long_lines:
            warnings.append(f"{len(long_lines)} code line(s) exceed 160 chars (e.g. L{long_lines[0]}) — wrap long expressions cleanly.")

    if resolved.suffix.lower() in {".js", ".html", ".htm"}:
        js_err = validate_embedded_scripts(content, rel)
        if js_err:
            return f"SYNTAX GUARD REJECTED {op_name} on {path}:\n  {js_err}\nOperation was NOT applied.", ""

    if resolved.suffix.lower() in {".py", ".html", ".js"} and not rel.startswith(("tests/", "skills/", "data/")) and rel not in ("companion/safety.py", "companion/disciplines.py"):
        for rx, label in STUB_PATTERNS:
            if rx.search(content):
                warnings.append(f"Detected {label} in `{rel}` — replace stubs with complete working logic before verification.")

    warn_str = ("\n[INLINE LINT WARNING: " + " | ".join(warnings) + "]") if warnings else ""
    return None, warn_str


def try_indent_shifted_replace(text: str, old: str, new: str) -> Optional[str]:
    """If old matches a unique block in text shifted by a uniform leading-space offset, apply shifted new."""
    t_lines, o_lines, n_lines = text.split("\n"), old.split("\n"), new.split("\n")
    if not o_lines or len(o_lines) > len(t_lines):
        return None
    matches: List[Tuple[int, int]] = []
    for i in range(len(t_lines) - len(o_lines) + 1):
        if any(t_lines[i + k].strip() != o_lines[k].strip() for k in range(len(o_lines))):
            continue
        rest_deltas = {
            (len(o_lines[k]) - len(o_lines[k].lstrip(" "))) - (len(t_lines[i + k]) - len(t_lines[i + k].lstrip(" ")))
            for k in range(1, len(o_lines))
            if t_lines[i + k].strip()
        }
        d0 = (len(o_lines[0]) - len(o_lines[0].lstrip(" "))) - (len(t_lines[i]) - len(t_lines[i].lstrip(" ")))
        all_deltas = rest_deltas | ({d0} if t_lines[i].strip() else set())
        if len(all_deltas) == 1:
            matches.append((i, next(iter(all_deltas))))
        elif len(rest_deltas) == 1 and o_lines[0] == o_lines[0].lstrip(" "):
            matches.append((i, next(iter(rest_deltas))))
    if len(matches) != 1:
        return None
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


_KILL_FAMILY_RE = re.compile(r"\b(?:kill|pkill|killall|taskkill|Stop-Process)\b", re.I)


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
        if name == "run_shell" and _KILL_FAMILY_RE.search(str(args.get("command") or "")):
            self.family_counts["process_kill"] += 1
            if self.family_counts["process_kill"] > 2:
                self.consecutive_errors = max(self.consecutive_errors, self.max_consecutive_errors)
                return (
                    "LOOP GUARD INTERVENTION: Repeated process-kill attempts detected in this turn — a supervisor (`systemctl --user`, `systemctl`, Docker, or parent daemon) is likely respawning the process. "
                    "Escalating to `reasoning`: inspect `systemctl --user list-units`, `systemctl status <pid>`, or `docker ps` instead of killing respawned PIDs."
                )
        sig = self._signature(name, args)
        self.call_counts[sig] += 1
        if sig == self.last_signature:
            self.consecutive_identical += 1
        else:
            self.last_signature = sig
            self.consecutive_identical = 1

        if self.consecutive_identical > self.max_identical_calls or self.call_counts[sig] > self.max_identical_calls + 1:
            return (
                f"LOOP GUARD INTERVENTION: You have called `{name}` with identical arguments "
                f"{self.call_counts[sig]} times in this turn. Repeating the exact same call will not "
                "produce a different outcome.\nRecovery Guidance:\n"
                f"1. Consult the Deep Reasoner via `workflow_manage(action='subagent', tier='reasoning', skill='ooda-troubleshooting', goal='Diagnose why {name} is repeating')`.\n"
                "2. If you are trying to run Python code on Windows, use `run_python` instead of `run_shell`.\n"
                "3. If you already received the information you needed, stop calling tools and answer the user."
            )
        return None

    def record_result(self, name: str, result: str) -> Optional[str]:
        is_err = result.startswith((
            "error:",
            "error in ",
            "SYNTAX GUARD REJECTED",
            "SAFETY GUARD REJECTED",
            "HYGIENE GUARD REJECTED",
            "ARCHITECTURE GUARD REJECTED",
            "PHASE TOOL GATE REJECTED",
            "RISK GATE REJECTED",
            "SKILL FORGE REJECTED",
            "WIKI AUTHOR GUARD REJECTED",
            "PRIVILEGE GUARD REJECTED",
        )) or bool(re.match(r"^exit=(?!0\b)-?\d+\n", result))
        self.consecutive_errors = (self.consecutive_errors + 1) if is_err else 0
        if self.consecutive_errors >= self.max_consecutive_errors:
            return (
                f"\n[SELF-RECOVERY HINT: {self.consecutive_errors} consecutive tool errors detected. "
                "Pause and apply the OODA Loop (`skills/ooda-troubleshooting/SKILL.md`): "
                "call `workflow_manage(action='subagent', tier='reasoning', skill='ooda-troubleshooting', goal='Orient & decide root-cause fix')` "
                "or run `workflow_manage(action='run_recipe', recipe='ooda-diagnose')` before attempting another edit. "
                "On Windows, use `run_python` for Python scripts and `system_doctor` to check project health.]"
            )
        return None


def run_unit_tests(root: Optional[Path] = None, timeout: int = 90, pattern: str = "test_*.py") -> Dict[str, Any]:
    """Run the workspace's unittest suite (or a specific `pattern`) in a clean UTF-8 subprocess."""
    target_root = (root or settings.active_project_root).resolve()
    start_dir = "tests" if (target_root / "tests").is_dir() else ("." if list(target_root.glob("test_*.py")) else "")
    if not start_dir:
        return {"passed": True, "ran": 0, "output": "No tests/ directory found."}

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONPATH": str(target_root), "COMPANION_IN_DOCTOR_TESTS": "1"}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", start_dir, "-p", pattern, "-v"],
            cwd=str(target_root),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        raw_out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        m_ran = re.search(r"Ran\s+(\d+)\s+test", raw_out)
        ran_cnt = int(m_ran.group(1)) if m_ran else 0
        out = raw_out[-3000:].split("\n", 1)[-1] if len(raw_out) > 3000 else raw_out
        return {"passed": proc.returncode == 0, "ran": ran_cnt, "returncode": proc.returncode, "output": out}
    except Exception as exc:
        return {"passed": False, "ran": 0, "returncode": -1, "output": f"Failed to run unit tests: {type(exc).__name__}: {exc}"}


def _inspect_doctor_db(target_root: Path) -> Tuple[str, int, Dict[str, str]]:
    """Inspect SQLite ADRs & profile read-only when DB exists so `run_system_doctor` never mutates live `data/`."""
    db_file = settings.db_path if target_root == settings.root.resolve() else (target_root / "data" / "conversations.db")
    if db_file.is_file():
        try:
            with contextlib.closing(sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)) as ro_conn:
                adr_cnt = int(ro_conn.execute("SELECT COUNT(*) FROM architecture_decisions").fetchone()[0])
                prof = dict(ro_conn.execute("SELECT key, value FROM user_profile").fetchall())
                if adr_cnt > 0 and prof:
                    return db_file.name, adr_cnt, prof
        except Exception:
            pass
    from companion.memory_store import MemoryStore

    mem = MemoryStore(db_path=db_file)
    try:
        return mem.db_path.name, len(mem.get_adrs()), mem.get_user_profile()
    finally:
        mem.conn.close()


def run_system_doctor(root: Optional[Path] = None, run_tests: bool = True) -> Dict[str, Any]:
    """Run comprehensive 7-stage self-diagnostics across Companion AI OS."""
    target_root = (root or settings.root).resolve()
    checks: List[Dict[str, Any]] = []

    cfg_issues = settings.validate()
    checks.append({"check": "1. Configuration & .env", "passed": len(cfg_issues) == 0, "details": "Settings valid" if not cfg_issues else "; ".join(cfg_issues)})

    syntax_ok, syntax_results = verify_repository_syntax(target_root)
    bad_files = [f"{r.path}: {r.syntax_error}" for r in syntax_results if not r.valid_syntax]
    checks.append({
        "check": "2. Python AST & Syntax Integrity",
        "passed": syntax_ok,
        "details": f"All {len(syntax_results)} Python files compiled cleanly." if syntax_ok else f"Syntax errors: {'; '.join(bad_files)}",
    })

    from configs.architecture import verify_architecture

    arch_res = verify_architecture(target_root)
    checks.append({
        "check": "3. A-Frame Architecture Boundaries",
        "passed": bool(arch_res.get("valid")),
        "details": f"All {arch_res.get('component_count')} core architectural components verified."
        if arch_res.get("valid")
        else f"Issues: missing={arch_res.get('missing_components')}, violations={arch_res.get('layer_violations')}",
    })

    from companion.sdd import verify_sdd

    sdd_rep = verify_sdd(target_root)
    checks.append({
        "check": "4. Living Spec-Driven Development (SDD) Sync",
        "passed": sdd_rep.valid,
        "details": f"4/4 canonical specs synced, {len(sdd_rep.contract_ids)} subsystem contracts, {sdd_rep.active_adr_count} active ADRs."
        if sdd_rep.valid
        else sdd_rep.format_summary(),
    })

    hygiene_rep = analyze_workspace_hygiene(target_root)
    checks.append({
        "check": "5. Codebase Hygiene, Dead-Code & Simplicity",
        "passed": hygiene_rep.clean,
        "details": f"Zero dead code, zero unused imports, all {hygiene_rep.scanned_files} files within simplicity budgets."
        if hygiene_rep.clean
        else hygiene_rep.format_summary(),
    })

    db_name, adr_cnt, profile = _inspect_doctor_db(target_root)
    checks.append({
        "check": "6. SQLite Memory, ADRs & Autonomy Store",
        "passed": adr_cnt > 0 and len(profile) > 0,
        "details": f"DB healthy ({db_name}): {adr_cnt} ADRs, {len(profile)} profile keys, background_autonomy={profile.get('background_autonomy', profile.get('hobby_mode', 'off'))}, self_dev_mode={profile.get('self_dev_mode', 'off')}.",
    })

    if run_tests:
        if os.environ.get("COMPANION_IN_DOCTOR_TESTS") == "1":
            checks.append({"check": "7. Automated Unit Test Suite", "passed": True, "details": "Nested test suite invocation skipped inside active unittest run."})
        else:
            test_res = run_unit_tests(target_root)
            test_out = str(test_res["output"])
            ran_lines = [ln.strip() for ln in test_out.splitlines() if ln.strip().startswith("Ran ")]
            details_str = (f"{ran_lines[-1]} — OK" if ran_lines else "All unit tests passed.") if test_res["passed"] else test_out
            checks.append({"check": "7. Automated Unit Test Suite", "passed": bool(test_res["passed"]), "details": details_str})

    all_passed = all(c["passed"] for c in checks)
    lines = [f"=== Companion System Doctor: {'HEALTHY (PASS)' if all_passed else 'ISSUES DETECTED (FAIL)'} ==="]
    lines.extend(f"[{'PASS' if c['passed'] else 'FAIL'}] {c['check']}: {c['details']}" for c in checks)
    return {"healthy": all_passed, "checks": checks, "summary": "\n".join(lines)}
