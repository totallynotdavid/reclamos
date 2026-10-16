import pytest
from reclamos_eval import accuracy


def test_accuracy_counts_exact_matches() -> None:
    assert accuracy(["a", "b", "c"], ["a", "x", "c"]) == pytest.approx(2 / 3)


def test_accuracy_rejects_unaligned_inputs() -> None:
    with pytest.raises(ValueError, match="same length"):
        accuracy(["a"], [])
