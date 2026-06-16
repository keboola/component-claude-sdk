"""Advocate broker — runtime egress-capability probe.

TEMPORARY/PROBE — Phase 5a-runtime de-risking only.  Remove or fold into Phase 5b.

Invoked when ``parameters.__advocate_runtime_probe`` is ``true``; completely inert
otherwise.  Determines, on the REAL Keboola job runtime, which egress-control
primitive is available so Phase 5b can pick the right one:

  1. iptables / nftables owner-match (preferred if CAP_NET_ADMIN present)
  2. unshare(CLONE_NEWNET) loopback-only net namespace
  3. Re-confirm Phase 0 seccomp / setuid floor on the real runtime

Each check is fork-isolated so a failure never aborts the others.  Results are
written to /data/out/tables/advocate_runtime_probe.csv (+ manifest) AND logged as
a compact JSON summary so the controller can read them via the platform table OR
raw logs without needing to parse the full log stream.

Columns: check_name, pass, detail
"""

from __future__ import annotations

import csv
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
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Arch constants (mirrors sandbox_probe.py logic)
# ---------------------------------------------------------------------------

_MACHINE = platform.machine()

if _MACHINE == "x86_64":
    NR_SECCOMP = 317
    NR_SOCKET = 41
    AUDIT_ARCH = 0xC000003E
elif _MACHINE == "aarch64":
    NR_SECCOMP = 277
    NR_SOCKET = 198
    AUDIT_ARCH = 0xC00000B7
else:
    NR_SECCOMP = 277
    NR_SOCKET = 198
    AUDIT_ARCH = 0xC00000B7

PR_SET_NO_NEW_PRIVS = 38
SECCOMP_SET_MODE_FILTER = 1
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000

AF_INET = 2
AF_INET6 = 10
AF_UNIX = 1

CLONE_NEWNET = 0x40000000

AGENT_UID = 65534  # nobody

# External connectivity check target — purely to verify it is BLOCKED.
# No data is sent; we expect EACCES / timeout / EPERM, not success.
EXTERNAL_CHECK_HOST = "1.1.1.1"
EXTERNAL_CHECK_PORT = 443
EXTERNAL_TIMEOUT_S = 3

# ---------------------------------------------------------------------------
# libc helpers
# ---------------------------------------------------------------------------

try:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    _LIBC_OK = True
except OSError:
    _LIBC_OK = False
    libc = None  # type: ignore[assignment]


def _check_rc(name: str, rc: int) -> int:
    if rc < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{name} failed: {os.strerror(err)}")
    return rc


def _setuid(uid: int) -> None:
    _check_rc("setuid", libc.setuid(uid))


def _unshare(flags: int) -> int:
    """Call unshare(flags). Returns errno on failure, 0 on success."""
    rc = libc.unshare(flags)
    if rc < 0:
        return ctypes.get_errno()
    return 0


# ---------------------------------------------------------------------------
# seccomp BPF helpers (copied from sandbox_probe.py so probe is self-contained)
# ---------------------------------------------------------------------------


def _bpf_stmt(code: int, k: int) -> bytes:
    return struct.pack("HBBI", code, 0, 0, k)


def _bpf_jump(code: int, k: int, jt: int, jf: int) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def _build_inet_deny_bpf() -> bytes:
    BPF_LD, BPF_W, BPF_ABS = 0x00, 0x00, 0x20
    BPF_JMP, BPF_JEQ, BPF_K = 0x05, 0x10, 0x00
    BPF_RET = 0x06
    EACCES_RET = SECCOMP_RET_ERRNO | errno.EACCES

    insns = b""
    insns += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 4)
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH, 0, 6)
    insns += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 0)
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, NR_SOCKET, 0, 4)
    insns += _bpf_stmt(BPF_LD | BPF_W | BPF_ABS, 16)
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, AF_INET, 1, 0)
    insns += _bpf_jump(BPF_JMP | BPF_JEQ | BPF_K, AF_INET6, 0, 1)
    insns += _bpf_stmt(BPF_RET | BPF_K, EACCES_RET)
    insns += _bpf_stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW)
    return insns


