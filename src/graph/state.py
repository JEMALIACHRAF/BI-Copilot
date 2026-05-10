"""Graph state — the single source of truth flowing between agents.

Every node reads from and writes to this object. We keep it strict
(Pydantic) rather than a free-form TypedDict so a typo in a field name
fails fast in tests instead of silently propagating.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TableRef(BaseModel):
    """A single qualified table reference produced by the Planner."""

    project: str
    dataset: str
    table: str
    relevance: float = Field(ge=0.0, le=1.0)
    reason: str

    @property
    def fqn(self) -> str:
        """Fully-qualified BigQuery name, ready to drop into SQL."""
        return f"`{self.project}.{self.dataset}.{self.table}`"


class ExecutionPlan(BaseModel):
    """Planner output — what the SQL agent will work from."""

    intent: str
    tables: list[TableRef]
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    time_window: str | None = None
    requires_window_function: bool = False
    requires_cte: bool = False
    notes: str = ""


class SqlAttempt(BaseModel):
    """One iteration of the ReAct loop in the SQL agent."""

    iteration: int
    sql: str
    error: str | None = None
    error_kind: Literal["syntax", "dry_run", "runtime", "none"] = "none"
    duration_ms: int


class QueryResult(BaseModel):
    """Tabular result from BigQuery, capped to a sensible row count."""

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    bytes_processed: int
    cached: bool = False

    @property
    def is_empty(self) -> bool:
        return self.row_count == 0


class VizSpec(BaseModel):
    """Vega-Lite spec produced by the Viz Agent."""

    chart_type: Literal["bar", "line", "scatter", "table", "kpi", "heatmap", "pie"]
    spec: dict[str, Any]
    rationale: str


class Narrative(BaseModel):
    """Final natural-language summary."""

    headline: str
    summary: str
    key_insights: list[str]
    follow_up_questions: list[str]


class GraphState(BaseModel):
    """Full state passed through the LangGraph workflow."""

    # ── Input ────────────────────────────────────────────────────────
    question: str
    user_id: str | None = None
    started_at: datetime = Field(default_factory=datetime.utcnow)

    # ── Planner output ──────────────────────────────────────────────
    plan: ExecutionPlan | None = None

    # ── SQL agent state ─────────────────────────────────────────────
    sql_attempts: list[SqlAttempt] = Field(default_factory=list)
    final_sql: str | None = None
    query_result: QueryResult | None = None

    # ── Downstream agents ───────────────────────────────────────────
    viz: VizSpec | None = None
    narrative: Narrative | None = None

    # ── Control flow ────────────────────────────────────────────────
    error: str | None = None
    should_replan: bool = False
    is_complete: bool = False

    # ── Metrics ─────────────────────────────────────────────────────
    total_tokens: int = 0
    total_cost_usd: float = 0.0
