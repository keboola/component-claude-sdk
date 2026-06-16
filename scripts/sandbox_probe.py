"""
sandbox_probe.py — Phase 0 spike: confirm OS primitives for the in-container sandbox.

Run inside a Linux container (python:3.14-slim, as root):

    docker build -f scripts/Dockerfile.probe -t sandbox-probe .
    docker run --rm --read-only --tmpfs /tmp:exec sandbox-probe

Or without a Dockerfile (quick one-liner):

    docker run --rm --read-only --tmpfs /tmp:exec \\
        -v $(pwd)/scripts:/probe:ro \\
        python:3.14-slim \\
        python /probe/sandbox_probe.py

Checks performed:
  1. seccomp: self-imposed filter blocks socket(AF_INET/AF_INET6) in a child; AF_UNIX still works.
  2. seccomp inherited across exec: a grandchild python -c confirms the filter survives exec.
  3. setuid drop: root→unprivileged UID.
  4. secret-file isolation: unprivileged child cannot read a root-owned chmod-600 temp file.
  5. unshare probe (optional hardening §5.2): CLONE_NEWNET / CLONE_NEWNS / CLONE_NEWPID.

Output: a single JSON object to stdout; DEBUG logs go to stderr.

Architecture note:
  Syscall numbers and BPF arch tags differ between x86_64 and aarch64.
  This probe detects the running arch and uses the appropriate constants.
  Logic intended for Phase 1's src/advocate/sandbox.py build_seccomp_filter().
"""

from __future__ import annotations

import ctypes
import errno
import json
import logging
import os
import platform
import socket
import struct
import sys
import tempfile
from typing import Any

logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Arch-specific constants
# ---------------------------------------------------------------------------

_MACHINE = platform.machine()
log.debug("machine: %s", _MACHINE)

if _MACHINE == "x86_64":
    NR_SECCOMP = 317
    NR_SOCKET = 41
    AUDIT_ARCH = 0xC000003E  # AUDIT_ARCH_X86_64
elif _MACHINE == "aarch64":
    NR_SECCOMP = 277
    NR_SOCKET = 198
    AUDIT_ARCH = 0xC00000B7  # AUDIT_ARCH_AARCH64
else:
    # Best-effort fallback; the probe will report the arch mismatch.
    NR_SECCOMP = 277
    NR_SOCKET = 198
    AUDIT_ARCH = 0xC00000B7
    log.warning("Unknown machine %s — using aarch64 constants; results may be incorrect", _MACHINE)

# Shared constants
PR_SET_NO_NEW_PRIVS = 38
SECCOMP_SET_MODE_FILTER = 1
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000  # | errno in low 16 bits

AF_INET = 2
AF_INET6 = 10
AF_UNIX = 1

CLONE_NEWNS = 0x00020000
CLONE_NEWNET = 0x40000000
CLONE_NEWPID = 0x20000000

AGENT_UID = 65534  # nobody

# ---------------------------------------------------------------------------
# ctypes / libc helpers
# ---------------------------------------------------------------------------

libc = ctypes.CDLL("libc.so.6", use_errno=True)


def _check_rc(name: str, rc: int) -> int:
    if rc < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{name} failed: {os.strerror(err)}")
    return rc


def prctl(option: int, arg2: int = 0) -> int:
    rc = libc.prctl(option, arg2, 0, 0, 0)
    return _check_rc("prctl", rc)


def setuid(uid: int) -> None:
    _check_rc("setuid", libc.setuid(uid))


def unshare(flags: int) -> bool:
    rc = libc.unshare(flags)
    if rc < 0:
        err = ctypes.get_errno()
        log.debug("unshare(0x%x) failed: %s", flags, os.strerror(err))
        return False
    return True


# ---------------------------------------------------------------------------
# BPF / seccomp filter builder
#
# Intended to be lifted into Phase 1 src/advocate/sandbox.py as
# build_seccomp_filter() (minus the arch-detection which will live at module level).
# ---------------------------------------------------------------------------


def _bpf_stmt(code: int, k: int) -> bytes:
    return struct.pack("HBBI", code, 0, 0, k)


