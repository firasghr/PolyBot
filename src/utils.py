"""
Shared utilities for the PolyBot system.

Provides helpers for:
  - Environment variable loading
  - Structured logging setup
  - JSON serialisation/deserialisation
  - Common HTTP session factory
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any


def load_env(path: str = ".env") -> None:
    """
    Naively load KEY=VALUE pairs from a .env file into ``os.environ``.

    Skips comment lines (starting with ``#``) and empty lines.
    Does not override variables already set in the environment.
    """
    env_file = Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging with a consistent timestamp format."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )


def pretty_json(obj: Any) -> str:
    """Return a human-readable JSON string for any serialisable object."""
    return json.dumps(obj, indent=2, default=str)


def save_json(obj: Any, path: str) -> None:
    """Serialise *obj* to *path* as pretty-printed JSON."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=2, default=str))
    logging.getLogger(__name__).info("Saved JSON to %s", path)


def load_json(path: str) -> Any:
    """Load and return JSON from *path*, or None if the file is missing."""
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text())
