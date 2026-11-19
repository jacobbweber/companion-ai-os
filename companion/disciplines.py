"""
Engineering Disciplines, Architectural Boundaries & Code Quality Validators
(A-Frame Right Leg — Domain & Engineering Practices).

Codifies deterministic practices across:
1. Product & Requirements Analysis (INTAKE)
2. Research & Codebase Discovery (DISCOVER)
3. Software Architecture & A-Frame Design (DESIGN)
4. Project Planning & Task Breakdown (PLAN)
5. Clean Modular Implementation (IMPLEMENT)
6. Quality Assurance & Automated Verification (VERIFY)
7. Self-Healing & Checkpoint Recovery (RECOVER)
8. Continuous Learning & Memory Sync (REFLECT)
"""

import ast
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from companion.config import SKIP_DIRS

_SCRIPT_TAG_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.I | re.S)
_SCRIPT_SRC_ATTR_RE = re.compile(r"\bsrc\s*=", re.I)
_SCRIPT_TYPE_ATTR_RE = re.compile(r"""\btype\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_JS_SCRIPT_TYPES = frozenset({"", "text/javascript", "application/javascript", "text/ecmascript", "application/ecmascript", "module"})
_PY_SLICE_IN_JS_RE = re.compile(r"\b[A-Za-z_$][\w$]*\[\s*\d*\s*:\s*\d+\s*\]")
_PY_BOOL_IN_JS_TPL_RE = re.compile(r"\$\{[^}\n]*\b(?:or|and)\b[^}\n]*\}")

FORBIDDEN_EXTERNAL_IMPORTS = {
    "fastapi", "uvicorn", "flask", "django", "starlette", "tornado",
    "requests", "pydantic", "aiohttp", "httpx", "dotenv", "bs4", "rich",
}

STUB_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\[Echo\]", re.I), "hardcoded `[Echo]` stub response"),
    (re.compile(r"\b(?:TODO|FIXME)\s*:\s*(?:placeholder|stub|implement\s+later|wire\s+later)\b", re.I), "unfinished placeholder/stub marker"),
    (re.compile(r"raise\s+NotImplementedError\b"), "unimplemented `NotImplementedError` stub"),
)


class ArchitectureLayer(Enum):
    """The core architectural layers in the Companion A-Frame Architecture."""

    INFRASTRUCTURE = "infrastructure"  # Left Leg Base: Config (.env), Waterfall Telemetry, Multi-Tier LLM HTTP
    DOMAIN = "domain"                  # Right Leg Base: Checkpoints, Safety, SQLite, TriggerPolicy, ProjectGraph, Disciplines, Grounding, Hygiene, SDD
    WORKFLOW = "workflow"              # A-Frame Crossbar: State-Machine Graph (`workflow_graph`), GoalPlan (`workflow_dag`) & Code-Controlled DAG Runtime (`dag_runtime`)
    APPLICATION = "application"        # Upper A-Frame: Intake, Skill Forge, Compaction, Tools, Context, Routines, Autonomy, Blueprint, Eval, Agent
    PRESENTATION = "presentation"      # A-Frame Apex: CLI/REPL Entrypoint (harness.py) & Stdlib LAN Web UI (surfaces/)
    UTILITY = "utility"                # Cross-cutting stdlib helpers & test suite


# Layer dependency hierarchy: higher index layers may depend on lower index layers, never vice versa.
LAYER_ORDER: Dict[ArchitectureLayer, int] = {
    ArchitectureLayer.UTILITY: 0,
    ArchitectureLayer.INFRASTRUCTURE: 1,
    ArchitectureLayer.DOMAIN: 2,
    ArchitectureLayer.WORKFLOW: 3,
    ArchitectureLayer.APPLICATION: 4,
    ArchitectureLayer.PRESENTATION: 5,
}


# Mapping of project modules to their A-Frame Architecture Layer
MODULE_LAYER_MAP: Dict[str, ArchitectureLayer] = {
    "companion": ArchitectureLayer.INFRASTRUCTURE, "companion.config": ArchitectureLayer.INFRASTRUCTURE,
    "companion.streaming": ArchitectureLayer.INFRASTRUCTURE, "companion.telemetry": ArchitectureLayer.INFRASTRUCTURE,
    "companion.llm_wire": ArchitectureLayer.INFRASTRUCTURE, "companion.llm": ArchitectureLayer.INFRASTRUCTURE,
    "companion.checkpoints": ArchitectureLayer.DOMAIN, "companion.safety": ArchitectureLayer.DOMAIN,
    "companion.memory_store": ArchitectureLayer.DOMAIN, "companion.semantic_memory": ArchitectureLayer.DOMAIN,
    "companion.disciplines": ArchitectureLayer.DOMAIN, "companion.hygiene": ArchitectureLayer.DOMAIN,
    "companion.sdd": ArchitectureLayer.DOMAIN, "companion.grounding": ArchitectureLayer.DOMAIN,
    "companion.archive_and_approvals": ArchitectureLayer.DOMAIN, "companion.vault": ArchitectureLayer.DOMAIN,
    "companion.wiki_templates": ArchitectureLayer.DOMAIN, "companion.wiki_engine": ArchitectureLayer.DOMAIN,
    "companion.wiki_curation": ArchitectureLayer.DOMAIN, "companion.wiki_maintenance": ArchitectureLayer.DOMAIN,
    "companion.attachments": ArchitectureLayer.DOMAIN, "companion.trigger_policy": ArchitectureLayer.DOMAIN,
    "companion.project_graph": ArchitectureLayer.DOMAIN,
    "companion.education_engine": ArchitectureLayer.DOMAIN, "companion.model_registry": ArchitectureLayer.DOMAIN,
    "companion.workflow_graph": ArchitectureLayer.WORKFLOW, "companion.workflow_dag": ArchitectureLayer.WORKFLOW,
    "companion.dag_runtime": ArchitectureLayer.WORKFLOW,
    "companion.compaction": ArchitectureLayer.APPLICATION, "companion.tool_schemas": ArchitectureLayer.APPLICATION,
    "companion.intake": ArchitectureLayer.APPLICATION,
    "companion.autonomy": ArchitectureLayer.APPLICATION, "companion.blueprint": ArchitectureLayer.APPLICATION,
    "companion.skill_forge": ArchitectureLayer.APPLICATION, "companion.eval_harness": ArchitectureLayer.APPLICATION,
    "companion.background_jobs": ArchitectureLayer.APPLICATION, "companion.endpoints": ArchitectureLayer.APPLICATION,
    "companion.mcp_hub": ArchitectureLayer.APPLICATION, "companion.routines": ArchitectureLayer.APPLICATION,
    "companion.calendar_engine": ArchitectureLayer.APPLICATION, "companion.core_actions": ArchitectureLayer.APPLICATION,
    "companion.harvest_tools": ArchitectureLayer.APPLICATION, "companion.os_tools": ArchitectureLayer.APPLICATION,
    "companion.tools": ArchitectureLayer.APPLICATION, "companion.context": ArchitectureLayer.APPLICATION,
    "companion.agent": ArchitectureLayer.APPLICATION,
    "configs": ArchitectureLayer.DOMAIN, "configs.architecture": ArchitectureLayer.DOMAIN,
    "memory": ArchitectureLayer.DOMAIN, "memory.chat_memory": ArchitectureLayer.DOMAIN,
    "surfaces": ArchitectureLayer.PRESENTATION, "surfaces.web_assets": ArchitectureLayer.PRESENTATION,
    "surfaces.api_extensions": ArchitectureLayer.PRESENTATION, "surfaces.web_server": ArchitectureLayer.PRESENTATION,
    "surfaces.windows_tray": ArchitectureLayer.PRESENTATION,
    "harness": ArchitectureLayer.PRESENTATION,
}


def iter_ast_import_targets(tree: ast.AST, current_pkg: str = "") -> List[Tuple[str, int]]:
    """Extract `(dotted_target, lineno)` pairs from `import` and `from ... import ...` statements."""
    targets: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        lineno = int(getattr(node, "lineno", 1) or 1)
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append((alias.name, lineno))
        elif isinstance(node, ast.ImportFrom):
            base_mod = node.module or ""
            if node.level and current_pkg:
                parts = current_pkg.split(".")
                keep = max(0, len(parts) - (node.level - 1))
                prefix = ".".join(parts[:keep])
                base_mod = f"{prefix}.{base_mod}".strip(".") if base_mod else prefix
            if base_mod:
                targets.append((base_mod, lineno))
            for alias in node.names:
                if base_mod and alias.name != "*":
                    targets.append((f"{base_mod}.{alias.name}", lineno))
    return targets


def check_module_layer_imports(tree: ast.AST, rel_or_dotted: str) -> List[Tuple[str, ArchitectureLayer, str, ArchitectureLayer, int]]:
    """Return upward A-Frame layer import violations `(src_mod, src_layer, tgt_mod, tgt_layer, lineno)`."""
    dotted = rel_or_dotted.replace("\\", "/").removesuffix(".py").replace("/", ".").removesuffix(".__init__")
    src_layer = MODULE_LAYER_MAP.get(dotted)
    if not src_layer:
        return []
    src_order = LAYER_ORDER.get(src_layer, 99)
    current_pkg = dotted if (rel_or_dotted.endswith("__init__.py") or "." not in dotted and dotted in ("companion", "configs", "memory", "surfaces")) else (dotted.rsplit(".", 1)[0] if "." in dotted else "")
    violations: List[Tuple[str, ArchitectureLayer, str, ArchitectureLayer, int]] = []
    seen: set[Tuple[str, str]] = set()
    for target, lineno in iter_ast_import_targets(tree, current_pkg=current_pkg):
        if target in MODULE_LAYER_MAP:
            tgt_layer = MODULE_LAYER_MAP[target]
            if LAYER_ORDER.get(tgt_layer, 0) > src_order and (dotted, target) not in seen:
                seen.add((dotted, target))
                violations.append((dotted, src_layer, target, tgt_layer, lineno))
    return violations


@dataclass(frozen=True)
class DisciplineSpec:
    """Defines an engineering discipline enforced by the Companion State Machine."""

    id: str
    name: str
    phase: str
    purpose: str
    checklist: Tuple[str, ...]
    anti_patterns: Tuple[str, ...]
    deliverable: str


DISCIPLINES: Dict[str, DisciplineSpec] = {
    "INTAKE": DisciplineSpec(
        id="product_requirements",
        name="Product & Requirements Engineering",
        phase="INTAKE",
        purpose="Translate the user's high-level, non-technical intent into clear goals and deterministic acceptance criteria.",
        checklist=(
            "Identify the core outcome the user wants to achieve.",
            "Check persistent memory and backlog for prior context or user preferences.",
            "Formulate explicit, testable Acceptance Criteria before designing or coding.",
            "Never burden the user with low-level engineering jargon; own the technical decisions.",
        ),
        anti_patterns=(
            "Jumping straight into editing files without defining what success looks like.",
            "Asking a non-technical user to pick code patterns or debug stack traces.",
        ),
        deliverable="Clear goal statement and measurable Acceptance Criteria registered in the workflow.",
    ),
    "DISCOVER": DisciplineSpec(
        id="discovery_analysis",
        name="Codebase Discovery & Impact Analysis",
        phase="DISCOVER",
        purpose="Inspect existing modules, schemas, and tests to understand the blast radius before making changes.",
        checklist=(
            "Use read_file, search, or list_dir to inspect target files before editing.",
            "Identify which A-Frame layer(s) are affected.",
            "Check existing unit tests in tests/ to see what coverage already exists.",
        ),
        anti_patterns=(
            "Blindly overwriting files without reading their current implementation.",
            "Creating duplicate disconnected modules that are never wired into the main system.",
        ),
        deliverable="List of target files, dependencies, and existing behavior.",
    ),
    "DESIGN": DisciplineSpec(
        id="software_architecture",
        name="Software Architecture & SOLID System Design",
        phase="DESIGN",
        purpose="Ensure clean module boundaries, SOLID principles, DRY code, and zero-dependency portability.",
        checklist=(
            "For non-trivial architecture tradeoffs, consult the Deep Reasoner via `workflow_manage(action='subagent', tier='reasoning', skill='deep-thinking', goal='...')`.",
            "Single Responsibility (SRP): Each module/class owns one cohesive responsibility.",
            "Open/Closed (OCP): Add tools, states, or rules via registries without rewriting core loops.",
            "Dependency Direction: Presentation -> Application -> Workflow -> Domain -> Infrastructure.",
            "Stdlib-First Portability: Use Python 3.12 standard library; avoid fragile external packages.",
            "Record significant structural choices as an Architecture Decision Record (ADR) in memory.",
        ),
        anti_patterns=(
            "Writing 'theater' modules (files that exist on disk but are never imported or executed).",
            "Circular imports or upward layer dependencies.",
        ),
        deliverable="Component design and ADR recorded in persistent memory.",
    ),
    "PLAN": DisciplineSpec(
        id="engineering_planning",
        name="Task Breakdown & Safety Checkpointing",
        phase="PLAN",
        purpose="Break the design into atomic, ordered implementation & verification tasks with a rollback checkpoint.",
        checklist=(
            "Create concrete tasks in the SQLite task tracker with priority and status.",
            "Ensure a safety checkpoint exists before modifying core source files.",
            "Include a specific verification/test step in the plan (use `tier='reasoning'` if multi-constraint sequencing is needed).",
        ),
        anti_patterns=(
            "Making sweeping multi-file edits without a checkpoint or task list.",
        ),
        deliverable="Ordered tasks in task tracker and an active safety checkpoint.",
    ),
    "IMPLEMENT": DisciplineSpec(
        id="clean_implementation",
        name="Clean, Resilient Software Implementation",
        phase="IMPLEMENT",
        purpose="Write clean, UTF-8 safe, Windows-compatible Python code with automatic syntax verification.",
        checklist=(
            "Use edit_file for targeted changes and write_file for new modules.",
            "Ensure every modified module is properly wired/imported where needed.",
            "Use run_python for executing Python snippets (avoid bash heredocs << EOF on Windows).",
            "Write or update unit tests in tests/ alongside new functionality.",
        ),
        anti_patterns=(
            "Using Linux-only shell constructs (<< EOF, grep, head) in Windows cmd.exe.",
            "Repeating an identical failing tool call without changing approach.",
        ),
        deliverable="Syntactically valid, integrated source code and corresponding unit tests.",
    ),
    "VERIFY": DisciplineSpec(
        id="quality_assurance",
        name="Deterministic QA, Testing & System Doctor Verification",
        phase="VERIFY",
        purpose="Prove that the implementation works deterministically via automated tests and health checks.",
        checklist=(
            "Run system_doctor or unit tests to verify all checks pass.",
            "Verify that no regressions were introduced in existing tests.",
            "Verify that every acceptance criterion from INTAKE is satisfied.",
        ),
        anti_patterns=(
            "Claiming a feature is complete without running tests or verifying integration.",
            "Ignoring test failures or non-zero exit codes.",
        ),
        deliverable="Passing system_doctor diagnostic and unit test report.",
    ),
    "RECOVER": DisciplineSpec(
        id="self_recovery",
        name="OODA Loop Self-Healing, Diagnostics & Rollback Recovery",
        phase="RECOVER",
        purpose="Apply the OODA Loop (Observe -> Orient -> Decide -> Act) to recover from errors or failed tests without losing system stability.",
        checklist=(
            "OBSERVE (`tier='fast'`): Inspect the exact error message, failing test traceback, or Waterfall trace.",
            "ORIENT & DECIDE (`tier='reasoning'`): Call `workflow_manage(action='subagent', tier='reasoning', skill='ooda-troubleshooting', goal='...')` or run recipe `ooda-diagnose` to isolate root cause.",
            "ACT (`tier='fast'`): If a core file was corrupted or tests fail catastrophically, rollback via `checkpoint_manage`, apply the minimal fix, and re-verify.",
            "Log the root cause in system notes so the same mistake is never repeated.",
        ),
        anti_patterns=(
            "Jumping straight from Observe to Act (guessing and editing files before orienting on root cause).",
            "Leaving broken code on disk or panicking when a tool returns an error.",
        ),
        deliverable="Restored healthy system state and documented root cause.",
    ),
    "REFLECT": DisciplineSpec(
        id="continuous_learning",
        name="Knowledge Persistence & Executive Reporting",
        phase="REFLECT",
        purpose="Persist lessons learned, sync MEMORY.md, close completed tasks, and report clearly to the user.",
        checklist=(
            "Mark completed tasks/backlog items as 'completed' in SQLite memory.",
            "Record key decisions or lessons learned and sync MEMORY.md.",
            "Summarize what was built, verified, and what is next in plain, friendly language for the user.",
        ),
        anti_patterns=(
            "Leaving tasks marked 'pending' after finishing them.",
            "Forgetting to update persistent memory before ending the workflow.",
        ),
        deliverable="Synchronized MEMORY.md, updated task statuses, and clear executive summary.",
    ),
}


@dataclass
class CodeAnalysisResult:
    """Result of static AST analysis on a Python source file."""

    path: str
    valid_syntax: bool
    syntax_error: Optional[str] = None
    line_count: int = 0
    has_module_docstring: bool = False
    functions: List[str] = field(default_factory=list)
    classes: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _is_abstract_class_or_fn(cls_node: Optional[ast.ClassDef], fn_node: ast.AST) -> bool:
    for dec in getattr(fn_node, "decorator_list", ()):
        dec_name = dec.id if isinstance(dec, ast.Name) else (dec.attr if isinstance(dec, ast.Attribute) else "")
        if dec_name in ("abstractmethod", "abstractclassmethod", "abstractstaticmethod", "abstractproperty"):
            return True
    if cls_node is not None:
        for base in cls_node.bases:
            bname = base.id if isinstance(base, ast.Name) else (base.attr if isinstance(base, ast.Attribute) else "")
            if bname in ("ABC", "Protocol"):
                return True
        for kw in cls_node.keywords:
            kval = kw.value.id if isinstance(kw.value, ast.Name) else (kw.value.attr if isinstance(kw.value, ast.Attribute) else "")
            if kw.arg == "metaclass" and kval == "ABCMeta":
                return True
    return False


def find_non_abstract_stubs(content: str, suffix: str = ".py") -> List[Tuple[int, str]]:
    """Detect unfinished stubs while exempting valid Python `ABC` / `Protocol` / `@abstractmethod` `NotImplementedError`s."""
    hits: List[Tuple[int, str]] = []
    exempt_lines: set[int] = set()
    if suffix.lower() == ".py" and "NotImplementedError" in content:
        try:
            tree = ast.parse(content)
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_abstract_class_or_fn(None, node):
                    exempt_lines.update(range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1))
                elif isinstance(node, ast.ClassDef):
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_abstract_class_or_fn(node, sub):
                            exempt_lines.update(range(sub.lineno, getattr(sub, "end_lineno", sub.lineno) + 1))
        except SyntaxError:
            pass
    for rx, label in STUB_PATTERNS:
        for m in rx.finditer(content):
            lineno = content[: m.start()].count("\n") + 1
            if "NotImplementedError" in label and lineno in exempt_lines:
                continue
            hits.append((lineno, label))
    return hits


def validate_javascript_syntax(
    js_source: str, label: str = "<script>", check_pythonisms: bool = False, is_module: bool = False,
) -> Optional[str]:
    """Validate JavaScript syntax via `node --check` (plus Companion-internal Pythonism guards when `check_pythonisms=True`)."""
    code = (js_source or "").strip()
    if not code:
        return None
    if check_pythonisms:
        m_slice = _PY_SLICE_IN_JS_RE.search(code)
        if m_slice:
            return f"JavaScript SyntaxError in `{label}`: Python slice syntax `{m_slice.group(0)}` is invalid in JS (use `.slice(...)`)."
        m_bool = _PY_BOOL_IN_JS_TPL_RE.search(code)
        if m_bool:
            return f"JavaScript SyntaxError in `{label}`: Python `or`/`and` inside `{m_bool.group(0)[:60]}` is invalid in JS (use `||` / `&&`)."
    node_bin = shutil.which("node")
    if not node_bin:
        return None
    tmp_path = ""
    try:
        ext = ".mjs" if is_module else ".js"
        with tempfile.NamedTemporaryFile("w", suffix=ext, delete=False, encoding="utf-8") as tmp_f:
            tmp_f.write(code)
            tmp_path = tmp_f.name
        proc = subprocess.run(
            [node_bin, "--check", tmp_path],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if proc.returncode != 0:
            err_lines = [ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip() and not ln.strip().startswith("at ")]
            short_err = " | ".join(err_lines[1:4] if len(err_lines) > 1 else err_lines[:3])
            return f"JavaScript SyntaxError in `{label}`: {short_err}"
    except Exception:
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return None


def validate_embedded_scripts(content: str, filename: str = "<script>", check_pythonisms: bool = False) -> Optional[str]:
    """Validate standalone `.js`/`.mjs` source or executable `<script>` blocks inside `.html` (skipping JSON-LD/importmap)."""
    low = filename.lower()
    if low.endswith((".js", ".mjs", ".cjs")):
        return validate_javascript_syntax(content, filename, check_pythonisms=check_pythonisms, is_module=low.endswith(".mjs"))
    for idx, match in enumerate(_SCRIPT_TAG_RE.finditer(content or ""), 1):
        attrs_raw, body = match.group(1) or "", match.group(2) or ""
        if _SCRIPT_SRC_ATTR_RE.search(attrs_raw):
            continue
        tm = _SCRIPT_TYPE_ATTR_RE.search(attrs_raw)
        stype = ((tm.group(1) or tm.group(2) or tm.group(3) or "") if tm else "").strip().lower().split(";", 1)[0].strip()
        if stype not in _JS_SCRIPT_TYPES:
            continue
        err = validate_javascript_syntax(body, f"{filename}#script[{idx}]", check_pythonisms=check_pythonisms, is_module=(stype == "module"))
        if err:
            return err
    return None


def _check_python_embedded_js(tree: ast.Module, filename: str) -> Optional[str]:
    norm = filename.replace("\\", "/")
    if "/tests/" in f"/{norm}" or norm.startswith("tests/"):
        return None
    is_self_surface = "/surfaces/" in f"/{norm}" or norm.startswith("surfaces/")
    if not is_self_surface:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            val = node.value.value
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    if tgt.id.endswith(("_SCRIPT", "_JS")):
                        err = validate_javascript_syntax(val, f"{norm}:{tgt.id}", check_pythonisms=True)
                        if err:
                            return err
                    elif tgt.id.endswith(("_HTML", "_TEMPLATE")) and "<script" in val.lower():
                        err = validate_embedded_scripts(val, f"{norm}:{tgt.id}", check_pythonisms=True)
                        if err:
                            return err
    return None


def analyze_python_source(source: str, filename: str = "<string>") -> CodeAnalysisResult:
    """Parse Python source with AST and check syntax, embedded JS scripts, structure, and docstrings."""
    lines = source.splitlines()
    result = CodeAnalysisResult(path=filename, valid_syntax=True, line_count=len(lines))
    try:
        tree = ast.parse(source, filename=filename)
        compile(tree, filename=filename, mode="exec")
    except SyntaxError as exc:
        result.valid_syntax = False
        err_line = lines[exc.lineno - 1].strip() if exc.lineno and 1 <= exc.lineno <= len(lines) else ""
        snippet = f" -> `{err_line[:120]}`" if err_line else ""
        result.syntax_error = f"{type(exc).__name__} at line {exc.lineno}, col {exc.offset}: {exc.msg}{snippet}"
        return result
    except Exception as exc:
        result.valid_syntax = False
        result.syntax_error = f"{type(exc).__name__}: {exc}"
        return result

    js_err = _check_python_embedded_js(tree, filename)
    if js_err:
        result.valid_syntax = False
        result.syntax_error = js_err
        return result

    result.has_module_docstring = ast.get_docstring(tree) is not None
    if not result.has_module_docstring and len(lines) > 10:
        result.warnings.append("Missing module-level docstring.")

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.functions.append(node.name)
        elif isinstance(node, ast.ClassDef):
            result.classes.append(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                result.imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                result.imports.append(node.module)

    return result


def analyze_python_file(path: Path) -> CodeAnalysisResult:
    """Analyze a Python file on disk."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return CodeAnalysisResult(path=str(path), valid_syntax=False, syntax_error=f"Could not read file: {exc}")
    return analyze_python_source(source, filename=str(path))


def walk_pruned_files(root: Path) -> List[Path]:
    """Return sorted regular files under `root` while pruning `SKIP_DIRS` in O(1) via `os.walk`."""
    if not root.is_dir():
        return []
    found: List[Path] = []
    for cur_root, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        base = Path(cur_root)
        for fn in sorted(files):
            cand = base / fn
            if cand.is_file():
                found.append(cand)
    found.sort()
    return found


def verify_repository_syntax(root: Path) -> Tuple[bool, List[CodeAnalysisResult]]:
    """Verify that every .py file (and Web UI `.js`/`.html` asset) in the repository has valid syntax."""
    all_files = walk_pruned_files(root)
    is_self = (root / "companion").is_dir()
    results = [analyze_python_file(p) for p in all_files if p.suffix.lower() == ".py"]
    for asset in (p for p in all_files if p.suffix.lower() in {".js", ".html"} and "surfaces" in p.parts):
        js_err = validate_embedded_scripts(asset.read_text(encoding="utf-8", errors="replace"), str(asset.relative_to(root)), check_pythonisms=is_self)
        if js_err:
            results.append(CodeAnalysisResult(path=str(asset), valid_syntax=False, syntax_error=js_err))
    return all(r.valid_syntax for r in results), results


GENERAL_TRACKED_EXTENSIONS: FrozenSet[str] = frozenset({
    ".py", ".pyi", ".md", ".mdx", ".json", ".jsonc", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf",
    ".html", ".htm", ".css", ".scss", ".less", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".txt", ".rst", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".sql", ".graphql", ".proto",
    ".csv", ".xml", ".svg", ".cs", ".csproj", ".sln", ".razor", ".go", ".mod", ".rs",
    ".java", ".kt", ".kts", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".rb", ".php", ".lua",
})


class ActionRiskTier(str, Enum):
    """Domain-neutral action risk classification (`R0`..`R3`) enforced at tool & DAG execution boundaries."""
    R0_READ_ONLY = "R0"
    R1_LOCAL_REVERSIBLE = "R1"
    R2_CAPABILITY_PROMOTION = "R2"
    R3_HIGH_IMPACT_MUTATION = "R3"


_R3_SHELL_RE = re.compile(
    r"(?:\b(?:ssh|scp|sftp|rsync)\s+\S+|\bgit\s+push\b|\b(?:npm|twine|cargo)\s+publish\b|"
    r"\b(?:curl|wget)\b[^\n]*(?:\b(?:-X|--request)\s*(?:POST|PUT|DELETE|PATCH)\b|(?:\s|^)(?:-d|--data|--data-raw|--data-binary|--json|-F|--form)\b)|"
    r"\bInvoke-(?:RestMethod|WebRequest)\b[^\n]*(?:-Method\s+(?:Post|Put|Delete|Patch)\b|-Body\b)|"
    r"\b(?:kill|pkill|killall|taskkill|Stop-Process)\b|"
    r"\bsystemctl\b(?!\s+(?:--[a-z0-9=-]+\s+|-[a-zA-Z]+\s+)*(?:status|list-(?:units|unit-files|timers|sockets|dependencies)|is-(?:active|enabled|failed|system-running)|show|cat|help)\b)|"
    r"\b(?:schtasks|reg\s+(?:add|delete)|netsh|sc\s+(?:create|delete|config))\b)",
    re.I,
)
_R3_PYTHON_RE = re.compile(
    r"(?:\b(?:urllib\.request\.urlopen|http\.client\.HTTPConnection|http\.client\.HTTPSConnection|socket\.create_connection|smtplib\.SMTP|ftplib\.FTP)\b|"
    r"\bsubprocess\.(?:run|Popen|call|check_call|check_output)\b|"
    r"\bos\.(?:system|popen|kill|killpg)\b|"
    r"\bshutil\.rmtree\s*\(\s*['\"](?:/|[A-Za-z]:\\|\.\.))",
)
_R0_ACTIONS: Dict[str, FrozenSet[str]] = {
    "checkpoint_manage": frozenset({"", "list", "git_log"}),
    "memory_manage": frozenset({"get_notes", "get_adrs", "get_profile", "search", "search_history", "recent_history", "query_facts", "read_fact"}),
    "task_manage": frozenset({"list_tasks", "list_backlog", "list_all"}),
    "sdd_manage": frozenset({"", "verify", "status", "read_spec", "get_adrs"}),
    "project_manage": frozenset({"", "status", "current", "list"}),
    "job_manage": frozenset({"", "list", "status"}),
    "wiki_manage": frozenset({"", "list", "tree", "read", "search", "graph", "diff", "list_stale", "list_trash", "list_templates", "read_template", "tag_authority", "inspect_graduation", "validate_curation", "curation_history"}),
    "vault_manage": frozenset({"", "list", "check_secret", "list_missing", "audit"}),
    "endpoint_manage": frozenset({"", "list", "test", "inspect_host", "ssh_read_file"}),
    "mcp_manage": frozenset({"", "list", "discover", "list_partners", "handshake_partner", "check_partner_job"}),
    "routine_manage": frozenset({"", "list", "autonomy_status", "morning", "list_approvals", "sandbox_list", "sandbox_diff", "prompt_list"}),
    "agile_manage": frozenset({"", "board_view", "list_items", "roadmap", "list_boards", "list_epics", "list_sprints", "capacity"}),
}


def classify_action_risk(tool_name: str, args: Optional[Dict[str, Any]] = None) -> ActionRiskTier:
    """Classify `(tool_name, args)` into `R0`..`R3` without domain-specific hardcoding."""
    payload, action = args or {}, str((args or {}).get("action") or (args or {}).get("subaction") or "").strip().lower()
    if tool_name in ("read_file", "list_dir", "search", "web_fetch", "web_search", "system_doctor"):
        return ActionRiskTier.R0_READ_ONLY
    if tool_name == "telemetry_manage":
        return ActionRiskTier.R0_READ_ONLY if action != "log_event" else ActionRiskTier.R1_LOCAL_REVERSIBLE
    if tool_name == "workflow_manage":
        return ActionRiskTier.R0_READ_ONLY if action in ("", "status", "list_dags", "list_recipes", "http_smoke", "check_skill") else (ActionRiskTier.R2_CAPABILITY_PROMOTION if action == "bootstrap_skill" else ActionRiskTier.R1_LOCAL_REVERSIBLE)
    if tool_name == "endpoint_manage":
        if action in ("ssh_exec", "winrm_exec", "propose", "register", "remove"):
            return ActionRiskTier.R3_HIGH_IMPACT_MUTATION
        if action == "api_request" and str(payload.get("method") or "GET").strip().upper() not in ("GET", "HEAD"):
            return ActionRiskTier.R3_HIGH_IMPACT_MUTATION
    if tool_name == "mcp_manage" and action in ("call_tool", "delegate_partner", "propose_server", "register_server", "register", "remove", "register_partner", "remove_partner"):
        return ActionRiskTier.R3_HIGH_IMPACT_MUTATION
    if tool_name == "mcp_manage" and action == "harvest_skill":
        return ActionRiskTier.R2_CAPABILITY_PROMOTION
    if tool_name == "run_shell" and _R3_SHELL_RE.search(str(payload.get("command") or "")):
        return ActionRiskTier.R3_HIGH_IMPACT_MUTATION
    if tool_name == "run_python":
        py_code = str(payload.get("code") or "")
        if _R3_SHELL_RE.search(py_code) or _R3_PYTHON_RE.search(py_code):
            return ActionRiskTier.R3_HIGH_IMPACT_MUTATION
    if tool_name in ("write_file", "edit_file") and (str(payload.get("path") or "").replace("\\", "/").startswith("skills/") or "/skills/" in str(payload.get("path") or "").replace("\\", "/")):
        return ActionRiskTier.R2_CAPABILITY_PROMOTION
    if tool_name == "project_manage" and not action and any(str(payload.get(k) or "").strip() for k in ("target", "path", "name")):
        return ActionRiskTier.R1_LOCAL_REVERSIBLE
    if tool_name in _R0_ACTIONS and action in _R0_ACTIONS[tool_name]:
        return ActionRiskTier.R0_READ_ONLY
    return ActionRiskTier.R1_LOCAL_REVERSIBLE


@dataclass(frozen=True)
class WorkspaceContract:
    """Pluggable workspace verification and rule contract (`CompanionSelfContract` vs `GeneralWorkspaceContract`)."""

    contract_id: str
    enforce_stdlib_only: bool
    enforce_sdd_and_doctor: bool
    tracked_extensions: FrozenSet[str]
    impl_rules_prompt: str


def resolve_workspace_contract(root: Path, is_self_project: bool = False) -> WorkspaceContract:
    """Return `CompanionSelfContract` for Companion core and `GeneralWorkspaceContract` for external projects."""
    resolved = root.resolve()
    if is_self_project or ((resolved / "companion").is_dir() and (resolved / "specs" / "ARCHITECTURE.md").is_file()):
        return WorkspaceContract("companion-self", True, True, GENERAL_TRACKED_EXTENSIONS, "Rules: Obey ADR-001/ADR-015 (100% Python stdlib in Ring-0 core; PEP 723 inline deps allowed in Ring-1 skills/<name>/scripts/*.py), ADR-005 (sync canonical `specs/` if adding core modules), and hygiene budgets (<=700L/module, <=100L/fn).")
    return WorkspaceContract("general-workspace", False, False, GENERAL_TRACKED_EXTENSIONS, f"Rules: Active workspace (`{resolved}`) — create/update the requested artifacts, configurations, scripts, or tests directly via `write_file`/`edit_file` and verify behavior empirically (`run_python`/`run_shell`). Do NOT call `sdd_manage` or inspect `.companion_backups`.")
