"""
Orthogonal Local SDLC, Secret-Shielded Git/Worktrees, Blind-Vault Git Auth & Managed Preview Servers
(`companion/sdlc_tools.py` — A-Frame Application Layer).

Provides:
1. `discover_repo_sdlc_context`: Repo rulebook (`.agents/rules/`, `AGENTS.md`, `CLAUDE.md`) & stack auto-discovery.
2. `tool_git_manage`: Air-gap-safe local branches, parallel `git worktree` checkouts, secret/artifact staging shield,
   protected-branch & `--force` blocks, `delivery_policy` enforcement, and Blind-Vault `GIT_SSH_COMMAND`/`GIT_ASKPASS`.
3. `PreviewProcessManager` (`tool_dev_preview`): Free-port allocation, secret-scrubbed isolated temp env/DB,
   OS process-group lifecycle, live ring-buffer logs, loopback HTTP probes, vision critique, and guaranteed teardown.
"""

import atexit, base64, collections, contextlib, json, os, re, shutil, signal, socket, subprocess, sys  # noqa: E401
import tempfile, threading, time, urllib.error, urllib.request, uuid  # noqa: E401
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, Iterator, List, Optional, Tuple

from companion.archive_and_approvals import get_approval_queue
from companion.attachments import ingest_attachment, inspect_image_bytes
from companion.config import is_in_agent_tool_turn, settings
from companion.llm import chat, normalize_assistant_message, tier_context
from companion.memory_store import get_memory_store
from companion.project_graph import get_project_graph
from companion.safety import check_command_safety, check_path_safety
from companion.streaming import get_active_cancel_scope, wire_attachment_context
from companion.tool_schemas import bool_arg, int_arg, str_arg
from companion.vault import _lock_windows_owner_dacl, get_vault_manager

__all__ = [
    "PreviewInstance", "PreviewProcessManager", "check_git_stage_safety",
    "discover_repo_sdlc_context", "preview_manager", "preview_turn_cleanup_scope",
    "tool_dev_preview", "tool_git_manage",
]

_PROTECTED_BRANCHES = frozenset({"main", "master", "production", "release"})
_BLOCKED_STAGE_NAMES = frozenset({".env", "secrets.env", "id_rsa", "id_ed25519", "id_ecdsa"})
_BLOCKED_STAGE_EXTS = frozenset({
    ".pem", ".key", ".p12", ".pfx", ".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3",
})
_BLOCKED_STAGE_DIRS = frozenset({"node_modules", ".companion_backups", ".ssh", ".ssh_tmp"})
_STRIP_PREVIEW_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASS", "_PAT", "_CREDENTIAL")
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


def discover_repo_sdlc_context(root: Path, project: Optional[Dict[str, Any]] = None) -> str:
    """Discover repo-local engineering rules (`.agents/rules/`, `AGENTS.md`, `CLAUDE.md`) and stack commands."""
    resolved = root.resolve()
    if not resolved.is_dir():
        return ""
    proj = project or _active_project_record()
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

    parts = [f"delivery_policy=`{proj.get('delivery_policy', 'local_only')}`"]
    if proj.get("git_credential_ref"):
        parts.append(f"git_auth=`vault://{proj['git_credential_ref']}`")
    if proj.get("test_command"):
        parts.append(f"test_cmd=`{proj['test_command']}`")
    if stacks:
        parts.append("stack=" + "; ".join(stacks))
    if rule_notes:
        parts.append("repo_rules=" + ", ".join(rule_notes))
    return "sdlc_repo_context: " + " | ".join(parts)


