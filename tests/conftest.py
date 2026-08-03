"""Shared fixture loading for the parity suites.

The JSON files under shared/parity/ are the single spec for logic that exists in
both this backend and the Next.js frontend. See shared/parity/README.md.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PARITY_DIR = REPO_ROOT / "shared" / "parity"


def load_parity_fixture(name: str) -> dict:
    path = PARITY_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"Shared parity fixture missing: {path}. "
            "It is also consumed by the frontend test suite — do not move it "
            "without updating both runners."
        )
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)
