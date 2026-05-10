"""Thin BigQuery wrapper exposing two operations: dry-run and execute.

Dry-run is *cheap* (no bytes billed, no slot time) and tells us whether
the engine accepts the query and how many bytes it would scan. We use
this in the SQL agent's ReAct loop to catch errors that static parsing
misses (bad column names, type mismatches, joins on incompatible types).

Execute enforces:
  • a hard byte cap (`bq_max_bytes_billed`) so a runaway query can't
    cost us €€€,
  • a wall-clock timeout,
  • automatic conversion to a tabular `QueryResult`.
"""

import time
from dataclasses import dataclass

from google.api_core.exceptions import BadRequest, GoogleAPIError
from google.cloud import bigquery
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.core.config import get_settings
from src.core.logging import get_logger
from src.graph.state import QueryResult

logger = get_logger(__name__)


@dataclass(frozen=True)
class DryRunInfo:
    valid: bool
    error: str | None
    bytes_processed: int
    referenced_tables: tuple[str, ...]


class BigQueryTool:
    """High-level BigQuery client used by the SQL agent."""

    def __init__(self, client: bigquery.Client | None = None) -> None:
        self._settings = get_settings()
        self._client = client or bigquery.Client(project=self._settings.gcp_project_id)

    # ── Dry-run ──────────────────────────────────────────────────────
    def dry_run(self, sql: str) -> DryRunInfo:
        """Validate + estimate without scanning any bytes."""
        config = bigquery.QueryJobConfig(
            dry_run=True,
            use_query_cache=False,
            maximum_bytes_billed=self._settings.bq_max_bytes_billed,
        )
        try:
            job = self._client.query(sql, job_config=config, location=self._settings.bq_location)
            tables = tuple(
                f"{t.project}.{t.dataset_id}.{t.table_id}" for t in (job.referenced_tables or [])
            )
            return DryRunInfo(
                valid=True,
                error=None,
                bytes_processed=int(job.total_bytes_processed or 0),
                referenced_tables=tables,
            )
        except BadRequest as exc:
            return DryRunInfo(
                valid=False,
                error=_format_bq_error(exc),
                bytes_processed=0,
                referenced_tables=(),
            )
        except GoogleAPIError as exc:
            return DryRunInfo(
                valid=False,
                error=str(exc),
                bytes_processed=0,
                referenced_tables=(),
            )

    # ── Execute ──────────────────────────────────────────────────────
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(GoogleAPIError),
        reraise=True,
    )
    def execute(self, sql: str) -> QueryResult:
        """Run the query and return a `QueryResult`. Retries transient errors."""
        config = bigquery.QueryJobConfig(
            use_query_cache=True,
            maximum_bytes_billed=self._settings.bq_max_bytes_billed,
            priority=bigquery.QueryPriority.INTERACTIVE,
        )
        start = time.perf_counter()
        job = self._client.query(sql, job_config=config, location=self._settings.bq_location)

        # IMPORTANT: read schema from the RowIterator returned by .result(), not
        # from `job.schema` — the job-level schema is sometimes None for SELECT
        # queries until the row iterator has been materialized.
        row_iterator = job.result(timeout=self._settings.bq_query_timeout_seconds)
        schema = row_iterator.schema or job.schema or []
        columns = [field.name for field in schema]

        records = [{col: _coerce(row[col]) for col in columns} for row in row_iterator]

        elapsed_ms = int((time.perf_counter() - start) * 1000)

        logger.info(
            "bigquery.executed",
            elapsed_ms=elapsed_ms,
            rows=len(records),
            bytes=int(job.total_bytes_processed or 0),
            cached=bool(job.cache_hit),
        )

        return QueryResult(
            columns=columns,
            rows=records,
            row_count=len(records),
            bytes_processed=int(job.total_bytes_processed or 0),
            cached=bool(job.cache_hit),
        )


def _coerce(value: object) -> object:
    """Normalize BigQuery types to JSON-friendly Python types."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):  # date / datetime / timestamp
        return value.isoformat()
    if hasattr(value, "__float__") and not isinstance(value, (int, float, bool)):
        return float(value)  # Decimal / NUMERIC
    return value


def _format_bq_error(exc: BadRequest) -> str:
    """Pull the most useful chunk out of a BadRequest for LLM feedback."""
    if exc.errors:
        first = exc.errors[0]
        msg = first.get("message", str(exc))
        location = first.get("location", "")
        return f"{msg}" + (f"  [at {location}]" if location else "")
    return str(exc)


def estimate_cost_usd(bytes_processed: int) -> float:
    """BigQuery on-demand pricing: $5 per TiB scanned."""
    tib = bytes_processed / (1024**4)
    return round(tib * 5.0, 4)