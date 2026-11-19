#!/usr/bin/env python3
"""
Companion AI OS — Self-Evolving, Spec-Driven, State-Machine Local AI Companion & Engineering Agent.

Entry Point, CLI/REPL & Non-Blocking Idle Autonomy Watcher (A-Frame Presentation Apex).
Delegates to the modular `companion/` A-Frame package while preserving full
backward-compatible module exports and boot-time self-recovery.

Usage:
  Interactive REPL : python harness.py
  LAN Web UI       : python harness.py --web [--host 0.0.0.0] [--port 8000]
  Desktop Tray App : pythonw harness.py --tray [--port 8000]
  Autonomous Goal  : python harness.py --goal "your multi-step feature"
  One-shot prompt  : python harness.py "your prompt"
  Health Doctor    : python harness.py --doctor
  Live Status HUD  : python harness.py --status
  Autonomy Controls: python harness.py "/autonomy on" | "/autonomy status" | "/morning"
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

# ---------------------------------------------------------------------------
# 0. Windows UTF-8 Stream Protection & Boot-Time Self-Recovery Wrapper
# ---------------------------------------------------------------------------
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
for _s in ("stdout", "stderr"):
    _stream = getattr(sys, _s, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

if os.name == "nt" and (sys.stdout is None or Path(sys.executable).name.lower().startswith("pythonw")):
    _no_win_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if _no_win_flag and not getattr(subprocess.Popen, "_companion_no_window", False):
        _orig_popen_init = subprocess.Popen.__init__

        def _windowless_popen_init(self, *args, **kwargs):
            kwargs["creationflags"] = int(kwargs.get("creationflags") or 0) | _no_win_flag
            return _orig_popen_init(self, *args, **kwargs)

        subprocess.Popen.__init__ = _windowless_popen_init  # type: ignore[method-assign]
        setattr(subprocess.Popen, "_companion_no_window", True)


def _emergency_boot_rollback(root_dir: Path, err: Exception) -> bool:
    """If an emergency boot recovery flag is set and imports fail, restore touched files from a fresh CoW checkpoint."""
    if "--doctor" in sys.argv or os.environ.get("COMPANION_TEST_MODE") == "1" or os.environ.get("COMPANION_BOOT_ROLLBACK", "").strip().lower() not in ("1", "true", "on", "yes"):
        return False
    raw_data = os.environ.get("COMPANION_DATA_DIR", "").strip()
    data_base = (Path(raw_data) if Path(raw_data).is_absolute() else (root_dir / raw_data)).resolve() if raw_data else (root_dir / "data")
    candidates = [
        data_base / "checkpoints" / "companion-self" / "checkpoints",
        data_base / ".companion_backups" / "checkpoints",
        root_dir / ".companion_backups" / "checkpoints",
    ]
    cp_root = next((c for c in candidates if c.is_dir()), None)
    if cp_root is None:
        return False
    now_ts = time.time()
    cps = sorted(
        [d for d in cp_root.iterdir() if d.is_dir() and (d / "manifest.json").is_file() and (now_ts - d.stat().st_mtime) <= 600.0],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    if not cps:
        return False
    latest = cps[0]
    try:
        manifest = json.loads((latest / "manifest.json").read_text(encoding="utf-8"))
        ws_dir, blobs_dir, root_res = (latest / "workspace").resolve(), (latest.parent.parent / "blobs").resolve(), root_dir.resolve()
        for entry in manifest.get("files", []):
            rel = Path(entry["path"])
            if rel.is_absolute() or ".." in rel.parts:
                continue
            dst = (root_res / rel).resolve()
            if not dst.is_relative_to(root_res):
                continue
            sha = str(entry.get("sha256") or "")
            blob = (blobs_dir / sha[:2] / sha).resolve() if len(sha) >= 4 else None
            src = blob if (blob and blob.is_file()) else (ws_dir / rel).resolve()
            if src.is_file() and not src.is_symlink():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        print(
            f"[SELF-RECOVERY] Boot error ({type(err).__name__}: {err}) healed by restoring checkpoint {latest.name}.",
            file=sys.stderr,
        )
        return True
    except Exception:
        return False


_BOOT_ROOT = Path(__file__).resolve().parent
if str(_BOOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOT_ROOT))


def _extract_boot_env_flags(raw_argv: List[str]) -> List[str]:
    """Apply early `--data-dir <path>` and `--locked` flags before `companion.config` initializes."""
    cleaned: List[str] = []
    idx = 0
    while idx < len(raw_argv):
        tok = raw_argv[idx].strip()
        if tok == "--data-dir" and idx + 1 < len(raw_argv):
            os.environ["COMPANION_DATA_DIR"] = raw_argv[idx + 1].strip()
            idx += 2
        elif tok.startswith("--data-dir="):
            os.environ["COMPANION_DATA_DIR"] = tok.split("=", 1)[1].strip()
            idx += 1
        elif tok in ("--locked", "--immutable"):
            os.environ["COMPANION_IMMUTABLE_APP"] = "1"
            idx += 1
        else:
            cleaned.append(raw_argv[idx])
            idx += 1
    return cleaned


_BOOT_ARGV = _extract_boot_env_flags(sys.argv[1:]) if __name__ == "__main__" else []

for _boot_attempt in range(2):
    try:
        from companion import __version__
        from companion.agent import handle_slash_command, run_agent_turn
        from companion.config import settings
        from companion.context import build_system_prompt, is_identity_bootstrapped, run_first_boot_onboarding
        from companion.llm import chat, model_gate
        from companion.memory_store import get_memory_store
        from companion.routines import IdleAutonomyWatcher, reconcile_startup_missed_runs
        from companion.safety import checkpoint_manager, run_system_doctor
        from companion.tools import (
            TOOL_IMPLS, TOOLS, dispatch, resolve,
            tool_agile_manage, tool_calendar_manage, tool_checkpoint_manage, tool_dev_preview, tool_edit_file,
            tool_education_manage, tool_endpoint_manage, tool_git_manage,
            tool_job_manage, tool_list_dir, tool_mcp_manage, tool_memory_manage, tool_project_manage,
            tool_read_file, tool_routine_manage, tool_run_python, tool_run_shell, tool_sdd_manage,
            tool_search, tool_system_doctor, tool_task_manage, tool_telemetry_manage,
            tool_vault_manage, tool_web_fetch, tool_web_search, tool_wiki_manage, tool_workflow_manage, tool_write_file,
        )
        from companion.workflow_graph import workflow_engine

        break
    except Exception as _boot_exc:
        if _boot_attempt == 0 and _emergency_boot_rollback(_BOOT_ROOT, _boot_exc):
            continue
        raise

# ---------------------------------------------------------------------------
# 1. Backward-Compatible Module-Level Exports
# ---------------------------------------------------------------------------
BASE_URL = settings.base_url
MODEL = settings.model
API_KEY = settings.api_key
ROOT = settings.root
MAX_TOOL_ITERS = settings.max_iters
TEMPERATURE = settings.temperature
SHELL_TIMEOUT = settings.shell_timeout
HTTP_TIMEOUT = settings.http_timeout
OUT_LIMIT = settings.out_limit
SEARCH_LIMIT = settings.search_limit

_CACHED_SYSTEM_PROMPT: Optional[str] = None


def __getattr__(name: str) -> str:
    global _CACHED_SYSTEM_PROMPT
    if name == "SYSTEM_PROMPT":
        if _CACHED_SYSTEM_PROMPT is None:
            _CACHED_SYSTEM_PROMPT = build_system_prompt()
        return _CACHED_SYSTEM_PROMPT
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def run(messages):
    """Execute the executive agent loop with full safety, memory logging, ModelGate foreground priority, and HUD."""
    with model_gate.foreground_turn():
        return run_agent_turn(messages, max_iters=MAX_TOOL_ITERS, chat_fn=chat)


def _coalesce_pasted_lines(
    first_line: str,
    has_more_fn: Callable[[], bool],
    read_line_fn: Callable[[], str],
    max_extra_lines: int = 200,
) -> str:
    """Drain immediately buffered stdin lines so multi-line console pastes form one prompt."""
    lines: List[str] = [first_line.rstrip("\r\n")]
    for _ in range(max_extra_lines):
        if not has_more_fn():
            break
        lines.append(read_line_fn().rstrip("\r\n"))
    return "\n".join(lines).strip()


def _read_interactive_input_with_idle_autonomy() -> Optional[str]:
    """Read user input (coalescing multi-line pastes) while running preemptible background idle routines & goal slices."""
    if not sys.stdin.isatty() or os.name != "nt":
        return input("\nyou> ").strip()

    import msvcrt

    store = get_memory_store()
    if not store.is_background_autonomy_enabled():
        first = input("\nyou> ")
        time.sleep(0.01)
        return _coalesce_pasted_lines(first, msvcrt.kbhit, sys.stdin.readline)

    while msvcrt.kbhit():
        msvcrt.getwch()

    sys.stdout.write("\nyou> ")
    sys.stdout.flush()
    last_activity = time.monotonic()
    ran_idle_cycle = False
    active_worker: Optional[threading.Thread] = None
    abort_event = threading.Event()
    watcher = IdleAutonomyWatcher(is_busy_fn=lambda: msvcrt.kbhit(), store=store)

    while not msvcrt.kbhit():
        wait_mins = settings.hobby_cooldown_minutes if ran_idle_cycle else settings.idle_minutes
        if (active_worker is None or not active_worker.is_alive()) and (time.monotonic() - last_activity) >= (wait_mins * 60):
            abort_event.clear()

            def _bg_tick(idle_sec: float = float(wait_mins * 60)) -> None:
                res = watcher.tick_once(idle_seconds=idle_sec, abort_check=lambda: abort_event.is_set() or msvcrt.kbhit())
                if res.get("action") in ("idle_routine", "idle_goal_slice", "scheduled_routine"):
                    print(f"\n[IDLE AUTONOMY] Completed `{res.get('action')}`. Type `/morning` for full report.")
                    sys.stdout.write("\nyou> ")
                    sys.stdout.flush()

            active_worker = threading.Thread(target=_bg_tick, daemon=True)
            active_worker.start()
            ran_idle_cycle = True
            last_activity = time.monotonic()
        time.sleep(0.05)

    abort_event.set()
    first = sys.stdin.readline()
    time.sleep(0.01)
    return _coalesce_pasted_lines(first, msvcrt.kbhit, sys.stdin.readline)


def _parse_host_port_args(rest: List[str]) -> tuple[str, Optional[int], bool]:
    """Parse optional `--host <h>`, `--port <p>`, `--no-browser`, or positional `[port]` flags."""
    host = settings.web_host
    port_arg: Optional[int] = None
    auto_open = True
    idx = 0
    while idx < len(rest):
        tok = rest[idx].strip()
        if tok == "--host" and idx + 1 < len(rest):
            host = rest[idx + 1].strip() or host
            idx += 2
        elif tok == "--port" and idx + 1 < len(rest) and rest[idx + 1].strip().isdigit():
            port_arg = int(rest[idx + 1].strip())
            idx += 2
        elif tok == "--no-browser":
            auto_open = False
            idx += 1
        elif tok.isdigit() and port_arg is None:
            port_arg = int(tok)
            idx += 1
        else:
            idx += 1
    return host, port_arg, auto_open


def _handle_cli_args(argv: List[str], store) -> None:
    """Execute CLI flags (`--web`, `--tray`, `--doctor`, `--goal`, etc.) or a one-shot prompt."""
    first = argv[0].strip()
    if first in ("--web", "--ui", "/web"):
        from surfaces.web_server import start_web_server

        host_arg, port_arg, _ = _parse_host_port_args(argv[1:])
        start_web_server(host=host_arg, port=port_arg)
        return
    if first in ("--tray", "/tray"):
        from surfaces.windows_tray import start_tray_app

        host_arg, port_arg, auto_open = _parse_host_port_args(argv[1:])
        start_tray_app(host=host_arg, port=port_arg, auto_open_browser=auto_open)
        return

    flag_map = {
        "--doctor": "/doctor", "-d": "/doctor",
        "--status": "/status", "-s": "/status",
        "--project": "/project", "-p": "/project",
        "--workflow": "/workflow", "-w": "/workflow",
        "--plan": "/plan", "--blueprint": "/plan", "--approve": "/approve", "--eval": "/eval",
        "--goal": "/goal", "-g": "/goal",
        "--backlog": "/backlog", "-b": "/backlog",
        "--specs": "/specs", "--skills": "/skills", "--subagent": "/subagent", "--hygiene": "/hygiene",
        "--autonomy": "/autonomy", "--morning": "/morning",
        "--memory": "/memory", "--checkpoint": "/checkpoint",
        "--rollback": "/rollback", "--reset": "/reset",
        "--identity": "/identity", "--tone": "/tone", "--model": "/model", "--models": "/model", "--providers": "/providers", "--onboard": "/onboard",
        "--checkin": "/checkin", "--telemetry": "/telemetry",
        "--traces": "/traces", "--logs": "/logs",
        "--compact": "/compact",
        "--jobs": "/jobs", "--wiki": "/wiki", "--vault": "/vault", "--endpoints": "/endpoints",
        "--mcp": "/mcp", "--partners": "/partners", "--routines": "/routines", "--approvals": "/approvals",
        "--trust": "/trust", "--prompts": "/prompts", "--backup": "/backup", "--archive": "/archive",
        "--board": "/board", "--boards": "/boards", "--sprint": "/sprint", "--calendar": "/calendar", "--study": "/study",
        "--help": "/help", "-h": "/help",
    }
    if first in flag_map:
        messages = [{"role": "system", "content": build_system_prompt(store=store)}]
        extra = " ".join(argv[1:]).strip()
        cmd_to_run = f"{flag_map[first]} {extra}".strip()
        out = handle_slash_command(cmd_to_run, messages, store=store)
        print(out)
        if first in ("--doctor", "-d") and "ISSUES DETECTED (FAIL)" in str(out):
            sys.exit(1)
        if first == "--eval" and ("ISSUES DETECTED" in str(out) or "OFFLINE" in str(out)):
            sys.exit(1)
        return

    prompt_text = " ".join(argv).strip()
    messages = [{"role": "system", "content": build_system_prompt(store=store)}]
    slash_out = handle_slash_command(prompt_text, messages, store=store)
    if slash_out is not None:
        print(slash_out)
        return

    messages.append({"role": "user", "content": prompt_text})
    try:
        print(run(messages))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# 2. Entry point (CLI Flags, One-Shot Mode & Interactive REPL)
# ---------------------------------------------------------------------------
def main() -> None:
    argv = _extract_boot_env_flags(sys.argv[1:])
    if argv and argv[0].strip() in ("--doctor", "-d", "/doctor"):
        rep = run_system_doctor(run_tests=True)
        print(rep["summary"])
        if not rep["healthy"]:
            sys.exit(1)
        return
    store = get_memory_store()
    store.sync_memory_md()
    reconcile_startup_missed_runs(store=store)

    if argv:
        _handle_cli_args(argv, store)
        return

    autonomy_badge = "ON" if store.is_background_autonomy_enabled() else "OFF"
    print(f"Companion AI OS v{__version__[:3]}  |  model={settings.model}  |  project={settings.get_workspace().project_id}  |  autonomy={autonomy_badge}  |  app=IMMUTABLE")
    print("Commands: /status, /autonomy, /jobs, /wiki, /vault, /endpoints, /mcp, /routines, /trust, /goal, /doctor, /help, exit")
    if not is_identity_bootstrapped(store=store) and sys.stdin.isatty():
        run_first_boot_onboarding(store=store)
    unseen = store.get_autonomy_journal(unseen_only=True, mark_seen=False)
    if unseen:
        print(f"[MORNING BRIEFING] {len(unseen)} background routine/goal session(s) completed while you were away! Type `/morning` to review.")

    messages = [{"role": "system", "content": build_system_prompt(store=store)}]

    while True:
        try:
            user = _read_interactive_input_with_idle_autonomy()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user.lower() in ("exit", "quit", "q"):
            break

        slash_resp = handle_slash_command(user, messages)
        if slash_resp is not None:
            print("\n" + slash_resp)
            continue

        pre_turn_messages = list(messages)
        messages.append({"role": "user", "content": user})
        try:
            print("\n" + run(messages))
        except RuntimeError as e:
            print(f"\nerror: {e}\n(context rolled back to pre-turn state; SQLite memory intact)", file=sys.stderr)
            messages[:] = pre_turn_messages
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] = build_system_prompt(store=store)
            else:
                messages.insert(0, {"role": "system", "content": build_system_prompt(store=store)})


if __name__ == "__main__":
    main()
