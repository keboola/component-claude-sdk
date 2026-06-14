"""Unit tests for OutputWriter — promoting agent /tmp/outputs to tables."""

import csv
import json
import os

import pytest
from keboola.component.exceptions import UserException

from output_writer import OutputWriter


class FakeComponent:
    def __init__(self, root):
        self.tables_path = os.path.join(root, "out", "tables")
        os.makedirs(self.tables_path, exist_ok=True)
        self.manifests = []

    def create_out_table_definition(self, name, schema=None, primary_key=None, incremental=None,
                                    has_header=None, **kwargs):
        return _TableDef(os.path.join(self.tables_path, name), name, schema, primary_key,
                         incremental, has_header)

    def write_manifest(self, definition):
        self.manifests.append(definition)


class _TableDef:
    def __init__(self, full_path, name, schema, pk, incremental, has_header):
        self.full_path = full_path
        self.name = name
        self.schema = schema
        self.primary_key = pk
        self.incremental = incremental
        self.has_header = has_header
        self.destination = ""  # never set by the writer


def _agent_csv(agent_dir, name, header, rows, meta=None):
    os.makedirs(agent_dir, exist_ok=True)
    path = os.path.join(agent_dir, name)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
    if meta is not None:
        with open(path + ".meta.json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
    return path


def test_promotes_plain_csv(tmp_path):
    comp = FakeComponent(str(tmp_path))
    agent_dir = str(tmp_path / "agent_out")
    _agent_csv(agent_dir, "foo.csv", ["id", "name"], [["1", "a"]])
    writer = OutputWriter(comp, agent_output_dir=agent_dir)

    promoted = writer.promote(default_incremental=False)
    assert promoted == ["foo.csv"]

    out_csv = os.path.join(comp.tables_path, "foo.csv")
    rows = list(csv.DictReader(open(out_csv, encoding="utf-8")))
    assert rows == [{"id": "1", "name": "a"}]

    manifest = comp.manifests[0]
    assert manifest.has_header is True
    assert manifest.incremental is False
    assert manifest.destination == ""  # defaultBucket overrides; never set


def test_meta_declares_incremental_and_pk(tmp_path):
    comp = FakeComponent(str(tmp_path))
    agent_dir = str(tmp_path / "agent_out")
    _agent_csv(agent_dir, "orders.csv", ["id", "amount"], [["1", "10"]],
               meta={"incremental": True, "primary_key": ["id"]})
    writer = OutputWriter(comp, agent_output_dir=agent_dir)
    writer.promote()
    manifest = comp.manifests[0]
    assert manifest.incremental is True
    assert manifest.primary_key == ["id"]


def test_incremental_without_pk_raises(tmp_path):
    comp = FakeComponent(str(tmp_path))
    agent_dir = str(tmp_path / "agent_out")
    _agent_csv(agent_dir, "bad.csv", ["a"], [["1"]], meta={"incremental": True})
    writer = OutputWriter(comp, agent_output_dir=agent_dir)
    with pytest.raises(UserException) as exc:
        writer.promote()
    assert "primary_key" in str(exc.value)


def test_default_incremental_applies_when_no_meta(tmp_path):
    comp = FakeComponent(str(tmp_path))
    agent_dir = str(tmp_path / "agent_out")
    _agent_csv(agent_dir, "t.csv", ["pk", "v"], [["1", "x"]],
               meta={"primary_key": ["pk"]})  # no explicit incremental flag
    writer = OutputWriter(comp, agent_output_dir=agent_dir)
    writer.promote(default_incremental=True)
    assert comp.manifests[0].incremental is True


def test_missing_dir_is_noop(tmp_path):
    comp = FakeComponent(str(tmp_path))
    writer = OutputWriter(comp, agent_output_dir=str(tmp_path / "does_not_exist"))
    assert writer.promote() == []


def test_empty_csv_skipped(tmp_path):
    comp = FakeComponent(str(tmp_path))
    agent_dir = str(tmp_path / "agent_out")
    os.makedirs(agent_dir, exist_ok=True)
    open(os.path.join(agent_dir, "empty.csv"), "w").close()
    writer = OutputWriter(comp, agent_output_dir=agent_dir)
    writer.promote()
    assert comp.manifests == []


def test_ensure_dir_creates_scratch(tmp_path):
    comp = FakeComponent(str(tmp_path))
    agent_dir = str(tmp_path / "scratch")
    writer = OutputWriter(comp, agent_output_dir=agent_dir)
    assert writer.ensure_dir() == agent_dir
    assert os.path.isdir(agent_dir)
