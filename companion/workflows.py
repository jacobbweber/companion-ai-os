"""
Declarative JSON Workflow Engine, Domain Capability Pack Builder & Verification Specs (`companion/workflows.py`).

Replaces arbitrary Python-lambda `DAGSpec` files (`dag_runtime.py` + `workflow_dag.py`) with:
1. Declarative JSON Workflow Specs (`*.workflow.json`) across `skills/*/workflows/`, `data/skills/*/workflows/`,
   and `data/domains/*/workflows/` supporting `nodes` + `edges: [{from, to, condition}]` + `foreach` fan-out/fan-in.
2. Typed Node Catalog:
   - `llm_step` (`bounded_llm_step`): bounded zero-tool LLM step with mandatory JSON `output_schema` validation
   - `run_script`: deterministic Python CLI script runner with JSON payload I/O and `output_schema` validation
   - `call_tool` (`tool_or_script_step`): orthogonal primitive tool invocation with recursive `{{...}}` rendering
   - `mcp_step`: outbound MCP tool invocation
   - `human_gate`: SQLite `dag_runs` pause (`waiting_approval`) + exact-node `resume_dag_run`
3. `contextvars.copy_context().run` propagation across `ThreadPoolExecutor` worker threads.
4. `ensure_domain_pack`: self-contained ICM Domain Capability Pack initializer (`CONTEXT.md`, numbered stage folders,
   isolated WAL SQLite `data/domains/<pack>/<pack>.db`, `scripts/`, and `workflows/*.workflow.json`).
5. `GoalPlan`, `GoalStep`, and `VerificationSpec` verification contracts.
"""

import ast
import concurrent.futures
import contextlib
import contextvars
import inspect
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from companion.approvals import ActionRiskTier, get_approval_queue
from companion.config import SELF_PROJECT_ID, settings
from companion.context import discover_skills, get_knowledge_port, invalidate_skills_cache, rank_relevant_skills
from companion.memory_store import MemoryStore, get_memory_store, has_memory_store_override
from companion.telemetry import redact_secrets, telemetry
from companion.tools import check_command_safety, get_execution_target, validate_python_syntax
from companion.vault import CheckpointManager, checkpoint_manager, get_vault_manager

VALID_WORKFLOW_NODE_TYPES = (
    "llm_step",
    "run_script",
    "call_tool",
    "mcp_step",
    "human_gate",
    # Programmatic / backward-compatible aliases
    "bounded_llm_step",
    "tool_or_script_step",
    "python_step",
)
VALID_DAG_NODE_KINDS = VALID_WORKFLOW_NODE_TYPES

__all__ = [
    "DAGNode",
    "DAGNodeSpec",
    "DAGRuntimeBindings",
    "DAGSpec",
    "DOMAIN_VALIDATOR_REGISTRY",
    "ExecutorBindings",
    "GoalPlan",
    "GoalStep",
    "VALID_DAG_NODE_KINDS",
    "VALID_WORKFLOW_NODE_TYPES",
    "VerificationSpec",
    "WorkflowNode",
    "WorkflowSpec",
    "build_goal_plan_from_tasks",
    "build_skill_script_command",
    "check_make_vs_borrow",
    "dag_node",
    "discover_dags",
    "discover_skill_domain_validators",
    "discover_workflows",
    "distill_workflow_reflection",
    "dry_run_dag",
    "dry_run_workflow",
    "ensure_domain_pack",
    "evaluate_step_verification",
    "execute_customizations_action",
    "execute_dag_by_name",
    "execute_dag_spec",
    "execute_workflow_by_name",
    "execute_workflow_spec",
    "get_dag_run",
    "is_node_output_passed",
    "list_dag_runs",
    "load_workflow_spec_from_file",
    "parse_pep723_dependencies",
    "register_dag_runtime_bindings",
    "register_domain_validator",
    "resume_dag_run",
    "resume_workflow_run",
    "run_skill_script",
    "self_test_dag",
    "stage_and_promote_skill",
    "validate_skill_markdown",
    "validate_typed_payload",
]

_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FILE_TOKEN_RE = re.compile(r"`?([a-zA-Z0-9_./-]+\.(?:py|md|json|toml|yaml|yml|ini|html|css|js|ts|txt|sh|sql|csv|xml|stl|step|scad))`?")
_DAG_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_TYPE_MAP: Dict[str, Tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
    "list": (list,),
    "dict": (dict,),
    "any": (object,),
}
_KIND_ALIAS: Dict[str, str] = {
    "llm": "llm_step",
    "bounded_llm_step": "llm_step",
    "script": "run_script",
    "tool": "call_tool",
    "tool_or_script_step": "call_tool",
    "mcp": "mcp_step",
    "python": "python_step",
}


# ---------------------------------------------------------------------------
# 1. Dependency-Inverted Executor Bindings & Schema Validation
# ---------------------------------------------------------------------------
@dataclass
class DAGRuntimeBindings:
    tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None
    mcp_fn: Optional[Callable[[str, str, Dict[str, Any]], Dict[str, Any]]] = None
    llm_fn: Optional[Callable[[str, str, str], str]] = None


_DEFAULT_BINDINGS = DAGRuntimeBindings()


def register_dag_runtime_bindings(
    tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None,
    mcp_fn: Optional[Callable[[str, str, Dict[str, Any]], Dict[str, Any]]] = None,
    llm_fn: Optional[Callable[[str, str, str], str]] = None,
) -> None:
    if tool_fn is not None:
        _DEFAULT_BINDINGS.tool_fn = tool_fn
    if mcp_fn is not None:
        _DEFAULT_BINDINGS.mcp_fn = mcp_fn
    if llm_fn is not None:
        _DEFAULT_BINDINGS.llm_fn = llm_fn


class ExecutorBindings:
    _tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None
    _subagent_fn: Optional[Callable[..., str]] = None

    @classmethod
    def bind(cls, tool_fn: Callable[[str, Dict[str, Any]], str], subagent_fn: Optional[Callable[..., str]] = None) -> None:
        cls._tool_fn = tool_fn
        cls._subagent_fn = subagent_fn

    @classmethod
    def resolve(
        cls,
        tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None,
        subagent_fn: Optional[Callable[..., str]] = None,
    ) -> Tuple[Optional[Callable[[str, Dict[str, Any]], str]], Optional[Callable[..., str]]]:
        return tool_fn or cls._tool_fn, subagent_fn or cls._subagent_fn


def _norm_schema(sch: Any) -> Dict[str, str]:
    if not isinstance(sch, dict):
        return {}
    return {str(k): (v.__name__ if isinstance(v, type) else str(v)) for k, v in sch.items()}


def validate_typed_payload(payload: Any, schema: Dict[str, str], label: str) -> Optional[str]:
    """Validate `payload` dict against a typed contract like `{'items': 'list', 'note?': 'str'}`."""
    if not schema:
        return None
    if not isinstance(payload, dict):
        return f"{label} must be a dict, got {type(payload).__name__}."
    for raw_key, raw_type in schema.items():
        optional = raw_key.endswith("?")
        key = raw_key[:-1] if optional else raw_key
        tname = (raw_type or "any").strip().lower()
        if key not in payload or payload[key] is None:
            if optional:
                continue
            return f"{label} missing required key `{key}` ({tname})."
        val = payload[key]
        if tname == "int" and isinstance(val, bool):
            return f"{label} key `{key}` expected `int`, got `bool`."
        allowed = _TYPE_MAP.get(tname)
        if allowed is None:
            return f"{label} has unknown schema type `{tname}` on key `{key}`."
        if not isinstance(val, allowed):
            return f"{label} key `{key}` expected `{tname}`, got `{type(val).__name__}`."
    return None


# ---------------------------------------------------------------------------
# 2. Declarative `WorkflowSpec` & `WorkflowNode` (`DAGSpec` / `DAGNode` Aliases)
# ---------------------------------------------------------------------------
@dataclass
class WorkflowNode:
    """Single typed node inside a declarative `WorkflowSpec` (`*.workflow.json`)."""

    node_id: str
    kind: str = "call_tool"
    depends_on: List[str] = field(default_factory=list)
    input_schema: Dict[str, str] = field(default_factory=dict)
    output_schema: Dict[str, str] = field(default_factory=dict)
    config: Dict[str, Any] = field(default_factory=dict)
    fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    tool_name: str = ""
    script: str = ""
    cli_args: List[str] = field(default_factory=list)
    args_builder: Optional[Union[Dict[str, Any], Callable[[Dict[str, Any]], Dict[str, Any]]]] = None
    server_id: str = ""
    prompt_template: str = ""
    system_prompt: str = "Return concise, valid JSON strictly matching the requested output schema."
    tier: str = "fast"
    foreach: str = ""
    condition: Optional[Union[str, Callable[[Dict[str, Any]], bool]]] = None
    risk_tier: str = "R1"
    retries: int = 0
    retry_backoff_ms: int = 25
    timeout_sec: float = 60.0
    description: str = ""

    def __post_init__(self) -> None:
        self.kind = _KIND_ALIAS.get(self.kind, self.kind)
        if self.fn is not None and self.kind == "call_tool" and not self.tool_name:
            self.kind = "python_step"
        self.input_schema = _norm_schema(self.input_schema)
        self.output_schema = _norm_schema(self.output_schema)
        cfg = dict(self.config or {})
        if cfg:
            if not self.tool_name and (cfg.get("tool_name") or cfg.get("tool")):
                self.tool_name = str(cfg.get("tool_name") or cfg.get("tool"))
            if not self.script and cfg.get("script"):
                self.script = str(cfg["script"])
            raw_cli = cfg.get("cli_args") if isinstance(cfg.get("cli_args"), list) else (cfg.get("args") if isinstance(cfg.get("args"), list) else None)
            if not self.cli_args and raw_cli is not None:
                self.cli_args = [str(x) for x in raw_cli]
            if self.args_builder is None and isinstance(cfg.get("args"), dict):
                self.args_builder = dict(cfg["args"])
            if not self.server_id and (cfg.get("server_id") or cfg.get("server")):
                self.server_id = str(cfg.get("server_id") or cfg.get("server"))
            if not self.prompt_template and (cfg.get("prompt_template") or cfg.get("prompt")):
                self.prompt_template = str(cfg.get("prompt_template") or cfg.get("prompt"))
            if cfg.get("system_prompt"):
                self.system_prompt = str(cfg["system_prompt"])
            if cfg.get("tier"):
                self.tier = str(cfg["tier"])

    def to_dict(self) -> Dict[str, Any]:
        cfg = dict(self.config or {})
        if self.tool_name:
            cfg.setdefault("tool_name", self.tool_name)
        if self.script:
            cfg.setdefault("script", self.script)
        if self.cli_args:
            cfg.setdefault("cli_args", list(self.cli_args))
        if isinstance(self.args_builder, dict):
            cfg.setdefault("args", dict(self.args_builder))
        if self.server_id:
            cfg.setdefault("server_id", self.server_id)
        if self.prompt_template:
            cfg.setdefault("prompt_template", self.prompt_template)
        if self.tier and self.tier != "fast":
            cfg.setdefault("tier", self.tier)
        out: Dict[str, Any] = {"id": self.node_id, "type": self.kind, "config": cfg}
        if self.depends_on:
            out["depends_on"] = list(self.depends_on)
        if self.input_schema:
            out["input_schema"] = dict(self.input_schema)
        if self.output_schema:
            out["output_schema"] = dict(self.output_schema)
        if self.foreach:
            out["foreach"] = self.foreach
        if isinstance(self.condition, str) and self.condition:
            out["condition"] = self.condition
        if self.risk_tier != "R1":
            out["risk_tier"] = self.risk_tier
        if self.retries:
            out["retries"] = self.retries
        if self.timeout_sec != 60.0:
            out["timeout_sec"] = self.timeout_sec
        if self.description:
            out["description"] = self.description
        return out


