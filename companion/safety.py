"""
Workspace Path/Command Safety Guards, Indent-Shifted Edit Recovery, LoopGuard & Runtime Health Doctor.

Guarantees mechanical WorkspaceContext path confinement, blocks destructive shell/SQL/secret-exfil
patterns, detects infinite tool loops, and verifies runtime health without repo self-policing bloat.
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
from companion.config import settings, walk_pruned_files

__all__ = [
    "CheckpointManager", "LoopGuard", "check_command_safety", "check_path_safety",
    "checkpoint_manager", "get_active_checkpoint_manager", "lint_single_file_edit",
    "normalize_windows_shell_command", "run_system_doctor", "run_unit_tests",
    "try_indent_shifted_replace", "validate_python_syntax",
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
    (re.compile(r"(?:UPDATE\s+user_profile|INSERT\s+[^\n;]*user_profile|REPLACE\s+INTO\s+user_profile)\b[^\n;]*(?:trust_mode|immutable_app|immutable_core|self_dev_mode)|(?:trust_mode|immutable_app|immutable_core|self_dev_mode)[^\n;]*(?:UPDATE\s+user_profile|INSERT\s+INTO\s+user_profile)", re.I), "privilege-escalation mutation of protected `user_profile` governance keys"),
    (re.compile(r"\b(?:pkill|killall)\b[^\n;&|]*\b(?:systemd|init|launchd|sshd)\b", re.I), "signaling OS/session init or SSH supervisor daemon"),
    (re.compile(r"\bsystemctl\b[^\n;&|]*\b(?:stop|kill|disable|mask)\b[^\n;&|]*\bcompanion(?:\.service)?\b", re.I), "stopping or disabling Companion's own service unit"),
    (re.compile(r"(?:secrets\.env|\.comp_ssh_|\.secrets_|\.ssh_tmp|(?:^|[\s'\"/\\])\.env(?!\.(?:example|sample|template))\b)", re.I), "reading or mutating Blind Vault / `.env` secret stores from subprocess"),
)

_TRAILING_OR_TRUE_RE = re.compile(r"\s*\|\|\s*(?:true|:\s*|exit\s+0)\s*$", re.I)
_PY_SELF_KILL_RE = re.compile(r"\bos\.kill(?:pg)?\s*\(\s*(?:(-?\d+)|(os\.getp?pid\s*\(\s*\)))")
_SCHEMA_TYPO_RE = re.compile(r"(?:is required\b|unknown [a-z_]+ action|JSON parse error|must be (?:one of|a non-empty)|unexpected keyword)", re.I)


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
    if allowed_roots is not None:
        roots = [r.resolve() for r in allowed_roots]
    else:
        ws = settings.get_workspace()
        if ws.is_self_project:
            roots = [ws.self_root, settings.effective_data_dir.resolve(), *([settings.data_dir.resolve()] if settings.data_dir else []), tmp_root]
        elif write:
            if resolved.is_relative_to(ws.self_root) and not resolved.is_relative_to(ws.project_root) and not resolved.is_relative_to(settings.effective_data_dir.resolve()):
                return (
                    f"SAFETY GUARD REJECTED path: Companion's installed `app/` runtime (`{ws.self_root}`) is permanently IMMUTABLE / READ-ONLY "
                    f"while working on external project `{ws.project_id}` (`{ws.project_root}`)."
                )
            roots = [ws.project_root, settings.domains_dir.resolve(), settings.scratch_dir.resolve(), tmp_root]
        else:
            roots = [ws.project_root, settings.effective_data_dir.resolve(), (ws.self_root / "skills").resolve(), tmp_root]

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
        wiki_roots = [(settings.effective_data_dir / "wiki").resolve(), (settings.root / "data" / "wiki").resolve()]
        if any(resolved.is_relative_to(wr) for wr in wiki_roots):
            return (
                f"SAFETY GUARD REJECTED path: direct `write_file`/`edit_file` inside `{resolved.parent}` is blocked. "
                "Use `wiki_manage` so Knowledge Wiki co-ownership and curation rules are enforced."
            )
        ws_chk = settings.get_workspace()
        if ws_chk.is_self_project and resolved.is_relative_to(ws_chk.self_root):
            data_roots = [settings.effective_data_dir.resolve()]
            if not settings.immutable_app or settings.data_dir is None:
                data_roots.append((ws_chk.self_root / "data").resolve())
            if not any(resolved.is_relative_to(dr) for dr in data_roots):
                return (
                    f"SAFETY GUARD REJECTED path: Companion's installed `app/` runtime (`{ws_chk.self_root}`) is permanently IMMUTABLE. "
                    "Write inside `data/` (`data/domains/`, `data/scratch/`, `data/skills/`) or focus an external Project clone (`workspace_path`)."
                )
    return None


def validate_python_syntax(source: str, filename: str = "<string>") -> Tuple[bool, Optional[str]]:
    """Validate that Python source code parses and compiles without SyntaxError."""
    try:
        tree = ast.parse(source, filename=filename)
        compile(tree, filename=filename, mode="exec")
        return True, None
    except SyntaxError as exc:
        return False, f"SyntaxError at line {exc.lineno}, col {exc.offset}: {exc.msg}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


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
            return (
                f"\n[SELF-RECOVERY HINT: {self.consecutive_errors} consecutive tool errors detected. "
                "Pause and inspect the root cause before retrying.]"
            )
        if self.consecutive_errors == 1 and _SCHEMA_TYPO_RE.search(result):
            return f"\n[Parameter hint for `{name}`: check parameter names/types against the tool schema and retry cleanly.]"
        return None


def run_unit_tests(root: Optional[Path] = None, timeout: int = 90, pattern: str = "test_*.py") -> Dict[str, Any]:
    """Run the workspace's unittest suite (or a specific `pattern`) in a clean UTF-8 subprocess."""
    target_root = (root or settings.active_project_root).resolve()
    start_dir = "tests" if (target_root / "tests").is_dir() else ("." if list(target_root.glob("test_*.py")) else "")
    if not start_dir:
        return {"passed": True, "ran": 0, "output": "No tests/ directory found."}
    env = {
        **os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
        "PYTHONPATH": str(target_root), "COMPANION_IN_DOCTOR_TESTS": "1", "COMPANION_TEST_MODE": "1",
        "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1",
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", start_dir, "-p", pattern, "-v"],
            cwd=str(target_root), env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        raw_out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        m_ran = re.search(r"Ran\s+(\d+)\s+test", raw_out)
        out = raw_out[-3000:].split("\n", 1)[-1] if len(raw_out) > 3000 else raw_out
        return {"passed": proc.returncode == 0, "ran": int(m_ran.group(1)) if m_ran else 0, "returncode": proc.returncode, "output": out}
    except Exception as exc:
        return {"passed": False, "ran": 0, "returncode": -1, "output": f"Failed to run unit tests: {type(exc).__name__}: {exc}"}


def _inspect_doctor_db(target_root: Path) -> Tuple[str, int, Dict[str, str]]:
    """Inspect SQLite profile read-only when DB exists so `run_system_doctor` never mutates live `data/`."""
    db_file = settings.db_path if target_root == settings.root.resolve() else (target_root / "data" / "conversations.db")
    if db_file.is_file():
        with contextlib.suppress(Exception), contextlib.closing(sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True)) as ro_conn:
            prof = dict(ro_conn.execute("SELECT key, value FROM user_profile").fetchall())
            ok_chk = ro_conn.execute("PRAGMA quick_check").fetchone()
            if prof and ok_chk and ok_chk[0] == "ok":
                return db_file.name, len(prof), prof
    from companion.memory_store import MemoryStore

    mem = MemoryStore(db_path=db_file)
    try:
        prof = mem.get_user_profile()
        return mem.db_path.name, len(prof), prof
    finally:
        mem.close()


