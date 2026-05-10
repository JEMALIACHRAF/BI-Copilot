"""Application configuration via environment variables.

All settings are loaded once at startup and immutable thereafter.
Follows the 12-factor app config principle: every deployment-specific
value comes from the environment, never from code.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the BI Copilot service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────
    app_name: str = "bi-copilot"
    env: Literal["dev", "staging", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # ── LLM ──────────────────────────────────────────────────────────
    openai_api_key: str = Field(..., description="OpenAI API key")
    llm_model: str = "gpt-4o"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 2048
    llm_timeout_seconds: int = 60

    # ── BigQuery ─────────────────────────────────────────────────────
    gcp_project_id: str = Field(..., description="GCP project ID (where queries are billed)")
    bq_dataset: str = "bi_copilot"
    # Optional override — set this when the data lives in a different project than
    # the one paying for the queries (e.g. `bigquery-public-data` for public sets).
    # When unset, defaults to gcp_project_id.
    bq_dataset_project: str | None = None
    bq_location: str = "EU"
    bq_max_bytes_billed: int = 10 * 1024**3  # 10 GiB hard cap per query
    bq_query_timeout_seconds: int = 30

    # ── Agent loop ──────────────────────────────────────────────────
    sql_max_iterations: int = 5
    sql_dry_run_required: bool = True
    planner_max_tables: int = 8

    # ── API ─────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_cors_origins: list[str] = ["http://localhost:5173"]
    api_rate_limit_per_minute: int = 30

    # ── Observability ───────────────────────────────────────────────
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def data_project(self) -> str:
        """Project that hosts the queried data (may differ from the billing project)."""
        return self.bq_dataset_project or self.gcp_project_id


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance.

    Cached so we read the environment exactly once per process.
    Tests can override via `Settings(...)` directly.
    """
    return Settings()  # type: ignore[call-arg]