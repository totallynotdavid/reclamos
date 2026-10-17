import json
from pathlib import Path

from reclamos_eval.gate import Baseline, check, read_baseline, write_baseline
from reclamos_eval.questions import Evidence, Question
from reclamos_eval.report import Attempt, Result, format_table, percentile, summarize


def question(name: str, split: str, *relevant: int) -> Question:
    return Question(
        id=name,
        query=name,
        split=split,
        answerable=bool(relevant),
        relevant=relevant,
        evidence=tuple(Evidence(i, "cita") for i in relevant),
        note=None if relevant else "fuera del corpus",
    )


def attempt(*ranked: int, seconds: float = 0.01) -> Attempt:
    return Attempt(ranked, seconds, None)


RESULTS = [
    Result(
        question("a", "tune", 1),
        "billing",
        {"vector": attempt(1), "text": attempt(9), "hybrid": attempt(1)},
    ),
    Result(
        question("b", "held_out", 2),
        "porting",
        {"vector": attempt(9, 2), "text": attempt(9), "hybrid": attempt(9, 2)},
    ),
    Result(
        question("c", "tune"),
        None,
        {"vector": attempt(1), "text": attempt(), "hybrid": attempt(1)},
    ),
]


def test_summary_scores_each_group_and_leaves_out_unanswerable_questions() -> None:
    summary = summarize(RESULTS)
    assert summary["questions"]["answerable"] == 2
    assert summary["questions"]["unanswerable"] == 1
    hybrid = summary["modes"]["hybrid"]
    assert hybrid["all"]["queries"] == 2
    assert hybrid["all"]["mrr"] == 0.75
    assert hybrid["splits"]["tune"]["recall_at_5"] == 1.0
    assert hybrid["splits"]["held_out"]["mrr"] == 0.5
    assert hybrid["claim_types"]["porting"]["queries"] == 1
    assert summary["modes"]["text"]["all"]["recall_at_5"] == 0.0


def test_details_give_the_rank_of_the_first_right_resolution() -> None:
    by_id = {d["id"]: d for d in summarize(RESULTS)["details"]}
    assert by_id["b"]["modes"]["vector"]["rank"] == 2
    assert by_id["b"]["modes"]["text"]["rank"] is None
    assert "rank" not in by_id["c"]["modes"]["vector"]
    json.dumps(summarize(RESULTS))


def test_table_has_a_row_per_mode_and_a_latency_block() -> None:
    lines = format_table(summarize(RESULTS)).splitlines()
    assert lines[0].startswith("3 questions: 2 answerable (")
    assert any(
        line.startswith("split held_out (1)")
        and line.split()[-5:] == ["vector", "1.000", "1.000", "0.500", "0.631"]
        for line in lines
    )
    assert any(line.startswith("search latency ms") for line in lines)


def test_percentile_is_the_nearest_rank() -> None:
    values = [float(n) for n in range(1, 101)]
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile([4.0], 95) == 4.0


def test_gate_passes_a_fall_of_exactly_the_tolerance_and_fails_more() -> None:
    baseline = Baseline(0.80, 50)
    assert check(Baseline(0.78, 50), baseline).passed
    verdict = check(Baseline(0.779, 50), baseline)
    assert not verdict.passed
    assert "down 2.1 points" in verdict.message
    assert check(Baseline(0.95, 50), baseline).passed


def test_baseline_round_trips_through_its_file(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    write_baseline(Baseline(0.87654, 64), path)
    assert read_baseline(path) == Baseline(0.8765, 64)
