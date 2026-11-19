"""
Architecture Module — A-Frame Architecture Specification for Companion AI OS.

Defines the core architectural layers, component specifications, SOLID principles,
and boundary verification utilities.
"""

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from companion import __version__
from companion.disciplines import (
    ArchitectureLayer,
    LAYER_ORDER,
    MODULE_LAYER_MAP,
    check_module_layer_imports,
    verify_repository_syntax,
    walk_pruned_files,
)


@dataclass
class ComponentSpec:
    """Specification for a component within the A-Frame architecture."""

    name: str
    layer: ArchitectureLayer
    module_path: str
    description: str
    dependencies: List[str] = field(default_factory=list)
    optional: bool = False


@dataclass
class ArchitectureFrame:
    """The complete A-Frame Architecture definition."""

    name: str
    version: str = __version__
    layers: Dict[ArchitectureLayer, List[ComponentSpec]] = field(default_factory=dict)
    principles: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.layers:
            self.layers = {layer: [] for layer in ArchitectureLayer}


SOLID_PRINCIPLES: List[str] = [
    "Single Responsibility (SRP) - Each module and class has one cohesive responsibility",
    "Open/Closed (OCP) - Extend tools, code-controlled DAGs, domain validators, and TriggerPolicy prechecks via registries without modifying core loops",
    "Liskov Substitution (LSP) - Components and tool handlers adhere to uniform contracts",
    "Interface Segregation (ISP) - Small, focused tool schemas and clean module APIs",
    "Dependency Inversion (DIP) - High-level policies depend on clean abstractions and downward layer flow",
    "Living Spec-Driven Development (SDD) - Strictly 4 canonical spec files in specs/ verified bidirectionally against code",
    "AST Dead-Code Hygiene - Zero unused imports, zero unreferenced definitions, and strict line budgets",
]

