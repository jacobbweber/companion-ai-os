"""
Companion Configuration Package (Backward-Compatible Facade).

Exports Settings, settings, load_dotenv, and architecture helpers without
requiring any third-party dependencies.
"""

from companion.config import Settings, load_dotenv, parse_env_file, settings
from configs.architecture import (
    ArchitectureFrame,
    ArchitectureLayer,
    ComponentSpec,
    get_architecture_frame,
    print_architecture,
    verify_architecture,
)

__all__ = [
    "Settings",
    "settings",
    "load_dotenv",
    "parse_env_file",
    "ArchitectureFrame",
    "ArchitectureLayer",
    "ComponentSpec",
    "get_architecture_frame",
    "print_architecture",
    "verify_architecture",
]
