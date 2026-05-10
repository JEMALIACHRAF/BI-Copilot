"""LangGraph workflow definition.

The graph topology:

    START → planner → sql_agent → viz_agent → narrator → END
              │           │
              │ (no       │ (loop exhausted
              │  tables)  │  or runtime error)
              └───────────┴─────────────► END

Conditional edges short-circuit to END whenever an upstream agent sets
`state.is_complete = True` or `state.error`. This keeps the graph linear
and easy to reason about — no implicit retries, no hidden state.
"""

from langgraph.graph import END, StateGraph

from src.agents.narrator import NarratorAgent
from src.agents.planner import PlannerAgent
from src.agents.sql_agent import SqlAgent
from src.agents.viz_agent import VizAgent
from src.core.logging import get_logger
from src.graph.state import GraphState

logger = get_logger(__name__)


def build_workflow(
    planner: PlannerAgent | None = None,
    sql_agent: SqlAgent | None = None,
    viz_agent: VizAgent | None = None,
    narrator: NarratorAgent | None = None,
):
    """Assemble the four-agent graph and return a compiled runnable.

    Agents are injected for testability; defaults wire up the production stack.
    """
    planner = planner or PlannerAgent()
    sql_agent = sql_agent or SqlAgent()
    viz_agent = viz_agent or VizAgent()
    narrator = narrator or NarratorAgent()

    graph = StateGraph(GraphState)

    graph.add_node("planner", planner)
    graph.add_node("sql_agent", sql_agent)
    graph.add_node("viz_agent", viz_agent)
    graph.add_node("narrator", narrator)

    graph.set_entry_point("planner")

    graph.add_conditional_edges("planner", _after_planner, {"sql": "sql_agent", "end": END})
    graph.add_conditional_edges("sql_agent", _after_sql, {"viz": "viz_agent", "end": END})
    graph.add_edge("viz_agent", "narrator")
    graph.add_edge("narrator", END)

    return graph.compile()


def _after_planner(state: GraphState) -> str:
    """Skip downstream agents if the planner failed or found nothing."""
    if state.is_complete or state.error:
        return "end"
    return "sql"


def _after_sql(state: GraphState) -> str:
    """Skip viz/narrator if the SQL agent gave up or hit a runtime error."""
    if state.is_complete or state.error or state.query_result is None:
        return "end"
    return "viz"
