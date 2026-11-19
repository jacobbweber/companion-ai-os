# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Publish Pull Request Code-Controlled DAG (`skills/github-workflow/dags/publish_pr.py`).

Enforces project `delivery_policy != "local_only"`, pushes the feature branch via `git_manage`,
opens or updates the GitHub Pull Request via Blind Vault, and gates PR merge behind `human_gate`.
"""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion.config import settings  # noqa: E402
from companion.dag_runtime import DAGNode, DAGSpec, dry_run_dag  # noqa: E402
from companion.memory_store import get_memory_store  # noqa: E402
from companion.project_graph import get_project_graph  # noqa: E402


def _load_gh_helper() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "github_pr_ci.py"
    spec = importlib.util.spec_from_file_location("github_pr_ci", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _step_verify_policy(ctx: Dict[str, Any]) -> Dict[str, Any]:
    params = ctx.get("params", {})
    head = str(params.get("head") or "").strip()
    if not head or head in ("main", "master"):
        raise RuntimeError(f"Feature branch required (`head={head!r}`); direct `main`/`master` PR publish is blocked.")
    pg = get_project_graph(store=get_memory_store())
    pid = str(params.get("project_id") or settings.workspace.project_id or "").strip()
    proj = pg.get_project(pid) if pid else None
    policy = str((proj or {}).get("delivery_policy") or params.get("delivery_policy") or "local_only")
    if policy == "local_only":
        raise RuntimeError("Project `delivery_policy` is `local_only`; remote branch push & PR creation are blocked.")
    cred_ref = str(params.get("credential_ref") or (proj or {}).get("git_credential_ref") or "github-pat")
    return {"ok": True, "delivery_policy": policy, "credential_ref": cred_ref, "head": head}


def _step_open_pr_and_checks(ctx: Dict[str, Any]) -> Dict[str, Any]:
    params = ctx.get("params", {})
    pol = ctx["nodes"]["verify_delivery_policy"]
    gh = _load_gh_helper()
    repo = str(params.get("repo") or "").strip()
    base = str(params.get("base") or "main").strip()
    title = str(params.get("title") or f"feat: {pol['head']}").strip()
    body = str(params.get("body") or "Automated verified PR from Companion SDLC.").strip()
    api_base = str(params.get("api_base") or "https://api.github.com").strip()
    pr_res = gh.upsert_pull_request(repo, pol["head"], base, title, body, pol["credential_ref"], api_base=api_base)
    if not pr_res.get("ok"):
        raise RuntimeError(f"GitHub PR upsert failed: {pr_res.get('error')}")
    chk_res = gh.inspect_check_runs(repo, pol["head"], pol["credential_ref"], api_base=api_base)
    return {"ok": True, "pr_number": int(pr_res.get("number") or 0), "html_url": str(pr_res.get("html_url") or ""), "checks": chk_res}


def _step_merge_pr(ctx: Dict[str, Any]) -> Dict[str, Any]:
    params = ctx.get("params", {})
    pol = ctx["nodes"]["verify_delivery_policy"]
    pr_info = ctx["nodes"]["open_or_update_pr"]
    gh = _load_gh_helper()
    api_base = str(params.get("api_base") or "https://api.github.com").strip()
    res = gh.merge_pull_request(
        str(params.get("repo") or ""), int(pr_info["pr_number"]), pol["credential_ref"],
        merge_method=str(params.get("merge_method") or "squash"), approved=True, api_base=api_base,
    )
    if not res.get("ok"):
        raise RuntimeError(f"GitHub PR merge failed: {res.get('error')}")
    return res


DAG = DAGSpec(
    name="publish-pr",
    description="Push verified feature branch, open/update GitHub PR via Blind Vault, inspect Actions checks, and gate PR merge behind human approval.",
    input_schema={"repo": "str", "head": "str", "base?": "str", "title?": "str", "body?": "str", "credential_ref?": "str", "auto_merge?": "bool"},
    nodes=[
        DAGNode(
            node_id="verify_delivery_policy",
            kind="python_step",
            fn=_step_verify_policy,
            output_schema={"ok": "bool", "delivery_policy": "str", "credential_ref": "str", "head": "str"},
        ),
        DAGNode(
            node_id="push_feature_branch",
            kind="tool_or_script_step",
            depends_on=["verify_delivery_policy"],
            tool_name="git_manage",
            args_builder=lambda ctx: {"action": "push", "remote": str(ctx["params"].get("remote") or "origin"), "branch": str(ctx["params"]["head"])},
            output_schema={"ok": "bool"},
        ),
        DAGNode(
            node_id="open_or_update_pr",
            kind="python_step",
            depends_on=["push_feature_branch"],
            fn=_step_open_pr_and_checks,
            output_schema={"ok": "bool", "pr_number": "int", "html_url": "str"},
        ),
        DAGNode(
            node_id="merge_approval_gate",
            kind="human_gate",
            depends_on=["open_or_update_pr"],
            condition="params.auto_merge",
            risk_tier="R3",
            description="Approve squash-merge of verified Pull Request into base branch.",
        ),
        DAGNode(
            node_id="merge_pr",
            kind="python_step",
            depends_on=["merge_approval_gate"],
            condition="params.auto_merge",
            fn=_step_merge_pr,
            output_schema={"ok": "bool"},
        ),
    ],
    self_test_fn=lambda: bool(dry_run_dag(DAG).get("ok")),
)


if __name__ == "__main__":
    res = dry_run_dag(DAG)
    print(json.dumps(res))
    raise SystemExit(0 if res.get("ok") else 1)
