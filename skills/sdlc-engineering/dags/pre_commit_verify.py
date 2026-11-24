# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Pre-Commit SDLC Verification DAG (`skills/sdlc-engineering/dags/pre_commit_verify.py`).

Validates feature-branch discipline, secret/DB staging safety (`check_git_stage_safety`),
and project test execution before `git_manage(action="commit")`.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion.config import settings  # noqa: E402
from companion.dag_runtime import DAGNode, DAGSpec, dry_run_dag  # noqa: E402
from companion.sdlc_tools import check_git_stage_safety  # noqa: E402


def _resolve_repo(ctx: Dict[str, Any]) -> Path:
    params = ctx.get("params", {})
    raw = str(params.get("repo_root") or params.get("repo_path") or "").strip()
    return Path(raw).resolve() if raw else settings.get_workspace().project_root.resolve()


def _step_stage_safety(ctx: Dict[str, Any]) -> Dict[str, Any]:
    repo = _resolve_repo(ctx)
    params = ctx.get("params", {})
    files = params.get("files") if isinstance(params.get("files"), list) else ["."]
    err = check_git_stage_safety(repo, [str(f) for f in files])
    if err:
        raise RuntimeError(err)
    branch = ""
    if (repo / ".git").exists():
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, timeout=10,
        )
        branch = (proc.stdout or "").strip()
        if bool(params.get("require_feature_branch", True)) and branch in ("main", "master"):
            raise RuntimeError(f"Direct commit on `{branch}` is blocked; switch to a `feat/*` or `fix/*` branch.")
    return {"ok": True, "repo_root": repo.as_posix(), "branch": branch}


def _step_run_tests(ctx: Dict[str, Any]) -> Dict[str, Any]:
    repo = _resolve_repo(ctx)
    params = ctx.get("params", {})
    cmd = str(params.get("test_command") or "").strip()
    if not cmd:
        if (repo / "tests").is_dir():
            cmd = f'"{sys.executable}" -m unittest discover -s tests -p "test_*.py"'
        elif (repo / "package.json").is_file():
            cmd = "npm test"
        else:
            return {"ok": True, "skipped": True, "detail": "No default test suite detected."}
    env = os.environ.copy()
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    env["no_proxy"] = "127.0.0.1,localhost,::1"
    proc = subprocess.run(cmd, cwd=str(repo), shell=True, capture_output=True, text=True, timeout=180, env=env)
    tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-1200:]
    if proc.returncode != 0:
        raise RuntimeError(f"Repository tests failed (exit={proc.returncode}):\n{tail}")
    return {"ok": True, "skipped": False, "command": cmd, "output_tail": tail}


DAG = DAGSpec(
    name="pre-commit-verify",
    description="Deterministic pre-commit SDLC gate checking feature-branch safety, secret/DB staging blocks, and repository unit tests.",
    input_schema={"repo_root?": "str", "files?": "list", "test_command?": "str", "require_feature_branch?": "bool"},
    nodes=[
        DAGNode(
            node_id="check_stage_safety",
            kind="python_step",
            fn=_step_stage_safety,
            output_schema={"ok": "bool", "repo_root": "str", "branch": "str"},
        ),
        DAGNode(
            node_id="run_repo_tests",
            kind="python_step",
            depends_on=["check_stage_safety"],
            fn=_step_run_tests,
            timeout_sec=200.0,
            output_schema={"ok": "bool", "skipped": "bool"},
        ),
    ],
    self_test_fn=lambda: bool(dry_run_dag(DAG).get("ok")),
)


if __name__ == "__main__":
    res = dry_run_dag(DAG)
    print(json.dumps(res))
    raise SystemExit(0 if res.get("ok") else 1)
