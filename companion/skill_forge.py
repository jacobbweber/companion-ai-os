"""
Transactional Capability Forge, Python DAG Verifier, Safe Script Runner & `REFLECT` Distiller
(`companion/skill_forge.py` — A-Frame Application Layer).

Provides:
1. `check_make_vs_borrow`: checks existing 3-Tier `skills/` and `KnowledgePort` before authoring.
2. `stage_and_promote_skill`: stages candidate `SKILL.md`, `dags/*.py`, `scripts/*.py`,
   and `references/*.md` in an isolated temp directory, enforces AST/stdlib/hygiene/DAG linters,
   `dry_run_dag`, and `--self-test` execution, checkpoints `skills/`, and promotes atomically (`R2`).
3. `run_skill_script`: executes promoted `skills/<skill>/scripts/<script>.py` inside the active
   `ExecutionTarget` with post-exec syntax healing.
4. `distill_workflow_reflection`: deterministic post-`VERIFY` `REFLECT` compounding into `KnowledgePort`.
"""

import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from companion.checkpoints import CheckpointManager, checkpoint_manager
from companion.config import SELF_PROJECT_ID, settings
from companion.dag_runtime import discover_dags, execute_dag_by_name, get_dag_run, list_dag_runs, resume_dag_run, self_test_dag
from companion.disciplines import iter_ast_import_targets
from companion.grounding import (
    discover_skills,
    get_execution_target,
    get_knowledge_port,
    invalidate_skills_cache,
    rank_relevant_skills,
)
from companion.hygiene import find_unused_imports_in_source
from companion.memory_store import MemoryStore, get_memory_store, has_memory_store_override
from companion.safety import validate_python_syntax
from companion.sdd import (
    check_wiki_author_guard,
    is_undeclared_skill_import,
    parse_pep723_dependencies,
    validate_dag_python_source,
    validate_skill_markdown,
    verify_skills_directory,
)
from companion.telemetry import redact_secrets, telemetry
from companion.vault import get_vault_manager

__all__ = [
    "build_skill_script_command",
    "check_make_vs_borrow",
    "distill_workflow_reflection",
    "execute_customizations_action",
    "run_skill_script",
    "stage_and_promote_skill",
]

_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


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
    """Enforce syntax, ADR-001 Ring-1 PEP 723 rules, line budgets, and zero unused imports on staged `.py` assets."""
    valid, syn_err = validate_python_syntax(code, filename=fname)
    if not valid:
        return f"{kind} `{fname}` SyntaxError: {syn_err}"
    _raw_deps, allowed_imports, pep_err = parse_pep723_dependencies(code)
    if pep_err:
        return f"{kind} `{fname}` {pep_err}"
    lines = code.splitlines()
    if len(lines) > settings.max_module_lines:
        return f"{kind} `{fname}` exceeds module budget ({len(lines)} > {settings.max_module_lines} lines)."
    tree = ast.parse(code, filename=fname)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_len = getattr(node, "end_lineno", node.lineno) - node.lineno + 1
            if fn_len > settings.max_function_lines:
                return f"{kind} `{fname}` function `{node.name}()` exceeds {settings.max_function_lines} lines ({fn_len}L)."
    for target, lineno in iter_ast_import_targets(tree):
        top_pkg = target.split(".")[0].lower()
        if is_undeclared_skill_import(top_pkg, allowed_imports):
            return (
                f"{kind} `{fname}:{lineno}` imports undeclared third-party package `{top_pkg}` "
                "(declare in PEP 723 `# /// script` `dependencies = [...]` or use Python stdlib)."
            )
    unused = find_unused_imports_in_source(tree, f"{kind.lower()}s/{fname}")
    if unused:
        return f"{kind} `{fname}` has unused import(s): {'; '.join(unused)}"
    if kind == "Script" and "--self-test" not in code:
        return f"Script `{fname}` must implement a `--self-test` CLI entrypoint."
    if kind == "DAG":
        dag_err = validate_dag_python_source(code, filename=fname)
        if dag_err:
            return f"DAG `{fname}`: {dag_err}"
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
    dags: Optional[Dict[str, str]],
    scripts: Optional[Dict[str, str]],
) -> Tuple[Optional[str], List[Path], List[Path]]:
    """Write and lint `SKILL.md`, `references/`, `dags/`, and `scripts/` inside `stage_skill`."""
    stage_skill.mkdir(parents=True, exist_ok=True)
    (stage_skill / "SKILL.md").write_text((skill_md or "").replace("\r\n", "\n"), encoding="utf-8", newline="\n")

    for fname, content in (references or {}).items():
        clean_f = Path(fname).name
        if not clean_f.endswith(".md"):
            return f"SKILL FORGE REJECTED: reference `{fname}` must end with `.md`.", [], []
        ref_d = stage_skill / "references"
        ref_d.mkdir(exist_ok=True)
        (ref_d / clean_f).write_text(str(content).replace("\r\n", "\n"), encoding="utf-8", newline="\n")

    staged_dags: List[Path] = []
    for fname, content in (dags or {}).items():
        clean_f = Path(fname).name
        if not clean_f.endswith(".py"):
            return f"SKILL FORGE REJECTED: Python DAG `{fname}` must end with `.py`.", [], []
        norm_code = str(content).replace("\r\n", "\n")
        lint_err = _lint_staged_python(clean_f, norm_code, "DAG")
        if lint_err:
            return f"SKILL FORGE REJECTED (`dags/{clean_f}`): {lint_err}", [], []
        dag_d = stage_skill / "dags"
        dag_d.mkdir(exist_ok=True)
        dag_p = dag_d / clean_f
        dag_p.write_text(norm_code, encoding="utf-8", newline="\n")
        staged_dags.append(dag_p)

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
    return None, staged_dags, staged_scripts


