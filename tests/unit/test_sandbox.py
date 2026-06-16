"""Unit tests for advocate/sandbox.py — BPF program shape validation.

These tests run on macOS too (no seccomp/setuid available); they only validate
that build_seccomp_filter() produces a BPF program with the expected structure.
The Linux integration tests live in tests/integration/test_sandbox_linux.py.
"""

from __future__ import annotations

import errno as _errno
import platform
import struct

import pytest

from advocate.sandbox import (
    AF_INET,
    AF_INET6,
    AF_UNIX,
    AUDIT_ARCH,
    NR_SOCKET,
    SECCOMP_RET_ALLOW,
    SECCOMP_RET_ERRNO,
    build_seccomp_filter,
)

# ---------------------------------------------------------------------------
# BPF instruction decoder
# ---------------------------------------------------------------------------

BPF_LD = 0x00
BPF_W = 0x00
BPF_ABS = 0x20
BPF_JMP = 0x05
BPF_JEQ = 0x10
BPF_K = 0x00
BPF_RET = 0x06


def _decode(raw: bytes) -> list[dict]:
    """Decode raw BPF bytecode into a list of instruction dicts."""
    assert len(raw) % 8 == 0, "BPF instruction must be 8 bytes"
    insns = []
    for i in range(len(raw) // 8):
        code, jt, jf, k = struct.unpack_from("HBBI", raw, i * 8)
        insns.append({"code": code, "jt": jt, "jf": jf, "k": k, "idx": i})
    return insns


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _eacces_ret() -> int:
    return SECCOMP_RET_ERRNO | _errno.EACCES


# ---------------------------------------------------------------------------
# Tests — BPF program shape
# ---------------------------------------------------------------------------


class TestBuildSeccompFilterShape:
    """Assert structural properties of the BPF program returned by build_seccomp_filter()."""

    @pytest.fixture(scope="class")
    @classmethod
    def prog_bytes(cls):
        return build_seccomp_filter()

    @pytest.fixture(scope="class")
    @classmethod
    def insns(cls, prog_bytes):
        return _decode(prog_bytes)

    def test_returns_bytes(self, prog_bytes):
        assert isinstance(prog_bytes, bytes)

    def test_length_multiple_of_8(self, prog_bytes):
        assert len(prog_bytes) % 8 == 0

    def test_exactly_9_instructions(self, insns):
        # 0: load arch, 1: arch check, 2: load nr, 3: nr check,
        # 4: load args[0], 5: AF_INET check, 6: AF_INET6 check,
        # 7: deny, 8: allow
        assert len(insns) == 9

    def test_insn0_loads_arch_field(self, insns):
        """insn 0: BPF_LD | BPF_W | BPF_ABS, offset=4 (arch field in seccomp_data)."""
        i = insns[0]
        assert i["code"] == (BPF_LD | BPF_W | BPF_ABS)
        assert i["k"] == 4  # offset of arch in struct seccomp_data

    def test_insn1_checks_audit_arch_and_denies_on_mismatch(self, insns):
        """insn 1: JEQ arch; arch-mismatch branch goes to DENY (not ALLOW).

        The verifier hardening note requires arch-mismatch → DENY.
        jt=0 → match path falls through to insn 2 (load nr).
        jf=5 → mismatch jumps forward 5 → lands at insn 7 (deny).
        """
        i = insns[1]
        assert i["code"] == (BPF_JMP | BPF_JEQ | BPF_K)
        assert i["k"] == AUDIT_ARCH
        # match (jt=0): continue to insn 2
        assert i["jt"] == 0
        # mismatch (jf): jump to the deny instruction (idx 7)
        # insn1 is at idx 1; deny is at idx 7; delta = 7 - 1 - 1 = 5
        assert i["jf"] == 5, f"arch-mismatch must jump to deny (idx 7), got jf={i['jf']}"

    def test_insn2_loads_syscall_nr(self, insns):
        """insn 2: BPF_LD | BPF_W | BPF_ABS, offset=0 (nr field)."""
        i = insns[2]
        assert i["code"] == (BPF_LD | BPF_W | BPF_ABS)
        assert i["k"] == 0  # offset of nr in struct seccomp_data

    def test_insn3_checks_nr_socket_and_allows_other_syscalls(self, insns):
        """insn 3: JEQ NR_SOCKET; non-socket syscalls jump to allow (idx 8)."""
        i = insns[3]
        assert i["code"] == (BPF_JMP | BPF_JEQ | BPF_K)
        assert i["k"] == NR_SOCKET
        # match (jt=0): continue to insn 4 (load domain arg)
        assert i["jt"] == 0
        # non-socket (jf): jump to allow at idx 8
        # insn3 is at idx 3; allow is at idx 8; delta = 8 - 3 - 1 = 4
        assert i["jf"] == 4, f"non-socket must jump to allow (idx 8), got jf={i['jf']}"

    def test_insn4_loads_domain_arg(self, insns):
        """insn 4: load args[0] (domain), offset=16 in struct seccomp_data."""
        i = insns[4]
        assert i["code"] == (BPF_LD | BPF_W | BPF_ABS)
        assert i["k"] == 16  # offset of args[0]

    def test_insn5_branches_on_af_inet(self, insns):
        """insn 5: JEQ AF_INET → deny; no match falls through to AF_INET6 check."""
        i = insns[5]
        assert i["code"] == (BPF_JMP | BPF_JEQ | BPF_K)
        assert i["k"] == AF_INET
        # match: skip 1 → land at insn 7 (deny)
        assert i["jt"] == 1
        # no match: fall through to insn 6
        assert i["jf"] == 0

    def test_insn6_branches_on_af_inet6(self, insns):
        """insn 6: JEQ AF_INET6 → deny; no match → allow."""
        i = insns[6]
        assert i["code"] == (BPF_JMP | BPF_JEQ | BPF_K)
        assert i["k"] == AF_INET6
        # match: skip 0 → land at insn 7 (deny)
        assert i["jt"] == 0
        # no match: skip 1 → land at insn 8 (allow)
        assert i["jf"] == 1

    def test_insn7_is_deny_eacces(self, insns):
        """insn 7: RET with EACCES."""
        i = insns[7]
        assert i["code"] == (BPF_RET | BPF_K)
        assert i["k"] == _eacces_ret()

    def test_insn8_is_allow(self, insns):
        """insn 8: RET ALLOW."""
        i = insns[8]
        assert i["code"] == (BPF_RET | BPF_K)
        assert i["k"] == SECCOMP_RET_ALLOW

    def test_af_unix_not_denied(self):
        """AF_UNIX is neither AF_INET nor AF_INET6 — confirm the constants."""
        assert AF_UNIX == 1
        assert AF_INET == 2
        assert AF_INET6 == 10
        assert AF_UNIX not in (AF_INET, AF_INET6)


class TestArchConstants:
    """Confirm arch constants match the probe's values for the running machine."""

    def test_audit_arch_matches_known_values(self):
        machine = platform.machine()
        if machine == "x86_64":
            assert AUDIT_ARCH == 0xC000003E
            assert NR_SOCKET == 41
        elif machine == "aarch64":
            assert AUDIT_ARCH == 0xC00000B7
            assert NR_SOCKET == 198
        else:
            pytest.skip(f"Unknown machine {machine!r} — no known-correct constants to compare")

    def test_seccomp_ret_allow_value(self):
        assert SECCOMP_RET_ALLOW == 0x7FFF0000

    def test_seccomp_ret_errno_base(self):
        assert SECCOMP_RET_ERRNO == 0x00050000
