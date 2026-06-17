"""Repo-wide pytest fixtures.

The Advocate asserts ``ptrace_scope >= 1`` at boot (HIGH-2) and fails closed
otherwise. CI and dev machines may run with ``ptrace_scope=0`` (e.g. some
container hosts), which is irrelevant to the unit/datadir/functional suites —
those never spawn a real same-UID agent against a live Advocate. Set the
documented dev/test override for the whole session so the suite is deterministic
regardless of the runner's kernel setting. The override is also inherited by the
functional subprocess (it reads ``os.environ``).
"""

from __future__ import annotations

import os

os.environ.setdefault("ADVOCATE_ALLOW_UNSAFE_PTRACE", "1")