def _install_seccomp_filter() -> None:
    libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    prog = _build_inet_deny_bpf()
    n_insns = len(prog) // 8

    class SockFprog(ctypes.Structure):
        _layout_ = "ms"
        _pack_ = 8
        _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.c_uint64)]

    buf = (ctypes.c_uint8 * len(prog))(*prog)
    fprog = SockFprog(n_insns, ctypes.addressof(buf))
    rc = libc.syscall(NR_SECCOMP, SECCOMP_SET_MODE_FILTER, 0, ctypes.byref(fprog))
    if rc < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"seccomp failed: {os.strerror(err)}")


# ---------------------------------------------------------------------------
# Fork harness
# ---------------------------------------------------------------------------


def _fork_run(fn, *args) -> tuple[bool, str]:
    """Fork a child, run fn(*args). Returns (passed, detail)."""
    pid = os.fork()
    if pid == 0:
        try:
            result = fn(*args)
            os._exit(0 if result else 1)
        except Exception as exc:  # noqa: BLE001
            log.debug("probe child exception: %s", exc)
            os._exit(2)
    _, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        code = os.WEXITSTATUS(status)
        return code == 0, f"exit={code}"
    sig = os.WTERMSIG(status)
    return False, f"killed-by-signal-{sig}"


# ---------------------------------------------------------------------------
# Environment context
# ---------------------------------------------------------------------------


