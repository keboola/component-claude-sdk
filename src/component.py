"""keboola.app-claude-sdk — Claude Agent SDK runner.

A highly configurable Claude Agent SDK runner inside Keboola. ``run()`` is a thin
orchestrator (spec §6.1) delegating to private methods; the SDK boundary lives in
``ClaudeRunner`` and the runtime SDK overlay (``SdkVersionManager``) runs first so
any overlay is on ``sys.path`` before a single ``claude_agent_sdk`` symbol is used.

Boot sequence (Broker V0, spec §6):

0.  Env-scrub re-exec — KBC_TOKEN removed from the Advocate exec-time environ so
    /proc/<advocate>/environ does not expose it; the value is passed back via an
    inherited pipe fd (held in memory only). At boot it is set transiently so the
    keboola base class captures it at construction, then PURGED from os.environ
    before the agent spawns (the SDK transport merges os.environ into the agent's
    env, so a lingering KBC_TOKEN would otherwise leak directly — HIGH-1).
1.  Assert ptrace_scope >= 1 — all in-memory secret protection rests on it; fail
    closed otherwise (HIGH-2).
2.  Read config.json → Configuration.
3.  Scrub /data/config.json in place — agent cannot read decrypted secrets.
4.  Derive + sign the Intent Contract (Phase 0).
5.  Start AdvocateServer on loopback TCP 127.0.0.1:<port>.
6.  Build CLEARED agent env: ANTHROPIC_BASE_URL=loopback, dummy ANTHROPIC_API_KEY,
    writable /tmp caches, explicit blanks for KBC_TOKEN/GITHUB_TOKEN/GH_TOKEN —
    NO real key, NO KBC_TOKEN, NO github_token.
7.  Run tasks via ClaudeRunner (SDK spawns the CLI with the cleared env).
8.  Parent promotes outputs, tears down server (always, via finally).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys

from keboola.component.base import ComponentBase, sync_action
from keboola.component.exceptions import UserException
from keboola.vcr import DefaultSanitizer

from claude_runner import ClaudeRunner, ClaudeRunResult
from configuration import Configuration
from output_writer import OutputWriter
from plugin_manager import PluginManager, PluginResult
from sdk_version_manager import SdkVersionManager
from sync_actions import check_anthropic_connection, list_github_repos
from tasks import Task, TaskSource
from transcript_writer import TranscriptWriter

# VCR cassette sanitizers — picked up automatically by the datadirtest scaffolder
# during recording (spec §7). The ONLY in-process HTTP we record is the
# testConnection Anthropic Messages ping, whose key rides in the ``x-api-key``
# header. DefaultSanitizer already strips every header except a small safe set
# (content-type/length/accept), so ``x-api-key``/``authorization`` never reach
# the cassette; we additionally list the key field names so any stray copy in a
# URL or body is redacted too. NO secret value must ever land in a cassette.
VCR_SANITIZERS = [
    DefaultSanitizer(
        additional_sensitive_fields=[
            "x-api-key",
            "anthropic_key",
            "#anthropic_key",
            "api_key",
            "github_token",
            "#github_token",
        ],
    ),
]

WORKSPACE_DIR = "/tmp/claude-workspace"  # noqa: S108 — /tmp is the only writable path in the read-only image

# The image root is read-only at runtime; the agent-runtime launchers (uvx for
# Python MCP servers, npx for Node MCP servers) default their caches/HOME to the
# read-only filesystem and die before the MCP server starts. Point them at the
# writable /tmp so configured stdio MCP servers actually launch (Finding 5).
AGENT_HOME = "/tmp/agent-home"  # noqa: S108 — only /tmp is writable in the read-only image
UV_CACHE_DIR = "/tmp/uv-cache"  # noqa: S108
NPM_CONFIG_CACHE = "/tmp/npm-cache"  # noqa: S108
XDG_CACHE_HOME = "/tmp/xdg-cache"  # noqa: S108

# Dummy API key placed in the CLEARED agent env.  The agent never sees the real
# key; the AdvocateServer proxy strips this and injects the real key server-side.
_DUMMY_ANTHROPIC_KEY = "dummy-advocate-key"  # noqa: S105 — intentionally fake/not a real credential

# Guard env-var used by the env-scrub re-exec path to avoid an infinite loop.
# When present (= "1"), the entry-point knows a scrub has already happened and
# skips re-execing again.
_SCRUB_DONE_ENV = "_ADVOCATE_ENV_SCRUB_DONE"

# Pipe fd number reserved for passing KBC_TOKEN back after env-scrub.
# We always use fd 3 (first non-standard fd above stderr=2).
_KBC_TOKEN_PIPE_FD = 3

# ptrace_scope gate (HIGH-2). All in-memory secret protection in the Advocate
# rests on the kernel restricting same-UID PTRACE_ATTACH (Yama ptrace_scope >= 1).
# With ptrace_scope=0 the agent can attach to the Advocate and read every secret,
# so the broker provides no protection — we fail closed.
_PTRACE_SCOPE_PATH = "/proc/sys/kernel/yama/ptrace_scope"
# Dev/test escape hatch (NEVER set on the real runtime): proceed despite
# ptrace_scope=0. Used by the test suite (see tests/conftest.py) where no real
# same-UID agent attaches to a live Advocate.
_PTRACE_OVERRIDE_ENV = "ADVOCATE_ALLOW_UNSAFE_PTRACE"

# Container env vars that must never reach the agent. The SDK transport merges
# os.environ into the agent subprocess env, so the cleared env must EXPLICITLY
# blank these (an override of "" wins over any inherited value) — defense in
# depth alongside the KBC_TOKEN purge (HIGH-1 / MED-1).
_AGENT_ENV_BLANKS = ("KBC_TOKEN", "KBC_URL", "GITHUB_TOKEN", "GH_TOKEN")


log = logging.getLogger(__name__)


def _read_ptrace_scope() -> int | None:
    """Return the kernel ptrace_scope value, or ``None`` if it cannot be read.

    ``None`` means non-Linux / no Yama LSM (e.g. local macOS dev) — the caller
    treats that as "cannot verify" and warns rather than failing.
    """
    try:
        with open(_PTRACE_SCOPE_PATH, encoding="utf-8") as fh:
            return int(fh.read().strip())
    # fmt: skip — ruff format (requires-python="~=3.14.0") has a known bug that
    # rewrites this into the bare-comma PEP 758 form (`except OSError, ValueError:`),
    # which is a SyntaxError on <3.14 and reads like the old Python 2 multi-except
    # syntax (astral-sh/ruff#23090). See advocate/contract.py for the same pattern.
    except (OSError, ValueError):  # fmt: skip
        return None


def _assert_ptrace_protected() -> None:
    """Fail closed unless same-UID PTRACE_ATTACH is restricted (HIGH-2).

    Called at the start of the agent run, before any secret is used to do
    privileged I/O. If ptrace_scope is 0 the Advocate's in-memory secrets are
    readable by the same-UID agent, so the broker offers no protection — refuse
    to run (overridable only via the documented dev/test env var).
    """
    scope = _read_ptrace_scope()
    if scope is None:
        log.warning(
            "ptrace_scope unreadable at %s — cannot verify same-UID memory protection "
            "(expected on non-Linux/dev; the production runtime enforces ptrace_scope=1).",
            _PTRACE_SCOPE_PATH,
        )
        return
    if scope >= 1:
        log.info("ptrace_scope=%d — same-UID PTRACE_ATTACH restricted; Advocate memory protected.", scope)
        return
    if os.environ.get(_PTRACE_OVERRIDE_ENV) == "1":
        log.warning(
            "ptrace_scope=0 but %s=1 — proceeding UNSAFELY (dev/test override). Same-UID "
            "PTRACE_ATTACH is NOT restricted; broker memory is readable by the agent.",
            _PTRACE_OVERRIDE_ENV,
        )
        return
    raise UserException(
        "ptrace_scope=0: a same-UID process can PTRACE_ATTACH the Advocate and read every "
        "decrypted secret, so the broker provides no protection. Refusing to run. The Keboola "
        f"runtime sets ptrace_scope=1; if this fired on the platform, escalate. (Dev/test only: "
        f"set {_PTRACE_OVERRIDE_ENV}=1 to override.)"
    )


def _scrub_config_json_impl(kbc_datadir: str) -> None:
    """Overwrite ``<kbc_datadir>/config.json`` with a secrets-scrubbed copy.

    Any key whose name starts with ``#`` has its value replaced with ``""``
    (preserving the key so downstream validation that checks for its presence
    still passes, but removing the decrypted value from disk).  Applied
    recursively through nested dicts and lists.  All structural config
    (storage settings, non-``#`` parameters) is left intact.

    ASSUMPTION: this relies on the Keboola config schema convention that every
    encrypted secret is stored under a ``#``-prefixed key.  A secret stored under
    a plain key (no ``#`` prefix) would NOT be scrubbed by this function.

    The write is atomic: the scrubbed content is written to a temp file in the
    same directory and then renamed over the original with ``os.replace``.  This
    prevents a truncated/corrupt config.json if the process crashes mid-write
    (the keboola.component base class re-reads config.json lazily throughout the
    run, so a corrupt file would break output promotion).

    Non-fatal if the file is absent or unreadable (logs at DEBUG/WARNING).

    This is extracted as a module-level function so tests can call it directly
    without standing up a full Component instance.
    """
    import json  # keep this import here (not top-level) to match component style
    import tempfile

    config_path = os.path.join(kbc_datadir, "config.json")

    def _scrub_obj(obj: object) -> object:
        """Recursively replace #-key values with empty strings."""
        if isinstance(obj, dict):
            return {k: ("" if isinstance(k, str) and k.startswith("#") else _scrub_obj(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_scrub_obj(item) for item in obj]
        return obj

    try:
        with open(config_path, encoding="utf-8") as fh:
            data = json.load(fh)
        scrubbed = _scrub_obj(data)
        # Write atomically: temp file in the same dir, then os.replace (POSIX rename).
        # Keeps the same filesystem so rename is guaranteed atomic; avoids a torn
        # write leaving keboola.component with a truncated config.json.
        tmp_fd, tmp_path = tempfile.mkstemp(dir=kbc_datadir, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(scrubbed, fh)
            os.replace(tmp_path, config_path)
        except Exception:
            # Clean up the temp file if anything goes wrong before the rename.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        log.debug("Scrubbed %s — decrypted #-secret values removed from disk", config_path)
    except FileNotFoundError:
        log.debug("config.json not found at %s — nothing to scrub", config_path)
    except OSError as exc:
        log.warning("Could not scrub config.json at %s: %s", config_path, exc)


def _perform_env_scrub() -> None:
    """Re-exec the current process with KBC_TOKEN stripped from the environment.

    /proc/<advocate>/environ is readable by the same-uid agent (ptrace_scope=1
    does not gate /proc/<pid>/environ — confirmed on-platform, spec §5.1 pt3).
    If KBC_TOKEN stays in the exec-time env it leaks to the agent.

    Mitigation:
    1. Open a pipe; write KBC_TOKEN to the write end.
    2. Re-exec sys.argv with a scrubbed env (KBC_TOKEN removed, _SCRUB_DONE_ENV=1)
       but with the pipe's READ fd inherited (fd 3).
    3. After re-exec the caller reads KBC_TOKEN back from fd 3 and holds it
       in memory only (never in /proc/<advocate>/environ again).

    This function should be called ONCE at process start, before anything else,
    when _SCRUB_DONE_ENV is not set.  The re-exec'd process skips this function
    (guard marker is set) and reads KBC_TOKEN from fd 3.
    """
    # Fast path: if KBC_TOKEN is not in the env there is nothing to scrub and no
    # need to pay the cost of a full process re-exec.
    if "KBC_TOKEN" not in os.environ:
        return

    token = os.environ.get("KBC_TOKEN", "")

    # Build a pipe: read_fd will be inherited across exec, write_fd closed after write.
    read_fd, write_fd = os.pipe()

    # Write the token to the pipe (non-blocking; token is small).
    try:
        os.write(write_fd, token.encode("utf-8"))
    finally:
        os.close(write_fd)

    # Build scrubbed environment: remove KBC_TOKEN, mark scrub done.
    scrubbed_env = {k: v for k, v in os.environ.items() if k != "KBC_TOKEN"}
    scrubbed_env[_SCRUB_DONE_ENV] = "1"

    # Ensure read_fd is at fd position _KBC_TOKEN_PIPE_FD (3).
    # dup2 reassigns without closing the target; close original if different.
    if read_fd != _KBC_TOKEN_PIPE_FD:
        os.dup2(read_fd, _KBC_TOKEN_PIPE_FD)
        os.close(read_fd)
    # else: already at fd 3; no dup needed

    # Prevent fd 3 from being auto-closed on exec (no CLOEXEC).
    # os.pipe() sets O_CLOEXEC on Linux by default; clear it so it survives exec.
    import fcntl

    flags = fcntl.fcntl(_KBC_TOKEN_PIPE_FD, fcntl.F_GETFD)
    fcntl.fcntl(_KBC_TOKEN_PIPE_FD, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)

    # Re-exec this process with the scrubbed env.  execv replaces the process image;
    # if it succeeds this line is never reached.  If it fails (exec error), we fall
    # through and continue without the scrub (graceful degradation).
    log.info("env-scrub: re-execing with KBC_TOKEN stripped from exec-time env")
    try:
        os.execve(sys.executable, [sys.executable] + sys.argv, scrubbed_env)
    except OSError as exc:
        # execve failed (rare: missing interpreter, permissions).  Clean up the pipe
        # fd and continue without scrub — the Advocate still runs but KBC_TOKEN
        # remains in /proc/self/environ.  Log loudly so this is not silent.
        os.close(_KBC_TOKEN_PIPE_FD)
        log.warning(
            "env-scrub: re-exec failed (%s); KBC_TOKEN remains in /proc/self/environ — "
            "pair with a least-privilege Storage token as interim mitigation (spec §5.1 pt3)",
            exc,
        )


def _read_kbc_token_from_pipe() -> str:
    """After a successful env-scrub re-exec, read KBC_TOKEN back from the inherited pipe.

    The re-exec'ing process wrote the token to the write end of the pipe and then
    closed it; after exec the write end is gone (was closed before exec), so
    reading until EOF returns the full token. A single ``os.read(fd, 4096)`` call
    is NOT sufficient — a token longer than 4096 bytes would be silently
    truncated (Finding 7), so we loop until an empty read signals EOF.

    Returns the token string, or "" if the fd is not open / empty.
    """
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(_KBC_TOKEN_PIPE_FD, 4096)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(_KBC_TOKEN_PIPE_FD)
        return b"".join(chunks).decode("utf-8")
    except OSError:
        # fd not open — either env-scrub was skipped or this is not the re-exec'd
        # process; fall back gracefully.
        return ""


class Component(ComponentBase):
    """Orchestrates a configured Claude agent run over Keboola data."""

    def __init__(self):
        super().__init__()
        self._sdk_manager = SdkVersionManager()
        self._plugin_manager = PluginManager()
        self._output_writer = OutputWriter(self)
        self._runner = ClaudeRunner(workspace_dir=WORKSPACE_DIR)

    def run(self) -> None:
        """Orchestrate the Broker V0 boot sequence, task loop, and output promotion.

        Boot sequence (spec §6):
        1.  Assert ptrace_scope >= 1 — fail closed if the runtime cannot protect
            the Advocate's in-memory secrets from a same-UID PTRACE_ATTACH (HIGH-2).
        2.  Read config.json → Configuration (secrets loaded into memory).
        3.  Scrub /data/config.json in place — same-uid agent cannot read secrets.
        4.  (Env-scrub handled at process entry — see module-level boot code.)
        5.  Derive + sign the Intent Contract (Phase 0, clean).
        6.  Start AdvocateServer on loopback TCP 127.0.0.1:<port>.
        7.  Build CLEARED agent env (no secrets; loopback routing).
        8.  Run tasks via ClaudeRunner.  Outputs promoted by parent.
        9.  Tear down server (always, via finally).

        The ``write_always`` transcript tables MUST be flushed even when a task
        loop or output promotion raises, so the per-task work and ``promote()``
        run inside a ``try`` whose ``finally`` always flushes the transcript
        before any exception propagates (output-state durability guarantee).
        """
        # Step 1: Fail closed unless the kernel protects Advocate memory (before
        # any secret is decrypted into memory below).
        _assert_ptrace_protected()

        # Step 2: Parse config (reads decrypted secrets into memory).
        config = Configuration(**self.configuration.parameters)
        log.info("Starting Claude SDK run: %s", config.log_safe_summary())
        self._warn_if_memory_intensive(config)

        # Step 2: Scrub /data/config.json — remove decrypted #-secret values.
        # The keboola.component base class re-reads config.json lazily throughout
        # the run (for output manifests etc.), so we CANNOT safely unlink it.
        # Instead we OVERWRITE it with a secrets-scrubbed copy that retains all
        # structural config (storage, tables, etc.) but replaces #-key values with
        # empty strings.  The agent is spawned later with the scrubbed file on disk;
        # the real values are held only in the ``config`` Python object (spec §5.1 pt1).
        self._scrub_config_json()

        # Steps 4–8 are handled by the main orchestration path.
        self._run_with_broker(config)

    def _run_with_broker(self, config: Configuration) -> None:
        """Run the full Broker V0 boot sequence: contract, server, tasks, teardown.

        Separated from ``run()`` so tests can call it directly with a config
        object (bypassing the config.json read/unlink), and to keep each step
        clearly delineated for review.
        """
        from advocate.contract import derive_contract, new_invocation_secret, sign_contract
        from advocate.server import AdvocateServer

        # Step 4: Derive + sign the Intent Contract (Phase 0 — clean data only).
        secret = new_invocation_secret()
        contract = derive_contract(config, operates_on=config.operates_on)
        envelope = sign_contract(contract, secret)

        # Step 5: Build the MCP configs dict for the server.
        mcp_configs = {server.name: server for server in config.mcp_servers}

        # HIGH-3: scope the GitHub broker's destination allowlist to the repos in
        # operates_on. ``Configuration`` already requires ``operates_on`` when
        # github_enabled (fail-closed), so this is a concrete path when GitHub is
        # in play; otherwise deny all GitHub paths (empty allowlist) — the broker
        # is unreachable anyway since the contract grants no gh.* capability.
        github_allowed_destinations = self._build_github_allowed_destinations(config)

        # Start the loopback-TCP AdvocateServer. ``start()`` lives inside the same
        # try/finally that owns ``stop()`` (Finding 5) — if any setup below
        # (SDK overlay, plugin install, transcript build) raises, the server (and
        # its MCP subprocesses) must still be torn down rather than leaked.
        server = AdvocateServer(
            config.anthropic_key,
            mcp_configs=mcp_configs,
            github_token=config.github_token,
            github_allowed_destinations=github_allowed_destinations,
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        transcript = None
        results: list[ClaudeRunResult] = []
        try:
            server.start()
            port = server.port
            log.info("AdvocateServer running on 127.0.0.1:%d", port)

            sdk_version, plugin_result, cleared_env = self._ensure_sdk_and_env(config, port)
            transcript = self._build_transcript(config, sdk_version, plugin_result.resolved)
            tasks = TaskSource(config).load(self.get_input_tables_definitions())

            for task in tasks:
                results.append(self._run_one_task(task, config, plugin_result.sdk_plugins, cleared_env, transcript))
            self._output_writer.promote(default_incremental=config.output.default_incremental)
        finally:
            if transcript is not None:
                transcript.flush()
            server.stop()

        self._report_outcome(results)

    def _ensure_sdk_and_env(self, config: Configuration, proxy_port: int) -> tuple[str, PluginResult, dict[str, str]]:
        """Resolve SDK version, install plugins (Advocate side), build cleared env.

        Plugin install happens HERE — on the Advocate side — while secrets are
        still available (github_token for private plugins).  The cleared env passed
        to the agent carries ONLY routing values, no raw secrets (spec §14).
        """
        sdk_version = self._sdk_manager.ensure(config.sdk_version, config.sdk_version_on_failure.value)
        cleared_env = self._build_cleared_env(config, proxy_port)
        for cache_dir in (AGENT_HOME, UV_CACHE_DIR, NPM_CONFIG_CACHE, XDG_CACHE_HOME):
            os.makedirs(cache_dir, exist_ok=True)
        # Plugin install env has access to github_token (private plugin auth)
        # but is built from the minimal _plugin_install_env helper (§14 fix).
        plugin_result = self._plugin_manager.prepare(
            config.plugins, cleared_env, github_token=config.github_token, secret_values=self._secret_values(config)
        )
        os.makedirs(WORKSPACE_DIR, exist_ok=True)
        self._output_writer.ensure_dir()
        if config.workspace_input_files:
            self._stage_input_files()
        return sdk_version, plugin_result, cleared_env

    @staticmethod
    def _warn_if_memory_intensive(config: Configuration) -> None:
        """Warn (non-blocking) that MCP servers / plugins may need a bigger backend.

        MCP servers and plugins spawn subprocesses; the default ``small`` backend
        gives the container only ~256 MB, which OOM-kills those workloads (Finding
        6, verified on-platform). Lightweight setups may still fit, so this is a
        logged WARNING, not a hard error — it preserves flexibility while pointing
        users at the only working memory lever, ``runtime.backend.type``.
        """
        if config.mcp_servers or config.plugins:
            logging.warning(
                "MCP servers and/or plugins are configured; they spawn subprocesses and may exceed "
                "the default backend's memory. If you hit an out-of-memory error, set "
                "'runtime.backend.type: medium' (or larger) in the configuration."
            )

    @staticmethod
    def _secret_values(config: Configuration) -> list[str]:
        """All secret strings to scrub from any captured output (defense-in-depth)."""
        secrets = [config.anthropic_key, config.github_token]
        for server in config.mcp_servers:
            secrets.extend(getattr(server, "env", {}).values())
            secrets.extend(getattr(server, "headers", {}).values())
        return [s for s in secrets if s]

    @staticmethod
    def _build_github_allowed_destinations(config: Configuration) -> list[str]:
        """Path-prefix allowlist for the GitHub broker, one entry per operates_on repo.

        An "org/*" entry becomes the org-only prefix "/repos/org" — the broker's
        existing child-path matching (github_broker._path_allowed) already scopes
        that to every repo under the org without further narrowing here.
        """
        from advocate.contract import operates_on_to_repo_path  # noqa: PLC0415

        if not (config.github_enabled and config.operates_on):
            return []
        return [f"/repos/{operates_on_to_repo_path(entry)}" for entry in config.operates_on]

    def _stage_input_files(self) -> None:
        """Copy /data/in/files/ into the agent workspace so the agent can read them.

        Paired with ``add_dirs=[workspace]`` on ClaudeAgentOptions (spec §5.1), this
        makes user-uploaded files available to the agent. No-op when there are none.
        """
        in_files = self.files_in_path
        if not os.path.isdir(in_files):
            return
        staged = 0
        for name in os.listdir(in_files):
            src = os.path.join(in_files, name)
            if not os.path.isfile(src) or name.endswith(".manifest"):
                continue
            shutil.copy2(src, os.path.join(WORKSPACE_DIR, name))
            staged += 1
        if staged:
            log.info("Staged %d input file(s) into the agent workspace.", staged)

    @staticmethod
    def _scrub_config_json() -> None:
        """Overwrite /data/config.json with a secrets-scrubbed copy.

        The Keboola platform decrypts ``#``-prefixed secret fields into
        config.json before the container starts.  We MUST remove the plaintext
        secret values before spawning the agent subprocess, so the same-uid agent
        cannot read them from disk.

        We CANNOT simply unlink the file because the ``keboola.component`` base
        class (``ComponentBase.configuration``) re-reads config.json lazily
        throughout the run — for output manifests, state writes, etc. — and will
        raise ``FileNotFoundError`` if the file is gone.

        Instead we OVERWRITE with a scrubbed copy: any key whose name starts with
        ``#`` has its value replaced with an empty string (preserving the key
        presence so validation that checks for the key's existence still passes,
        but removing the decrypted value from disk).  All structural config
        (storage, tables, parameters without ``#``) is preserved unchanged.

        The real secret values remain only in the ``Configuration`` Python object
        held in the Advocate process memory (spec §5.1 pt1 / §6 step 3).
        """
        kbc_datadir = os.environ.get("KBC_DATADIR", "/data")
        _scrub_config_json_impl(kbc_datadir)

    @staticmethod
    def _build_cleared_env(config: Configuration, proxy_port: int) -> dict[str, str]:
        """Build the CLEARED agent subprocess env (spec §6 step 7).

        The agent env carries ONLY:
        - ANTHROPIC_BASE_URL pointing at the loopback proxy (not the real API).
        - A DUMMY ANTHROPIC_API_KEY — the real key is injected server-side.
        - Writable /tmp caches for uvx/npx MCP launchers (Finding 5).
        - CLAUDE_CODE_DISABLE_AUTO_MEMORY=1.
        - Best-effort MCP proxy env (TODO: confirmed routing TBD on-platform §8).

        What is EXPLICITLY absent / blanked:
        - KBC_TOKEN (env-scrubbed + purged from Advocate; blanked here too)
        - Real #anthropic_key (held by AdvocateServer; agent uses dummy)
        - GITHUB_TOKEN / GH_TOKEN (brokered server-side; blanked here)
        - Any MCP server secret env values (injected server-side)

        IMPORTANT: the SDK transport MERGES the Advocate's ``os.environ`` into the
        agent subprocess env (``{**os.environ, **options.env}``).  A cleared env
        therefore only *overrides* keys it sets — it does NOT drop inherited
        container vars.  So we explicitly blank the sensitive ones (``""`` wins
        over any inherited value) as defense-in-depth alongside the KBC_TOKEN
        purge at boot (HIGH-1 / MED-1).

        NOTE: MCP/GitHub CLI wiring via MCP_PROXY_URL / HTTPS_PROXY is MARKED for
        on-platform confirmation (spec §8).  The Anthropic path is the confirmed
        working channel for V0.
        """
        env: dict[str, str] = {
            # Loopback proxy — agent model calls land here; real key injected server-side.
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{proxy_port}",
            "ANTHROPIC_API_KEY": _DUMMY_ANTHROPIC_KEY,
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            # Writable HOME + caches for the read-only image (Finding 5).
            "HOME": AGENT_HOME,
            "UV_CACHE_DIR": UV_CACHE_DIR,
            "NPM_CONFIG_CACHE": NPM_CONFIG_CACHE,
            "XDG_CACHE_HOME": XDG_CACHE_HOME,
        }
        # Defense-in-depth: explicitly blank inherited container secrets so the
        # SDK's os.environ merge cannot pass them through to the agent.
        for key in _AGENT_ENV_BLANKS:
            env[key] = ""
        # MCP proxy wiring — best-effort; mechanism confirmed via binary probe but
        # end-to-end validation pending on-platform (spec §8 NOTE).
        # TODO: confirm MCP_PROXY_URL routing on-platform before GA.
        if config.mcp_servers:
            env["MCP_PROXY_URL"] = f"http://127.0.0.1:{proxy_port}/v1/mcp"

        # GitHub: no GITHUB_TOKEN / GH_TOKEN in the agent env.
        # GitHub API calls are brokered via /v1/github on the loopback server.
        # The github-shim / HTTPS_PROXY path (spec §8) needs on-platform validation.
        # TODO: confirm GitHub CLI routing on-platform.

        return env

    def _build_transcript(
        self, config: Configuration, sdk_version: str, plugins_resolved: dict[str, str]
    ) -> TranscriptWriter:
        return TranscriptWriter(
            component=self,
            files_out_path=self.files_out_path,
            sdk_version_resolved=sdk_version,
            plugins_resolved=plugins_resolved,
            secret_values=self._secret_values(config),
        )

    def _run_one_task(
        self,
        task: Task,
        config: Configuration,
        plugin_paths: list[dict[str, str]],
        env: dict[str, str],
        transcript: TranscriptWriter,
    ) -> ClaudeRunResult:
        """Run one task inside its own event loop, teeing messages to the transcript.

        A per-task failure is captured as a failed ``ClaudeRunResult`` rather than
        propagated. ``run_task`` deliberately maps caps/auth/process errors to a
        ``UserException``; if that escaped the batch loop it would abort before
        ``promote()`` and discard output tables already written by earlier
        successful tasks. Returning a failed result instead keeps the batch loop's
        behaviour identical whether a cap arrives as a ResultMessage (soft) or a
        raise (hard): the run continues, promote() runs, and ``_report_outcome``
        still exits 1 because a failed result is present.
        """
        log.info("Running task '%s'.", task.task_id)
        options = self._runner.build_options(task, config, plugin_paths, env)
        transcript.begin_task(task.task_id)
        try:
            result = asyncio.run(self._runner.run_task(task, options, on_message=transcript.on_message))
        except UserException as exc:
            log.warning("Task '%s' failed: %s", task.task_id, exc)
            result = ClaudeRunResult(task_id=task.task_id, success=False, error_message=str(exc))
        result.extra_args["model"] = task.model or config.model.value
        transcript.end_task(result)
        return result

    @staticmethod
    def _report_outcome(results: list[ClaudeRunResult]) -> None:
        """Decide the exit: any failed task -> UserException (exit 1)."""
        failed = [r for r in results if not r.success]
        if failed:
            details = "; ".join(f"{r.task_id}: {r.error_message or r.subtype}" for r in failed)
            raise UserException(f"{len(failed)} of {len(results)} task(s) failed: {details}")
        log.info("All %d task(s) completed successfully.", len(results))

    @sync_action("testConnection")
    def test_connection(self):
        """Validate #anthropic_key with a single cheap in-process API call."""
        config = Configuration(**self.configuration.parameters)
        return check_anthropic_connection(config.anthropic_key)

    @sync_action("load_github_repos")
    def load_github_repos(self):
        """Populate the Repositories multi-select with repos the token can access."""
        config = Configuration(**self.configuration.parameters)
        return list_github_repos(config.github_token)


def _boot() -> None:
    """Construct the component and run the selected action (Broker V0 boot).

    KBC_TOKEN lifecycle (HIGH-1): after the env-scrub re-exec the token is NOT in
    os.environ. We recover it from the inherited pipe and set it TRANSIENTLY so the
    keboola base class captures it into ``environment_variables.token`` at
    construction. We then PURGE it from os.environ before any action runs — the SDK
    transport merges os.environ into the agent subprocess env, so a lingering
    KBC_TOKEN would otherwise leak straight into the agent (and any child's /proc).
    The base class keeps its captured copy in memory; Storage I/O is unaffected.
    """
    if os.environ.get(_SCRUB_DONE_ENV) == "1":
        # Re-exec'd process: recover KBC_TOKEN from the inherited pipe fd and set
        # it transiently for the base-class capture below.
        recovered = _read_kbc_token_from_pipe()
        if recovered:
            os.environ["KBC_TOKEN"] = recovered

    try:
        comp = Component()
        # The base class has now captured KBC_TOKEN (if any). Purge it from
        # os.environ unconditionally so it cannot be inherited by the agent
        # subprocess. (In the rare env-scrub-failed path this still removes it
        # from the inherited env; the /proc stack copy is covered by the warning
        # logged at scrub time.)
        os.environ.pop("KBC_TOKEN", None)
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        sys.exit(1)
    except Exception as exc:
        logging.exception(exc)
        sys.exit(2)


# ---------------------------------------------------------------------------
# Broker V0 entry-point boot: env-scrub guard
# ---------------------------------------------------------------------------
# This runs when the module is executed as __main__ (the component entry point).
# It must run BEFORE any Keboola component infrastructure reads config or spawns
# anything.
#
# The guard ensures env-scrub happens exactly once:
# - First exec (no _SCRUB_DONE_ENV): _perform_env_scrub() re-execs the process
#   with KBC_TOKEN stripped.  execve() replaces the process image; the code
#   below the call never runs in the first instance.  On execve failure (or when
#   there was no KBC_TOKEN to scrub) it returns and _boot() runs normally.
# - Second exec (_SCRUB_DONE_ENV=1): _perform_env_scrub() is skipped and _boot()
#   recovers KBC_TOKEN from the inherited pipe (transient set + purge — HIGH-1).
if __name__ == "__main__":
    if os.environ.get(_SCRUB_DONE_ENV) != "1":
        _perform_env_scrub()  # re-execs on success; returns only on no-op/failure
    _boot()
