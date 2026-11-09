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
    assert settings.data_dir == Path("data")
    assert settings.alpaca_api_key_id is None
    assert settings.alpaca_api_secret_key is None


def test_settings_reads_env_vars_set_after_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Import the module first, *then* set env vars, then construct —
    this is only possible if nothing read os.environ at import time."""
    sys.modules.pop("config.settings", None)
    import config.settings

    monkeypatch.setenv("KESTREL_ALPACA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("KESTREL_ALPACA_API_SECRET_KEY", "test-secret")

    settings = config.settings.Settings(_env_file=None)
    assert settings.alpaca_api_key_id is not None
    assert settings.alpaca_api_key_id.get_secret_value() == "test-key-id"


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
