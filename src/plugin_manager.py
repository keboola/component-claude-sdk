"""Runtime plugin installation via the bundled ``claude plugin`` CLI (spec §6.4).

Plugins cannot be baked into the read-only image, so they are added/installed at
job start into a writable ``/tmp/claude-home`` and loaded into the SDK by local
path (the Python SDK only supports ``{"type": "local", "path": ...}``).

Each entry chooses a pinned ref (reproducible) or ``latest`` (re-pull newest).
Private sources authenticate via the ``GITHUB_TOKEN``/``GH_TOKEN`` already in the
subprocess ``env``. All CLI output is logged with secret scrubbing; a non-zero
exit becomes a ``UserException`` naming the failing source.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from keboola.component.exceptions import UserException

from configuration import PUBLIC_MARKETPLACE_REGISTRY, PluginEntry


@cache
def _resolve_claude_cli() -> str:
    """Resolve the absolute path of the bundled ``claude`` CLI.

    The CLI is shipped INSIDE the ``claude-agent-sdk`` package (``_bundled/claude``)
    and is NOT on ``PATH`` in the slim image, so a bare ``"claude"`` would fail with
    ``FileNotFoundError``. We resolve it the same way the SDK's own transport does
    (``<claude_agent_sdk package dir>/_bundled/claude``), importing the package
    lazily so this works against either the baked SDK or a runtime ``sdk_version``
    overlay (the overlay is on ``sys.path`` before this is first called).
    """
    cli_name = "claude.exe" if platform.system() == "Windows" else "claude"
    try:
        import claude_agent_sdk

        package_dir = Path(claude_agent_sdk.__file__).resolve().parent
        bundled = package_dir / "_bundled" / cli_name
        if bundled.is_file():
            return str(bundled)
    except Exception as exc:  # ImportError or a malformed install
        logging.warning("Could not locate the bundled claude CLI via claude_agent_sdk: %s", exc)

    # Fall back to a PATH lookup so a system-wide install still works locally.
    if found := shutil.which("claude"):
        return found

    raise UserException(
        "Claude CLI not found: the bundled 'claude' binary could not be located in the "
        "claude-agent-sdk package and no 'claude' is on PATH. Plugin install cannot proceed."
    )

CLAUDE_HOME = "/tmp/claude-home"  # noqa: S108 — /tmp is the only writable path in the read-only image
PLUGIN_CACHE_DIR = f"{CLAUDE_HOME}/plugins/cache"


@dataclass
class PluginResult:
    """Outcome of preparing the configured plugins."""

    sdk_plugins: list[dict[str, str]] = field(default_factory=list)  # [{"type":"local","path":...}]
    resolved: dict[str, str] = field(default_factory=dict)  # plugin/source -> resolved ref


class PluginManager:
    """Installs configured plugins via the bundled ``claude`` CLI."""

    def __init__(self, claude_home: str = CLAUDE_HOME) -> None:
        self._claude_home = claude_home
        self._cache_dir = f"{claude_home}/plugins/cache"
        self._secret_values: list[str] = []

    def prepare(
        self,
        plugins: list[PluginEntry],
        env: dict[str, str],
        github_token: str = "",
        secret_values: list[str] | None = None,
    ) -> PluginResult:
        """Install every configured plugin and return SDK local-plugin configs.

        ``env`` is mutated in place with the plugin/home env vars so the caller's
        subprocess (and the SDK) share the writable home. ``secret_values`` is the
        FULL set of secret strings (Anthropic key, GitHub token, MCP secrets) to
        scrub from any captured CLI output — defense-in-depth, symmetric scrub.
        ``github_token`` is also used for the private-source auth requirement.
        """
        # Scrub the full secret set (caller-provided) plus the github_token.
        self._secret_values = [s for s in {*(secret_values or []), github_token} if s]

        env["CLAUDE_CONFIG_DIR"] = self._claude_home
        env["CLAUDE_CODE_PLUGIN_CACHE_DIR"] = self._cache_dir
        env["CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE"] = "1"

        result = PluginResult()
        if not plugins:
            return result

        os.makedirs(self._cache_dir, exist_ok=True)
        for entry in plugins:
            self._prepare_entry(entry, env, github_token, result)
        return result

    def _prepare_entry(
        self, entry: PluginEntry, env: dict[str, str], github_token: str, result: PluginResult
    ) -> None:
        source = self._resolve_source(entry, github_token)
        marketplace = self._marketplace_name(source)

        if entry.version == "latest":
            self._run(["plugin", "marketplace", "add", source], env, source)
            self._run(["plugin", "marketplace", "update", marketplace], env, source)
        else:
            pinned_source = self._pin_source(source, entry.version)
            self._run(["plugin", "marketplace", "add", pinned_source], env, source)

        plugin_names = entry.plugins or ["*"]
        for name in plugin_names:
            self._install_plugin(name, marketplace, env, source)
            result.resolved[f"{marketplace}/{name}"] = entry.version

        for path in self._cache_paths(marketplace, env, source):
            result.sdk_plugins.append({"type": "local", "path": path})

    @staticmethod
    def _resolve_source(entry: PluginEntry, github_token: str) -> str:
        """Resolve a public shorthand to owner/repo; pass explicit sources through."""
        if entry.private and not github_token:
            raise UserException(
                f"Plugin source '{entry.source}' is marked private but no #github_token is set."
            )
        if "/" not in entry.source and ":" not in entry.source:
            resolved = PUBLIC_MARKETPLACE_REGISTRY.get(entry.source)
            if resolved is None:
                raise UserException(
                    f"Unknown public plugin shorthand '{entry.source}'. Use a known shorthand "
                    f"({sorted(PUBLIC_MARKETPLACE_REGISTRY)}) or an explicit owner/repo or git URL."
                )
            return resolved
        return entry.source

    @staticmethod
    def _marketplace_name(source: str) -> str:
        """The marketplace handle the CLI registers — the repo name segment."""
        base = source.rstrip("/").split("/")[-1]
        for suffix in (".git",):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        return base

    @staticmethod
    def _pin_source(source: str, ref: str) -> str:
        """Attach the pinned ref: ``#ref`` for git URLs, ``@ref`` for owner/repo."""
        if source.startswith(("http://", "https://", "git@", "ssh://")):
            return f"{source}#{ref}"
        return f"{source}@{ref}"

    def _install_plugin(self, name: str, marketplace: str, env: dict[str, str], source: str) -> None:
        target = marketplace if name == "*" else f"{name}@{marketplace}"
        self._run(["plugin", "install", target], env, source)

    def _cache_paths(self, marketplace: str, env: dict[str, str], source: str) -> list[str]:
        """Resolve the local cache paths of the installed marketplace plugins."""
        proc = self._run(["plugin", "marketplace", "list", "--json"], env, source, check=False)
        paths: list[str] = []
        try:
            data = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            logging.warning("Could not parse 'claude plugin marketplace list --json' output; using cache dir.")
            data = []
        for market in data if isinstance(data, list) else data.get("marketplaces", []):
            if isinstance(market, dict) and market.get("name") == marketplace:
                path = market.get("path") or market.get("localPath")
                if path:
                    paths.append(path)
        if not paths:
            # Fall back to the conventional cache location for this marketplace.
            paths.append(f"{self._cache_dir}/{marketplace}")
        return paths

    def _run(
        self,
        args: list[str],
        env: dict[str, str],
        source: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run a ``claude`` CLI command, logging scrubbed output."""
        cmd = [_resolve_claude_cli(), *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **env})
        except OSError as exc:
            # FileNotFoundError / PermissionError on launch — surface as a clean
            # UserException (exit 1) instead of an unhandled crash (exit 2).
            raise UserException(
                f"Claude CLI failed to launch for plugin install (source '{source}'): {exc}"
            ) from exc
        logging.info("claude %s -> exit %s", " ".join(args), proc.returncode)
        scrubbed_out = self._scrub(proc.stdout)
        if scrubbed_out.strip():
            logging.debug("claude %s stdout: %s", args[0], scrubbed_out)
        if check and proc.returncode != 0:
            scrubbed_err = self._scrub(proc.stderr or proc.stdout)
            raise UserException(
                f"Plugin command 'claude {' '.join(args)}' failed for source '{source}': {scrubbed_err.strip()}"
            )
        return proc

    def _scrub(self, text: str | None) -> str:
        """Redact every known secret value from text destined for a log/message."""
        if not text:
            return ""
        for secret in self._secret_values:
            text = text.replace(secret, "***")
        return text
