"""Comprehensive, Self-Guarded Tool Registry (A-Frame Application Layer, 25 tools)."""

import json
import os
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from companion.archive_and_approvals import get_approval_queue
from companion.config import SKIP_DIRS, agent_tool_turn_context, is_in_agent_tool_turn, settings
from companion.disciplines import ActionRiskTier, classify_action_risk
from companion.grounding import get_execution_target
from companion.harvest_tools import (
    tool_agile_manage, tool_calendar_manage, tool_education_manage, tool_endpoint_manage,
    tool_job_manage, tool_mcp_manage, tool_routine_manage, tool_vault_manage, tool_wiki_manage,
)
from companion.hygiene import extract_python_symbol
from companion.llm_wire import check_url_ssrf, urlopen_safe
from companion.memory_store import get_memory_store
from companion.os_tools import (
    tool_checkpoint_manage, tool_memory_manage, tool_project_manage,
    tool_sdd_manage, tool_system_doctor, tool_task_manage, tool_telemetry_manage, tool_workflow_manage,
)
from companion.safety import (
    check_command_safety, check_path_safety, lint_single_file_edit,
    normalize_windows_shell_command, try_indent_shifted_replace, validate_python_syntax,
)
from companion.streaming import emit_stream_event
from companion.telemetry import telemetry
from companion.tool_schemas import (
    PHASE_ALLOWED_TOOLS, TOOLS, bool_arg, check_phase_tool_permission, current_workflow_phase,
    filter_tools_for_phase, int_arg, is_r3_action_approved, phase_tool_context, r3_approval_context, str_arg,
)

__all__ = [
    "PHASE_ALLOWED_TOOLS", "TOOLS", "TOOL_IMPLS", "check_phase_tool_permission", "current_workflow_phase", "dispatch",
    "filter_tools_for_phase", "phase_tool_context", "r3_approval_context", "resolve", "tool_agile_manage", "tool_calendar_manage",
    "tool_checkpoint_manage", "tool_edit_file", "tool_education_manage", "tool_endpoint_manage",
    "tool_job_manage", "tool_list_dir", "tool_mcp_manage", "tool_memory_manage", "tool_project_manage", "tool_read_file",
    "tool_routine_manage", "tool_run_python", "tool_run_shell", "tool_sdd_manage", "tool_search", "tool_system_doctor",
    "tool_task_manage", "tool_telemetry_manage", "tool_vault_manage", "tool_web_fetch", "tool_wiki_manage", "tool_workflow_manage", "tool_write_file",
]


def resolve(p: str, for_write: bool = False) -> Path:
    """Resolve `p` against the active `WorkspaceContext` (allowing read fallback to Companion `skills/` & `specs/`)."""
    cand = Path(p)
    if cand.is_absolute():
        return cand
    ws = settings.get_workspace()
    if not for_write and not ws.is_self_project:
        if (ws.project_root / p).exists():
            return ws.project_root / p
        if p.replace("\\", "/").startswith(("skills/", "specs/")) and (ws.self_root / p).exists():
            return ws.self_root / p
    return ws.project_root / p


def _closest_line_hint(text: str, old: str) -> str:
    """Find the closest matching line in text to help the caller fix indentation mismatches."""
    first_probe = next((ln.strip() for ln in old.splitlines() if ln.strip()), "")
    if not first_probe:
        return ""
    file_lines = text.splitlines()
    for idx, fline in enumerate(file_lines):
        if first_probe in fline:
            lo, hi = max(0, idx - 1), min(len(file_lines), idx + len(old.splitlines()) + 2)
            return f"\nExact file content around lines {lo + 1}-{hi} (copy exact indentation):\n```text\n" + "\n".join(file_lines[lo:hi]) + "\n```"
    return ""


def tool_read_file(args: Dict[str, Any]) -> str:
    path_str = str_arg(args, "path")
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
        return f"# {p}  (symbol `{symbol}` [{kind}], lines {s_ln}-{e_ln} of {len(lines)})\n" + "\n".join(
            f"{i:5d}|{lines[i - 1]}" for i in range(s_ln, min(e_ln, len(lines)) + 1)
        )
    start = max(1, int_arg(args, "start_line", 1) or 1)
    end = min(len(lines), int_arg(args, "end_line", 0) or len(lines))
    return (
        "error: start_line must be <= end_line"
        if start > end
        else f"# {p}  (lines {start}-{end} of {len(lines)})\n" + "\n".join(f"{i:5d}|{lines[i - 1]}" for i in range(start, end + 1))
    )