def _run_staged_self_tests(staged_dags: List[Path], staged_scripts: List[Path], sandbox_dir: Path) -> Optional[str]:
    """Execute `self_test_dag` on staged Python DAGs and `--self-test` on staged helper scripts."""
    for dag_path in staged_dags:
        dag_res = self_test_dag(dag_path)
        if not dag_res.get("ok"):
            return f"SKILL FORGE REJECTED (`dags/{dag_path.name}`): {dag_res.get('error', 'self-test failed')}"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    for sc_path in staged_scripts:
        cmd, cmd_err = build_skill_script_command(sc_path, ["--self-test"])
        if cmd_err or cmd is None:
            return f"SKILL FORGE REJECTED (`{sc_path.name}`): {cmd_err}"
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(sandbox_dir),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
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
    dags: Optional[Dict[str, str]] = None,
    references: Optional[Dict[str, str]] = None,
    self_test: bool = True,
    overwrite: bool = False,
    root: Optional[Path] = None,
    scope: str = "core",
) -> Dict[str, Any]:
    """Stage, lint, `--self-test`, checkpoint, and atomically promote a 3-Tier Capability Pack (`R2`)."""
    clean_name = (name or "").strip().lower()
    if not clean_name or not _KEBAB_RE.match(clean_name):
        msg = f"SKILL FORGE REJECTED: skill name `{name}` must be kebab-case (`[a-z0-9-]+`)."
        return {"promoted": False, "error": msg, "summary": msg}

    eff_scope = (scope or "core").strip().lower()
    target_root = (root or settings.root).resolve()
    is_user_scope = eff_scope == "user"
    if not is_user_scope and target_root == settings.root.resolve() and not settings.is_self_dev_enabled():
        msg = (
            "SAFETY GUARD REJECTED: Promoting skills into Companion core (`skills/`) requires `Self-Dev Mode = ON` "
            "(`/selfdev on`), or pass `scope='user'` to save an update-safe user skill inside `data/skills/`."
        )
        return {"promoted": False, "error": msg, "summary": msg}
    skills_parent = (settings.user_skills_dir if root is None else (target_root / "data" / "skills")) if is_user_scope else (target_root / "skills")
    dest_dir = skills_parent.resolve() / clean_name
    if dest_dir.exists():
        if not overwrite:
            msg = f"SKILL FORGE REJECTED: `skills/{clean_name}` already exists (pass `overwrite=True` to update)."
            return {"promoted": False, "error": msg, "summary": msg}
        for existing_md in sorted(dest_dir.glob("references/*.md")):
            guard_err = check_wiki_author_guard(existing_md)
            if guard_err:
                return {"promoted": False, "error": guard_err, "summary": guard_err}

    md_err = validate_skill_markdown(skill_md or "", expected_name=clean_name)
    if md_err:
        msg = f"SKILL FORGE REJECTED (`SKILL.md`): {md_err}"
        return {"promoted": False, "error": msg, "summary": msg}

    with tempfile.TemporaryDirectory() as tmp:
        stage_root = Path(tmp) / "stage_repo"
        stage_skill = stage_root / "skills" / clean_name
        pop_err, staged_dags, staged_scripts = _populate_staged_skill_dir(stage_skill, skill_md, references, dags, scripts)
        if pop_err:
            return {"promoted": False, "error": pop_err, "summary": pop_err}

        dir_issues = verify_skills_directory(stage_root)
        if dir_issues:
            msg = f"SKILL FORGE REJECTED (structure check): {'; '.join(dir_issues)}"
            return {"promoted": False, "error": msg, "summary": msg}

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
                value=f"Promoted verified 3-Tier skill `{rel_lbl}/SKILL.md` (scripts={list((scripts or {}).keys())}, dags={list((dags or {}).keys())}).",
                project_id="global",
                source="skill_forge",
                confidence=1.0,
            )
    telemetry.log("INFO", "skill_forge", f"Promoted verified skill `{rel_lbl}` (cp={cp['checkpoint_id']})")
    summary = (
        f"Promoted verified skill `{rel_lbl}/` (checkpoint `{cp['checkpoint_id']}`; "
        f"scripts={len(scripts or {})}, dags={len(dags or {})}, references={len(references or {})})."
    )
    return {"promoted": True, "skill": clean_name, "scope": "user" if is_user_scope else "core", "path": str(dest_dir), "checkpoint_id": cp["checkpoint_id"], "summary": summary}