DAGNode = WorkflowNode
DAGNodeSpec = WorkflowNode


def dag_node(
    node_id: str,
    *,
    depends_on: Optional[List[str]] = None,
    input_schema: Optional[Dict[str, str]] = None,
    output_schema: Optional[Dict[str, str]] = None,
    foreach: str = "",
    condition: Optional[Union[str, Callable[[Dict[str, Any]], bool]]] = None,
    risk_tier: str = "R1",
    retries: int = 0,
    timeout_sec: float = 60.0,
    description: str = "",
) -> Callable[[Callable[[Dict[str, Any]], Dict[str, Any]]], WorkflowNode]:
    def _wrap(func: Callable[[Dict[str, Any]], Dict[str, Any]]) -> WorkflowNode:
        return WorkflowNode(
            node_id=node_id,
            kind="python_step",
            depends_on=list(depends_on or []),
            input_schema=dict(input_schema or {}),
            output_schema=dict(output_schema or {}),
            fn=func,
            foreach=foreach,
            condition=condition,
            risk_tier=risk_tier,
            retries=retries,
            timeout_sec=timeout_sec,
            description=description or (func.__doc__ or "").strip(),
        )
    return _wrap


@dataclass
class WorkflowSpec:
    """Verified directed acyclic workflow definition supporting declarative `*.workflow.json`."""

    name: str
    description: str = ""
    nodes: List[WorkflowNode] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)
    input_schema: Dict[str, str] = field(default_factory=dict)
    self_test_fn: Optional[Callable[[], bool]] = None
    source_path: str = ""

    def __post_init__(self) -> None:
        self.input_schema = _norm_schema(self.input_schema)
        if not self.description and self.name:
            self.description = f"Declarative workflow `{self.name}`."
        coerced: List[WorkflowNode] = []
        valid_fields = set(WorkflowNode.__dataclass_fields__)
        for nd in list(self.nodes or []):
            if isinstance(nd, dict):
                d = dict(nd)
                if "self_test_fn" in d and self.self_test_fn is None and callable(d["self_test_fn"]):
                    self.self_test_fn = d.pop("self_test_fn")
                nid = str(d.get("node_id") or d.get("id") or d.get("name") or "").strip()
                raw_k = str(d.get("kind") or d.get("type") or ("python_step" if callable(d.get("fn") or d.get("func")) else "call_tool"))
                kw = {k: v for k, v in d.items() if k in valid_fields}
                kw["node_id"] = nid
                kw["kind"] = _KIND_ALIAS.get(raw_k, raw_k)
                if "func" in d and "fn" not in kw:
                    kw["fn"] = d["func"]
                if "prompt" in d and "prompt_template" not in kw:
                    kw["prompt_template"] = str(d["prompt"])
                if "tool" in d and "tool_name" not in kw:
                    kw["tool_name"] = str(d["tool"])
                if "args" in d:
                    if isinstance(d["args"], dict) and "args_builder" not in kw:
                        kw["args_builder"] = dict(d["args"])
                    elif isinstance(d["args"], list) and "cli_args" not in kw:
                        kw["cli_args"] = [str(x) for x in d["args"]]
                if "script_args" in d and isinstance(d["script_args"], list) and "cli_args" not in kw:
                    kw["cli_args"] = [str(x) for x in d["script_args"]]
                coerced.append(WorkflowNode(**kw))
            else:
                coerced.append(nd)
        by_id = {n.node_id: n for n in coerced if n.node_id}
        for edge in list(self.edges or []):
            if not isinstance(edge, dict):
                continue
            src = str(edge.get("from") or edge.get("source") or "").strip()
            dst = str(edge.get("to") or edge.get("target") or "").strip()
            e_cond = str(edge.get("condition") or "").strip()
            if src and dst and dst in by_id:
                target_nd = by_id[dst]
                if src not in target_nd.depends_on:
                    target_nd.depends_on.append(src)
                if e_cond and not target_nd.condition:
                    target_nd.condition = e_cond
        self.nodes = coerced

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], source_path: str = "") -> "WorkflowSpec":
        if not isinstance(raw, dict):
            raise ValueError("Workflow JSON root must be an object.")
        nodes_raw = raw.get("nodes")
        if not isinstance(nodes_raw, list):
            raise ValueError("Workflow JSON must contain a `nodes` array.")
        for item in nodes_raw:
            if isinstance(item, dict) and str(item.get("type") or item.get("kind") or "") == "python_step":
                raise ValueError("Declarative JSON workflows forbid `python_step` code lambdas; use `run_script`, `call_tool`, or `llm_step`.")
        return cls(
            name=str(raw.get("name") or "").strip(),
            description=str(raw.get("description") or "").strip(),
            nodes=nodes_raw,
            edges=list(raw.get("edges") or []) if isinstance(raw.get("edges"), list) else [],
            input_schema=raw.get("input_schema") if isinstance(raw.get("input_schema"), dict) else {},
            source_path=source_path,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": list(self.edges),
        }

    def validate(self) -> Optional[str]:
        if not self.name or not _KEBAB_RE.match(self.name):
            return f"Workflow name must be kebab-case (`[a-z0-9-]+`), got `{self.name}`."
        if len((self.description or "").strip()) < 10:
            return f"Workflow `{self.name}` must include a description (>= 10 chars)."
        if not self.nodes:
            return f"Workflow `{self.name}` must define at least one node."
        by_id: Dict[str, WorkflowNode] = {}
        for nd in self.nodes:
            nid = (nd.node_id or "").strip()
            if not nid or nid in by_id:
                return f"Workflow `{self.name}` has empty or duplicate node id `{nid}`."
            if nd.kind not in ("llm_step", "run_script", "call_tool", "mcp_step", "human_gate", "python_step"):
                return f"Node `{nid}` has invalid type `{nd.kind}`."
            if nd.kind == "python_step" and nd.fn is None:
                return f"Python node `{nid}` must bind `fn`."
            if nd.kind == "run_script" and not nd.script.strip():
                return f"Node `{nid}` (`run_script`) must specify `script`."
            if nd.kind in ("call_tool", "mcp_step") and not nd.tool_name.strip():
                return f"Node `{nid}` (`{nd.kind}`) must specify `tool_name`."
            if nd.kind == "llm_step" and not nd.prompt_template.strip():
                return f"Node `{nid}` (`llm_step`) must specify `prompt_template`."
            for schema_lbl, sch in (("input_schema", nd.input_schema), ("output_schema", nd.output_schema)):
                for rk, rt in sch.items():
                    if rt.strip().lower() not in _TYPE_MAP:
                        return f"Node `{nid}` {schema_lbl} key `{rk}` uses unknown type `{rt}`."
            by_id[nid] = nd
        for edge in self.edges:
            if isinstance(edge, dict):
                s_id = str(edge.get("from") or edge.get("source") or "").strip()
                t_id = str(edge.get("to") or edge.get("target") or "").strip()
                if s_id not in by_id or t_id not in by_id:
                    return f"Workflow `{self.name}` edge `{s_id} -> {t_id}` references unknown node."
        for nd in self.nodes:
            for dep in nd.depends_on:
                if dep not in by_id:
                    return f"Node `{nd.node_id}` depends on unknown node `{dep}`."
            if nd.foreach:
                src_root = nd.foreach.split(".", 1)[0].strip()
                if src_root != "params" and src_root not in nd.depends_on:
                    return f"Node `{nd.node_id}` foreach `{nd.foreach}` must reference `params.<key>` or a declared `depends_on` node."
        try:
            self.topological_order()
        except ValueError as exc:
            return str(exc)
        return None

    def topological_order(self) -> List[WorkflowNode]:
        by_id = {n.node_id: n for n in self.nodes}
        in_deg = {n.node_id: len(n.depends_on) for n in self.nodes}
        children: Dict[str, List[str]] = {n.node_id: [] for n in self.nodes}
        for n in self.nodes:
            for dep in n.depends_on:
                children[dep].append(n.node_id)
        ready = [n.node_id for n in self.nodes if in_deg[n.node_id] == 0]
        ordered: List[WorkflowNode] = []
        while ready:
            cur = ready.pop(0)
            ordered.append(by_id[cur])
            for ch in children[cur]:
                in_deg[ch] -= 1
                if in_deg[ch] == 0:
                    ready.append(ch)
        if len(ordered) != len(self.nodes):
            raise ValueError(f"Workflow `{self.name}` contains a dependency cycle.")
        return ordered


DAGSpec = WorkflowSpec


