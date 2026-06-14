"""VCR functional tests — the testConnection sync action only (spec §7).

The Claude agent loop runs the ``claude`` CLI as a subprocess that makes its own
outbound HTTPS, so in-process VCR cannot capture it; those paths are covered by
the datadir functional tests under ``tests/datadir/`` (SDK boundary mocked) and
the unit tests under ``tests/unit/``. The **single in-process Anthropic HTTP
call** is the ``testConnection`` sync action, which IS VCR-recordable — recorded
once against the real API (key from ``secrets.json``), then replayed offline.

Each case under ``tests/functional/<name>/`` carries a recorded cassette; the
``VCR_SANITIZERS`` in ``src/component.py`` scrub the key/auth headers so no
secret value is stored.
"""

from pathlib import Path

import pytest
from keboola.datadirtest.vcr import VCRDataDirTester, get_test_cases

FUNCTIONAL_DIR = str(Path(__file__).parent / "functional")
COMPONENT_SCRIPT = str(Path(__file__).parent.parent / "src" / "component.py")


@pytest.mark.parametrize("test_name", get_test_cases(FUNCTIONAL_DIR))
def test_functional(test_name):
    """Replay a single VCR functional test case offline."""
    tester = VCRDataDirTester(
        data_dir=FUNCTIONAL_DIR,
        component_script=COMPONENT_SCRIPT,
        selected_tests=[test_name],
        vcr_mode="replay",
    )
    tester.run()
