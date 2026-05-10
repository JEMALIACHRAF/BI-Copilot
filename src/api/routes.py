"""HTTP routes for the BI Copilot."""

import time

from fastapi import APIRouter, HTTPException, status

from src.api.models import AskRequest, AskResponse, HealthResponse, QueryMetadata
from src.core.config import get_settings
from src.core.logging import get_logger
from src.core.observability import trace_run
from src.graph.state import GraphState
from src.graph.workflow import build_workflow

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["bi-copilot"])

# The graph is stateless and cheap to keep around — build once at import time.
_workflow = build_workflow()


@router.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest) -> AskResponse:
    """Run the full agent pipeline against the user's question."""
    started = time.perf_counter()

    initial_state = GraphState(question=request.question, user_id=request.user_id)

    with trace_run("bi_copilot.ask", question=request.question, user_id=request.user_id):
        try:
            final = _workflow.invoke(initial_state)
        except Exception as exc:  # noqa: BLE001 — surface as 500 to the client
            logger.exception("workflow.failed", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Pipeline error: {exc}",
            ) from exc

    # `final` may come back as a dict (LangGraph default) or our model.
    state = final if isinstance(final, GraphState) else GraphState.model_validate(final)

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if state.error and state.query_result is None:
        return AskResponse(
            question=state.question,
            sql=state.final_sql,
            data=[],
            columns=[],
            viz=None,
            narrative=None,
            follow_ups=[],
            metadata=QueryMetadata(
                sql_attempts=len(state.sql_attempts),
                total_latency_ms=elapsed_ms,
                cost_usd=round(state.total_cost_usd, 4),
                rows_scanned=0,
                cached=False,
                tokens_used=state.total_tokens,
            ),
            error=state.error,
        )

    result = state.query_result
    return AskResponse(
        question=state.question,
        sql=state.final_sql,
        data=result.rows if result else [],
        columns=result.columns if result else [],
        viz=state.viz.model_dump() if state.viz else None,
        narrative=state.narrative.model_dump() if state.narrative else None,
        follow_ups=state.narrative.follow_up_questions if state.narrative else [],
        metadata=QueryMetadata(
            sql_attempts=len(state.sql_attempts),
            total_latency_ms=elapsed_ms,
            cost_usd=round(state.total_cost_usd, 4),
            rows_scanned=result.bytes_processed if result else 0,
            cached=result.cached if result else False,
            tokens_used=state.total_tokens,
        ),
        error=None,
    )


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe + dependency status."""
    settings = get_settings()
    checks = {
        "openai_configured": bool(settings.openai_api_key),
        "gcp_configured": bool(settings.gcp_project_id),
        "langfuse_enabled": settings.langfuse_enabled,
    }
    return HealthResponse(status="ok", version="0.1.0", checks=checks)