# ---------------------------------------------------------------------------
# 3. Workflow Node Execution, `contextvars` Threading & SQLite Checkpoints
# ---------------------------------------------------------------------------
def _init_dag_tables(store: MemoryStore) -> None:
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS dag_runs ("
        "run_id TEXT PRIMARY KEY, dag_name TEXT NOT NULL, status TEXT NOT NULL, "
        "params_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', "
        "paused_node_id TEXT DEFAULT '', approval_id TEXT DEFAULT '', error TEXT DEFAULT '', "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS dag_node_runs ("
        "run_id TEXT NOT NULL, node_id TEXT NOT NULL, status TEXT NOT NULL, "
        "attempts INTEGER DEFAULT 1, output_json TEXT DEFAULT '{}', error TEXT DEFAULT '', "
        "duration_ms REAL DEFAULT 0.0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (run_id, node_id))"
    )


def _resolve_dotted_path(ctx: Dict[str, Any], expr: str) -> Any:
    parts = [p.strip() for p in expr.split(".") if p.strip()]
    if not parts:
        return None
    if parts[0] == "params":
        cur: Any = ctx.get("params", {})
    elif parts[0] == "item":
        cur = ctx.get("item")
    elif parts[0] == "nodes" and len(parts) >= 2:
        cur = ctx.get("nodes", {}).get(parts[1])
        parts = parts[1:]
    else:
        cur = ctx.get("nodes", {}).get(parts[0])
    for part in parts[1:]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _render_template_str(tpl: str, ctx: Dict[str, Any]) -> Any:
    exact = re.fullmatch(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}", tpl.strip())
    if exact:
        expr = exact.group(1).strip()
        val = ctx.get("item") if expr == "item" else _resolve_dotted_path(ctx, expr)
        if val is None and expr in ctx.get("params", {}):
            val = ctx["params"][expr]
        if isinstance(val, (dict, list, bool, int, float)):
            return val
        return "" if val is None else str(val)

    def _sub(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        val = ctx.get("item") if expr == "item" else _resolve_dotted_path(ctx, expr)
        if val is None and expr in ctx.get("params", {}):
            val = ctx["params"][expr]
        return json.dumps(val) if isinstance(val, (dict, list)) else str(val if val is not None else "")

    rendered = re.sub(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}", _sub, tpl)
    if "{" in rendered and "}" in rendered:
        with contextlib.suppress(Exception):
            rendered = rendered.format_map(ctx)
    return rendered


def _render_value(val: Any, ctx: Dict[str, Any]) -> Any:
    if isinstance(val, str):
        return _render_template_str(val, ctx)
    if isinstance(val, dict):
        return {k: _render_value(v, ctx) for k, v in val.items()}
    if isinstance(val, list):
        return [_render_value(v, ctx) for v in val]
    return val


def _eval_node_condition(cond: Optional[Union[str, Callable[[Dict[str, Any]], bool]]], ctx: Dict[str, Any]) -> bool:
    if cond is None:
        return True
    if callable(cond):
        return bool(cond(ctx))
    clean = str(cond).strip()
    if not clean or clean == "always":
        return True
    if "==" in clean:
        lhs, rhs = [x.strip() for x in clean.split("==", 1)]
        lval = _resolve_dotted_path(ctx, lhs)
        rval = rhs.strip("'\"")
        if rval.lower() in ("true", "false"):
            return bool(lval) is (rval.lower() == "true")
        return str(lval) == rval
    neg = clean.startswith("!")
    path = clean[1:].strip() if neg else clean
    res = bool(_resolve_dotted_path(ctx, path))
    return (not res) if neg else res


def _parse_llm_json(raw_text: str, require_json_object: bool = False) -> Dict[str, Any]:
    text = (raw_text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    for cand in ([fenced.group(1)] if fenced else []) + [text]:
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    brace = re.search(r"(\{.*\})", text, re.S)
    if brace:
        try:
            parsed = json.loads(brace.group(1))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    if require_json_object:
        raise ValueError(f"LLM step did not return a valid JSON object: {text[:180]}")
    return {"text": text, "ok": bool(text)}


def _resolve_workflow_script(script_ref: str, spec_source_path: str = "") -> Path:
    raw = (script_ref or "").strip()
    cand = Path(raw)
    if cand.is_absolute() and cand.is_file():
        return cand.resolve()
    norm = raw.replace("\\", "/").strip("/")
    roots: List[Path] = []
    if spec_source_path:
        sp = Path(spec_source_path).resolve()
        # e.g. skills/<pack>/workflows/foo.workflow.json -> pack root is sp.parent.parent
        roots.extend([sp.parent, sp.parent.parent])
    roots.extend([
        settings.active_project_root.resolve(),
        settings.effective_data_dir.resolve(),
        settings.domains_dir.resolve(),
        settings.user_skills_dir.resolve(),
        settings.root.resolve(),
    ])
    for r in roots:
        for rel in (norm, f"scripts/{norm}"):
            p = (r / rel).resolve()
            if p.is_file():
                return p
    if norm.startswith("data/"):
        p_data = (settings.effective_data_dir / norm[5:]).resolve()
        if p_data.is_file():
            return p_data
    raise FileNotFoundError(f"Workflow script `{script_ref}` not found.")


def _invoke_run_script(node: WorkflowNode, unit_ctx: Dict[str, Any], spec_source: str) -> Dict[str, Any]:
    script_path = _resolve_workflow_script(node.script, spec_source)
    rendered_args = [str(_render_value(a, unit_ctx)) for a in (node.cli_args or [])]
    extra_payload = _render_value(node.args_builder, unit_ctx) if isinstance(node.args_builder, dict) else {}
    stdin_obj = {
        "params": unit_ctx.get("params", {}),
        "input": {**(unit_ctx.get("input") or {}), **(extra_payload if isinstance(extra_payload, dict) else {})},
        "nodes": unit_ctx.get("nodes", {}),
        "item": unit_ctx.get("item"),
    }
    mem = get_memory_store()
    env = {
        **get_execution_target().exec_env_and_cwd()[0],
        "PYTHONPATH": str(settings.root.resolve()),
        "COMPANION_DB_PATH": str(mem.db_path.resolve()),
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }
    proc = subprocess.run(
        [sys.executable, "-B", str(script_path), *rendered_args],
        input=json.dumps(stdin_obj, default=str),
        cwd=str(settings.active_project_root.resolve()),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(1.0, float(node.timeout_sec)),
    )
    stdout_s = (proc.stdout or "").strip()
    stderr_s = (proc.stderr or "").strip()
    parsed: Optional[Dict[str, Any]] = None
    if stdout_s:
        for candidate in (stdout_s, stdout_s.splitlines()[-1]):
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    parsed = obj
                    break
            except Exception:
                continue
    if proc.returncode != 0:
        err_detail = (parsed or {}).get("error") or stderr_s or stdout_s or f"exit={proc.returncode}"
        raise RuntimeError(f"Script `{script_path.name}` failed (exit={proc.returncode}): {err_detail}")
    if parsed is not None:
        if parsed.get("ok") is False:
            raise RuntimeError(f"Script `{script_path.name}` reported error: {parsed.get('error') or parsed}")
        return parsed
    return {"ok": True, "output": stdout_s}


def _invoke_single_unit(node: WorkflowNode, unit_ctx: Dict[str, Any], bindings: DAGRuntimeBindings, spec_source: str = "") -> Dict[str, Any]:
    in_err = validate_typed_payload(unit_ctx.get("input", unit_ctx.get("params", {})), node.input_schema, f"Node `{node.node_id}` input")
    if in_err:
        raise ValueError(in_err)
    if node.kind == "human_gate":
        return {"approved": True, "node_id": node.node_id}
    if node.kind == "python_step":
        assert node.fn is not None
        params = list(inspect.signature(node.fn).parameters.values())
        merged = unit_ctx.get("input", unit_ctx.get("params", {}))
        if params and all(p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY) for p in params) and params[0].name not in ("ctx", "context", "_ctx", "unit_ctx") and all(p.name in merged or p.default is not p.empty for p in params):
            out = node.fn(**{p.name: merged[p.name] for p in params if p.name in merged})
        else:
            out = node.fn(unit_ctx)
    elif node.kind == "run_script":
        out = _invoke_run_script(node, unit_ctx, spec_source)
    elif node.kind == "call_tool":
        if bindings.tool_fn is None:
            raise RuntimeError("No tool executor bound to workflow runtime.")
        raw_args = node.args_builder(unit_ctx) if callable(node.args_builder) else dict(node.args_builder or {})
        rendered = _render_value(raw_args, unit_ctx)
        tool_out = bindings.tool_fn(node.tool_name, rendered)
        if str(tool_out).startswith("error:"):
            raise RuntimeError(str(tool_out))
        try:
            parsed = json.loads(tool_out)
            out = parsed if isinstance(parsed, dict) else {"output": str(tool_out), "ok": True}
        except Exception:
            out = {"output": str(tool_out), "ok": not str(tool_out).startswith("exit=1")}
    elif node.kind == "mcp_step":
        if bindings.mcp_fn is None:
            raise RuntimeError("No MCP executor bound to workflow runtime.")
        raw_args = node.args_builder(unit_ctx) if callable(node.args_builder) else dict(node.args_builder or {})
        rendered = _render_value(raw_args, unit_ctx)
        out = bindings.mcp_fn(node.server_id, node.tool_name, rendered)
        if not isinstance(out, dict) or out.get("ok") is False:
            raise RuntimeError(f"MCP step `{node.node_id}` failed: {out}")
    elif node.kind == "llm_step":
        if bindings.llm_fn is None:
            raise RuntimeError("No bounded LLM executor bound to workflow runtime.")
        prompt = str(_render_value(node.prompt_template, unit_ctx))
        schema_hint = f"\n\nRespond ONLY with a valid JSON object matching schema `{json.dumps(node.output_schema)}`." if node.output_schema else ""
        raw_reply = bindings.llm_fn(prompt + schema_hint, node.system_prompt, node.tier)
        strict_json = bool(node.output_schema and set(node.output_schema) - {"text", "text?", "ok", "ok?"})
        out = _parse_llm_json(raw_reply, require_json_object=strict_json)
    else:
        raise ValueError(f"Unsupported workflow node type `{node.kind}`.")
    out_err = validate_typed_payload(out, node.output_schema, f"Node `{node.node_id}` output")
    if out_err:
        raise ValueError(out_err)
    return out


def _execute_node_with_retries(
    node: WorkflowNode, ctx: Dict[str, Any], bindings: DAGRuntimeBindings, spec_source: str = "",
) -> Tuple[Dict[str, Any], int]:
    max_attempts = max(1, int(node.retries) + 1)
    last_err: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                if node.foreach:
                    seq = _resolve_dotted_path(ctx, node.foreach)
                    if not isinstance(seq, list):
                        raise ValueError(f"Node `{node.node_id}` foreach `{node.foreach}` resolved to {type(seq).__name__}, expected list.")
                    results: List[Dict[str, Any]] = []
                    for idx, elem in enumerate(seq):
                        sub_in = elem if isinstance(elem, dict) else {"item": elem, **ctx["params"]}
                        top_in = {k: v for k, v in sub_in.items() if k not in ("params", "nodes", "run_id", "input", "item", "item_index")}
                        sub_ctx = {**top_in, **ctx, "item": elem, "item_index": idx, "input": sub_in}
                        fut = pool.submit(contextvars.copy_context().run, _invoke_single_unit, node, sub_ctx, bindings, spec_source)
                        results.append(fut.result(timeout=max(0.5, float(node.timeout_sec))))
                    return {"items": results, "count": len(results)}, attempt
                merged_in = {**ctx["params"], **{k: v for dep in node.depends_on for k, v in (ctx["nodes"].get(dep) or {}).items() if isinstance(ctx["nodes"].get(dep), dict)}}
                top_in = {k: v for k, v in merged_in.items() if k not in ("params", "nodes", "run_id", "input", "item", "item_index")}
                unit_ctx = {**top_in, **ctx, "input": merged_in}
                fut = pool.submit(contextvars.copy_context().run, _invoke_single_unit, node, unit_ctx, bindings, spec_source)
                return fut.result(timeout=max(0.5, float(node.timeout_sec))), attempt
        except Exception as exc:
            last_err = exc
            if attempt < max_attempts and node.retry_backoff_ms > 0:
                time.sleep(node.retry_backoff_ms / 1000.0)
    raise RuntimeError(f"Node `{node.node_id}` failed after {max_attempts} attempt(s): {last_err}")


def _save_node_checkpoint(store: MemoryStore, run_id: str, node_id: str, status: str, attempts: int, output: Dict[str, Any], error: str, duration_ms: float) -> None:
    with store._lock:
        store.conn.execute(
            "INSERT INTO dag_node_runs (run_id, node_id, status, attempts, output_json, error, duration_ms, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(run_id, node_id) DO UPDATE SET status = excluded.status, attempts = excluded.attempts, "
            "output_json = excluded.output_json, error = excluded.error, duration_ms = excluded.duration_ms, updated_at = CURRENT_TIMESTAMP",
            (run_id, node_id, status, attempts, json.dumps(output), error, round(duration_ms, 2)),
        )
        store.conn.commit()


def _load_and_init_dag_run(mem: MemoryStore, rid: str, dag_name: str, eff_params: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], set[str]]:
    cached_nodes: Dict[str, Dict[str, Any]] = {}
    completed_ids: set[str] = set()
    for nrow in mem._fetch_dicts(["node_id", "status", "output_json"], "SELECT node_id, status, output_json FROM dag_node_runs WHERE run_id = ?", (rid,)):
        if nrow["status"] in ("completed", "skipped"):
            completed_ids.add(str(nrow["node_id"]))
            try:
                cached_nodes[str(nrow["node_id"])] = json.loads(nrow["output_json"] or "{}")
            except Exception:
                cached_nodes[str(nrow["node_id"])] = {}
    with mem._lock:
        mem.conn.execute(
            "INSERT INTO dag_runs (run_id, dag_name, status, params_json, outputs_json, paused_node_id, approval_id, error) VALUES (?, ?, 'running', ?, ?, '', '', '') "
            "ON CONFLICT(run_id) DO UPDATE SET status = 'running', paused_node_id = '', error = '', updated_at = CURRENT_TIMESTAMP",
            (rid, dag_name, json.dumps(eff_params), json.dumps(cached_nodes)),
        )
        mem.conn.commit()
    return cached_nodes, completed_ids


def execute_workflow_spec(
    dag: WorkflowSpec, params: Optional[Dict[str, Any]] = None, *, store: Optional[MemoryStore] = None,
    bindings: Optional[DAGRuntimeBindings] = None, run_id: Optional[str] = None, approved_nodes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute a `WorkflowSpec` with SQLite step checkpointing, `foreach` fan-out/fan-in, and `human_gate`/`R3` pause."""
    val_err = dag.validate()
    if val_err:
        return {"ok": False, "status": "invalid_dag", "error": val_err, "summary": f"Workflow `{dag.name}` validation failed: {val_err}"}
    eff_params = dict(params or {})
    p_err = validate_typed_payload(eff_params, dag.input_schema, f"Workflow `{dag.name}` params")
    if p_err:
        return {"ok": False, "status": "invalid_params", "error": p_err, "summary": f"Workflow `{dag.name}` input error: {p_err}"}

    mem = store or get_memory_store()
    mem.ensure_schema("dag_runtime", _init_dag_tables)
    eff_bindings, rid, approved_set = (bindings or _DEFAULT_BINDINGS), (run_id or f"dagrun_{uuid.uuid4().hex[:10]}").strip(), set(approved_nodes or [])
    cached_nodes, completed_ids = _load_and_init_dag_run(mem, rid, dag.name, eff_params)

    ctx: Dict[str, Any] = {"params": eff_params, "nodes": dict(cached_nodes), "run_id": rid}
    ordered = dag.topological_order()
    for node in ordered:
        if node.node_id in completed_ids:
            continue
        if not _eval_node_condition(node.condition, ctx):
            ctx["nodes"][node.node_id] = {"skipped": True}
            _save_node_checkpoint(mem, rid, node.node_id, "skipped", 0, {"skipped": True}, "", 0.0)
            continue
        if (node.kind == "human_gate" or node.risk_tier.upper() == "R3") and (node.node_id not in approved_set):
            allowed, apr = get_approval_queue(mem).check_or_enqueue(
                category="dag_gate", action_name="resume_dag_run", target=f"{dag.name}:{node.node_id}",
                summary=f"Approve workflow gate `{node.node_id}` in `{dag.name}` (`{rid}`)",
                payload={"run_id": rid, "dag_name": dag.name, "node_id": node.node_id},
                requester="companion", confirmed=False, always_require_in_autonomous=True,
            )
            if not allowed:
                with mem._lock:
                    mem.conn.execute(
                        "UPDATE dag_runs SET status = 'waiting_approval', outputs_json = ?, paused_node_id = ?, approval_id = ?, updated_at = CURRENT_TIMESTAMP WHERE run_id = ?",
                        (json.dumps(ctx["nodes"]), node.node_id, apr["approval_id"], rid),
                    )
                    mem.conn.commit()
                return {"ok": True, "status": "waiting_approval", "run_id": rid, "dag_name": dag.name, "paused_node_id": node.node_id, "approval_id": apr["approval_id"], "nodes": ctx["nodes"], "summary": f"Workflow `{dag.name}` (`{rid}`) paused at `{node.node_id}` awaiting approval (`{apr['approval_id']}`)."}
        t0 = time.perf_counter()
        try:
            out, attempts = _execute_node_with_retries(node, ctx, eff_bindings, spec_source=dag.source_path)
        except Exception as exc:
            dur, err_msg = (time.perf_counter() - t0) * 1000.0, str(exc)
            _save_node_checkpoint(mem, rid, node.node_id, "failed", max(1, node.retries + 1), {}, err_msg, dur)
            with mem._lock:
                mem.conn.execute("UPDATE dag_runs SET status = 'failed', outputs_json = ?, paused_node_id = ?, error = ?, updated_at = CURRENT_TIMESTAMP WHERE run_id = ?", (json.dumps(ctx["nodes"]), node.node_id, err_msg, rid))
                mem.conn.commit()
            telemetry.log("WARN", "workflows", f"Workflow `{dag.name}` failed at `{node.node_id}`: {err_msg[:140]}")
            return {"ok": False, "status": "failed", "run_id": rid, "dag_name": dag.name, "failed_node_id": node.node_id, "error": err_msg, "nodes": ctx["nodes"], "summary": f"Workflow `{dag.name}` (`{rid}`) failed at `{node.node_id}`: {err_msg}"}
        ctx["nodes"][node.node_id] = out
        _save_node_checkpoint(mem, rid, node.node_id, "completed", attempts, out, "", (time.perf_counter() - t0) * 1000.0)

    with mem._lock:
        mem.conn.execute("UPDATE dag_runs SET status = 'completed', outputs_json = ?, paused_node_id = '', error = '', updated_at = CURRENT_TIMESTAMP WHERE run_id = ?", (json.dumps(ctx["nodes"]), rid))
        mem.conn.commit()
    final_out = ctx["nodes"].get(ordered[-1].node_id, {})
    return {"ok": True, "status": "completed", "run_id": rid, "dag_name": dag.name, "nodes": ctx["nodes"], "final_output": final_out, "summary": f"Workflow `{dag.name}` (`{rid}`) completed ({len(ordered)} node(s)). Final output: {json.dumps(final_out, default=str)}"}


execute_dag_spec = execute_workflow_spec


def load_workflow_spec_from_file(path: Path) -> WorkflowSpec:
    """Load and parse a declarative JSON workflow file (`*.workflow.json`)."""
    resolved = path.resolve()
    if resolved.suffix.lower() != ".json":
        raise ValueError(f"Only declarative JSON workflows (`*.workflow.json`) are permitted on disk; got `{resolved.name}`.")
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    return WorkflowSpec.from_dict(raw, source_path=str(resolved))


def dry_run_workflow(dag_or_path: Union[WorkflowSpec, Path]) -> Dict[str, Any]:
    """Validate acyclic topology, node schemas, script paths, and `foreach` bindings without side effects."""
    try:
        dag = load_workflow_spec_from_file(dag_or_path) if isinstance(dag_or_path, Path) else dag_or_path
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    err = dag.validate()
    if err:
        return {"ok": False, "dag_name": dag.name, "error": err}
    for nd in dag.nodes:
        if nd.kind == "run_script":
            try:
                _resolve_workflow_script(nd.script, dag.source_path)
            except Exception as exc:
                return {"ok": False, "dag_name": dag.name, "error": str(exc)}
    ordered = [n.node_id for n in dag.topological_order()]
    return {"ok": True, "dag_name": dag.name, "description": dag.description, "topological_order": ordered, "node_count": len(ordered)}


dry_run_dag = dry_run_workflow


def self_test_dag(path: Path, timeout_sec: int = 20) -> Dict[str, Any]:
    """Validate declarative `*.workflow.json` topology, schemas, and any referenced script `--self-test`."""
    dry = dry_run_workflow(path)
    if not dry.get("ok"):
        return dry
    dag = load_workflow_spec_from_file(path)
    checked_scripts: List[str] = []
    for nd in dag.nodes:
        if nd.kind == "run_script":
            sp = _resolve_workflow_script(nd.script, dag.source_path)
            src = sp.read_text(encoding="utf-8", errors="replace")
            if "--self-test" in src and str(sp) not in checked_scripts:
                checked_scripts.append(str(sp))
                proc = subprocess.run(
                    [sys.executable, "-B", str(sp), "--self-test"],
                    cwd=str(settings.root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_sec,
                )
                if proc.returncode != 0:
                    return {**dry, "ok": False, "error": f"Script `{sp.name} --self-test` failed: {(proc.stderr or proc.stdout).strip()[:280]}"}
    return {**dry, "ok": True, "self_test": "declarative_json_verified"}


def discover_workflows(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Discover all verified declarative `*.workflow.json` specs across `skills/`, `data/skills/`, and `data/domains/`."""
    base = (root or settings.root).resolve()
    search_roots = [base / "skills", base / "data" / "skills", base / "data" / "domains"]
    if root is None or base == settings.root.resolve():
        eff_data = settings.effective_data_dir.resolve()
        for sub in ("skills", "domains"):
            cand = eff_data / sub
            if cand.is_dir() and cand not in search_roots:
                search_roots.append(cand)
    found: Dict[str, Dict[str, Any]] = {}
    for sroot in search_roots:
        if not sroot.is_dir():
            continue
        for pat in ("*/workflows/*.workflow.json", "*/workflows/*.json", "*/dags/*.workflow.json", "*/dags/*.json"):
            for j_file in sorted(sroot.glob(pat)):
                if j_file.name.startswith("_"):
                    continue
                try:
                    dag = load_workflow_spec_from_file(j_file)
                    if dag.validate() is None:
                        found[dag.name] = {
                            "name": dag.name,
                            "skill": j_file.parent.parent.name,
                            "description": dag.description,
                            "path": str(j_file),
                            "nodes": [n.node_id for n in dag.topological_order()],
                            "input_schema": dag.input_schema,
                        }
                except Exception:
                    continue
    return [found[k] for k in sorted(found)]


discover_dags = discover_workflows


def execute_workflow_by_name(
    dag_name: str, params: Optional[Dict[str, Any]] = None, *, store: Optional[MemoryStore] = None,
    bindings: Optional[DAGRuntimeBindings] = None, root: Optional[Path] = None,
    run_id: Optional[str] = None, approved_nodes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    clean = (dag_name or "").strip().lower().removesuffix(".workflow.json").removesuffix(".json").removesuffix(".py").replace("_", "-")
    catalog = {d["name"]: d for d in discover_workflows(root=root)}
    if clean not in catalog:
        err = f"Workflow `{dag_name}` not found. Available: {sorted(catalog)}"
        return {"ok": False, "status": "not_found", "error": err, "summary": err}
    dag = load_workflow_spec_from_file(Path(catalog[clean]["path"]))
    return execute_workflow_spec(dag, params=params, store=store, bindings=bindings, run_id=run_id, approved_nodes=approved_nodes)


execute_dag_by_name = execute_workflow_by_name


def resume_workflow_run(
    run_id: str, *, approved_node_id: str = "", store: Optional[MemoryStore] = None,
    bindings: Optional[DAGRuntimeBindings] = None, root: Optional[Path] = None, dag_spec: Optional[WorkflowSpec] = None,
) -> Dict[str, Any]:
    mem = store or get_memory_store()
    mem.ensure_schema("dag_runtime", _init_dag_tables)
    rows = mem._fetch_dicts(["run_id", "dag_name", "status", "params_json", "paused_node_id"], "SELECT run_id, dag_name, status, params_json, paused_node_id FROM dag_runs WHERE run_id = ?", (run_id.strip(),))
    if not rows:
        err = f"Workflow run `{run_id}` not found."
        return {"ok": False, "status": "not_found", "error": err, "summary": err}
    rec = rows[0]
    try:
        params = json.loads(rec["params_json"] or "{}")
    except Exception:
        params = {}
    approved = [approved_node_id or str(rec["paused_node_id"] or "")] if (approved_node_id or rec["status"] == "waiting_approval") else []
    if dag_spec is not None:
        return execute_workflow_spec(dag_spec, params=params, store=mem, bindings=bindings, run_id=rec["run_id"], approved_nodes=approved)
    return execute_workflow_by_name(rec["dag_name"], params=params, store=mem, bindings=bindings, root=root, run_id=rec["run_id"], approved_nodes=approved)


resume_dag_run = resume_workflow_run


def get_dag_run(run_id: str, store: Optional[MemoryStore] = None) -> Optional[Dict[str, Any]]:
    mem = store or get_memory_store()
    mem.ensure_schema("dag_runtime", _init_dag_tables)
    rows = mem._fetch_dicts(
        ["run_id", "dag_name", "status", "params_json", "outputs_json", "paused_node_id", "approval_id", "error", "created_at", "updated_at"],
        "SELECT run_id, dag_name, status, params_json, outputs_json, paused_node_id, approval_id, error, created_at, updated_at FROM dag_runs WHERE run_id = ?",
        (run_id.strip(),),
    )
    if not rows:
        return None
    out = dict(rows[0])
    node_rows = mem._fetch_dicts(
        ["node_id", "status", "attempts", "output_json", "error", "duration_ms", "updated_at"],
        "SELECT node_id, status, attempts, output_json, error, duration_ms, updated_at FROM dag_node_runs WHERE run_id = ? ORDER BY rowid ASC",
        (run_id.strip(),),
    )
    for nr in node_rows:
        try:
            nr["output"] = json.loads(nr.get("output_json") or "{}")
        except Exception:
            nr["output"] = nr.get("output_json")
    out["node_runs"] = node_rows
    return out


def list_dag_runs(store: Optional[MemoryStore] = None, limit: int = 25) -> List[Dict[str, Any]]:
    mem = store or get_memory_store()
    mem.ensure_schema("dag_runtime", _init_dag_tables)
    cap = max(1, min(int(limit or 25), 100))
    return mem._fetch_dicts(
        ["run_id", "dag_name", "status", "paused_node_id", "approval_id", "error", "created_at", "updated_at"],
        "SELECT run_id, dag_name, status, paused_node_id, approval_id, error, created_at, updated_at FROM dag_runs ORDER BY updated_at DESC, rowid DESC LIMIT ?",
        (cap,),
    )


# ---------------------------------------------------------------------------
# 4. ICM Self-Contained Domain Capability Pack Initializer (`data/domains/<pack>/`)
# ---------------------------------------------------------------------------
def ensure_domain_pack(
    pack_name: str,
    description: str,
    *,
    schema_sql: str = "",
    context_md: str = "",
    scripts: Optional[Dict[str, str]] = None,
    workflows: Optional[Dict[str, Union[str, Dict[str, Any]]]] = None,
    domains_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Create or update a self-contained ICM Domain Capability Pack under `data/domains/<pack>/`."""
    slug = re.sub(r"[^a-z0-9]+", "-", (pack_name or "").strip().lower()).strip("-")
    if not slug or not _KEBAB_RE.match(slug):
        return {"ok": False, "error": f"Domain pack name must be kebab-case, got `{pack_name}`."}
    base_dir = ((domains_root or settings.domains_dir) / slug).resolve()
    for folder in ("01_inbox", "02_records", "03_reports", "scripts", "workflows"):
        (base_dir / folder).mkdir(parents=True, exist_ok=True)
    db_path = base_dir / f"{slug}.db"
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if schema_sql.strip():
            conn.executescript(schema_sql)
        conn.commit()
    ctx_file = base_dir / "CONTEXT.md"
    if context_md.strip() or not ctx_file.is_file():
        body = context_md.strip() or (
            f"---\nname: {slug}\ndescription: {description.strip() or f'Domain capability pack for {slug}.'}\n"
            f"required_tools: [read_file, write_file, run_command, ops_manage]\n---\n"
            f"# Domain Pack: {slug}\n\n{description.strip()}\n\n"
            f"## Domain Layout\n"
            f"- SQLite Database: `{db_path}` (WAL mode)\n"
            f"- Stage Folders  : `01_inbox/`, `02_records/`, `03_reports/`\n"
            f"- CLI Scripts    : `scripts/`\n"
            f"- JSON Workflows : `workflows/`\n"
        )
        ctx_file.write_text(body + "\n", encoding="utf-8")
    for sname, sbody in (scripts or {}).items():
        clean_s = Path(sname).name if sname.endswith(".py") else f"{Path(sname).name}.py"
        ok_syn, syn_err = validate_python_syntax(sbody, filename=clean_s)
        if not ok_syn:
            return {"ok": False, "error": f"SyntaxError in script `{clean_s}`: {syn_err}"}
        (base_dir / "scripts" / clean_s).write_text(sbody, encoding="utf-8")
    for wname, wspec in (workflows or {}).items():
        stem = Path(wname).name.removesuffix(".workflow.json").removesuffix(".json")
        wf_file = base_dir / "workflows" / f"{stem}.workflow.json"
        wf_dict = json.loads(wspec) if isinstance(wspec, str) else dict(wspec)
        spec_obj = WorkflowSpec.from_dict(wf_dict, source_path=str(wf_file))
        v_err = spec_obj.validate()
        if v_err:
            return {"ok": False, "error": f"Invalid workflow `{wf_file.name}`: {v_err}"}
        wf_file.write_text(json.dumps(wf_dict, indent=2) + "\n", encoding="utf-8")

    root_ctx = settings.root_context_md_path
    if not root_ctx.is_file():
        root_ctx.parent.mkdir(parents=True, exist_ok=True)
        root_ctx.write_text(
            "# Companion Workspace Context (`data/CONTEXT.md`)\n\n"
            "Self-contained Domain Capability Packs live under `data/domains/<pack>/` (each with `CONTEXT.md`, `<pack>.db`, `scripts/`, and `workflows/`).\n",
            encoding="utf-8",
        )
    invalidate_skills_cache()
    return {
        "ok": True,
        "pack": slug,
        "path": str(base_dir),
        "db_path": str(db_path),
        "context_md": str(ctx_file),
        "summary": f"Initialized Domain Capability Pack `{slug}` at `{base_dir}` (SQLite `{db_path.name}`).",
    }


# ---------------------------------------------------------------------------
# 5. `GoalPlan`, `GoalStep`, `VerificationSpec` & Pluggable Domain Validators
# ---------------------------------------------------------------------------
VALID_VERIFICATION_KINDS = ("evidence_file", "check_command", "script_validator", "domain_validator", "assertion", "human_gate")
DomainValidatorFn = Callable[[str, Dict[str, Any], Path], Tuple[bool, str]]
DOMAIN_VALIDATOR_REGISTRY: Dict[str, DomainValidatorFn] = {}


def register_domain_validator(name: str, fn: DomainValidatorFn) -> None:
    DOMAIN_VALIDATOR_REGISTRY[name.strip().lower()] = fn


def discover_skill_domain_validators(root: Optional[Path] = None) -> Dict[str, Path]:
    base = (root or settings.root).resolve()
    eff_data = settings.effective_data_dir.resolve()
    roots = [base / "skills", eff_data / "skills", eff_data / "domains"]
    found: Dict[str, Path] = {}
    for r in roots:
        if not r.is_dir():
            continue
        for sc in sorted(r.glob("*/scripts/*_validator.py")):
            vname = sc.stem.removesuffix("_validator").lower()
            if vname:
                found[vname] = sc
    return found


def is_node_output_passed(output: str) -> bool:
    clean = (output or "").strip()
    if not clean:
        return False
    fail_prefixes = (
        "error:", "exit=1", "SYNTAX GUARD REJECTED", "SAFETY GUARD REJECTED",
        "SKILL LINTER REJECTED", "WIKI AUTHOR GUARD REJECTED",
        "PHASE TOOL GATE REJECTED", "RISK GATE REJECTED",
    )
    if clean.startswith(fail_prefixes) or re.match(r"^exit=(?!0\b)-?\d+\b", clean):
        return False
    return "ISSUES DETECTED (" not in clean


@dataclass
class VerificationSpec:
    kind: str = "assertion"
    target: str = ""
    contains: str = ""
    description: str = ""
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"kind": self.kind, "target": self.target, "contains": self.contains, "description": self.description}
        if self.params:
            out["params"] = dict(self.params)
        return out

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "VerificationSpec":
        kind = str(raw.get("kind") or "assertion").strip().lower()
        if kind not in VALID_VERIFICATION_KINDS:
            kind = "assertion"
        p_raw = raw.get("params")
        return cls(
            kind=kind, target=str(raw.get("target") or ""), contains=str(raw.get("contains") or ""),
            description=str(raw.get("description") or ""), params=dict(p_raw) if isinstance(p_raw, dict) else {},
        )


@dataclass
class GoalStep:
    step_id: str
    title: str
    depends_on: List[str] = field(default_factory=list)
    risk_tier: str = ActionRiskTier.R1_LOCAL_REVERSIBLE.value
    verification: VerificationSpec = field(default_factory=VerificationSpec)
    status: str = "pending"
    evidence: str = ""
    task_id: Optional[int] = None
    stage_kind: str = "DELIVER_AND_VERIFY"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id, "title": self.title, "depends_on": list(self.depends_on),
            "risk_tier": self.risk_tier, "stage_kind": self.stage_kind, "verification": self.verification.to_dict(),
            "status": self.status, "evidence": self.evidence, "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], idx: int = 1) -> "GoalStep":
        v_raw = raw.get("verification")
        v_spec = VerificationSpec.from_dict(v_raw) if isinstance(v_raw, dict) else VerificationSpec()
        deps = [str(d) for d in raw.get("depends_on", [])] if isinstance(raw.get("depends_on"), list) else []
        risk = str(raw.get("risk_tier") or "R1").upper()
        if risk not in ("R0", "R1", "R2", "R3"):
            risk = "R1"
        return cls(
            step_id=str(raw.get("step_id") or raw.get("id") or f"s{idx}"),
            title=str(raw.get("title") or raw.get("description") or f"Step {idx}"),
            depends_on=deps, risk_tier=risk, stage_kind=str(raw.get("stage_kind") or "DELIVER_AND_VERIFY"),
            verification=v_spec, status=str(raw.get("status") or "pending"), evidence=str(raw.get("evidence") or ""),
            task_id=int(raw["task_id"]) if raw.get("task_id") is not None else None,
        )


@dataclass
class GoalPlan:
    goal: str
    steps: List[GoalStep] = field(default_factory=list)

    def __post_init__(self) -> None:
        seen_ids: set[str] = set()
        for idx, s in enumerate(self.steps, 1):
            base_id = (s.step_id or f"s{idx}").strip() or f"s{idx}"
            cand, suffix = base_id, 2
            while cand in seen_ids:
                cand = f"{base_id}_{suffix}"
                suffix += 1
            s.step_id = cand
            seen_ids.add(cand)
        deps_map: Dict[str, List[str]] = {}
        for s in self.steps:
            clean_deps: List[str] = []
            for dep in s.depends_on:
                if dep == s.step_id or dep not in seen_ids or dep in clean_deps:
                    continue
                stack, visited, forms_cycle = [dep], set(), False
                while stack:
                    cur = stack.pop()
                    if cur == s.step_id:
                        forms_cycle = True
                        break
                    if cur not in visited:
                        visited.add(cur)
                        stack.extend(deps_map.get(cur, ()))
                if not forms_cycle:
                    clean_deps.append(dep)
            s.depends_on = clean_deps
            deps_map[s.step_id] = clean_deps

    def ready_steps(self) -> List[GoalStep]:
        done = {s.step_id for s in self.steps if s.status == "completed"}
        return [s for s in self.steps if s.status in ("pending", "in_progress") and all(d in done for d in s.depends_on)]

    def requires_pause(self, step: GoalStep, r3_approved: bool = False, approved_r3_steps: Optional[List[str]] = None) -> Tuple[bool, str]:
        if step.verification.kind == "human_gate" and step.evidence != "human_approved":
            return True, f"Step `{step.step_id}` ({step.title}) requires human review/alignment (`human_gate`)."
        step_r3_ok = r3_approved or (step.step_id in set(approved_r3_steps or []))
        if step.risk_tier == ActionRiskTier.R3_HIGH_IMPACT_MUTATION.value and not step_r3_ok:
            return True, f"Step `{step.step_id}` ({step.title}) is classified `R3` (high-impact mutation) and requires explicit user approval."
        return False, ""

    def to_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "GoalPlan":
        raw_steps = raw.get("steps") if isinstance(raw.get("steps"), list) else []
        return cls(goal=str(raw.get("goal") or ""), steps=[GoalStep.from_dict(s, i + 1) for i, s in enumerate(raw_steps) if isinstance(s, dict)])


def _validate_structured_file(path: Path, raw_text: Optional[str] = None) -> Optional[str]:
    if raw_text is None:
        try:
            txt = get_execution_target().read_text(path)
        except Exception:
            return f"Missing expected artifact `{path.name}`"
    else:
        txt = raw_text
    ext = path.suffix.lower()
    if ext == ".py":
        ok_syn, syn_err = validate_python_syntax(txt, filename=str(path))
        return None if ok_syn else f"Python SyntaxError in `{path.name}`: {syn_err}"
    if ext == ".json":
        try:
            json.loads(txt)
        except Exception as exc:
            return f"Invalid JSON in `{path.name}`: {exc}"
    elif ext == ".toml":
        try:
            tomllib.loads(txt)
        except Exception as exc:
            return f"Invalid TOML in `{path.name}`: {exc}"
    return None


def _run_domain_validator(spec: VerificationSpec, target_root: Path, step_output: str = "") -> Tuple[bool, str]:
    raw_target = (spec.target or "").strip()
    vname, _, sub_target = raw_target.partition(":")
    vkey = (vname or str(spec.params.get("validator") or "")).strip().lower()
    arg_target = sub_target.strip() or str(spec.params.get("path") or step_output or "")
    eff_params = {**dict(spec.params), "step_output": step_output}
    if not vkey:
        return False, "VerificationSpec `domain_validator` requires `target='<validator_name>:<target>'`."
    if vkey in DOMAIN_VALIDATOR_REGISTRY:
        return DOMAIN_VALIDATOR_REGISTRY[vkey](arg_target, eff_params, target_root)
    discovered = discover_skill_domain_validators()
    if vkey in discovered:
        payload = json.dumps({"target": arg_target, "params": eff_params, "workspace_root": str(target_root)})
        proc = subprocess.run(
            [sys.executable, str(discovered[vkey])],
            input=payload, cwd=str(target_root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        out_txt = (proc.stdout or proc.stderr or "").strip()
        try:
            res_json = json.loads(out_txt)
            if isinstance(res_json, dict):
                return bool(res_json.get("passed", proc.returncode == 0)), str(res_json.get("detail") or out_txt)[:280]
        except Exception:
            pass
        return proc.returncode == 0, out_txt[:280] or f"Domain validator `{vkey}` exited {proc.returncode}."
    return False, f"Domain validator `{vkey}` not registered or found in `skills/` or `data/domains/`."


def evaluate_step_verification(
    spec: VerificationSpec, root: Path, step_output: str = "", changed_paths: Optional[List[Path]] = None,
) -> Tuple[bool, str]:
    target_root = root.resolve()
    for cp in (changed_paths or []):
        if cp.is_file():
            err = _validate_structured_file(cp)
            if err:
                return False, err
    if spec.kind == "human_gate":
        return (True, "Human gate approved.") if "HUMAN_APPROVED" in (step_output or "") else (False, f"HUMAN_GATE_REQUIRED: {spec.description or spec.target or 'Awaiting user confirmation'}")
    if spec.kind == "domain_validator":
        return _run_domain_validator(spec, target_root, step_output=step_output)
    if spec.kind == "evidence_file":
        if spec.target and spec.target != "*":
            norm_t = spec.target.replace("\\", "/").strip("/")
            data_root = settings.effective_data_dir.resolve()
            if target_root == settings.root.resolve() and norm_t.startswith("data/"):
                cand_data = (data_root / norm_t[5:]).resolve()
                p = cand_data if cand_data.exists() else (target_root / norm_t).resolve()
            else:
                p = (Path(spec.target) if Path(spec.target).is_absolute() else (target_root / spec.target)).resolve()
            if not (p.is_relative_to(target_root) or p.is_relative_to(data_root)):
                return False, f"Evidence file path `{spec.target}` escapes workspace root."
            try:
                txt = get_execution_target().read_text(p)
            except Exception:
                txt = ""
            if not txt:
                return False, f"Expected non-empty evidence file `{spec.target}` was not created."
            err = _validate_structured_file(p, raw_text=txt)
            if err:
                return False, err
            if spec.contains and spec.contains not in txt:
                return False, f"Evidence file `{spec.target}` missing expected content `{spec.contains}`."
            return True, f"Evidence file `{spec.target}` verified ({len(txt)} chars)."
        return (True, f"Verified {len(changed_paths)} modified artifact(s).") if changed_paths else (False, "No workspace artifact created for `evidence_file` gate.")

    if spec.kind in ("check_command", "script_validator"):
        cmd = spec.target.strip()
        if not cmd:
            return False, f"`{spec.kind}` requires a non-empty `target` command or script."
        if spec.kind == "script_validator" and cmd.endswith(".py"):
            script_p = (Path(cmd) if Path(cmd).is_absolute() else (target_root / cmd)).resolve()
            if not any(script_p.is_relative_to(r) for r in (target_root, (settings.root / "skills").resolve(), settings.user_skills_dir.resolve(), settings.domains_dir.resolve())):
                return False, f"Validator script `{cmd}` escapes workspace and skills roots."
            if not script_p.is_file():
                return False, f"Validator script `{cmd}` not found."
            run_args: Any = [sys.executable, str(script_p)]
            use_shell = False
        else:
            safe_err = check_command_safety(cmd)
            if safe_err:
                return False, safe_err
            run_args, use_shell = cmd, True
        rc, stdout_s, stderr_s, guard_err = get_execution_target().exec_command(
            run_args, shell=use_shell, timeout=30, extra_env={"PYTHONPATH": str(target_root)}, cwd_override=str(target_root)
        )
        if rc == -1 and guard_err:
            return False, f"Verification command `{cmd}` {guard_err}"
        if guard_err:
            return False, guard_err
        combined = (stdout_s + "\n" + stderr_s).strip()
        if rc != 0:
            return False, f"Verification command `{cmd}` exited {rc}: {combined[:220]}"
        if spec.contains and spec.contains not in combined:
            return False, f"Verification output missing `{spec.contains}`: {combined[:180]}"
        return True, f"Command `{cmd}` passed (exit=0)."

    ok = is_node_output_passed(step_output)
    if ok and spec.contains and spec.contains.lower() not in (step_output or "").lower():
        return False, f"Assertion failed: output missing `{spec.contains}`."
    return (True, "Step assertion & artifact checks clean.") if ok else (False, f"Step output reported error: {(step_output or '')[:180]}")


def _match_step_to_task_id(title: str, pending_tasks: List[Tuple[int, str, str, int]], used_ids: set[int]) -> Optional[int]:
    step_toks = {w for w in re.findall(r"[a-z0-9_./-]{3,}", (title or "").lower()) if w not in ("the", "and", "for", "with", "step", "task")}
    best_id, best_score = None, 0
    for tid, desc, _st, _prio in pending_tasks:
        if tid in used_ids:
            continue
        overlap = len(step_toks & set(re.findall(r"[a-z0-9_./-]{3,}", desc.lower())))
        if overlap > best_score:
            best_score, best_id = overlap, tid
    return best_id if best_score >= 1 else None


def build_goal_plan_from_tasks(goal: str, pending_tasks: List[Tuple[int, str, str, int]], plan_text: str = "") -> GoalPlan:
    for m in _DAG_JSON_BLOCK_RE.finditer(plan_text or ""):
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list) and parsed["steps"]:
                plan = GoalPlan.from_dict({"goal": goal, "steps": parsed["steps"]})
                used_ids: set[int] = {s.task_id for s in plan.steps if s.task_id is not None}
                for idx, step in enumerate(plan.steps):
                    if step.task_id is None:
                        matched = _match_step_to_task_id(step.title, pending_tasks, used_ids)
                        if matched is None and idx < len(pending_tasks) and pending_tasks[idx][0] not in used_ids:
                            matched = pending_tasks[idx][0]
                        if matched is not None:
                            step.task_id = matched
                            used_ids.add(matched)
                return plan
        except Exception:
            continue

    steps: List[GoalStep] = []
    prev_id: Optional[str] = None
    for idx, (tid, desc, _st, _prio) in enumerate(pending_tasks, 1):
        sid = f"s{idx}"
        low = desc.lower()
        f_match = _FILE_TOKEN_RE.search(desc)
        v_spec = (
            VerificationSpec("evidence_file", target=f_match.group(1), description=f"Artifact `{f_match.group(1)}` exists")
            if f_match and any(w in low for w in ("create", "write", "generate", "save", "produce", "export"))
            else VerificationSpec("assertion", description="Step verified cleanly")
        )
        risk = "R3" if any(w in low for w in ("deploy", "remote", "force", "drop ", "delete production")) else "R1"
        steps.append(GoalStep(step_id=sid, title=desc, depends_on=[prev_id] if prev_id else [], risk_tier=risk, verification=v_spec, task_id=int(tid)))
        prev_id = sid
    return GoalPlan(goal=goal, steps=steps)


# ---------------------------------------------------------------------------
# 6. Skill & Capability Pack Forge, Safe Script Runner & Customizations Bus
# ---------------------------------------------------------------------------
_PEP723_BLOCK_RE = re.compile(r"(?m)^# /// script\s*\n((?:^#(?: .*)?\n)*)^# ///\s*$")
_DEP_NAME_RE = re.compile(r"^([A-Za-z0-9_.-]+)")
_PYPI_IMPORT_ALIASES: Dict[str, str] = {
    "beautifulsoup4": "bs4", "pillow": "pil", "pyyaml": "yaml", "scikit-learn": "sklearn", "opencv-python": "cv2",
}


def validate_skill_markdown(content: str, expected_name: Optional[str] = None) -> Optional[str]:
    """Validate a `skills/<name>/SKILL.md` file's YAML frontmatter and basic structure."""
    norm = content.replace("\r\n", "\n").strip()
    if not norm.startswith("---\n"):
        return "SKILL.md must start with YAML frontmatter delimited by `---`."
    end_idx = norm.find("\n---", 4)
    if end_idx == -1:
        return "SKILL.md is missing closing `---` for YAML frontmatter."
    fm_block = norm[4:end_idx]
    meta: Dict[str, str] = {}
    cur_key = ""
    for line in fm_block.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if (line.startswith(" ") or line.startswith("\t")) and cur_key:
            item = s[1:].strip().strip("'\"") if s.startswith("-") else s.strip("'\"")
            meta[cur_key] = f"{meta[cur_key]}, {item}" if (meta[cur_key] and s.startswith("-")) else f"{meta[cur_key]} {item}".strip()
        elif ":" in line:
            k, v = line.split(":", 1)
            cur_key = k.strip().lower()
            val = v.strip().strip("'\"")
            meta[cur_key] = "" if val in (">", "|", ">-", "|-") else val
    name = meta.get("name", "")
    if not name or not _KEBAB_RE.match(name):
        return f"SKILL.md frontmatter `name` must be kebab-case (`[a-z0-9-]+`), got `{name}`."
    if expected_name and _KEBAB_RE.match(expected_name) and name != expected_name:
        return f"SKILL.md frontmatter `name: {name}` does not match folder name `{expected_name}`."
    if len(meta.get("description", "")) < 10:
        return "SKILL.md frontmatter must include a concise `description` (>= 10 chars)."
    return None


def parse_pep723_dependencies(source: str) -> Tuple[List[str], Set[str], Optional[str]]:
    """Parse PEP 723 `# /// script` metadata, returning `(raw_deps, allowed_import_roots, error)`."""
    match = _PEP723_BLOCK_RE.search((source or "").replace("\r\n", "\n"))
    if not match:
        return [], set(), None
    uncommented = "\n".join(line[2:] if line.startswith("# ") else line[1:] for line in match.group(1).splitlines())
    try:
        meta = tomllib.loads(uncommented)
    except Exception as exc:
        return [], set(), f"Invalid PEP 723 `# /// script` TOML metadata: {exc}"
    deps = meta.get("dependencies", [])
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        return [], set(), "PEP 723 `dependencies` must be a list of package requirement strings."
    raw_deps: List[str] = []
    allowed_imports: Set[str] = set()
    for req in deps:
        clean_req = req.strip()
        if not clean_req:
            continue
        raw_deps.append(clean_req)
        m = _DEP_NAME_RE.match(clean_req)
        if m:
            pkg = m.group(1).lower()
            norm = pkg.replace("-", "_").replace(".", "_")
            allowed_imports.update({pkg, norm})
            if pkg in _PYPI_IMPORT_ALIASES:
                allowed_imports.add(_PYPI_IMPORT_ALIASES[pkg])
    return raw_deps, allowed_imports, None


def _check_wiki_author_guard(md_path: Path) -> Optional[str]:
    if not md_path.is_file():
        return None
    head = md_path.read_text(encoding="utf-8", errors="replace")[:600]
    if re.search(r"(?im)^author\s*:\s*['\"]?user['\"]?\s*$", head):
        return f"WIKI AUTHOR GUARD REJECTED: `{md_path.name}` has `author: user` and cannot be overwritten automatically."
    return None


def check_make_vs_borrow(capability_query: str, root: Optional[Path] = None) -> Dict[str, Any]:
    """Check existing `skills/` and `KnowledgePort` before forging a new procedural capability."""
    target_root = (root or settings.root).resolve()
    skills = discover_skills(target_root)
    matched_skills = rank_relevant_skills(capability_query, skills, top_k=3)
    known_facts = get_knowledge_port().search(capability_query, limit=4)
    rec = "BORROW" if matched_skills else "FORGE"
    s_names = [s["name"] for s in matched_skills]
    summary = (
        f"Make-vs-Borrow Check: `{rec}` — existing relevant skills: {s_names or 'none'}; "
        f"grounded facts: {[f.key for f in known_facts] or 'none'}."
    )
    return {"recommendation": rec, "existing_skills": matched_skills, "known_facts": [f.key for f in known_facts], "summary": summary}


def _lint_staged_python(fname: str, code: str, kind: str) -> Optional[str]:
    valid, syn_err = validate_python_syntax(code, filename=fname)
    if not valid:
        return f"{kind} `{fname}` SyntaxError: {syn_err}"
    _raw_deps, _allowed_imports, pep_err = parse_pep723_dependencies(code)
    if pep_err:
        return f"{kind} `{fname}` {pep_err}"
    if kind == "Script" and "--self-test" not in code:
        return f"Script `{fname}` must implement a `--self-test` CLI entrypoint."
    return None


def build_skill_script_command(
    sc_path: Path,
    extra_args: List[str],
    source: Optional[str] = None,
) -> Tuple[Optional[List[str]], Optional[str]]:
    """Resolve `sys.executable` for pure-stdlib scripts or isolated `uv run` for PEP 723 skill scripts."""
    code = source if source is not None else sc_path.read_text(encoding="utf-8", errors="replace")
    raw_deps, _allowed, pep_err = parse_pep723_dependencies(code)
    if pep_err:
        return None, pep_err
    if not raw_deps:
        return [sys.executable, str(sc_path), *extra_args], None
    uv_bin = shutil.which("uv")
    if not uv_bin:
        return None, (
            f"PEP 723 skill script `{sc_path.name}` declares isolated packages {raw_deps}, "
            "which requires `uv` on PATH (`winget install astral-sh.uv`). Core Python remains untouched."
        )
    return [uv_bin, "run", "--quiet", str(sc_path), *extra_args], None


def _populate_staged_skill_dir(
    stage_skill: Path,
    skill_md: str,
    references: Optional[Dict[str, str]],
    dags: Optional[Dict[str, Any]],
    scripts: Optional[Dict[str, str]],
) -> Tuple[Optional[str], List[Path], List[Path]]:
    stage_skill.mkdir(parents=True, exist_ok=True)
    (stage_skill / "SKILL.md").write_text((skill_md or "").replace("\r\n", "\n"), encoding="utf-8", newline="\n")

    for fname, content in (references or {}).items():
        clean_f = Path(fname).name
        if not clean_f.endswith(".md"):
            return f"SKILL FORGE REJECTED: reference `{fname}` must end with `.md`.", [], []
        ref_d = stage_skill / "references"
        ref_d.mkdir(exist_ok=True)
        (ref_d / clean_f).write_text(str(content).replace("\r\n", "\n"), encoding="utf-8", newline="\n")

    staged_scripts: List[Path] = []
    for fname, content in (scripts or {}).items():
        clean_f = Path(fname).name
        if not clean_f.endswith(".py"):
            return f"SKILL FORGE REJECTED: script `{fname}` must end with `.py`.", [], []
        norm_code = str(content).replace("\r\n", "\n")
        lint_err = _lint_staged_python(clean_f, norm_code, "Script")
        if lint_err:
            return f"SKILL FORGE REJECTED (`scripts/{clean_f}`): {lint_err}", [], []
        sc_d = stage_skill / "scripts"
        sc_d.mkdir(exist_ok=True)
        sc_path = sc_d / clean_f
        sc_path.write_text(norm_code, encoding="utf-8", newline="\n")
        staged_scripts.append(sc_path)

    staged_dags: List[Path] = []
    for fname, content in (dags or {}).items():
        clean_f = Path(fname).name
        if clean_f.endswith(".py"):
            return (
                f"SKILL FORGE REJECTED (`{clean_f}`): Python-lambda `.py` DAG files are retired. "
                "Author a declarative JSON workflow (`<name>.workflow.json`) using `run_script`, `call_tool`, `llm_step`, `mcp_step`, or `human_gate`.",
                [],
                [],
            )
        stem = clean_f.removesuffix(".workflow.json").removesuffix(".json")
        out_name = f"{stem}.workflow.json"
        try:
            wf_obj = dict(content) if isinstance(content, dict) else json.loads(str(content))
            wf_spec = WorkflowSpec.from_dict(wf_obj)
            val_err = wf_spec.validate()
        except Exception as exc:
            return f"SKILL FORGE REJECTED (`workflows/{out_name}`): invalid declarative workflow JSON ({exc}).", [], []
        if val_err:
            return f"SKILL FORGE REJECTED (`workflows/{out_name}`): {val_err}", [], []
        wf_d = stage_skill / "workflows"
        wf_d.mkdir(exist_ok=True)
        wf_p = wf_d / out_name
        wf_p.write_text(json.dumps(wf_obj, indent=2) + "\n", encoding="utf-8", newline="\n")
        staged_dags.append(wf_p)
    return None, staged_dags, staged_scripts


def _run_staged_self_tests(staged_dags: List[Path], staged_scripts: List[Path], sandbox_dir: Path) -> Optional[str]:
    for dag_path in staged_dags:
        dag_res = self_test_dag(dag_path)
        if not dag_res.get("ok"):
            return f"SKILL FORGE REJECTED (`workflows/{dag_path.name}`): {dag_res.get('error', 'self-test failed')}"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    for sc_path in staged_scripts:
        cmd, cmd_err = build_skill_script_command(sc_path, ["--self-test"])
        if cmd_err or cmd is None:
            return f"SKILL FORGE REJECTED (`{sc_path.name}`): {cmd_err}"
        try:
            proc = subprocess.run(
                cmd, cwd=str(sandbox_dir), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
            )
        except Exception as exc:
            return f"SKILL FORGE REJECTED (`{sc_path.name} --self-test` crashed/timed out): {exc}"
        if proc.returncode != 0:
            out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[:260]
            return f"SKILL FORGE REJECTED (`{sc_path.name} --self-test` exited {proc.returncode}): {out}"
    return None


def stage_and_promote_skill(
    name: str,
    skill_md: str,
    scripts: Optional[Dict[str, str]] = None,
    dags: Optional[Dict[str, Any]] = None,
    references: Optional[Dict[str, str]] = None,
    self_test: bool = True,
    overwrite: bool = False,
    root: Optional[Path] = None,
    scope: str = "core",
    workflows: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Stage, lint, `--self-test`, checkpoint, and atomically promote a Capability Pack (`R2`)."""
    clean_name = (name or "").strip().lower()
    if not clean_name or not _KEBAB_RE.match(clean_name):
        msg = f"SKILL FORGE REJECTED: skill name `{name}` must be kebab-case (`[a-z0-9-]+`)."
        return {"promoted": False, "error": msg, "summary": msg}

    eff_workflows = {**(dags or {}), **(workflows or {})}
    eff_scope = (scope or "core").strip().lower()
    target_root = (root or settings.root).resolve()
    is_user_scope = eff_scope == "user"
    if not is_user_scope and target_root == settings.root.resolve():
        msg = (
            "SAFETY GUARD REJECTED: Companion's installed `app/` (`skills/`) is permanently IMMUTABLE (`ADR-019`). "
            "Pass `scope='user'` to save an update-safe skill inside `data/skills/`, or promote into an external Project clone (`workspace_path`)."
        )
        return {"promoted": False, "error": msg, "summary": msg}
    skills_parent = (settings.user_skills_dir if root is None else (target_root / "data" / "skills")) if is_user_scope else (target_root / "skills")
    dest_dir = skills_parent.resolve() / clean_name
    if dest_dir.exists():
        if not overwrite:
            msg = f"SKILL FORGE REJECTED: `skills/{clean_name}` already exists (pass `overwrite=True` to update)."
            return {"promoted": False, "error": msg, "summary": msg}
        for existing_md in sorted(dest_dir.glob("references/*.md")):
            guard_err = _check_wiki_author_guard(existing_md)
            if guard_err:
                return {"promoted": False, "error": guard_err, "summary": guard_err}

    md_err = validate_skill_markdown(skill_md or "", expected_name=clean_name)
    if md_err:
        msg = f"SKILL FORGE REJECTED (`SKILL.md`): {md_err}"
        return {"promoted": False, "error": msg, "summary": msg}

    with tempfile.TemporaryDirectory() as tmp:
        stage_root = Path(tmp) / "stage_repo"
        stage_skill = stage_root / "skills" / clean_name
        pop_err, staged_dags, staged_scripts = _populate_staged_skill_dir(stage_skill, skill_md, references, eff_workflows, scripts)
        if pop_err:
            return {"promoted": False, "error": pop_err, "summary": pop_err}

        if self_test and (staged_dags or staged_scripts):
            test_err = _run_staged_self_tests(staged_dags, staged_scripts, Path(tmp) / "self_test_sandbox")
            if test_err:
                return {"promoted": False, "error": test_err, "summary": test_err}

        cp = {"checkpoint_id": "user_data_skill"} if is_user_scope else (checkpoint_manager if target_root == settings.root.resolve() else CheckpointManager(root=target_root, store=False)).create_checkpoint(label=f"skill_forge_{clean_name}")
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.copytree(stage_skill, dest_dir)
        invalidate_skills_cache()

    rel_lbl = f"{'data/skills' if is_user_scope else 'skills'}/{clean_name}"
    if root is None or target_root == settings.root.resolve():
        if "unittest" not in sys.modules or has_memory_store_override():
            get_knowledge_port().propose_update(
                key=f"skill:{clean_name}",
                value=f"Promoted verified skill `{rel_lbl}/SKILL.md` (scripts={list((scripts or {}).keys())}, workflows={list(eff_workflows.keys())}).",
                project_id="global",
                source="skill_forge",
                confidence=1.0,
            )
    telemetry.log("INFO", "workflows", f"Promoted verified skill `{rel_lbl}` (cp={cp['checkpoint_id']})")
    summary = (
        f"Promoted verified skill `{rel_lbl}/` (checkpoint `{cp['checkpoint_id']}`; "
        f"scripts={len(scripts or {})}, workflows={len(eff_workflows)}, references={len(references or {})})."
    )
    return {"promoted": True, "skill": clean_name, "scope": "user" if is_user_scope else "core", "path": str(dest_dir), "checkpoint_id": cp["checkpoint_id"], "summary": summary}


def run_skill_script(
    skill_name: str,
    script_name: str,
    script_args: Optional[List[str]] = None,
    root: Optional[Path] = None,
    timeout: int = 30,
) -> str:
    """Execute a promoted `skills/<skill>/scripts/<script>.py` or `data/domains/<pack>/scripts/<script>.py`."""
    s_clean = (skill_name or "").strip().lower()
    f_clean = Path((script_name or "").strip()).name
    if not s_clean or not f_clean:
        return "error: skill and script name are required for run_script"
    if not _KEBAB_RE.match(s_clean):
        return f"error: invalid skill name '{s_clean}' (must be kebab-case)"
    if not f_clean.endswith(".py"):
        f_clean += ".py"
    base_root = (root or settings.root).resolve()
    cand_roots = (
        [settings.domains_dir.resolve(), settings.user_skills_dir.resolve(), (base_root / "skills").resolve()]
        if root is None or base_root == settings.root.resolve()
        else [(base_root / "data" / "domains").resolve(), (base_root / "data" / "skills").resolve(), (base_root / "skills").resolve()]
    )
    sc_path = next(
        (
            (sr / s_clean / "scripts" / f_clean).resolve()
            for sr in cand_roots
            if (sr / s_clean / "scripts" / f_clean).resolve().is_relative_to(sr)
            and (sr / s_clean / "scripts" / f_clean).is_file()
        ),
        None,
    )
    if sc_path is None:
        return f"error: skill script not found: `skills/{s_clean}/scripts/{f_clean}`"

    cmd, cmd_err = build_skill_script_command(sc_path, [str(a) for a in (script_args or [])])
    if cmd_err or cmd is None:
        return f"error: {cmd_err}"
    rc, stdout, stderr, guard_err = get_execution_target().exec_command(cmd, shell=False, timeout=timeout)
    if rc == -1 and guard_err:
        return f"error: skill script `{s_clean}/{f_clean}` {guard_err}"
    if guard_err:
        return f"exit=1\n{redact_secrets(guard_err)}"
    raw_out = (stdout + (("\n[stderr]\n" + stderr) if stderr else "")).strip() or "(no output)"
    out = redact_secrets(raw_out)
    if "unittest" not in sys.modules or has_memory_store_override():
        out = get_vault_manager().scrub_output(out)
    return f"exit={rc}\n{out[: settings.out_limit] + chr(10) + '... [truncated]' if len(out) > settings.out_limit else out}"


def _summarize_artifact_structure(path: Path) -> Optional[str]:
    suf = path.suffix.lower()
    if suf not in (".json", ".toml", ".py"):
        return None
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
        if suf == ".json":
            obj = json.loads(txt)
            return f"JSON keys={list(obj.keys())[:10]}" if isinstance(obj, dict) else f"JSON {type(obj).__name__}"
        if suf == ".toml":
            obj = tomllib.loads(txt)
            return f"TOML sections={list(obj.keys())[:10]}"
        tree = ast.parse(txt, filename=path.name)
        syms = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        return f"Python symbols={syms[:10]}" if syms else f"Python module ({len(txt.splitlines())}L)"
    except Exception:
        return None


def distill_workflow_reflection(
    goal: str,
    wf_id: Optional[int],
    changed_paths: List[Path],
    target_root: Path,
    store: MemoryStore,
) -> Dict[str, Any]:
    """Persist verified deliverable facts into `KnowledgePort` post-verification."""
    rels: List[str] = []
    artifact_facts: List[Tuple[str, str]] = []
    for p in changed_paths:
        if p.is_file():
            try:
                rel = str(p.resolve().relative_to(target_root.resolve())).replace("\\", "/")
            except ValueError:
                rel = p.name
            rels.append(rel)
            if len(artifact_facts) < 5:
                struct = _summarize_artifact_structure(p)
                if struct:
                    artifact_facts.append((f"artifact:{rel}", f"Verified artifact `{rel}` ({struct})."))
    pid = SELF_PROJECT_ID if target_root.resolve() == settings.root.resolve() else settings.get_workspace().project_id
    port = get_knowledge_port(store=store)
    key = f"workflow:{wf_id or 'latest'}:deliverables"
    val = f"Goal `{goal[:100]}` verified" + (f" -> artifacts: {', '.join(rels[:10])}" if rels else " (zero file mutations needed).")
    port.propose_update(key=key, value=val, project_id=pid, source="reflect", confidence=1.0)
    for a_key, a_val in artifact_facts:
        port.propose_update(key=a_key, value=a_val, project_id=pid, source="reflect", confidence=0.95)
    return {"fact_key": key, "artifacts_recorded": rels[:10], "artifact_facts": [k for k, _ in artifact_facts]}


def execute_customizations_action(p: Dict[str, Any], _actor: str = "user", store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """CoreAction handler for Customizations (`skills/`, `data/skills/`, `data/domains/`, declarative `workflows`, and `dag_runs`)."""
    mem = store or get_memory_store()
    sub = str(p.get("subaction") or p.get("action") or "list").strip().lower()
    if sub in ("read_skill", "inspect_skill"):
        s_clean = str(p.get("skill") or p.get("name") or "").strip().lower()
        req_asset = str(p.get("asset") or "").strip().lstrip("/\\").replace("\\", "/")
        roots = [settings.domains_dir.resolve(), settings.user_skills_dir.resolve(), (settings.root / "skills").resolve()]
        sdir = next(
            (
                (r / s_clean).resolve()
                for r in roots
                if s_clean
                and ((r / s_clean / "SKILL.md").is_file() or (r / s_clean / "CONTEXT.md").is_file())
                and (r / s_clean).resolve().is_relative_to(r)
            ),
            None,
        )
        if sdir is None:
            return {"ok": False, "error": f"Skill or Domain Capability Pack `{s_clean}` not found."}
        entry_doc = "SKILL.md" if (sdir / "SKILL.md").is_file() else "CONTEXT.md"
        asset = req_asset if req_asset and (sdir / req_asset).is_file() else entry_doc
        target = (sdir / asset).resolve()
        if not target.is_relative_to(sdir) or not target.is_file():
            return {"ok": False, "error": f"Asset `{req_asset or asset}` not found inside `{s_clean}`."}
        files = [
            entry_doc,
            *[
                f"{sub_d}/{f.name}"
                for sub_d in ("references", "workflows", "dags", "scripts", "01_inbox", "02_records", "03_reports")
                if (sdir / sub_d).is_dir()
                for f in sorted((sdir / sub_d).iterdir())
                if f.is_file() and f.suffix.lower() in (".md", ".py", ".json", ".sql", ".csv", ".txt")
            ],
        ]
        return {
            "ok": True, "skill": s_clean, "asset": asset, "files": files,
            "content": target.read_text(encoding="utf-8", errors="replace"),
            "summary": f"Loaded `{s_clean}/{asset}`.",
        }
    if sub == "get_dag_run":
        rec = get_dag_run(str(p.get("run_id") or ""), store=mem)
        return {"ok": rec is not None, "dag_run": rec, "run": rec, "node_runs": (rec or {}).get("node_runs", []), "error": "" if rec else "Workflow run not found."}
    if sub in ("run_dag", "resume_dag"):
        from companion.agent import ensure_dag_runtime_bound

        ensure_dag_runtime_bound()
        if sub == "resume_dag":
            return resume_dag_run(str(p.get("run_id") or ""), store=mem)
        raw_params = p.get("params")
        params = raw_params if isinstance(raw_params, dict) else (json.loads(raw_params) if isinstance(raw_params, str) and raw_params.strip().startswith("{") else {})
        return execute_dag_by_name(str(p.get("dag") or p.get("dag_name") or ""), params=params, store=mem)
    skills = discover_skills(settings.root)
    dags = discover_dags(settings.root)
    runs = list_dag_runs(store=mem, limit=int(p.get("limit") or 20))
    return {
        "ok": True, "skills": skills, "dags": dags, "dag_runs": runs,
        "summary": f"Customizations: {len(skills)} Capability Pack(s), {len(dags)} Declarative Workflow(s), {len(runs)} recent run(s).",
    }
