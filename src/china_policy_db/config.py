"""Minimal runtime config for the standalone data repository."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA_DIR = _REPO_ROOT / "data"


def get_config() -> dict[str, Any]:
    """Return config compatible with the vendored MOSAIC dataflow modules."""

    return {
        "data_cache_dir": os.getenv("CHINA_POLICY_DB_DATA_DIR", str(_DEFAULT_DATA_DIR)),
    }


def data_root() -> Path:
    return Path(get_config()["data_cache_dir"]).expanduser().resolve()
