"""
Dynamic `GoalPlan` DAGs, Evidence-Based `VerificationSpec` & Recipe Runner
(`companion/workflow_dag.py` — A-Frame Workflow Crossbar Layer).

Provides:
1. `ExecutorBindings`: class-level Dependency Inversion registry so every `WorkflowEngine`
   instance (including isolated external workspaces and web sessions) shares bound executors.
2. Domain-neutral `GoalPlan`, `GoalStep`, and `VerificationSpec` (`evidence_file`,
   `check_command`, `script_validator`, `assertion`, `human_gate`) with topological readiness,
   structured `.json`/`.toml`/`.py` syntax validation, and `R3`/`human_gate` pause detection.
3. Declarative JSON Workflow Recipe discovery, field interpolation, and execution.
"""

import json
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from companion.checkpoints import CheckpointManager
from companion.config import settings
from companion.disciplines import ActionRiskTier, analyze_python_source
from companion.grounding import get_execution_target
from companion.llm import tier_context
from companion.memory_store import MemoryStore
from companion.safety import check_command_safety
from companion.sdd import validate_workflow_recipe_json
from companion.telemetry import telemetry

__all__ = [
    "ExecutorBindings", "GoalPlan", "GoalStep", "VerificationSpec", "build_goal_plan_from_tasks",
    "discover_workflow_recipes", "evaluate_step_verification", "execute_workflow_recipe",
    "format_recipes_catalog", "is_node_output_passed", "resolve_recipe_file",
]

_STEP_REF_RE = re.compile(r"\{\{steps\.([a-zA-Z0-9_-]+)\.output(?:\.([a-zA-Z0-9_.-]+))?\}\}")
_OPTIONAL_INPUT_RE = re.compile(r"\{\{inputs\.[a-zA-Z0-9_-]+\}\}")
_FILE_TOKEN_RE = re.compile(r"`?([a-zA-Z0-9_./-]+\.(?:py|md|json|toml|yaml|yml|ini|html|css|js|ts|txt|sh|sql|csv|xml))`?")
_DAG_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


class ExecutorBindings:
    """Shared Dependency-Inversion bindings for Application-layer tool and subagent executors."""

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


@dataclass
class VerificationSpec:
    """Domain-neutral verification contract for a single `GoalStep`."""

    kind: str = "assertion"
    target: str = ""
    contains: str = ""
    description: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "target": self.target, "contains": self.contains, "description": self.description}

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "VerificationSpec":
        kind = str(raw.get("kind") or "assertion").strip().lower()
        if kind not in ("evidence_file", "check_command", "script_validator", "assertion", "human_gate"):
            kind = "assertion"
        return cls(kind, str(raw.get("target") or ""), str(raw.get("contains") or ""), str(raw.get("description") or ""))


@dataclass
class GoalStep:
    """Single node inside a dynamic `GoalPlan` execution DAG."""

    step_id: str
    title: str
    depends_on: List[str] = field(default_factory=list)
    risk_tier: str = ActionRiskTier.R1_LOCAL_REVERSIBLE.value
    verification: VerificationSpec = field(default_factory=VerificationSpec)
    status: str = "pending"
    evidence: str = ""
    task_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id, "title": self.title, "depends_on": list(self.depends_on),
            "risk_tier": self.risk_tier, "verification": self.verification.to_dict(),
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
            step_id=str(raw.get("step_id") or raw.get("id") or f"s{idx}"), title=str(raw.get("title") or raw.get("description") or f"Step {idx}"),
            depends_on=deps, risk_tier=risk, verification=v_spec, status=str(raw.get("status") or "pending"),
            evidence=str(raw.get("evidence") or ""), task_id=int(raw["task_id"]) if raw.get("task_id") is not None else None,
        )


@dataclass
class GoalPlan:
    """Structured multi-step execution DAG persisted in `workflow_runs.metadata_json['goal_dag']`."""

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
    """Validate structured syntax for `.py`, `.json`, or `.toml` files."""
    if raw_text is None:
        try:
            txt = get_execution_target().read_text(path)
        except Exception:
            return f"Missing expected artifact `{path.name}`"
    else:
        txt = raw_text
    ext = path.suffix.lower()
    if ext == ".py":
        res = analyze_python_source(txt, filename=str(path))
        return None if res.valid_syntax else f"Python SyntaxError in `{path.name}`: {res.syntax_error}"
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


