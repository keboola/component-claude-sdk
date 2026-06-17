"""Unit tests for the Pydantic Configuration model."""

import pytest
from keboola.component.exceptions import UserException

from configuration import (
    Configuration,
    Effort,
    McpRemoteServer,
    McpStdioServer,
    Model,
    PermissionMode,
)


def _base(**overrides):
    data = {"#anthropic_key": "KEY_NAME_ONLY"}
    data.update(overrides)
    return data


def test_minimal_config_parses_with_defaults():
    cfg = Configuration(**_base())
    assert cfg.anthropic_key == "KEY_NAME_ONLY"
    assert cfg.model == Model.opus_4_8
    assert cfg.max_turns == 20
    assert cfg.max_budget_usd == 10.0
    assert cfg.permission_mode == PermissionMode.dont_ask
    assert cfg.sdk_version == "pinned"
    assert cfg.task_id_filter is None


def test_missing_anthropic_key_raises_user_exception():
    with pytest.raises(UserException) as exc:
        Configuration(print_hello=True)  # no #anthropic_key
    assert "#anthropic_key" in str(exc.value) or "anthropic_key" in str(exc.value)


def test_partial_config_for_test_connection():
    # The testConnection sync action instantiates from just the key.
    cfg = Configuration(**{"#anthropic_key": "KEY_NAME_ONLY"})
    assert cfg.anthropic_key == "KEY_NAME_ONLY"


def test_alias_roundtrip_github_token():
    cfg = Configuration(**_base(**{"#github_token": "GH_NAME_ONLY"}))
    assert cfg.github_token == "GH_NAME_ONLY"


def test_github_enabled_requires_operates_on():
    """HIGH-3: github_enabled without operates_on fails closed at config parse."""
    with pytest.raises(UserException) as exc:
        Configuration(**_base(github_enabled=True))
    assert "operates_on" in str(exc.value)


def test_github_enabled_rejects_malformed_operates_on():
    """operates_on must be 'org/repo' — a bare name (no slash) is rejected."""
    with pytest.raises(UserException) as exc:
        Configuration(**_base(github_enabled=True, operates_on="just-a-name"))
    assert "org/repo" in str(exc.value)


def test_github_enabled_with_valid_operates_on_parses():
    cfg = Configuration(**_base(github_enabled=True, operates_on="org/repo-X"))
    assert cfg.github_enabled is True
    assert cfg.operates_on == "org/repo-X"
    # Default writable-branch scope confines the agent to its own branches.
    assert cfg.writable_branches == ["agent/*"]


def test_operates_on_optional_when_github_disabled():
    """operates_on stays optional when GitHub is off."""
    cfg = Configuration(**_base())
    assert cfg.github_enabled is False
    assert cfg.operates_on is None


def test_prompting_permission_mode_rejected():
    with pytest.raises(UserException) as exc:
        Configuration(**_base(permission_mode="acceptEdits"))
    assert "permission_mode" in str(exc.value)


def test_non_prompting_permission_mode_accepted():
    cfg = Configuration(**_base(permission_mode="bypassPermissions"))
    assert cfg.permission_mode == PermissionMode.bypass_permissions


def test_task_id_filter_string_normalised_to_list():
    cfg = Configuration(**_base(task_id_filter="sync-orders"))
    assert cfg.selected_task_ids() == ["sync-orders"]


def test_task_id_filter_comma_separated_string_split():
    cfg = Configuration(**_base(task_id_filter="sync-orders, summarize ,"))
    assert cfg.selected_task_ids() == ["sync-orders", "summarize"]


def test_task_id_filter_empty_string_is_none():
    cfg = Configuration(**_base(task_id_filter="   "))
    assert cfg.selected_task_ids() is None


def test_task_id_filter_list_cleaned():
    cfg = Configuration(**_base(task_id_filter=["a", " b ", ""]))
    assert cfg.selected_task_ids() == ["a", "b"]


def test_mcp_stdio_server_discriminated():
    cfg = Configuration(
        **_base(
            mcp_servers=[
                {"type": "stdio", "name": "kbc", "command": "uvx", "args": ["keboola-mcp-server"]},
            ]
        )
    )
    assert isinstance(cfg.mcp_servers[0], McpStdioServer)
    assert cfg.mcp_servers[0].command == "uvx"


