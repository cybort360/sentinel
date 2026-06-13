"""Configuration for the MemoryMCP server (architecture.md §5.3)."""

from __future__ import annotations

import os
from dataclasses import dataclass

_DEFAULT_DB_PATH = "./data/memory.db"


@dataclass(frozen=True)
class MemoryConfig:
    """Settings for a MemoryMCP store.

    Attributes:
        db_path: SQLite database path (``MEMORY_DB_PATH`` in ``.env``).
    """

    db_path: str = _DEFAULT_DB_PATH

    @classmethod
    def from_env(cls) -> MemoryConfig:
        """Build a config from environment variables (``MEMORY_DB_PATH``)."""
        return cls(db_path=os.environ.get("MEMORY_DB_PATH", _DEFAULT_DB_PATH))