def evaluate_step_verification(
    spec: VerificationSpec,
    root: Path,
    step_output: str = "",
    changed_paths: Optional[List[Path]] = None,
) -> Tuple[bool, str]:
    """Deterministically evaluate a `VerificationSpec` against workspace artifacts or check commands."""
    target_root = root.resolve()
    for cp in (changed_paths or []):
        if cp.is_file():
            err = _validate_structured_file(cp)
            if err:
                return False, err

    if spec.kind == "human_gate":
        if "HUMAN_APPROVED" in (step_output or ""):
            return True, "Human gate approved."
        return False, f"HUMAN_GATE_REQUIRED: {spec.description or spec.target or 'Awaiting user confirmation'}"

    if spec.kind == "evidence_file":
        if spec.target and spec.target != "*":
            p = (target_root / spec.target).resolve()
            if not p.is_relative_to(target_root):
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
        if not changed_paths:
            return False, "No workspace artifact created for `evidence_file` gate."
        return True, f"Verified {len(changed_paths)} modified artifact(s)."

    if spec.kind in ("check_command", "script_validator"):
        cmd = spec.target.strip()
        if not cmd:
            return False, f"`{spec.kind}` requires a non-empty `target` command or script."
        if spec.kind == "script_validator" and cmd.endswith(".py"):
            script_p = (Path(cmd) if Path(cmd).is_absolute() else (target_root / cmd)).resolve()
            if not any(script_p.is_relative_to(r) for r in (target_root, (settings.root / "skills").resolve(), settings.user_skills_dir.resolve())):
                return False, f"Validator script `{cmd}` escapes workspace and skills roots."
            if not script_p.is_file():
                return False, f"Validator script `{cmd}` not found."
            run_args: Any = [sys.executable, str(script_p)]
            use_shell = False
        else:
            safe_err = check_command_safety(cmd)
            if safe_err:
                return False, safe_err
            run_args = cmd
            use_shell = True
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
    """Match a DAG step title to the best-scoring SQLite pending task by token overlap before positional fallback."""
    step_toks = {w for w in re.findall(r"[a-z0-9_./-]{3,}", (title or "").lower()) if w not in ("the", "and", "for", "with", "step", "task")}
    best_id, best_score = None, 0
    for tid, desc, _st, _prio in pending_tasks:
        if tid in used_ids:
            continue
        overlap = len(step_toks & set(re.findall(r"[a-z0-9_./-]{3,}", desc.lower())))
        if overlap > best_score:
            best_score, best_id = overlap, tid
    return best_id if best_score >= 1 else None


def build_goal_plan_from_tasks(
    goal: str,
    pending_tasks: List[Tuple[int, str, str, int]],
    plan_text: str = "",
) -> GoalPlan:
    """Construct a structured `GoalPlan` DAG from explicit JSON in `plan_text` or SQLite tasks."""
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
        f_match = _FILE_TOKEN_RE.search(desc)
        low_d = desc.lower()
        if "human_gate" in low_d or "await user approval" in low_d:
            v_spec = VerificationSpec("human_gate", description=desc[:120])
        elif f_match and any(w in low_d for w in ("create", "write", "generate", "draft", "author")):
            v_spec = VerificationSpec("evidence_file", target=f_match.group(1), description=f"Verify `{f_match.group(1)}` exists and is valid")
        else:
            v_spec = VerificationSpec("assertion", description="Verify clean execution and syntax")
        risk = "R3" if any(k in low_d for k in ("push to remote", "migrate live", "production cutover")) else ("R2" if "skills/" in low_d else "R1")
        steps.append(GoalStep(step_id=sid, title=desc, depends_on=[prev_id] if prev_id else [], risk_tier=risk, verification=v_spec, task_id=tid))
        prev_id = sid
    return GoalPlan(goal=goal, steps=steps)


def _extract_step_field(raw_output: str, field_path: Optional[str]) -> str:
    text = raw_output or ""
    if not field_path:
        return text
    if field_path == "first_line":
        return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    try:
        curr: Any = json.loads(text.strip())
        for part in field_path.split("."):
            curr = curr[part] if isinstance(curr, dict) and part in curr else None
        if curr is not None:
            return json.dumps(curr) if isinstance(curr, (dict, list)) else str(curr)
    except Exception:
        pass
    m = re.search(rf"(?im)^\s*{re.escape(field_path)}\s*[:=]\s*(.+?)\s*$", text)
    return m.group(1).strip() if m else ""


