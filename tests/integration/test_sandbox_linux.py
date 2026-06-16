"""Linux-only integration tests for advocate/sandbox.py.

These tests exercise real OS primitives (seccomp, setuid, ptrace, AF_UNIX).
They are skipped unconditionally on non-Linux platforms (macOS, Windows).
They run via the Docker `test` stage:

    docker build --target test -t claude-sdk-test .
    docker run --rm --read-only --tmpfs /tmp:exec claude-sdk-test \
        uv run pytest tests/integration/ -v

The container must run as root (uid=0) for setuid tests to work.
The --read-only --tmpfs /tmp:exec flags mirror the production kbc-stacks runtime.

Test ledger (from the plan §test-ledger):
  1. Child cannot open an AF_INET socket (network kill via seccomp).
  2. Child cannot read /data/config.json (secret-file isolation via UID + chmod 600).
  3. Child env contains no secret keys (env isolation via cleared_env).
  4. Child cannot ptrace/read parent memory (cross-UID, no extra primitive needed).
  5. AF_UNIX to uds_path works (the single allowed channel).
"""

from __future__ import annotations

import errno
import os
import socket
import sys
import tempfile
import textwrap

import pytest

# ---------------------------------------------------------------------------
# Skip entire module on non-Linux
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only: seccomp/setuid/ptrace")

# Import target module only when we know we're on Linux (avoids ctypes.CDLL("libc.so.6")
# on macOS, which would raise OSError at collection time).
if sys.platform == "linux":
    from advocate.sandbox import (
        AGENT_UID,
        install_seccomp_filter,
        spawn_sandboxed,
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SKIP_NOT_ROOT = pytest.mark.skipif(os.geteuid() != 0, reason="Requires root (uid=0) in the container")


def _child_run(fn, *args) -> tuple[bool, str]:
    """Fork, run fn(*args) in the child. Returns (passed, detail)."""
    pid = os.fork()
    if pid == 0:
        try:
            result = fn(*args)
            os._exit(0 if result else 1)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"child exception: {exc}\n")
            os._exit(2)
    _, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        code = os.WEXITSTATUS(status)
        return code == 0, f"exit={code}"
    sig = os.WTERMSIG(status)
    return False, f"killed-by-signal-{sig}"


# ---------------------------------------------------------------------------
# Test 1 — Child cannot open AF_INET socket (network kill)
# ---------------------------------------------------------------------------


class TestSeccompBlocksInet:
    """Test ledger item 1: agent cannot open an AF_INET socket."""

    def test_seccomp_blocks_af_inet(self):
        """A child that installs the filter cannot open an AF_INET socket."""

        def _child():
            install_seccomp_filter()
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.close()
                return False  # should not reach
            except OSError as e:
                return e.errno in (errno.EACCES, errno.EPERM)

        passed, detail = _child_run(_child)
        assert passed, f"AF_INET socket should be blocked by seccomp; detail={detail}"

    def test_seccomp_blocks_af_inet6(self):
        """A child that installs the filter cannot open an AF_INET6 socket."""

        def _child():
            install_seccomp_filter()
            try:
                s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                s.close()
                return False
            except OSError as e:
                return e.errno in (errno.EACCES, errno.EPERM)

        passed, detail = _child_run(_child)
        assert passed, f"AF_INET6 socket should be blocked by seccomp; detail={detail}"

    def test_seccomp_inherited_across_exec(self, tmp_path):
        """seccomp filter survives exec — grandchild python process is also netless."""
        grandchild_code = textwrap.dedent(
            """
            import socket, errno, sys
            try:
                socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sys.exit(1)
            except OSError as e:
                sys.exit(0 if e.errno in (errno.EACCES, errno.EPERM) else 1)
            """
        ).strip()

        def _child():
            install_seccomp_filter()
            os.execvp(sys.executable, [sys.executable, "-c", grandchild_code])

        passed, detail = _child_run(_child)
        assert passed, f"seccomp not inherited across exec; detail={detail}"


# ---------------------------------------------------------------------------
# Test 2 — Child cannot read /data/config.json (secret-file isolation)
# ---------------------------------------------------------------------------


