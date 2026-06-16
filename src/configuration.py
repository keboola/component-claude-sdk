"""Typed Pydantic configuration for keboola.app-claude-sdk.

The component is configured with a single (non-row) configuration; the model
tree below maps every parameter onto a concrete ``ClaudeAgentOptions`` field or
subprocess ``env`` var (see the design spec §5.1). Encrypted ``#``-prefixed JSON
keys are exposed as clean Python attributes via ``Field(alias="#...")``.

Secret VALUES are never logged or echoed here; only key NAMES appear in code.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from keboola.component.exceptions import UserException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

# Built-in registry of vetted PUBLIC plugin-marketplace shorthands -> canonical
# GitHub "owner/repo". Used by PluginManager to resolve a friendly source name.
# Private sources are always given as an explicit owner/repo or URL (never here).
PUBLIC_MARKETPLACE_REGISTRY: dict[str, str] = {
    "superpowers": "obra/superpowers",
}

# Non-prompting SDK permission modes — the only ones viable in a headless
# Keboola job. Prompting modes (default/acceptEdits/plan) would hang the
# container until the job times out, so they are rejected (spec §6.5).
NON_PROMPTING_PERMISSION_MODES = frozenset({"dontAsk", "bypassPermissions", "auto"})

# Display-only section wrappers introduced by the sectioned configSchema UI
# (grid / grid-strict groups). The persisted JSON nests fields under these
# objects, but the config MODEL stays flat — the ``_flatten_sections``
# validator below lifts these wrappers back up so BOTH the historical flat
# shape and the new nested shape parse to the same model. Keep this list in
# sync with the top-level object properties in ``configSchema.json`` that exist
# purely for layout. NB: ``task`` and ``output`` are REAL nested model fields
# (TaskConfig / OutputConfig) and must NOT be listed here.
_SECTION_WRAPPERS = frozenset(
    {
        "connection",
        "model_budget",
        "permissions",
        "github",
        "task_output",
        "mcp_section",
        "plugins_section",
        "advanced",
    }
)


def _coerce_empty_object(v):
    """Coerce the job runtime's empty-object artefact ``[]`` back to ``{}``.

    Keboola's job runtime rewrites an empty JSON object ``{}`` in the container
    config.json to an empty array ``[]``. A ``dict``-typed field that is left
    empty therefore arrives as ``[]`` and would fail validation. Only the EMPTY
    list is coerced; a populated list is left untouched so it still fails loudly.
    """
    if isinstance(v, list) and not v:
        return {}
    return v


class Model(StrEnum):
    """User-selectable Claude model ids (bare ids, no date suffix)."""

    opus_4_8 = "claude-opus-4-8"
    sonnet_4_6 = "claude-sonnet-4-6"
    haiku_4_5 = "claude-haiku-4-5"


class Effort(StrEnum):
    """SDK EffortLevel enum (claude_agent_sdk types.EffortLevel)."""

    low = "low"
    medium = "medium"
    high = "high"
    xhigh = "xhigh"
    max = "max"


class PermissionMode(StrEnum):
    """The non-prompting subset of the SDK PermissionMode enum (spec §6.5)."""

    dont_ask = "dontAsk"
    bypass_permissions = "bypassPermissions"
    auto = "auto"


class SdkVersionOnFailure(StrEnum):
    """Behaviour when a non-pinned runtime SDK install fails (spec §2.10)."""

    fail = "fail"
    fallback_pinned = "fallback_pinned"


class McpStdioServer(BaseModel):
    """A stdio MCP server launched as an in-container subprocess.

    Secrets live in ``env`` and arrive decrypted at runtime.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: Literal["stdio"] = "stdio"
    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    _coerce_env = field_validator("env", mode="before")(_coerce_empty_object)


class McpRemoteServer(BaseModel):
    """An HTTP or SSE MCP server. Secrets live in ``headers`` (Bearer token)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: Literal["http", "sse"]
    name: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)

    _coerce_headers = field_validator("headers", mode="before")(_coerce_empty_object)


# Discriminated union on ``type`` so a config entry is parsed into the right shape.
McpServerConfig = Annotated[McpStdioServer | McpRemoteServer, Field(discriminator="type")]


class PluginEntry(BaseModel):
    """One plugin-marketplace source to install at runtime (spec §2.8).

    ``source`` is a public shorthand (resolved via PUBLIC_MARKETPLACE_REGISTRY),
    an explicit ``owner/repo``, a git URL, or a remote marketplace.json URL.
    ``version`` is ``latest`` (re-pull newest) or a pinned tag/SHA/branch.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    source: str
    private: bool = False
    plugins: list[str] = Field(default_factory=list)
    version: str = "latest"

    @field_validator("source")
    @classmethod
    def _source_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("plugin source must not be empty")
        return v.strip()


class TaskConfig(BaseModel):
    """Per-run task content used only in config-prompt mode (no tasks table)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    prompt: str = ""
    system_prompt: str = ""


class OutputConfig(BaseModel):
    """Defaults for agent-produced output tables (spec §2.6)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    default_incremental: bool = False


