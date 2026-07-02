"""Session transcript sinks (spec §2.6.1).

Every run writes the SDK message stream via two complementary sinks:

1. **Structured tables** ``claude_sessions`` (one row per event, the verbatim
   line preserved in ``raw_json``) and ``claude_runs`` (one row per task) under
   ``/data/out/tables/`` — queryable in Storage, native ``schema`` manifests,
   incremental with PKs, and a real ``write_always=True``. **This is the
   always-on, failure-durable transcript of record** — it survives an exit-1 job.
2. **Raw JSONL file artifacts** under ``/data/out/files/`` — one line per SDK
   message, full fidelity (large tool payloads survive). Keboola file output
   mapping has **no** ``write_always`` (it is a ``TableDefinition``-only
   attribute), so this artifact is uploaded only on a **successful** job; it is
   the full-fidelity success-path convenience, not the durability guarantee.

The brief's "all the session JSONL lines, regardless of success/failure"
requirement is satisfied by sink 1 (every line lands in ``claude_sessions``
verbatim, write_always); sink 2 adds untruncated fidelity on the success path.

The writer is given the component (for the library's manifest/path machinery)
and the resolved SDK version + plugin refs (for ``claude_runs`` traceability).
"""

from __future__ import annotations

import csv
import dataclasses
import json
import logging
import os
from typing import Any

from keboola.component.base import ComponentBase
from keboola.component.dao import BaseType, ColumnDefinition

from claude_runner import ClaudeRunResult

SESSIONS_TABLE = "claude_sessions"
RUNS_TABLE = "claude_runs"
SESSION_TAGS = ["claude-sdk", "session-transcript"]


# PK columns are non-nullable; everything else is explicitly nullable so an
# empty cell (e.g. total_cost_usd / api_error_status when the SDK returns None)
# loads as NULL into the authoritative-typed column instead of failing the load.
def _string_col(primary_key: bool = False, nullable: bool = True) -> ColumnDefinition:
    return ColumnDefinition(
        data_types=BaseType.string(), primary_key=primary_key, nullable=not primary_key and nullable
    )


def _int_col(primary_key: bool = False, nullable: bool = True) -> ColumnDefinition:
    return ColumnDefinition(
        data_types=BaseType.integer(), primary_key=primary_key, nullable=not primary_key and nullable
    )


def _float_col(nullable: bool = True) -> ColumnDefinition:
    return ColumnDefinition(data_types=BaseType.float(), nullable=nullable)


# claude_sessions: one row per SDK message event (mostly STRING + numeric seq).
SESSIONS_SCHEMA: dict[str, ColumnDefinition] = {
    "task_id": _string_col(primary_key=True),
    "session_id": _string_col(primary_key=True),
    "seq": _int_col(primary_key=True),
    "ts": _string_col(),
    "type": _string_col(),
    "subtype": _string_col(),
    "role": _string_col(),
    "text": _string_col(),
    "tool_name": _string_col(),
    "tool_input_json": _string_col(),
    "tool_result_json": _string_col(),
    "is_error": _string_col(),
    "raw_json": _string_col(),
}

# claude_runs: one row per task with real numeric types for cost/turns/duration.
RUNS_SCHEMA: dict[str, ColumnDefinition] = {
    "task_id": _string_col(primary_key=True),
    "session_id": _string_col(primary_key=True),
    "success": _string_col(),
    "subtype": _string_col(),
    "is_error": _string_col(),
    "num_turns": _int_col(),
    "duration_ms": _int_col(),
    "total_cost_usd": _float_col(),
    "model": _string_col(),
    "sdk_version_resolved": _string_col(),
    "plugins_resolved": _string_col(),
    "api_error_status": _string_col(),
    "result_text": _string_col(),
}


