"""The judged question set."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TUNE = "tune"
HELD_OUT = "held_out"
SPLITS = (TUNE, HELD_OUT)

DATA = Path(__file__).resolve().parents[2] / "data"
QUESTIONS = DATA / "queries.jsonl"


class QuestionFileError(ValueError):
    """A line of the question file is not a valid question."""


@dataclass(frozen=True, slots=True)
class Evidence:
    resolution_id: int
    quote: str


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    query: str
    split: str
    answerable: bool
    relevant: tuple[int, ...]  # the first one sets the question's claim type
    evidence: tuple[Evidence, ...]
    note: str | None = None


def load_questions(path: Path = QUESTIONS) -> list[Question]:
    """Read and check a question file. A broken line raises `QuestionFileError` naming it."""
    questions: list[Question] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            question = _parse(json.loads(line))
        except (KeyError, TypeError, ValueError) as error:
            raise QuestionFileError(f"{path}:{number}: {error}") from error
        if question.id in seen:
            raise QuestionFileError(f"{path}:{number}: duplicate id {question.id}")
        seen.add(question.id)
        questions.append(question)
    return questions


def _parse(record: dict[str, Any]) -> Question:
    question = Question(
        id=str(record["id"]),
        query=str(record["query"]).strip(),
        split=str(record["split"]),
        answerable=_boolean(record["answerable"]),
        relevant=tuple(int(i) for i in record["relevant"]),
        evidence=tuple(
            Evidence(int(item["resolution_id"]), str(item["quote"]).strip())
            for item in record["evidence"]
        ),
        note=str(record["note"]) if record.get("note") else None,
    )
    if not question.query:
        raise ValueError(f"{question.id} has an empty query")
    if question.split not in SPLITS:
        raise ValueError(f"{question.id} has split {question.split!r}, not {SPLITS}")
    if question.answerable:
        if not question.relevant:
            raise ValueError(f"{question.id} is answerable but has no relevant")
        quoted = {item.resolution_id for item in question.evidence}
        if quoted != set(question.relevant) or len(set(question.relevant)) != len(
            question.relevant
        ):
            raise ValueError(
                f"{question.id} needs one quote for each relevant resolution; "
                f"relevant {sorted(question.relevant)}, quoted {sorted(quoted)}"
            )
        if not all(item.quote for item in question.evidence):
            raise ValueError(f"{question.id} has an empty quote")
    elif question.relevant or question.evidence:
        raise ValueError(f"{question.id} is unanswerable but names resolutions")
    elif not question.note:
        raise ValueError(f"{question.id} is unanswerable and needs a note")
    return question


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"answerable must be true or false, not {value!r}")
    return value


def locate(body: str, quote: str) -> tuple[int, int] | None:
    """Where `quote` sits in `body` as (start, end) offsets, comparing runs of
    whitespace as one, or None when it does not occur."""
    words = quote.split()
    if not words:
        return None
    found = re.search(r"\s+".join(re.escape(word) for word in words), body)
    return None if found is None else found.span()
