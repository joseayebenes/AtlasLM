"""The fixtures of the tests of atlas (tests/conftest.py at the root): the samples, the fake
API, toml. hephesto is tested against the atlas of the same repository."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.conftest import *  # noqa: E402,F401,F403