def test_mcp_http_server_discriminated():
    cfg = Configuration(
        **_base(
            mcp_servers=[
                {"type": "http", "name": "remote", "url": "https://example.com/mcp"},
            ]
        )
    )
    assert isinstance(cfg.mcp_servers[0], McpRemoteServer)
    assert cfg.mcp_servers[0].url == "https://example.com/mcp"


def test_mcp_stdio_empty_env_list_coerced_to_dict():
    """The job runtime rewrites empty {} -> []; an MCP env=[] must parse to {}."""
    cfg = Configuration(
        **_base(
            mcp_servers=[
                {"type": "stdio", "name": "kbc", "command": "uvx", "env": []},
            ]
        )
    )
    assert cfg.mcp_servers[0].env == {}


def test_mcp_remote_empty_headers_list_coerced_to_dict():
    cfg = Configuration(
        **_base(
            mcp_servers=[
                {"type": "http", "name": "remote", "url": "https://example.com/mcp", "headers": []},
            ]
        )
    )
    assert cfg.mcp_servers[0].headers == {}


def test_mcp_populated_env_dict_still_parses():
    cfg = Configuration(
        **_base(
            mcp_servers=[
                {"type": "stdio", "name": "kbc", "command": "uvx", "env": {"TOKEN": "abc"}},
            ]
        )
    )
    assert cfg.mcp_servers[0].env == {"TOKEN": "abc"}


def test_mcp_non_empty_list_env_still_rejected():
    """Only the EMPTY list is coerced; a populated list must still fail loudly."""
    with pytest.raises(UserException) as exc:
        Configuration(
            **_base(
                mcp_servers=[
                    {"type": "stdio", "name": "kbc", "command": "uvx", "env": ["A", "B"]},
                ]
            )
        )
    assert "env" in str(exc.value)


def test_private_plugin_without_token_raises():
    with pytest.raises(UserException) as exc:
        Configuration(**_base(plugins=[{"source": "keboola/cf-claude-code-kit", "private": True, "plugins": ["x"]}]))
    assert "github_token" in str(exc.value)


def test_private_plugin_with_token_ok():
    cfg = Configuration(
        **_base(
            **{"#github_token": "GH_NAME_ONLY"},
            plugins=[{"source": "keboola/cf-claude-code-kit", "private": True, "version": "v1.4.0"}],
        )
    )
    assert cfg.plugins[0].private is True
    assert cfg.plugins[0].version == "v1.4.0"


def test_public_plugin_defaults_latest():
    cfg = Configuration(**_base(plugins=[{"source": "superpowers"}]))
    assert cfg.plugins[0].version == "latest"
    assert cfg.plugins[0].private is False


def test_effective_budget_clamps_to_ceiling():
    cfg = Configuration(**_base(max_budget_usd=10.0))
    assert cfg.effective_budget(25.0) == 10.0
    assert cfg.effective_budget(3.0) == 3.0
    assert cfg.effective_budget(None) == 10.0


def test_log_safe_summary_has_no_secret_values():
    cfg = Configuration(**_base(**{"#github_token": "GH_NAME_ONLY"}))
    summary = cfg.log_safe_summary()
    assert "KEY_NAME_ONLY" not in str(summary)
    assert "GH_NAME_ONLY" not in str(summary)
    assert summary["has_github_token"] is True


def test_log_safe_summary_includes_effort_and_fallback_model():
    cfg = Configuration(**_base(effort="high", fallback_model="claude-haiku-4-5"))
    summary = cfg.log_safe_summary()
    assert summary["effort"] == "high"
    assert summary["fallback_model"] == "claude-haiku-4-5"


def test_log_safe_summary_effort_and_fallback_default_none():
    cfg = Configuration(**_base())
    summary = cfg.log_safe_summary()
    assert summary["effort"] is None
    assert summary["fallback_model"] is None


# --- sectioned (nested) configSchema shape -> flattens to the same model ---


