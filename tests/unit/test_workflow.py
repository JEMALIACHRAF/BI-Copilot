"""End-to-end graph test with mocked agents — verifies the routing logic."""

from unittest.mock import MagicMock

from src.graph.state import (
    ExecutionPlan,
    GraphState,
    Narrative,
    QueryResult,
    TableRef,
    VizSpec,
)
from src.graph.workflow import build_workflow


def _make_planner_response():
    plan = ExecutionPlan(
        intent="test",
        tables=[TableRef(project="p", dataset="d", table="t", relevance=1.0, reason="")],
    )
    return {"plan": plan}


def _make_planner_no_tables():
    plan = ExecutionPlan(intent="test", tables=[], notes="No matching schema.")
    return {"plan": plan, "error": "no tables", "is_complete": True}


def _make_sql_response():
    return {
        "final_sql": "SELECT 1",
        "query_result": QueryResult(
            columns=["x"], rows=[{"x": 1}], row_count=1, bytes_processed=10
        ),
        "sql_attempts": [],
    }


def _make_viz_response():
    return {"viz": VizSpec(chart_type="kpi", spec={}, rationale="single value")}


def _make_narrator_response():
    return {
        "narrative": Narrative(
            headline="ok",
            summary="ok",
            key_insights=["one"],
            follow_up_questions=["next?"],
        ),
        "is_complete": True,
    }


class TestWorkflowRouting:
    def test_happy_path_runs_all_four_agents(self):
        planner = MagicMock(side_effect=lambda s: _make_planner_response())
        sql_agent = MagicMock(side_effect=lambda s: _make_sql_response())
        viz_agent = MagicMock(side_effect=lambda s: _make_viz_response())
        narrator = MagicMock(side_effect=lambda s: _make_narrator_response())

        graph = build_workflow(planner, sql_agent, viz_agent, narrator)
        result = graph.invoke(GraphState(question="test?"))

        assert planner.called
        assert sql_agent.called
        assert viz_agent.called
        assert narrator.called
        assert result["narrative"] is not None

    def test_short_circuits_when_planner_finds_no_tables(self):
        planner = MagicMock(side_effect=lambda s: _make_planner_no_tables())
        sql_agent = MagicMock()
        viz_agent = MagicMock()
        narrator = MagicMock()

        graph = build_workflow(planner, sql_agent, viz_agent, narrator)
        graph.invoke(GraphState(question="test?"))

        assert planner.called
        sql_agent.assert_not_called()
        viz_agent.assert_not_called()
        narrator.assert_not_called()
