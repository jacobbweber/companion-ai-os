# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Safe Deployment Code-Controlled DAG (`skills/devops-ops/dags/safe_deploy.py`).

Deterministic 4-node release promotion pipeline:
1. `pre_deploy_snapshot`: Captures restorable snapshot of `target_app_dir` (and optional SQLite/Wiki backup).
2. `deploy_approval_gate`: `R3` `human_gate` pausing in `ApprovalQueue` in `balanced`/`strict` mode.
3. `sync_app_runtime`: Syncs clean packaged `app/` runtime (excluding `tests/`, `.git/`, `data/`, secrets).
4. `verify_or_rollback`: Probes `/api/health` + packaged `7/7` `System Doctor`; automatically restores
   `pre_deploy_snapshot` and verifies rollback integrity on any failure.
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


def _load_ops() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "safe_deploy_ops.py"
    spec = importlib.util.spec_from_file_location("safe_deploy_ops", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _step_snapshot(ctx: Dict[str, Any]) -> Dict[str, Any]:
    params = ctx.get("params", {})
    ops = _load_ops()
    target_raw = str(params.get("target_app_dir") or "").strip()
    if not target_raw:
        raise ValueError("`target_app_dir` is required for `safe-deploy`.")
    target = Path(target_raw).resolve()
    bk_raw = str(params.get("backup_dir") or "").strip()
    bk_dir = Path(bk_raw).resolve() if bk_raw else (target.parent / "backups")
    store = get_memory_store() if bool(params.get("snapshot_data_store", True)) else None
    return ops.snapshot_app_dir(target, bk_dir, label=str(params.get("label") or "safe-deploy"), store=store)


def _step_sync(ctx: Dict[str, Any]) -> Dict[str, Any]:
    params = ctx.get("params", {})
    ops = _load_ops()
    src_raw = str(params.get("source_repo") or "").strip()
    src = Path(src_raw).resolve() if src_raw else settings.root.resolve()
    target = Path(str(params["target_app_dir"])).resolve()
    return ops.sync_packaged_app_tree(src, target)


def _step_verify_or_rollback(ctx: Dict[str, Any]) -> Dict[str, Any]:
    params = ctx.get("params", {})
    ops = _load_ops()
    snap = ctx["nodes"]["pre_deploy_snapshot"]
    target = Path(str(params["target_app_dir"])).resolve()
    ver = ops.verify_app_health_and_doctor(
        target,
        health_url=str(params.get("health_url") or ""),
        boot_ephemeral_health=bool(params.get("boot_ephemeral_health", True)),
        simulate_health_failure=bool(params.get("simulate_health_failure", False)),
        run_doctor=bool(params.get("run_doctor", True)),
    )
    if ver.get("ok"):
        return {
            "ok": True,
            "deployed": True,
            "rolled_back": False,
            "rollback_verified": False,
            "doctor_ok": bool(ver.get("doctor_ok", True)),
            "health_status": int((ver.get("health") or {}).get("status") or 200),
            "snapshot_dir": str(snap["snapshot_dir"]),
        }
    rb = ops.restore_app_snapshot(Path(str(snap["snapshot_dir"])), target)
    rb_doc_ok = True
    if snap.get("existed") and bool(params.get("run_doctor", True)):
        rb_ver = ops.verify_app_health_and_doctor(target, boot_ephemeral_health=False, run_doctor=True)
        rb_doc_ok = bool(rb_ver.get("doctor_ok"))
    return {
        "ok": False,
        "deployed": False,
        "rolled_back": bool(rb.get("ok")),
        "rollback_verified": rb_doc_ok,
        "doctor_ok": False,
        "health_status": int((ver.get("health") or {}).get("status") or 0),
        "failure_reason": str(ver.get("reason") or "Post-deploy verification failed"),
        "snapshot_dir": str(snap["snapshot_dir"]),
    }


DAG = DAGSpec(
    name="safe-deploy",
    description="Promote verified repository code into a packaged app/ runtime with pre-deploy snapshot backup, R3 human approval gate, /api/health + 7/7 Doctor verification, and automatic rollback.",
    input_schema={
        "target_app_dir": "str",
        "source_repo?": "str",
        "backup_dir?": "str",
        "health_url?": "str",
        "boot_ephemeral_health?": "bool",
        "simulate_health_failure?": "bool",
        "run_doctor?": "bool",
        "snapshot_data_store?": "bool",
        "label?": "str",
    },
    nodes=[
        DAGNode(
            node_id="pre_deploy_snapshot",
            kind="python_step",
            fn=_step_snapshot,
            output_schema={"ok": "bool", "snapshot_dir": "str", "existed": "bool"},
        ),
        DAGNode(
            node_id="deploy_approval_gate",
            kind="human_gate",
            depends_on=["pre_deploy_snapshot"],
            risk_tier="R3",
            description="Approve syncing verified release artifacts into target_app_dir.",
        ),
        DAGNode(
            node_id="sync_app_runtime",
            kind="python_step",
            depends_on=["deploy_approval_gate"],
            fn=_step_sync,
            output_schema={"ok": "bool", "tests_excluded": "bool"},
        ),
        DAGNode(
            node_id="verify_or_rollback",
            kind="python_step",
            depends_on=["sync_app_runtime"],
            fn=_step_verify_or_rollback,
            timeout_sec=180.0,
            output_schema={"ok": "bool", "deployed": "bool", "rolled_back": "bool", "rollback_verified": "bool"},
        ),
    ],
    self_test_fn=lambda: bool(dry_run_dag(DAG).get("ok")),
)


if __name__ == "__main__":
    res = dry_run_dag(DAG)
    print(json.dumps(res))
    raise SystemExit(0 if res.get("ok") else 1)