def _interpolate_recipe_value(val: Any, context_vars: Dict[str, Any], step_outputs: Optional[Dict[str, str]] = None) -> Any:
    if isinstance(val, str):
        out = _STEP_REF_RE.sub(lambda m: _extract_step_field(step_outputs.get(m.group(1), ""), m.group(2)), val) if step_outputs is not None else val
        for k, v in context_vars.items():
            out = out.replace(f"{{{{inputs.{k}}}}}", str(v)).replace(f"{{{{{k}}}}}", str(v))
        return _OPTIONAL_INPUT_RE.sub("", out)
    if isinstance(val, list):
        return [_interpolate_recipe_value(item, context_vars, step_outputs) for item in val]
    if isinstance(val, dict):
        return {k: _interpolate_recipe_value(v, context_vars, step_outputs) for k, v in val.items()}
    return val


def _evaluate_edge_condition(condition: str, node_passed: bool, output_text: str) -> bool:
    cond = (condition or "always").strip()
    if cond in ("always", "passed", "failed"):
        return True if cond == "always" else (node_passed if cond == "passed" else not node_passed)
    if cond.startswith("contains:"):
        return cond[len("contains:") :].lower() in output_text.lower()
    return (cond[len("not_contains:") :].lower() not in output_text.lower()) if cond.startswith("not_contains:") else False


def is_node_output_passed(output: str) -> bool:
    """Determine whether a workflow node or step output represents a clean pass."""
    text = (output or "").strip()
    if text.startswith(("error:", "exit=1", "SYNTAX GUARD REJECTED", "SKILL LINTER REJECTED", "WORKFLOW RECIPE LINTER REJECTED", "RISK GATE REJECTED", "PHASE TOOL GATE REJECTED")):
        return False
    return not any(m in text for m in ("ISSUES DETECTED (FAIL)", "SPEC DRIFT / SPRAWL DETECTED (FAIL)", "HYGIENE ISSUES DETECTED"))


def discover_workflow_recipes(root: Path) -> List[Dict[str, Any]]:
    """Discover and validate all declarative JSON workflow recipes under built-in and user `skills/*/workflows/*.json`."""
    skills_dir = (root / "skills") if (root / "skills").is_dir() else (settings.root / "skills")
    scan_dirs = [d for d in (skills_dir, *([settings.user_skills_dir] if root.resolve() == settings.root.resolve() else [])) if d.is_dir()]
    if not scan_dirs:
        return []
    wf_map = {wf.stem: wf for d in scan_dirs for wf in sorted(d.glob("*/workflows/*.json"), key=lambda p: str(p).lower())}
    recipes: List[Dict[str, Any]] = []
    for wf_file in (wf_map[k] for k in sorted(wf_map)):
        raw = wf_file.read_text(encoding="utf-8", errors="replace")
        err = validate_workflow_recipe_json(raw, expected_name=wf_file.stem)
        data = json.loads(raw) if err is None else {}
        rel_p = str(wf_file.relative_to(root)).replace("\\", "/") if wf_file.is_relative_to(root) else str(wf_file)
        recipes.append({
            "name": str(data.get("name") or wf_file.stem), "skill": wf_file.parent.parent.name,
            "path": rel_p,
            "description": str(data.get("description") or (err or "")),
            "inputs": data.get("inputs") if isinstance(data.get("inputs"), dict) else {},
            "node_count": len(data.get("nodes", [])) if isinstance(data.get("nodes"), list) else 0,
            "valid": err is None,
        })
    return recipes


def format_recipes_catalog(root: Path) -> str:
    recipes = discover_workflow_recipes(root)
    if not recipes:
        return "=== Declarative Workflow Recipes ===\n(No JSON workflow recipes found in `skills/*/workflows/*.json`.)"
    lines = [f"=== Declarative JSON Workflow Recipes ({len(recipes)} available) ==="]
    for r in recipes:
        inp_str = f" | inputs={list(r['inputs'].keys())}" if r["inputs"] else ""
        lines.append(f"  * `{r['name']}` (Skill: `{r['skill']}` | `{r['path']}` | {r['node_count']} nodes{inp_str})\n    {r['description']}")
    lines.append("\n(Run with `/workflow run <recipe-name>` or `workflow_manage(action='run_recipe', recipe='<name>')`.)")
    return "\n".join(lines)


