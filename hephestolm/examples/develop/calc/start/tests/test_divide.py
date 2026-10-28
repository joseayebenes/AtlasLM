"""Acceptance test of divide: each step prints where it fails."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import calc  # noqa: E402


def check(step, got, want):
    if got != want:
        print(f"Step {step}: expected {want!r}, received {got!r}")
        sys.exit(1)


divide = getattr(calc, "divide", None)
check(1, callable(divide), True)
check(2, divide(6, 3), 2)
check(3, divide(7, 2), 3.5)
try:
    divide(1, 0)
    check(4, "no error", "ValueError")
except ValueError as e:
    check(4, str(e), "division by zero")
print("passed")
