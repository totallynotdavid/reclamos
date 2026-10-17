"""The regression gate: fused recall@5 on the fixed index against a baseline."""

import json
from dataclasses import dataclass
from pathlib import Path

from reclamos_eval.questions import DATA

GATE_DATA = DATA / "gate"
SUBSET = GATE_DATA / "subset.jsonl.gz"
BASELINE = GATE_DATA / "baseline.json"
# The largest recall@5 drop below the baseline that still passes.
TOLERANCE = 0.02


@dataclass(frozen=True, slots=True)
class Baseline:
    recall_at_5: float
    questions: int


@dataclass(frozen=True, slots=True)
class Verdict:
    passed: bool
    message: str


def read_baseline(path: Path = BASELINE) -> Baseline:
    record = json.loads(path.read_text(encoding="utf-8"))
    return Baseline(float(record["recall_at_5"]), int(record["questions"]))


def write_baseline(baseline: Baseline, path: Path = BASELINE) -> None:
    record = {
        "recall_at_5": round(baseline.recall_at_5, 4),
        "questions": baseline.questions,
    }
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def check(measured: Baseline, baseline: Baseline) -> Verdict:
    """Pass unless `measured` recall@5 is more than `TOLERANCE` below `baseline`."""
    drop = round(baseline.recall_at_5 - measured.recall_at_5, 6)
    summary = (
        f"fused recall@5 {measured.recall_at_5:.4f} over {measured.questions} questions; "
        f"baseline {baseline.recall_at_5:.4f}"
    )
    if drop > TOLERANCE:
        return Verdict(
            False,
            f"FAIL: {summary}; down {drop * 100:.1f} points, more than the "
            f"{TOLERANCE * 100:.0f} allowed",
        )
    return Verdict(True, f"ok: {summary}")
