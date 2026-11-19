"""
Rich OpenAI Tool Schemas & Phase-Scoped `AllowedTools` Gating (A-Frame Application Layer).

Provides:
1. All 28 deterministic tool schemas with explicit 1-line parameter descriptions so local
   open-weight models (`Qwen`, `DeepSeek`, `Llama`, `Mistral`) never guess argument semantics.
2. Mechanical `PHASE_ALLOWED_TOOLS` & fine-grained `(tool, action)` gating (`phase_tool_context`,
   `filter_tools_for_phase`, `check_phase_tool_permission`) locking out file/state mutations in read-only phases.
"""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterator, List, Optional, Tuple

from companion.config import SKIP_DIRS, settings


_ACTIVE_PHASE: ContextVar[str] = ContextVar("companion_active_phase", default="")
_R3_STEP_APPROVED: ContextVar[bool] = ContextVar("companion_r3_step_approved", default=False)


def _p(ptype: str, desc: str, enum: Optional[List[str]] = None, items_type: Optional[str] = None) -> Dict[str, Any]:
    """Build a compact JSON Schema property descriptor with an explicit description."""
    spec: Dict[str, Any] = {"type": ptype, "description": desc}
    if enum:
        spec["enum"] = enum
    if items_type:
        spec["items"] = {"type": items_type}
    return spec


def _tool(name: str, desc: str, props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {"type": "object", "properties": props, "required": required}}}


