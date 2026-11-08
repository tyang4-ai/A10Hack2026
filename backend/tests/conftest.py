"""Shared pytest fixtures.

- Every test not marked `live` runs offline: STORE=local, a temp DATA_DIR, FAKE_MODELS=1, no AWS resource
  names, and backend/.env is never read. `settings` gives you that Settings object (get_settings() returns it too).
- Tests marked `@pytest.mark.live` are skipped unless AXELROD_LIVE=1, and they read the real settings.
- `fixture_bundle` is the mock's full fixture (fictional company), `fixture_report` its AgentReport.
"""
from __future__ import annotations

import copy
import os
from collections.abc import Iterator

import pytest

from backend.axelrod import config as axconfig
from backend.axelrod.contract import m

LIVE = os.environ.get("AXELROD_LIVE") == "1"

# Env vars that could point a test at real AWS resources or change the offline defaults.
_SCRUB = [var for field, var in axconfig.ENV_VARS.items() if field not in ("store", "data_dir", "fake_models")]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if LIVE:
        return
    skip = pytest.mark.skip(reason="calls AWS: set AXELROD_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _offline_settings(request: pytest.FixtureRequest, tmp_path, monkeypatch) -> Iterator[axconfig.Settings | None]:
    if request.node.get_closest_marker("live"):
        axconfig.set_settings(None)
        yield None
        axconfig.set_settings(None)
        return
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env = {"STORE": "local", "DATA_DIR": str(data_dir), "FAKE_MODELS": "1"}
    for var in _SCRUB:
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setenv("AXELROD_ENV_FILE", "")  # subprocesses skip backend/.env too
    settings = axconfig.load_settings(env_file=None, environ=env)
    axconfig.set_settings(settings)
    yield settings
    axconfig.set_settings(None)


@pytest.fixture
def settings(_offline_settings: axconfig.Settings | None) -> axconfig.Settings:
    """Offline settings (STORE=local, temp DATA_DIR, FAKE_MODELS=1). Live tests get the real get_settings()."""
    return _offline_settings if _offline_settings is not None else axconfig.get_settings()


@pytest.fixture(scope="session")
def fixture_bundle() -> dict:
    """mock/build_fixtures.build(): needs, sources, docs, items, report, summary, timelines, page_text. Read-only."""
    from mock import build_fixtures

    return build_fixtures.build()


@pytest.fixture
def fixture_report(fixture_bundle: dict) -> m.AgentReport:
    """A realistic, fully verified AgentReport (fictional company), fresh for each test."""
    return m.AgentReport.model_validate(copy.deepcopy(fixture_bundle["report"]))
