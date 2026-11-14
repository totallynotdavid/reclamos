"""The `reclamos answers` commands."""

import json
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from reclamos_api.answers.answer import Answer, AnswerSettings, answer_question
from reclamos_api.answers.judge import Verdict, judge_answer
from reclamos_api.answers.llm import LanguageModel, model_from_settings
from reclamos_api.answers.spend import Budget, Ledger, SpendRefused
from reclamos_api.config import Settings
from reclamos_api.db import database_pool
from reclamos_api.evaluation.run import git_state
from reclamos_api.retrieval.embedder import Embedder
from reclamos_eval.questions import Question, load_questions

DEFAULT_OUTPUT = Path(".data/answers-eval.json")

# Thresholds the report shows the refusal trade-off at.
SWEEP = tuple(round(0.70 + 0.01 * step, 2) for step in range(21))


def require_model(settings: Settings, model: LanguageModel | None) -> LanguageModel:
    model = model or model_from_settings(settings)
    if model is None:
        raise SystemExit("set ANTHROPIC_API_KEY to call the answer model")
    return model


async def ask(
    settings: Settings,
    embedder: Embedder,
    model: LanguageModel,
    question: str,
) -> str:
    async with database_pool(
        settings.database_url, queue_schema=settings.queue_schema
    ) as pool:
        ledger = Ledger(pool, daily_cap_usd=settings.daily_usd_cap)
        try:
            result = await answer_question(
                pool, embedder, ledger, model, _answer_settings(settings), question
            )
        except SpendRefused as refused:
            raise SystemExit(f"not asked: {refused}") from None
        spent = await ledger.spent_today()
    return f"{format_answer(result)}\n\nUS${result.usd:.4f}; today US${spent:.4f}"


def format_answer(result: Answer) -> str:
    similarity = (
        "none" if result.best_similarity is None else f"{result.best_similarity:.3f}"
    )
    if result.outcome == "refused":
        return f"refused: {result.reason} (best similarity {similarity})"
    lines = [result.text, ""]
    for citation in result.citations:
        fields = result.passages[citation.passage - 1].fields
        lines.append(
            f"[{citation.passage}] {fields['number'] or fields['expediente']}"
            f" {fields['page_url']}\n    «{citation.quote}»"
        )
    lines.append(f"\nbest similarity {similarity}")
    return "\n".join(lines)


@dataclass(slots=True)
class Scored:
    question: Question
    answer: Answer
    seconds: float
    verdict: Verdict | None