TOOLS: List[Dict[str, Any]] = [
    _tool("read_file", "Read numbered lines or an exact Python AST symbol (`func`, `Class`, `Class.method`) from a file.", {
        "path": _p("string", "Relative or absolute file path inside the active WorkspaceContext."),
        "start_line": _p("integer", "1-based first line to read (optional; ignored when `symbol` is set)."),
        "end_line": _p("integer", "1-based last line to read inclusive (optional; ignored when `symbol` is set)."),
        "symbol": _p("string", "Optional Python AST symbol (`func_name`, `ClassName`, or `ClassName.method`) to slice."),
    }, ["path"]),
    _tool("write_file", "Create or overwrite a file (auto-backups & runs instant AST syntax, line-budget, ADR-001, and layer micro-linter).", {
        "path": _p("string", "Relative or absolute path of the file to write in the active project."),
        "content": _p("string", "Complete UTF-8 text content to write to the file."),
    }, ["path", "content"]),
    _tool("edit_file", "Replace an EXACT unique string in an existing file (auto-backups & runs instant AST/hygiene/layer micro-linter).", {
        "path": _p("string", "Relative or absolute path of the existing file to modify."),
        "old_str": _p("string", "Exact unique existing block of text (including indentation) to replace."),
        "new_str": _p("string", "Replacement text block that drops cleanly in place of `old_str`."),
    }, ["path", "old_str", "new_str"]),
    _tool("list_dir", "List files and subdirectories inside a directory in the active WorkspaceContext.", {
        "path": _p("string", "Relative or absolute directory path (defaults to `.`)."),
    }, []),
    _tool("search", "Search text across workspace files by case-insensitive substring or regular expression.", {
        "pattern": _p("string", "Substring or regex pattern to search for."),
        "path": _p("string", "Directory or file path to scope the search (defaults to `.`)."),
        "is_regex": _p("boolean", "Set true to treat `pattern` as a Python regular expression."),
        "file_glob": _p("string", "Optional filename glob filter such as `*.py`, `*.ts`, or `*.md`."),
    }, ["pattern"]),
    _tool("run_python", "Execute Python 3 code directly in the active project root with UTF-8 output (preferred on Windows).", {
        "code": _p("string", "Valid Python 3 snippet to execute via `sys.executable -c`."),
    }, ["code"]),
    _tool("run_shell", "Execute a safe non-destructive shell command in the active project root.", {
        "command": _p("string", "Shell command line to run (destructive commands are blocked by Safety Guard)."),
    }, ["command"]),
    _tool("web_fetch", "Fetch an HTTP/HTTPS URL with SSRF protection, 512 KB stream cap, and automatic HTML-to-Markdown conversion.", {
        "url": _p("string", "Full `http://` or `https://` URL to fetch."),
        "raw": _p("boolean", "Set true to return raw HTML instead of readable Markdown."),
    }, ["url"]),
    _tool("web_search", "Search the public web or configured local SearXNG endpoint (gated by LAN `local_only` privacy posture).", {
        "query": _p("string", "Search query keywords or natural-language question to look up."),
        "max_results": _p("integer", "Maximum number of search results to return (1-10, default 5)."),
    }, ["query"]),
    _tool("memory_manage", "Manage persistent SQLite memory, KnowledgePort facts, ADRs, user profile, notes, history, and `MEMORY.md` sync.", {
        "action": _p("string", "Memory or KnowledgePort operation to execute.", enum=["add_note", "get_notes", "add_adr", "get_adrs", "set_profile", "get_profile", "upsert_fact", "query_facts", "search", "search_history", "recent_history", "sync"]),
        "content": _p("string", "System note or lesson text (for `add_note`)."),
        "category": _p("string", "Note category tag such as `architecture`, `lesson`, or `general`."),
        "title": _p("string", "ADR title prefixed like `ADR-014: ...` (for `add_adr`)."),
        "context": _p("string", "Architectural problem context (for `add_adr`)."),
        "decision": _p("string", "Concrete architectural decision adopted (for `add_adr`)."),
        "consequences": _p("string", "Expected tradeoffs and verification rules (for `add_adr`)."),
        "key": _p("string", "User profile or KnowledgePort fact key (for `set_profile`, `upsert_fact`, or exact `query_facts`)."),
        "value": _p("string", "User profile or KnowledgePort fact value (for `set_profile` or `upsert_fact`)."),
        "source": _p("string", "Provenance tag for `upsert_fact` (such as `user`, `verified`, or `reflect`)."),
        "confidence": _p("number", "Confidence score between 0.0 and 1.0 for `upsert_fact` (default 1.0)."),
        "query": _p("string", "Keyword search string across `query_facts` or SQLite conversation history."),
        "time_window": _p("string", "Optional SQLite relative window such as `-7 days` or `-24 hours`."),
        "limit": _p("integer", "Maximum number of rows to return (default 15-20)."),
    }, ["action"]),
    _tool("task_manage", "Create, update, and query internal software-workflow tasks and Companion's own engineering backlog (`MEMORY.md`). For the user's personal, home, or work tasks/goals, use `agile_manage` instead.", {
        "action": _p("string", "Task or backlog operation to execute.", enum=["add_task", "update_task", "list_tasks", "add_backlog", "update_backlog", "list_backlog", "list_all"]),
        "description": _p("string", "Clear, atomic task or backlog item description."),
        "task_id": _p("integer", "Numeric ID of an existing task (for `update_task`)."),
        "backlog_id": _p("integer", "Numeric ID of an existing backlog item (for `update_backlog`)."),
        "status": _p("string", "Target status (`pending`, `in_progress`, `completed`, `blocked`)."),
        "priority": _p("integer", "Priority from 1 (highest) to 10 (lowest); default 5."),
        "category": _p("string", "Backlog category (`architecture`, `reliability`, `ux`, `hygiene`)."),
        "acceptance_criteria": _p("string", "Testable definition of done for a task."),
        "verification_result": _p("string", "Evidence or test output proving task completion."),
        "estimated_effort": _p("string", "T-shirt or time estimate (`S`, `M`, `L`, `30m`)."),
    }, ["action"]),
    _tool("workflow_manage", "Drive the Software Engineering State-Machine Workflow (`INTAKE..COMPLETE` with code snapshots & unit-test gates), draft/approve code Blueprints, run `/goal` software builds, forge/run 3-Tier Capability Packs, spawn subagents, or execute/resume code-controlled Python DAGs (`dags/*.py`). NEVER use `start`/`autonomy`/`blueprint` for personal, home, or work goals — use `agile_manage` instead.", {
        "action": _p("string", "Workflow state-machine, Skill Forge, or code-controlled DAG runtime action.", enum=["start", "status", "transition", "recover", "blueprint", "approve", "autonomy", "http_smoke", "subagent", "list_dags", "run_dag", "resume_dag", "check_skill", "bootstrap_skill", "promote_skill", "run_script", "abort", "complete"]),
        "workflow_type": _p("string", "Software workflow classification (never use for personal goals — use `agile_manage` instead).", enum=["self_evolution", "engineering_feature", "bug_fix", "architecture_refactor"]),
        "goal": _p("string", "Objective text (pass `'resume'` with `action='autonomy'` to resume an interrupted goal)."),
        "module": _p("string", "Target Python web server module for `http_smoke` (default `surfaces.web_server`)."),
        "endpoints": _p("array", "URL paths to probe during `http_smoke` (e.g. `['/','/api/status']`).", items_type="string"),
        "skill": _p("string", "Skill name (`skills/<skill>/` or `data/skills/<skill>/`) for `subagent`, `check_skill`, `bootstrap_skill`, or `run_script`."),
        "script": _p("string", "Script filename inside `scripts/` for `run_script`."),
        "script_args": _p("array", "Command-line argument strings passed to `run_script`.", items_type="string"),
        "skill_md": _p("string", "Complete `SKILL.md` for `bootstrap_skill` (MUST start with `---` YAML frontmatter defining `name`, `description`, and `required_tools: [...]`, and body MUST include `## When to Use This Skill` and `## Step-by-Step Procedure`)."),
        "scripts": _p("object", "Map of `filename.py` -> Python source code (pure stdlib or PEP 723 `# /// script` isolated `uv` dependencies) for `bootstrap_skill`."),
        "dags": _p("object", "Map of `filename.py` -> Python `DAGSpec` module (`from companion.dag_runtime import DAGNode, DAGSpec; DAG = DAGSpec(name='...', description='...', input_schema={'x': 'list'}, nodes=[DAGNode(node_id='step1', kind='python_step', output_schema={'n': 'int'}, fn=lambda ctx: ...)], self_test_fn=lambda: True)`) for `bootstrap_skill`."),
        "references": _p("object", "Map of `doc.md` -> Markdown reference text for `bootstrap_skill`."),
        "overwrite": _p("boolean", "Set true on `bootstrap_skill` to overwrite an existing skill directory."),
        "scope": _p("string", "Skill promotion target (`user` saves update-safe skill in `data/skills/`; `core` targets external project `skills/`).", enum=["core", "user"]),
        "tier": _p("string", "Model tier for `subagent`.", enum=["default", "fast", "coding", "reasoning", "audio", "image", "video"]),
        "dag": _p("string", "Code-controlled `DAGSpec` name (for `run_dag`)."),
        "run_id": _p("string", "Persisted SQLite DAG run ID (`dagrun_...`) for `resume_dag`."),
        "params": _p("object", "Key-value input dictionary passed into `run_dag`."),
        "max_iters": _p("integer", "Maximum tool iterations per subagent or autonomy phase."),
        "to_state": _p("string", "Target state for `transition`.", enum=["INTAKE", "DISCOVER", "DESIGN", "PLAN", "IMPLEMENT", "VERIFY", "RECOVER", "REFLECT", "COMPLETE", "ABORTED"]),
        "summary": _p("string", "Concise summary recorded in the SQLite workflow transition log."),
        "acceptance_criteria": _p("string", "Explicit verification gate criteria for the workflow."),
        "reason": _p("string", "Diagnostic failure reason when triggering `recover`."),
        "rollback": _p("boolean", "Set true on `recover` to restore files to the workflow's safety checkpoint."),
    }, ["action"]),
    _tool("checkpoint_manage", "Create, list, or rollback full workspace snapshots, or record/inspect local Git save-points in the active project.", {
        "action": _p("string", "Checkpoint or Git save-point action.", enum=["create", "list", "rollback", "git_save", "git_log"]),
        "label": _p("string", "Descriptive label when creating a checkpoint or commit message for `git_save`."),
        "checkpoint_id": _p("string", "Checkpoint ID to restore on `rollback` (defaults to latest)."),
    }, ["action"]),
    _tool("sdd_manage", "Verify 4-File Canonical SDD & import-qualified Dead-Code Hygiene, or read/sync specs and ADRs.", {
        "action": _p("string", "SDD operation.", enum=["verify", "status", "sync", "read_spec", "add_adr", "get_adrs"]),
        "spec_name": _p("string", "Canonical spec file for `read_spec`.", enum=["PRODUCT.md", "ARCHITECTURE.md", "CONTRACTS.md", "DECISIONS.md"]),
        "title": _p("string", "ADR title when `action='add_adr'` (e.g., `ADR-014: ...`)."),
        "context": _p("string", "Architectural context or problem statement for `add_adr`."),
        "decision": _p("string", "Core architectural decision text when `action='add_adr'`."),
        "consequences": _p("string", "Expected tradeoffs and verification outcomes for `add_adr`."),
    }, ["action"]),
    _tool("telemetry_manage", "Inspect KPIs, Waterfall execution traces, and structured application logs (`DEBUG`, `INFO`, `WARN`, `ERROR`).", {
        "action": _p("string", "Telemetry query or log action.", enum=["kpis", "waterfall", "recent_traces", "logs", "log_event", "prune"]),
        "trace_id": _p("string", "Specific trace ID to inspect for `waterfall`."),
        "level": _p("string", "Log severity filter or event level.", enum=["DEBUG", "INFO", "WARN", "ERROR"]),
        "component": _p("string", "Subsystem name filter (e.g. `llm`, `safety`, `autonomy`, `web`)."),
        "message": _p("string", "Log message text when `action='log_event'`."),
        "limit": _p("integer", "Maximum trace or log entries to display."),
    }, ["action"]),
    _tool("project_manage", "Manage multi-project `WorkspaceContext` (`ADR-007`): inspect, list, initialize/switch external projects (locking Companion core read-only), or return to `self`.", {
        "action": _p("string", "Workspace management action.", enum=["status", "list", "init", "switch", "self"]),
        "target": _p("string", "Project name or folder path for `init` or `switch` (created outside Companion core)."),
    }, ["action"]),
    _tool("system_doctor", "Run 7-stage system health diagnostics (config, AST syntax, A-Frame layers, SDD sync, import-qualified dead-code hygiene, SQLite, unit tests).", {
        "run_tests": _p("boolean", "Whether to run Stage 7 automated unit tests (default true)."),
    }, []),
    _tool("job_manage", "Dispatch, inspect, cancel, or resume Forever-Chat background jobs so long tasks never clutter active chat.", {
        "action": _p("string", "Background job management operation.", enum=["list", "dispatch", "status", "cancel", "resume"]),
        "goal": _p("string", "Self-contained objective brief when `action='dispatch'`."),
        "title": _p("string", "Concise Departures Board title for `dispatch`."),
        "job_id": _p("string", "Target background job ID (`bgj_...`) for `status`, `cancel`, or `resume`."),
        "tier": _p("string", "Model tier for the background worker (`fast` or `reasoning`)."),
        "allowed_tools": _p("string", "Optional comma-separated tool names to grant the worker."),
    }, ["action"]),
    _tool("wiki_manage", "Operate the Co-Owned 4-Folder Knowledge Wiki Warehouse (`01_inbox`..`04_archive`), 11 governed templates, Multi-Turn Deliberate Curation (`inspect_graduation` -> `read` -> `validate_curation` -> `commit_curation`), `ontology` cross-domain bridges, Never-Delete lineage (`curation_history`/`revert_curation`), and 2D Mind Map.", {
        "action": _p("string", "Wiki Warehouse, Template, Maintenance, or Deliberate Curation operation (`capture_inbox` stages in `01_inbox/`; `curate`/`save` writes governed notes; `inspect_graduation` -> `validate_curation` -> `commit_curation` graduates into `02_knowledge/`).", enum=[
            "list", "tree", "read", "search", "capture_inbox", "save", "curate", "graduate",
            "inspect_graduation", "validate_curation", "commit_curation", "curation_history", "revert_curation",
            "list_templates", "read_template", "save_template", "tag_authority", "create_proxy_note", "deep_curate_job",
            "diff", "consolidate_ontology", "verify_note", "list_stale", "stage_turn",
            "trash", "list_trash", "restore", "graph", "reindex", "export_chat",
        ]),
        "path": _p("string", "Relative Wiki note path or stem (e.g. `01_inbox/my-note.md` or `02_knowledge/homelab/esxi-host.md`)."),
        "title": _p("string", "Human-readable note or template title."),
        "summary": _p("string", "1-2 sentence executive summary required in `02_knowledge/` frontmatter (or asset summary for `create_proxy_note`)."),
        "template": _p("string", "Governed template slug (`zettelkasten-atomic`, `adr-decision`, `sop-runbook`, `concept-map-system-hub`, `concept-comparison`, `dikw-pyramid-of-insight`, `weekly-worklog`, `standard-note`, `homelab-host-spec`, `homelab-network-matrix`, `proxy-asset-note`)."),
        "content": _p("string", "Markdown note body matching the selected template's `required_sections` (supports `[[wikilinks]]`, zero unfilled placeholders)."),
        "body": _p("string", "Alias for `content` Markdown note body when saving or curating a note."),
        "query": _p("string", "Search query string when `action='search'` — supports YAML frontmatter `key:value` filters (`tag:...`, `domain:...`, `template:...`, `ontology:...`, `author:...`, `type:...`, `has:...`, or any custom YAML key) combined with free-text and 2-hop `ontology` bridges."),
        "domain": _p("string", "Top-level domain folder under `02_knowledge/` (e.g. `homelab`, `equipment-ops`, `software-engineering`, `health-fitness`)."),
        "topic": _p("string", "Optional second-level topic folder under `02_knowledge/<domain>/`."),
        "tags": _p("string", "Comma-separated kebab-case tags for note frontmatter."),
        "ontology": _p("string", "Comma-separated canonical ontology bridge concepts (e.g. `datacenter-compute,virtualization-fabric` or `preventive-maintenance`)."),
        "curation_id": _p("string", "Active curation session ID (`cur_...`) returned by `inspect_graduation` (for `validate_curation` and `commit_curation`)."),
        "strategy": _p("string", "Deliberate graduation strategy for `validate_curation`.", enum=["CREATE_NEW", "MERGE_AND_REFINE", "SPLIT_MULTI", "KEEP_SEPARATE"]),
        "outputs_json": _p("string", "JSON array string of curated output note objects `[{\"path\":\"02_knowledge/...\",\"title\":\"...\",\"summary\":\"...\",\"template\":\"...\",\"domain\":\"...\",\"tags\":[\"...\"],\"ontology\":[\"...\"],\"content\":\"...\"}]` for `validate_curation`."),
        "rationale": _p("string", "Explicit architectural rationale (mandatory when `strategy='KEEP_SEPARATE'` despite high overlap)."),
        "allow_condense": _p("boolean", "Set true only when intentionally condensing verbose prose during `MERGE_AND_REFINE`."),
        "tx_id": _p("string", "Curation transaction ID (`ctx_...`) to roll back when `action='revert_curation'`."),
        "asset_path": _p("string", "Source binary/PDF/image asset path when `action='create_proxy_note'`."),
        "highlights": _p("string", "Key specs, facts, or extracted highlights for `create_proxy_note`."),
        "required_sections": _p("string", "Comma-separated required `## Heading` names when creating a custom template via `save_template`."),
        "document_type": _p("string", "Document category (`atomic`, `adr`, `sop`, `hub`, `comparison`, `synthesis`, `log`, `reference`, `asset`, `matrix`, `proxy`) for `save_template`."),
        "when_to_use": _p("string", "Selection guidance for `save_template`."),
        "compare_to": _p("string", "Optional archived revision path to compare against when `action='diff'`."),
        "approval_id": _p("string", "Optional pending `apr_...` ID to preview Unified/Split diff when `action='diff'`."),
        "aliases": _p("string", "Optional `alias->canonical` mappings (comma-separated) when `action='consolidate_ontology'`."),
        "dry_run": _p("boolean", "Preview ontology synonym consolidation without mutating files when `action='consolidate_ontology'`."),
        "review_after_days": _p("integer", "Optional custom verification cadence in days when `action='verify_note'`."),
        "expected_hash": _p("string", "Optional 16-char SHA-256 `content_hash` for optimistic concurrency check on `save`."),
    }, ["action"]),
    _tool("vault_manage", "Inspect Zero-Reveal Blind Credential Vault metadata (`is_set`, missing refs, audit log) or request scope access — never reveals secret values.", {
        "action": _p("string", "Blind Vault metadata action (raw values are never accessible).", enum=["list", "check_secret", "list_missing", "declare", "request_scope", "audit"]),
        "name": _p("string", "Symbolic kebab-case credential name (e.g. `node-ssh` or `home-assistant-token`)."),
        "cred_type": _p("string", "Credential category when declaring a placeholder.", enum=["api_key", "bearer_token", "basic_auth", "ssh_key", "custom"]),
        "caller_scope": _p("string", "Authorized connector scope (e.g. `endpoint:gpu-node`, `mcp:github`, `partner:lab-peer`)."),
        "description": _p("string", "Plain-English note describing what service uses this symbolic credential."),
    }, ["action"]),
    _tool("endpoint_manage", "Propose (approval-staged), register, probe, hardware-profile, and interact with remote `ssh`, Windows `winrm`, and `http_api` endpoints using symbolic Vault refs.", {
        "action": _p("string", "Remote endpoint operation across SSH, WinRM, and HTTP APIs (`propose` stages discovered endpoints into ApprovalQueue).", enum=["list", "propose", "register", "remove", "test", "inspect_host", "ssh_exec", "ssh_read_file", "winrm_exec", "api_request"]),
        "endpoint_id": _p("string", "Unique kebab-case endpoint identifier (e.g. `gpu-node` or `workstation-winrm`)."),
        "protocol": _p("string", "Transport protocol when `action='propose'` or `'register'`.", enum=["ssh", "winrm", "http_api"]),
        "host": _p("string", "Target hostname, LAN IP, or base URL for the endpoint."),
        "port": _p("integer", "Optional TCP port override (defaults: 22 SSH, 5985 WinRM, 80/443 HTTP)."),
        "username": _p("string", "Optional remote login username for SSH, WinRM, or Basic Auth."),
        "auth_mode": _p("string", "Authentication mechanism bound to `credential_ref`.", enum=["none", "ssh_key", "ssh_agent", "basic_auth", "bearer_token", "api_key_header"]),
        "credential_ref": _p("string", "Symbolic Blind Vault secret name (NEVER pass a raw password, key, or token)."),
        "label": _p("string", "Optional human-friendly display label for the endpoint."),
        "auth_header": _p("string", "Custom HTTP authentication header name (default `X-API-Key`) for `api_key_header`."),
        "verify_tls": _p("boolean", "Whether to enforce HTTPS certificate verification (default true)."),
        "command": _p("string", "Bounded shell or PowerShell command for `ssh_exec` or `winrm_exec`."),
        "remote_path": _p("string", "Remote file path to inspect safely via `ssh_read_file`."),
        "method": _p("string", "HTTP verb (`GET`, `POST`, `PUT`, `DELETE`) when `action='api_request'`."),
        "path": _p("string", "URL path suffix (e.g. `/v1/models`) when `action='api_request'`."),
        "json_body": _p("object", "Optional JSON request payload dictionary when `action='api_request'`."),
    }, ["action"]),
    _tool("mcp_manage", "Propose/discover/call outbound MCP servers, harvest MCP tool schemas into Capability Packs (`harvest_skill`), and handshake/delegate jobs to Federated Partners.", {
        "action": _p("string", "Federated MCP Hub, Harvester, or Partner action (`propose_server` stages approval; `harvest_skill` builds `data/skills/<pack_name>/`).", enum=["list", "propose_server", "register_server", "discover", "call_tool", "harvest_skill", "list_partners", "register_partner", "handshake_partner", "delegate_partner", "check_partner_job"]),
        "server_id": _p("string", "Outbound MCP server slug for `propose_server`, `register_server`, `discover`, `call_tool`, or `harvest_skill`."),
        "pack_name": _p("string", "Optional target Capability Pack slug in `data/skills/` when `action='harvest_skill'`."),
        "transport": _p("string", "Outbound MCP transport (`stdio` or `http`).", enum=["stdio", "http"]),
        "command": _p("string", "Executable command for `stdio` MCP server registration."),
        "url": _p("string", "Remote HTTP MCP endpoint URL (`http://.../api/mcp`) for servers or partners."),
        "credential_ref": _p("string", "Symbolic Vault token reference for authenticating to the server or partner."),
        "tool_name": _p("string", "Discovered MCP tool name to invoke when `action='call_tool'`."),
        "arguments": _p("object", "Key-value JSON arguments dictionary passed to `call_tool`."),
        "partner_id": _p("string", "Paired Federated Companion ID (e.g. `lab-peer` or `workstation`)."),
        "goal": _p("string", "Task goal to delegate to a remote Federated Partner when `action='delegate_partner'`."),
        "job_id": _p("string", "Remote background job ID when `action='check_partner_job'`."),
    }, ["action"]),
    _tool("routine_manage", "Manage Unified `TriggerPolicy` Routines (`manual`, `scheduled`, `on_idle` with time-slot windows), Background Autonomy (`autonomy_status`, `autonomy_on`, `autonomy_off`, `morning`), saved prompts, and pending approvals.", {
        "action": _p("string", "Routine, Background Autonomy, saved prompt, or approval inspection action.", enum=["list", "create", "enable", "disable", "delete", "tick", "run_now", "autonomy_status", "autonomy_on", "autonomy_off", "morning", "list_approvals", "prompt_list", "prompt_save", "prompt_render"]),
        "routine_id": _p("string", "Kebab-case identifier for `create`, `enable`, `disable`, `delete`, or `run_now`."),
        "title": _p("string", "Optional human-readable title when creating or updating a routine."),
        "mode": _p("string", "TriggerPolicy mode (`manual`, `scheduled`, or `on_idle`).", enum=["manual", "scheduled", "on_idle"]),
        "schedule_expr": _p("string", "Schedule expression when `mode='scheduled'` (`every 30m`, `every 6h`, `daily 08:00`, or 5-field cron)."),
        "allowed_window": _p("string", "Optional local time window when `mode='on_idle'` (e.g. `22:00-06:00` or `Mon-Fri 22:00-06:00`)."),
        "idle_min_seconds": _p("integer", "Minimum idle seconds before an `on_idle` routine fires (default 900)."),
        "cooldown_seconds": _p("integer", "Minimum cooldown seconds between `on_idle` runs (default 3600)."),
        "precheck_name": _p("string", "Optional deterministic `<5ms` precheck (`wiki_inbox_or_stale`, `recent_unconsolidated_chat`, `recent_warn_or_slow_spans`, `active_goal_has_ready_items`)."),
        "target_kind": _p("string", "Verified routine target category (free-form prompts are forbidden).", enum=["dag", "maintenance_agent", "goal_slice", "wiki_inbox_triage", "wiki_deep_curate", "snapshot_backup", "endpoint_pulse", "chat_archive"]),
        "target_name": _p("string", "Code-controlled DAG name or maintenance target name."),
        "inputs": _p("object", "Optional key-value input dictionary passed to the routine target."),
        "variables": _p("object", "Optional template variable dictionary for `prompt_render` or `create`."),
        "name": _p("string", "Template slug for `prompt_save` or `prompt_render`."),
        "template": _p("string", "Reusable prompt text (with optional `{{var}}` placeholders) for `prompt_save`."),
    }, ["action"]),
    _tool("agile_manage", "Operate the Universal Headless Project & Goal Graph (`projects`, `milestones`, `timeboxes`, `work_items` with `owner`, `depends_on`, DIKW `stage_kind`, and `TriggerPolicy`) plus Kanban/Sprint projections.", {
        "action": _p("string", "Project & Goal Graph or Kanban projection sub-action (`create_milestone`/`save_epic` for goals; `create_item`/`save_card` for work items).", enum=["board_view", "list_items", "roadmap", "list_boards", "create_project", "update_project", "save_board", "list_epics", "create_milestone", "save_epic", "delete_epic", "seed_dikw", "list_sprints", "create_timebox", "start_sprint", "rollover_sprint", "capacity", "create_item", "update_item", "save_card", "move_card", "delete_item", "delete_card", "slice_unblocker"]),
        "project_id": _p("string", "Project ID (`prj_...` or board slug such as `life-home`, `work-career`, `software-companion`)."),
        "board_id": _p("string", "Board slug alias (`all-active-sprint`, `life-home`, `work-career`, `software-companion`, or custom project ID)."),
        "epic_id": _p("string", "Milestone/Epic ID (`ms_...` or `ep_...`) when updating/deleting or linking a child work item."),
        "card_id": _p("string", "Work-item ID (`wi_...` or `crd_...`) for `update_item`/`save_card`, `move_card`, `delete_card`, or `slice_unblocker`."),
        "title": _p("string", "Title for project, milestone/epic, timebox/sprint, work item, or `slice_unblocker` first step."),
        "description": _p("string", "Detailed notes, milestone summary, acceptance criteria, or context."),
        "owner": _p("string", "Execution owner (`companion`, `human`, or `collaborative`).", enum=["companion", "human", "collaborative"]),
        "stage_kind": _p("string", "Prerequisite DIKW/engineering stage category.", enum=["OBSERVE_DATA", "CURATE_DIKW", "FORGE_CAPABILITY", "DELIVER_AND_VERIFY"]),
        "depends_on": _p("array", "Upstream work-item IDs that must finish before this item is unblocked.", items_type="string"),
        "dag_name": _p("string", "Optional verified code-controlled DAG name bound to this work item."),
        "checklist": _p("array", "Optional checklist subtask strings inside a work item.", items_type="string"),
        "status": _p("string", "Work-item column/lifecycle status.", enum=["backlog", "sprint_todo", "ready", "in_progress", "blocked", "verify", "done"]),
        "horizon": _p("string", "Planning horizon (`now`, `next`, `later`, `someday`).", enum=["now", "next", "later", "someday"]),
        "effort_size": _p("string", "Effort slot size (`quick` <30m=1, `half_day`=2, `full_day`=4, `multi_day`=8).", enum=["quick", "half_day", "full_day", "multi_day"]),
        "day_slot": _p("string", "Day-of-week execution slot (`weekday`, `weekend`, `any`).", enum=["weekday", "weekend", "any"]),
        "priority": _p("integer", "Work-item priority from 1 (highest) to 5 (lowest)."),
        "start_date": _p("string", "Optional start date (`YYYY-MM-DD`) for a milestone or timebox."),
        "target_date": _p("string", "Optional target completion date (`YYYY-MM-DD`) for a milestone/epic."),
        "planned_date": _p("string", "Planned execution date (`YYYY-MM-DD`) surfaced on the Unified Calendar."),
        "due_date": _p("string", "Hard due date (`YYYY-MM-DD`) surfaced on the Unified Calendar."),
        "blocked_reason": _p("string", "Reason or external dependency blocking a `blocked` work item."),
        "weekday_capacity_slots": _p("integer", "Weekly weekday slot budget when starting/updating a timebox (default 10)."),
        "weekend_capacity_slots": _p("integer", "Weekly weekend slot budget when starting/updating a timebox (default 6)."),
        "kind": _p("string", "Project category when `action='create_project'`/`'save_board'`.", enum=["life", "work", "project", "software", "general"]),
        "workspace_path": _p("string", "Optional local Git repository directory bound to this project (`create_project` or `update_project`)."),
        "delivery_policy": _p("string", "Per-project Git delivery posture (`local_only` default, `remote_branch_pr`, or `ask_me`).", enum=["local_only", "remote_branch_pr", "ask_me"]),
        "git_credential_ref": _p("string", "Optional symbolic Blind Vault slug (`vault://...`) for authenticated Git SSH/PAT operations."),
        "test_command": _p("string", "Optional custom test command override for this project."),
        "preview_command": _p("string", "Optional custom `dev_preview` startup command template (supports `{port}`)."),
    }, ["action"]),
    _tool("calendar_manage", "Inspect or schedule across the Unified Calendar (appointments, focus blocks, sprint task dates, deadlines, routines, and due SRS study reviews).", {
        "action": _p("string", "Calendar sub-action.", enum=["window", "today", "week", "month", "list_events", "add_event", "delete_event"]),
        "title": _p("string", "Event/appointment title when `action='add_event'`."),
        "start_at": _p("string", "Event start date/time (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`)."),
        "end_at": _p("string", "Optional event end date/time (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`)."),
        "kind": _p("string", "Calendar event category.", enum=["appointment", "focus_block", "milestone", "reminder"]),
        "all_day": _p("boolean", "Set true for all-day calendar entries."),
        "start_date": _p("string", "Window start date (`YYYY-MM-DD`) for `window` or `list_events`."),
        "days": _p("integer", "Number of days to include in `window` (default 35)."),
        "board_id": _p("string", "Optional board filter or linked board ID."),
        "card_id": _p("string", "Optional linked Kanban card ID (`crd_...`)."),
        "event_id": _p("string", "Event ID (`evt_...`) when updating or deleting an event."),
        "notes": _p("string", "Optional location or preparation notes."),
    }, ["action"]),
    _tool("education_manage", "Operate Headless Education courses, pedagogical stages (`priming`..`mastered`), spaced-repetition grading, Wiki study curation, and inline Chat flashcard decks.", {
        "action": _p("string", "Education & mastery sub-action.", enum=["progress", "list_courses", "save_course", "set_active", "advance_stage", "upsert_items", "grade_item", "due", "flashcard_deck", "curate_note"]),
        "course_id": _p("string", "Course slug (`course_id`) to query or update (defaults to active course)."),
        "title": _p("string", "Course title (`save_course`) or study module title (`curate_note`)."),
        "domain": _p("string", "Subject domain slug (e.g. `networking`, `python`, `automotive`)."),
        "goal_summary": _p("string", "Learning objective summary for `save_course`."),
        "modules": _p("array", "Ordered list of syllabus module titles for `save_course`.", items_type="string"),
        "module_slug": _p("string", "Target module slug for `advance_stage`, `upsert_items`, or `curate_note`."),
        "stage": _p("string", "Pedagogical stage for `advance_stage`.", enum=["priming", "dual_coding", "elaboration", "retrieval", "mastered"]),
        "items": {"type": "array", "description": "Preferred batch array of `{prompt, answer, topic, explanation}` objects for `upsert_items` (saves all flashcards in 1 call).", "items": {"type": "object"}},
        "prompt": _p("string", "Flashcard front or quiz question prompt when adding a single item via `upsert_items`."),
        "answer": _p("string", "Flashcard back or canonical answer when adding a single item via `upsert_items`."),
        "topic": _p("string", "Concept/topic label for study items or `flashcard_deck` filter."),
        "item_id": _p("string", "Study item ID (`edu_...`) for `grade_item`."),
        "passed": _p("boolean", "True if the learner recalled the item accurately (`grade_item`)."),
        "miss_reason": _p("string", "Brief diagnostic note when `passed=False` (e.g. `confused TCP vs UDP handshake`)."),
        "core_idea": _p("string", "Single atomic principle summary for `curate_note`."),
        "mechanism_markdown": _p("string", "Detailed explanation & examples Markdown for `curate_note`."),
        "mermaid_diagram": _p("string", "Optional Mermaid diagram body (without fences) for dual-coding `curate_note`."),
        "limit": _p("integer", "Max items to return in `due` or `flashcard_deck` (default 6 for decks)."),
    }, ["action"]),
    _tool("git_manage", "Execute safe local Git, parallel `git worktree`, secret-shielded commit, and Blind-Vault-authenticated remote operations governed by project `delivery_policy`.", {
        "action": _p("string", "Git or worktree sub-action.", enum=["status", "diff", "log", "branch", "worktree_add", "worktree_list", "worktree_remove", "commit", "fetch", "pull", "push"]),
        "repo_path": _p("string", "Optional repository or worktree directory path (defaults to active project root)."),
        "branch": _p("string", "Feature/fix branch name (`feat/<slug>` or `fix/<slug>`) for `branch`, `worktree_add`, `pull`, or `push`."),
        "slug": _p("string", "Short kebab-case feature slug used when `worktree_add` auto-names `feat/<slug>`."),
        "worktree_path": _p("string", "Optional custom worktree checkout path (for `worktree_add` or `worktree_remove`)."),
        "message": _p("string", "Descriptive commit subject line when `action='commit'`."),
        "paths": _p("array", "Optional explicit relative file paths to stage on `commit` (secret/DB files are blocked).", items_type="string"),
        "staged": _p("boolean", "Set true on `diff` to inspect staged (`--cached`) changes."),
        "path": _p("string", "Optional single relative path filter for `diff`."),
        "remote": _p("string", "Git remote name (default `origin`) for `fetch`, `pull`, or `push`."),
        "credential_ref": _p("string", "Optional symbolic Blind Vault slug overriding the project's `git_credential_ref`."),
        "limit": _p("integer", "Number of recent commits to show for `log` (default 10)."),
    }, ["action"]),
    _tool("dev_preview", "Manage isolated local preview servers (`127.0.0.1:<port>`) in dedicated OS process groups with scrubbed temp env/DB, ring-buffer logs, HTTP probes, vision critique, and guaranteed teardown.", {
        "action": _p("string", "Preview server lifecycle or inspection sub-action.", enum=["start", "status", "list", "logs", "http_probe", "vision_critique", "stop", "stop_all"]),
        "preview_id": _p("string", "Preview instance ID (`prev_...`) returned by `start` (defaults to active preview)."),
        "cwd": _p("string", "Optional working directory for `start` (defaults to active project root or worktree)."),
        "command": _p("string", "Optional startup command override (supports `{port}`; auto-detected when omitted)."),
        "port": _p("integer", "Optional preferred loopback port (0 allocates a free `127.0.0.1` port automatically)."),
        "health_path": _p("string", "Readiness URL path to poll on `start` (e.g. `/api/health` or `/`)."),
        "timeout": _p("integer", "Maximum seconds to wait for preview server readiness on `start` (default 20)."),
        "tail": _p("integer", "Number of recent stdout/stderr ring-buffer lines to return on `logs` (default 60)."),
        "path": _p("string", "Loopback URL path to request on `http_probe` (e.g. `/api/health`)."),
        "method": _p("string", "HTTP method (`GET` or `POST`) for `http_probe` (default `GET`)."),
        "screenshot_paths": _p("array", "Captured Desktop/Mobile PNG file paths for `vision_critique`.", items_type="string"),
        "prompt": _p("string", "Optional visual QA prompt passed to the `image` vision tier on `vision_critique`."),
    }, ["action"]),
]

