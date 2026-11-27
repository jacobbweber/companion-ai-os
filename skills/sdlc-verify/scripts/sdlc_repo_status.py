# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Deterministic SDLC Artifact Completeness & Placeholder Hygiene Auditor (`skills/sdlc-verify/scripts/sdlc_repo_status.py`).

Supports CLI `--action status|audit`, `--repo-root <path>`, and `--self-test` for `sdlc_audit.workflow.json`.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion.config import settings  # noqa: E402
from companion.worktrees import inspect_sdlc_repo_state  # noqa: E402


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


def _resolve_repo(params: Dict[str, Any], cli_repo: str = "") -> Path:
    raw = (cli_repo or str(params.get("repo_root") or params.get("repo_path") or "")).strip()
    return Path(raw).resolve() if raw else settings.get_workspace().project_root.resolve()


def run_status(params: Dict[str, Any], cli_repo: str = "") -> Dict[str, Any]:
    repo = _resolve_repo(params, cli_repo=cli_repo)
    if not repo.is_dir():
        return {"ok": False, "error": f"Repository path does not exist: `{repo}`"}
    state = inspect_sdlc_repo_state(repo)
    return {
        "ok": True,
        "repo_root": str(state["repo_root"]),
        "repo_kind": str(state["repo_kind"]),
        "backlog_mode": str(state["backlog_mode"]),
        "detected_stage": int(state["detected_stage"]),
        "recommended_skill": str(state["recommended_skill"]),
        "stage_label": str(state["stage_label"]),
        "active_increment": str(state.get("active_increment") or ""),
        "upstream_artifacts": list(state.get("upstream_artifacts") or []),
        "placeholder_violations": list(state.get("placeholder_violations") or []),
        "hygiene_clean": bool(state.get("hygiene_clean", True)),
        "lifecycle_complete": bool(state.get("lifecycle_complete", False)),
        "summary": str(state.get("summary") or ""),
    }


def run_audit(params: Dict[str, Any], cli_repo: str = "") -> Dict[str, Any]:
    status_res = run_status(params, cli_repo=cli_repo)
    if not status_res.get("ok"):
        return {**status_res, "placeholder_count": 0, "placeholder_violations": []}
    issues = list(status_res.get("placeholder_violations") or [])
    strict = bool(params.get("fail_on_placeholders", True))
    if strict and issues:
        return {
            "ok": False,
            "repo_root": status_res["repo_root"],
            "detected_stage": status_res["detected_stage"],
            "recommended_skill": status_res["recommended_skill"],
            "placeholder_count": len(issues),
            "placeholder_violations": issues,
            "error": f"SDLC placeholder hygiene failed in {len(issues)} line(s): {', '.join(issues)}",
        }
    return {
        "ok": True,
        "repo_root": status_res["repo_root"],
        "detected_stage": status_res["detected_stage"],
        "recommended_skill": status_res["recommended_skill"],
        "placeholder_count": len(issues),
        "placeholder_violations": issues,
    }


def _self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        s0 = run_status({"repo_root": str(repo)})
        assert s0["ok"] and s0["detected_stage"] == 1 and s0["recommended_skill"] == "sdlc-product", s0

        (repo / "product.md").write_text("# Product Spec\n\nTODO: fill persona table\n", encoding="utf-8")
        a_dirty = run_audit({"repo_root": str(repo), "fail_on_placeholders": True})
        assert a_dirty["ok"] is False and a_dirty["placeholder_count"] == 1, a_dirty

        (repo / "product.md").write_text("# Product Spec\n\nClean finalized product specification.\n", encoding="utf-8")
        a_clean = run_audit({"repo_root": str(repo), "fail_on_placeholders": True})
        assert a_clean["ok"] is True and a_clean["detected_stage"] == 2 and a_clean["recommended_skill"] == "sdlc-architecture", a_clean

    print(json.dumps({"ok": True, "self_test": "PASS"}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic SDLC repository stage & placeholder hygiene auditor.")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--action", choices=["status", "audit"], default="status")
    parser.add_argument("--repo-root", default="")
    args = parser.parse_args()
    if args.self_test:
        return _self_test()
    stdin_data = _read_stdin_payload()
    params = dict(stdin_data.get("input") or stdin_data.get("params") or {})
    res = run_status(params, cli_repo=args.repo_root) if args.action == "status" else run_audit(params, cli_repo=args.repo_root)
    print(json.dumps(res))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
