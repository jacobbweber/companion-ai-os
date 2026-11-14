"""
Chat Memory — Backward-Compatible Facade over companion.memory_store.MemoryStore.

Supports all original methods plus the convenience helpers the agent expects:
- ChatMemory class (with both add_backlog and add_backlog_item)
- get_memory()
- log_user_message(), log_assistant_message(), log_tool_message()
- add_task(), update_task(), get_pending_tasks()
- add_backlog(), get_backlog()
- add_note(), get_notes()
"""

from pathlib import Path
from typing import Optional

from companion.memory_store import MemoryStore, get_memory_store


class ChatMemory(MemoryStore):
    """SQLite-based persistent memory for conversation history, tasks, backlog, and workflows."""

    def __init__(self, db_path: Optional[Path] = None):
        super().__init__(db_path=db_path)


_memory_instance: Optional[ChatMemory] = None


def get_memory() -> ChatMemory:
    """Get the global ChatMemory instance."""
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = ChatMemory()
    return _memory_instance


def log_user_message(content: str, tool_call_id: Optional[str] = None, session_tag: Optional[str] = None) -> int:
    """Log a user message."""
    return get_memory().log_message("user", content, tool_call_id, session_tag)


def log_assistant_message(content: str, tool_call_id: Optional[str] = None, session_tag: Optional[str] = None) -> int:
    """Log an assistant message."""
    return get_memory().log_message("assistant", content, tool_call_id, session_tag)


def log_tool_message(content: str, tool_call_id: Optional[str] = None, session_tag: Optional[str] = None) -> int:
    """Log a tool message."""
    return get_memory().log_message("tool", content, tool_call_id, session_tag)


def add_task(description: str, status: str = "pending", priority: int = 5) -> int:
    """Add a task."""
    return get_memory().add_task(description, status=status, priority=priority)


def update_task(task_id: int, status: str, completed_at: Optional[str] = None) -> bool:
    """Update a task's status."""
    return get_memory().update_task(task_id, status=status, completed_at=completed_at)


def get_pending_tasks():
    """Return all pending tasks."""
    return get_memory().get_pending_tasks()


def add_backlog(
    description: str,
    category: str = "general",
    status: str = "pending",
    estimated_effort: Optional[str] = None,
    priority: int = 5,
) -> int:
    """Add an item to the backlog."""
    return get_memory().add_backlog_item(
        description, category=category, status=status, estimated_effort=estimated_effort, priority=priority
    )


def get_backlog(memory_or_category=None, category: Optional[str] = None):
    """Get backlog items.

    Handles both get_backlog() / get_backlog('architecture') AND get_backlog(memory_instance)
    in case a caller passes the ChatMemory instance as the first argument.
    """
    if isinstance(memory_or_category, MemoryStore):
        return memory_or_category.get_backlog(category=category)
    return get_memory().get_backlog(category=memory_or_category or category)


def add_note(note: str, category: str = "general") -> int:
    """Add a system note."""
    return get_memory().add_system_note(note, category=category)


def get_notes(category: Optional[str] = None):
    """Get system notes."""
    return get_memory().get_system_notes(category=category)