class TranscriptWriter:
    """Tees the SDK message stream to a JSONL file + structured tables."""

    def __init__(
        self,
        component: ComponentBase,
        files_out_path: str,
        sdk_version_resolved: str,
        plugins_resolved: dict[str, str],
        secret_values: list[str] | None = None,
    ) -> None:
        self._component = component
        self._files_path = files_out_path
        self._sdk_version = sdk_version_resolved
        self._plugins_resolved = plugins_resolved
        self._secret_values = [s for s in (secret_values or []) if s]
        self._sessions_rows: list[dict[str, Any]] = []
        self._runs_rows: list[dict[str, Any]] = []
        # per-task streaming state
        self._task_id: str = ""
        self._seq: int = 0
        self._session_id: str = ""
        self._file = None
        self._file_path: str = ""

    # --- per-task lifecycle ---------------------------------------------------

    def begin_task(self, task_id: str) -> None:
        """Open the per-task JSONL file artifact and reset streaming state."""
        self._task_id = task_id
        self._seq = 0
        self._session_id = ""
        os.makedirs(self._files_path, exist_ok=True)
        self._file_path = os.path.join(self._files_path, f"claude_session_{task_id}.jsonl")
        self._file = open(self._file_path, "w", encoding="utf-8")  # noqa: SIM115 — closed in end_task

    def on_message(self, message: Any) -> None:
        """Tee one SDK message to the JSONL file and the sessions buffer."""
        row = self._message_to_row(message)
        newly_known_session_id = not self._session_id and row["session_id"]
        if newly_known_session_id:
            self._session_id = row["session_id"]
            # Back-fill any earlier buffered rows for THIS task that were
            # appended before the session_id was known (Finding 6). Without
            # this, those rows keep session_id="" and, with the incremental
            # PK (task_id, session_id, seq), a repeated task_id across runs
            # could collide/overwrite the earlier run's rows instead of each
            # row getting a distinct key.
            for buffered in reversed(self._sessions_rows):
                if buffered["task_id"] != self._task_id or buffered["session_id"]:
                    break
                buffered["session_id"] = self._session_id
        if self._file is not None:
            self._file.write(self._scrub(row["raw_json"]) + "\n")
        self._sessions_rows.append({k: self._scrub_value(v) for k, v in row.items()})
        self._seq += 1

    def end_task(self, result: ClaudeRunResult) -> None:
        """Close the JSONL file and append the claude_runs summary row."""
        if self._file is not None:
            self._file.close()
            self._file = None
        self._copy_sdk_on_disk_transcript(result.session_id or self._session_id)
        self._runs_rows.append(
            {
                "task_id": result.task_id,
                "session_id": result.session_id or self._session_id,
                "success": str(result.success).lower(),
                "subtype": result.subtype,
                "is_error": str(result.is_error).lower(),
                "num_turns": result.num_turns,
                "duration_ms": result.duration_ms,
                "total_cost_usd": result.total_cost_usd if result.total_cost_usd is not None else "",
                "model": self._scrub_value(result.extra_args.get("model", "")),
                "sdk_version_resolved": self._sdk_version,
                "plugins_resolved": json.dumps(self._plugins_resolved, sort_keys=True),
                "api_error_status": result.api_error_status if result.api_error_status is not None else "",
                "result_text": self._scrub_value(result.result_text or ""),
            }
        )

    # --- flush ----------------------------------------------------------------

    def flush(self) -> None:
        """Write claude_sessions + claude_runs CSVs and their manifests.

        Defensively close any per-task JSONL handle still open. If a task raised
        before :meth:`end_task` ran, the handle would otherwise leak and its
        buffered tail might not reach disk before the file manifest is registered
        here.
        """
        if self._file is not None:
            self._file.close()
            self._file = None
        self._write_file_manifests()
        self._write_table(SESSIONS_TABLE, SESSIONS_SCHEMA, self._sessions_rows)
        self._write_table(RUNS_TABLE, RUNS_SCHEMA, self._runs_rows)

    def _write_file_manifests(self) -> None:
        """Register every produced JSONL file as a tagged file artifact.

        NOTE: Keboola file output mapping has **no** ``write_always`` (it is a
        ``TableDefinition``-only attribute in ``keboola.component.dao``), so a
        JSONL file artifact is uploaded only on a SUCCESSFUL job. The always-on
        durable transcript-of-record is the ``claude_sessions`` TABLE sink — it
        stores every JSONL line verbatim in ``raw_json`` and carries a real
        ``write_always=True`` (so it survives an exit-1 job). The file is the
        full-fidelity success-path convenience.
        """
        if not os.path.isdir(self._files_path):
            return
        for name in os.listdir(self._files_path):
            if not name.endswith(".jsonl"):
                continue
            file_def = self._component.create_out_file_definition(name, tags=SESSION_TAGS)
            self._component.write_manifest(file_def)

    def _write_table(self, name: str, schema: dict[str, ColumnDefinition], rows: list[dict[str, Any]]) -> None:
        columns = list(schema.keys())
        primary_key = [c for c, d in schema.items() if d.primary_key]
        table_def = self._component.create_out_table_definition(
            f"{name}.csv",
            schema=schema,
            primary_key=primary_key,
            incremental=True,
            write_always=True,
            has_header=True,
        )
        with open(table_def.full_path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in columns})
        self._component.write_manifest(table_def)

    # --- helpers --------------------------------------------------------------

    def _message_to_row(self, message: Any) -> dict[str, Any]:
        """Project one SDK message onto the claude_sessions columns."""
        msg_type = type(message).__name__
        row = {
            "task_id": self._task_id,
            "session_id": getattr(message, "session_id", "") or self._session_id,
            "seq": self._seq,
            "ts": "",
            "type": msg_type,
            "subtype": getattr(message, "subtype", "") or "",
            "role": "",
            "text": "",
            "tool_name": "",
            "tool_input_json": "",
            "tool_result_json": "",
            "is_error": str(getattr(message, "is_error", "")).lower() if hasattr(message, "is_error") else "",
            "raw_json": self._serialize(message),
        }
        if msg_type == "SystemMessage":
            data = getattr(message, "data", {}) or {}
            if not row["session_id"]:
                row["session_id"] = data.get("session_id", "")
        elif msg_type in ("AssistantMessage", "UserMessage"):
            row["role"] = "assistant" if msg_type == "AssistantMessage" else "user"
            self._project_content_blocks(message, row)
        return row

    @staticmethod
    def _project_content_blocks(message: Any, row: dict[str, Any]) -> None:
        texts: list[str] = []
        for block in getattr(message, "content", []) or []:
            block_type = type(block).__name__
            if block_type == "TextBlock":
                texts.append(getattr(block, "text", ""))
            elif block_type == "ToolUseBlock":
                row["tool_name"] = getattr(block, "name", "")
                row["tool_input_json"] = json.dumps(getattr(block, "input", {}), default=str)
            elif block_type == "ToolResultBlock":
                content = getattr(block, "content", None)
                row["tool_result_json"] = content if isinstance(content, str) else json.dumps(content, default=str)
                if getattr(block, "is_error", None):
                    row["is_error"] = "true"
        if texts:
            row["text"] = "\n".join(texts)

    @staticmethod
    def _serialize(message: Any) -> str:
        """Serialize an SDK message (dataclass) to one JSON line."""
        try:
            if dataclasses.is_dataclass(message) and not isinstance(message, type):
                return json.dumps(dataclasses.asdict(message), default=str)
        except Exception:  # pragma: no cover - defensive
            pass
        return json.dumps({"repr": repr(message)}, default=str)

    def _copy_sdk_on_disk_transcript(self, session_id: str) -> None:
        """Best-effort copy of the SDK's own on-disk JSONL (R3 — tee is authoritative)."""
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if not config_dir or not session_id:
            return
        projects = os.path.join(config_dir, "projects")
        if not os.path.isdir(projects):
            return
        try:
            for project_dir in os.listdir(projects):
                candidate = os.path.join(projects, project_dir, f"{session_id}.jsonl")
                if os.path.isfile(candidate):
                    dest = os.path.join(self._files_path, f"claude_session_{self._task_id}_sdk.jsonl")
                    with open(candidate, encoding="utf-8") as src, open(dest, "w", encoding="utf-8") as out:
                        out.write(src.read())
                    return
        except OSError as exc:  # pragma: no cover - filesystem edge
            logging.info("Could not copy SDK on-disk transcript: %s", exc)

    def _scrub(self, text: str) -> str:
        for secret in self._secret_values:
            text = text.replace(secret, "***")
        return text

    def _scrub_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._scrub(value)
        return value
