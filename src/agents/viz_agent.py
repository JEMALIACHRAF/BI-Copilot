"""Viz agent — chart-type inference + minimal Vega-Lite spec."""

import json

from src.core.llm import LLMClient
from src.core.logging import get_logger
from src.graph.state import GraphState, VizSpec
from src.prompts.viz import VIZ_SYSTEM, VIZ_USER

logger = get_logger(__name__)

_MAX_SAMPLE = 10


class VizAgent:
    """Chooses a chart type and emits a Vega-Lite spec."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm or LLMClient()

    def __call__(self, state: GraphState) -> dict:
        log = logger.bind(agent="viz")

        if state.query_result is None or state.query_result.is_empty:
            return {
                "viz": VizSpec(chart_type="table", spec={}, rationale="No rows to visualize."),
            }

        result = state.query_result

        # Heuristic shortcut: 1 row × 1 numeric value → KPI, no LLM call needed.
        if result.row_count == 1 and len(result.columns) == 1:
            return {
                "viz": VizSpec(
                    chart_type="kpi",
                    spec={},
                    rationale="Single scalar result is best displayed as a KPI tile.",
                )
            }

        sample = result.rows[:_MAX_SAMPLE]
        schema_lines = [f"  • {col}" for col in result.columns]

        user = VIZ_USER.format(
            question=state.question,
            schema="\n".join(schema_lines),
            sample_size=len(sample),
            total=result.row_count,
            sample=json.dumps(sample, indent=2, default=str),
        )

        response = self._llm.complete(VIZ_SYSTEM, user, json_mode=True)

        try:
            payload = response.parse_json()
            viz = VizSpec(
                chart_type=payload.get("chart_type", "table"),
                spec=payload.get("spec", {}),
                rationale=payload.get("rationale", ""),
            )
        except (ValueError, KeyError) as exc:
            log.warning("viz.parse_failed", error=str(exc))
            viz = VizSpec(chart_type="table", spec={}, rationale="Falling back to table view.")

        log.info("viz.chosen", chart_type=viz.chart_type)

        return {
            "viz": viz,
            "total_tokens": state.total_tokens + response.input_tokens + response.output_tokens,
            "total_cost_usd": state.total_cost_usd + response.cost_usd,
        }
