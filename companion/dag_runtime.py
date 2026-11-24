"""
Code-Controlled Deterministic DAG Runtime (`companion/dag_runtime.py` — Workflow Layer).

Pure-stdlib Python DAG engine replacing static JSON recipes with verifiable code-controlled
`DAGSpec` graphs (`skills/*/dags/*.py` and `data/skills/*/dags/*.py`):
- Node kinds: `python_step`, `tool_or_script_step`, `mcp_step`, `bounded_llm_step`, `human_gate`
- Typed `input_schema` / `output_schema` contract validation at every node boundary
- Topological execution with conditional predicates and dynamic `foreach` fan-out / fan-in joins
- Per-node retry, backoff, and timeout enforcement
- SQLite step-level checkpointing (`dag_runs`, `dag_node_runs`) with mid-DAG `R3` / `human_gate`
  approval pause (`waiting_approval`) and exact-node resume (`resume_dag_run`)
"""

import concurrent.futures
import importlib.util
import inspect
import json
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from companion.approvals import get_approval_queue
from companion.config import settings
from companion.memory_store import MemoryStore, get_memory_store
from companion.telemetry import telemetry

VALID_DAG_NODE_KINDS = ("python_step", "tool_or_script_step", "mcp_step", "bounded_llm_step", "human_gate")
__all__ = [
    "DAGNodeSpec", "DAGRuntimeBindings", "DAGSpec", "VALID_DAG_NODE_KINDS", "dag_node",
    "discover_dags", "execute_dag_by_name", "execute_dag_spec", "get_dag_run", "list_dag_runs",
    "register_dag_runtime_bindings", "resume_dag_run", "validate_dag_spec",
]
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TYPE_MAP: Dict[str, Tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
    "list": (list,),
    "dict": (dict,),
    "any": (object,),
}


@dataclass
class DAGRuntimeBindings:
    """Dependency-inverted Application bindings so Workflow layer never imports upward."""

    tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None
    mcp_fn: Optional[Callable[[str, str, Dict[str, Any]], Dict[str, Any]]] = None
    llm_fn: Optional[Callable[[str, str, str], str]] = None


_DEFAULT_BINDINGS = DAGRuntimeBindings()


def register_dag_runtime_bindings(
    tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None,
    mcp_fn: Optional[Callable[[str, str, Dict[str, Any]], Dict[str, Any]]] = None,
    llm_fn: Optional[Callable[[str, str, str], str]] = None,
) -> None:
    """Register Application-layer tool, MCP, and bounded-LLM executors for `dag_runtime`."""
    if tool_fn is not None:
        _DEFAULT_BINDINGS.tool_fn = tool_fn
    if mcp_fn is not None:
        _DEFAULT_BINDINGS.mcp_fn = mcp_fn
    if llm_fn is not None:
        _DEFAULT_BINDINGS.llm_fn = llm_fn


def validate_typed_payload(payload: Any, schema: Dict[str, str], label: str) -> Optional[str]:
    """Validate `payload` dict against a lightweight type contract like `{'items': 'list', 'note?': 'str'}`."""
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


