"""Confirms Settings is safe to construct without any env vars set
(sane defaults) and does not read os.environ at import time — the
exact bug the spec's "lessons from previous projects" section warns
about (Section 2).
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("KESTREL_"):
            monkeypatch.delenv(key, raising=False)


def test_settings_has_sane_defaults_without_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    from config.settings import Settings

    settings = Settings()
    assert settings.data_dir == Path("market_data")
    assert settings.binance_testnet_api_key is None
    assert settings.kraken_api_key is None
    assert settings.live_trading_enabled is False


def test_settings_reads_env_vars_set_after_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Import the module first, *then* set env vars, then construct —
    this is only possible if nothing read os.environ at import time."""
    sys.modules.pop("config.settings", None)
    import config.settings

    monkeypatch.setenv("KESTREL_BINANCE_TESTNET_API_KEY", "test-key-id")
    monkeypatch.setenv("KESTREL_LIVE_TRADING_ENABLED", "true")

    settings = config.settings.Settings(_env_file=None)
    assert settings.binance_testnet_api_key is not None
    assert settings.binance_testnet_api_key.get_secret_value() == "test-key-id"
    assert settings.live_trading_enabled is True


def test_settings_module_has_no_module_level_settings_instance() -> None:
    """Guard against a regression to a cached-singleton-at-import-time
    pattern: the module must not expose a ready-made Settings object."""
    importlib.reload(importlib.import_module("config.settings"))
    import config.settings

    for name in dir(config.settings):
        value = getattr(config.settings, name)
        assert not isinstance(value, config.settings.Settings), (
            f"config.settings.{name} is a module-level Settings instance"
        )
