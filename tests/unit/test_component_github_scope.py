"""Unit tests for Component._build_github_allowed_destinations (multi-repo/org-wildcard scoping)."""

from component import Component
from configuration import Configuration


def _cfg(**overrides):
    data = {"#anthropic_key": "KEY_NAME_ONLY"}
    data.update(overrides)
    return Configuration(**data)


def test_github_disabled_yields_no_destinations():
    cfg = _cfg(github_enabled=False)
    assert Component._build_github_allowed_destinations(cfg) == []


def test_single_repo_yields_one_destination():
    cfg = _cfg(github_enabled=True, operates_on=["org/repo-X"])
    assert Component._build_github_allowed_destinations(cfg) == ["/repos/org/repo-X"]


def test_multiple_repos_yield_multiple_destinations():
    cfg = _cfg(github_enabled=True, operates_on=["org/repo-X", "org/repo-Y"])
    assert Component._build_github_allowed_destinations(cfg) == ["/repos/org/repo-X", "/repos/org/repo-Y"]


def test_org_wildcard_yields_org_only_destination():
    cfg = _cfg(github_enabled=True, operates_on=["org/*"])
    assert Component._build_github_allowed_destinations(cfg) == ["/repos/org"]


def test_mixed_repos_and_wildcard():
    cfg = _cfg(github_enabled=True, operates_on=["org/repo-X", "other-org/*"])
    assert Component._build_github_allowed_destinations(cfg) == ["/repos/org/repo-X", "/repos/other-org"]