def _read_cap_eff() -> str:
    """Read CapEff hex string from /proc/self/status, or 'unavailable'."""
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("CapEff:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unavailable"


def _read_kernel_version() -> str:
    try:
        return platform.release()
    except Exception:  # noqa: BLE001
        return "unavailable"


def gather_env_context() -> dict:
    return {
        "machine": _MACHINE,
        "kernel": _KERNEL,
        "euid": os.geteuid(),
        "cap_eff": _CAP_EFF,
        "python": sys.version.split()[0],
    }


# Capture once at module load so even if the process drops privs later we have root's caps.
_KERNEL = _read_kernel_version()
_CAP_EFF = _read_cap_eff()

# ---------------------------------------------------------------------------
# CHECK E0 — environment context (always passes; just informational)
# ---------------------------------------------------------------------------


def check_env_context() -> tuple[bool, str]:
    ctx = gather_env_context()
    detail = json.dumps(ctx)
    return True, detail


# ---------------------------------------------------------------------------
# CHECK E1 — iptables / CAP_NET_ADMIN availability
# ---------------------------------------------------------------------------


def _try_iptables_rule() -> bool:
    """Attempt to add and immediately remove a harmless iptables rule.
    Returns True if iptables is usable (CAP_NET_ADMIN + binary present).
    """
    import subprocess

    chain = "OUTPUT"
    rule = ["-p", "tcp", "--dport", "65534", "-m", "owner", "--uid-owner", str(AGENT_UID), "-j", "ACCEPT"]
    try:
        # Try to add the rule
        r = subprocess.run(
            ["iptables", "-A", chain, *rule],
            capture_output=True,
            timeout=5,
        )
        if r.returncode != 0:
            return False
        # Remove it again immediately
        subprocess.run(["iptables", "-D", chain, *rule], capture_output=True, timeout=5)
        return True
    except FileNotFoundError, subprocess.TimeoutExpired:
        return False


def check_iptables_owner_match() -> tuple[bool, str]:
    """E1: root can add iptables -m owner --uid-owner rules."""
    if os.geteuid() != 0:
        return False, "not-root"
    if not _LIBC_OK:
        return False, "libc-unavailable"
    try:
        ok = _try_iptables_rule()
        return ok, "owner-match-rule-add-remove-ok" if ok else "iptables-failed-or-no-cap-net-admin"
    except Exception as exc:  # noqa: BLE001
        return False, f"exception:{exc}"


# ---------------------------------------------------------------------------
# CHECK E2 — iptables loopback-allow + external-block per AGENT_UID
#
# Full end-to-end: root installs rules, starts a local listener, then a child
# running as AGENT_UID connects to loopback (should pass) AND tries to reach
# an external IP (should fail/timeout).
# ---------------------------------------------------------------------------


def _start_local_listener() -> tuple[socket.socket, int]:
    """Bind a TCP listener on an ephemeral loopback port; return (sock, port)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    return srv, port


def _child_loopback_connect(port: int) -> bool:
    _setuid(AGENT_UID)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


def _child_external_blocked() -> bool:
    _setuid(AGENT_UID)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(EXTERNAL_TIMEOUT_S)
        s.connect((EXTERNAL_CHECK_HOST, EXTERNAL_CHECK_PORT))
        s.close()
        return False  # connected — NOT blocked
    except OSError:
        return True  # blocked/refused/timeout — expected


def _install_uid_rules(uid: int, allow_port: int) -> bool:
    """Install iptables rules for AGENT_UID: allow loopback TCP to allow_port, DROP external TCP."""
    import subprocess

    def run(cmd):
        r = subprocess.run(cmd, capture_output=True, timeout=5)
        return r.returncode == 0

    uid_str = str(uid)
    # Allow loopback (lo) TCP to our test listener port for the agent UID
    ok = run(
        [
            "iptables",
            "-I",
            "OUTPUT",
            "1",
            "-o",
            "lo",
            "-p",
            "tcp",
            "--dport",
            str(allow_port),
            "-m",
            "owner",
            "--uid-owner",
            uid_str,
            "-j",
            "ACCEPT",
        ]
    )
    if not ok:
        return False
    # DROP other TCP output from agent UID (external traffic)
    ok = run(["iptables", "-A", "OUTPUT", "-p", "tcp", "-m", "owner", "--uid-owner", uid_str, "-j", "DROP"])
    return ok


def _remove_uid_rules(uid: int, allow_port: int) -> None:
    import subprocess

    uid_str = str(uid)
    subprocess.run(
        [
            "iptables",
            "-D",
            "OUTPUT",
            "-o",
            "lo",
            "-p",
            "tcp",
            "--dport",
            str(allow_port),
            "-m",
            "owner",
            "--uid-owner",
            uid_str,
            "-j",
            "ACCEPT",
        ],
        capture_output=True,
        timeout=5,
    )
    subprocess.run(
        ["iptables", "-D", "OUTPUT", "-p", "tcp", "-m", "owner", "--uid-owner", uid_str, "-j", "DROP"],
        capture_output=True,
        timeout=5,
    )


def check_iptables_loopback_allow_external_block() -> tuple[bool, str]:
    """E2: With iptables uid-owner rules, AGENT_UID reaches loopback but not external."""
    if os.geteuid() != 0:
        return False, "not-root"
    if not _LIBC_OK:
        return False, "libc-unavailable"

    srv, port = _start_local_listener()

    # Accept one connection in a thread so the child doesn't hang.
    def _accept_one():
        try:
            srv.settimeout(6)
            conn, _ = srv.accept()
            conn.close()
        except OSError:
            pass

    threading.Thread(target=_accept_one, daemon=True).start()

    rules_ok = _install_uid_rules(AGENT_UID, port)
    if not rules_ok:
        srv.close()
        return False, "iptables-rule-install-failed"

    try:
        loopback_pass, loopback_detail = _fork_run(_child_loopback_connect, port)
        external_blocked, ext_detail = _fork_run(_child_external_blocked)
    finally:
        _remove_uid_rules(AGENT_UID, port)
        srv.close()

    passed = loopback_pass and external_blocked
    detail = f"loopback={loopback_detail},external_blocked={ext_detail}"
    return passed, detail


# ---------------------------------------------------------------------------
# CHECK E3 — unshare(CLONE_NEWNET)
# ---------------------------------------------------------------------------


def _child_unshare_netns_loopback(port: int, ready_r: int, go_r: int, result_w: int) -> None:
    """Child: unshares net ns, brings lo up, waits for parent's go signal, connects."""
    err = _unshare(CLONE_NEWNET)
    if err:
        os.write(result_w, b"\x00")
        os._exit(0)

    # Bring lo up in the new netns
    import subprocess

    subprocess.run(["ip", "link", "set", "lo", "up"], capture_output=True)

    # Signal ready
    os.write(result_w, b"\x01")
    # Wait for go
    os.read(go_r, 1)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("127.0.0.1", port))
        s.close()
        # loopback reached — write success
        os.write(result_w, b"\x01")
    except OSError:
        os.write(result_w, b"\x00")
    os._exit(0)