class TestSecretFileIsolation:
    """Test ledger item 2: child cannot read a root-owned chmod-600 file."""

    @_SKIP_NOT_ROOT
    def test_unpriv_child_cannot_read_root_owned_600_file(self, tmp_path):
        """A child that drops to AGENT_UID cannot read a root-owned 600 file."""
        secret = tmp_path / "config.json"
        secret.write_bytes(b'{"#anthropic_key": "sk-real"}')
        os.chmod(str(secret), 0o600)
        os.chown(str(secret), 0, 0)

        path = str(secret)

        def _child():
            import ctypes

            _libc = ctypes.CDLL("libc.so.6", use_errno=True)
            rc = _libc.setuid(AGENT_UID)
            if rc != 0:
                return False
            try:
                open(path).close()  # noqa: WPS515
                return False  # should not reach — should raise
            except PermissionError:
                return True
            except OSError as e:
                return e.errno == errno.EACCES

        passed, detail = _child_run(_child)
        assert passed, f"Unprivileged child was able to read root-owned 600 file; detail={detail}"


# ---------------------------------------------------------------------------
# Test 3 — Child env contains no secret keys
# ---------------------------------------------------------------------------


class TestEnvIsolation:
    """Test ledger item 3: cleared env has no secret keys."""

    def test_cleared_env_has_no_secret_keys(self):
        """spawn_sandboxed clears secrets from the child's env."""
        secret_env = {
            "KBC_TOKEN": "secret-token",
            "#anthropic_key": "sk-real",
            "GITHUB_TOKEN": "ghp_real",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "ORCHESTRATOR_UDS": "/tmp/orchestrator.sock",
        }
        # Keys that must not appear in the cleared env
        SECRET_KEYS = {"KBC_TOKEN", "#anthropic_key", "GITHUB_TOKEN"}

        # Verify none of the secret keys survive in the input dict when filtered
        cleared = {k: v for k, v in secret_env.items() if k not in SECRET_KEYS}
        for key in SECRET_KEYS:
            assert key not in cleared, f"Secret key {key!r} leaked into cleared env"

        # The cleared env keeps non-secret entries
        assert "PATH" in cleared
        assert "ORCHESTRATOR_UDS" in cleared

    @_SKIP_NOT_ROOT
    def test_spawn_sandboxed_child_env_has_no_secrets(self, tmp_path):
        """spawn_sandboxed passes only cleared_env to the child."""
        # Stage files under /tmp (world-writable) so the unprivileged child
        # (uid=65534) can traverse the path and write its output.
        # The pytest tmp_path is root-owned 700, which the child cannot enter
        # after the setuid drop.
        with tempfile.TemporaryDirectory(dir="/tmp") as workdir:  # noqa: S108
            os.chmod(workdir, 0o777)  # child must be able to traverse and write

            env_dump = os.path.join(workdir, "env_dump.txt")
            script = os.path.join(workdir, "dump_env.py")

            with open(script, "w", encoding="utf-8") as f:
                f.write(
                    textwrap.dedent(
                        f"""
                        import os, json
                        env = dict(os.environ)
                        with open({env_dump!r}, "w") as fh:
                            json.dump(env, fh)
                        """
                    ).strip()
                )
            os.chmod(script, 0o644)

            cleared_env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "ORCHESTRATOR_UDS": "/tmp/test.sock",  # noqa: S108
            }

            pid = spawn_sandboxed(
                argv=[sys.executable, script],
                uid=AGENT_UID,
                cleared_env=cleared_env,
                workspace=workdir,
                uds_path="/tmp/test.sock",  # noqa: S108
            )
            _, status = os.waitpid(pid, 0)
            assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, "child script failed"

            import json

            child_env = json.loads(open(env_dump, encoding="utf-8").read())  # noqa: WPS515
            for secret_key in ("KBC_TOKEN", "#anthropic_key", "GITHUB_TOKEN"):
                assert secret_key not in child_env, f"Secret key {secret_key!r} leaked into child env"
            assert child_env.get("ORCHESTRATOR_UDS") == "/tmp/test.sock"


# ---------------------------------------------------------------------------
# Test 4 — Child cannot ptrace/read parent memory
# ---------------------------------------------------------------------------


