"""Narrator agent — exec summary + suggested follow-ups."""

import json

from src.core.llm import LLMClient
from src.core.logging import get_logger
from src.graph.state import GraphState, Narrative
from src.prompts.narrator import NARRATOR_SYSTEM, NARRATOR_USER

logger = get_logger(__name__)

_MAX_RESULT_ROWS_FOR_PROMPT = 50


class NarratorAgent:
    """Produces a C-level executive summary of the query result."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm or LLMClient()

    def __call__(self, state: GraphState) -> dict:
        log = logger.bind(agent="narrator")

        if state.query_result is None:
            return {
                "narrative": Narrative(
                    headline="No result available.",
                    summary="The query did not return data.",
                    key_insights=[],
                    follow_up_questions=[],
                ),
                "is_complete": True,
            }

        result = state.query_result
        if result.is_empty:
            return {
                "narrative": Narrative(
                    headline="No matching rows.",
                    summary=(
                        "The query ran successfully but returned zero rows. "
                        "Consider broadening the filters or time window."
                    ),
                    key_insights=[],
                    follow_up_questions=[
                        "Could you widen the date range?",
                        "Are there equivalent records under a different category?",
                    ],
                ),
                "is_complete": True,
            }

        # Prompt cost grows linearly in row count — cap the sample sent to the LLM.
        rows_for_prompt = result.rows[:_MAX_RESULT_ROWS_FOR_PROMPT]
        result_table = json.dumps(rows_for_prompt, indent=2, default=str)
        truncation_note = ""
        if result.row_count > _MAX_RESULT_ROWS_FOR_PROMPT:
            truncation_note = (
                f"\n\n(Showing first {_MAX_RESULT_ROWS_FOR_PROMPT} of {result.row_count} rows.)"
            )

        chart_type = state.viz.chart_type if state.viz else "table"
        user = NARRATOR_USER.format(
            question=state.question,
            row_count=result.row_count,
            result_table=result_table + truncation_note,
            chart_type=chart_type,
        )

        response = self._llm.complete(NARRATOR_SYSTEM, user, json_mode=True)

        try:
            payload = response.parse_json()
            narrative = Narrative(
                headline=payload.get("headline", ""),
                summary=payload.get("summary", ""),
                key_insights=list(payload.get("key_insights", [])),
                follow_up_questions=list(payload.get("follow_up_questions", [])),
            )
        except (ValueError, KeyError) as exc:
            log.warning("narrator.parse_failed", error=str(exc))
            narrative = Narrative(
                headline="Result returned successfully.",
                summary=f"Query returned {result.row_count} rows.",
                key_insights=[],
                follow_up_questions=[],
            )

        log.info("narrator.success", insights=len(narrative.key_insights))

        return {
            "narrative": narrative,
            "is_complete": True,
            "total_tokens": state.total_tokens + response.input_tokens + response.output_tokens,
            "total_cost_usd": state.total_cost_usd + response.cost_usd,
        }