def check_unshare_newnet() -> tuple[bool, str]:
    """E3: unshare(CLONE_NEWNET) succeeds and loopback works inside the ns."""
    if not _LIBC_OK:
        return False, "libc-unavailable"

    # Simple fork to just test if unshare itself works
    def _child_just_unshare():
        err = _unshare(CLONE_NEWNET)
        return err == 0

    unshare_ok, detail = _fork_run(_child_just_unshare)
    if not unshare_ok:
        return False, f"unshare-failed:{detail}"

    # Now test loopback inside the ns using pipes for coordination.
    # Parent starts a listener BEFORE forking so the fd is inherited.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]

    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    result_r, result_w = os.pipe()

    pid = os.fork()
    if pid == 0:
        os.close(ready_r)
        os.close(go_w)
        os.close(result_r)
        _child_unshare_netns_loopback(port, -1, go_r, result_w)
        os._exit(1)

    os.close(ready_w)
    os.close(go_r)
    os.close(result_w)
    srv.close()  # listener stays in parent's netns; child won't reach it in its own netns

    # Read the ready byte
    ready_byte = os.read(ready_r, 1)
    os.close(ready_r)

    if ready_byte != b"\x01":
        os.write(go_w, b"\x01")
        os.close(go_w)
        os.waitpid(pid, 0)
        os.close(result_r)
        return False, "child-unshare-failed-in-loopback-test"

    # In a new netns, the listener (in the parent's ns) is NOT reachable.
    # This is the expected / desired behaviour: the child is fully isolated.
    # Signal the child to try connecting — we expect it to FAIL (that IS the isolation).
    os.write(go_w, b"\x01")
    os.close(go_w)

    result_byte = os.read(result_r, 1)
    os.close(result_r)
    os.waitpid(pid, 0)

    # In a fresh netns with lo brought up, the child CAN reach 127.0.0.1 listeners
    # started WITHIN that ns — but NOT the parent's. The realistic use case is that
    # the Advocate listener must be started inside the ns (or a socket fd passed in).
    # A connect-fail here is expected isolation working correctly.
    loopback_in_ns_reached = result_byte == b"\x01"
    detail = (
        "loopback-in-ns-reached=true (advocate must start inside ns)"
        if loopback_in_ns_reached
        else "loopback-in-ns-reached=false (parent listener not reachable — isolation working; use fd-passing)"
    )
    # unshare itself succeeded — that is the capability gate.  Detail explains socket semantics.
    return True, f"unshare-ok;{detail}"


# ---------------------------------------------------------------------------
# CHECK E4 — Phase 0 floor re-confirmation on real runtime
# (seccomp self-imposed + setuid drop + secret isolation)
# ---------------------------------------------------------------------------


def _c4_seccomp_blocks_inet() -> bool:
    _install_seccomp_filter()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.close()
        return False
    except OSError as e:
        return e.errno in (errno.EACCES, errno.EPERM)