CORE_COMPONENTS: List[ComponentSpec] = [
    # Left Leg Base: Infrastructure Layer
    ComponentSpec(
        name="Configuration & .env Loader",
        layer=ArchitectureLayer.INFRASTRUCTURE,
        module_path="companion/config.py",
        description="Zero-dependency .env parser and validated Settings singleton",
        dependencies=[],
    ),
    ComponentSpec(
        name="Unified Telemetry, Waterfall Tracer & KPI Engine",
        layer=ArchitectureLayer.INFRASTRUCTURE,
        module_path="companion/telemetry.py",
        description="Spine-level SQLite waterfall trace spans, structured app logs (DEBUG/INFO/WARN/ERROR), and KPI scorecard",
        dependencies=["Configuration & .env Loader"],
    ),
    ComponentSpec(
        name="Live SSE & Stream Event Bus",
        layer=ArchitectureLayer.INFRASTRUCTURE,
        module_path="companion/streaming.py",
        description="ContextVar token/thought/phase/tool stream event bus and OpenAI SSE stream assembler",
        dependencies=[],
    ),
    ComponentSpec(
        name="Multi-Dialect LLM Wire Translators",
        layer=ArchitectureLayer.INFRASTRUCTURE,
        module_path="companion/llm_wire.py",
        description="Pure-stdlib wire request/SSE translators for OpenAI-compatible, Anthropic Messages, and Gemini Native v1beta APIs plus LAN privacy helpers",
        dependencies=["Configuration & .env Loader", "Live SSE & Stream Event Bus"],
    ),
    ComponentSpec(
        name="LLM HTTP Client",
        layer=ArchitectureLayer.INFRASTRUCTURE,
        module_path="companion/llm.py",
        description="Resilient multi-dialect LLM client with live SSE streaming, ModelGate, model discovery, LAN privacy lock, and failover",
        dependencies=["Configuration & .env Loader", "Live SSE & Stream Event Bus", "Multi-Dialect LLM Wire Translators"],
    ),
    # Right Leg Base: Domain Layer
    ComponentSpec(
        name="Hybrid Semantic & RRF Memory Engine",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/semantic_memory.py",
        description="Zero-swap morphological/concept-cluster sparse cosine similarity, optional dense vectors, and Reciprocal Rank Fusion",
        dependencies=["Configuration & .env Loader"],
    ),
    ComponentSpec(
        name="Workspace Checkpoints & Atomic Backups",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/checkpoints.py",
        description="WorkspaceContext-bound file backups, full snapshot checkpoints, rollback restoration, and post-exec syntax healing",
        dependencies=["Configuration & .env Loader", "Engineering Disciplines & Validators"],
    ),
    ComponentSpec(
        name="Safety, Inline Micro-Linter & System Doctor",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/safety.py",
        description="ADR-007 WorkspaceContext path lock, <5ms inline AST/hygiene/layer micro-linter, LoopGuard, and 7-stage doctor",
        dependencies=["Configuration & .env Loader", "Workspace Checkpoints & Atomic Backups"],
    ),
    ComponentSpec(
        name="Persistent Memory, ADR & Autonomy Store",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/memory_store.py",
        description="SQLite engine for conversations, Universal Project & Goal Graph tables, ADRs, user profile, workflows, and autonomy journal",
        dependencies=["Configuration & .env Loader"],
    ),
    ComponentSpec(
        name="Backward-Compatible ChatMemory Facade",
        layer=ArchitectureLayer.DOMAIN,
        module_path="memory/chat_memory.py",
        description="Backward-compatible ChatMemory facade over companion/memory_store.py",
        dependencies=["Persistent Memory, ADR & Autonomy Store"],
    ),
    ComponentSpec(
        name="Unified TriggerPolicy & Precheck Registry",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/trigger_policy.py",
        description="Unified manual/scheduled/on_idle TriggerPolicy, overnight time-slot window math, cron evaluator, and <5ms zero-token prechecks",
        dependencies=["Configuration & .env Loader", "Persistent Memory, ADR & Autonomy Store"],
    ),
    ComponentSpec(
        name="Engineering Disciplines & Validators",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/disciplines.py",
        description="Software engineering, architecture, QA playbooks, and AST static code analyzers",
        dependencies=[],
    ),
    ComponentSpec(
        name="AST Code Hygiene & Dead-Code Scanner",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/hygiene.py",
        description="Deterministic AST scanner for unused imports, dead definitions, orphan files, and size budgets",
        dependencies=["Configuration & .env Loader"],
    ),
    ComponentSpec(
        name="Living Canonical SDD Engine",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/sdd.py",
        description="Enforces 4-file canonical specs/ pyramid, bidirectional Spec-Code drift checks, and ADR compaction",
        dependencies=["Persistent Memory, ADR & Autonomy Store", "Engineering Disciplines & Validators"],
    ),
    ComponentSpec(
        name="Epistemic Grounding, KnowledgePort & ExecutionTarget",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/grounding.py",
        description="Pluggable KnowledgePort adapter seam, <10ms epistemic grounding HUD, Top-K skill ranking, and isolated ExecutionTarget",
        dependencies=[
            "Configuration & .env Loader",
            "Persistent Memory, ADR & Autonomy Store",
            "Workspace Checkpoints & Atomic Backups",
            "AST Code Hygiene & Dead-Code Scanner",
        ],
    ),
    ComponentSpec(
        name="Forever-Chat Cold Archive, Approvals Queue, Trust Modes & Prompts",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/archive_and_approvals.py",
        description="Hot/Cold Forever Chat archive, fingerprint-verified ApprovalQueue, 3-tier Trust Modes, session nudges, and Saved Prompts",
        dependencies=["Persistent Memory, ADR & Autonomy Store", "Hybrid Semantic & RRF Memory Engine"],
    ),
    ComponentSpec(
        name="Zero-Reveal Blind Credential Vault & Snapshot Backups",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/vault.py",
        description="Owner-locked write-only secrets.env Vault, JIT connector resolver, output scrubber, and secret-free snapshot backups",
        dependencies=["Configuration & .env Loader", "Forever-Chat Cold Archive, Approvals Queue, Trust Modes & Prompts"],
    ),
    ComponentSpec(
        name="Governed Wiki Templates, YAML & Ontology Authority",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/wiki_templates.py",
        description="11 built-in Wiki templates, pure-stdlib YAML frontmatter parser/serializer, tag/ontology authority, and strict curated note validator",
        dependencies=["Persistent Memory, ADR & Autonomy Store"],
    ),
    ComponentSpec(
        name="Co-Owned 4-Folder Knowledge Wiki Engine 2.0",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/wiki_engine.py",
        description="Plain-Markdown 4-folder Warehouse (01_inbox..04_archive), Dual-Author guard, 2-hop ontology bridges, proxy notes, and 2D Mind Map sidecar",
        dependencies=["Configuration & .env Loader", "Forever-Chat Cold Archive, Approvals Queue, Trust Modes & Prompts", "Governed Wiki Templates, YAML & Ontology Authority"],
    ),
    ComponentSpec(
        name="Multi-Turn Wiki Curation & Lineage Engine",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/wiki_curation.py",
        description="Deliberate multi-turn Inbox-to-Knowledge overlap audit, mandatory full-read gate, 4 curation strategies, Never-Delete archive lineage, and 1-click revert",
        dependencies=["Co-Owned 4-Folder Knowledge Wiki Engine 2.0", "Governed Wiki Templates, YAML & Ontology Authority", "Hybrid Semantic & RRF Memory Engine"],
    ),
    ComponentSpec(
        name="Wiki 3.1 Diffs, Ontology Consolidation, Freshness & Asset Engine",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/wiki_maintenance.py",
        description="Unified & side-by-side lineage/approval diffs, ontology synonym consolidation, staleness cadences, inline diagram assets, and 1-click chat-turn staging",
        dependencies=["Co-Owned 4-Folder Knowledge Wiki Engine 2.0", "Governed Wiki Templates, YAML & Ontology Authority", "Multimodal Chat Attachments & Document Extractor", "Forever-Chat Cold Archive, Approvals Queue, Trust Modes & Prompts"],
    ),
    ComponentSpec(
        name="Multimodal Chat Attachments & Document Extractor",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/attachments.py",
        description="Stdlib image/Office/data/code/PDF attachment store with Vault output scrubbing, Wiki 03_resources overflow, and vision wire URIs",
        dependencies=["Configuration & .env Loader", "Persistent Memory, ADR & Autonomy Store", "Zero-Reveal Blind Credential Vault & Snapshot Backups", "Co-Owned 4-Folder Knowledge Wiki Engine 2.0"],
    ),
    ComponentSpec(
        name="Universal Headless Project & Goal Graph",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/project_graph.py",
        description="Domain-blind Universal Project & Goal Graph (projects, milestones, timeboxes, work_items with owner, depends_on, DIKW stage_kind, and TriggerPolicy)",
        dependencies=["Configuration & .env Loader", "Persistent Memory, ADR & Autonomy Store", "Unified TriggerPolicy & Precheck Registry"],
    ),
    ComponentSpec(
        name="Headless Education & Mastery Engine",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/education_engine.py",
        description="Deterministic course syllabi, 4-stage Socratic pedagogy, SM-2 spaced repetition, Wiki study note curation, and inline Chat flashcard decks",
        dependencies=["Persistent Memory, ADR & Autonomy Store", "Co-Owned 4-Folder Knowledge Wiki Engine 2.0", "Governed Wiki Templates, YAML & Ontology Authority"],
    ),
    ComponentSpec(
        name="Multi-Provider LLM Registry & Capability Tier Matrix",
        layer=ArchitectureLayer.DOMAIN,
        module_path="companion/model_registry.py",
        description="12-preset local/frontier LLM provider registry, Zero-Reveal Blind Vault API key binding, hot-reload 8-slot capability tier matrix, and LAN privacy lock",
        dependencies=["Configuration & .env Loader", "Multi-Dialect LLM Wire Translators", "LLM HTTP Client", "Persistent Memory, ADR & Autonomy Store", "Zero-Reveal Blind Credential Vault & Snapshot Backups"],
    ),
    # Crossbar: Deterministic Workflow Graph Layer
    ComponentSpec(
        name="Code-Controlled Deterministic DAG Runtime",
        layer=ArchitectureLayer.WORKFLOW,
        module_path="companion/dag_runtime.py",
        description="Pure-stdlib typed Python DAGSpec runtime with foreach fan-out/fan-in joins, retries/timeouts, SQLite node checkpoints, and mid-DAG R3/human_gate pause/resume",
        dependencies=["Persistent Memory, ADR & Autonomy Store", "Forever-Chat Cold Archive, Approvals Queue, Trust Modes & Prompts", "Engineering Disciplines & Validators"],
    ),
    ComponentSpec(
        name="Dynamic GoalPlan DAG & Pluggable VerificationSpec Engine",
        layer=ArchitectureLayer.WORKFLOW,
        module_path="companion/workflow_dag.py",
        description="Topological GoalPlan DAG, pluggable domain_validator VerificationSpec registry, and ExecutorBindings",
        dependencies=["Epistemic Grounding, KnowledgePort & ExecutionTarget", "Persistent Memory, ADR & Autonomy Store", "Engineering Disciplines & Validators", "Workspace Checkpoints & Atomic Backups", "Safety, Inline Micro-Linter & System Doctor"],
    ),
    ComponentSpec(
        name="Deterministic Workflow State Machine",
        layer=ArchitectureLayer.WORKFLOW,
        module_path="companion/workflow_graph.py",
        description="Directed graph workflow engine (INTAKE -> DISCOVER -> DESIGN -> PLAN -> IMPLEMENT -> VERIFY -> REFLECT) with phase gates and code-controlled DAG dispatch",
        dependencies=["Code-Controlled Deterministic DAG Runtime", "Dynamic GoalPlan DAG & Pluggable VerificationSpec Engine", "Persistent Memory, ADR & Autonomy Store", "Engineering Disciplines & Validators", "Workspace Checkpoints & Atomic Backups", "Safety, Inline Micro-Linter & System Doctor"],
    ),
    # Upper A-Frame: Application Layer
    ComponentSpec(
        name="Cognitive Triage Gate & Tier Router",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/intake.py",
        description="Deterministic-guarded Cognitive Triage (DIRECT_TURN, EXECUTE_VERIFIED_DAG, GOAL_ALIGN_PLAN, ADVANCE_ACTIVE_GOAL), reasoning tier routing, and HTTP smoke probes",
        dependencies=["Configuration & .env Loader", "Epistemic Grounding, KnowledgePort & ExecutionTarget", "Universal Headless Project & Goal Graph", "Code-Controlled Deterministic DAG Runtime"],
    ),
    ComponentSpec(
        name="Transactional Skill & DAG Forge + REFLECT Distiller",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/skill_forge.py",
        description="Make-vs-borrow check, isolated staging/linting/--self-test promotion for scripts & code-controlled DAGs, and REFLECT fact distillation",
        dependencies=["Epistemic Grounding, KnowledgePort & ExecutionTarget", "Code-Controlled Deterministic DAG Runtime", "Living Canonical SDD Engine", "Safety, Inline Micro-Linter & System Doctor"],
    ),
    ComponentSpec(
        name="Lossless Compaction & Phase Handoff Engine",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/compaction.py",
        description="KeyTokens-verified 2-stage conversation compaction and lossless multi-phase handoff builder",
        dependencies=["Configuration & .env Loader", "Persistent Memory, ADR & Autonomy Store"],
    ),
    ComponentSpec(
        name="Rich Tool Schemas & Phase Tool Gate",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/tool_schemas.py",
        description="26 rich OpenAI tool schemas with per-parameter descriptions and mechanical PHASE_ALLOWED_TOOLS gating",
        dependencies=[],
    ),
    ComponentSpec(
        name="OS State & Governance Tools",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/os_tools.py",
        description="Companion OS state, memory, task, workflow, DAG, checkpoint, SDD, project, telemetry, web_search, and doctor tool handlers",
        dependencies=["Persistent Memory, ADR & Autonomy Store", "Deterministic Workflow State Machine", "Code-Controlled Deterministic DAG Runtime", "Safety, Inline Micro-Linter & System Doctor"],
    ),
    ComponentSpec(
        name="Tool Registry & Safe Dispatch",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/tools.py",
        description="26 deterministic tools covering WorkspaceContext I/O, AST symbol reads, inline linting, Python/shell runners, native MCP schemas, and phase-gated dispatch",
        dependencies=["Configuration & .env Loader", "OS State & Governance Tools", "Safety, Inline Micro-Linter & System Doctor"],
    ),
    ComponentSpec(
        name="Dynamic Context & Compaction Engine",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/context.py",
        description="Builds identity-aware system prompts with live State Machine & Project Graph HUD and compacts long conversation histories",
        dependencies=["Persistent Memory, ADR & Autonomy Store", "Universal Headless Project & Goal Graph", "Deterministic Workflow State Machine"],
    ),
    ComponentSpec(
        name="Executive Agent Loop",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/agent.py",
        description="Resilient tool-calling loop with LoopGuard, auto-logging, iteration budget warnings, and forced final synthesis",
        dependencies=["LLM HTTP Client", "Tool Registry & Safe Dispatch", "Dynamic Context & Compaction Engine", "Safety, Inline Micro-Linter & System Doctor"],
    ),
    ComponentSpec(
        name="Multi-Phase Autonomous Engineering Driver",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/autonomy.py",
        description="Continuous multi-phase SDD build driver with mechanical tier routing, anti-stub scanning, HTTP smoke probes, and doctor gates",
        dependencies=["LLM HTTP Client", "Deterministic Workflow State Machine", "Safety, Inline Micro-Linter & System Doctor"],
    ),
    ComponentSpec(
        name="Interactive Blueprint Approval Gate",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/blueprint.py",
        description="Plain-English Plan -> Approve -> Build steering wheel pausing workflows at PLAN with read-only tool locks until approved",
        dependencies=["Multi-Phase Autonomous Engineering Driver", "Deterministic Workflow State Machine"],
    ),
    ComponentSpec(
        name="Live Model Flight Evaluation Harness",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/eval_harness.py",
        description="Isolated multi-flight behavioral benchmark suite testing local models against AST symbol reads, inline linting, and Blueprint builds",
        dependencies=[
            "Interactive Blueprint Approval Gate",
            "Safety, Inline Micro-Linter & System Doctor",
        ],
    ),
    ComponentSpec(
        name="Forever-Chat Background Job Dispatcher",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/background_jobs.py",
        description="Zero-Identity background workers under ModelGate priority with <=200-token Wiki Inbox overflow and crash recovery",
        dependencies=["LLM HTTP Client", "Co-Owned 4-Folder Knowledge Wiki Engine 2.0", "Forever-Chat Cold Archive, Approvals Queue, Trust Modes & Prompts"],
    ),
    ComponentSpec(
        name="Universal SSH, WinRM & HTTP Endpoint Connector",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/endpoints.py",
        description="Symbolic-only multi-protocol remote connector with JIT Blind Vault injection, SSRF guards, and host hardware profiling",
        dependencies=["Zero-Reveal Blind Credential Vault & Snapshot Backups", "Safety, Inline Micro-Linter & System Doctor"],
    ),
    ComponentSpec(
        name="Federated MCP Hub & Partner-Aware Federation",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/mcp_hub.py",
        description="Outbound stdio/HTTP MCP client with fingerprint approval, inbound Order Counter MCP server, and Paired Partner HUD",
        dependencies=["Forever-Chat Background Job Dispatcher", "Zero-Reveal Blind Credential Vault & Snapshot Backups", "Co-Owned 4-Folder Knowledge Wiki Engine 2.0"],
    ),
    ComponentSpec(
        name="Unified TriggerPolicy Routines, Idle Autonomy & Clone Sandboxes",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/routines.py",
        description="Unified scheduled & on_idle routines (retiring hobbies), IdleAutonomyWatcher, Morning Briefing, downtime reconciliation, and isolated self-dev clone sandboxes",
        dependencies=["Unified TriggerPolicy & Precheck Registry", "Universal Headless Project & Goal Graph", "Forever-Chat Background Job Dispatcher", "Universal SSH, WinRM & HTTP Endpoint Connector", "Zero-Reveal Blind Credential Vault & Snapshot Backups"],
    ),
    ComponentSpec(
        name="Unified Multi-Source Calendar Engine",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/calendar_engine.py",
        description="Normalized Month/Week/Agenda timeline merging appointments, active timeboxes, work-item planned/due dates, scheduled routines, and due SRS study reviews",
        dependencies=["Universal Headless Project & Goal Graph", "Headless Education & Mastery Engine", "Unified TriggerPolicy Routines, Idle Autonomy & Clone Sandboxes"],
    ),
    ComponentSpec(
        name="Shared Core Action Bus",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/core_actions.py",
        description="Unified actor-aware CORE_ACTIONS registry shared across CLI, REPL slash commands, Web API, and LLM tools",
        dependencies=["Forever-Chat Background Job Dispatcher", "Co-Owned 4-Folder Knowledge Wiki Engine 2.0", "Multi-Turn Wiki Curation & Lineage Engine", "Universal SSH, WinRM & HTTP Endpoint Connector", "Federated MCP Hub & Partner-Aware Federation", "Unified TriggerPolicy Routines, Idle Autonomy & Clone Sandboxes", "Universal Headless Project & Goal Graph", "Unified Multi-Source Calendar Engine", "Headless Education & Mastery Engine"],
    ),
    ComponentSpec(
        name="Companion 3.0 Harvest LLM Tool Adapters",
        layer=ArchitectureLayer.APPLICATION,
        module_path="companion/harvest_tools.py",
        description="Thin actor='companion' adapters exposing job_manage, wiki_manage, vault_manage, endpoint_manage, mcp_manage, routine_manage, agile_manage, calendar_manage, and education_manage",
        dependencies=["Shared Core Action Bus"],
    ),
    # Apex: Presentation Layer
    ComponentSpec(
        name="CLI, REPL & Non-Blocking Idle Watcher",
        layer=ArchitectureLayer.PRESENTATION,
        module_path="harness.py",
        description="Self-recovering entry point supporting REPL slash commands, paste coalescing, IdleAutonomyWatcher, and diagnostic flags",
        dependencies=["Executive Agent Loop", "Unified TriggerPolicy Routines, Idle Autonomy & Clone Sandboxes"],
    ),
    ComponentSpec(
        name="Zero-Dependency Responsive Web UI Assets",
        layer=ArchitectureLayer.PRESENTATION,
        module_path="surfaces/web_assets.py",
        description="Mobile-first responsive dark-mode HTML5/CSS3/ES2022 interface for LAN browser access with zero external dependencies",
        dependencies=[],
    ),
    ComponentSpec(
        name="Headless Web API Extensions & Hosted MCP Route Adapter",
        layer=ArchitectureLayer.PRESENTATION,
        module_path="surfaces/api_extensions.py",
        description="Exposes GET/POST /api/actions and Bearer-authenticated /api/mcp Order Counter on the LAN server",
        dependencies=["Shared Core Action Bus", "Federated MCP Hub & Partner-Aware Federation"],
    ),
    ComponentSpec(
        name="Stdlib LAN HTTP Server & Session Controller",
        layer=ArchitectureLayer.PRESENTATION,
        module_path="surfaces/web_server.py",
        description="Threaded Python stdlib HTTP server binding 127.0.0.1 by default (or token-gated LAN) with WAL-isolated status/history/telemetry APIs and foreground chat/goal execution",
        dependencies=[
            "Executive Agent Loop",
            "Multi-Phase Autonomous Engineering Driver",
            "Zero-Dependency Responsive Web UI Assets",
        ],
    ),
    ComponentSpec(
        name="Desktop System Tray Launcher & Port Guard",
        layer=ArchitectureLayer.PRESENTATION,
        module_path="surfaces/windows_tray.py",
        description="Zero-dependency Win32 ctypes System Tray controller, single-instance health check, and graceful port-conflict diagnostics",
        dependencies=[
            "Configuration & .env Loader",
            "Stdlib LAN HTTP Server & Session Controller",
        ],
    ),
]