async def evaluate(
    settings: Settings,
    embedder: Embedder,
    model: LanguageModel,
    *,
    questions_path: Path,
    output: Path,
    budget_usd: float,
    limit: int | None,
    min_similarity: float | None,
    judge: bool,
) -> str:
    """Answer the question set, judge the answers, write the results as JSON to
    `output` and return a summary. Stops before the run would pass `budget_usd`."""
    questions = load_questions(questions_path)[:limit]
    answer_settings = _answer_settings(settings, min_similarity)
    budget = Budget(budget_usd)
    scored: list[Scored] = []
    stopped: str | None = None
    async with database_pool(
        settings.database_url, queue_schema=settings.queue_schema
    ) as pool:
        ledger = Ledger(pool, daily_cap_usd=settings.daily_usd_cap)
        # The first embedding loads the model. Keep that out of the latency.
        await embedder.embed_query(questions[0].query)
        for question in questions:
            try:
                started = perf_counter()
                result = await answer_question(
                    pool,
                    embedder,
                    ledger,
                    model,
                    answer_settings,
                    question.query,
                    budget=budget,
                )
                seconds = perf_counter() - started
                verdict = (
                    await judge_answer(
                        ledger, model, settings.judge_model, result, budget=budget
                    )
                    if judge and result.outcome == "answered"
                    else None
                )
            except SpendRefused as refused:
                stopped = f"stopped before {question.id}: {refused}"
                break
            scored.append(Scored(question, result, seconds, verdict))
    summary = summarize(scored, len(questions), budget)
    report = {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": git_state(),
        "answer_model": answer_settings.model,
        "judge_model": settings.judge_model if judge else None,
        "min_similarity": answer_settings.min_similarity,
        "stopped": stopped,
        "summary": summary,
        "questions": [_record(item) for item in scored],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    text = format_summary(summary)
    if stopped:
        text += f"\n\n{stopped}"
    return f"{text}\n\nresults written to {output}"


def _answer_settings(
    settings: Settings, min_similarity: float | None = None
) -> AnswerSettings:
    return AnswerSettings(
        model=settings.answer_model,
        min_similarity=(
            settings.refusal_min_similarity
            if min_similarity is None
            else min_similarity
        ),
    )


def cites_relevant(item: Scored) -> bool:
    relevant = set(item.question.relevant)
    return any(c.resolution_id in relevant for c in item.answer.citations)


def summarize(scored: list[Scored], asked: int, budget: Budget) -> dict[str, Any]:
    answerable = [s for s in scored if s.question.answerable]
    unanswerable = [s for s in scored if not s.question.answerable]
    answered = [s for s in answerable if s.answer.outcome == "answered"]
    judged = [s for s in scored if s.verdict and s.verdict.faithful is not None]
    seconds = sorted(s.seconds for s in scored)
    return {
        "questions": asked,
        "run": len(scored),
        "answerable": len(answerable),
        "answer_rate": _share(len(answered), len(answerable)),
        "cites_relevant": _share(sum(map(cites_relevant, answered)), len(answered)),
        "faithful": _share(
            sum(1 for s in judged if s.verdict and s.verdict.faithful), len(judged)
        ),
        "judged": len(judged),
        "unanswerable": len(unanswerable),
        "refused_unanswerable": _share(
            sum(1 for s in unanswerable if s.answer.outcome == "refused"),
            len(unanswerable),
        ),
        "reasons": dict(
            Counter(s.answer.reason for s in scored if s.answer.reason is not None)
        ),
        "rejected_citations": sum(len(s.answer.rejected) for s in scored),
        "usd": round(budget.spent_usd, 6),
        "seconds_p50": statistics.median(seconds) if seconds else None,
        "seconds_p95": _p95(seconds),
        "sweep": sweep(scored),
    }


def sweep(scored: list[Scored]) -> list[dict[str, float | None]]:
    """For each threshold, the share of answerable questions it would let
    through and the share of unanswerable ones it would refuse. Only thresholds
    at or above the run's own are meaningful."""
    rows: list[dict[str, float | None]] = []
    for threshold in SWEEP:

        def passes(item: Scored, threshold: float = threshold) -> bool:
            return (item.answer.best_similarity or 0.0) >= threshold

        answerable = [s for s in scored if s.question.answerable]
        unanswerable = [s for s in scored if not s.question.answerable]
        rows.append(
            {
                "threshold": threshold,
                "answerable_kept": _share(
                    sum(map(passes, answerable)), len(answerable)
                ),
                "unanswerable_refused": _share(
                    sum(1 for s in unanswerable if not passes(s)), len(unanswerable)
                ),
            }
        )
    return rows


def format_summary(summary: dict[str, Any]) -> str:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    def secs(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}s"

    return "\n".join(
        [
            f"questions run          {summary['run']} of {summary['questions']}",
            f"answered (answerable)  {pct(summary['answer_rate'])}"
            f" of {summary['answerable']}",
            f"cites a relevant one   {pct(summary['cites_relevant'])}",
            f"faithful               {pct(summary['faithful'])}"
            f" of {summary['judged']} judged",
            f"refused (unanswerable) {pct(summary['refused_unanswerable'])}"
            f" of {summary['unanswerable']}",
            f"refusal reasons        {summary['reasons']}",
            f"rejected citations     {summary['rejected_citations']}",
            f"latency p50 / p95      {secs(summary['seconds_p50'])}"
            f" / {secs(summary['seconds_p95'])}",
            f"spent                  US${summary['usd']:.4f}",
        ]
    )


def _record(item: Scored) -> dict[str, Any]:
    result = item.answer
    return {
        "id": item.question.id,
        "split": item.question.split,
        "answerable": item.question.answerable,
        "outcome": result.outcome,
        "reason": result.reason,
        "best_similarity": result.best_similarity,
        "answer": result.text,
        "cited": [c.resolution_id for c in result.citations],
        "relevant": list(item.question.relevant),
        "cites_relevant": cites_relevant(item),
        "rejected": result.rejected,
        "faithful": item.verdict.faithful if item.verdict else None,
        "unsupported": list(item.verdict.unsupported) if item.verdict else [],
        "usd": round(result.usd + (item.verdict.usd if item.verdict else 0.0), 6),
        "seconds": round(item.seconds, 3),
    }


def _share(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _p95(ordered: list[float]) -> float | None:
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