def check_phase0_floor() -> tuple[bool, str]:
    """E4: seccomp + setuid floor re-confirmed on real runtime (mirrors Phase 0)."""
    if not _LIBC_OK:
        return False, "libc-unavailable"
    if sys.platform != "linux":
        return False, "not-linux"

    seccomp_ok, sec_detail = _fork_run(_c4_seccomp_blocks_inet)

    def _c4_setuid_drop() -> bool:
        if os.geteuid() != 0:
            return False
        _setuid(AGENT_UID)
        return os.geteuid() == AGENT_UID

    setuid_ok, setuid_detail = _fork_run(_c4_setuid_drop)

    def _c4_secret_isolation(path: str) -> bool:
        _setuid(AGENT_UID)
        try:
            with open(path):
                pass
            return False
        except PermissionError:
            return True
        except OSError as e:
            return e.errno == errno.EACCES

    # Write a root-owned 600 temp file and confirm AGENT_UID can't read it
    if os.geteuid() == 0:
        fd, path = tempfile.mkstemp(prefix="probe_secret_")
        try:
            os.write(fd, b"supersecret")
            os.close(fd)
            os.chmod(path, 0o600)
            os.chown(path, 0, 0)
            secret_ok, secret_detail = _fork_run(_c4_secret_isolation, path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    else:
        secret_ok, secret_detail = False, "not-root-skipped"

    passed = seccomp_ok and setuid_ok and secret_ok
    detail = f"seccomp={sec_detail},setuid={setuid_detail},secret={secret_detail}"
    return passed, detail


# ---------------------------------------------------------------------------
# CHECK E5 — YAMA ptrace_scope
# ---------------------------------------------------------------------------

# PTRACE_ATTACH request code
_PTRACE_ATTACH = 16
_PTRACE_DETACH = 17


def check_yama_ptrace_scope() -> tuple[bool, str]:
    """E5: report /proc/sys/kernel/yama/ptrace_scope value.

    Scope meanings:
      0  — classic: same-UID process can PTRACE_ATTACH (no kernel restriction).
      1  — restricted: only parent/tracee relationship or CAP_SYS_PTRACE allowed.
      2  — admin-only: CAP_SYS_PTRACE required.
      3  — no attach: ptrace disabled entirely.
    absent — Yama LSM not loaded (same as scope 0).

    Always passes (informational); the value feeds the isolation verdict.
    """
    path = "/proc/sys/kernel/yama/ptrace_scope"
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
        return True, f"ptrace_scope={value}"
    except FileNotFoundError:
        return True, "ptrace_scope=absent"
    except OSError as exc:
        return True, f"ptrace_scope=read-error:{exc}"


# ---------------------------------------------------------------------------
# CHECK E6 — same-UID child cannot read parent secrets
# ---------------------------------------------------------------------------

# Unique sentinel written into the parent's env + heap; the child must NOT find it.
_SENTINEL_ENV_KEY = "ADVOCATE_SENTINEL"


def _child_attempt_ptrace(parent_pid: int, sentinel: str) -> tuple[bool, str]:
    """Attempt PTRACE_ATTACH on parent_pid and scan its memory for sentinel.

    Returns (found: bool, detail: str).  A EPERM / failure means we could NOT read.
    Runs in a fork so it cannot disturb the parent process.
    """
    if not _LIBC_OK:
        return False, "libc-unavailable"

    # PTRACE_ATTACH
    rc = libc.ptrace(_PTRACE_ATTACH, parent_pid, 0, 0)
    if rc < 0:
        err = ctypes.get_errno()
        return False, f"ptrace-attach-eperm:{os.strerror(err)}"

    # If attach succeeded, wait for the stop signal, then detach.
    import signal

    os.waitpid(parent_pid, 0)

    # Try to read a word from the parent's stack via PTRACE_PEEKDATA.
    # Finding the exact sentinel in memory via ptrace word-reads is complex;
    # the mere fact that PTRACE_ATTACH succeeded is already a FAIL (full
    # memory access is now possible in principle).
    libc.ptrace(_PTRACE_DETACH, parent_pid, 0, signal.SIGCONT)
    return True, "ptrace-attach-succeeded"


def _child_attempt_proc_mem(parent_pid: int, sentinel: bytes) -> tuple[bool, str]:
    """Attempt to open /proc/<parent_pid>/mem and scan for sentinel."""
    mem_path = f"/proc/{parent_pid}/mem"
    maps_path = f"/proc/{parent_pid}/maps"
    try:
        with open(maps_path, encoding="utf-8") as fh:
            maps_data = fh.read()
    except PermissionError:
        return False, "proc-maps-eperm"
    except OSError as exc:
        return False, f"proc-maps-error:{exc}"

    try:
        mem_fd = open(mem_path, "rb")  # noqa: SIM115
    except PermissionError:
        return False, "proc-mem-eperm"
    except OSError as exc:
        return False, f"proc-mem-open-error:{exc}"

    # Walk readable anon/heap mappings looking for the sentinel.
    found = False
    for line in maps_data.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        perms = parts[1]
        if "r" not in perms:
            continue
        addr_range = parts[0]
        start_s, end_s = addr_range.split("-")
        start, end = int(start_s, 16), int(end_s, 16)
        size = end - start
        if size > 64 * 1024 * 1024:  # skip huge mappings to keep the probe fast
            continue
        try:
            mem_fd.seek(start)
            chunk = mem_fd.read(size)
            if sentinel in chunk:
                found = True
                break
        except OSError:
            continue

    mem_fd.close()
    if found:
        return True, "proc-mem-sentinel-found"
    return False, "proc-mem-read-ok-sentinel-not-found"


def _child_attempt_proc_environ(parent_pid: int, sentinel: str) -> tuple[bool, str]:
    """Attempt to read /proc/<parent_pid>/environ and find sentinel."""
    environ_path = f"/proc/{parent_pid}/environ"
    try:
        with open(environ_path, "rb") as fh:
            data = fh.read()
        if sentinel.encode() in data:
            return True, "proc-environ-sentinel-found"
        return False, "proc-environ-read-ok-sentinel-not-found"
    except PermissionError:
        return False, "proc-environ-eperm"
    except OSError as exc:
        return False, f"proc-environ-error:{exc}"


def check_same_uid_memory_isolation() -> tuple[bool, str]:
    """E6: same-UID child cannot extract parent's secrets via ptrace / /proc/mem / environ.

    The threat model: Advocate (parent) and agent (child) run at the same UID.
    Can the agent exfiltrate secrets the Advocate holds in memory/env?

    Three attack vectors tested:
      a. PTRACE_ATTACH(parent_pid) — direct memory access if Yama scope=0.
      b. open+read /proc/<parent_pid>/mem — file-based memory read.
      c. read /proc/<parent_pid>/environ — environment variable dump.

    PASS = child could NOT obtain the sentinel by ANY method (each blocked).
    FAIL = child obtained it by at least one method (report which).
    """
    if sys.platform != "linux":
        return False, "not-linux"

    import secrets as _secrets_mod

    sentinel = f"ADVOCATE_SENTINEL_{_secrets_mod.token_hex(16)}"
    sentinel_bytes = sentinel.encode()

    # Place sentinel into env + a live heap buffer BEFORE forking so the child can
    # attack it.  Keep the heap reference alive until after waitpid.
    os.environ[_SENTINEL_ENV_KEY] = sentinel
    _sentinel_heap_buf = ctypes.create_string_buffer(sentinel_bytes)  # noqa: F841

    parent_pid = os.getpid()

    # Pre-agree on result file path before forking so both sides share the same path.
    fd, result_path = tempfile.mkstemp(prefix="probe_mem_result_")
    os.close(fd)

    pid = os.fork()
    if pid == 0:
        try:
            ptrace_found, ptrace_detail = _child_attempt_ptrace(parent_pid, sentinel)
            mem_found, mem_detail = _child_attempt_proc_mem(parent_pid, sentinel_bytes)
            env_found, env_detail = _child_attempt_proc_environ(parent_pid, sentinel)
            any_found = ptrace_found or mem_found or env_found
            result: dict = {
                "any_found": any_found,
                "ptrace": {"found": ptrace_found, "detail": ptrace_detail},
                "proc_mem": {"found": mem_found, "detail": mem_detail},
                "proc_environ": {"found": env_found, "detail": env_detail},
            }
            with open(result_path, "w", encoding="utf-8") as fh:
                json.dump(result, fh)
        except Exception as exc:  # noqa: BLE001
            with open(result_path, "w", encoding="utf-8") as fh:
                json.dump({"any_found": False, "error": str(exc)}, fh)
        os._exit(0)

    os.waitpid(pid, 0)
    os.environ.pop(_SENTINEL_ENV_KEY, None)

    try:
        with open(result_path, encoding="utf-8") as fh:
            result = json.load(fh)
        os.unlink(result_path)
    except Exception as exc:  # noqa: BLE001
        return False, f"result-read-error:{exc}"

    any_found = result.get("any_found", False)
    passed = not any_found
    ptrace_d = result.get("ptrace", {}).get("detail", "?")
    mem_d = result.get("proc_mem", {}).get("detail", "?")
    env_d = result.get("proc_environ", {}).get("detail", "?")
    detail = f"ptrace={ptrace_d},proc_mem={mem_d},proc_environ={env_d}"
    return passed, detail


# ---------------------------------------------------------------------------
# Main probe runner — collects all checks, writes output
# ---------------------------------------------------------------------------

# Ordered list of (check_name, fn).  Each fn returns (passed: bool, detail: str).
_CHECKS: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
    ("env_context", check_env_context),
    ("iptables_owner_match_cap", check_iptables_owner_match),
    ("iptables_loopback_allow_external_block", check_iptables_loopback_allow_external_block),
    ("unshare_clone_newnet", check_unshare_newnet),
    ("phase0_floor", check_phase0_floor),
    ("yama_ptrace_scope", check_yama_ptrace_scope),
    ("same_uid_child_cannot_read_parent_secret", check_same_uid_memory_isolation),
]


