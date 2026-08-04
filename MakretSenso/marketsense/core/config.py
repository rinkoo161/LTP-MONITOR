"""Typed settings, loaded once per process from env / .env.

Unlike ltp-monitor's config.py (whose save() silently drops unregistered
keys — a documented recurring trap there), this is pydantic-settings:
an unknown MS_* variable is a startup ERROR, not a silent no-op, and a
missing one falls back to the default declared here. Config is read-only
at runtime; there is no save() to drop anything.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",  # unknown MS_* keys fail loudly at startup
    )

    # database
    db_url: str = "postgresql+psycopg://localhost:5432/marketsense"
    db_url_test: str = "postgresql+psycopg://localhost:5432/marketsense_test"

    # NSE politeness budget (see net/budget.py for semantics)
    nse_budget_per_min: int = 30
    nse_breaker_threshold: int = 2
    nse_breaker_cooldown: int = 180

    # storage
    data_dir: Path = Path("~/.marketsense")

    # LLM (Phase 2; registered now so the config surface is stable)
    ai_engine: str = "local"  # local | online | auto | off
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"

    # api
    api_host: str = "127.0.0.1"
    api_port: int = 8100  # ltp-monitor owns 8000

    @field_validator("data_dir")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()

    @field_validator("ai_engine")
    @classmethod
    def _engine(cls, v: str) -> str:
        allowed = {"local", "online", "auto", "off"}
        if v not in allowed:
            raise ValueError(f"ai_engine must be one of {sorted(allowed)}, got {v!r}")
        return v

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "documents"


@lru_cache(maxsize=1)
def settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.pdf_dir.mkdir(parents=True, exist_ok=True)
    return s