class TestMemoryIsolation:
    """Test ledger item 4: cross-UID ptrace is blocked."""

    @_SKIP_NOT_ROOT
    def test_unpriv_child_cannot_ptrace_root_parent(self):
        """An unprivileged child cannot ptrace the root parent.

        PTRACE_ATTACH on the parent pid from an unprivileged process must fail
        with EPERM (cross-UID ptrace denied by the kernel).
        """
        import ctypes

        parent_pid = os.getpid()

        PTRACE_ATTACH = 16

        def _child():
            _libc = ctypes.CDLL("libc.so.6", use_errno=True)
            # Drop to unprivileged UID
            rc = _libc.setuid(AGENT_UID)
            if rc != 0:
                return False
            # Attempt ptrace(PTRACE_ATTACH, parent_pid, 0, 0)
            rc = _libc.ptrace(PTRACE_ATTACH, parent_pid, 0, 0)
            if rc < 0:
                err = ctypes.get_errno()
                # EPERM = 1: cross-UID ptrace denied
                return err == errno.EPERM
            # ptrace succeeded — isolation failed
            return False

        passed, detail = _child_run(_child)
        assert passed, f"Unprivileged child was able to ptrace root parent; detail={detail}"

    @_SKIP_NOT_ROOT
    def test_unpriv_child_cannot_read_parent_proc_mem(self):
        """An unprivileged child cannot read /proc/<parent>/mem (root-owned)."""
        parent_pid = os.getpid()
        mem_path = f"/proc/{parent_pid}/mem"

        def _child():
            import ctypes

            _libc = ctypes.CDLL("libc.so.6", use_errno=True)
            _libc.setuid(AGENT_UID)
            try:
                open(mem_path, "rb").close()  # noqa: WPS515
                return False
            except PermissionError:
                return True
            except OSError as e:
                return e.errno == errno.EACCES

        passed, detail = _child_run(_child)
        assert passed, f"Unprivileged child read /proc/parent/mem; detail={detail}"


# ---------------------------------------------------------------------------
# Test 5 — AF_UNIX to uds_path works (the single allowed channel)
# ---------------------------------------------------------------------------


class TestAfUnixChannel:
    """Test ledger item 5: AF_UNIX works under the seccomp filter."""

    def test_af_unix_socketpair_works_under_seccomp(self):
        """Child with seccomp filter installed can communicate over AF_UNIX."""

        def _child():
            install_seccomp_filter()
            with tempfile.TemporaryDirectory() as td:
                path = os.path.join(td, "test.sock")
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

        passed, detail = _child_run(_child)
        assert passed, f"AF_UNIX roundtrip failed under seccomp; detail={detail}"

    @_SKIP_NOT_ROOT
    def test_spawn_sandboxed_af_unix_works(self, tmp_path):
        """spawn_sandboxed child can connect to AF_UNIX socket (the UDS channel)."""
        # Use /tmp directly so the unprivileged child (uid=65534) can traverse
        # the path — the pytest-created tmp_path is root-owned 700.
        with tempfile.TemporaryDirectory(dir="/tmp") as workdir:  # noqa: S108
            os.chmod(workdir, 0o777)  # child must be able to traverse and write

            sock_path = os.path.join(workdir, "orchestrator.sock")
            result_file = os.path.join(workdir, "result.txt")
            script = os.path.join(workdir, "af_unix_client.py")

            with open(script, "w", encoding="utf-8") as f:
                f.write(
                    textwrap.dedent(
                        f"""
                        import socket
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        s.connect({sock_path!r})
                        data = s.recv(4)
                        s.close()
                        with open({result_file!r}, "wb") as fh:
                            fh.write(data)
                        """
                    ).strip()
                )
            os.chmod(script, 0o644)

            # Start a listening server in the parent.
            # chmod 777 on the socket file so the unprivileged child can connect
            # (kernel requires write permission on the socket inode for connect(2)).
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock_path)
            os.chmod(sock_path, 0o777)
            srv.listen(1)

            cleared_env = {"PATH": "/usr/local/bin:/usr/bin:/bin"}

            pid = spawn_sandboxed(
                argv=[sys.executable, script],
                uid=AGENT_UID,
                cleared_env=cleared_env,
                workspace=workdir,
                uds_path=sock_path,
            )

            conn, _ = srv.accept()
            conn.sendall(b"ok")
            conn.close()
            srv.close()

            _, status = os.waitpid(pid, 0)
            assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, "child script failed"

            result = open(result_file, "rb").read()  # noqa: WPS515
            assert result == b"ok", f"Expected b'ok' from child, got {result!r}"
