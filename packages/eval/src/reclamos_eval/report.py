"""Aggregate per-question rankings into the numbers the project reports."""

import math
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from reclamos_eval.questions import SPLITS, Question
from reclamos_eval.retrieval import Scores, score

MODES = ("vector", "text", "hybrid")
METRICS = ("recall_at_5", "recall_at_10", "mrr", "ndcg_at_10")
DETAIL_RANKS = 10


@dataclass(frozen=True, slots=True)
class Attempt:
    """One search of one question in one mode."""

    ranked: tuple[int, ...]  # distinct resolutions, best first
    seconds: float
    top_score: (
        float | None
    )  # cosine of the best vector hit, ts_rank of the best text hit


@dataclass(frozen=True, slots=True)
class Result:
    question: Question
    claim_type: str | None  # of the first relevant resolution
    attempts: dict[str, Attempt]

    def scores(self, mode: str) -> Scores:
        return score(self.question.relevant, self.attempts[mode].ranked)


def percentile(values: Sequence[float], percent: float) -> float:
    """The smallest value with at least `percent` of the values at or below it."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    return ordered[max(math.ceil(percent / 100 * len(ordered)), 1) - 1]


def summarize(results: Sequence[Result]) -> dict[str, Any]:
    """The metrics of every mode: overall, per split and per claim type, with the
    search latency, and the ranking of each question."""
    answerable = [r for r in results if r.question.answerable]
    claim_types = sorted({r.claim_type for r in answerable if r.claim_type})
    modes = sorted({mode for r in results for mode in r.attempts}, key=MODES.index)
    return {
        "questions": {
            "total": len(results),
            "answerable": len(answerable),
            "unanswerable": len(results) - len(answerable),
            "splits": dict(Counter(r.question.split for r in results)),
            "claim_types": dict(Counter(r.claim_type for r in answerable)),
        },
        "modes": {
            mode: {
                "all": _mean(answerable, mode),
                "splits": {
                    split: _mean(
                        _only(answerable, lambda r, s=split: r.question.split == s),
                        mode,
                    )
                    for split in SPLITS
                },
                "claim_types": {
                    claim_type: _mean(
                        _only(answerable, lambda r, c=claim_type: r.claim_type == c),
                        mode,
                    )
                    for claim_type in claim_types
                },
                "latency_ms": _latency(results, mode),
            }
            for mode in modes
        },
        "details": [_detail(r) for r in results],
    }


def _only(results: Iterable[Result], keep: Callable[[Result], bool]) -> list[Result]:
    return [r for r in results if keep(r)]


def _mean(results: Sequence[Result], mode: str) -> dict[str, Any]:
    scored = [r.scores(mode) for r in results]
    return {"queries": len(scored)} | {
        metric: round(sum(getattr(s, metric) for s in scored) / len(scored), 4)
        if scored
        else None
        for metric in METRICS
    }


def _latency(results: Sequence[Result], mode: str) -> dict[str, float]:
    seconds = [r.attempts[mode].seconds * 1000 for r in results]
    return {
        "p50": round(percentile(seconds, 50), 1),
        "p95": round(percentile(seconds, 95), 1),
    }


def _detail(result: Result) -> dict[str, Any]:
    question = result.question
    detail: dict[str, Any] = {
        "id": question.id,
        "split": question.split,
        "answerable": question.answerable,
        "claim_type": result.claim_type,
        "relevant": sorted(question.relevant),
    }
    detail["modes"] = {}
    for mode, attempt in result.attempts.items():
        entry: dict[str, Any] = {
            "ranked": list(attempt.ranked[:DETAIL_RANKS]),
            "top_score": None
            if attempt.top_score is None
            else round(attempt.top_score, 4),
        }
        if question.answerable:
            entry["rank"] = next(
                (i for i, r in enumerate(attempt.ranked, 1) if r in question.relevant),
                None,
            )
        detail["modes"][mode] = entry
    return detail


def format_table(summary: dict[str, Any]) -> str:
    """The summary as text: one block per group, one row per mode."""
    counts = summary["questions"]
    lines = [
        (
            f"{counts['total']} questions: {counts['answerable']} answerable "
            f"({', '.join(f'{n} {s}' for s, n in counts['splits'].items())}), "
            f"{counts['unanswerable']} unanswerable (not scored)"
        ),
    ]
    modes: dict[str, Any] = summary["modes"]
    first = next(iter(modes.values()))
    groups = [("all", lambda m: m["all"])]
    groups += [(f"split {s}", lambda m, s=s: m["splits"][s]) for s in first["splits"]]
    groups += [
        (f"claim {c}", lambda m, c=c: m["claim_types"][c]) for c in first["claim_types"]
    ]
    labels = [f"{name} ({pick(first)['queries']})" for name, pick in groups]
    width = max(len(label) for label in [*labels, "search latency ms"]) + 2
    lines += [
        "",
        f"{'':<{width}}{'mode':<8}{'recall@5':>9}{'recall@10':>10}{'MRR':>7}{'nDCG@10':>9}",
    ]
    for label, (_, pick) in zip(labels, groups, strict=True):
        for index, (mode, data) in enumerate(modes.items()):
            row = pick(data)
            lines.append(
                f"{label if index == 0 else '':<{width}}{mode:<8}"
                f"{_cell(row['recall_at_5'], 9)}{_cell(row['recall_at_10'], 10)}"
                f"{_cell(row['mrr'], 7)}{_cell(row['ndcg_at_10'], 9)}"
            )
    lines += ["", f"{'search latency ms':<{width}}{'mode':<8}{'p50':>9}{'p95':>10}"]
    for mode, data in modes.items():
        latency = data["latency_ms"]
        lines.append(
            f"{'':<{width}}{mode:<8}{latency['p50']:>9.1f}{latency['p95']:>10.1f}"
        )
    return "\n".join(lines)


def _cell(value: float | None, width: int) -> str:
    return f"{'-':>{width}}" if value is None else f"{value:>{width}.3f}"
