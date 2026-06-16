"""keboola.app-claude-sdk — Claude Agent SDK runner.

A highly configurable Claude Agent SDK runner inside Keboola. ``run()`` is a thin
orchestrator (spec §6.1) delegating to private methods; the SDK boundary lives in
``ClaudeRunner`` and the runtime SDK overlay (``SdkVersionManager``) runs first so
any overlay is on ``sys.path`` before a single ``claude_agent_sdk`` symbol is used.
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
from sync_actions import check_anthropic_connection
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


class Component(ComponentBase):
    """Orchestrates a configured Claude agent run over Keboola data."""

    def __init__(self):
        super().__init__()
        self._sdk_manager = SdkVersionManager()
        self._plugin_manager = PluginManager()
        self._output_writer = OutputWriter(self)
        self._runner = ClaudeRunner(workspace_dir=WORKSPACE_DIR)

    def run(self) -> None:
        """Orchestrate the run: ensure SDK, prepare env, run tasks, finalize.

        The ``write_always`` transcript tables MUST be flushed even when a task
        loop or output promotion raises, so the per-task work and ``promote()``
        run inside a ``try`` whose ``finally`` always flushes the transcript
        before any exception propagates (output-state durability guarantee).
        """
        if self.configuration.parameters.get("run_netns_probe"):
            self._run_netns_probe()
            return

        config = Configuration(**self.configuration.parameters)
        logging.info("Starting Claude SDK run: %s", config.log_safe_summary())
        self._warn_if_memory_intensive(config)

        sdk_version, plugin_result, env = self._ensure_sdk_and_env(config)
        transcript = self._build_transcript(config, sdk_version, plugin_result.resolved)
        tasks = TaskSource(config).load(self.get_input_tables_definitions())

        results: list[ClaudeRunResult] = []
        try:
            for task in tasks:
                results.append(self._run_one_task(task, config, plugin_result.sdk_plugins, env, transcript))
            self._output_writer.promote(default_incremental=config.output.default_incremental)
        finally:
            transcript.flush()

        self._report_outcome(results)

    def _run_netns_probe(self) -> None:
        """Gate-zero spike (advocate POC): report this job pod's capabilities and whether
        an empty network namespace can be created (the in-container agent jail depends on
        CAP_SYS_ADMIN). Flag-gated via ``parameters.run_netns_probe``; default off, never
        runs in a normal agent job. Output goes to the job log."""
        import subprocess  # noqa: PLC0415 — local import; only used by this opt-in diagnostic

        logging.warning("=== GATE-ZERO NETNS PROBE (advocate POC) ===")
        logging.warning("uid=%s euid=%s", os.getuid(), os.geteuid())
        try:
            with open("/proc/self/status") as fh:
                for line in fh:
                    if line.startswith(("CapEff", "CapBnd", "CapPrm")):
                        logging.warning(line.strip())
        except OSError:
            logging.warning("could not read /proc/self/status")
        proc = subprocess.run(
            [sys.executable, "scripts/spike/netns_probe.py"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in (proc.stdout + proc.stderr).splitlines():
            logging.warning("probe| %s", line)
        logging.warning("=== probe exit code: %s ===", proc.returncode)

    def _ensure_sdk_and_env(self, config: Configuration) -> tuple[str, PluginResult, dict[str, str]]:
        """Step 1a + 2: resolve the SDK version, prepare plugins and the env."""
        sdk_version = self._sdk_manager.ensure(config.sdk_version, config.sdk_version_on_failure.value)
        env = self._build_env(config)
        for cache_dir in (AGENT_HOME, UV_CACHE_DIR, NPM_CONFIG_CACHE, XDG_CACHE_HOME):
            os.makedirs(cache_dir, exist_ok=True)
        plugin_result = self._plugin_manager.prepare(
            config.plugins, env, github_token=config.github_token, secret_values=self._secret_values(config)
        )
        os.makedirs(WORKSPACE_DIR, exist_ok=True)
        self._output_writer.ensure_dir()
        if config.workspace_input_files:
            self._stage_input_files()
        return sdk_version, plugin_result, env

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
            logging.info("Staged %d input file(s) into the agent workspace.", staged)

    @staticmethod
    def _build_env(config: Configuration) -> dict[str, str]:
        """Build the subprocess env (secrets injected; never logged).

        Besides the Anthropic/GitHub secrets, this redirects the agent-runtime
        launchers' caches and HOME to the writable ``/tmp`` (the image root is
        read-only at runtime). Without this, ``uvx``/``npx`` MCP servers fail to
        initialise their cache and never launch (Finding 5).
        """
        env: dict[str, str] = {
            "ANTHROPIC_API_KEY": config.anthropic_key,
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            # Writable HOME + caches for the read-only image (Finding 5).
            "HOME": AGENT_HOME,
            "UV_CACHE_DIR": UV_CACHE_DIR,
            "NPM_CONFIG_CACHE": NPM_CONFIG_CACHE,
            "XDG_CACHE_HOME": XDG_CACHE_HOME,
        }
        if config.github_token:
            env["GITHUB_TOKEN"] = config.github_token
            env["GH_TOKEN"] = config.github_token
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
        """Run one task inside its own event loop, teeing messages to the transcript."""
        logging.info("Running task '%s'.", task.task_id)
        options = self._runner.build_options(task, config, plugin_paths, env)
        transcript.begin_task(task.task_id)
        result = asyncio.run(self._runner.run_task(task, options, on_message=transcript.on_message))
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
        logging.info("All %d task(s) completed successfully.", len(results))

    @sync_action("testConnection")
    def test_connection(self):
        """Validate #anthropic_key with a single cheap in-process API call."""
        config = Configuration(**self.configuration.parameters)
        return check_anthropic_connection(config.anthropic_key)


if __name__ == "__main__":
    try:
        comp = Component()
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        sys.exit(1)
    except Exception as exc:
        logging.exception(exc)
        sys.exit(2)
