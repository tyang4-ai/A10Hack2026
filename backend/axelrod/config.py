"""Runtime settings, read from the environment with backend/.env as the fallback.

`get_settings()` is the only place that reads `.env`, and only on its first call. Real environment
variables win over the file. Empty values count as unset. AXELROD_ENV_FILE overrides the file path
(an empty value means no file at all, which the tests use).
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

BACKEND_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = BACKEND_DIR / ".env"

# Settings field -> environment variable.
ENV_VARS: dict[str, str] = {
    "store": "STORE",
    "aws_region": "AWS_REGION",
    "model_large": "MODEL_LARGE",
    "model_small": "MODEL_SMALL",
    "guardrail_id": "GUARDRAIL_ID",
    "guardrail_version": "GUARDRAIL_VERSION",
    "vault_bucket": "VAULT_BUCKET",
    "runs_table": "RUNS_TABLE",
    "events_table": "EVENTS_TABLE",
    "data_dir": "DATA_DIR",
    "max_concurrency_small": "MAX_CONCURRENCY_SMALL",
    "max_concurrency_large": "MAX_CONCURRENCY_LARGE",
    "budget_usd": "BUDGET_USD",
    "prices": "PRICES_JSON",
    "fake_models": "FAKE_MODELS",
    "http_user_agent": "HTTP_USER_AGENT",
    "api_key": "AXELROD_API_KEY",
    "public_ui_url": "PUBLIC_UI_URL",
    "stage_pacing": "STAGE_PACING",
    "protected_runs": "PROTECTED_RUNS",
    "test_page_url": "TEST_PAGE_URL",
    "cache_documents": "CACHE_DOCUMENTS",
    "cache_models": "CACHE_MODELS",
}


class Settings(BaseModel):
    store: Literal["local", "aws"] = "local"                 # STORE
    aws_region: str = "us-east-1"                            # AWS_REGION
    model_large: str = "us.anthropic.claude-sonnet-5-5"      # MODEL_LARGE (A-setup confirms)
    model_small: str = "us.anthropic.claude-haiku-5-5"       # MODEL_SMALL
    guardrail_id: str | None = None                          # GUARDRAIL_ID
    guardrail_version: str | None = None                     # GUARDRAIL_VERSION
    vault_bucket: str | None = None                          # VAULT_BUCKET
    runs_table: str | None = None                            # RUNS_TABLE
    events_table: str | None = None                          # EVENTS_TABLE
    data_dir: Path = Path("backend/.data")                   # DATA_DIR (local store and vault)
    max_concurrency_small: int = 16                          # MAX_CONCURRENCY_SMALL
    max_concurrency_large: int = 4                           # MAX_CONCURRENCY_LARGE
    budget_usd: float = 5.0                                  # BUDGET_USD (per run)
    prices: dict[str, dict[str, float]] = {}                 # PRICES_JSON {"<model id>": {"in": usd_per_mtok, "out": usd_per_mtok}}
    fake_models: bool = False                                # FAKE_MODELS
    http_user_agent: str = "Axelrod/0.1 (hackathon research harness)"   # HTTP_USER_AGENT
    api_key: str | None = None                               # AXELROD_API_KEY
    public_ui_url: str | None = None                         # PUBLIC_UI_URL (CLI prints run links)
    stage_pacing: float = 0.0                                # STAGE_PACING (seconds of pause at the start of each stage)
    protected_runs: list[str] = []                           # PROTECTED_RUNS comma-separated run ids DELETE refuses
    test_page_url: str | None = None                         # TEST_PAGE_URL (planted page S-010, served by our own API)
    cache_documents: bool = False                            # CACHE_DOCUMENTS on/off: serve fetch bodies from the S3 cache
    cache_models: Literal["off", "record", "replay"] = "off"  # CACHE_MODELS: record or replay model answers


def _env_file_from_environ() -> Path | None:
    override = os.environ.get("AXELROD_ENV_FILE")
    if override is None:
        return ENV_FILE
    return Path(override) if override.strip() else None


def load_settings(env_file: Path | None = ENV_FILE, environ: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from env_file (python-dotenv, if it exists) with environ on top (default os.environ).

    Uncached and side-effect free: os.environ is never modified.
    """
    from dotenv import dotenv_values

    raw: dict[str, str] = {}
    if env_file is not None and Path(env_file).is_file():
        raw.update({k: v for k, v in dotenv_values(env_file, encoding="utf-8").items() if v is not None})
    raw.update(os.environ if environ is None else environ)

    values: dict[str, object] = {}
    for field, var in ENV_VARS.items():
        value = raw.get(var)
        if value is None or not value.strip():
            continue
        value = value.strip()
        if field == "prices":
            try:
                values[field] = json.loads(value)
            except json.JSONDecodeError as e:
                raise ValueError(f"PRICES_JSON is not valid JSON: {e}") from e
        elif field in ("store", "cache_models", "cache_documents"):
            values[field] = value.lower()
        elif field == "protected_runs":
            values[field] = [x.strip() for x in value.split(",") if x.strip()]
        else:
            values[field] = value
    return Settings.model_validate(values)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Cached settings. The first call loads backend/.env with python-dotenv, then the real environment wins."""
    global _settings
    if _settings is None:
        _settings = load_settings(_env_file_from_environ())
    return _settings


def set_settings(settings: Settings | None) -> None:
    """Replace the cached settings (tests do this), or pass None so the next get_settings() reloads."""
    global _settings
    _settings = settings


# Same name as functools.lru_cache's, for anyone who reaches for it.
get_settings.cache_clear = lambda: set_settings(None)  # type: ignore[attr-defined]