ALL_TOOL_NAMES: FrozenSet[str] = frozenset(t["function"]["name"] for t in TOOLS)
_READ_ORIENT_TOOLS: Tuple[str, ...] = (
    "read_file", "list_dir", "search", "web_fetch", "web_search", "memory_manage", "task_manage",
    "workflow_manage", "checkpoint_manage", "sdd_manage", "telemetry_manage", "project_manage",
    "system_doctor", "job_manage", "wiki_manage", "vault_manage", "endpoint_manage", "mcp_manage",
    "routine_manage", "agile_manage", "calendar_manage", "education_manage", "git_manage", "dev_preview",
)
_REPO_INTERNAL_TOOLS: FrozenSet[str] = frozenset({"sdd_manage", "checkpoint_manage", "task_manage", "system_doctor", "git_manage", "dev_preview"})
_CODE_MARKERS: FrozenSet[str] = frozenset({".git", "pyproject.toml", "package.json", "cargo.toml", "go.mod", "makefile", "requirements.txt"})
_CODE_EXTS: FrozenSet[str] = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".rs", ".go", ".cs", ".csproj", ".sln", ".java", ".c", ".cpp", ".sh", ".ps1"})

PHASE_ALLOWED_TOOLS: Dict[str, FrozenSet[str]] = {
    "INTAKE": frozenset(_READ_ORIENT_TOOLS),
    "DISCOVER": frozenset(_READ_ORIENT_TOOLS),
    "DESIGN": frozenset(_READ_ORIENT_TOOLS),
    "PLAN": frozenset(_READ_ORIENT_TOOLS),
    "IMPLEMENT": ALL_TOOL_NAMES,
    "RECOVER": ALL_TOOL_NAMES,
    "VERIFY": frozenset((*_READ_ORIENT_TOOLS, "run_python", "run_shell")),
    "REFLECT": frozenset(_READ_ORIENT_TOOLS),
}


