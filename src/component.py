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

from keboola.component.base import ComponentBase, sync_action
from keboola.component.exceptions import UserException

from claude_runner import ClaudeRunner
from configuration import Configuration
from output_writer import OutputWriter
from plugin_manager import PluginManager
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

    def run(self):
        """Orchestrate the run: ensure SDK, prepare env, run tasks, finalize."""
        config = Configuration(**self.configuration.parameters)
        logging.info("Starting Claude SDK run: %s", config.log_safe_summary())

        sdk_version, plugin_result, env = self._ensure_sdk_and_env(config)
        transcript = self._build_transcript(config, sdk_version, plugin_result.resolved)

        tasks = TaskSource(config).load(self.get_input_tables_definitions())
        results = [self._run_one_task(task, config, plugin_result.sdk_plugins, env, transcript) for task in tasks]

        self._finalize(config, transcript, results)

    def _ensure_sdk_and_env(self, config: Configuration):
        """Step 1a + 2: resolve the SDK version, prepare plugins and the env."""
        sdk_version = self._sdk_manager.ensure(config.sdk_version, config.sdk_version_on_failure.value)
        env = self._build_env(config)
        plugin_result = self._plugin_manager.prepare(config.plugins, env, github_token=config.github_token)
        os.makedirs(WORKSPACE_DIR, exist_ok=True)
        self._output_writer.ensure_dir()
        return sdk_version, plugin_result, env

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

    def _build_transcript(self, config: Configuration, sdk_version: str, plugins_resolved: dict[str, str]):
        secrets = [s for s in (config.anthropic_key, config.github_token) if s]
        return TranscriptWriter(
            component=self,
            files_out_path=self.files_out_path,
            sdk_version_resolved=sdk_version,
            plugins_resolved=plugins_resolved,
            secret_values=secrets,
        )

    def _run_one_task(self, task: Task, config: Configuration, plugin_paths, env, transcript: TranscriptWriter):
        """Run one task inside its own event loop, teeing messages to the transcript."""
        logging.info("Running task '%s'.", task.task_id)
        options = self._runner.build_options(task, config, plugin_paths, env)
        transcript.begin_task(task.task_id)
        result = asyncio.run(self._runner.run_task(task, options, on_message=transcript.on_message))
        result.extra_args["model"] = task.model or config.model.value
        transcript.end_task(result)
        return result

    def _finalize(self, config: Configuration, transcript: TranscriptWriter, results):
        """Promote agent outputs, flush transcripts (always), decide exit code."""
        self._output_writer.promote(default_incremental=config.output.default_incremental)
        transcript.flush()

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
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