@dataclass
class DAGNode:
    """Single deterministic node inside a `DAGSpec`."""

    node_id: str
    kind: str = "python_step"
    depends_on: List[str] = field(default_factory=list)
    input_schema: Dict[str, str] = field(default_factory=dict)
    output_schema: Dict[str, str] = field(default_factory=dict)
    fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    tool_name: str = ""
    args_builder: Optional[Union[Dict[str, Any], Callable[[Dict[str, Any]], Dict[str, Any]]]] = None
    server_id: str = ""
    prompt_template: str = ""
    system_prompt: str = "Return concise, valid JSON matching the requested schema."
    tier: str = "fast"
    foreach: str = ""
    condition: Optional[Union[str, Callable[[Dict[str, Any]], bool]]] = None
    risk_tier: str = "R1"
    retries: int = 0
    retry_backoff_ms: int = 25
    timeout_sec: float = 60.0
    description: str = ""


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
) -> Callable[[Callable[[Dict[str, Any]], Dict[str, Any]]], DAGNode]:
    """Decorator turning a pure Python function `(ctx) -> dict` into a typed `DAGNode`."""
    def _wrap(func: Callable[[Dict[str, Any]], Dict[str, Any]]) -> DAGNode:
        return DAGNode(
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


def _norm_schema(sch: Any) -> Dict[str, str]:
    if not isinstance(sch, dict):
        return {}
    return {str(k): (v.__name__ if isinstance(v, type) else str(v)) for k, v in sch.items()}


@dataclass
class DAGSpec:
    """Verified directed acyclic graph definition with topological ordering and dry-run checks."""

    name: str
    description: str = ""
    nodes: List[DAGNode] = field(default_factory=list)
    input_schema: Dict[str, str] = field(default_factory=dict)
    self_test_fn: Optional[Callable[[], bool]] = None

    def __post_init__(self) -> None:
        self.input_schema = _norm_schema(self.input_schema)
        if not self.description and self.name:
            self.description = f"Verified Python DAG `{self.name}`."
        coerced: List[DAGNode] = []
        valid_fields = set(DAGNode.__dataclass_fields__)
        kind_alias = {"python": "python_step", "tool": "tool_or_script_step", "script": "tool_or_script_step", "mcp": "mcp_step", "llm": "bounded_llm_step"}
        for nd in list(self.nodes or []):
            if isinstance(nd, dict):
                d = dict(nd)
                if "self_test_fn" in d and self.self_test_fn is None:
                    self.self_test_fn = d.pop("self_test_fn")
                nid = d.get("node_id") or d.get("id") or d.get("name") or ""
                raw_k = str(d.get("kind") or d.get("type") or "python_step")
                kw = {k: v for k, v in d.items() if k in valid_fields}
                kw["node_id"] = nid
                kw["kind"] = kind_alias.get(raw_k, raw_k)
                if "func" in d and "fn" not in kw:
                    kw["fn"] = d["func"]
                kw["input_schema"] = _norm_schema(kw.get("input_schema"))
                kw["output_schema"] = _norm_schema(kw.get("output_schema"))
                coerced.append(DAGNode(**kw))
            else:
                nd.input_schema = _norm_schema(nd.input_schema)
                nd.output_schema = _norm_schema(nd.output_schema)
                coerced.append(nd)
        self.nodes = coerced

    def validate(self) -> Optional[str]:
        if not self.name or not _KEBAB_RE.match(self.name):
            return f"DAG name must be kebab-case (`[a-z0-9-]+`), got `{self.name}`."
        if len((self.description or "").strip()) < 10:
            return f"DAG `{self.name}` must include a description (>= 10 chars)."
        if not self.nodes:
            return f"DAG `{self.name}` must define at least one node."
        by_id: Dict[str, DAGNode] = {}
        for nd in self.nodes:
            nid = (nd.node_id or "").strip()
            if not nid or nid in by_id:
                return f"DAG `{self.name}` has empty or duplicate node_id `{nid}`."
            if nd.kind not in VALID_DAG_NODE_KINDS:
                return f"Node `{nid}` has invalid kind `{nd.kind}` (valid: {VALID_DAG_NODE_KINDS})."
            if nd.kind == "python_step" and nd.fn is None:
                return f"Python node `{nid}` must bind `fn`."
            if nd.kind in ("tool_or_script_step", "mcp_step") and not nd.tool_name.strip():
                return f"Node `{nid}` (`{nd.kind}`) must specify `tool_name`."
            if nd.kind == "bounded_llm_step" and not nd.prompt_template.strip():
                return f"Node `{nid}` (`bounded_llm_step`) must specify `prompt_template`."
            for schema_lbl, sch in (("input_schema", nd.input_schema), ("output_schema", nd.output_schema)):
                for rk, rt in sch.items():
                    if rt.strip().lower() not in _TYPE_MAP:
                        return f"Node `{nid}` {schema_lbl} key `{rk}` uses unknown type `{rt}`."
            by_id[nid] = nd
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

    def topological_order(self) -> List[DAGNode]:
        by_id = {n.node_id: n for n in self.nodes}
        in_deg = {n.node_id: len(n.depends_on) for n in self.nodes}
        children: Dict[str, List[str]] = {n.node_id: [] for n in self.nodes}
        for n in self.nodes:
            for dep in n.depends_on:
                children[dep].append(n.node_id)
        ready = [n.node_id for n in self.nodes if in_deg[n.node_id] == 0]
        ordered: List[DAGNode] = []
        while ready:
            cur = ready.pop(0)
            ordered.append(by_id[cur])
            for ch in children[cur]:
                in_deg[ch] -= 1
                if in_deg[ch] == 0:
                    ready.append(ch)
        if len(ordered) != len(self.nodes):
            raise ValueError(f"DAG `{self.name}` contains a dependency cycle.")
        return ordered


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
    cur: Any = ctx["params"] if parts[0] == "params" else ctx["nodes"].get(parts[0])
    for part in parts[1:]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _render_template(tpl: str, ctx: Dict[str, Any]) -> str:
    def _sub(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        if expr == "item":
            val = ctx.get("item")
        else:
            val = _resolve_dotted_path(ctx, expr)
            if val is None and expr in ctx.get("params", {}):
                val = ctx["params"][expr]
        return json.dumps(val) if isinstance(val, (dict, list)) else str(val if val is not None else "")
    rendered = re.sub(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}", _sub, tpl)
    if "{" in rendered and "}" in rendered:
        try:
            rendered = rendered.format_map(ctx)
        except Exception:
            pass
    return rendered


def _eval_node_condition(cond: Optional[Union[str, Callable[[Dict[str, Any]], bool]]], ctx: Dict[str, Any]) -> bool:
    if cond is None:
        return True
    if callable(cond):
        return bool(cond(ctx))
    clean = str(cond).strip()
    if not clean or clean == "always":
        return True
    neg = clean.startswith("!")
    path = clean[1:].strip() if neg else clean
    val = _resolve_dotted_path(ctx, path)
    res = bool(val)
    return (not res) if neg else res


def _parse_llm_json_or_wrap(raw_text: str) -> Dict[str, Any]:
    text = (raw_text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    brace = re.search(r"(\{.*\})", text, re.S)
    if brace:
        try:
            parsed = json.loads(brace.group(1))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {"text": text, "ok": bool(text)}


def _invoke_single_unit(node: DAGNode, unit_ctx: Dict[str, Any], bindings: DAGRuntimeBindings) -> Dict[str, Any]:
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
    elif node.kind == "tool_or_script_step":
        if bindings.tool_fn is None:
            raise RuntimeError("No tool executor bound to DAG runtime.")
        raw_args = node.args_builder(unit_ctx) if callable(node.args_builder) else dict(node.args_builder or {})
        rendered = {k: (_render_template(v, unit_ctx) if isinstance(v, str) else v) for k, v in raw_args.items()}
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
            raise RuntimeError("No MCP executor bound to DAG runtime.")
        raw_args = node.args_builder(unit_ctx) if callable(node.args_builder) else dict(node.args_builder or {})
        rendered = {k: (_render_template(v, unit_ctx) if isinstance(v, str) else v) for k, v in raw_args.items()}
        out = bindings.mcp_fn(node.server_id, node.tool_name, rendered)
        if not isinstance(out, dict) or out.get("ok") is False:
            raise RuntimeError(f"MCP step `{node.node_id}` failed: {out}")
    elif node.kind == "bounded_llm_step":
        if bindings.llm_fn is None:
            raise RuntimeError("No bounded LLM executor bound to DAG runtime.")
        prompt = _render_template(node.prompt_template, unit_ctx)
        raw_reply = bindings.llm_fn(prompt, node.system_prompt, node.tier)
        out = _parse_llm_json_or_wrap(raw_reply)
    else:
        raise ValueError(f"Unsupported node kind `{node.kind}`.")
    out_err = validate_typed_payload(out, node.output_schema, f"Node `{node.node_id}` output")
    if out_err:
        raise ValueError(out_err)
    return out


def _execute_node_with_retries(node: DAGNode, ctx: Dict[str, Any], bindings: DAGRuntimeBindings) -> Tuple[Dict[str, Any], int]:
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
                        fut = pool.submit(_invoke_single_unit, node, sub_ctx, bindings)
                        results.append(fut.result(timeout=max(0.5, float(node.timeout_sec))))
                    return {"items": results, "count": len(results)}, attempt
                merged_in = {**ctx["params"], **{k: v for dep in node.depends_on for k, v in (ctx["nodes"].get(dep) or {}).items() if isinstance(ctx["nodes"].get(dep), dict)}}
                top_in = {k: v for k, v in merged_in.items() if k not in ("params", "nodes", "run_id", "input", "item", "item_index")}
                unit_ctx = {**top_in, **ctx, "input": merged_in}
                fut = pool.submit(_invoke_single_unit, node, unit_ctx, bindings)
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


def _load_and_init_dag_run(mem: MemoryStore, rid: str, dag_name: str, eff_params: Dict[str, Any]) -> tuple[Dict[str, Dict[str, Any]], set[str]]:
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
            "INSERT INTO dag_runs (run_id, dag_name, status, params_json, outputs_json, paused_node_id, error) VALUES (?, ?, 'running', ?, ?, '', '') "
            "ON CONFLICT(run_id) DO UPDATE SET status = 'running', paused_node_id = '', error = '', updated_at = CURRENT_TIMESTAMP",
            (rid, dag_name, json.dumps(eff_params), json.dumps(cached_nodes)),
        )
        mem.conn.commit()
    return cached_nodes, completed_ids


def execute_dag_spec(
    dag: DAGSpec, params: Optional[Dict[str, Any]] = None, *, store: Optional[MemoryStore] = None,
    bindings: Optional[DAGRuntimeBindings] = None, run_id: Optional[str] = None, approved_nodes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute a `DAGSpec` with SQLite step checkpointing, `foreach` fan-out/fan-in, and `R3` pause/resume."""
    val_err = dag.validate()
    if val_err:
        return {"ok": False, "status": "invalid_dag", "error": val_err, "summary": f"DAG `{dag.name}` validation failed: {val_err}"}
    eff_params = dict(params or {})
    p_err = validate_typed_payload(eff_params, dag.input_schema, f"DAG `{dag.name}` params")
    if p_err:
        return {"ok": False, "status": "invalid_params", "error": p_err, "summary": f"DAG `{dag.name}` input error: {p_err}"}

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
                summary=f"Approve mid-DAG gate `{node.node_id}` in `{dag.name}` (`{rid}`)",
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
                return {"ok": True, "status": "waiting_approval", "run_id": rid, "dag_name": dag.name, "paused_node_id": node.node_id, "approval_id": apr["approval_id"], "nodes": ctx["nodes"], "summary": f"DAG `{dag.name}` (`{rid}`) paused at `{node.node_id}` awaiting approval (`{apr['approval_id']}`)."}
        t0 = time.perf_counter()
        try:
            out, attempts = _execute_node_with_retries(node, ctx, eff_bindings)
        except Exception as exc:
            dur, err_msg = (time.perf_counter() - t0) * 1000.0, str(exc)
            _save_node_checkpoint(mem, rid, node.node_id, "failed", max(1, node.retries + 1), {}, err_msg, dur)
            with mem._lock:
                mem.conn.execute("UPDATE dag_runs SET status = 'failed', outputs_json = ?, paused_node_id = ?, error = ?, updated_at = CURRENT_TIMESTAMP WHERE run_id = ?", (json.dumps(ctx["nodes"]), node.node_id, err_msg, rid))
                mem.conn.commit()
            telemetry.log("WARN", "dag_runtime", f"DAG `{dag.name}` failed at `{node.node_id}`: {err_msg[:140]}")
            return {"ok": False, "status": "failed", "run_id": rid, "dag_name": dag.name, "failed_node_id": node.node_id, "error": err_msg, "nodes": ctx["nodes"], "summary": f"DAG `{dag.name}` (`{rid}`) failed at `{node.node_id}`: {err_msg}"}
        ctx["nodes"][node.node_id] = out
        _save_node_checkpoint(mem, rid, node.node_id, "completed", attempts, out, "", (time.perf_counter() - t0) * 1000.0)

    with mem._lock:
        mem.conn.execute("UPDATE dag_runs SET status = 'completed', outputs_json = ?, paused_node_id = '', error = '', updated_at = CURRENT_TIMESTAMP WHERE run_id = ?", (json.dumps(ctx["nodes"]), rid))
        mem.conn.commit()
    final_out = ctx["nodes"].get(ordered[-1].node_id, {})
    return {"ok": True, "status": "completed", "run_id": rid, "dag_name": dag.name, "nodes": ctx["nodes"], "final_output": final_out, "summary": f"DAG `{dag.name}` (`{rid}`) completed ({len(ordered)} node(s)). Final output: {json.dumps(final_out, default=str)}"}


def load_dag_spec_from_file(path: Path) -> DAGSpec:
    """Import a Python DAG file (`skills/*/dags/*.py`) and return its `DAG` or `build_dag()` `DAGSpec`."""
    resolved = path.resolve()
    mod_name = f"_companion_dag_{resolved.stem}_{abs(hash(str(resolved))) & 0xFFFF:04x}"
    spec = importlib.util.spec_from_file_location(mod_name, str(resolved))
    if spec is None or spec.loader is None:
        raise ValueError(f"Could not create module spec for `{resolved}`.")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    dag_obj = getattr(mod, "DAG", None)
    if dag_obj is None and callable(getattr(mod, "build_dag", None)):
        dag_obj = mod.build_dag()
    if not isinstance(dag_obj, DAGSpec):
        raise ValueError(f"`{resolved.name}` must expose `DAG: DAGSpec` or `build_dag() -> DAGSpec`.")
    return dag_obj


def dry_run_dag(dag_or_path: Union[DAGSpec, Path]) -> Dict[str, Any]:
    """Validate acyclic topology, node schemas, and `foreach` bindings without running side effects."""
    try:
        dag = load_dag_spec_from_file(dag_or_path) if isinstance(dag_or_path, Path) else dag_or_path
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    err = dag.validate()
    if err:
        return {"ok": False, "dag_name": dag.name, "error": err}
    ordered = [n.node_id for n in dag.topological_order()]
    return {"ok": True, "dag_name": dag.name, "description": dag.description, "topological_order": ordered, "node_count": len(ordered)}


def self_test_dag(path: Path, timeout_sec: int = 20) -> Dict[str, Any]:
    """Run both structural `dry_run_dag` AND `--self-test` execution for a Python DAG module."""
    dry = dry_run_dag(path)
    if not dry.get("ok"):
        return dry
    dag = load_dag_spec_from_file(path)
    if callable(dag.self_test_fn):
        try:
            ok = bool(dag.self_test_fn())
            return {**dry, "ok": ok, "self_test": "callable_passed" if ok else "callable_returned_false"}
        except Exception as exc:
            return {**dry, "ok": False, "error": f"DAG `self_test_fn` raised {type(exc).__name__}: {exc}"}
    proc = subprocess.run(
        [sys.executable, str(path.resolve()), "--self-test"],
        cwd=str(settings.root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
    )
    if proc.returncode != 0:
        return {**dry, "ok": False, "error": f"`--self-test` failed (exit={proc.returncode}): {(proc.stderr or proc.stdout).strip()[:300]}"}
    return {**dry, "ok": True, "self_test": "cli_passed"}


def discover_dags(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Discover all verified Python DAGs across `skills/*/dags/*.py` and `data/skills/*/dags/*.py`."""
    base = (root or settings.root).resolve()
    search_roots = [base / "skills", base / "data" / "skills"]
    data_skills = settings.effective_data_dir.resolve() / "skills"
    if (root is None or base == settings.root.resolve()) and data_skills.is_dir() and data_skills not in search_roots:
        search_roots.append(data_skills)
    found: Dict[str, Dict[str, Any]] = {}
    for sroot in search_roots:
        if not sroot.is_dir():
            continue
        for py_file in sorted(sroot.glob("*/dags/*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                dag = load_dag_spec_from_file(py_file)
                if dag.validate() is None:
                    found[dag.name] = {
                        "name": dag.name,
                        "skill": py_file.parent.parent.name,
                        "description": dag.description,
                        "path": str(py_file),
                        "nodes": [n.node_id for n in dag.topological_order()],
                        "input_schema": dag.input_schema,
                    }
            except Exception:
                continue
    return [found[k] for k in sorted(found)]


def execute_dag_by_name(
    dag_name: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    store: Optional[MemoryStore] = None,
    bindings: Optional[DAGRuntimeBindings] = None,
    root: Optional[Path] = None,
    run_id: Optional[str] = None,
    approved_nodes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Locate a discovered Python `DAGSpec` by kebab-case name and execute it."""
    clean = (dag_name or "").strip().lower().removesuffix(".py").replace("_", "-")
    catalog = {d["name"]: d for d in discover_dags(root=root)}
    if clean not in catalog:
        err = f"DAG `{dag_name}` not found. Available: {sorted(catalog)}"
        return {"ok": False, "status": "not_found", "error": err, "summary": err}
    dag = load_dag_spec_from_file(Path(catalog[clean]["path"]))
    return execute_dag_spec(dag, params=params, store=store, bindings=bindings, run_id=run_id, approved_nodes=approved_nodes)


def resume_dag_run(
    run_id: str,
    *,
    approved_node_id: str = "",
    store: Optional[MemoryStore] = None,
    bindings: Optional[DAGRuntimeBindings] = None,
    root: Optional[Path] = None,
    dag_spec: Optional[DAGSpec] = None,
) -> Dict[str, Any]:
    """Resume a failed or `waiting_approval` DAG run right from its checkpointed node without re-running upstream nodes."""
    mem = store or get_memory_store()
    mem.ensure_schema("dag_runtime", _init_dag_tables)
    rows = mem._fetch_dicts(["run_id", "dag_name", "status", "params_json", "paused_node_id"], "SELECT run_id, dag_name, status, params_json, paused_node_id FROM dag_runs WHERE run_id = ?", (run_id.strip(),))
    if not rows:
        err = f"DAG run `{run_id}` not found."
        return {"ok": False, "status": "not_found", "error": err, "summary": err}
    rec = rows[0]
    try:
        params = json.loads(rec["params_json"] or "{}")
    except Exception:
        params = {}
    approved = [approved_node_id or str(rec["paused_node_id"] or "")] if (approved_node_id or rec["status"] == "waiting_approval") else []
    if dag_spec is not None:
        return execute_dag_spec(dag_spec, params=params, store=mem, bindings=bindings, run_id=rec["run_id"], approved_nodes=approved)
    return execute_dag_by_name(rec["dag_name"], params=params, store=mem, bindings=bindings, root=root, run_id=rec["run_id"], approved_nodes=approved)


def get_dag_run(run_id: str, store: Optional[MemoryStore] = None) -> Optional[Dict[str, Any]]:
    """Inspect a persisted `dag_runs` record and all of its per-node checkpoints."""
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
    """Return recent persisted `dag_runs` records ordered newest-first."""
    mem = store or get_memory_store()
    mem.ensure_schema("dag_runtime", _init_dag_tables)
    cap = max(1, min(int(limit or 25), 100))
    return mem._fetch_dicts(
        ["run_id", "dag_name", "status", "paused_node_id", "approval_id", "error", "created_at", "updated_at"],
        "SELECT run_id, dag_name, status, paused_node_id, approval_id, error, created_at, updated_at FROM dag_runs ORDER BY updated_at DESC, rowid DESC LIMIT ?",
        (cap,),
    )
