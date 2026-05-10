"""LLM-as-a-judge evaluator for benchmark results.

Strict row-set equality is brittle: the agent's SQL can be semantically
equivalent to the ground truth but return rows that differ in precision,
filter interpretation, or aggregation level. This module asks an LLM to
score the agent's answer along three axes (correctness, equivalence,
severity) using a charitable, defensible-interpretation framework.

The judge is itself an LLM call (GPT-4o), so it costs ~$0.01 per question.
We run the cheap strict comparison first; the judge only runs when strict
comparison fails, so cost is bounded by the failure rate.
"""

import json
from dataclasses import dataclass

from src.core.llm import LLMClient
from src.core.logging import get_logger

logger = get_logger(__name__)


_JUDGE_SYSTEM = """You are evaluating whether a SQL agent's output correctly answers a business question.

You will be given:
  1. The original natural-language question.
  2. The agent's SQL and its result rows.
  3. A reference ("ground truth") SQL and its result rows.

The agent's output may differ from the ground truth in form yet still answer the question correctly. Your job is to be a CHARITABLE, FAIR judge that recognizes when two valid interpretations of an ambiguous question produce different but equally correct answers.

Rules for judging:

  1. ACCEPT defensible alternative interpretations.
     If the question is ambiguous (e.g. "per user" can mean per-user series OR a single aggregate, or "last 90 days" can mean 88-94 days), the agent's choice is acceptable as long as it is a reasonable reading of the question. Don't penalize the agent for choosing a different valid interpretation than the ground truth.

  2. ACCEPT equivalent column choices.
     If the question asks for "the top product" and the agent returns `product_id` while the ground truth returns `product_name`, that's equivalent — both identify the product. ACCEPT.
     Same for: country code vs country name, user_id vs email, month-as-date vs month-as-string.

  3. ACCEPT minor numeric differences.
     - Within 5% relative error or 1 unit absolute (whichever is larger) → ACCEPT.
     - Different rounding precision → ACCEPT.
     - Floats vs decimals → ACCEPT.

  4. ACCEPT minor date/window boundary differences.
     "Last 90 days" returning 88, 91, or 94 rows → ACCEPT (off-by-one on date math is not a bug).
     `< 25` vs `<= 25` for age boundaries → ACCEPT (the question rarely specifies the boundary precisely).
     Different month formats (`'2024-01'` string vs `DATE '2024-01-01'`) → ACCEPT.

  5. ACCEPT different aggregation granularity if the question is unclear.
     If the question says "per user" but doesn't specify whether to return per-user rows or a single aggregate → ACCEPT either.

  6. REJECT (mark MAJOR) only when:
     - Wrong table, wrong entity, or critical column missing.
     - Filter logic fundamentally wrong (asking about completed orders, agent ignores order status entirely).
     - Numeric values off by 10x or more.
     - Agent result is empty/NULL when ground truth has substantive data.
     - Agent returns a single number when the question clearly asks for a series, or vice versa AND the question is unambiguous about it.

  7. MARK MINOR when:
     - The agent answers the question correctly but with cosmetic differences (column ordering, naming, formatting).
     - Numeric values within 5% of ground truth.

Severity classification:
  • "none"  → strict and judge agree, perfect match.
  • "minor" → defensibly correct alternative; the answer is useful to the user.
  • "major" → the agent's answer would mislead a non-technical user reading it.

Return ONLY a JSON object, no prose:
{
  "is_correct":     <bool>,            // true iff severity is "none" or "minor"
  "score":          <float, 0.0..1.0>, // 1.0=perfect, 0.7=minor, 0.0=major
  "severity":       "<none|minor|major>",
  "reasoning":      "<one sentence explaining the verdict>"
}"""

_JUDGE_USER_TEMPLATE = """Question: {question}

Agent SQL:
```sql
{agent_sql}
```

Agent result ({agent_n} rows, first 10):
{agent_rows}

Ground-truth SQL:
```sql
{gt_sql}
```

Ground-truth result ({gt_n} rows, first 10):
{gt_rows}

Apply the rules. Be charitable to defensible alternative interpretations. Return the JSON verdict."""


@dataclass(frozen=True)
class JudgeVerdict:
    is_correct: bool
    score: float
    severity: str   # "none" | "minor" | "major"
    reasoning: str
    cost_usd: float


class LLMJudge:
    """LLM-based semantic equivalence judge for benchmark answers."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm or LLMClient()

    def evaluate(
        self,
        question: str,
        agent_sql: str | None,
        agent_rows: list[dict],
        gt_sql: str,
        gt_rows: list[dict],
    ) -> JudgeVerdict:
        """Score the agent's answer against the ground truth."""
        if agent_sql is None or not agent_rows:
            return JudgeVerdict(
                is_correct=False,
                score=0.0,
                severity="major",
                reasoning="Agent produced no SQL or no result.",
                cost_usd=0.0,
            )

        user = _JUDGE_USER_TEMPLATE.format(
            question=question,
            agent_sql=agent_sql,
            agent_n=len(agent_rows),
            agent_rows=json.dumps(agent_rows[:10], indent=2, default=str),
            gt_sql=gt_sql,
            gt_n=len(gt_rows),
            gt_rows=json.dumps(gt_rows[:10], indent=2, default=str),
        )

        response = self._llm.complete(_JUDGE_SYSTEM, user, json_mode=True, temperature=0.0)

        try:
            payload = response.parse_json()
            return JudgeVerdict(
                is_correct=bool(payload.get("is_correct", False)),
                score=float(payload.get("score", 0.0)),
                severity=str(payload.get("severity", "major")),
                reasoning=str(payload.get("reasoning", "")),
                cost_usd=response.cost_usd,
            )
        except (ValueError, KeyError) as exc:
            logger.warning("judge.parse_failed", error=str(exc), raw=response.content[:200])
            return JudgeVerdict(
                is_correct=False,
                score=0.0,
                severity="major",
                reasoning=f"Judge response could not be parsed: {exc}",
                cost_usd=response.cost_usd,
            )