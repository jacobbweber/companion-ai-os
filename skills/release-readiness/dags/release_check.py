# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Release Readiness Code-Controlled DAG (`skills/release-readiness/dags/release_check.py`).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion.dag_runtime import DAGNode, DAGSpec, dry_run_dag  # noqa: E402

DAG = DAGSpec(
    name="release-check",
    description="Zero-token deterministic release readiness gate verifying .gitignore privacy shield, 4-file SDD sync, and 7-stage System Doctor.",
    nodes=[
        DAGNode(
            node_id="check_gitignore",
            kind="tool_or_script_step",
            tool_name="read_file",
            args_builder={"path": ".gitignore"},
            output_schema={"ok": "bool"},
        ),
        DAGNode(
            node_id="sdd_verify",
            kind="tool_or_script_step",
            depends_on=["check_gitignore"],
            tool_name="sdd_manage",
            args_builder={"action": "verify"},
            output_schema={"ok": "bool"},
        ),
        DAGNode(
            node_id="doctor_gate",
            kind="tool_or_script_step",
            depends_on=["sdd_verify"],
            tool_name="system_doctor",
            args_builder={"run_tests": True},
            output_schema={"ok": "bool"},
        ),
    ],
    self_test_fn=lambda: bool(dry_run_dag(DAG).get("ok")),
)


if __name__ == "__main__":
    res = dry_run_dag(DAG)
    print(json.dumps(res))
    raise SystemExit(0 if res.get("ok") else 1)
