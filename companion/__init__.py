"""
Companion AI OS — Self-Evolving, State-Machine & Code-Controlled DAG Local AI Companion.

Built on a zero-dependency Python 3.12 standard library A-Frame Architecture (45 core modules):
  - Left Leg (Infrastructure): Config & WorkspaceContext (`config`), Waterfall Telemetry (`telemetry`), Live SSE Stream Bus (`streaming`), Multi-Dialect Wire Translators (`llm_wire`), Multi-Tier LLM Client (`llm`)
  - Right Leg (Domain & SDD): Checkpoints (`checkpoints`), Safety & Doctor (`safety`), Hybrid Semantic Memory (`semantic_memory`), SQLite Store (`memory_store`), Unified TriggerPolicy (`trigger_policy`), Universal Project & Goal Graph (`project_graph`), Disciplines & Contracts (`disciplines`), Epistemic Grounding & ExecutionTarget (`grounding`), Cold Archive & Approvals (`archive_and_approvals`), Blind Credential Vault (`vault`), Multi-Provider LLM Registry (`model_registry`), Governed Wiki Templates (`wiki_templates`), Co-Owned Knowledge Wiki (`wiki_engine`), Multi-Turn Wiki Curation (`wiki_curation`), Wiki Maintenance & Diffs (`wiki_maintenance`), Multimodal Chat Attachments (`attachments`), Headless Socratic Education (`education_engine`), Hygiene (`hygiene`), Canonical SDD (`sdd`)
  - Crossbar (Workflow): Persistent State-Machine Graph (`workflow_graph`), Pluggable VerificationSpec & GoalPlan (`workflow_dag`), & Code-Controlled Python DAG Runtime (`dag_runtime`)
  - Upper A-Frame (Application): Cognitive Triage & Tier Router (`intake`), Skill & DAG Forge (`skill_forge`), Compaction (`compaction`), Phase-Gated Schemas (`tool_schemas`), Background Jobs (`background_jobs`), Remote Endpoints (`endpoints`), Federated MCP Hub & Harvester (`mcp_hub`), Unified TriggerPolicy Routines & Idle Watcher (`routines`), Unified Calendar (`calendar_engine`), Unified Core Actions (`core_actions`), Harvest Tools (`harvest_tools`), OS & Workspace Tools (`os_tools`, `tools`), Context HUD (`context`), Autonomy (`autonomy`), Blueprint (`blueprint`), Eval (`eval_harness`), and Executive Agent Loop (`agent`)
  - Apex (Presentation): CLI/REPL (`harness.py`) & Loopback-Default Token-Gated Web UI (`surfaces/`)
"""

import os
import sys

__version__ = "3.5.0"


def ensure_utf8_streams() -> None:
    """Ensure stdout and stderr use UTF-8 with replacement on Windows to prevent UnicodeEncodeError."""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


ensure_utf8_streams()