def test_nested_section_shape_minimal_parses():
    """The new sectioned UI nests the key under a 'connection' wrapper."""
    cfg = Configuration(connection={"#anthropic_key": "KEY_NAME_ONLY"})
    assert cfg.anthropic_key == "KEY_NAME_ONLY"
    assert cfg.model == Model.opus_4_8


def test_nested_section_shape_full_round_trip():
    """A fully sectioned config flattens to the same model as the flat shape."""
    cfg = Configuration(
        connection={"#anthropic_key": "KEY_NAME_ONLY"},
        model_budget={
            "model": "claude-sonnet-4-6",
            "max_turns": 5,
            "max_budget_usd": 2.0,
            "effort": "high",
        },
        permissions={
            "permission_mode": "bypassPermissions",
            "allowed_tools": ["Read", "Bash(git *)"],
        },
        github={"github_enabled": True, "#github_token": "GH_NAME_ONLY", "operates_on": "org/repo-X"},
        task_output={
            "task": {"prompt": "do the thing"},
            "task_id_filter": "a, b",
            "output": {"default_incremental": True},
        },
        mcp_section={
            "mcp_servers": [{"type": "stdio", "name": "kbc", "command": "uvx"}],
        },
        plugins_section={"plugins": [{"source": "superpowers"}]},
        advanced={
            "system_prompt": "be terse",
            "setting_sources": ["project"],
            "sdk_version": "0.2.105",
            "workspace_input_files": True,
        },
    )
    assert cfg.anthropic_key == "KEY_NAME_ONLY"
    assert cfg.github_token == "GH_NAME_ONLY"
    assert cfg.model == Model.sonnet_4_6
    assert cfg.max_turns == 5
    assert cfg.max_budget_usd == 2.0
    assert cfg.effort == Effort.high
    assert cfg.permission_mode == PermissionMode.bypass_permissions
    assert cfg.allowed_tools == ["Read", "Bash(git *)"]
    assert cfg.github_enabled is True
    assert cfg.task.prompt == "do the thing"
    assert cfg.selected_task_ids() == ["a", "b"]
    assert cfg.output.default_incremental is True
    assert isinstance(cfg.mcp_servers[0], McpStdioServer)
    assert cfg.plugins[0].source == "superpowers"
    assert cfg.system_prompt == "be terse"
    assert cfg.setting_sources == ["project"]
    assert cfg.sdk_version == "0.2.105"
    assert cfg.workspace_input_files is True


def test_nested_empty_section_list_artifact_ignored():
    """The job runtime rewrites an empty {} section to []; it must be ignored."""
    cfg = Configuration(
        connection={"#anthropic_key": "KEY_NAME_ONLY"},
        model_budget=[],
        advanced=[],
    )
    assert cfg.anthropic_key == "KEY_NAME_ONLY"
    assert cfg.model == Model.opus_4_8


def test_root_level_value_wins_over_section_wrapper():
    """An explicit root value beats a wrapper value (deterministic mixed shape)."""
    cfg = Configuration(
        **{"#anthropic_key": "KEY_NAME_ONLY"},
        model_budget={"max_turns": 99},
        max_turns=7,
    )
    assert cfg.max_turns == 7


def test_flat_shape_still_parses_alongside_sections():
    """The historical flat shape (no wrappers) is left fully untouched."""
    cfg = Configuration(**_base(max_turns=3, github_enabled=True, operates_on="org/repo-X"))
    assert cfg.max_turns == 3
    assert cfg.github_enabled is True


def test_system_prompt_lifts_from_either_section():
    """system_prompt moved Advanced -> Task,Prompt&Output; both placements lift.

    The flatten validator lifts any section wrapper, so a config saved with
    system_prompt under the new 'task_output' section AND one saved with it
    under the old 'advanced' section both reach the same config field.
    """
    new_placement = Configuration(
        connection={"#anthropic_key": "KEY_NAME_ONLY"},
        task_output={"system_prompt": "role-new", "task": {"prompt": "p"}},
    )
    assert new_placement.system_prompt == "role-new"

    old_placement = Configuration(
        connection={"#anthropic_key": "KEY_NAME_ONLY"},
        advanced={"system_prompt": "role-legacy"},
    )
    assert old_placement.system_prompt == "role-legacy"