def resolve_recipe_file(root: Path, recipe_name_or_path: str) -> Optional[Path]:
    q = (recipe_name_or_path or "").strip()
    if not q:
        return None
    for base in (root, settings.root):
        direct = Path(q) if Path(q).is_absolute() else (base / q)
        if direct.is_file():
            return direct
        skills_dir = base / "skills"
        if skills_dir.is_dir():
            for wf_file in skills_dir.glob("*/workflows/*.json"):
                if wf_file.stem.lower() == q.lower() or f"{wf_file.parent.parent.name}/{wf_file.stem}".lower() == q.lower():
                    return wf_file
    return None


def _execute_single_recipe_node(
    node: Dict[str, Any], ntier: str, ctx_vars: Dict[str, Any], step_outputs: Dict[str, str],
    active_tool: Optional[Callable[[str, Dict[str, Any]], str]], active_sub: Optional[Callable[..., str]],
    root: Path, store: MemoryStore, cp_mgr: CheckpointManager, depth: int,
) -> str:
    ntype, curr_id = str(node["type"]), str(node["id"])
    with tier_context(ntier):
        if ntype == "tool":
            t_name = str(node["tool"])
            t_args = _interpolate_recipe_value(node.get("args") or {}, ctx_vars, step_outputs)
            return active_tool(t_name, t_args) if active_tool else f"error: no tool executor bound for {t_name}"
        if ntype == "recipe":
            if depth >= 3:
                return "error: maximum nested recipe depth (3) reached."
            sub_name = str(node["recipe"])
            sub_args = _interpolate_recipe_value(node.get("args") or node.get("inputs") or {}, ctx_vars, step_outputs)
            sub_res = execute_workflow_recipe(root, store, cp_mgr, sub_name, params=sub_args, tool_fn=active_tool, subagent_fn=active_sub, _depth=depth + 1)
            return str(sub_res["summary"]) if sub_res.get("status") == "COMPLETE" else f"error: sub-recipe `{sub_name}` failed:\n{sub_res.get('summary', '')}"
        prompt = str(_interpolate_recipe_value(node.get("prompt") or "", ctx_vars, step_outputs))
        skill = str(node.get("skill") or "") or None
        iters = int(node.get("max_iters") or (1 if ntype == "llm" else 6))
        return active_sub(prompt, skill, iters) if active_sub else f"error: no subagent executor bound for {curr_id}"