def current_workflow_phase() -> str:
    """Return the active phase-scoped tool gate name (or `''` when ungated)."""
    return _ACTIVE_PHASE.get()


@contextmanager
def phase_tool_context(phase: Optional[str]) -> Iterator[str]:
    """Bind a mechanical workflow phase (`DISCOVER`, `DESIGN`, `PLAN`, `IMPLEMENT`, etc.) to the current thread/turn."""
    clean = (phase or "").strip().upper()
    token = _ACTIVE_PHASE.set(clean)
    try:
        yield clean
    finally:
        _ACTIVE_PHASE.reset(token)


def is_r3_action_approved() -> bool:
    """Return `True` when executing inside a user-approved `R3` DAG step context."""
    return bool(_R3_STEP_APPROVED.get())


@contextmanager
def r3_approval_context(approved: bool = True) -> Iterator[bool]:
    """Temporarily authorize `R3` tool execution strictly for the duration of an approved `GoalStep`."""
    token = _R3_STEP_APPROVED.set(bool(approved))
    try:
        yield bool(approved)
    finally:
        _R3_STEP_APPROVED.reset(token)


def _workspace_hidden_tools(eff_phase: str) -> FrozenSet[str]:
    ws = settings.get_workspace()
    if ws.is_self_project:
        return frozenset() if eff_phase else _REPO_INTERNAL_TOOLS
    root = ws.project_root
    if (root / "companion").is_dir() and (root / "specs" / "ARCHITECTURE.md").is_file():
        return frozenset()
    if eff_phase or any((root / m).exists() for m in _CODE_MARKERS):
        return frozenset({"sdd_manage", "system_doctor"})
    try:
        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            if any(Path(fn).suffix.lower() in _CODE_EXTS or fn.lower() in _CODE_MARKERS for fn in files):
                return frozenset({"sdd_manage", "system_doctor"})
            if Path(cur) != root:
                dirs[:] = []
    except OSError:
        pass
    return _REPO_INTERNAL_TOOLS


