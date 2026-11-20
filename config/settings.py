"""The ``Settings`` object. Construct exactly one, at process start.

Nothing in this module reads ``os.environ`` at import time — it is only
read when ``Settings()`` is actually instantiated, which ``cli.py`` does
once in each command's entrypoint. Every other module receives the
values it needs as plain function parameters.

Separate keys per venue *and per mode* (design rule 9): the Binance
testnet key can never be mistaken for a Kraken live key because they
are different fields, and live credentials are only ever read by the
``live`` code path behind the Phase 7 gates (``live/gates.py``).
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
    # Not "data": that's the `data` Python package's directory.
    data_dir: Path = Path("market_data")
    reports_dir: Path = Path("reports")
    postgres_dsn: str = "postgresql+psycopg://kestrel:kestrel@localhost:5432/kestrel"

    # --- Binance Spot Testnet (sandbox mode; keys via GitHub login on testnet.binance.vision) ---
    binance_testnet_api_key: SecretStr | None = None
    binance_testnet_api_secret: SecretStr | None = None

    # --- Kraken (live mode only; never read unless the Phase 7 gates pass) ---
    kraken_api_key: SecretStr | None = None
    kraken_api_secret: SecretStr | None = None

    # --- Interactive Brokers, via IB Gateway (deploy/ runs it with IBC) ---
    # 4002 = Gateway paper, 4001 = Gateway live. The engine never logs in
    # itself; IBC holds the username/password inside the gateway container.
    ibkr_host: str = "127.0.0.1"
    ibkr_paper_port: int = 4002
    ibkr_live_port: int = 4001
    ibkr_client_id: int = 17
    # Account ids are never committed (repo is public); optional, used to
    # pick the right account when a login sees more than one.
    ibkr_paper_account: SecretStr | None = None
    ibkr_live_account: SecretStr | None = None

    # --- alerts (ntfy) ---
    ntfy_url: str | None = None  # e.g. http://127.0.0.1:<port> for the box's own ntfy
    ntfy_topic: str = "kestrel"
    ntfy_token: SecretStr | None = None

    # --- live trading master switch (spec Section 13, gate 1 of 2) ---
    live_trading_enabled: bool = False

    # --- service ports (all bound to 127.0.0.1; ADR 0003) ---
    metrics_port: int = 9710
    metrics_addr: str = "127.0.0.1"  # the container sets 0.0.0.0; Compose publishes on 127.0.0.1
    api_port: int = 8710
    admin_port: int = 8711

    # --- misc ---
    log_level: str = "INFO"
    log_json: bool = True