def execute_workflow_recipe(
    root: Path, store: MemoryStore, cp_mgr: CheckpointManager, recipe_name_or_path: str,
    params: Optional[Dict[str, Any]] = None, tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None,
    subagent_fn: Optional[Callable[..., str]] = None, max_steps: int = 20, _depth: int = 0,
) -> Dict[str, Any]:
    """Execute a declarative JSON Workflow DAG Recipe while preserving any superseded parent workflow metadata."""
    wf_path = resolve_recipe_file(root, recipe_name_or_path)
    if wf_path is None or not wf_path.is_file():
        msg = f"error: workflow recipe '{recipe_name_or_path}' not found."
        return {"valid": False, "recipe": recipe_name_or_path, "status": "error", "steps": [], "summary": msg}
    raw = wf_path.read_text(encoding="utf-8", errors="replace")
    err = validate_workflow_recipe_json(raw, expected_name=wf_path.stem)
    if err:
        msg = f"error: invalid workflow recipe `{wf_path.name}`: {err}"
        return {"valid": False, "recipe": wf_path.stem, "status": "error", "steps": [], "summary": msg}

    data = json.loads(raw)
    r_name = str(data["name"])
    decl_inputs, user_params = dict(data.get("inputs") or {}), dict(params or {})
    if user_params.get("goal"):
        for k in decl_inputs:
            if k not in user_params and k not in ("run_tests", "mode"):
                user_params[k] = user_params["goal"]
    ctx_vars: Dict[str, Any] = {**decl_inputs, **user_params, "last_output": ""}
    nodes_by_id = {str(n["id"]): n for n in data["nodes"]}
    edges, curr_id = data["edges"], str(data["nodes"][0]["id"])
    active_tool, active_sub = ExecutorBindings.resolve(tool_fn, subagent_fn)
    cp_id = cp_mgr.create_checkpoint(label=f"recipe_{r_name}")["checkpoint_id"] if _depth == 0 else ""

    pre_active = store.get_active_workflow()
    wf_id = store.create_workflow_run(workflow_type=f"recipe:{r_name}", goal=str(data["description"]), initial_state=curr_id, checkpoint_id=cp_id)
    is_live_db = store.db_path.resolve() == settings.db_path.resolve()
    own_tid = telemetry.start_trace(f"recipe:{r_name}", str(data["description"])) if (is_live_db and telemetry.active_trace_id is None) else None
    steps: List[Dict[str, Any]] = []
    step_outputs: Dict[str, str] = {}
    final_state, rolled_back = "ABORTED", False

    try:
        for _ in range(max(1, max_steps)):
            node = nodes_by_id.get(curr_id)
            if not node:
                break
            ntype = str(node["type"])
            ntier = str(node.get("tier") or ("reasoning" if ntype == "llm" else "fast")).strip().lower()
            t0 = time.perf_counter()
            output = _execute_single_recipe_node(node, ntier, ctx_vars, step_outputs, active_tool, active_sub, root, store, cp_mgr, _depth)
            dur_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            passed = is_node_output_passed(output)
            ctx_vars["last_output"], step_outputs[curr_id] = output, output
            telemetry.record_span("workflow_node", f"{r_name}:{curr_id}", dur_ms, status="ok" if passed else "error", detail=f"tier={ntier} {output[:100].replace(chr(10), ' ')}")

            next_id, matched_cond = ("COMPLETE" if passed else "ABORTED"), "default"
            for edge in edges:
                if str(edge.get("from")) == curr_id and _evaluate_edge_condition(str(edge.get("condition") or "always"), passed, output):
                    next_id, matched_cond = str(edge["to"]), str(edge.get("condition") or "always")
                    break
            steps.append({"node": curr_id, "type": ntype, "tier": ntier, "passed": passed, "duration_ms": dur_ms, "next": next_id, "condition": matched_cond, "output": output})
            status_tag = "completed" if next_id == "COMPLETE" else ("aborted" if next_id == "ABORTED" else "active")
            store.update_workflow_state(wf_id, curr_id, next_id, f"Node `{curr_id}` ({'PASS' if passed else 'FAIL'}) -> `{next_id}`", status=status_tag, checkpoint_id=cp_id, verification_passed=passed)
            if next_id in ("COMPLETE", "ABORTED"):
                final_state = next_id
                break
            curr_id = next_id
    finally:
        curr_active = store.get_active_workflow()
        if curr_active and int(curr_active["id"]) == wf_id:
            store.update_workflow_state(wf_id, curr_id, "ABORTED", "Recipe terminated early", status="aborted", checkpoint_id=cp_id, verification_passed=False)
        if _depth == 0 and final_state == "ABORTED" and cp_id and bool(data.get("rollback_on_abort", True)):
            cp_mgr.rollback_checkpoint(cp_id, reason=f"Workflow recipe `{r_name}` aborted")
            rolled_back = True
        if pre_active and int(pre_active["id"]) != wf_id:
            store.restore_superseded_workflow(int(pre_active["id"]))
        if own_tid:
            telemetry.end_trace(own_tid, status="ok" if final_state == "COMPLETE" else "error")
        store.sync_memory_md()

    cp_note = f" | Checkpoint: `{cp_id}`{' (AUTO-ROLLED BACK)' if rolled_back else ''}" if cp_id else ""
    lines = [f"=== Workflow Recipe `{r_name}` Execution: {final_state} ({len(steps)} step(s){cp_note}) ==="]
    for idx, s in enumerate(steps, 1):
        badge = "PASS" if s["passed"] else "FAIL"
        preview = s["output"].splitlines()[0][:120] if s["output"] else "(empty)"
        lines.append(f"  {idx}. [{badge} | {s['duration_ms']:.1f}ms | tier={s['tier']}] `{s['node']}` ({s['type']}) --({s['condition']})--> `{s['next']}`\n     Output: {preview}")
    return {"valid": True, "recipe": r_name, "workflow_id": wf_id, "checkpoint_id": cp_id, "rolled_back": rolled_back, "status": final_state, "steps": steps, "summary": "\n".join(lines)}
