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

from claude_runner import ClaudeRunner, ClaudeRunResult
from configuration import Configuration
from output_writer import OutputWriter
from plugin_manager import PluginManager, PluginResult
from sdk_version_manager import SdkVersionManager
from sync_actions import check_anthropic_connection
from tasks import Task, TaskSource
from transcript_writer import TranscriptWriter

WORKSPACE_DIR = "/tmp/claude-workspace"  # noqa: S108 — /tmp is the only writable path in the read-only image


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
        config = Configuration(**self.configuration.parameters)
        logging.info("Starting Claude SDK run: %s", config.log_safe_summary())

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

    def _ensure_sdk_and_env(self, config: Configuration) -> tuple[str, PluginResult, dict[str, str]]:
        """Step 1a + 2: resolve the SDK version, prepare plugins and the env."""
        sdk_version = self._sdk_manager.ensure(config.sdk_version, config.sdk_version_on_failure.value)
        env = self._build_env(config)
        plugin_result = self._plugin_manager.prepare(
            config.plugins, env, github_token=config.github_token, secret_values=self._secret_values(config)
        )
        os.makedirs(WORKSPACE_DIR, exist_ok=True)
        self._output_writer.ensure_dir()
        if config.workspace_input_files:
            self._stage_input_files()
        return sdk_version, plugin_result, env

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
        """Build the subprocess env (secrets injected; never logged)."""
        env: dict[str, str] = {
            "ANTHROPIC_API_KEY": config.anthropic_key,
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
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
