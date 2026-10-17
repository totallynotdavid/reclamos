import json
from pathlib import Path
from typing import Any

import pytest
from reclamos_eval.questions import HELD_OUT, QUESTIONS, load_questions, locate


def write(path: Path, *records: dict[str, Any]) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def answerable(**changes: Any) -> dict[str, Any]:
    return {
        "id": "q1",
        "query": "me cobraron de más",
        "split": "tune",
        "answerable": True,
        "relevant": [7, 3],
        "evidence": [
            {"resolution_id": 7, "quote": "cobro indebido"},
            {"resolution_id": 3, "quote": "cobro en exceso"},
        ],
    } | changes


def test_a_question_keeps_the_order_of_its_relevant_resolutions(tmp_path: Path) -> None:
    [question] = load_questions(write(tmp_path / "q.jsonl", answerable()))
    assert question.relevant == (7, 3)
    assert question.answerable


def test_an_unanswerable_question_needs_a_note_and_no_resolutions(
    tmp_path: Path,
) -> None:
    refusal = answerable(answerable=False, relevant=[], evidence=[])
    with pytest.raises(ValueError, match="needs a note"):
        load_questions(write(tmp_path / "q.jsonl", refusal))
    [question] = load_questions(
        write(
            tmp_path / "q.jsonl", refusal | {"note": "ninguna resolución trata precios"}
        )
    )
    assert not question.answerable
    with pytest.raises(ValueError, match="names resolutions"):
        load_questions(
            write(tmp_path / "q.jsonl", refusal | {"note": "x", "relevant": [7]})
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"split": "test"}, "split"),
        ({"evidence": []}, "one quote for each"),
        ({"relevant": [7, 7], "evidence": answerable()["evidence"][:1]}, "one quote"),
        ({"answerable": "yes"}, "true or false"),
        ({"query": "  "}, "empty query"),
    ],
)
def test_a_broken_question_names_its_line(
    tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    path = write(tmp_path / "q.jsonl", answerable(), answerable(id="q2") | changes)
    with pytest.raises(ValueError, match=rf"q\.jsonl:2: .*{message}"):
        load_questions(path)


def test_ids_are_unique(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate id q1"):
        load_questions(write(tmp_path / "q.jsonl", answerable(), answerable()))


def test_quotes_are_found_across_line_breaks() -> None:
    body = "Se declara\nfundado   el reclamo.\nOtro párrafo."
    assert locate(body, "declara fundado el reclamo") == (3, 31)
    assert locate(body, "infundado") is None


def test_the_committed_questions_hold_a_fifth_back_and_include_refusals() -> None:
    questions = load_questions(QUESTIONS)
    held_out = [q for q in questions if q.split == HELD_OUT]
    assert len(questions) >= 100
    assert 0.15 <= len(held_out) / len(questions) <= 0.25
    assert sum(not q.answerable for q in questions) >= 10