def get_architecture_frame() -> ArchitectureFrame:
    """Return a fresh ArchitectureFrame populated with all registered core components."""
    frame = ArchitectureFrame(
        name="Companion AI OS (A-Frame Architecture)",
        version=__version__,
        principles=list(SOLID_PRINCIPLES),
    )
    for comp in CORE_COMPONENTS:
        frame.layers.setdefault(comp.layer, []).append(comp)
    return frame


def _check_layer_dependencies(root: Path) -> List[str]:
    """Verify downward-only A-Frame layer dependencies across all production Python modules."""
    violations: List[str] = []
    for py_file in walk_pruned_files(root):
        if py_file.suffix.lower() != ".py":
            continue
        rel = str(py_file.relative_to(root)).replace("\\", "/")
        if rel.startswith(("tests/", "skills/")):
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"), filename=rel)
        except Exception:
            continue
        for src_mod, src_layer, tgt_mod, tgt_layer, _lineno in check_module_layer_imports(tree, rel):
            violations.append(f"{src_mod} ({src_layer.value}) -> {tgt_mod} ({tgt_layer.value})")
    return violations


def verify_architecture(root: Optional[Path] = None) -> Dict[str, object]:
    """Verify that all registered architectural components exist, obey downward layer flow, and pass syntax checks."""
    if root is None:
        root = Path(__file__).resolve().parent.parent
    frame = get_architecture_frame()
    missing_files: List[str] = []
    present_files: List[str] = []
    known_names = {c.name for c in CORE_COMPONENTS}
    registered_paths = {c.module_path for c in CORE_COMPONENTS}

    for comp in CORE_COMPONENTS:
        full_path = root / comp.module_path
        if full_path.is_file():
            present_files.append(comp.module_path)
        else:
            missing_files.append(comp.module_path)
        dotted = comp.module_path.removesuffix(".py").replace("/", ".")
        if dotted not in MODULE_LAYER_MAP:
            missing_files.append(f"unmapped:{dotted}")
        for dep_name in comp.dependencies:
            if dep_name not in known_names:
                missing_files.append(f"unknown_dep:{comp.name}->{dep_name}")

    for pkg_name in ("companion", "surfaces", "memory"):
        pkg_dir = root / pkg_name
        if pkg_dir.is_dir():
            for py_mod in sorted(pkg_dir.glob("*.py")):
                if py_mod.name == "__init__.py":
                    continue
                rel = f"{pkg_name}/{py_mod.name}"
                if rel not in registered_paths:
                    missing_files.append(f"unregistered:{rel}")

    layer_violations = _check_layer_dependencies(root)
    syntax_ok, syntax_results = verify_repository_syntax(root)
    invalid_files = [r.path for r in syntax_results if not r.valid_syntax]

    return {
        "valid": len(missing_files) == 0 and len(layer_violations) == 0 and syntax_ok,
        "architecture_name": frame.name,
        "version": frame.version,
        "component_count": len(CORE_COMPONENTS),
        "present_components": present_files,
        "missing_components": missing_files,
        "layer_violations": layer_violations,
        "syntax_valid": syntax_ok,
        "invalid_syntax_files": invalid_files,
    }


def format_architecture_summary() -> str:
    """Return a human-readable summary of the A-Frame Architecture."""
    frame = get_architecture_frame()
    lines = [f"=== {frame.name} v{frame.version} ===", "Core Principles:"]
    lines.extend(f"  * {p}" for p in frame.principles)
    lines.append("")

    ordered_layers = sorted(ArchitectureLayer, key=lambda l: LAYER_ORDER.get(l, 99))
    for layer in ordered_layers:
        components = frame.layers.get(layer, [])
        if not components:
            continue
        lines.append(f"[{layer.value.upper()} LAYER]")
        for comp in components:
            lines.append(f"  - {comp.name} ({comp.module_path}): {comp.description}")
            if comp.dependencies:
                lines.append(f"    Dependencies: {', '.join(comp.dependencies)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def print_architecture() -> None:
    """Print the architecture frame in a readable format."""
    print(format_architecture_summary())


COMPANION_ARCHITECTURE = get_architecture_frame()


if __name__ == "__main__":
    print_architecture()
