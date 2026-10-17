"""Acceptance test of add: each step prints where it fails."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import calc  # noqa: E402


def check(step, got, want):
    if got != want:
        print(f"Step {step}: expected {want!r}, received {got!r}")
        sys.exit(1)


check(1, calc.add(1, 2), 3)
check(2, calc.add(-4, 4), 0)
check(3, calc.add(2.5, 0.5), 3.0)
print("passed")
