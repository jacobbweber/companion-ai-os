# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
OODA Diagnose Code-Controlled DAG (`skills/ooda-troubleshooting/dags/ooda_diagnose.py`).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion.dag_runtime import DAGNode, DAGSpec, dry_run_dag  # noqa: E402

DAG = DAGSpec(
    name="ooda-diagnose",
    description="Execute Boyd's OODA Loop: observe System Doctor diagnostics and WARN/ERROR telemetry logs deterministically, then orient & decide root cause on the reasoning tier.",
    input_schema={"issue?": "str", "goal?": "str"},
    nodes=[
        DAGNode(
            node_id="observe_doctor",
            kind="tool_or_script_step",
            tool_name="system_doctor",
            args_builder={"run_tests": True},
            output_schema={"ok": "bool"},
        ),
        DAGNode(
            node_id="observe_logs",
            kind="tool_or_script_step",
            depends_on=["observe_doctor"],
            tool_name="telemetry_manage",
            args_builder={"action": "logs", "level": "WARN", "limit": 20},
            output_schema={"ok": "bool"},
        ),
        DAGNode(
            node_id="orient_and_decide",
            kind="bounded_llm_step",
            depends_on=["observe_doctor", "observe_logs"],
            tier="reasoning",
            prompt_template=(
                "Issue: {{params.issue}} {{params.goal}}\n"
                "Observed Doctor:\n{{observe_doctor.output}}\n"
                "Observed Telemetry:\n{{observe_logs.output}}\n"
                "Return JSON with `hypotheses`, `root_cause`, `minimal_fix`, and `verification_check`."
            ),
            output_schema={},
        ),
    ],
    self_test_fn=lambda: bool(dry_run_dag(DAG).get("ok")),
)


if __name__ == "__main__":
    res = dry_run_dag(DAG)
    print(json.dumps(res))
    raise SystemExit(0 if res.get("ok") else 1)
