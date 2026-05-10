"""Benchmark harness for the multi-agent pipeline.

Multi-criteria scoring (each question gets multiple verdicts):

  • execution      — did the agent produce non-empty SQL + non-empty result?
  • strict_match   — does the value-set match the ground truth byte-for-byte?
  • judge_match    — does an LLM judge consider the result semantically equivalent?

Final accuracy uses `judge_match`, which is what we care about in production.
`strict_match` is reported alongside for regression detection.

The LLM judge runs ONLY when strict_match fails, so cost is bounded by the
failure rate.

Usage:
    python scripts/run_benchmark.py --suite all --output reports/bench.json
    python scripts/run_benchmark.py --suite all --no-judge   # strict only
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

from src.core.logging import configure_logging, get_logger
from src.eval.judge import LLMJudge
from src.graph.state import GraphState
from src.graph.workflow import build_workflow
from src.tools.bigquery_tool import BigQueryTool

DATA_PATH = Path(__file__).parent.parent / "data" / "benchmark_queries.json"
NUMERIC_TOLERANCE_DECIMALS = 2


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--suite", default="all", help="Filter by difficulty: easy|medium|hard|all")
    p.add_argument("--output", default="reports/bench.json")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip LLM-as-a-judge evaluation; report strict matching only.",
    )
    return p.parse_args()


def load_questions(path: Path = DATA_PATH) -> list[dict]:
    with path.open() as f:
        return json.load(f)


# ──────────────────────────────────────────────────────────────────────
# Strict comparison
# ──────────────────────────────────────────────────────────────────────
def _coerce_value_to_string(v: object) -> str:
    """Render any value as a sortable string. Used only for dedupe / set equality."""
    if v is None:
        return "\x00NULL"  # sorts before everything else
    if isinstance(v, bool):
        return f"\x01BOOL:{v}"
    if isinstance(v, int) and not isinstance(v, bool):
        return f"\x02INT:{v:020d}"
    if isinstance(v, float):
        return f"\x03FLT:{round(v, NUMERIC_TOLERANCE_DECIMALS):030.4f}"
    return f"\x04STR:{str(v).strip()}"


def _row_signature(row: dict) -> tuple:
    """Sorted tuple of stringified values; column names ignored, types preserved by prefix."""
    return tuple(sorted(_coerce_value_to_string(v) for v in row.values()))


def _normalize(rows: list[dict]) -> list[tuple]:
    return sorted(_row_signature(r) for r in rows)


def strict_compare(agent_rows: list[dict], gt_rows: list[dict]) -> bool:
    """Set-equality of value rows. Tolerates row reordering and column renaming."""
    return _normalize(agent_rows) == _normalize(gt_rows)


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────
def run() -> None:
    args = parse_args()
    configure_logging()
    logger = get_logger("benchmark")

    questions = load_questions()
    if args.suite != "all":
        questions = [q for q in questions if q["difficulty"] == args.suite]
    if args.limit:
        questions = questions[: args.limit]

    workflow = build_workflow()
    bq = BigQueryTool()
    judge = None if args.no_judge else LLMJudge()

    by_difficulty_strict: dict[str, list[bool]] = defaultdict(list)
    by_difficulty_judge: dict[str, list[bool]] = defaultdict(list)
    per_question: list[dict] = []

    total_latency = 0.0
    total_attempts = 0
    total_judge_cost = 0.0

    for i, q in enumerate(questions, 1):
        logger.info("benchmark.run", index=i, total=len(questions), difficulty=q["difficulty"])
        start = time.perf_counter()

        agent_state = None
        agent_rows: list[dict] = []
        agent_sql: str | None = None
        gt_rows: list[dict] = []
        error: str | None = None

        try:
            final = workflow.invoke(GraphState(question=q["question"]))
            state = GraphState.model_validate(final) if isinstance(final, dict) else final
            agent_state = state
            agent_sql = state.final_sql

            if state.query_result is not None:
                agent_rows = state.query_result.rows

            if not agent_rows:
                error = state.error or "agent produced no rows"

            gt_result = bq.execute(q["ground_truth_sql"])
            gt_rows = gt_result.rows
        except Exception as exc:  # noqa: BLE001
            error = str(exc)

        elapsed = time.perf_counter() - start
        total_latency += elapsed
        attempts = len(agent_state.sql_attempts) if agent_state else 0
        total_attempts += attempts

        # ── Verdicts ─────────────────────────────────────────────────
        executed = bool(agent_rows)
        try:
            strict_pass = executed and strict_compare(agent_rows, gt_rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning("strict_compare_failed", error=str(exc))
            strict_pass = False

        judge_verdict = None
        judge_pass: bool | None = None
        if not args.no_judge and not strict_pass and executed:
            try:
                judge_verdict = judge.evaluate(
                    q["question"], agent_sql, agent_rows, q["ground_truth_sql"], gt_rows
                )
                total_judge_cost += judge_verdict.cost_usd
                judge_pass = judge_verdict.is_correct
            except Exception as exc:  # noqa: BLE001
                logger.warning("judge.failed", error=str(exc))
                judge_pass = None

        if strict_pass:
            judge_pass = True

        by_difficulty_strict[q["difficulty"]].append(strict_pass)
        if judge_pass is not None:
            by_difficulty_judge[q["difficulty"]].append(judge_pass)

        # ── Live progress ────────────────────────────────────────────
        strict_marker = "✓" if strict_pass else "✗"
        if judge_pass is True and not strict_pass:
            judge_marker = " (judge: ✓ minor)"
        elif judge_pass is False:
            judge_marker = " (judge: ✗ major)"
        else:
            judge_marker = ""
        print(
            f"  {strict_marker} {q['id']:<12} {q['difficulty']:<7} "
            f"{elapsed:>6.1f}s  attempts={attempts}{judge_marker}"
        )

        per_question.append(
            {
                "id": q["id"],
                "question": q["question"],
                "difficulty": q["difficulty"],
                "executed": executed,
                "strict_pass": strict_pass,
                "judge_pass": judge_pass,
                "judge_severity": judge_verdict.severity if judge_verdict else None,
                "judge_reasoning": judge_verdict.reasoning if judge_verdict else None,
                "duration_s": round(elapsed, 2),
                "sql_attempts": attempts,
                "agent_sql": agent_sql,
                "agent_rows_preview": agent_rows[:3],
                "ground_truth_rows_preview": gt_rows[:3],
                "error": error,
            }
        )

    # ── Aggregate ────────────────────────────────────────────────────
    total_n = sum(len(rs) for rs in by_difficulty_strict.values())
    strict_passed = sum(b for rs in by_difficulty_strict.values() for b in rs)
    judge_passed = sum(b for rs in by_difficulty_judge.values() for b in rs)

    summary = {
        "total": total_n,
        "strict_accuracy": round(strict_passed / total_n, 3) if total_n else 0,
        "judge_accuracy": round(judge_passed / total_n, 3) if total_n else 0,
        "strict_passed": strict_passed,
        "judge_passed": judge_passed,
        "avg_latency_s": round(total_latency / total_n, 2) if total_n else 0,
        "avg_sql_attempts": round(total_attempts / total_n, 2) if total_n else 0,
        "judge_cost_usd": round(total_judge_cost, 4),
        "by_difficulty": {
            d: {
                "strict_accuracy": round(sum(rs) / len(rs), 3) if rs else 0,
                "judge_accuracy": (
                    round(
                        sum(by_difficulty_judge[d]) / len(by_difficulty_judge[d]), 3
                    )
                    if by_difficulty_judge[d]
                    else 0
                ),
                "n": len(rs),
            }
            for d, rs in by_difficulty_strict.items()
        },
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump({"summary": summary, "questions": per_question}, f, indent=2, default=str)

    print("\n" + "=" * 64)
    print(f"Strict accuracy:  {summary['strict_accuracy'] * 100:>5.1f}%  ({strict_passed}/{total_n})")
    if not args.no_judge:
        print(f"Judge  accuracy:  {summary['judge_accuracy'] * 100:>5.1f}%  ({judge_passed}/{total_n})")
    print()
    for d, stats in summary["by_difficulty"].items():
        print(
            f"  {d:<8} strict {stats['strict_accuracy'] * 100:>5.1f}%   "
            f"judge {stats['judge_accuracy'] * 100:>5.1f}%   "
            f"(n={stats['n']})"
        )
    print()
    print(f"  Avg latency:      {summary['avg_latency_s']}s")
    print(f"  Avg SQL attempts: {summary['avg_sql_attempts']}")
    if not args.no_judge:
        print(f"  Judge cost:       ${summary['judge_cost_usd']}")
    print()
    print(f"Report written to {out_path}")


if __name__ == "__main__":
    run()