def tool_write_file(args: Dict[str, Any]) -> str:
    path_str = str_arg(args, "path")
    if not path_str or args.get("content") is None:
        return "error: path is required" if not path_str else "error: content is required"
    p, norm_content = resolve(path_str, for_write=True), str(args.get("content", "")).replace("\r\n", "\n")
    path_err = check_path_safety(p, write=True)
    if path_err:
        return path_err
    if p.is_dir():
        return f"error: target path is an existing directory, not a file: {p}"
    hard_err, lint_warn = lint_single_file_edit(p, norm_content, op_name="write_file", governed_root=settings.root)
    if hard_err:
        return hard_err
    get_execution_target().write_text(p, norm_content, backup=True)
    return f"wrote {len(norm_content)} chars to {p}{lint_warn}"


def tool_edit_file(args: Dict[str, Any]) -> str:
    path_str = str_arg(args, "path")
    if not path_str:
        return "error: path is required"
    p = resolve(path_str, for_write=True)
    path_err = check_path_safety(p, write=True)
    if path_err:
        return path_err
    if not p.is_file():
        return f"error: file not found: {p}"
    old_raw, new_raw = args.get("old_str"), args.get("new_str")
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
    hard_err, lint_warn = lint_single_file_edit(p, updated_text, op_name="edit_file", governed_root=settings.root)
    if hard_err:
        return hard_err
    target.write_text(p, updated_text, backup=True)
    return f"edited {p} (replaced 1 occurrence){lint_warn}"


def tool_list_dir(args: Dict[str, Any]) -> str:
    p = resolve(str_arg(args, "path", ".") or ".", for_write=False)
    path_err = check_path_safety(p, write=False)
    if path_err:
        return path_err
    if not p.is_dir():
        return f"error: directory not found: {p}"
    names = get_execution_target().list_dir(p)
    return f"{p}/  (empty)" if not names else f"{p}/\n" + "\n".join(names)


_BINARY_EXTS = {".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3", ".pyc", ".pyo", ".pyd", ".exe", ".dll", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip"}


def tool_search(args: Dict[str, Any]) -> str:
    pattern = str_arg(args, "pattern")
    if not pattern:
        return "error: pattern is required"
    base = resolve(str_arg(args, "path", ".") or ".", for_write=False)
    path_err = check_path_safety(base, write=False)
    if path_err:
        return path_err
    try:
        rx = re.compile(pattern) if bool_arg(args, "is_regex", False) else None
    except re.error as e:
        return f"error: invalid regex: {e}"
    needle, matches, files = pattern.lower(), [], ([base] if base.is_file() else [])
    if base.is_dir():
        for root, dirs, fnames in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            files.extend(
                Path(root) / fn
                for fn in fnames
                if Path(fn).suffix.lower() not in _BINARY_EXTS and fn.lower() not in (".env", "secrets.env")
            )
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
    """Execute Python code directly in the active `ExecutionTarget` with UTF-8 encoding."""
    code = str(args.get("code") or "")
    cmd_err = check_command_safety(code)
    if cmd_err and "self_dev_mode" not in cmd_err:
        return cmd_err
    return get_execution_target().run_python_code(code, timeout=settings.shell_timeout)


_HEREDOC_RE = re.compile(r"python(?:3)?\s+<<-?\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\1\b", re.DOTALL)
_MULTILINE_PY_C_RE = re.compile(r'^(?:cd\s+[^\n&]+&&\s*)?python(?:3)?\s+-c\s+(["\'])(.*)\1\s*$', re.DOTALL)
_HEAD_TAIL_PIPE_RE = re.compile(r"\s*\|\s*(head|tail)(?:\s+-n\s*|\s+-)?(\d+)?\s*$", re.IGNORECASE)


def tool_run_shell(args: Dict[str, Any]) -> str:
    cmd = str(args.get("command") or "")
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
    return f"exit={rc}\n{out[: settings.out_limit] + chr(10) + '... [truncated]' if len(out) > settings.out_limit else out}"


_BLOCKED_FETCH_PORTS = {21, 22, 23, 25, 135, 139, 445, 3306, 3389, 5432, 6379, 11211, 27017}
_BLOCKED_METADATA_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}
_MAX_WEB_FETCH_BYTES = 524_288


class _HtmlToMarkdownParser(HTMLParser):
    """Lightweight stdlib HTML-to-Markdown converter stripping scripts/styles/SVGs."""

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


