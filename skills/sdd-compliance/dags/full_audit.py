# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Full SDD & Hygiene Audit Code-Controlled DAG (`skills/sdd-compliance/dags/full_audit.py`).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion.dag_runtime import DAGNode, DAGSpec, dry_run_dag  # noqa: E402

DAG = DAGSpec(
    name="full-audit",
    description="Deterministic zero-LLM audit verifying 4-file canonical SDD sync, AST dead-code hygiene, and 7-stage System Doctor.",
    input_schema={"run_tests?": "bool"},
    nodes=[
        DAGNode(
            node_id="sdd_verify",
            kind="tool_or_script_step",
            tool_name="sdd_manage",
            args_builder={"action": "verify"},
            output_schema={"ok": "bool"},
        ),
        DAGNode(
            node_id="doctor_full",
            kind="tool_or_script_step",
            depends_on=["sdd_verify"],
            tool_name="system_doctor",
            args_builder=lambda ctx: {"run_tests": bool(ctx["params"].get("run_tests", True))},
            output_schema={"ok": "bool"},
        ),
    ],
    self_test_fn=lambda: bool(dry_run_dag(DAG).get("ok")),
)


if __name__ == "__main__":
    res = dry_run_dag(DAG)
    print(json.dumps(res))
    raise SystemExit(0 if res.get("ok") else 1)
