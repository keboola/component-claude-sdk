"""The SDK boundary — builds ClaudeAgentOptions and drives the query() loop.

This is the single seam the tests mock (``ClaudeRunner._query``). The
``claude_agent_sdk`` import is **lazy** (inside methods) so the optional runtime
overlay (SdkVersionManager, spec §2.10) is on ``sys.path`` before any SDK symbol
is touched.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from keboola.component.exceptions import UserException

from configuration import Configuration, McpRemoteServer, McpStdioServer
from tasks import Task

if TYPE_CHECKING:  # import only for type-checkers; runtime import stays lazy/overlay-safe
    from claude_agent_sdk import ClaudeAgentOptions

# Budget-cap result subtypes the SDK emits (spec §4 / §12: it is a subtype value).
BUDGET_CAP_SUBTYPES = frozenset({"error_max_budget", "error_max_budget_usd"})
TURN_CAP_SUBTYPES = frozenset({"error_max_turns"})

# GitHub working requires Bash and the gh/git scoped tools (spec §5.1).
GITHUB_TOOLS = ("Bash", "Bash(gh *)", "Bash(git *)")


@dataclass
class ClaudeRunResult:
    """Captured outcome of one agent task (from the terminal ResultMessage)."""

    task_id: str
    success: bool
    session_id: str = ""
    subtype: str = ""
    is_error: bool = False
    result_text: str | None = None
    total_cost_usd: float | None = None
    duration_ms: int = 0
    num_turns: int = 0
    usage: dict[str, Any] | None = None
    model_usage: dict[str, Any] | None = None
    api_error_status: int | None = None
    error_message: str | None = None
    extra_args: dict[str, Any] = field(default_factory=dict)


class ClaudeRunner:
    """Owns the ClaudeAgentOptions build and the single-shot query() loop."""

    def __init__(self, workspace_dir: str) -> None:
        self._workspace_dir = workspace_dir

    def build_options(
        self,
        task: Task,
        config: Configuration,
        plugin_paths: list[dict[str, str]],
        env: dict[str, str],
    ) -> ClaudeAgentOptions:
        """Map merged (config + task) settings to a ClaudeAgentOptions.

        Lazy SDK import keeps the runtime overlay safe.
        """
        from claude_agent_sdk import ClaudeAgentOptions

        allowed = list(config.allowed_tools)
        if config.github_enabled:
            for tool in GITHUB_TOOLS:
                if tool not in allowed:
                    allowed.append(tool)

        kwargs: dict[str, Any] = {
            "model": task.model or config.model.value,
            "max_turns": task.max_turns if task.max_turns is not None else config.max_turns,
            "max_budget_usd": config.effective_budget(task.max_budget_usd),
            "permission_mode": config.permission_mode.value,
            "allowed_tools": allowed,
            "disallowed_tools": list(config.disallowed_tools),
            "mcp_servers": self._build_mcp_servers(config),
            "plugins": plugin_paths,
            "setting_sources": list(config.setting_sources),
            "cwd": self._workspace_dir,
            "env": env,
        }
        system_prompt = task.system_prompt or config.system_prompt
        if system_prompt:
            kwargs["system_prompt"] = system_prompt
        if config.fallback_model is not None:
            kwargs["fallback_model"] = config.fallback_model.value
        if config.effort is not None:
            kwargs["effort"] = config.effort.value
        if config.workspace_input_files:
            kwargs["add_dirs"] = [self._workspace_dir]

        settings_path = self._write_settings_file(config.settings_json, env)
        if settings_path:
            kwargs["settings"] = settings_path

        return ClaudeAgentOptions(**kwargs)

    def _write_settings_file(self, settings_json: dict[str, Any] | str | None, env: dict[str, str]) -> str | None:
        """Persist the ``settings_json`` passthrough to a file and return its path.

        ``ClaudeAgentOptions.settings`` is a file PATH, so we materialise the
        config object (or a raw JSON string) under the writable home and hand the
        SDK the path (spec §5.1). Returns ``None`` when nothing is configured.
        """
        if not settings_json:
            return None
        home = env.get("CLAUDE_CONFIG_DIR") or self._workspace_dir
        os.makedirs(home, exist_ok=True)
        path = os.path.join(home, "settings.json")
        if isinstance(settings_json, str):
            content = settings_json
        else:
            content = json.dumps(settings_json)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    @staticmethod
    def _build_mcp_servers(config: Configuration) -> dict[str, dict[str, Any]]:
        """Translate the typed MCP server list into the SDK's dict shape."""
        servers: dict[str, dict[str, Any]] = {}
        for server in config.mcp_servers:
            if isinstance(server, McpStdioServer):
                servers[server.name] = {
                    "type": "stdio",
                    "command": server.command,
                    "args": list(server.args),
                    "env": dict(server.env),
                }
            elif isinstance(server, McpRemoteServer):
                servers[server.name] = {
                    "type": server.type,
                    "url": server.url,
                    "headers": dict(server.headers),
                }
        return servers

    async def run_task(
        self,
        task: Task,
        options: Any,
        on_message: Callable[[Any], Awaitable[None] | None],
    ) -> ClaudeRunResult:
        """Run one task, teeing every message to ``on_message``; capture result.

        A raise from the SDK query loop is translated into a ``UserException``
        (exit 1) with an actionable message, rather than escaping as an opaque
        exit 2. The lazy SDK import keeps the runtime overlay safe.
        """
        from claude_agent_sdk import CLIConnectionError, CLINotFoundError, ProcessError

        result_message = None
        try:
            async for message in self._query(task.prompt, options):
                maybe = on_message(message)
                if maybe is not None:
                    await maybe
                if type(message).__name__ == "ResultMessage":
                    result_message = message
        except (CLINotFoundError, CLIConnectionError) as exc:
            raise UserException(
                f"The agent CLI/MCP failed to launch for task '{task.task_id}': {exc}"
            ) from exc
        except ProcessError as exc:
            raise self._map_run_error(task, exc) from exc
        except UserException:
            raise
        except Exception as exc:
            # The SDK re-raises a non-terminal cap/error from receive_messages as a
            # BARE Exception (claude_agent_sdk/_internal/query.py: receive_messages
            # raises Exception(error_text)), so a turn-cap / budget-cap escapes the
            # typed handlers above and would crash as exit 2. Map it to a clean
            # exit-1 UserException (Finding 7) — a cap then ends the run gracefully.
            raise self._map_run_error(task, exc) from exc

        if result_message is None:
            logging.warning("Task '%s' produced no ResultMessage.", task.task_id)
            return ClaudeRunResult(
                task_id=task.task_id,
                success=False,
                error_message="No result message returned by the agent (run did not complete).",
            )
        return self._to_result(task, result_message)

    @staticmethod
    def _looks_like_auth_error(detail: str) -> bool:
        lowered = detail.lower()
        return any(s in lowered for s in ("401", "authentication", "unauthorized", "invalid api key", "api key"))

    @staticmethod
    def _looks_like_cap_error(detail: str) -> bool:
        """Whether an error string is a turn-cap / budget-cap exhaustion."""
        lowered = detail.lower()
        return any(
            s in lowered
            for s in ("error_max_turns", "error_max_budget", "maximum number of turns", "budget")
        )

    def _map_run_error(self, task: Task, exc: Exception) -> UserException:
        """Translate a query-loop raise into an actionable exit-1 UserException."""
        detail = str(exc)
        if self._looks_like_cap_error(detail):
            return UserException(
                f"Task '{task.task_id}' hit its turn/budget cap and stopped: {detail}"
            )
        if self._looks_like_auth_error(detail):
            return UserException(
                f"Anthropic authentication failed for task '{task.task_id}' — check #anthropic_key."
            )
        return UserException(f"The agent process failed for task '{task.task_id}': {detail}")

    @staticmethod
    def _to_result(task: Task, message: Any) -> ClaudeRunResult:
        subtype = getattr(message, "subtype", "") or ""
        is_error = bool(getattr(message, "is_error", False))
        cap_hit = subtype in BUDGET_CAP_SUBTYPES or subtype in TURN_CAP_SUBTYPES
        success = not is_error and not cap_hit
        error_message = None
        if cap_hit:
            error_message = f"Run stopped by cap (subtype={subtype})."
        elif is_error:
            errs = getattr(message, "errors", None)
            error_message = "; ".join(errs) if errs else f"Run ended with error (subtype={subtype})."
        return ClaudeRunResult(
            task_id=task.task_id,
            success=success,
            session_id=getattr(message, "session_id", "") or "",
            subtype=subtype,
            is_error=is_error,
            result_text=getattr(message, "result", None),
            total_cost_usd=getattr(message, "total_cost_usd", None),
            duration_ms=getattr(message, "duration_ms", 0) or 0,
            num_turns=getattr(message, "num_turns", 0) or 0,
            usage=getattr(message, "usage", None),
            model_usage=getattr(message, "model_usage", None),
            api_error_status=getattr(message, "api_error_status", None),
            error_message=error_message,
        )

    def _query(self, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        """The SDK seam — lazily imports and calls ``query``.

        Tests monkeypatch this to yield a canned, typed message stream so the
        whole pipeline runs with no network and no subprocess (spec §7).
        """
        from claude_agent_sdk import query

        return query(prompt=prompt, options=options)
