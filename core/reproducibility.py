"""Reproducibility metadata (design rule 8): every backtest run records
its git SHA, a config hash and a data hash, in the report and (from
Phase 3) the trial ledger.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Any

import pandas as pd


def git_sha() -> str | None:
    """None if this isn't a git checkout (or git isn't on PATH) — a
    reproducibility field we can't fill in is more honest than a fake one."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def hash_config(config: dict[str, Any]) -> str:
    """Stable hash of a config dict — same content, same hash,
    regardless of key order (``sort_keys=True``)."""
    canonical = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def hash_dataframe(df: pd.DataFrame) -> str:
    """Content hash of a DataFrame's actual values and index — proves a
    backtest ran on specific data, not just "some BTCUSDT history"."""
    row_hashes = pd.util.hash_pandas_object(df, index=True)
    return hashlib.sha256(row_hashes.to_numpy().tobytes()).hexdigest()[:16]