def _validate_web_fetch_url(url: str) -> Optional[str]:
    """Block SSRF against cloud metadata, loopback/private LAN addresses, and non-HTTP service ports."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "error: valid http:// or https:// url is required"
    host = parsed.hostname.strip().lower()
    if host in _BLOCKED_METADATA_HOSTS:
        return f"SAFETY GUARD REJECTED: cloud metadata host blocked (`{host}`)"
    return check_url_ssrf(url, allow_private_lan=False, blocked_ports=tuple(_BLOCKED_FETCH_PORTS))


def tool_web_fetch(args: Dict[str, Any]) -> str:
    url = str_arg(args, "url")
    url_err = _validate_web_fetch_url(url)
    if url_err:
        return url_err
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "companion-os/3.4"})
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
    return text[: settings.out_limit] + ("\n... [truncated]" if (len(text) > settings.out_limit or byte_capped) else "")


TOOL_IMPLS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "read_file": tool_read_file, "write_file": tool_write_file, "edit_file": tool_edit_file,
    "list_dir": tool_list_dir, "search": tool_search, "run_python": tool_run_python,
    "run_shell": tool_run_shell, "web_fetch": tool_web_fetch, "memory_manage": tool_memory_manage,
    "task_manage": tool_task_manage, "workflow_manage": tool_workflow_manage, "checkpoint_manage": tool_checkpoint_manage,
    "sdd_manage": tool_sdd_manage, "project_manage": tool_project_manage,
    "system_doctor": tool_system_doctor, "telemetry_manage": tool_telemetry_manage,
    "job_manage": tool_job_manage, "wiki_manage": tool_wiki_manage, "vault_manage": tool_vault_manage,
    "endpoint_manage": tool_endpoint_manage, "mcp_manage": tool_mcp_manage, "routine_manage": tool_routine_manage,
    "agile_manage": tool_agile_manage, "calendar_manage": tool_calendar_manage, "education_manage": tool_education_manage,
}

_GUARD_PREFIXES = (
    "SYNTAX GUARD REJECTED", "SAFETY GUARD REJECTED", "HYGIENE GUARD REJECTED", "ARCHITECTURE GUARD REJECTED",
    "PHASE TOOL GATE REJECTED", "RISK GATE REJECTED", "SKILL FORGE REJECTED", "WIKI AUTHOR GUARD REJECTED",
    "PRIVILEGE GUARD REJECTED", "DEPLOYMENT LOCK REJECTED",
    "exit=1\nSYNTAX GUARD REJECTED", "SKILL LINTER REJECTED", "WORKFLOW RECIPE LINTER REJECTED",
)


def _check_r3_risk_gate(name: str, args: Dict[str, Any]) -> Optional[str]:
    if classify_action_risk(name, args) != ActionRiskTier.R3_HIGH_IMPACT_MUTATION:
        return None
    if name in ("endpoint_manage", "mcp_manage") and not current_workflow_phase():
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
    target_lbl = str(args.get("command") or args.get("code") or args.get("action") or name)[:120]
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


def dispatch(name: str, args: Dict[str, Any]) -> str:
    fn = TOOL_IMPLS.get(name)
    if fn is None:
        telemetry.log("WARN", "tools", f"Unknown tool requested: {name}")
        return f"error: unknown tool: {name}"
    gate_err = check_phase_tool_permission(name, args=args) or _check_r3_risk_gate(name, args)
    if gate_err:
        telemetry.record_span("tool_call", name, 0.1, status="error", detail=gate_err[:140])
        if telemetry.active_trace_id:
            telemetry.log("WARN", "safety", f"Tool/risk gate blocked `{name}`: {gate_err[:80]}")
        return gate_err
    emit_stream_event({"type": "tool_start", "tool": name})
    t0 = time.perf_counter()
    try:
        with agent_tool_turn_context():
            res = fn(args)
        dur_ms = (time.perf_counter() - t0) * 1000.0
        is_guard = res.startswith(_GUARD_PREFIXES)
        is_err = is_guard or res.startswith(("error:", "exit=1"))
        if is_guard and telemetry.active_trace_id:
            telemetry.log("WARN", "safety", f"Safety/Write Guard blocked operation in `{name}`")
        telemetry.record_span("tool_call", name, dur_ms, status="error" if is_err else "ok", detail=res[:140].replace("\n", " "))
        emit_stream_event({"type": "tool_end", "tool": name, "ok": not is_err, "duration_ms": round(dur_ms, 1)})
        return res
    except Exception as e:
        dur_ms = (time.perf_counter() - t0) * 1000.0
        err_msg = f"error in {name}: {type(e).__name__}: {e}"
        telemetry.record_span("tool_call", name, dur_ms, status="error", detail=err_msg[:140])
        telemetry.log("ERROR", "tools", err_msg)
        emit_stream_event({"type": "tool_end", "tool": name, "ok": False, "duration_ms": round(dur_ms, 1)})
        return err_msg
