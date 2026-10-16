"""Score a running API's `POST /search` on a judged query set.

python -m reclamos_eval --api http://localhost:8000
"""

import argparse
import json
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from reclamos_eval.retrieval import evaluate, load_judgments

DEFAULT_QUERIES = Path(__file__).resolve().parents[2] / "data" / "queries.jsonl"
MODES = ("vector", "text", "hybrid")
# Request enough chunks to usually include the five best distinct resolutions.
CHUNKS_PER_QUERY = 50


def search_resolutions(api: str, query: str, mode: str) -> list[int]:
    request = urllib.request.Request(
        f"{api.rstrip('/')}/search",
        data=json.dumps({"query": query, "k": CHUNKS_PER_QUERY, "mode": mode}).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    return [hit["resolution_id"] for hit in payload["results"]]


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="reclamos-eval")
    parser.add_argument("--api", required=True, help="base URL of the API")
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--k", type=int, default=5, help="resolutions for recall")
    arguments = parser.parse_args(argv)

    judgments = load_judgments(arguments.queries)
    print(f"{len(judgments)} queries from {arguments.queries}")
    print(f"{'mode':<8}{f'recall@{arguments.k}':>10}{'MRR':>8}")
    for mode in MODES:
        scores = evaluate(
            judgments,
            lambda query, mode=mode: search_resolutions(arguments.api, query, mode),
            k=arguments.k,
        )
        print(f"{mode:<8}{scores.recall:>10.3f}{scores.mrr:>8.3f}")


if __name__ == "__main__":
    main()
