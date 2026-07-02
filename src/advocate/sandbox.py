"""advocate/sandbox.py — BPF seccomp filter builder (V0 stub).

The only public API is:

  build_seccomp_filter() → bytes
      Return a BPF program (raw bytecode) that:
        - denies socket(AF_INET/AF_INET6) with EACCES, and
        - allows everything else, including AF_UNIX.
      The filter is arch-aware: if the running arch does not match the compiled
      AUDIT_ARCH constant, the filter DENIES the call (fail-closed) rather than
      allowing it — preventing a multi-arch-safe filter from falling open.

V0 note: spawn_sandboxed and install_seccomp_filter were removed because:
  - The runtime runs as euid=1000 (non-root), so uid-drop via setuid() is a
    no-op and seccomp via prctl requires CAP_SYS_ADMIN on newer kernels.
  - The ``claude`` CLI requires loopback TCP (AF_INET); installing the
    AF_INET-deny filter breaks it.
  - build_seccomp_filter() is retained as a V1+ stub (spec §12.6).

Architecture note:
  Syscall numbers and AUDIT_ARCH tags differ between x86_64 and aarch64.
  Constants are selected at module load time via platform.machine().
  Lifted directly from scripts/sandbox_probe.py (Phase 0 proven implementation).
"""

from __future__ import annotations

import errno
import logging
import platform
import struct

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Arch-specific constants (selected at module load)
# ---------------------------------------------------------------------------

_MACHINE = platform.machine()

if _MACHINE == "x86_64":
    NR_SECCOMP: int = 317
    NR_SOCKET: int = 41
    AUDIT_ARCH: int = 0xC000003E  # AUDIT_ARCH_X86_64
elif _MACHINE in ("aarch64", "arm64"):  # arm64 = Apple Silicon alias for aarch64
    NR_SECCOMP = 277
    NR_SOCKET = 198
    AUDIT_ARCH = 0xC00000B7  # AUDIT_ARCH_AARCH64
else:
    # Unknown arch — keep conservative defaults.  The arch-mismatch branch in
    # the BPF program will DENY rather than fall open, so running with wrong
    # constants is a hard failure rather than a silent security hole.
    log.warning("sandbox: unknown machine %r — using aarch64 constants; seccomp will deny all socket()", _MACHINE)
    NR_SECCOMP = 277
    NR_SOCKET = 198
    AUDIT_ARCH = 0xC00000B7

# Shared constants
SECCOMP_RET_ALLOW: int = 0x7FFF0000
SECCOMP_RET_ERRNO: int = 0x00050000  # OR'd with errno in low 16 bits

AF_INET: int = 2
AF_INET6: int = 10
AF_UNIX: int = 1

# ---------------------------------------------------------------------------
# BPF instruction builders
# ---------------------------------------------------------------------------


def _bpf_stmt(code: int, k: int) -> bytes:
    return struct.pack("HBBI", code, 0, 0, k)


def _bpf_jump(code: int, k: int, jt: int, jf: int) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


# ---------------------------------------------------------------------------
# Public: build_seccomp_filter
# ---------------------------------------------------------------------------


def build_seccomp_filter() -> bytes:
    """Build a BPF program that denies socket(AF_INET/AF_INET6) with EACCES.

    Returns raw BPF bytecode (9 instructions × 8 bytes = 72 bytes).

    NOT called in V0 — the runtime is non-root (euid=1000) and the ``claude``
    CLI requires loopback TCP; seccomp AF_INET deny breaks it.  Retained as a
    future V1+ stub.  See spec §12.6.

    Arch-mismatch policy (hardened vs. the Phase 0 probe):
        If the running arch does not match AUDIT_ARCH, the filter DENIES the
        call rather than allowing it.  This makes the multi-arch filter
        fail-closed: a filter built for aarch64 that somehow runs on x86_64
        blocks all socket() calls rather than silently allowing inet sockets.

    BPF_ABS offsets into struct seccomp_data:
        +0   u32 nr        (syscall number)
        +4   u32 arch      (AUDIT_ARCH_*)
        +8   u64 ip        (8 bytes — skipped)
        +16  u64 args[0]   (first syscall arg = socket domain)

    Instruction table:
        0: load arch
        1: JEQ AUDIT_ARCH; match→insn2, mismatch→insn7 (DENY)
        2: load nr
        3: JEQ NR_SOCKET; match→insn4, no-match→insn8 (ALLOW)
        4: load args[0] (domain)
        5: JEQ AF_INET;  match→insn7 (DENY), no-match→insn6
        6: JEQ AF_INET6; match→insn7 (DENY), no-match→insn8 (ALLOW)
        7: RET EACCES  (DENY)
        8: RET ALLOW
    """
    BPF_LD = 0x00
    BPF_W = 0x00
    BPF_ABS = 0x20
    BPF_JMP = 0x05
    BPF_JEQ = 0x10
    BPF_K = 0x00
    BPF_RET = 0x06

    EACCES_RET = SECCOMP_RET_ERRNO | errno.EACCES

    # insn 1: JEQ AUDIT_ARCH; jt=0 (match→fall through to insn 2), jf=5 (mismatch→insn 7 DENY)
    # insn 3: JEQ NR_SOCKET;  jt=0 (match→fall through to insn 4), jf=4 (no-match→insn 8 ALLOW)
    # insn 5: JEQ AF_INET;    jt=1 (match→skip insn 6→insn 7 DENY), jf=0 (no-match→insn 6)
    # insn 6: JEQ AF_INET6;   jt=0 (match→insn 7 DENY), jf=1 (no-match→skip insn 7→insn 8 ALLOW)

    insns = b""
    insns += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 4)  # 0: load arch field
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH, 0, 5)  # 1: arch match→2, mismatch→7 (DENY)
    insns += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 0)  # 2: load nr field
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, NR_SOCKET, 0, 4)  # 3: socket→4, other→8 (ALLOW)
    insns += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 16)  # 4: load args[0] (domain)
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, AF_INET, 1, 0)  # 5: AF_INET→7 (DENY), else→6
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, AF_INET6, 0, 1)  # 6: AF_INET6→7 (DENY), else→8 (ALLOW)
    insns += _bpf_stmt(BPF_RET | BPF_K, EACCES_RET)  # 7: DENY
    insns += _bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW)  # 8: ALLOW

    assert len(insns) % 8 == 0
    return insns