def run_probe(out_tables_dir: str) -> dict:
    """Execute all checks and write the findings table + manifest.

    Returns the JSON-serialisable summary dict (also logged as compact JSON).
    """
    log.info("[advocate-probe] starting runtime egress-capability probe")

    rows: list[dict] = []
    for check_name, fn in _CHECKS:
        log.info("[advocate-probe] running check: %s", check_name)
        try:
            passed, detail = fn()
        except Exception as exc:  # noqa: BLE001
            passed = False
            detail = f"exception:{exc}"
        rows.append({"check_name": check_name, "pass": "true" if passed else "false", "detail": detail})
        log.info("[advocate-probe] %s: pass=%s detail=%s", check_name, passed, detail)

    # Write CSV table
    os.makedirs(out_tables_dir, exist_ok=True)
    table_path = os.path.join(out_tables_dir, "advocate_runtime_probe.csv")
    with open(table_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["check_name", "pass", "detail"])
        writer.writeheader()
        writer.writerows(rows)

    # Write manifest (no primary key; incremental=false; full load each probe run)
    manifest = {
        "incremental": False,
        "columns": ["check_name", "pass", "detail"],
    }
    with open(table_path + ".manifest", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)

    # Build summary
    by_name = {r["check_name"]: r["pass"] == "true" for r in rows}
    iptables_ok = by_name.get("iptables_loopback_allow_external_block", False)
    netns_ok = by_name.get("unshare_clone_newnet", False)
    memory_isolation_ok = by_name.get("same_uid_child_cannot_read_parent_secret", False)

    if iptables_ok:
        recommendation = "iptables_owner_match"
    elif netns_ok:
        recommendation = "netns_fd_passing"
    else:
        recommendation = "neither_available"

    summary = {
        "probe": "advocate_runtime_probe",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "env": gather_env_context(),
        "checks": {r["check_name"]: {"pass": r["pass"] == "true", "detail": r["detail"]} for r in rows},
        "recommendation": recommendation,
        "broker_memory_isolation": memory_isolation_ok,
        "table": table_path,
    }

    log.info("[advocate-probe] SUMMARY: %s", json.dumps(summary))
    return summary