def run_system_doctor(root: Optional[Path] = None, run_tests: bool = True) -> Dict[str, Any]:
    """Run 7-stage operational Runtime Health Doctor (zero repo self-policing or builder contamination)."""
    target_root = (root or settings.root).resolve()
    checks: List[Dict[str, Any]] = []

    cfg_issues = settings.validate()
    checks.append({"check": "1. Configuration & .env", "passed": len(cfg_issues) == 0, "details": "Settings valid" if not cfg_issues else "; ".join(cfg_issues)})

    py_files = [p for p in walk_pruned_files(target_root) if p.suffix.lower() == ".py" and ".agents" not in p.parts]
    bad_syntax: List[str] = []
    for pf in py_files:
        ok_syn, syn_err = validate_python_syntax(pf.read_text(encoding="utf-8", errors="replace"), filename=str(pf))
        if not ok_syn:
            bad_syntax.append(f"{pf.relative_to(target_root)}: {syn_err}")
    checks.append({
        "check": "2. Runtime Python Syntax Integrity",
        "passed": len(bad_syntax) == 0,
        "details": f"All {len(py_files)} runtime Python files compiled cleanly." if not bad_syntax else f"Syntax errors: {'; '.join(bad_syntax[:5])}",
    })

    static_dir = target_root / "surfaces" / "static"
    required_static = ("index.html", "styles.css", "app.js")
    missing_static = [fn for fn in required_static if not (static_dir / fn).is_file()] if (target_root / "surfaces").is_dir() else []
    checks.append({
        "check": "3. Web Surface Static Bundle",
        "passed": len(missing_static) == 0,
        "details": "Compiled Web UI assets (`index.html`, `styles.css`, `app.js`) verified." if not missing_static else f"Missing static assets: {missing_static}",
    })

    skills_dir = target_root / "skills"
    leaked_builder = [d.name for d in (skills_dir.iterdir() if skills_dir.is_dir() else ()) if d.is_dir() and d.name in {"anti-sprawl-gate", "product-intent-dogfood", "sdd-compliance", "web-surface-dev", "release-readiness", "devops-ops"}]
    checks.append({
        "check": "4. Runtime Skills & Builder Boundary",
        "passed": len(leaked_builder) == 0,
        "details": "Clean runtime boundary (zero `.agents` builder skills leaked into `skills/`)." if not leaked_builder else f"Builder skill leak in `skills/`: {leaked_builder}",
    })

    domains_root = settings.domains_dir
    bad_domain_dbs: List[str] = []
    domain_db_count = 0
    if domains_root.is_dir():
        for db_p in domains_root.rglob("*.db"):
            domain_db_count += 1
            try:
                with contextlib.closing(sqlite3.connect(f"file:{db_p.as_posix()}?mode=ro", uri=True)) as dconn:
                    qc = dconn.execute("PRAGMA quick_check").fetchone()
                    if not qc or qc[0] != "ok":
                        bad_domain_dbs.append(db_p.name)
            except Exception as exc:
                bad_domain_dbs.append(f"{db_p.name} ({exc})")
    checks.append({
        "check": "5. Domain Capability Pack SQLite Isolation",
        "passed": len(bad_domain_dbs) == 0,
        "details": f"{domain_db_count} domain SQLite DB(s) healthy." if not bad_domain_dbs else f"Corrupt domain DB(s): {bad_domain_dbs}",
    })

    db_name, prof_cnt, profile = _inspect_doctor_db(target_root)
    checks.append({
        "check": "6. Core SQLite WAL Store & Blind Vault",
        "passed": prof_cnt > 0,
        "details": f"DB healthy ({db_name}): {prof_cnt} profile keys, background_autonomy={profile.get('background_autonomy', 'off')}.",
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
    lines = [f"=== Companion Runtime Doctor: {'HEALTHY (PASS)' if all_passed else 'ISSUES DETECTED (FAIL)'} ==="]
    lines.extend(f"[{'PASS' if c['passed'] else 'FAIL'}] {c['check']}: {c['details']}" for c in checks)
    return {"healthy": all_passed, "checks": checks, "stages": checks, "summary": "\n".join(lines)}
