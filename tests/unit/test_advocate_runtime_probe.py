"""Unit tests for the advocate runtime egress probe.

Covers:
  - probe gate: normal runs are unaffected when flag is absent
  - probe gate: flag present short-circuits into the probe path
  - probe module: run_probe() writes the expected CSV + manifest
  - probe module: check_env_context always passes
  - probe module: recommendation logic (iptables > netns > neither)
"""

from __future__ import annotations

import csv
import json
import os

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_datadir(tmp_path, parameters):
    data_dir = tmp_path / "data"
    (data_dir / "in" / "tables").mkdir(parents=True)
    (data_dir / "in" / "files").mkdir(parents=True)
    (data_dir / "out" / "tables").mkdir(parents=True)
    (data_dir / "out" / "files").mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({"parameters": parameters}), encoding="utf-8")
    return str(data_dir)


# ---------------------------------------------------------------------------
# Gate tests — component.py
# ---------------------------------------------------------------------------


def test_probe_flag_absent_runs_normally(tmp_path, monkeypatch):
    """When __advocate_runtime_probe is absent, component.run() executes the normal task loop."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

    from component import Component

    data_dir = _make_datadir(
        tmp_path,
        {"#anthropic_key": "KEY_NAME_ONLY", "task": {"prompt": "hello"}},
    )
    monkeypatch.setenv("KBC_DATADIR", data_dir)
    comp = Component()

    async def _canned_stream(prompt, options):
        yield SystemMessage(subtype="init", data={"session_id": "s"})
        yield AssistantMessage(content=[TextBlock(text="hi")], model="claude-opus-4-8", session_id="s")
        yield ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=5,
            is_error=False,
            num_turns=1,
            session_id="s",
            total_cost_usd=0.0,
            result="ok",
        )

    monkeypatch.setattr(comp._runner, "_query", _canned_stream)

    # Must NOT call run_probe
    probe_called = []
    monkeypatch.setattr("component.run_probe", lambda *a, **kw: probe_called.append(True) or {})

    comp.run()

    assert not probe_called, "run_probe must not be called when flag is absent"
    # Normal transcript tables exist
    assert os.path.isfile(os.path.join(data_dir, "out", "tables", "claude_runs.csv"))


def test_probe_flag_true_short_circuits(tmp_path, monkeypatch):
    """When __advocate_runtime_probe is true, run() delegates to _run_advocate_probe
    and does NOT enter the normal task loop."""
    from component import Component

    data_dir = _make_datadir(
        tmp_path,
        {
            "#anthropic_key": "KEY_NAME_ONLY",
            "__advocate_runtime_probe": True,
            "task": {"prompt": "should not run"},
        },
    )
    monkeypatch.setenv("KBC_DATADIR", data_dir)
    comp = Component()

    sdk_called = []
    monkeypatch.setattr(comp._runner, "_query", lambda *a, **kw: sdk_called.append(True))

    probe_results = []

    def _fake_probe(out_tables_dir):
        probe_results.append(out_tables_dir)
        return {"recommendation": "iptables_owner_match"}

    monkeypatch.setattr("component.run_probe", _fake_probe)

    comp.run()

    assert not sdk_called, "SDK must not be called when probe mode is active"
    assert probe_results, "run_probe must have been called"
    # out_tables_dir points to the data dir tables path
    assert "out" in probe_results[0]
    assert "tables" in probe_results[0]


def test_probe_flag_false_runs_normally(tmp_path, monkeypatch):
    """Falsy __advocate_runtime_probe (explicit False) still runs normally."""
    from claude_agent_sdk import ResultMessage, SystemMessage

    from component import Component

    data_dir = _make_datadir(
        tmp_path,
        {
            "#anthropic_key": "KEY_NAME_ONLY",
            "__advocate_runtime_probe": False,
            "task": {"prompt": "hi"},
        },
    )
    monkeypatch.setenv("KBC_DATADIR", data_dir)
    comp = Component()

    async def _canned(prompt, options):
        yield SystemMessage(subtype="init", data={"session_id": "s"})
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            total_cost_usd=0.0,
            result="ok",
        )

    monkeypatch.setattr(comp._runner, "_query", _canned)

    probe_called = []
    monkeypatch.setattr("component.run_probe", lambda *a, **kw: probe_called.append(True) or {})

    comp.run()
    assert not probe_called


# ---------------------------------------------------------------------------
# run_probe() output shape
# ---------------------------------------------------------------------------


def test_run_probe_writes_csv_and_manifest(tmp_path, monkeypatch):
    """run_probe() writes advocate_runtime_probe.csv + .manifest with correct schema."""
    import advocate.runtime_probe as rp

    # Stub every check to a fast PASS so the test is deterministic everywhere
    def _fake_check():
        return True, "stubbed"

    for check_name, _ in rp._CHECKS:
        pass  # just enumerate to confirm the list is non-empty

    # Monkeypatch all check functions to return fast stubs
    stubbed = [(name, _fake_check) for name, _ in rp._CHECKS]
    monkeypatch.setattr(rp, "_CHECKS", stubbed)

    out_dir = str(tmp_path / "out" / "tables")
    summary = rp.run_probe(out_dir)

    table = os.path.join(out_dir, "advocate_runtime_probe.csv")
    manifest = table + ".manifest"

    assert os.path.isfile(table)
    assert os.path.isfile(manifest)

    rows = list(csv.DictReader(open(table, encoding="utf-8")))
    assert len(rows) == len(stubbed)
    assert set(rows[0].keys()) == {"check_name", "pass", "detail"}
    for row in rows:
        assert row["pass"] == "true"

    meta = json.loads(open(manifest, encoding="utf-8").read())
    assert meta["incremental"] is False
    assert "columns" in meta

    # Summary shape
    assert "recommendation" in summary
    assert "env" in summary
    assert "checks" in summary
    assert "timestamp" in summary


def test_run_probe_recommendation_iptables_preferred(tmp_path, monkeypatch):
    """When iptables_loopback_allow_external_block passes, recommendation=iptables_owner_match."""
    import advocate.runtime_probe as rp

    def _iptables_pass():
        return True, "ok"

    def _fail():
        return False, "nope"

    stubbed = [
        ("env_context", _fail),
        ("iptables_owner_match_cap", _fail),
        ("iptables_loopback_allow_external_block", _iptables_pass),
        ("unshare_clone_newnet", _fail),
        ("phase0_floor", _fail),
    ]
    monkeypatch.setattr(rp, "_CHECKS", stubbed)

    summary = rp.run_probe(str(tmp_path))
    assert summary["recommendation"] == "iptables_owner_match"


def test_run_probe_recommendation_netns_fallback(tmp_path, monkeypatch):
    """When iptables fails but unshare succeeds, recommendation=netns_fd_passing."""
    import advocate.runtime_probe as rp

    def _pass():
        return True, "ok"

    def _fail():
        return False, "nope"

    stubbed = [
        ("env_context", _fail),
        ("iptables_owner_match_cap", _fail),
        ("iptables_loopback_allow_external_block", _fail),
        ("unshare_clone_newnet", _pass),
        ("phase0_floor", _fail),
    ]
    monkeypatch.setattr(rp, "_CHECKS", stubbed)

    summary = rp.run_probe(str(tmp_path))
    assert summary["recommendation"] == "netns_fd_passing"


def test_run_probe_recommendation_neither(tmp_path, monkeypatch):
    """When both iptables and unshare fail, recommendation=neither_available."""
    import advocate.runtime_probe as rp

    def _fail():
        return False, "nope"

    stubbed = [(name, _fail) for name, _ in rp._CHECKS]
    monkeypatch.setattr(rp, "_CHECKS", stubbed)

    summary = rp.run_probe(str(tmp_path))
    assert summary["recommendation"] == "neither_available"


def test_check_env_context_always_passes():
    """check_env_context() always returns (True, json_string)."""
    from advocate.runtime_probe import check_env_context

    passed, detail = check_env_context()
    assert passed is True
    data = json.loads(detail)
    assert "machine" in data
    assert "euid" in data


def test_run_probe_tolerates_check_exception(tmp_path, monkeypatch):
    """If a check raises an exception, run_probe catches it as FAIL and continues."""
    import advocate.runtime_probe as rp

    def _explodes():
        raise RuntimeError("kaboom")

    def _fine():
        return True, "ok"

    stubbed = [("env_context", _explodes), ("iptables_owner_match_cap", _fine)]
    monkeypatch.setattr(rp, "_CHECKS", stubbed)

    summary = rp.run_probe(str(tmp_path))
    checks = summary["checks"]
    assert checks["env_context"]["pass"] is False
    assert "kaboom" in checks["env_context"]["detail"]
    assert checks["iptables_owner_match_cap"]["pass"] is True
