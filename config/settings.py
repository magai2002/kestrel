"""The ``Settings`` object. Construct exactly one, at process start.

Nothing in this module reads ``os.environ`` at import time — it is only
read when ``Settings()`` is actually instantiated, which ``cli.py`` does
once in each command's entrypoint. Every other module receives the
values it needs as plain function parameters.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="KESTREL_",
        extra="ignore",
    )

    # --- storage ---
    data_dir: Path = Path("data")
    postgres_dsn: str = "postgresql+psycopg://kestrel:kestrel@localhost:5432/kestrel"

    # --- Alpaca market data (data-only keys; see docs/api-notes.md) ---
    # Not a live-trading credential (design rule 4 in the spec: no live
    # keys before Phase 7). A free Alpaca paper account's keys are
    # required by Alpaca for *any* equities market-data call, including
    # pure historical-bar research requests.
    alpaca_api_key_id: SecretStr | None = None
    alpaca_api_secret_key: SecretStr | None = None

    # --- misc ---
    log_level: str = "INFO"
