from collections.abc import Iterable


def accuracy(expected: Iterable[str], actual: Iterable[str]) -> float:
    """Return exact-match accuracy for aligned expected and actual answers."""
    expected_values = list(expected)
    actual_values = list(actual)
    if len(expected_values) != len(actual_values):
        raise ValueError("expected and actual must have the same length")
    if not expected_values:
        return 0.0
    return sum(
        left == right
        for left, right in zip(expected_values, actual_values, strict=True)
    ) / len(expected_values)
