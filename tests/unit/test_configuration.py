"""Unit tests for the Pydantic Configuration model."""

import pytest
from keboola.component.exceptions import UserException

from configuration import (
    Configuration,
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


def test_private_plugin_without_token_raises():
    with pytest.raises(UserException) as exc:
        Configuration(
            **_base(plugins=[{"source": "keboola/cf-claude-code-kit", "private": True, "plugins": ["x"]}])
        )
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
