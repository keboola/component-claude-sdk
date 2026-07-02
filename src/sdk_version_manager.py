"""Optional runtime overlay of the claude-agent-sdk (spec §2.10).

The image bakes ``claude-agent-sdk==0.2.101`` (with its bundled CLI). To move to
a newer SDK/CLI without an image rebuild, ``ensure()`` can pip-install a
requested version into a writable ``/tmp`` overlay and put it first on
``sys.path`` so it shadows the baked package — **before** any module imports the
SDK. Because the SDK bundles the CLI pinned to the package version, upgrading the
package upgrades the CLI too.

``ensure()`` must run as the very first step of ``run()`` (spec §6.1 step 1a);
``ClaudeRunner`` and ``PluginManager`` import ``claude_agent_sdk`` lazily so the
overlay is on the path before any SDK symbol is touched.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from importlib.metadata import PathDistribution
from pathlib import Path

from keboola.component.exceptions import UserException

PINNED = "pinned"
OVERLAY_DIR = "/tmp/sdk-overlay"  # noqa: S108 — /tmp is the only writable path in the read-only image
PIP_CACHE_DIR = "/tmp/pip-cache"  # noqa: S108 — writable cache so pip doesn't warn about a read-only home
PACKAGE = "claude-agent-sdk"
# Bound the pip install so a stalled fetch (no egress, registry down) cannot hang
# the job indefinitely — the platform would otherwise kill it with no useful
# error (Finding 4).
_PIP_INSTALL_TIMEOUT_SECONDS = 300


class SdkVersionManager:
    """Resolves which claude-agent-sdk version the run uses."""

    def __init__(self, overlay_dir: str = OVERLAY_DIR) -> None:
        self._overlay_dir = overlay_dir

    def ensure(self, sdk_version: str, on_failure: str) -> str:
        """Return the resolved SDK version, installing an overlay if requested.

        - ``pinned`` -> no network; return the baked package version.
        - a concrete version or ``latest`` -> pip-install into the overlay dir,
          prepend it to ``sys.path``, return the installed version.

        On install failure: ``on_failure == "fail"`` raises ``UserException``;
        ``"fallback_pinned"`` logs a warning and returns the baked version.
        """
        sdk_version = (sdk_version or PINNED).strip()
        if sdk_version == PINNED:
            return self._baked_version()

        spec = PACKAGE if sdk_version == "latest" else f"{PACKAGE}=={sdk_version}"
        logging.info("Installing runtime SDK overlay: %s -> %s", spec, self._overlay_dir)
        try:
            self._pip_install(spec)
        except subprocess.CalledProcessError as exc:
            return self._handle_install_failure(exc, sdk_version, on_failure)
        except subprocess.TimeoutExpired as exc:
            return self._handle_install_timeout(exc, sdk_version, on_failure)

        # Guarantee the overlay shadows the baked SDK by sitting at sys.path[0].
        # If a prior call in the same process already added it further down, move
        # it back to the front rather than leaving it where "prepend" no longer holds.
        if self._overlay_dir in sys.path:
            sys.path.remove(self._overlay_dir)
        sys.path.insert(0, self._overlay_dir)
        resolved = self._overlay_version()
        logging.info("Runtime SDK overlay active: %s==%s", PACKAGE, resolved)
        return resolved

    def _pip_install(self, spec: str) -> None:
        # Point pip at a writable /tmp cache; the image's home dir is read-only,
        # which would otherwise emit a "cache dir not writable" warning.
        env = {**os.environ, "PIP_CACHE_DIR": PIP_CACHE_DIR}
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--target", self._overlay_dir, spec],
            capture_output=True,
            text=True,
            env=env,
            timeout=_PIP_INSTALL_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, output=result.stdout, stderr=result.stderr
            )

    def _handle_install_failure(self, exc: subprocess.CalledProcessError, sdk_version: str, on_failure: str) -> str:
        detail = (exc.stderr or exc.output or "").strip()
        if on_failure == "fail":
            raise UserException(
                f"Failed to install requested SDK version '{sdk_version}': {detail}. "
                f"Set sdk_version_on_failure='fallback_pinned' to continue on the baked version instead."
            ) from exc
        logging.warning(
            "Could not install SDK version '%s' (%s); falling back to the baked version.",
            sdk_version,
            detail,
        )
        return self._baked_version()

    def _handle_install_timeout(self, exc: subprocess.TimeoutExpired, sdk_version: str, on_failure: str) -> str:
        # A stalled pip install (no egress, hung registry) would otherwise hang
        # the job until the platform kills it with no useful error (Finding 4).
        if on_failure == "fail":
            raise UserException(
                f"Failed to install requested SDK version '{sdk_version}': "
                f"timed out after {_PIP_INSTALL_TIMEOUT_SECONDS}s. "
                f"Set sdk_version_on_failure='fallback_pinned' to continue on the baked version instead."
            ) from exc
        logging.warning(
            "Could not install SDK version '%s' (timed out after %ds); falling back to the baked version.",
            sdk_version,
            _PIP_INSTALL_TIMEOUT_SECONDS,
        )
        return self._baked_version()

    @staticmethod
    def _baked_version() -> str:
        import claude_agent_sdk

        return claude_agent_sdk.__version__

    def _overlay_version(self) -> str:
        """Read the installed version from the overlay dist metadata."""
        overlay = Path(self._overlay_dir)
        for dist_info in overlay.glob("claude_agent_sdk-*.dist-info"):
            try:
                return PathDistribution(dist_info).version
            except Exception:  # pragma: no cover - metadata read is best-effort
                continue
        # Fall back to the baked version string if metadata can't be read.
        return self._baked_version()