def _bpf_jump(code: int, k: int, jt: int, jf: int) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def _build_bpf_program() -> bytes:
    """
    Build a BPF program that:
      - Verifies the running arch matches AUDIT_ARCH (conservative: allow on mismatch).
      - Denies socket(AF_INET/AF_INET6) with EACCES.
      - Allows everything else (including AF_UNIX).

    BPF_ABS offsets into struct seccomp_data:
      +0   u32 nr        (syscall number)
      +4   u32 arch      (AUDIT_ARCH_*)
      +8   u64 ip        (instruction pointer — 8 bytes)
      +16  u64 args[0]   (first syscall arg = socket domain)

    Instruction index / jump table (9 insns):
      0: load arch
      1: if arch != AUDIT_ARCH → allow (insn 8)  [jf=6 means skip 6 → land at 8]
      2: load nr
      3: if nr != NR_SOCKET → allow (insn 8)      [jf=4 means skip 4 → land at 8]
      4: load args[0] (domain)
      5: if domain == AF_INET  → deny (insn 7)
      6: if domain == AF_INET6 → deny (insn 7); else allow (insn 8)
      7: deny (EACCES)
      8: allow
    """
    BPF_LD = 0x00
    BPF_W = 0x00
    BPF_ABS = 0x20
    BPF_JMP = 0x05
    BPF_JEQ = 0x10
    BPF_K = 0x00
    BPF_RET = 0x06

    EACCES_RET = SECCOMP_RET_ERRNO | errno.EACCES

    insns = b""
    insns += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 4)  # 0: load arch
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH, 0, 6)  # 1: arch mismatch → allow
    insns += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 0)  # 2: load nr
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, NR_SOCKET, 0, 4)  # 3: not socket → allow
    insns += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 16)  # 4: load args[0]
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, AF_INET, 1, 0)  # 5: AF_INET → deny
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, AF_INET6, 0, 1)  # 6: AF_INET6 → deny; else allow
    insns += _bpf_stmt(BPF_RET | BPF_K, EACCES_RET)  # 7: deny
    insns += _bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW)  # 8: allow

    assert len(insns) % 8 == 0
    return insns


def install_seccomp_filter() -> None:
    """
    Install NO_NEW_PRIVS + the INET-deny seccomp filter on the calling process.
    Inherited across fork/exec (that is the whole point of NO_NEW_PRIVS).

    This is the prototype for Phase 1's spawn_sandboxed() pre-exec hook.
    """
    prctl(PR_SET_NO_NEW_PRIVS, 1)

    prog = _build_bpf_program()
    n_insns = len(prog) // 8

    # sock_fprog layout (64-bit): { u16 len; [6 bytes pad]; u64* filter }
    # We store the pointer as a u64 field in a _layout_='ms' struct so
    # ctypes does not apply additional pointer-width-dependent sizing.
    class SockFprog(ctypes.Structure):
        _layout_ = "ms"  # suppress Python 3.14 deprecation warning for _pack_
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

    log.debug("seccomp filter installed (%d insns)", n_insns)


# ---------------------------------------------------------------------------
# Fork-based check harness
# ---------------------------------------------------------------------------


def _child_run(fn, *args) -> tuple[bool, str]:
    """
    Fork a child, run fn(*args) in it.
    Child exits: 0=pass, 1=fail, 2=exception.
    Returns (passed: bool, detail: str).
    """
    pid = os.fork()
    if pid == 0:
        try:
            result = fn(*args)
            os._exit(0 if result else 1)
        except Exception as exc:
            log.debug("child exception: %s", exc)
            os._exit(2)
    _, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        code = os.WEXITSTATUS(status)
        return code == 0, f"exit={code}"
    sig = os.WTERMSIG(status)
    return False, f"killed-by-signal-{sig}"


# ---------------------------------------------------------------------------
# Check 1: seccomp blocks AF_INET
# ---------------------------------------------------------------------------


def _c1_inet_blocked() -> bool:
    install_seccomp_filter()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.close()
        return False  # should not reach
    except OSError as e:
        blocked = e.errno in (errno.EACCES, errno.EPERM)
        log.debug("AF_INET socket error: errno=%d blocked=%s", e.errno, blocked)
        return blocked


def check_seccomp_inet_blocked() -> dict[str, Any]:
    passed, detail = _child_run(_c1_inet_blocked)
    return {"pass": passed, "detail": detail, "description": "seccomp blocks socket(AF_INET)"}


# ---------------------------------------------------------------------------
# Check 2: AF_UNIX still works under seccomp
# ---------------------------------------------------------------------------


def _c2_afunix_ok() -> bool:
    install_seccomp_filter()
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "probe.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cli.connect(path)
        conn, _ = srv.accept()
        conn.sendall(b"ok")
        data = cli.recv(4)
        cli.close()
        conn.close()
        srv.close()
    return data == b"ok"


def check_afunix_ok() -> dict[str, Any]:
    passed, detail = _child_run(_c2_afunix_ok)
    return {"pass": passed, "detail": detail, "description": "AF_UNIX works under seccomp"}


# ---------------------------------------------------------------------------
# Check 3: seccomp filter inherited across exec
# ---------------------------------------------------------------------------


def check_seccomp_inherited_across_exec() -> dict[str, Any]:
    """
    Parent installs seccomp, forks a child that execs python -c '...'.
    The exec'd process tries AF_INET; exit 0 if blocked.
    """
    grandchild_code = (
        "import socket, errno, sys\n"
        "try:\n"
        "    socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "    sys.exit(1)\n"
        "except OSError as e:\n"
        "    sys.exit(0 if e.errno in (errno.EACCES, errno.EPERM) else 1)\n"
    )

    def _child_exec():
        install_seccomp_filter()
        os.execvp(sys.executable, [sys.executable, "-c", grandchild_code])

    passed, detail = _child_run(_child_exec)
    return {"pass": passed, "detail": detail, "description": "seccomp inherited across exec"}


