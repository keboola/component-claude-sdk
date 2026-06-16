"""advocate/sandbox.py — spawn a child process under the security floor.

This module provides two public entry-points:

  build_seccomp_filter() → bytes
      Return a BPF program (raw bytecode) that:
        - denies socket(AF_INET/AF_INET6) with EACCES, and
        - allows everything else, including AF_UNIX.
      The filter is arch-aware: if the running arch does not match the compiled
      AUDIT_ARCH constant, the filter DENIES the call (fail-closed) rather than
      allowing it — preventing a multi-arch-safe filter from falling open.

  spawn_sandboxed(argv, *, uid, cleared_env, workspace, uds_path) → int
      Fork a child, install the seccomp filter in the child, drop to ``uid``,
      and exec ``argv`` with only ``cleared_env`` in the environment.
      Returns the child PID; the caller must waitpid().

OS primitives used (Linux only):
  - prctl(PR_SET_NO_NEW_PRIVS, 1)
  - seccomp(SECCOMP_SET_MODE_FILTER, ...)  via raw syscall
  - setuid(uid)
  - os.execve

Architecture note:
  Syscall numbers and AUDIT_ARCH tags differ between x86_64 and aarch64.
  Constants are selected at module load time via platform.machine().
  Lifted directly from scripts/sandbox_probe.py (Phase 0 proven implementation).

Namespace hardening (§5.2) is NOT wired in here — the floor runs without it.
An extension point is marked with ``_apply_optional_namespaces()`` below; slot
Phase 7 namespace work there without touching the floor logic.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import platform
import struct
import sys
from collections.abc import Sequence

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Arch-specific constants (selected at module load)
# ---------------------------------------------------------------------------

_MACHINE = platform.machine()

if _MACHINE == "x86_64":
    NR_SECCOMP: int = 317
    NR_SOCKET: int = 41
    AUDIT_ARCH: int = 0xC000003E  # AUDIT_ARCH_X86_64
elif _MACHINE == "aarch64":
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
PR_SET_NO_NEW_PRIVS: int = 38
SECCOMP_SET_MODE_FILTER: int = 1
SECCOMP_RET_ALLOW: int = 0x7FFF0000
SECCOMP_RET_ERRNO: int = 0x00050000  # OR'd with errno in low 16 bits

AF_INET: int = 2
AF_INET6: int = 10
AF_UNIX: int = 1

AGENT_UID: int = 65534  # nobody

# ---------------------------------------------------------------------------
# libc handle (lazy, Linux-only)
# ---------------------------------------------------------------------------

_libc: ctypes.CDLL | None = None


def _get_libc() -> ctypes.CDLL:
    global _libc  # noqa: PLW0603
    if _libc is None:
        if sys.platform != "linux":
            raise OSError("advocate.sandbox requires Linux (seccomp/prctl/setuid are Linux-only)")
        _libc = ctypes.CDLL("libc.so.6", use_errno=True)
    return _libc


def _check_rc(name: str, rc: int) -> int:
    if rc < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{name} failed: {os.strerror(err)}")
    return rc


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
    insns += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 4)              # 0: load arch field
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH, 0, 5)  # 1: arch match→2, mismatch→7 (DENY)
    insns += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 0)              # 2: load nr field
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, NR_SOCKET, 0, 4)   # 3: socket→4, other→8 (ALLOW)
    insns += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 16)             # 4: load args[0] (domain)
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, AF_INET, 1, 0)     # 5: AF_INET→7 (DENY), else→6
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, AF_INET6, 0, 1)    # 6: AF_INET6→7 (DENY), else→8 (ALLOW)
    insns += _bpf_stmt(BPF_RET | BPF_K, EACCES_RET)              # 7: DENY
    insns += _bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW)       # 8: ALLOW

    assert len(insns) % 8 == 0
    return insns


# ---------------------------------------------------------------------------
# Public: install_seccomp_filter (used directly in tests + called by spawn)
# ---------------------------------------------------------------------------


def install_seccomp_filter() -> None:
    """Install NO_NEW_PRIVS and the INET-deny seccomp filter on the calling process.

    The filter is inherited across fork/exec — every child and grandchild of
    the calling process is equally netless.  Call this in the child immediately
    before exec (or to test the filter in the current process).
    """
    libc = _get_libc()

    rc = libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    _check_rc("prctl(PR_SET_NO_NEW_PRIVS)", rc)

    prog = build_seccomp_filter()
    n_insns = len(prog) // 8

    # sock_fprog layout (64-bit): { u16 len; [6 bytes pad]; u64* filter }
    # Use _layout_='ms' + _pack_=8 to match the kernel's struct layout without
    # triggering Python 3.14's _pack_ deprecation warning.
    # Pass via ctypes.byref() — addressof() gives EFAULT (proven in Phase 0).
    class SockFprog(ctypes.Structure):
        _layout_ = "ms"
        _pack_ = 8
        _fields_ = [
            ("len", ctypes.c_uint16),
            ("filter", ctypes.c_uint64),
        ]

    filter_buf = (ctypes.c_uint8 * len(prog))(*prog)
    fprog = SockFprog(n_insns, ctypes.addressof(filter_buf))

    rc = libc.syscall(NR_SECCOMP, SECCOMP_SET_MODE_FILTER, 0, ctypes.byref(fprog))
    if rc < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"seccomp(SET_MODE_FILTER) failed: {os.strerror(err)}")

    log.debug("sandbox: seccomp filter installed (%d insns)", n_insns)


# ---------------------------------------------------------------------------
# §5.2 extension point — namespace hardening (deferred to Phase 7)
# ---------------------------------------------------------------------------


def _apply_optional_namespaces() -> None:  # pragma: no cover
    """Hook for Phase 7 (§5.2) namespace hardening.

    Call this in the child after install_seccomp_filter() and before setuid/exec.
    Current implementation is a no-op — the floor does not depend on namespaces.

    When the infra answers (spec §12.1) confirm CLONE_NEWNET / CLONE_NEWNS /
    CLONE_NEWPID availability on kbc-stacks, implement unshare() calls here.
    The caller (spawn_sandboxed) is already structured to invoke this hook.
    """
    # Phase 7: unshare(CLONE_NEWNET | CLONE_NEWNS | CLONE_NEWPID) here if available.
    pass


# ---------------------------------------------------------------------------
# Public: spawn_sandboxed
# ---------------------------------------------------------------------------


def spawn_sandboxed(
    argv: Sequence[str],
    *,
    uid: int,
    cleared_env: dict[str, str],
    workspace: str,
    uds_path: str,
) -> int:
    """Fork a child, sandbox it, and exec argv.  Returns the child PID.

    Security sequence in the child (spec §6 step 6):
        1. prctl(PR_SET_NO_NEW_PRIVS, 1)
        2. install seccomp filter (deny AF_INET/AF_INET6)
        3. _apply_optional_namespaces() — no-op now; Phase 7 hook
        4. setuid(uid)
        5. os.execve(argv[0], argv, cleared_env)

    The caller is responsible for:
      - Preparing cleared_env (no KBC_TOKEN, no #secrets — only ORCHESTRATOR_UDS,
        ANTHROPIC_BASE_URL, PATH, workspace paths, etc.).
      - Calling os.waitpid(pid, 0) on the returned pid.

    Args:
        argv:        Command + arguments for the child process.
        uid:         UID to drop to before exec (e.g. AGENT_UID = 65534).
        cleared_env: Environment dict for the child.  Must contain no secrets.
        workspace:   Working directory for the child (e.g. /tmp/agent).
        uds_path:    Path to the unix-domain socket the child may connect to.
                     Currently unused by the launcher itself — passed so callers
                     can wire ORCHESTRATOR_UDS into cleared_env before calling.

    Returns:
        int: PID of the forked child.

    Raises:
        OSError: if prctl, seccomp install, setuid, or exec fails.
    """
    if sys.platform != "linux":
        raise OSError("spawn_sandboxed requires Linux")

    libc = _get_libc()

    pid = os.fork()
    if pid != 0:
        # Parent: return child PID immediately.
        log.debug("sandbox: spawned child pid=%d uid=%d argv=%s", pid, uid, argv[0])
        return pid

    # ---- CHILD ----
    try:
        # 1+2. NO_NEW_PRIVS + seccomp
        install_seccomp_filter()

        # 3. Optional namespace hardening (§5.2 extension point, currently no-op)
        _apply_optional_namespaces()

        # 4. Drop privileges
        rc = libc.setuid(uid)
        if rc < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"setuid({uid}) failed: {os.strerror(err)}")

        log.debug("sandbox: child dropped to uid=%d", uid)

        # 5. Exec — replaces this process image entirely.
        os.execve(argv[0], list(argv), cleared_env)

    except Exception:  # noqa: BLE001
        log.exception("sandbox: child setup failed")
        os._exit(127)

    # Unreachable (execve replaces this image or raises)
    os._exit(127)  # pragma: no cover
