"""API request and response schemas.

Kept separate from internal graph state so the public contract can
evolve independently of the internal pipeline.
"""

from typing import Any

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    user_id: str | None = None


class SqlAttemptDTO(BaseModel):
    iteration: int
    sql: str
    error: str | None
    error_kind: str
    duration_ms: int


class QueryMetadata(BaseModel):
    sql_attempts: int
    total_latency_ms: int
    cost_usd: float
    rows_scanned: int
    cached: bool
    tokens_used: int


class AskResponse(BaseModel):
    question: str
    sql: str | None
    data: list[dict[str, Any]]
    columns: list[str]
    viz: dict[str, Any] | None
    narrative: dict[str, Any] | None
    follow_ups: list[str]
    metadata: QueryMetadata
    error: str | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    checks: dict[str, bool]
