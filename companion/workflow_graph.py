"""
Deterministic State-Machine Graph Engine (`companion/workflow_graph.py` — Crossbar Layer).

Guarantees structured, Spec-Driven execution across Product Analysis, Discovery,
Architecture, Planning, Implementation, Verification, Recovery, and Reflection,
delegating code-controlled Python `DAGSpec` discovery and execution to `companion/dag_runtime.py`.

Graph Topology:
  [INTAKE] -> [DISCOVER] -> [DESIGN] -> [PLAN] -> [IMPLEMENT] -> [VERIFY] -> [REFLECT] -> [COMPLETE]
                  ^             ^          ^          |   ^          |
                  |             |          +----------+   +----------+
                  +-------------+-------------------------|
                                                    [RECOVER] (Self-Healing & Rollback Node)
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from companion.config import settings
from companion.dag_runtime import (
    DAGRuntimeBindings,
    discover_dags,
    execute_dag_by_name,
    register_dag_runtime_bindings,
)
from companion.disciplines import DISCIPLINES, DisciplineSpec, verify_repository_syntax
from companion.memory_store import MemoryStore, get_memory_store
from companion.safety import CheckpointManager, get_active_checkpoint_manager, run_unit_tests
from companion.workflow_dag import ExecutorBindings, is_node_output_passed as _is_node_output_passed

__all__ = [
    "ALLOWED_TRANSITIONS",
    "TransitionResult",
    "WORKFLOW_TEMPLATES",
    "WorkflowEngine",
    "WorkflowState",
    "_is_node_output_passed",
    "format_dags_catalog",
    "workflow_engine",
]


class WorkflowState(str, Enum):
    """Nodes in the Companion Engineering & Capability State Machine Graph."""

    IDLE = "IDLE"
    INTAKE = "INTAKE"
    DISCOVER = "DISCOVER"
    DESIGN = "DESIGN"
    PLAN = "PLAN"
    IMPLEMENT = "IMPLEMENT"
    VERIFY = "VERIFY"
    RECOVER = "RECOVER"
    REFLECT = "REFLECT"
    COMPLETE = "COMPLETE"
    ABORTED = "ABORTED"


ALLOWED_TRANSITIONS: Dict[WorkflowState, Tuple[WorkflowState, ...]] = {
    WorkflowState.IDLE: (WorkflowState.INTAKE, WorkflowState.DISCOVER, WorkflowState.PLAN),
    WorkflowState.INTAKE: (WorkflowState.DISCOVER, WorkflowState.DESIGN, WorkflowState.PLAN, WorkflowState.RECOVER, WorkflowState.ABORTED),
    WorkflowState.DISCOVER: (WorkflowState.DESIGN, WorkflowState.PLAN, WorkflowState.IMPLEMENT, WorkflowState.INTAKE, WorkflowState.RECOVER, WorkflowState.ABORTED),
    WorkflowState.DESIGN: (WorkflowState.PLAN, WorkflowState.IMPLEMENT, WorkflowState.DISCOVER, WorkflowState.RECOVER, WorkflowState.ABORTED),
    WorkflowState.PLAN: (WorkflowState.IMPLEMENT, WorkflowState.DESIGN, WorkflowState.DISCOVER, WorkflowState.RECOVER, WorkflowState.ABORTED),
    WorkflowState.IMPLEMENT: (WorkflowState.VERIFY, WorkflowState.PLAN, WorkflowState.DISCOVER, WorkflowState.RECOVER, WorkflowState.ABORTED),
    WorkflowState.VERIFY: (WorkflowState.REFLECT, WorkflowState.COMPLETE, WorkflowState.IMPLEMENT, WorkflowState.RECOVER, WorkflowState.PLAN, WorkflowState.ABORTED),
    WorkflowState.RECOVER: (WorkflowState.PLAN, WorkflowState.IMPLEMENT, WorkflowState.DISCOVER, WorkflowState.VERIFY, WorkflowState.ABORTED),
    WorkflowState.REFLECT: (WorkflowState.COMPLETE, WorkflowState.IMPLEMENT, WorkflowState.VERIFY, WorkflowState.RECOVER, WorkflowState.ABORTED),
    WorkflowState.COMPLETE: (WorkflowState.IDLE,),
    WorkflowState.ABORTED: (WorkflowState.IDLE,),
}

_FULL_PATH = [
    WorkflowState.INTAKE, WorkflowState.DISCOVER, WorkflowState.DESIGN, WorkflowState.PLAN,
    WorkflowState.IMPLEMENT, WorkflowState.VERIFY, WorkflowState.REFLECT, WorkflowState.COMPLETE,
]

WORKFLOW_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "self_evolution": {"name": "Self-Evolution & Capability Upgrade", "initial_state": WorkflowState.INTAKE, "recommended_path": _FULL_PATH, "description": "Full Spec-Driven lifecycle for evolving Companion's architecture, tools, or prompts with checkpoint safety."},
    "engineering_feature": {"name": "Feature & Deliverable Execution", "initial_state": WorkflowState.INTAKE, "recommended_path": _FULL_PATH, "description": "End-to-end discovery, design, planning, implementation, and evidence-verified delivery workflow."},
    "bug_fix": {"name": "Deterministic Bug Diagnosis & Fix", "initial_state": WorkflowState.DISCOVER, "recommended_path": [WorkflowState.DISCOVER, WorkflowState.PLAN, WorkflowState.IMPLEMENT, WorkflowState.VERIFY, WorkflowState.REFLECT, WorkflowState.COMPLETE], "description": "Root-cause reproduction, targeted fix, regression test, and verification."},
    "architecture_refactor": {"name": "Architecture, Hygiene & SOLID Refactoring", "initial_state": WorkflowState.DISCOVER, "recommended_path": _FULL_PATH[1:], "description": "Structural refactoring and dead-code pruning with mandatory checkpointing and test verification."},
}


@dataclass
class TransitionResult:
    """Result of a state-machine transition attempt."""

    success: bool
    workflow_id: Optional[int]
    from_state: str
    to_state: str
    message: str
    gate_details: Optional[str] = None
    checkpoint_id: Optional[str] = None


def format_dags_catalog(root: Optional[Path] = None) -> str:
    """Format discovered code-controlled Python `DAGSpec` graphs across `skills/*/dags/*.py`."""
    dags = discover_dags(root=root)
    if not dags:
        return "No verified Python DAGs found in `skills/*/dags/*.py`."
    lines = [f"=== Verified Code-Controlled Python DAGs ({len(dags)}) ==="]
    for d in dags:
        lines.append(f"  * `{d['name']}` (pack: `{d['skill']}`) — {d['description']}\n    Nodes: {' -> '.join(d['nodes'])} | Path: `{d['path']}`")
    return "\n".join(lines)


class WorkflowEngine:
    """Persistent, gate-enforced State Machine & Code-Controlled Python DAG Engine."""

    def __init__(self, store: Optional[MemoryStore] = None, cp_manager: Optional[CheckpointManager] = None, root: Optional[Path] = None):
        self._store = store
        self._cp_manager = cp_manager
        self._root = root.resolve() if root else None
        self._tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None
        self._subagent_fn: Optional[Callable[..., str]] = None

    @property
    def store(self) -> MemoryStore:
        return self._store or get_memory_store()

    @property
    def cp_manager(self) -> CheckpointManager:
        if self._cp_manager is not None:
            return self._cp_manager
        if self._root is not None:
            self._cp_manager = CheckpointManager(root=self._root, backup_dir=self._root / ".companion_backups", store=self._store)
            return self._cp_manager
        mem = self.store
        if mem.db_path.resolve() != settings.db_path.resolve():
            iso_root = mem.db_path.parent
            cp = CheckpointManager(root=iso_root, backup_dir=iso_root / ".companion_backups", store=mem)
            if self._store is not None:
                self._cp_manager = cp
            return cp
        return get_active_checkpoint_manager()

    @property
    def root(self) -> Path:
        return self._root or self.cp_manager.root

    def bind_executors(self, tool_fn: Callable[[str, Dict[str, Any]], str], subagent_fn: Optional[Callable[..., str]] = None, llm_fn: Optional[Callable[[str, str, str], str]] = None) -> None:
        """Bind application-layer tool, subagent, and bounded-LLM executors via Dependency Inversion."""
        self._tool_fn, self._subagent_fn = tool_fn, subagent_fn
        ExecutorBindings.bind(tool_fn, subagent_fn)
        register_dag_runtime_bindings(tool_fn=tool_fn, llm_fn=llm_fn)

    def start_workflow(self, goal: str, workflow_type: str = "engineering_feature", acceptance_criteria: str = "", create_initial_checkpoint: bool = True) -> TransitionResult:
        """Start a new deterministic workflow run in the state machine."""
        workflow_type = workflow_type if workflow_type in WORKFLOW_TEMPLATES else "engineering_feature"
        initial_state: WorkflowState = WORKFLOW_TEMPLATES[workflow_type]["initial_state"]
        cp_id = self.cp_manager.create_checkpoint(label=f"wf_{workflow_type}")["checkpoint_id"] if create_initial_checkpoint else ""
        wf_id = self.store.create_workflow_run(workflow_type=workflow_type, goal=goal, initial_state=initial_state.value, acceptance_criteria=acceptance_criteria, checkpoint_id=cp_id, metadata={"template": workflow_type, "verified": False})
        self.store.sync_memory_md()
        disc = DISCIPLINES.get(initial_state.value)
        return TransitionResult(True, wf_id, WorkflowState.IDLE.value, initial_state.value, f"Workflow #{wf_id} ({workflow_type}) started in state `{initial_state.value}`.{f' Active Discipline: {disc.name} — {disc.purpose}' if disc else ''}", checkpoint_id=cp_id or None)

    def _run_verification_gate(self) -> Tuple[bool, str]:
        syntax_ok, syntax_results = verify_repository_syntax(self.root)
        bad_syntax = [f"{r.path}: {r.syntax_error}" for r in syntax_results if not r.valid_syntax]
        if not syntax_ok:
            return False, f"Syntax gate failed on {len(bad_syntax)} file(s): {'; '.join(bad_syntax)}"
        test_res = run_unit_tests(self.root)
        if not test_res["passed"]:
            return False, f"Unit test gate failed (exit={test_res.get('returncode')}):\n{test_res.get('output', '')}"
        return True, f"Verification passed ({len(syntax_results)} Python file(s) checked; tests passed)."

    def _format_discipline_guidance(self, state_val: str) -> str:
        disc: Optional[DisciplineSpec] = DISCIPLINES.get(state_val)
        return "" if not disc else f"\nActive Discipline [{disc.name}]: {disc.purpose}\nChecklist:\n" + "\n".join(f"  - {item}" for item in disc.checklist) + f"\nRequired Deliverable: {disc.deliverable}"

    def transition(
        self,
        to_state_str: str,
        summary: str,
        acceptance_criteria: Optional[str] = None,
        run_tests_on_verify: bool = True,
    ) -> TransitionResult:
        """Attempt a deterministic state transition in the active workflow graph."""
        active = self.store.get_active_workflow()
        if not active:
            return TransitionResult(False, None, WorkflowState.IDLE.value, to_state_str, "error: No active workflow. Call workflow_manage(action='start', goal='...') first.")

        wf_id, raw_from = int(active["id"]), str(active["current_state"])
        if raw_from not in WorkflowState._value2member_map_:
            return TransitionResult(False, wf_id, raw_from, to_state_str, f"Active workflow #{wf_id} (`{active.get('workflow_type')}`) is managed automatically by the DAG runtime.")
        from_state = WorkflowState(raw_from)
        try:
            target_state = WorkflowState(to_state_str.strip().upper())
        except ValueError:
            return TransitionResult(False, wf_id, from_state.value, to_state_str, f"error: Unknown workflow state '{to_state_str}'. Valid states: {[s.value for s in WorkflowState]}")

        allowed = ALLOWED_TRANSITIONS.get(from_state, ())
        if target_state not in allowed:
            return TransitionResult(False, wf_id, from_state.value, target_state.value, f"GATE BLOCKED: Transition `{from_state.value} -> {target_state.value}` is not permitted. Allowed transitions from `{from_state.value}`: {[s.value for s in allowed]}.")

        cp_id = active.get("checkpoint_id") or ""
        gate_details: Optional[str] = None
        meta_updates: Dict[str, Any] = {}
        if target_state == WorkflowState.IMPLEMENT and not cp_id:
            cp_id = self.cp_manager.create_checkpoint(label=f"wf{wf_id}_pre_implement")["checkpoint_id"]
            gate_details = f"Created automatic pre-implementation safety checkpoint `{cp_id}`."

        if target_state in (WorkflowState.VERIFY, WorkflowState.REFLECT, WorkflowState.COMPLETE) and run_tests_on_verify:
            passed, details = self._run_verification_gate()
            gate_details = details
            meta_updates.update({"verified": passed, "last_verification": details})
            if not passed:
                self.store.update_workflow_state(wf_id, from_state.value, WorkflowState.RECOVER.value, f"Auto-routed to RECOVER due to verification gate failure: {details[:300]}", status="active", checkpoint_id=cp_id, verification_passed=False, metadata_updates=meta_updates, acceptance_criteria=acceptance_criteria)
                self.store.sync_memory_md()
                return TransitionResult(False, wf_id, from_state.value, WorkflowState.RECOVER.value, f"VERIFICATION GATE FAILED: Cannot transition to `{target_state.value}` while syntax or unit tests fail. Automatically transitioned workflow #{wf_id} to `RECOVER`.\nDiagnostics:\n{details}", gate_details=details, checkpoint_id=cp_id)

        new_status = "completed" if target_state == WorkflowState.COMPLETE else ("aborted" if target_state == WorkflowState.ABORTED else "active")
        self.store.update_workflow_state(wf_id, from_state.value, target_state.value, summary, status=new_status, checkpoint_id=cp_id, verification_passed=meta_updates.get("verified", True), metadata_updates=meta_updates, acceptance_criteria=acceptance_criteria)
        self.store.sync_memory_md()
        disc_msg = self._format_discipline_guidance(target_state.value)
        return TransitionResult(True, wf_id, from_state.value, target_state.value, f"Workflow #{wf_id} transitioned `{from_state.value} -> {target_state.value}`. {gate_details or ''}{disc_msg}".strip(), gate_details=gate_details, checkpoint_id=cp_id or None)

    def recover(self, reason: str, rollback: bool = False) -> TransitionResult:
        active = self.store.get_active_workflow()
        if not active:
            if rollback:
                rb = self.cp_manager.rollback_checkpoint()
                return TransitionResult(bool(rb.get("restored")), None, WorkflowState.IDLE.value, WorkflowState.IDLE.value, f"Emergency rollback executed: {rb}")
            return TransitionResult(False, None, WorkflowState.IDLE.value, WorkflowState.RECOVER.value, "No active workflow to recover.")
        wf_id, from_state, cp_id = int(active["id"]), active["current_state"], active.get("checkpoint_id") or ""
        rb_msg = ""
        if rollback and cp_id:
            rb = self.cp_manager.rollback_checkpoint(cp_id)
            rb_msg = f" Rolled back {rb.get('restored_count', 0)} files from checkpoint `{cp_id}`."
        self.store.add_system_note(f"Workflow #{wf_id} recovery triggered from {from_state}: {reason}.{rb_msg}", category="recovery")
        self.store.update_workflow_state(wf_id, from_state, WorkflowState.RECOVER.value, f"Recovery: {reason}.{rb_msg}".strip(), status="active", verification_passed=False)
        self.store.sync_memory_md()
        return TransitionResult(True, wf_id, from_state, WorkflowState.RECOVER.value, f"Workflow #{wf_id} entered `RECOVER` state.{rb_msg} Inspect the issue, adjust your plan, and transition to `PLAN` or `IMPLEMENT`.", checkpoint_id=cp_id or None)

    def render_hud(self) -> str:
        active = self.store.get_active_workflow()
        if not active:
            return (
                "State Machine: `IDLE` (No active software engineering workflow).\n"
                "  - Multi-step builds: `workflow_manage(action='start', ...)` or `/plan` / `/goal`.\n"
                "  - Verified Python DAGs: `workflow_manage(action='run_dag', dag='<name>')`.\n"
                "  - Project & Goal Graph items: `agile_manage` (`companion` | `human` | `collaborative`)."
            )
        wf_id, wf_type, raw_state = active["id"], active["workflow_type"], str(active["current_state"])
        if raw_state not in WorkflowState._value2member_map_:
            return f"Active DAG Workflow #{wf_id} (`{wf_type}`): {active['goal']}\n  Current Node: `{raw_state}`"
        curr = WorkflowState(raw_state)
        tmpl = WORKFLOW_TEMPLATES.get(wf_type, WORKFLOW_TEMPLATES["engineering_feature"])
        graph_parts = [f"**[{n.value}]**" if n == curr else n.value for n in tmpl["recommended_path"]]
        if curr == WorkflowState.RECOVER:
            graph_parts.append("**[RECOVER]**")
        allowed = [s.value for s in ALLOWED_TRANSITIONS.get(curr, ())]
        disc = DISCIPLINES.get(curr.value)
        lines = [
            f"Active Workflow #{wf_id} (`{wf_type}`): {active['goal']}",
            f"  Graph Progress: {' -> '.join(graph_parts)}",
            f"  Current State : `{curr.value}` | Allowed Next States: {allowed}",
        ]
        if active.get("acceptance_criteria"):
            lines.append(f"  Acceptance Criteria: {active['acceptance_criteria']}")
        if active.get("checkpoint_id"):
            lines.append(f"  Safety Checkpoint  : `{active['checkpoint_id']}`")
        if disc:
            lines.append(f"  Active Discipline  : {disc.name} — {disc.purpose}")
            lines.append(f"  Phase Deliverable  : {disc.deliverable}")
        return "\n".join(lines)

    def discover_dags(self) -> List[Dict[str, Any]]:
        return discover_dags(root=self.root)

    def format_dags_catalog(self) -> str:
        return format_dags_catalog(root=self.root)

    def run_dag(
        self,
        dag_name: str,
        params: Optional[Dict[str, Any]] = None,
        tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None,
        llm_fn: Optional[Callable[[str, str, str], str]] = None,
    ) -> Dict[str, Any]:
        tool_cb, _ = ExecutorBindings.resolve(tool_fn or self._tool_fn, self._subagent_fn)
        bindings = DAGRuntimeBindings(tool_fn=tool_cb, llm_fn=llm_fn) if (tool_cb or llm_fn) else None
        return execute_dag_by_name(dag_name, params=params, store=self.store, bindings=bindings, root=self.root)


workflow_engine = WorkflowEngine()