def _is_allowed_sdlc_repo_dir(target: Path, mutating: bool) -> Optional[str]:
    resolved = target.resolve()
    self_root = settings.root.resolve()
    if mutating and resolved == self_root:
        return (
            f"SAFETY GUARD REJECTED: Companion's installed `app/` directory (`{self_root}`) is permanently IMMUTABLE. "
            "Run mutating Git/SDLC operations inside an external Project clone or worktree (`workspace_path`)."
        )
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
    raw_br = str_arg(args, "branch") or f"feat/{_slugify_branch_part(str_arg(args, 'slug') or 'slice')}"
    if raw_br.lower() in _PROTECTED_BRANCHES:
        return f"SAFETY GUARD REJECTED: create worktrees on a feature branch (`feat/...`), not `{raw_br}`."
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw_br.lower()).strip("-")[:48]
    wt_raw = str_arg(args, "worktree_path")
    wt_dir = Path(wt_raw).resolve() if wt_raw else (repo_root.parent / ".worktrees" / repo_root.name / slug).resolve()
    if wt_dir.is_relative_to(settings.root.resolve()):
        return "SAFETY GUARD REJECTED: worktree_path must live outside Companion's installed `app/` directory."
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
        return (
            f"SAFETY GUARD REJECTED: direct commits onto protected branch `{cur_br}` are blocked. "
            "Create/switch to a feature branch first via `git_manage(action='branch', branch='feat/<slug>')`."
        )
    msg = " ".join((str_arg(args, "message") or "").splitlines()).strip()
    if not msg:
        return "error: commit `message` is required"
    raw_paths = args.get("paths") if args.get("paths") is not None else args.get("files")
    cand_rels: List[str] = [str(p).strip() for p in raw_paths if str(p).strip()] if isinstance(raw_paths, list) and raw_paths else []
    if not cand_rels:
        rc_st, st_out = _run_git_cmd(repo_root, ["status", "--porcelain=v1"])
        if rc_st != 0:
            return f"error: {st_out}"
        for ln in st_out.splitlines():
            m_porc = re.match(r"^[MADRCU?! ]{1,2}\s+(.+)$", ln)
            if m_porc:
                entry = m_porc.group(1).split(" -> ")[-1].strip().strip('"')
                if entry:
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
    raw_repo = str_arg(args, "repo_path")
    repo_root = Path(raw_repo).resolve() if raw_repo else settings.active_project_root.resolve()
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
        if str_arg(args, "path"):
            d_args.extend(["--", str_arg(args, "path")])
        rc, out = _run_git_cmd(repo_root, d_args)
        return (out[: settings.out_limit] or "(no diff)") if rc == 0 else f"error: {out}"
    if action == "log":
        lim = max(1, min(int_arg(args, "limit", 10), 50))
        rc, out = _run_git_cmd(repo_root, ["log", f"-n{lim}", "--oneline", "--decorate"])
        return (out or "(no commits)") if rc == 0 else f"error: {out}"
    if action == "branch":
        br = str_arg(args, "branch")
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
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            with contextlib.suppress(Exception):
                pipe.close()


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
            cmd_spec, use_shell, eff_health = command.replace("{port}", str(alloc_port)), True, (health_path or "/")
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
        att = ingest_attachment(p.name, raw, store=get_memory_store())
        aid = (att.get("attachment") or {}).get("id", "") if att.get("ok") else ""
        ingested.append(f"`{p.name}` ({w}x{h}" + (f", attachment=`{aid}`" if aid else "") + ")")
        uris.append(f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}")
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
        cwd_str = str_arg(args, "cwd")
        sid = int(getattr(_ACTIVE_TURN_PREVIEW_SCOPE, "sid", 0) or 0)
        res = preview_manager.start(
            cwd=Path(cwd_str).resolve() if cwd_str else None, command=str_arg(args, "command"),
            port=int_arg(args, "port", 0), health_path=str_arg(args, "health_path") or str_arg(args, "ready_path"),
            startup_timeout=float(int_arg(args, "timeout", 20)), turn_scope_id=sid,
        )
        return json.dumps(res) if res.get("ok") else f"error: {res.get('error')}"
    if action in ("status", "list"):
        items = preview_manager.list_instances()
        return json.dumps({"ok": True, "previews": items})
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