class Configuration(BaseModel):
    """Top-level component configuration (spec §5.1).

    Every field maps to a ``ClaudeAgentOptions`` option or a subprocess ``env``
    var. ``#anthropic_key`` is the only required field, so the partial
    instantiation used by the ``testConnection`` sync action works with just it.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # --- secrets (arrive decrypted at runtime; never logged) ---
    anthropic_key: str = Field(alias="#anthropic_key")
    github_token: str = Field(alias="#github_token", default="")

    # --- runtime SDK/CLI version (spec §2.10) ---
    sdk_version: str = "pinned"
    sdk_version_on_failure: SdkVersionOnFailure = SdkVersionOnFailure.fail

    # --- model & loop controls ---
    model: Model = Model.opus_4_8
    fallback_model: Model | None = None
    max_turns: int = 20
    max_budget_usd: float = 10.0
    effort: Effort | None = None

    # --- permissions & tools ---
    permission_mode: PermissionMode = PermissionMode.dont_ask
    allowed_tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)

    # --- prompts & settings passthrough ---
    system_prompt: str = ""
    settings_json: dict | str | None = None
    setting_sources: list[Literal["user", "project", "local"]] = Field(default_factory=list)

    # --- MCP servers & plugins ---
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)
    plugins: list[PluginEntry] = Field(default_factory=list)

    # --- toggles ---
    github_enabled: bool = False
    workspace_input_files: bool = False

    # --- output behaviour ---
    output: OutputConfig = Field(default_factory=OutputConfig)

    # --- task selection / config-prompt-mode task ---
    task_id_filter: str | list[str] | None = None
    task: TaskConfig = Field(default_factory=TaskConfig)

    # --- intent contract scope (spec §10) ---
    operates_on: str | None = None
    """Optional ``org/repo`` target the agent operates on; used by the Advocate to narrow
    the contract destination to a specific GitHub repo. When None, the contract scope is
    broad (api.github.com with no repo restriction). See spec §10."""

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as e:
            error_messages = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Configuration validation error: {', '.join(error_messages)}") from e

    _coerce_settings_json = field_validator("settings_json", mode="before")(_coerce_empty_object)

    @model_validator(mode="before")
    @classmethod
    def _flatten_sections(cls, data):
        """Accept BOTH the flat config shape and the new sectioned shape.

        The sectioned configSchema groups fields under display-only wrapper
        objects (``connection``, ``model_budget``, ``advanced``, ...). Those
        wrappers are layout only — the model is flat — so lift each wrapper's
        contents up to the root before normal validation. The historical flat
        config (no wrappers) is left untouched, so every existing config,
        datadir fixture and VCR cassette still parses unchanged.

        An explicit root-level value always wins over a wrapper's value, so a
        hand-edited mix of both shapes is deterministic rather than ambiguous.
        """
        if not isinstance(data, dict):
            return data
        if not any(key in data for key in _SECTION_WRAPPERS):
            return data
        flattened = {k: v for k, v in data.items() if k not in _SECTION_WRAPPERS}
        for wrapper in _SECTION_WRAPPERS:
            section = data.get(wrapper)
            # The job runtime rewrites an empty {} to []; treat any non-dict
            # (or absent) wrapper as empty and skip it.
            if not isinstance(section, dict):
                continue
            for field_name, value in section.items():
                flattened.setdefault(field_name, value)
        return flattened

    @field_validator("permission_mode", mode="before")
    @classmethod
    def _reject_prompting_modes(cls, v):
        """A hand-edited config may carry a prompting mode — reject it loudly.

        The Enum already restricts the UI, but a raw string from a hand-edited
        config bypasses the dropdown; surface a clear error rather than hang.
        """
        if isinstance(v, str) and v not in NON_PROMPTING_PERMISSION_MODES:
            raise ValueError(
                f"permission_mode '{v}' prompts for approval and would hang a headless run; "
                f"choose one of {sorted(NON_PROMPTING_PERMISSION_MODES)}"
            )
        return v

    @field_validator("task_id_filter", mode="before")
    @classmethod
    def _normalise_task_filter(cls, v):
        """Normalise to ``None`` (all rows) or a non-empty list of task_ids.

        The UI surfaces this as a free-text field, so a single string may carry
        a comma-separated list (``"a, b"``); split it. A native list is taken
        as-is. Matching is exact string equality per spec §2.3.1.
        """
        if v is None:
            return None
        if isinstance(v, str):
            cleaned = [part.strip() for part in v.split(",") if part.strip()]
            return cleaned or None
        if isinstance(v, list):
            cleaned = [str(item).strip() for item in v if str(item).strip()]
            return cleaned or None
        return v

    @model_validator(mode="after")
    def _private_plugins_need_token(self) -> Configuration:
        """A private plugin source requires ``#github_token`` (spec §2.8)."""
        if not self.github_token:
            private = [p.source for p in self.plugins if p.private]
            if private:
                raise UserException(
                    f"Private plugin source(s) {private} require #github_token to be set in the configuration."
                )
        return self

    def selected_task_ids(self) -> list[str] | None:
        """Return the explicit task_id filter, or ``None`` to keep all rows."""
        return self.task_id_filter if isinstance(self.task_id_filter, list) else None

    def effective_budget(self, task_budget: float | None) -> float:
        """Clamp a per-task budget to the config-level ceiling (spec §2.7)."""
        if task_budget is None:
            return self.max_budget_usd
        return min(task_budget, self.max_budget_usd)

    def log_safe_summary(self) -> dict:
        """A dict safe to log — no secret values, only their presence."""
        return {
            "model": self.model.value,
            "fallback_model": self.fallback_model.value if self.fallback_model else None,
            "effort": self.effort.value if self.effort else None,
            "permission_mode": self.permission_mode.value,
            "max_turns": self.max_turns,
            "max_budget_usd": self.max_budget_usd,
            "sdk_version": self.sdk_version,
            "mcp_server_count": len(self.mcp_servers),
            "plugin_count": len(self.plugins),
            "github_enabled": self.github_enabled,
            "has_github_token": bool(self.github_token),
        }
