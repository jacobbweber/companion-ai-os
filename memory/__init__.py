"""
Memory Package — Exports persistent SQLite memory classes and helper functions.
"""

from memory.chat_memory import (
    ChatMemory,
    add_backlog,
    add_note,
    add_task,
    get_backlog,
    get_memory,
    get_notes,
    get_pending_tasks,
    log_assistant_message,
    log_tool_message,
    log_user_message,
    update_task,
)

__all__ = [
    "ChatMemory",
    "get_memory",
    "log_user_message",
    "log_assistant_message",
    "log_tool_message",
    "add_task",
    "update_task",
    "get_pending_tasks",
    "add_backlog",
    "get_backlog",
    "add_note",
    "get_notes",
]
