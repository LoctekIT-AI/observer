"""Run the tests against the package source in bundle/ (python3 -m pytest tests)."""
import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[2] / "bundle"
for path in (BUNDLE / "apps/cowork-runtime/src", BUNDLE / "packages/contracts/src", BUNDLE / "scripts"):
    sys.path.insert(0, str(path))
