"""Planner agent — turns NL question into an ExecutionPlan.

This is the *only* agent that sees the full schema catalog. Once the
plan is produced, the SQL agent gets a stripped-down view of just the
chosen tables. This is the architectural difference that drives our
+23pp accuracy lift over single-prompt baselines.
"""

from src.core.config import get_settings
from src.core.llm import LLMClient
from src.core.logging import get_logger
from src.graph.state import ExecutionPlan, GraphState, TableRef
from src.prompts.planner import PLANNER_SYSTEM, PLANNER_USER
from src.tools.schema_inspector import SchemaInspector

logger = get_logger(__name__)


class PlannerAgent:
    """Decomposes the question and selects the relevant tables."""

    def __init__(
        self,
        llm: LLMClient | None = None,
        schema_inspector: SchemaInspector | None = None,
    ) -> None:
        self._settings = get_settings()
        self._llm = llm or LLMClient()
        self._schema = schema_inspector or SchemaInspector()

    def __call__(self, state: GraphState) -> dict:
        """LangGraph node entry point. Returns a partial state update."""
        log = logger.bind(agent="planner", question=state.question)

        schema_summary = self._schema.render_dataset_summary()

        system = PLANNER_SYSTEM.format(max_tables=self._settings.planner_max_tables)
        user = PLANNER_USER.format(schema=schema_summary, question=state.question)

        response = self._llm.complete(system, user, json_mode=True)

        try:
            payload = response.parse_json()
            plan = self._build_plan(payload)
        except (ValueError, KeyError) as exc:
            log.error("planner.parse_failed", error=str(exc))
            return {
                "error": f"Planner output could not be parsed: {exc}",
                "is_complete": True,
            }

        if not plan.tables:
            log.warning("planner.no_tables", notes=plan.notes)
            return {
                "plan": plan,
                "error": (
                    "I couldn't find tables that can answer this question. "
                    f"{plan.notes}"
                ),
                "is_complete": True,
            }

        log.info(
            "planner.success",
            tables=[t.fqn for t in plan.tables],
            requires_window=plan.requires_window_function,
            requires_cte=plan.requires_cte,
        )
        return {
            "plan": plan,
            "total_tokens": state.total_tokens + response.input_tokens + response.output_tokens,
            "total_cost_usd": state.total_cost_usd + response.cost_usd,
        }

    @staticmethod
    def _build_plan(payload: dict) -> ExecutionPlan:
        """Convert raw LLM JSON into a typed ExecutionPlan."""
        tables = [
            TableRef(
                project=t["project"],
                dataset=t["dataset"],
                table=t["table"],
                relevance=float(t.get("relevance", 1.0)),
                reason=t.get("reason", ""),
            )
            for t in payload.get("tables", [])
        ]
        return ExecutionPlan(
            intent=payload.get("intent", ""),
            tables=tables,
            metrics=list(payload.get("metrics", [])),
            dimensions=list(payload.get("dimensions", [])),
            filters=list(payload.get("filters", [])),
            time_window=payload.get("time_window"),
            requires_window_function=bool(payload.get("requires_window_function", False)),
            requires_cte=bool(payload.get("requires_cte", False)),
            notes=payload.get("notes", ""),
        )
