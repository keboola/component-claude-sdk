"""Runtime plugin installation via the bundled ``claude plugin`` CLI (spec §6.4).

Plugins cannot be baked into the read-only image, so they are added/installed at
job start into a writable ``/tmp/claude-home`` and loaded into the SDK by local
path (the Python SDK only supports ``{"type": "local", "path": ...}``).

Each entry chooses a pinned ref (reproducible) or ``latest`` (re-pull newest).
Private sources authenticate via ``GITHUB_TOKEN``/``GH_TOKEN``, which the install
subprocess receives from the Advocate-held ``#github_token`` (injected into the
install env only — never into the cleared agent env). The install subprocess runs
Advocate-side and exits before the agent spawns, so the token is not exposed to
the agent. All CLI output is logged with secret scrubbing; a non-zero exit
becomes a ``UserException`` naming the failing source.
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

# Bound every ``claude`` CLI subprocess call (plugin add/install/marketplace list)
# so a stalled git clone or registry fetch cannot hang the job indefinitely — the
# platform would otherwise kill it with no useful error (Finding 4).
_CLI_TIMEOUT_SECONDS = 300


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
        # Advocate-held GitHub token for private-source auth; injected into the
        # install subprocess env only (never into the cleared agent env).
        self._github_token: str = ""

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
        # Hold the token for private-source auth in the install subprocess env.
        self._github_token = github_token

        env["CLAUDE_CONFIG_DIR"] = self._claude_home
        env["CLAUDE_CODE_PLUGIN_CACHE_DIR"] = self._cache_dir
        # NB: we deliberately do NOT set CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE.
        # Confirmed in-container (deterministic 3/3 vs 3/3): with that flag the CLI
        # leaves the clone under the source-derived temp name and skips the
        # rename-to-declared-name + validation, so marketplace.json is never found
        # at the expected path and `marketplace add` fails ("Marketplace file not
        # found …"). Without it the add succeeds and the marketplace registers under
        # its declared name (Finding 4).

        result = PluginResult()
        if not plugins:
            return result

        os.makedirs(self._cache_dir, exist_ok=True)
        for entry in plugins:
            self._prepare_entry(entry, env, github_token, result)
        return result

    def _prepare_entry(self, entry: PluginEntry, env: dict[str, str], github_token: str, result: PluginResult) -> None:
        source = self._resolve_source(entry, github_token)

        if entry.version == "latest":
            self._run(["plugin", "marketplace", "add", source], env, source)
        else:
            pinned_source = self._pin_source(source, entry.version)
            self._run(["plugin", "marketplace", "add", pinned_source], env, source)

        # The CLI registers the marketplace under the name DECLARED in the repo's
        # marketplace.json (e.g. obra/superpowers registers as "superpowers-dev"),
        # NOT a name derived from the source. Discover the real handle + install
        # location from `marketplace list --json` and use them for everything
        # downstream — deriving the name from the source mismatches and the
        # install/cache-path silently fail (Finding 4).
        marketplace, install_location = self._discover_marketplace(source, env)

        if entry.version == "latest":
            self._run(["plugin", "marketplace", "update", marketplace], env, source)

        plugin_names = self._resolve_plugin_names(entry, marketplace, install_location, source)
        for name in plugin_names:
            self._install_plugin(name, marketplace, env, source)
            result.resolved[f"{marketplace}/{name}"] = entry.version

        for path in self._cache_paths(marketplace, install_location):
            result.sdk_plugins.append({"type": "local", "path": path})

    def _resolve_plugin_names(
        self, entry: PluginEntry, marketplace: str, install_location: str | None, source: str
    ) -> list[str]:
        """The plugin NAMES to install for this entry.

        Explicit names are used as given. An empty list or ``["*"]`` means
        "install all": the marketplace is NAMED (e.g. ``superpowers-dev``) but
        contains plugins under their OWN names (e.g. ``superpowers``), so we must
        enumerate the DECLARED plugin names from the marketplace's
        ``marketplace.json`` — never pass ``"*"`` or the marketplace name to
        ``claude plugin install`` (Finding 8).
        """
        explicit = [n for n in entry.plugins if n and n != "*"]
        if explicit:
            return explicit
        declared = self._declared_plugin_names(install_location)
        if declared:
            return declared
        raise UserException(
            f"Plugin source '{source}' (marketplace '{marketplace}') declares no installable "
            f"plugins, or its marketplace.json could not be read to enumerate them. "
            f"List the plugin name(s) explicitly in the 'plugins' field."
        )

    @staticmethod
    def _declared_plugin_names(install_location: str | None) -> list[str]:
        """Read the plugin names declared in ``<installLocation>/.claude-plugin/marketplace.json``."""
        if not install_location:
            return []
        manifest = os.path.join(install_location, ".claude-plugin", "marketplace.json")
        try:
            with open(manifest, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Could not read declared plugins from '%s': %s", manifest, exc)
            return []
        plugins = data.get("plugins") if isinstance(data, dict) else None
        if not isinstance(plugins, list):
            return []
        return [p["name"] for p in plugins if isinstance(p, dict) and p.get("name")]

    @staticmethod
    def _resolve_source(entry: PluginEntry, github_token: str) -> str:
        """Resolve a public shorthand to owner/repo; pass explicit sources through."""
        if entry.private and not github_token:
            raise UserException(f"Plugin source '{entry.source}' is marked private but no #github_token is set.")
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
    def _source_repo(source: str) -> str:
        """The ``owner/repo`` slug used to match a registered marketplace's ``repo``."""
        base = source.split("#", 1)[0].split("@github.com:", 1)[-1]
        base = base.removeprefix("https://github.com/").removeprefix("http://github.com/")
        base = base.rstrip("/")
        if base.endswith(".git"):
            base = base[: -len(".git")]
        return base

    @staticmethod
    def _fallback_marketplace_name(source: str) -> str:
        """Last-resort handle if discovery fails — the repo name segment."""
        base = source.split("#", 1)[0].rstrip("/").split("/")[-1]
        if base.endswith(".git"):
            base = base[: -len(".git")]
        return base

    def _discover_marketplace(self, source: str, env: dict[str, str]) -> tuple[str, str | None]:
        """Find the REAL registered marketplace name + install location after add.

        The CLI registers a marketplace under the name declared in its
        ``marketplace.json``, which usually differs from anything derivable from
        the source string. We read ``marketplace list --json`` and match the entry
        by its ``repo``/``source`` against the requested source; on no match we
        fall back to the repo-name-segment guess so behaviour degrades, not breaks.
        """
        proc = self._run(["plugin", "marketplace", "list", "--json"], env, source, check=False)
        wanted_repo = self._source_repo(source)
        try:
            data = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            data = []
        markets = data if isinstance(data, list) else data.get("marketplaces", [])
        for market in markets:
            if not isinstance(market, dict):
                continue
            repo = (market.get("repo") or market.get("source") or "").rstrip("/")
            if repo and self._source_repo(repo) == wanted_repo and market.get("name"):
                return market["name"], market.get("installLocation") or market.get("path")
        # No match (e.g. only one freshly-added marketplace) — if there is exactly
        # one entry, trust it; otherwise fall back to the source-derived guess.
        if len(markets) == 1 and isinstance(markets[0], dict) and markets[0].get("name"):
            return markets[0]["name"], markets[0].get("installLocation") or markets[0].get("path")
        return self._fallback_marketplace_name(source), None

    @staticmethod
    def _pin_source(source: str, ref: str) -> str:
        """Attach the pinned ref: ``#ref`` for git URLs, ``@ref`` for owner/repo."""
        if source.startswith(("http://", "https://", "git@", "ssh://")):
            return f"{source}#{ref}"
        return f"{source}@{ref}"

    def _install_plugin(self, name: str, marketplace: str, env: dict[str, str], source: str) -> None:
        # Always a concrete <plugin>@<marketplace> target — "*"/the marketplace
        # name is never installable (Finding 8); callers resolve names first.
        self._run(["plugin", "install", f"{name}@{marketplace}"], env, source)

    def _cache_paths(self, marketplace: str, install_location: str | None) -> list[str]:
        """The local cache path the SDK loads the plugin from.

        Prefer the ``installLocation`` discovered from ``marketplace list --json``
        (authoritative — it reflects the DECLARED marketplace name the CLI cloned
        into). Fall back to the conventional ``<cache>/<marketplace>`` only when no
        location was discovered.
        """
        if install_location:
            return [install_location]
        return [f"{self._cache_dir}/{marketplace}"]

    def _run(
        self,
        args: list[str],
        env: dict[str, str],
        source: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run a ``claude`` CLI command, logging scrubbed output.

        Uses ``_plugin_install_env`` (§14 fix) so the subprocess receives only
        a minimal, explicit env — never the full ``os.environ`` which contains
        KBC_TOKEN and other platform-injected secrets.
        """
        cmd = [_resolve_claude_cli(), *args]
        install_env = self._plugin_install_env(env)
        # Inject the GitHub token into the install subprocess ONLY (Advocate-side,
        # short-lived, exits before the agent spawns) so private-source clones can
        # authenticate.  It is never placed in the cleared agent env.
        if self._github_token:
            install_env["GITHUB_TOKEN"] = self._github_token
            install_env["GH_TOKEN"] = self._github_token
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=install_env, timeout=_CLI_TIMEOUT_SECONDS)
        except OSError as exc:
            # FileNotFoundError / PermissionError on launch — surface as a clean
            # UserException (exit 1) instead of an unhandled crash (exit 2).
            raise UserException(f"Claude CLI failed to launch for plugin install (source '{source}'): {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            # A stalled git clone / registry fetch would otherwise hang the job
            # until the platform kills it with no useful error (Finding 4).
            raise UserException(
                f"Claude CLI command 'claude {' '.join(args)}' for source '{source}' "
                f"timed out after {_CLI_TIMEOUT_SECONDS}s"
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

    @staticmethod
    def _plugin_install_env(agent_env: dict[str, str]) -> dict[str, str]:
        """Build a minimal env for the plugin install subprocess.

        Plugin install runs on the Advocate side (trusted, has network) but must
        not inherit the full os.environ into the subprocess — that would leak
        KBC_TOKEN and other platform-injected secrets into the CLI child process
        where they may appear in /proc/<pid>/environ or error dumps.

        We pass only:
        - The plugin-specific vars already in agent_env (CLAUDE_CONFIG_DIR etc.)
        - PATH so the CLI can find git/system tools

        The GitHub token for private sources is added by the caller (``_run``) into
        the install subprocess env, NOT here — it must never enter the cleared
        agent env that this helper copies from.

        This is the §14 fix: stop {**os.environ, **env} subprocess inheritance.
        """
        install_env = dict(agent_env)
        # Always include PATH so the CLI can find git, node, etc.
        path = os.environ.get("PATH", "")
        if path:
            install_env.setdefault("PATH", path)
        return install_env

    def _scrub(self, text: str | None) -> str:
        """Redact every known secret value from text destined for a log/message."""
        if not text:
            return ""
        for secret in self._secret_values:
            text = text.replace(secret, "***")
        return text
