"""Promotes agent-produced tables to Keboola output mapping (spec §2.6, §4.6).

**Agent -> table hand-off convention (LOCKED here, documented in the README and
the prompt envelope):**

The agent writes its final output tables as **headered CSV files** into a known
scratch directory ``/tmp/outputs/``. For each ``<name>.csv`` the agent MAY drop a
sidecar ``<name>.csv.meta.json`` declaring its intent:

    {"incremental": true, "primary_key": ["id"]}

After the agent loop, ``OutputWriter.promote()`` scans ``/tmp/outputs/``, and for
every ``*.csv`` writes ``/data/out/tables/<name>.csv`` + a manifest with a native
``schema`` (all-STRING for agent tables — acceptable per spec §2.6), ``has_header``
True, and the PK/incremental from the sidecar (falling back to the config default
``output.default_incremental``). ``destination`` is never set — ``defaultBucket``
overrides it. Scratch lives in ``/tmp`` so it never becomes a spurious table.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import shutil
from typing import Any

from keboola.component.base import ComponentBase
from keboola.component.dao import BaseType, ColumnDefinition
from keboola.component.exceptions import UserException

AGENT_OUTPUT_DIR = "/tmp/outputs"  # noqa: S108 — /tmp is the only writable path in the read-only image
META_SUFFIX = ".meta.json"


class OutputWriter:
    """Reconciles the agent's filesystem outputs into manifested tables."""

    def __init__(self, component: ComponentBase, agent_output_dir: str = AGENT_OUTPUT_DIR) -> None:
        self._component = component
        self._agent_output_dir = agent_output_dir

    @property
    def agent_output_dir(self) -> str:
        return self._agent_output_dir

    def ensure_dir(self) -> str:
        """Create the agent output scratch dir and return its path."""
        os.makedirs(self._agent_output_dir, exist_ok=True)
        return self._agent_output_dir

    def promote(self, default_incremental: bool = False) -> list[str]:
        """Promote every ``*.csv`` in the agent output dir to an output table.

        Returns the list of promoted table names.
        """
        if not os.path.isdir(self._agent_output_dir):
            return []
        promoted: list[str] = []
        for filename in sorted(os.listdir(self._agent_output_dir)):
            if not filename.endswith(".csv"):
                continue
            self._promote_one(filename, default_incremental)
            promoted.append(filename)
        if promoted:
            logging.info("Promoted %d agent output table(s): %s", len(promoted), ", ".join(promoted))
        return promoted

    def _promote_one(self, filename: str, default_incremental: bool) -> None:
        src = os.path.join(self._agent_output_dir, filename)
        columns = self._read_header(src)
        if not columns:
            logging.warning("Skipping agent output '%s': empty or headerless CSV.", filename)
            return
        meta = self._read_meta(src)
        # Only honour a real JSON boolean — a truthy string like "false" must not
        # silently flip the table to incremental; fall back to the default instead.
        incremental_meta = meta.get("incremental")
        incremental = incremental_meta if isinstance(incremental_meta, bool) else default_incremental
        # primary_key must be a list of column names; a bare string would be
        # iterated character-by-character, silently yielding a wrong/empty PK.
        # Coerce a lone string to a one-element list and ignore any other shape.
        declared_pk = meta.get("primary_key", [])
        if isinstance(declared_pk, str):
            declared_pk = [declared_pk]
        elif not isinstance(declared_pk, list):
            declared_pk = []
        primary_key = [c for c in declared_pk if c in columns]
        if incremental and not primary_key:
            raise UserException(
                f"Agent output table '{filename}' is marked incremental but declares no primary_key "
                f"(unbounded append). Add a primary_key to its .meta.json or make it non-incremental."
            )

        schema = {
            col: ColumnDefinition(data_types=BaseType.string(), primary_key=col in primary_key) for col in columns
        }
        table_def = self._component.create_out_table_definition(
            filename,
            schema=schema,
            primary_key=primary_key,
            incremental=incremental,
            has_header=True,
        )
        shutil.copyfile(src, table_def.full_path)
        self._component.write_manifest(table_def)

    @staticmethod
    def _read_header(path: str) -> list[str]:
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                reader = csv.reader(fh)
                return next(reader, [])
        except OSError as exc:  # pragma: no cover - filesystem edge
            logging.warning("Could not read agent output '%s': %s", path, exc)
            return []

    @staticmethod
    def _read_meta(csv_path: str) -> dict[str, Any]:
        meta_path = csv_path + META_SUFFIX
        if not os.path.isfile(meta_path):
            return {}
        try:
            with open(meta_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Ignoring invalid output meta '%s': %s", meta_path, exc)
            return {}