def filter_tools_for_phase(phase: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return tool schemas permitted in `phase`, scoping repo-internal tools and appending approved MCP schemas."""
    eff = (phase if phase is not None else current_workflow_phase()).strip().upper()
    allowed, hidden = PHASE_ALLOWED_TOOLS.get(eff), _workspace_hidden_tools(eff)
    base = [t for t in TOOLS if t["function"]["name"] not in hidden and (not allowed or t["function"]["name"] in allowed)]
    if not allowed or eff in ("IMPLEMENT", "RECOVER"):
        try:
            from companion.mcp_hub import get_mcp_hub

            base.extend(get_mcp_hub().list_approved_openai_tools())
        except Exception:
            pass
    return base


_PHASE_LOCKED_ACTIONS: Dict[str, FrozenSet[str]] = {
    "checkpoint_manage": frozenset({"rollback", "git_save"}),
    "memory_manage": frozenset({"add_note", "set_profile", "add_adr", "upsert_fact", "sync"}),
    "sdd_manage": frozenset({"sync", "add_adr"}),
    "workflow_manage": frozenset({"start", "transition", "abort", "complete", "recover", "autonomy", "blueprint", "approve", "run_dag", "resume_dag", "run_recipe", "bootstrap_skill", "run_script"}),
    "job_manage": frozenset({"dispatch", "cancel", "resume"}),
    "wiki_manage": frozenset({
        "capture_inbox", "save", "curate", "graduate", "commit_curation", "revert_curation",
        "save_template", "create_proxy_note", "deep_curate_job", "consolidate_ontology",
        "verify_note", "stage_turn", "trash", "restore", "export_chat",
    }),
    "vault_manage": frozenset({"declare", "request_scope"}),
    "endpoint_manage": frozenset({"propose", "register", "remove", "ssh_exec", "winrm_exec", "api_request"}),
    "mcp_manage": frozenset({"propose_server", "register_server", "call_tool", "harvest_skill", "register_partner", "delegate_partner"}),
    "routine_manage": frozenset({"create", "enable", "disable", "delete", "tick", "run_now", "autonomy_on", "autonomy_off", "prompt_save"}),
    "agile_manage": frozenset({"create_project", "update_project", "save_board", "create_milestone", "save_epic", "delete_epic", "seed_dikw", "create_timebox", "start_sprint", "rollover_sprint", "create_item", "create_card", "update_item", "update_card", "save_card", "move_card", "delete_item", "delete_card", "slice_unblocker"}),
    "calendar_manage": frozenset({"add_event", "delete_event"}),
    "education_manage": frozenset({"save_course", "set_active", "advance_stage", "upsert_items", "grade_item", "curate_note"}),
    "git_manage": frozenset({"branch", "worktree_add", "worktree_remove", "commit", "fetch", "pull", "push"}),
    "dev_preview": frozenset({"start", "vision_critique", "stop", "stop_all"}),
}

_PHASE_ACTION_EXCEPTIONS: Dict[str, FrozenSet[Tuple[str, str]]] = {
    "VERIFY": frozenset({
        ("workflow_manage", "run_script"), ("dev_preview", "start"), ("dev_preview", "vision_critique"),
        ("dev_preview", "stop"), ("dev_preview", "stop_all"),
    }),
    "REFLECT": frozenset({("memory_manage", "upsert_fact"), ("memory_manage", "add_note"), ("wiki_manage", "capture_inbox")}),
}


def check_phase_tool_permission(tool_name: str, phase: Optional[str] = None, args: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Return a mechanical rejection string if `(tool_name, action)` is disallowed in `phase`."""
    eff = (phase if phase is not None else current_workflow_phase()).strip().upper()
    allowed = PHASE_ALLOWED_TOOLS.get(eff)
    if not allowed:
        return None
    if tool_name not in allowed:
        return (
            f"PHASE TOOL GATE REJECTED `{tool_name}` during `{eff}` phase: "
            f"file-mutating or execution tools outside `{sorted(allowed)}` are mechanically locked in `{eff}`. "
            "Complete discovery/design/planning first, or transition workflow to `IMPLEMENT` before modifying files."
        )
    if args is not None and eff in ("INTAKE", "DISCOVER", "DESIGN", "PLAN", "VERIFY", "REFLECT"):
        act = str(args.get("action") or "").strip().lower()
        if tool_name == "project_manage":
            has_tgt = any(str(args.get(k) or "").strip() for k in ("target", "path", "name"))
            if act not in ("", "status", "current", "list") or (not act and has_tgt):
                rej_act = act or "switch"
                return f"PHASE TOOL GATE REJECTED `project_manage(action='{rej_act}')` during `{eff}` phase: mutating or state-overriding sub-action `{rej_act}` is mechanically locked in `{eff}`."
            return None
        locked = _PHASE_LOCKED_ACTIONS.get(tool_name)
        allowed_pair = (tool_name, act) in _PHASE_ACTION_EXCEPTIONS.get(eff, frozenset())
        if locked and act in locked and not allowed_pair:
            return f"PHASE TOOL GATE REJECTED `{tool_name}(action='{act}')` during `{eff}` phase: mutating or state-overriding sub-action `{act}` is mechanically locked in `{eff}`."
    return None


def str_arg(args: Dict[str, Any], key: str, default: str = "") -> str:
    return default if args.get(key) is None else str(args.get(key)).strip()


def int_arg(args: Dict[str, Any], key: str, default: int) -> int:
    try:
        return default if args.get(key) in (None, "") else int(args[key])
    except (ValueError, TypeError):
        return default


def bool_arg(args: Dict[str, Any], key: str, default: bool = False) -> bool:
    val = args.get(key)
    return default if val is None else (val.strip().lower() in ("true", "1", "yes", "on") if isinstance(val, str) else bool(val))
