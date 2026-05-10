"""SQL agent — text-to-SQL with self-correcting ReAct loop.

Per ReAct, each iteration:
  1. THINK: read the plan + (on retry) the previous error
  2. ACT:   produce SQL
  3. OBSERVE: validate (sqlglot) → dry-run (BigQuery) → record outcome

We bail out as soon as a query passes validation + dry-run, or when we
hit `sql_max_iterations`. Every attempt is recorded in `state.sql_attempts`
so the API can surface the recovery trail to a debugger UI.
"""

import re
import time

from src.core.config import get_settings
from src.core.llm import LLMClient
from src.core.logging import get_logger
from src.graph.state import GraphState, SqlAttempt
from src.prompts.sql import SQL_SYSTEM, SQL_USER_INITIAL, SQL_USER_RETRY
from src.tools.bigquery_tool import BigQueryTool
from src.tools.schema_inspector import SchemaInspector
from src.tools.sql_validator import validate

logger = get_logger(__name__)

# LLMs occasionally wrap output in ```sql ... ``` despite prompt instructions.
# This regex pulls the content out of any fenced block, optionally tagged
# (```sql, ```SQL, ```, ```bigquery, ...).
_FENCE_RE = re.compile(r"^```(?:[a-zA-Z]+)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Remove surrounding markdown code fences if present, otherwise return as-is."""
    text = text.strip()
    match = _FENCE_RE.match(text)
    if match:
        return match.group(1).strip()
    return text


class SqlAgent:
    """Generates and self-corrects SQL until it passes validation + dry-run."""

    def __init__(
        self,
        llm: LLMClient | None = None,
        bq: BigQueryTool | None = None,
        schema_inspector: SchemaInspector | None = None,
    ) -> None:
        self._settings = get_settings()
        self._llm = llm or LLMClient()
        self._bq = bq or BigQueryTool()
        self._schema = schema_inspector or SchemaInspector()

    def __call__(self, state: GraphState) -> dict:
        log = logger.bind(agent="sql", question=state.question)

        if state.plan is None:
            return {"error": "SQL agent invoked without a plan", "is_complete": True}

        plan = state.plan
        table_schemas = self._render_chosen_tables(plan)

        attempts: list[SqlAttempt] = list(state.sql_attempts)
        previous_sql: str | None = None
        previous_error: str | None = None
        total_tokens_delta = 0
        total_cost_delta = 0.0

        for iteration in range(1, self._settings.sql_max_iterations + 1):
            iter_start = time.perf_counter()

            # ── 1. ASK THE LLM ──────────────────────────────────────
            if previous_sql is None:
                user = SQL_USER_INITIAL.format(
                    plan=self._render_plan(plan),
                    table_schemas=table_schemas,
                    question=state.question,
                )
            else:
                user = SQL_USER_RETRY.format(
                    previous_sql=previous_sql,
                    error=previous_error,
                    plan=self._render_plan(plan),
                    table_schemas=table_schemas,
                    question=state.question,
                )

            response = self._llm.complete(SQL_SYSTEM, user)
            total_tokens_delta += response.input_tokens + response.output_tokens
            total_cost_delta += response.cost_usd
            sql_raw = _strip_code_fence(response.content)

            # ── 2. STATIC VALIDATE ──────────────────────────────────
            validation = validate(sql_raw, enforce_limit=True)
            if not validation.is_valid:
                attempts.append(
                    SqlAttempt(
                        iteration=iteration,
                        sql=sql_raw,
                        error=validation.error,
                        error_kind="syntax",
                        duration_ms=int((time.perf_counter() - iter_start) * 1000),
                    )
                )
                log.warning(
                    "sql.syntax_error",
                    iteration=iteration,
                    error=validation.error,
                )
                previous_sql = sql_raw
                previous_error = validation.error
                continue

            normalized_sql = validation.normalized_sql or sql_raw

            # ── 3. DRY-RUN ──────────────────────────────────────────
            if self._settings.sql_dry_run_required:
                dry = self._bq.dry_run(normalized_sql)
                if not dry.valid:
                    attempts.append(
                        SqlAttempt(
                            iteration=iteration,
                            sql=normalized_sql,
                            error=dry.error,
                            error_kind="dry_run",
                            duration_ms=int((time.perf_counter() - iter_start) * 1000),
                        )
                    )
                    log.warning(
                        "sql.dry_run_failed",
                        iteration=iteration,
                        error=dry.error,
                    )
                    previous_sql = normalized_sql
                    previous_error = dry.error
                    continue

            # ── 4. SUCCESS — EXECUTE ────────────────────────────────
            attempts.append(
                SqlAttempt(
                    iteration=iteration,
                    sql=normalized_sql,
                    error=None,
                    error_kind="none",
                    duration_ms=int((time.perf_counter() - iter_start) * 1000),
                )
            )

            try:
                result = self._bq.execute(normalized_sql)
            except Exception as exc:  # noqa: BLE001 — surface as runtime error
                log.error("sql.execute_failed", error=str(exc))
                return {
                    "sql_attempts": attempts,
                    "final_sql": normalized_sql,
                    "error": f"Query execution failed: {exc}",
                    "is_complete": True,
                    "total_tokens": state.total_tokens + total_tokens_delta,
                    "total_cost_usd": state.total_cost_usd + total_cost_delta,
                }

            log.info(
                "sql.success",
                iteration=iteration,
                rows=result.row_count,
                bytes=result.bytes_processed,
                cached=result.cached,
            )
            return {
                "sql_attempts": attempts,
                "final_sql": normalized_sql,
                "query_result": result,
                "total_tokens": state.total_tokens + total_tokens_delta,
                "total_cost_usd": state.total_cost_usd + total_cost_delta,
            }

        # ── Loop exhausted ───────────────────────────────────────────
        log.error("sql.max_iterations_reached", iterations=self._settings.sql_max_iterations)
        return {
            "sql_attempts": attempts,
            "error": (
                f"SQL agent could not produce a valid query after "
                f"{self._settings.sql_max_iterations} attempts. Last error: {previous_error}"
            ),
            "is_complete": True,
            "total_tokens": state.total_tokens + total_tokens_delta,
            "total_cost_usd": state.total_cost_usd + total_cost_delta,
        }

    def _render_chosen_tables(self, plan) -> str:
        """Compact schema view limited to the tables the planner picked."""
        chosen = {(t.dataset, t.table) for t in plan.tables}
        all_tables = self._schema.list_tables()
        relevant = [t for t in all_tables if (t.dataset, t.table) in chosen]
        return "\n\n".join(t.render_for_prompt() for t in relevant)

    @staticmethod
    def _render_plan(plan) -> str:
        """Human-readable plan section for the SQL prompt."""
        lines = [f"Intent: {plan.intent}"]
        if plan.metrics:
            lines.append(f"Metrics: {', '.join(plan.metrics)}")
        if plan.dimensions:
            lines.append(f"Dimensions: {', '.join(plan.dimensions)}")
        if plan.filters:
            lines.append(f"Filters: {'; '.join(plan.filters)}")
        if plan.time_window:
            lines.append(f"Time window: {plan.time_window}")
        if plan.requires_window_function:
            lines.append("→ Window functions required.")
        if plan.requires_cte:
            lines.append("→ Use a WITH clause for staged aggregation.")
        if plan.notes:
            lines.append(f"Notes: {plan.notes}")
        return "\n".join(lines)