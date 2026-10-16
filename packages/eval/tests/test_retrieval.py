import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from reclamos_eval.__main__ import main
from reclamos_eval.retrieval import (
    Judgment,
    evaluate,
    load_judgments,
    recall_at_k,
    reciprocal_rank,
)


def test_recall_counts_distinct_resolutions_in_the_first_k() -> None:
    # Three chunks of resolution 1 take one slot, so 3 is still inside the top 3.
    ranked = [1, 1, 1, 2, 3]
    assert recall_at_k({3, 9}, ranked, 3) == 0.5
    assert recall_at_k({3, 9}, ranked, 2) == 0.0


def test_reciprocal_rank_is_the_first_relevant_resolution() -> None:
    assert reciprocal_rank({2}, [1, 1, 2, 2]) == 0.5
    assert reciprocal_rank({7}, [1, 2]) == 0.0


def test_evaluate_averages_over_queries() -> None:
    judgments = [
        Judgment("a", "uno", frozenset({1})),
        Judgment("b", "dos", frozenset({2})),
    ]
    rankings = {"uno": [1], "dos": [3, 2]}
    scores = evaluate(judgments, lambda query: rankings[query], k=1)
    assert (scores.recall, scores.mrr, scores.queries) == (0.5, 0.75, 2)


def test_judgments_reject_a_query_nothing_answers(tmp_path: Path) -> None:
    path = tmp_path / "queries.jsonl"
    path.write_text('{"id": "q1", "query": "hola", "relevant": []}\n')
    with pytest.raises(ValueError, match="relevant resolution"):
        load_judgments(path)


def test_command_scores_each_mode_of_a_running_api(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    modes: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            modes.append(body["mode"])
            # Only the hybrid mode finds resolution 5.
            ids = [5] if body["mode"] == "hybrid" else [6]
            payload = json.dumps(
                {"results": [{"resolution_id": i} for i in ids]}
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *arguments: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    queries = tmp_path / "queries.jsonl"
    queries.write_text('{"id": "q1", "query": "hola", "relevant": [5]}\n')
    try:
        main(
            [
                "--api",
                f"http://127.0.0.1:{server.server_port}",
                "--queries",
                str(queries),
            ]
        )
    finally:
        server.shutdown()

    lines = capsys.readouterr().out.splitlines()
    assert lines[2].split() == ["vector", "0.000", "0.000"]
    assert lines[3].split() == ["text", "0.000", "0.000"]
    assert lines[4].split() == ["hybrid", "1.000", "1.000"]
    assert modes == ["vector", "text", "hybrid"]
