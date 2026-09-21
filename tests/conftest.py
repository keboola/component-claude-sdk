"""Repo-wide pytest fixtures.

The Advocate protects its memory at boot (HIGH-2) by marking itself non-dumpable,
and falls back to requiring ``ptrace_scope >= 1`` only where ``prctl`` is
unavailable; it fails closed when neither holds. On a non-Linux dev machine with
``ptrace_scope=0`` that fallback would abort the suite, which is irrelevant here —
the unit/datadir/functional suites never spawn a real same-UID agent against a
live Advocate. Set the documented dev/test override for the whole session so the
suite is deterministic regardless of the runner's kernel. The override is also
inherited by the functional subprocess (it reads ``os.environ``), and it does NOT
mask the primary mechanism: ``TestAdvocateMemoryProtection`` asserts the real
``PR_SET_DUMPABLE`` behaviour in its own subprocesses.
"""

from __future__ import annotations

import os

os.environ.setdefault("ADVOCATE_ALLOW_UNSAFE_PTRACE", "1")
