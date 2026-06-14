"""Record the VCR cassettes for the testConnection functional tests.

This is a thin driver around ``keboola.vcr``'s scaffolder so the real
``#anthropic_key`` is loaded from the repo-root secrets file **by the runner
itself, in Python** — it is never named on a shell command line and never
printed. The recorded cassettes are then replayed offline by
``tests/test_functional.py`` (no key required to replay).

Run from the repo root:  ``uv run python scripts/record_cassettes.py``
Add ``--regenerate`` to force re-recording of existing cassettes.

Only the ``testConnection`` sync action makes an in-process Anthropic HTTP call
(spec §7); the agent loop's CLI subprocess is not VCR-recordable and is covered
by the mocked-boundary datadir/unit tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

from keboola.vcr.scaffolder import TestScaffolder

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRETS_FILE = REPO_ROOT / "secrets.json"  # gitignored; read here at runtime only
DEFINITIONS = REPO_ROOT / "tests" / "setup" / "configs.json"
OUTPUT_DIR = REPO_ROOT / "tests" / "functional"
COMPONENT = REPO_ROOT / "src" / "component.py"


def main() -> int:
    regenerate = "--regenerate" in sys.argv
    secrets_file = SECRETS_FILE if SECRETS_FILE.exists() else None
    if secrets_file is None:
        print("WARNING: no repo-root secrets file found — the success cassette needs a real key.")

    created = TestScaffolder().scaffold_from_json(
        definitions_file=DEFINITIONS,
        output_dir=OUTPUT_DIR,
        component_script=COMPONENT,
        record=True,
        secrets_file=secrets_file,
        regenerate=regenerate,
        # No DB driver in this component; skip auto-detection's extra work.
        db_adapter=None,
    )
    print(f"Scaffolded/recorded {len(created)} test folder(s):")
    for path in created:
        print(f"  - {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