# ---------------------------------------------------------------------------
# Check 4: root→unprivileged setuid drop
# ---------------------------------------------------------------------------


def _c4_setuid_drop() -> bool:
    setuid(AGENT_UID)
    effective = os.geteuid()
    log.debug("after setuid(%d): euid=%d", AGENT_UID, effective)
    return effective == AGENT_UID


def check_setuid_drop() -> dict[str, Any]:
    if os.geteuid() != 0:
        return {"pass": False, "detail": "not-root-skipped", "description": "root→uid drop"}
    passed, detail = _child_run(_c4_setuid_drop)
    return {"pass": passed, "detail": detail, "description": "root→uid drop (setuid)"}


# ---------------------------------------------------------------------------
# Check 5: unprivileged child cannot read root-owned 600 file
# ---------------------------------------------------------------------------


def _c5_cannot_read_secret(path: str) -> bool:
    setuid(AGENT_UID)
    try:
        with open(path):
            pass
        return False  # was able to read — isolation failed
    except PermissionError:
        return True
    except OSError as e:
        return e.errno == errno.EACCES


def check_unpriv_cannot_read_secret() -> dict[str, Any]:
    if os.geteuid() != 0:
        return {"pass": False, "detail": "not-root-skipped", "description": "unpriv cannot read secret"}
    fd, path = tempfile.mkstemp(prefix="probe_secret_")
    try:
        os.write(fd, b"supersecret")
        os.close(fd)
        os.chmod(path, 0o600)
        os.chown(path, 0, 0)
        passed, detail = _child_run(_c5_cannot_read_secret, path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return {"pass": passed, "detail": detail, "description": "unpriv child cannot read root-owned 600 file"}


# ---------------------------------------------------------------------------
# Check 6: unshare probes (optional §5.2 hardening)
# ---------------------------------------------------------------------------


def check_unshare_probes() -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name, flag in [("net", CLONE_NEWNET), ("mount", CLONE_NEWNS), ("pid", CLONE_NEWPID)]:

        def _child_unshare(f=flag):
            return unshare(f)

        passed, detail = _child_run(_child_unshare)
        results[name] = {"available": passed, "detail": detail}
    return results


# ---------------------------------------------------------------------------
# pyseccomp availability probe (answers the "which impl" question from the plan)
# ---------------------------------------------------------------------------


def probe_pyseccomp() -> dict[str, Any]:
    """
    Try to import pyseccomp (the 'seccomp' module).
    The plan requires we empirically determine whether it installs with only
    libseccomp2 (runtime) or needs gcc+libseccomp-dev (toolchain bloat).
    The Dockerfile.probe installs libseccomp2 only — if pyseccomp imports here,
    a prebuilt wheel exists and we could use it without a toolchain.
    """
    import importlib

    try:
        importlib.import_module("seccomp")
        return {"available": True, "note": "importable with libseccomp2 only (prebuilt wheel exists)"}
    except ImportError as e:
        return {"available": False, "note": str(e), "conclusion": "no prebuilt wheel; ctypes-bpf is the right choice"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if sys.platform != "linux":
        print(json.dumps({"error": "must run on Linux"}))
        sys.exit(1)

    findings: dict[str, Any] = {
        "machine": _MACHINE,
        "euid": os.geteuid(),
        "seccomp_impl": "ctypes-bpf",
        "pyseccomp_probe": probe_pyseccomp(),
    }

    log.info("=== CHECK 1: seccomp blocks AF_INET ===")
    findings["seccomp_inet_blocked"] = check_seccomp_inet_blocked()

    log.info("=== CHECK 2: AF_UNIX works under seccomp ===")
    findings["af_unix_ok"] = check_afunix_ok()

    log.info("=== CHECK 3: seccomp inherited across exec ===")
    findings["seccomp_exec_inherited"] = check_seccomp_inherited_across_exec()

    log.info("=== CHECK 4: setuid drop ===")
    findings["setuid_drop_ok"] = check_setuid_drop()

    log.info("=== CHECK 5: unpriv cannot read secret ===")
    findings["unpriv_cannot_read_config"] = check_unpriv_cannot_read_secret()

    log.info("=== CHECK 6: unshare probes ===")
    findings["unshare"] = check_unshare_probes()

    floor_checks = [
        findings["seccomp_inet_blocked"]["pass"],
        findings["af_unix_ok"]["pass"],
        findings["seccomp_exec_inherited"]["pass"],
        findings["setuid_drop_ok"]["pass"],
        findings["unpriv_cannot_read_config"]["pass"],
    ]
    findings["floor_holds"] = all(floor_checks)
    findings["floor_summary"] = "PASS" if findings["floor_holds"] else "FAIL"

    print(json.dumps(findings, indent=2))


if __name__ == "__main__":
    main()
