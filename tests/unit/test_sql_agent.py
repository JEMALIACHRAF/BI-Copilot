"""Unit tests for the SQL agent — covers the ReAct retry loop without GCP."""

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from src.agents.sql_agent import SqlAgent
from src.core.llm import LLMResponse
from src.graph.state import ExecutionPlan, GraphState, TableRef
from src.tools.bigquery_tool import DryRunInfo
from src.tools.schema_inspector import ColumnInfo, TableInfo


@dataclass
class _FakeLLM:
    """Yields a scripted sequence of responses, one per `complete()` call."""

    responses: list[str]
    calls: int = 0

    def complete(self, system, user, *, json_mode=False, temperature=None):  # noqa: ARG002
        text = self.responses[self.calls]
        self.calls += 1
        return LLMResponse(content=text, input_tokens=100, output_tokens=50, cost_usd=0.001)


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        intent="Count users",
        tables=[TableRef(project="p", dataset="d", table="users", relevance=1.0, reason="")],
        metrics=["count"],
        dimensions=[],
        filters=[],
    )


def _state(plan: ExecutionPlan | None = None) -> GraphState:
    return GraphState(question="how many users?", plan=plan or _plan())


def _table_info() -> TableInfo:
    return TableInfo(
        project="p",
        dataset="d",
        table="users",
        description="users table",
        row_count=1000,
        columns=(ColumnInfo("id", "INT64", None, False, False),),
    )


def _good_dry_run() -> DryRunInfo:
    return DryRunInfo(valid=True, error=None, bytes_processed=1000, referenced_tables=("p.d.users",))


def _bad_dry_run(msg: str = "Unknown column") -> DryRunInfo:
    return DryRunInfo(valid=False, error=msg, bytes_processed=0, referenced_tables=())


class TestSqlAgentSuccessPaths:
    def test_succeeds_on_first_attempt(self):
        llm = _FakeLLM(["SELECT COUNT(*) AS n FROM `p.d.users` LIMIT 1"])
        bq = MagicMock()
        bq.dry_run.return_value = _good_dry_run()
        bq.execute.return_value = MagicMock(
            columns=["n"], rows=[{"n": 42}], row_count=1, bytes_processed=1000, cached=False, is_empty=False
        )
        schema = MagicMock()
        schema.list_tables.return_value = [_table_info()]

        agent = SqlAgent(llm=llm, bq=bq, schema_inspector=schema)
        out = agent(_state())

        assert out["final_sql"]
        assert len(out["sql_attempts"]) == 1
        assert out["sql_attempts"][0].error is None
        bq.execute.assert_called_once()


class TestSqlAgentRetries:
    def test_recovers_on_dry_run_error(self):
        llm = _FakeLLM(
            [
                "SELECT bad_col FROM `p.d.users`",   # first attempt — dry-run fails
                "SELECT COUNT(*) FROM `p.d.users`",  # retry — succeeds
            ]
        )
        bq = MagicMock()
        bq.dry_run.side_effect = [_bad_dry_run("Unknown column bad_col"), _good_dry_run()]
        bq.execute.return_value = MagicMock(
            columns=["f0_"], rows=[{"f0_": 1}], row_count=1, bytes_processed=100, cached=False, is_empty=False
        )
        schema = MagicMock()
        schema.list_tables.return_value = [_table_info()]

        agent = SqlAgent(llm=llm, bq=bq, schema_inspector=schema)
        out = agent(_state())

        assert len(out["sql_attempts"]) == 2
        assert out["sql_attempts"][0].error is not None
        assert out["sql_attempts"][1].error is None

    def test_gives_up_after_max_iterations(self, monkeypatch):
        from src.core import config as config_module

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "sql_max_iterations", 2)

        llm = _FakeLLM(["SELECT bad FROM x", "SELECT also_bad FROM x"])
        bq = MagicMock()
        bq.dry_run.return_value = _bad_dry_run("nope")
        schema = MagicMock()
        schema.list_tables.return_value = [_table_info()]

        agent = SqlAgent(llm=llm, bq=bq, schema_inspector=schema)
        out = agent(_state())

        assert len(out["sql_attempts"]) == 2
        assert "could not produce a valid query" in out["error"]
        bq.execute.assert_not_called()


class TestSqlAgentInputValidation:
    def test_missing_plan_short_circuits(self):
        agent = SqlAgent(llm=_FakeLLM([]), bq=MagicMock(), schema_inspector=MagicMock())
        out = agent(GraphState(question="?"))
        assert out["error"]
        assert out["is_complete"]