def run_skill_script(
    skill_name: str,
    script_name: str,
    script_args: Optional[List[str]] = None,
    root: Optional[Path] = None,
    timeout: int = 30,
) -> str:
    """Execute a promoted `skills/<skill>/scripts/<script>.py` inside the active `ExecutionTarget`."""
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
        [settings.user_skills_dir.resolve(), (base_root / "skills").resolve()]
        if root is None or base_root == settings.root.resolve()
        else [(base_root / "data" / "skills").resolve(), (base_root / "skills").resolve()]
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
    rc, stdout, stderr, guard_err = get_execution_target().exec_command(
        cmd,
        shell=False,
        timeout=timeout,
    )
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
    """Extract a concise deterministic schema/symbol summary for `.json`, `.toml`, or `.py` deliverables."""
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
    """Persist verified deliverable facts into `KnowledgePort` during the post-`VERIFY` `REFLECT` phase."""
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
    """CoreAction handler for Customizations (`skills/`, `data/skills/`, verified Python `dags`, and `dag_runs`)."""
    mem = store or get_memory_store()
    sub = str(p.get("subaction") or p.get("action") or "list").strip().lower()
    if sub in ("read_skill", "inspect_skill"):
        s_clean = str(p.get("skill") or p.get("name") or "").strip().lower()
        asset = str(p.get("asset") or "SKILL.md").strip().lstrip("/\\").replace("\\", "/")
        roots = [settings.user_skills_dir.resolve(), (settings.root / "skills").resolve()]
        sdir = next(((r / s_clean).resolve() for r in roots if s_clean and (r / s_clean / "SKILL.md").is_file() and (r / s_clean).resolve().is_relative_to(r)), None)
        if sdir is None:
            return {"ok": False, "error": f"Skill pack `{s_clean}` not found."}
        target = (sdir / asset).resolve()
        if not target.is_relative_to(sdir) or not target.is_file():
            return {"ok": False, "error": f"Asset `{asset}` not found inside `{s_clean}`."}
        files = ["SKILL.md", *[f"{sub_d}/{f.name}" for sub_d in ("references", "dags", "scripts") if (sdir / sub_d).is_dir() for f in sorted((sdir / sub_d).iterdir()) if f.is_file()]]
        return {"ok": True, "skill": s_clean, "asset": asset, "files": files, "content": target.read_text(encoding="utf-8", errors="replace"), "summary": f"Loaded `{s_clean}/{asset}`."}
    if sub == "get_dag_run":
        rec = get_dag_run(str(p.get("run_id") or ""), store=mem)
        return {"ok": rec is not None, "dag_run": rec, "error": "" if rec else "DAG run not found."}
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
        "summary": f"Customizations: {len(skills)} Capability Pack(s), {len(dags)} Verified Python DAG(s), {len(runs)} recent DAG run(s).",
    }
