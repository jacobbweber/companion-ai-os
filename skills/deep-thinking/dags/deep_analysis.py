# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Deep Analysis Code-Controlled DAG (`skills/deep-thinking/dags/deep_analysis.py`).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion.dag_runtime import DAGNode, DAGSpec, dry_run_dag  # noqa: E402

DAG = DAGSpec(
    name="deep-analysis",
    description="Gather living architecture specs and ADRs deterministically, then synthesize a structured first-principles tradeoff matrix on the reasoning tier.",
    input_schema={"question?": "str", "goal?": "str"},
    nodes=[
        DAGNode(
            node_id="read_arch",
            kind="tool_or_script_step",
            tool_name="sdd_manage",
            args_builder={"action": "read_spec", "spec_name": "ARCHITECTURE"},
            output_schema={"ok": "bool"},
        ),
        DAGNode(
            node_id="read_adrs",
            kind="tool_or_script_step",
            depends_on=["read_arch"],
            tool_name="sdd_manage",
            args_builder={"action": "get_adrs"},
            output_schema={"ok": "bool"},
        ),
        DAGNode(
            node_id="synthesize_options",
            kind="bounded_llm_step",
            depends_on=["read_arch", "read_adrs"],
            tier="reasoning",
            system_prompt="You are a first-principles systems architect. Return JSON with keys `recommended_option`, `tradeoffs`, and `verification_plan`.",
            prompt_template="Question: {{params.question}} {{params.goal}}\nArchitecture:\n{{read_arch.output}}\nADRs:\n{{read_adrs.output}}",
            output_schema={},
        ),
    ],
    self_test_fn=lambda: bool(dry_run_dag(DAG).get("ok")),
)


if __name__ == "__main__":
    res = dry_run_dag(DAG)
    print(json.dumps(res))
    raise SystemExit(0 if res.get("ok") else 1)
