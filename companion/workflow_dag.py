"""
Dynamic `GoalPlan` DAGs & Pluggable `VerificationSpec` Domain Validators
(`companion/workflow_dag.py` — A-Frame Workflow Crossbar Layer).

Provides:
1. `ExecutorBindings`: class-level Dependency Inversion registry so every `WorkflowEngine`
   instance shares bound Application executors.
2. Domain-neutral `GoalPlan`, `GoalStep`, and `VerificationSpec` (`evidence_file`,
   `check_command`, `script_validator`, `domain_validator`, `assertion`, `human_gate`)
   with cycle-safe topological readiness, structured `.json`/`.toml`/`.py` validation,
   and `R3`/`human_gate` pause detection.
3. Pluggable Domain Validator Registry (`register_domain_validator`,
   `discover_skill_domain_validators`) allowing Capability Packs (`skills/*/scripts/*_validator.py`
   and `data/skills/*/scripts/*_validator.py`) to enforce deterministic CAD/Vision/IaC checks.
"""

import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from companion.config import settings
from companion.approvals import ActionRiskTier
from companion.tools import validate_python_syntax
from companion.tools import get_execution_target
from companion.tools import check_command_safety

__all__ = [
    "DOMAIN_VALIDATOR_REGISTRY",
    "ExecutorBindings",
    "GoalPlan",
    "GoalStep",
    "VerificationSpec",
    "build_goal_plan_from_tasks",
    "discover_skill_domain_validators",
    "evaluate_step_verification",
    "is_node_output_passed",
    "register_domain_validator",
]

_FILE_TOKEN_RE = re.compile(r"`?([a-zA-Z0-9_./-]+\.(?:py|md|json|toml|yaml|yml|ini|html|css|js|ts|txt|sh|sql|csv|xml|stl|step|scad))`?")
_DAG_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)

VALID_VERIFICATION_KINDS = (
    "evidence_file",
    "check_command",
    "script_validator",
    "domain_validator",
    "assertion",
    "human_gate",
)

DomainValidatorFn = Callable[[str, Dict[str, Any], Path], Tuple[bool, str]]
DOMAIN_VALIDATOR_REGISTRY: Dict[str, DomainValidatorFn] = {}


def register_domain_validator(name: str, fn: DomainValidatorFn) -> None:
    """Register a deterministic domain validator `(target, params, root) -> (passed, detail)`."""
    DOMAIN_VALIDATOR_REGISTRY[name.strip().lower()] = fn


def discover_skill_domain_validators(root: Optional[Path] = None) -> Dict[str, Path]:
    """Discover `<validator_name>_validator.py` scripts across `skills/` and `data/skills/`."""
    base = (root or settings.root).resolve()
    roots = [base / "skills", settings.effective_data_dir.resolve() / "skills"]
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
    """Determine whether a step or tool output succeeded cleanly."""
    clean = (output or "").strip()
    if not clean:
        return False
    fail_prefixes = (
        "error:",
        "exit=1",
        "SYNTAX GUARD REJECTED",
        "SAFETY GUARD REJECTED",
        "HYGIENE GUARD REJECTED",
        "ARCHITECTURE GUARD REJECTED",
        "SKILL LINTER REJECTED",
        "WIKI AUTHOR GUARD REJECTED",
        "PHASE TOOL GATE REJECTED",
        "RISK GATE REJECTED",
    )
    if clean.startswith(fail_prefixes) or re.match(r"^exit=(?!0\b)-?\d+\b", clean):
        return False
    return "ISSUES DETECTED (" not in clean and "SPEC DRIFT / SPRAWL DETECTED (FAIL)" not in clean


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
    """Domain-neutral verification contract for a single `GoalStep` or `WorkItem`."""

    kind: str = "assertion"
    target: str = ""
    contains: str = ""
    description: str = ""
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "kind": self.kind,
            "target": self.target,
            "contains": self.contains,
            "description": self.description,
        }
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
            kind=kind,
            target=str(raw.get("target") or ""),
            contains=str(raw.get("contains") or ""),
            description=str(raw.get("description") or ""),
            params=dict(p_raw) if isinstance(p_raw, dict) else {},
        )


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
    stage_kind: str = "DELIVER_AND_VERIFY"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "depends_on": list(self.depends_on),
            "risk_tier": self.risk_tier,
            "stage_kind": self.stage_kind,
            "verification": self.verification.to_dict(),
            "status": self.status,
            "evidence": self.evidence,
            "task_id": self.task_id,
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
            depends_on=deps,
            risk_tier=risk,
            stage_kind=str(raw.get("stage_kind") or "DELIVER_AND_VERIFY"),
            verification=v_spec,
            status=str(raw.get("status") or "pending"),
            evidence=str(raw.get("evidence") or ""),
            task_id=int(raw["task_id"]) if raw.get("task_id") is not None else None,
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
            input=payload,
            cwd=str(target_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        out_txt = (proc.stdout or proc.stderr or "").strip()
        try:
            res_json = json.loads(out_txt)
            if isinstance(res_json, dict):
                return bool(res_json.get("passed", proc.returncode == 0)), str(res_json.get("detail") or out_txt)[:280]
        except Exception:
            pass
        return proc.returncode == 0, out_txt[:280] or f"Domain validator `{vkey}` exited {proc.returncode}."
    return False, f"Domain validator `{vkey}` not registered or found in `skills/*/scripts/*_validator.py`."


def evaluate_step_verification(
    spec: VerificationSpec,
    root: Path,
    step_output: str = "",
    changed_paths: Optional[List[Path]] = None,
) -> Tuple[bool, str]:
    """Deterministically evaluate a `VerificationSpec` against workspace artifacts, commands, or domain validators."""
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
    """Construct a structured `GoalPlan` DAG from explicit JSON in `plan_text` or SQLite work items."""
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
