import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fakeembed import HashingEmbedder
from gobpe import FIXTURES
from reclamos_api import cli
from reclamos_api.corpus import repository
from reclamos_api.corpus.chunk import chunk_text
from reclamos_api.corpus.parse import parse_resolution
from reclamos_api.corpus.site import ListedResolution
from reclamos_api.db import database_pool
from reclamos_api.evaluation.subset import (
    PrecomputedEmbedder,
    load_subset,
    read_subset,
)
from reclamos_eval.gate import BASELINE, SUBSET, read_baseline
from reclamos_eval.questions import QUESTIONS, load_questions

pytestmark = pytest.mark.integration

QUEUE_SCHEMA = "task_queue"


@pytest.fixture
def environment(database_url: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("DATABASE_URL", database_url)
    return database_url


def run_database(url: str, work: Callable[[Any], Awaitable[Any]]) -> Any:
    async def run() -> Any:
        async with database_pool(url, queue_schema=QUEUE_SCHEMA) as pool:
            return await work(pool)

    return asyncio.run(run())


def load_fixed_index(url: str) -> None:
    async def work(pool: Any) -> None:
        async with pool.acquire() as connection:
            await load_subset(connection, read_subset(SUBSET))

    run_database(url, work)


def gate_questions_file(tmp_path: Path, *, held_out: int = 0) -> Path:
    """The questions the fixed index covers, the last `held_out` of them marked
    as held out."""
    asked = set(read_subset(SUBSET).questions)
    lines = [
        json.loads(line)
        for line in QUESTIONS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    chosen = [line for line in lines if line["id"] in asked]
    for line in chosen[len(chosen) - held_out :]:
        line["split"] = "held_out"
    path = tmp_path / "queries.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in chosen))
    return path


def eval_main(argv: list[str]) -> None:
    cli.main(["eval", *argv], embedder=PrecomputedEmbedder(read_subset(SUBSET)))


def test_eval_scores_every_mode_and_writes_the_results(
    environment: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    load_fixed_index(environment)
    output = tmp_path / "results.json"
    eval_main(
        [
            "--questions",
            str(gate_questions_file(tmp_path, held_out=5)),
            "--output",
            str(output),
        ]
    )

    table = capsys.readouterr().out
    for heading in ("recall@5", "recall@10", "MRR", "nDCG@10", "split held_out (5)"):
        assert heading in table
    assert "search latency ms" in table
    results = json.loads(output.read_text())
    subset = read_subset(SUBSET)
    assert results["model"] == subset.model
    assert results["corpus"]["resolutions"] == len(subset.resolutions)
    assert results["corpus"]["chunks"] == len(subset.chunks)
    assert results["questions"]["splits"]["held_out"] == 5
    assert set(results["modes"]) == {"vector", "text", "hybrid"}
    hybrid = results["modes"]["hybrid"]
    assert set(hybrid["splits"]) == {"tune", "held_out"}
    assert hybrid["latency_ms"]["p50"] <= hybrid["latency_ms"]["p95"]
    assert (
        sum(c["queries"] for c in hybrid["claim_types"].values())
        == hybrid["all"]["queries"]
    )
    assert {d["id"] for d in results["details"]} == set(subset.questions)


def test_eval_refuses_a_question_whose_resolution_is_missing(
    environment: str, tmp_path: Path
) -> None:
    load_fixed_index(environment)
    path = gate_questions_file(tmp_path)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    lines[0]["relevant"] = [1, *lines[0]["relevant"]]
    lines[0]["evidence"] = [{"resolution_id": 1, "quote": "x"}, *lines[0]["evidence"]]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    with pytest.raises(SystemExit, match=f"{lines[0]['id']} names resolution 1,"):
        eval_main(["--questions", str(path), "--output", str(tmp_path / "out.json")])
    assert not (tmp_path / "out.json").exists()


def test_eval_names_the_line_of_a_malformed_question(
    environment: str, tmp_path: Path
) -> None:
    load_fixed_index(environment)
    path = gate_questions_file(tmp_path)
    lines = path.read_text().splitlines()
    broken = json.loads(lines[1]) | {"split": "dev"}
    path.write_text("\n".join([lines[0], json.dumps(broken), *lines[2:]]) + "\n")

    with pytest.raises(SystemExit, match=rf"{path.name}:2: {broken['id']} has split"):
        eval_main(["--questions", str(path), "--output", str(tmp_path / "out.json")])


def test_eval_checks_each_quote_against_the_stored_text(
    environment: str, tmp_path: Path
) -> None:
    load_fixed_index(environment)
    path = gate_questions_file(tmp_path)
    question = load_questions(path)[0]
    resolution_id = question.evidence[0].resolution_id
    run_database(
        environment,
        lambda pool: pool.execute(
            "UPDATE resolutions SET body = 'otro texto' WHERE id = $1", resolution_id
        ),
    )

    with pytest.raises(SystemExit, match=f"{question.id}: the quote .* is not in"):
        eval_main(["--questions", str(path), "--output", str(tmp_path / "out.json")])

    run_database(
        environment,
        lambda pool: pool.execute(
            "UPDATE resolutions SET body = $2 WHERE id = $1",
            resolution_id,
            " ".join(item.quote for item in question.evidence),
        ),
    )
    eval_main(["--questions", str(path), "--output", str(tmp_path / "out.json")])
    assert (tmp_path / "out.json").exists()


def test_eval_refuses_to_run_before_every_chunk_has_a_vector(
    environment: str, tmp_path: Path
) -> None:
    load_fixed_index(environment)
    run_database(
        environment,
        lambda pool: pool.execute(
            "UPDATE chunks SET embedding = NULL WHERE ctid = (SELECT min(ctid) FROM chunks)"
        ),
    )
    with pytest.raises(SystemExit, match="1 chunks have no vector"):
        eval_main(
            [
                "--questions",
                str(gate_questions_file(tmp_path)),
                "--output",
                str(tmp_path / "out.json"),
            ]
        )


def gate_main(argv: list[str]) -> None:
    cli.main(["gate", *argv])


def test_gate_passes_at_the_committed_baseline_and_leaves_the_database_empty(
    environment: str, capsys: pytest.CaptureFixture[str]
) -> None:
    gate_main(["check"])

    out = capsys.readouterr().out
    assert out.splitlines()[-1].startswith("ok: fused recall@5 ")
    assert f"baseline {read_baseline().recall_at_5:.4f}" in out

    async def tables(pool: Any) -> list[str]:
        rows = await pool.fetch(
            "SELECT count(*) FROM resolutions UNION ALL SELECT count(*) FROM chunks"
        )
        return [row[0] for row in rows]

    assert run_database(environment, tables) == [0, 0]


def test_gate_fails_when_recall_falls_more_than_two_points(
    environment: str, tmp_path: Path
) -> None:
    baseline = tmp_path / "baseline.json"
    gate_main(["baseline", "--baseline", str(baseline)])
    measured = json.loads(baseline.read_text())

    baseline.write_text(
        json.dumps(measured | {"recall_at_5": measured["recall_at_5"] + 0.02})
    )
    gate_main(["check", "--baseline", str(baseline)])

    raised = measured["recall_at_5"] + 0.03
    baseline.write_text(json.dumps(measured | {"recall_at_5": raised}))
    with pytest.raises(SystemExit, match=r"FAIL: .*down 3\.0 points"):
        gate_main(["check", "--baseline", str(baseline)])
    assert json.loads(baseline.read_text())["recall_at_5"] == raised


def test_the_baseline_command_rewrites_only_the_file_it_is_given(
    environment: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    committed = BASELINE.read_text()
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"recall_at_5": 0.1, "questions": 1}')

    gate_main(["baseline", "--baseline", str(baseline)])

    assert capsys.readouterr().out.startswith(
        f"baseline {baseline} is now fused recall@5"
    )
    assert json.loads(baseline.read_text())["questions"] == len(
        read_subset(SUBSET).questions
    )
    assert BASELINE.read_text() == committed


def test_gate_asks_the_questions_the_subset_names(
    environment: str, tmp_path: Path
) -> None:
    asked = set(read_subset(SUBSET).questions)
    kept = [
        line
        for line in QUESTIONS.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["id"] not in asked
    ]
    path = tmp_path / "queries.jsonl"
    path.write_text("\n".join(kept) + "\n")

    with pytest.raises(SystemExit, match="rebuild it with `reclamos gate subset`"):
        gate_main(["check", "--questions", str(path)])


FIXTURE_IDS = (6134934, 6134935, 6134941)


async def seed_fixtures(pool: Any, embedder: HashingEmbedder) -> None:
    async with pool.acquire() as connection:
        for resolution_id in FIXTURE_IDS:
            body = (FIXTURES / f"{resolution_id}.txt").read_text()
            await repository.upsert_listed(
                connection,
                [
                    ListedResolution(
                        id=resolution_id,
                        page_url=f"https://www.gob.pe/{resolution_id}",
                        expediente="Expediente N.° 0000123-2022/TRASU/ST-RA",
                        published_on=date(2022, 12, 31),
                    )
                ],
            )
            await repository.record_extraction(
                connection, resolution_id, body=body, method="ocr", pages=1, ocr_pages=1
            )
            await repository.record_parse(
                connection, resolution_id, parse_resolution(body)
            )
            await repository.record_chunks(connection, resolution_id, chunk_text(body))
            rows = await repository.chunk_texts(connection, resolution_id)
            vectors = await embedder.embed_passages([row["text"] for row in rows])
            await repository.record_embeddings(
                connection,
                resolution_id,
                embedder.model,
                [
                    (row["id"], vector)
                    for row, vector in zip(rows, vectors, strict=True)
                ],
            )


def test_a_rebuilt_subset_loads_and_answers_the_questions_it_was_built_from(
    environment: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder = HashingEmbedder()
    run_database(environment, lambda pool: seed_fixtures(pool, embedder))
    body = (FIXTURES / f"{FIXTURE_IDS[0]}.txt").read_text()
    quote = " ".join(body.split()[200:230])
    questions = tmp_path / "queries.jsonl"
    questions.write_text(
        json.dumps(
            {
                "id": "q1",
                "query": quote,
                "split": "tune",
                "answerable": True,
                "relevant": [FIXTURE_IDS[0]],
                "evidence": [{"resolution_id": FIXTURE_IDS[0], "quote": quote}],
            }
        )
        + "\n"
    )
    subset = tmp_path / "subset.jsonl.gz"

    cli.main(
        [
            "gate",
            "subset",
            "--questions",
            str(questions),
            "--subset",
            str(subset),
            "--chunks",
            "6",
        ],
        embedder=embedder,
    )

    built = read_subset(subset)
    assert built.questions == ["q1"]
    assert len(built.chunks) == 6
    assert built.model == "hashing"
    baseline = tmp_path / "baseline.json"
    cli.main(
        [
            "gate",
            "baseline",
            "--questions",
            str(questions),
            "--subset",
            str(subset),
            "--baseline",
            str(baseline),
        ]
    )
    assert json.loads(baseline.read_text()) == {"recall_at_5": 1.0, "questions": 1}


def test_the_subset_keeps_the_chunks_that_hold_each_quote(
    environment: str, tmp_path: Path
) -> None:
    embedder = HashingEmbedder()
    run_database(environment, lambda pool: seed_fixtures(pool, embedder))
    words = (FIXTURES / f"{FIXTURE_IDS[1]}.txt").read_text().split()
    quote = " ".join(words[300:330])
    questions = tmp_path / "queries.jsonl"
    questions.write_text(
        json.dumps(
            {
                "id": "q1",
                "query": "una pregunta sin relación",
                "split": "tune",
                "answerable": True,
                "relevant": [FIXTURE_IDS[1]],
                "evidence": [{"resolution_id": FIXTURE_IDS[1], "quote": quote}],
            }
        )
        + "\n"
    )
    subset = tmp_path / "subset.jsonl.gz"

    # A budget of one chunk still keeps every chunk the quote touches.
    cli.main(
        [
            "gate",
            "subset",
            "--questions",
            str(questions),
            "--subset",
            str(subset),
            "--chunks",
            "1",
        ],
        embedder=embedder,
    )

    kept = read_subset(subset).chunks
    assert kept
    assert all(chunk["resolution_id"] == FIXTURE_IDS[1] for chunk in kept)
    assert quote.split()[0] in " ".join(chunk["text"] for chunk in kept)
