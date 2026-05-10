"""BigQuery schema inspector with in-process caching + value sampling.

Two upgrades over a naive schema dump:

1. Cross-project safety. Uses the BigQuery REST API (`list_tables` +
   `get_table`) instead of INFORMATION_SCHEMA queries. INFORMATION_SCHEMA
   doesn't grant read access across project boundaries, but the table
   metadata REST endpoints work seamlessly — essential when querying
   public datasets from a billing project.

2. Value sampling. For STRING columns, we run a single `APPROX_TOP_COUNT`
   query per table to surface the most frequent values. This is what
   prevents the SQL agent from hallucinating string filters
   (`status = 'completed'` vs the real `'Complete'`). Values are only kept
   when the top one repeats ≥10 times, which filters out high-cardinality
   ID-like columns automatically.

The cache is invalidated by TTL — a 5-minute window catches DDL changes
during dev without hammering the API in prod.
"""

import time
from dataclasses import dataclass
from typing import Any

from google.cloud import bigquery

from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)

_CACHE_TTL_SECONDS = 300
_TOP_VALUES_PER_COLUMN = 20
_MIN_TOP_COUNT_TO_SHOW = 10  # filters out ID-like high-cardinality columns
_DISPLAY_VALUES_PER_COLUMN = 8  # cap shown in the prompt


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    data_type: str
    description: str | None
    is_partitioning: bool
    is_clustering: bool
    sample_values: tuple[str, ...] = ()  # top frequent values for STRING cols


@dataclass(frozen=True)
class TableInfo:
    project: str
    dataset: str
    table: str
    description: str | None
    row_count: int
    columns: tuple[ColumnInfo, ...]

    def render_for_prompt(self) -> str:
        """Compact textual representation for inclusion in LLM prompts."""
        header = f"Table `{self.project}.{self.dataset}.{self.table}`"
        if self.description:
            header += f" — {self.description}"
        header += f"  ({self.row_count:,} rows)"

        col_lines = []
        for c in self.columns:
            tag = ""
            if c.is_partitioning:
                tag = " [PARTITION]"
            elif c.is_clustering:
                tag = " [CLUSTER]"
            desc = f" — {c.description}" if c.description else ""

            line = f"  • {c.name} ({c.data_type}){tag}{desc}"
            if c.sample_values:
                shown = c.sample_values[:_DISPLAY_VALUES_PER_COLUMN]
                quoted = ", ".join(f"'{v}'" for v in shown)
                line += f"\n      values: {quoted}"
                if len(c.sample_values) > _DISPLAY_VALUES_PER_COLUMN:
                    line += f" (and {len(c.sample_values) - _DISPLAY_VALUES_PER_COLUMN} more)"
            col_lines.append(line)

        return header + "\n" + "\n".join(col_lines)


class SchemaInspector:
    """Read-only view of dataset metadata, cached for reuse across requests."""

    def __init__(self, client: bigquery.Client | None = None) -> None:
        self._settings = get_settings()
        # Client uses the billing project; it can still query tables in other projects.
        self._client = client or bigquery.Client(project=self._settings.gcp_project_id)
        self._cache: dict[str, tuple[float, list[TableInfo]]] = {}

    def list_tables(self, dataset: str | None = None) -> list[TableInfo]:
        """Return all tables in the dataset, with full column metadata + samples."""
        dataset = dataset or self._settings.bq_dataset
        data_project = self._settings.data_project
        cache_key = f"{data_project}.{dataset}"

        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0] < _CACHE_TTL_SECONDS):
            return cached[1]

        tables = self._fetch_tables(data_project, dataset)
        self._cache[cache_key] = (time.time(), tables)
        logger.info(
            "schema.refreshed",
            project=data_project,
            dataset=dataset,
            table_count=len(tables),
        )
        return tables

    def _fetch_tables(self, project: str, dataset: str) -> list[TableInfo]:
        """List all tables in `project.dataset` via REST + sample STRING columns."""
        dataset_ref = f"{project}.{dataset}"
        results: list[TableInfo] = []

        for stub in self._client.list_tables(dataset_ref):
            full = self._client.get_table(f"{project}.{dataset}.{stub.table_id}")

            partition_field: str | None = None
            if full.time_partitioning is not None:
                partition_field = full.time_partitioning.field
            cluster_fields = set(full.clustering_fields or [])

            string_columns = [
                f.name for f in (full.schema or []) if f.field_type == "STRING"
            ]
            samples = self._fetch_string_samples(
                project, dataset, full.table_id, string_columns
            )

            cols = tuple(
                ColumnInfo(
                    name=field.name,
                    data_type=field.field_type,
                    description=field.description,
                    is_partitioning=(field.name == partition_field),
                    is_clustering=(field.name in cluster_fields),
                    sample_values=samples.get(field.name, ()),
                )
                for field in (full.schema or [])
            )

            results.append(
                TableInfo(
                    project=project,
                    dataset=dataset,
                    table=full.table_id,
                    description=full.description,
                    row_count=int(full.num_rows or 0),
                    columns=cols,
                )
            )
        return results

    def _fetch_string_samples(
        self,
        project: str,
        dataset: str,
        table: str,
        columns: list[str],
    ) -> dict[str, tuple[str, ...]]:
        """One `APPROX_TOP_COUNT` query covering every STRING column at once.

        Columns whose most frequent value repeats fewer than
        `_MIN_TOP_COUNT_TO_SHOW` times are skipped — they're almost certainly
        ID-like (one row per value), and listing 20 random IDs adds noise to
        the prompt without helping the SQL agent.
        """
        if not columns:
            return {}

        select_clauses = [
            f"APPROX_TOP_COUNT(`{col}`, {_TOP_VALUES_PER_COLUMN}) AS `top_{i}`"
            for i, col in enumerate(columns)
        ]
        sql = (
            f"SELECT {', '.join(select_clauses)} "
            f"FROM `{project}.{dataset}.{table}`"
        )

        result: dict[str, tuple[str, ...]] = {}
        try:
            job = self._client.query(sql, location=self._settings.bq_location)
            rows = list(job.result(timeout=15))
            if not rows:
                return {}
            row = rows[0]
            for i, col in enumerate(columns):
                top_list = row[f"top_{i}"]
                if not top_list:
                    continue
                # APPROX_TOP_COUNT returns: [{value: ..., count: ...}, ...]
                first_count = top_list[0].get("count", 0) if top_list else 0
                if first_count < _MIN_TOP_COUNT_TO_SHOW:
                    continue  # high-cardinality column, skip
                values = tuple(
                    str(item["value"])
                    for item in top_list
                    if item.get("value") is not None
                )
                if values:
                    result[col] = values
        except Exception as exc:  # noqa: BLE001 — sampling failure shouldn't kill the request
            logger.warning(
                "schema.value_sampling_failed",
                table=f"{project}.{dataset}.{table}",
                error=str(exc),
            )

        return result

    def render_dataset_summary(self, dataset: str | None = None) -> str:
        """Compact prompt-ready summary of the entire dataset."""
        tables = self.list_tables(dataset)
        return "\n\n".join(t.render_for_prompt() for t in tables)


def to_dict(table_info: TableInfo) -> dict[str, Any]:
    """Serialize for JSON responses / observability payloads."""
    return {
        "fqn": f"{table_info.project}.{table_info.dataset}.{table_info.table}",
        "description": table_info.description,
        "row_count": table_info.row_count,
        "columns": [
            {
                "name": c.name,
                "type": c.data_type,
                "description": c.description,
                "sample_values": list(c.sample_values),
            }
            for c in table_info.columns
        ],
    }