# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Deliver Feature Code-Controlled DAG (`skills/engineering-workflow/dags/deliver_feature.py`).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion.dag_runtime import DAGNode, DAGSpec, dry_run_dag  # noqa: E402

DAG = DAGSpec(
    name="deliver-feature",
    description="Create a workspace safety checkpoint, inspect SDD state, synthesize a clean design & plan, and verify codebase health.",
    input_schema={"goal?": "str"},
    nodes=[
        DAGNode(
            node_id="checkpoint",
            kind="tool_or_script_step",
            tool_name="checkpoint_manage",
            args_builder={"action": "create", "label": "pre_deliver_feature_dag"},
            output_schema={"ok": "bool"},
        ),
        DAGNode(
            node_id="sdd_baseline",
            kind="tool_or_script_step",
            depends_on=["checkpoint"],
            tool_name="sdd_manage",
            args_builder={"action": "verify"},
            output_schema={"ok": "bool"},
        ),
        DAGNode(
            node_id="architect_plan",
            kind="bounded_llm_step",
            depends_on=["sdd_baseline"],
            tier="reasoning",
            prompt_template="Goal: {{params.goal}}\nBaseline SDD:\n{{sdd_baseline.output}}\nReturn JSON with `modules_to_touch`, `invariants`, and `test_plan`.",
            output_schema={},
        ),
        DAGNode(
            node_id="verify_gate",
            kind="tool_or_script_step",
            depends_on=["architect_plan"],
            tool_name="system_doctor",
            args_builder={"run_tests": False},
            output_schema={"ok": "bool"},
        ),
    ],
    self_test_fn=lambda: bool(dry_run_dag(DAG).get("ok")),
)


if __name__ == "__main__":
    res = dry_run_dag(DAG)
    print(json.dumps(res))
    raise SystemExit(0 if res.get("ok") else 1)
