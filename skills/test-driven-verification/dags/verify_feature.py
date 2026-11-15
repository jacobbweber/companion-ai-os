# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Verify Feature Code-Controlled DAG (`skills/test-driven-verification/dags/verify_feature.py`).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion.dag_runtime import DAGNode, DAGSpec, dry_run_dag  # noqa: E402

DAG = DAGSpec(
    name="verify-feature",
    description="Run SDD hygiene checks, full 7-stage System Doctor unit tests, and ephemeral HTTP smoke probes.",
    nodes=[
        DAGNode(
            node_id="sdd_hygiene",
            kind="tool_or_script_step",
            tool_name="sdd_manage",
            args_builder={"action": "verify"},
            output_schema={"ok": "bool"},
        ),
        DAGNode(
            node_id="doctor_tests",
            kind="tool_or_script_step",
            depends_on=["sdd_hygiene"],
            tool_name="system_doctor",
            args_builder={"run_tests": True},
            output_schema={"ok": "bool"},
        ),
        DAGNode(
            node_id="http_smoke",
            kind="tool_or_script_step",
            depends_on=["doctor_tests"],
            tool_name="workflow_manage",
            args_builder={"action": "http_smoke", "module": "surfaces.web_server"},
            output_schema={"ok": "bool"},
        ),
    ],
    self_test_fn=lambda: bool(dry_run_dag(DAG).get("ok")),
)


if __name__ == "__main__":
    res = dry_run_dag(DAG)
    print(json.dumps(res))
    raise SystemExit(0 if res.get("ok") else 1)
