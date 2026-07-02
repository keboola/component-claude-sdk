"""Shared fixtures for the datadir functional tests.

These tests exercise the component **end-to-end through the Keboola ``/data``
contract** — real ``config.json`` parsing, real input tables, and the real
output mapping (CSV + ``.manifest`` files written under ``/data/out/``). They
mirror what ``keboola.datadirtest`` checks (config parsing + output/manifest
correctness) while keeping the failure-path exit codes directly assertable.

The Claude agent loop runs the ``claude`` CLI as a SUBPROCESS that makes its own
outbound HTTPS, so in-process VCR cannot capture it (spec §7). The single
``ClaudeRunner._query`` async-generator seam is therefore mocked to yield a
canned, typed SDK message stream — no network, no subprocess. (The one
in-process Anthropic HTTP call, ``testConnection``, is covered by the VCR
functional tests under ``tests/functional/``.)
"""

from __future__ import annotations

import csv
import json
import os
import shutil

import pytest

# /tmp scratch dirs the component writes to (module-level constants in src/).
# Both live under /tmp because the production image is read-only there; the
# tests point them at a per-test tmp_path so runs never collide.
import component as component_module
import output_writer as output_writer_module
from component import Component


@pytest.fixture
def datadir(tmp_path, monkeypatch):
    """Build a clean Keboola ``/data`` tree and point the component at it.

    Returns a small builder object exposing ``config(...)``, ``input_table(...)``,
    ``out_tables_dir`` / ``out_files_dir`` and the resolved ``/data`` path.
    Also redirects the component's ``/tmp`` workspace + agent-output scratch dirs
    into ``tmp_path`` so concurrent/sequential tests stay isolated.
    """
    data_dir = tmp_path / "data"
    for sub in ("in/tables", "in/files", "out/tables", "out/files"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KBC_DATADIR", str(data_dir))

    # Redirect the read-only-image /tmp scratch dirs into the test's tmp_path so
    # sequential tests never collide on the shared /tmp paths.
    workspace = str(tmp_path / "claude-workspace")
    agent_out = str(tmp_path / "agent-outputs")
    monkeypatch.setattr(component_module, "WORKSPACE_DIR", workspace)

    return _DataDir(data_dir, workspace, agent_out)


class _DataDir:
    def __init__(self, path, workspace, agent_out):
        self.path = path
        self.workspace = workspace
        self.agent_out = agent_out

    def build_component(self):
        """Construct the Component and wire its scratch dirs into tmp_path.

        ``OutputWriter``/``ClaudeRunner`` capture the ``/tmp`` constants at
        construction time, so we re-point the instances after ``__init__`` to
        keep each test isolated from the global ``/tmp`` scratch dirs.
        """
        comp = Component()
        comp._output_writer._agent_output_dir = self.agent_out
        comp._runner._workspace_dir = self.workspace
        return comp

    def config(self, parameters: dict) -> None:
        (self.path / "config.json").write_text(json.dumps({"parameters": parameters}), encoding="utf-8")

    def input_table(self, name: str, header: list[str], rows: list[list[str]]) -> None:
        """Write an input table CSV + manifest with ``destination = name``."""
        table_path = self.path / "in" / "tables" / f"{name}.csv"
        with open(table_path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            writer.writerows(rows)
        manifest = {"destination": name, "columns": header}
        (self.path / "in" / "tables" / f"{name}.csv.manifest").write_text(json.dumps(manifest), encoding="utf-8")

    @property
    def out_tables_dir(self):
        return self.path / "out" / "tables"

    @property
    def out_files_dir(self):
        return self.path / "out" / "files"

    def read_csv(self, name: str) -> list[dict]:
        with open(self.out_tables_dir / name, encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def read_manifest(self, name: str) -> dict:
        with open(self.out_tables_dir / f"{name}.manifest", encoding="utf-8") as fh:
            return json.load(fh)


def canned_stream(blocks=("done",), *, subtype="success", is_error=False, write_outputs=None, agent_out_dir=None):
    """Return a replacement for ``ClaudeRunner._query`` yielding a canned stream.

    ``write_outputs`` maps ``filename -> (header, rows[, meta_dict])``; when set,
    the stream writes those CSVs (and optional ``.meta.json`` sidecars) into
    ``agent_out_dir`` mid-stream, emulating the agent producing output tables
    that ``OutputWriter.promote()`` then lands as Keboola tables.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

    def _query(self, prompt, options):
        async def gen():
            yield SystemMessage(subtype="init", data={"session_id": "sess-test"})
            if write_outputs:
                _write_agent_outputs(agent_out_dir, write_outputs)
            for text in blocks:
                yield AssistantMessage(content=[TextBlock(text=text)], model="claude-opus-4-8", session_id="sess-test")
            yield ResultMessage(
                subtype=subtype,
                duration_ms=120,
                duration_api_ms=90,
                is_error=is_error,
                num_turns=1,
                session_id="sess-test",
                total_cost_usd=0.0,
                result="ok" if not is_error else "failed",
            )

        return gen()

    return _query


def _write_agent_outputs(agent_out_dir, outputs) -> None:
    os.makedirs(agent_out_dir, exist_ok=True)
    for filename, spec in outputs.items():
        header, rows = spec[0], spec[1]
        meta = spec[2] if len(spec) > 2 else None
        csv_path = os.path.join(agent_out_dir, filename)
        with open(csv_path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            writer.writerows(rows)
        if meta is not None:
            with open(csv_path + ".meta.json", "w", encoding="utf-8") as fh:
                json.dump(meta, fh)


def install(monkeypatch, query_fn) -> None:
    """Install the canned-stream seam on ``ClaudeRunner._query`` (class level)."""
    from claude_runner import ClaudeRunner

    monkeypatch.setattr(ClaudeRunner, "_query", query_fn)


@pytest.fixture(autouse=True)
def _clean_scratch():
    """Defensive: clear the real default /tmp scratch dirs around each test."""
    for path in (component_module.WORKSPACE_DIR, output_writer_module.AGENT_OUTPUT_DIR):
        shutil.rmtree(path, ignore_errors=True)
    yield
