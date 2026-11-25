"""
Companion AI OS — Local-First Frontier Single-Agent Companion & Capability Workspace.

Built on a zero-dependency Python 3.12 standard library core (25 cohesive modules):
  - Infrastructure & LLM Wire: `config`, `telemetry`, `streaming`, `llm_wire`, `llm`, `model_registry`, `vault`
  - Frontier Harness Core: `memory_store`, `approvals`, `attachments`, `context`, `tools`, `agent`
  - Knowledge, Planning & Workflows: `wiki`, `wiki_curation`, `education_engine`, `projects`, `calendar_engine`, `workflows`
  - Jobs, Routines, Dev Studio & Integrations: `jobs`, `routines`, `integrations`, `worktrees`, `core_actions`
  - Presentation Surfaces: CLI/REPL (`harness.py`) & Token-Gated Progressive-Disclosure Web Studio (`surfaces/`)
"""

import os
import sys

__version__ = "5.0.1"


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
