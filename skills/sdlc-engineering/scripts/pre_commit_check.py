# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Deterministic Pre-Commit SDLC Helper (`skills/sdlc-engineering/scripts/pre_commit_check.py`).

Supports CLI `--action stage-safety|run-tests` and `--self-test` for `pre_commit_verify.workflow.json`.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion.config import settings  # noqa: E402
from companion.sdlc_tools import check_git_stage_safety  # noqa: E402


def _read_stdin_payload() -> Dict[str, Any]:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _resolve_repo(params: Dict[str, Any]) -> Path:
    raw = str(params.get("repo_root") or params.get("repo_path") or "").strip()
    return Path(raw).resolve() if raw else settings.get_workspace().project_root.resolve()


def run_stage_safety(params: Dict[str, Any]) -> Dict[str, Any]:
    repo = _resolve_repo(params)
    files = params.get("files") if isinstance(params.get("files"), list) else ["."]
    err = check_git_stage_safety(repo, [str(f) for f in files])
    if err:
        return {"ok": False, "error": err}
    branch = ""
    if (repo / ".git").exists():
        proc = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo), capture_output=True, text=True, timeout=10)
        branch = (proc.stdout or "").strip()
        if bool(params.get("require_feature_branch", True)) and branch in ("main", "master"):
            return {"ok": False, "error": f"Direct commit on `{branch}` is blocked; switch to a `feat/*` or `fix/*` branch."}
    return {"ok": True, "repo_root": repo.as_posix(), "branch": branch}


def run_repo_tests(params: Dict[str, Any]) -> Dict[str, Any]:
    repo = _resolve_repo(params)
    cmd = str(params.get("test_command") or "").strip()
    if not cmd:
        if (repo / "tests").is_dir():
            cmd = f'"{sys.executable}" -m unittest discover -s tests -p "test_*.py"'
        elif (repo / "package.json").is_file():
            cmd = "npm test"
        else:
            return {"ok": True, "skipped": True, "detail": "No default test suite detected."}
    env = {**os.environ, "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1"}
    proc = subprocess.run(cmd, cwd=str(repo), shell=True, capture_output=True, text=True, timeout=180, env=env)
    tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-1200:]
    if proc.returncode != 0:
        return {"ok": False, "skipped": False, "error": f"Repository tests failed (exit={proc.returncode}):\n{tail}"}
    return {"ok": True, "skipped": False, "command": cmd, "output_tail": tail}


def _self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        (p / "app.py").write_text("x = 1\n", encoding="utf-8")
        s1 = run_stage_safety({"repo_root": str(p), "files": ["app.py"], "require_feature_branch": False})
        s2 = run_repo_tests({"repo_root": str(p)})
        assert s1["ok"] and s2["ok"] and s2["skipped"] is True, (s1, s2)
    print(json.dumps({"ok": True, "self_test": "PASS"}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic pre-commit SDLC check helper.")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--action", choices=["stage-safety", "run-tests"], default="stage-safety")
    args = parser.parse_args()
    if args.self_test:
        return _self_test()
    stdin_data = _read_stdin_payload()
    params = dict(stdin_data.get("input") or stdin_data.get("params") or {})
    res = run_stage_safety(params) if args.action == "stage-safety" else run_repo_tests(params)
    print(json.dumps(res))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
