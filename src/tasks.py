"""Task source — turns config / the ``tasks`` input table into a list of Tasks.

Two input modes (spec §2.3):
- **config-prompt mode**: no ``tasks`` input table; one Task from ``config.task``.
- **tasks-table mode**: an input table whose destination is ``tasks`` (or the
  sole mapped table); one Task per CSV row, with the ``task_id_filter`` row
  selector applied afterwards (spec §2.3.1).
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field

from keboola.component.dao import TableDefinition
from keboola.component.exceptions import UserException

from configuration import Configuration

TASKS_TABLE_NAME = "tasks"
REQUIRED_TASK_COLUMNS = ("task_id", "prompt")
# Columns the contract gives first-class meaning; everything else -> ``extra``.
KNOWN_TASK_COLUMNS = frozenset(
    {"task_id", "prompt", "system_prompt", "model", "max_turns", "max_budget_usd", "output_table"}
)


@dataclass
class Task:
    """One agent task to run (spec §2.3)."""

    task_id: str
    prompt: str
    system_prompt: str = ""
    model: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    output_table: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


class TaskSource:
    """Loads the tasks to run from config or the ``tasks`` input table."""

    def __init__(self, configuration: Configuration) -> None:
        self._config = configuration

    def load(self, input_tables: list[TableDefinition]) -> list[Task]:
        """Build the ordered list of tasks for this run."""
        tasks_table = self._select_tasks_table(input_tables)

        if tasks_table is None:
            return self._load_config_prompt_mode()

        tasks = self._load_tasks_table_mode(tasks_table)
        return self._apply_task_id_filter(tasks)

    def _select_tasks_table(self, input_tables: list[TableDefinition]) -> TableDefinition | None:
        """Pick the ``tasks`` table, or accept a sole input table by convention."""
        if not input_tables:
            return None
        for table in input_tables:
            if table.name == TASKS_TABLE_NAME:
                return table
        if len(input_tables) == 1:
            only = input_tables[0]
            logging.info(
                "Single input table '%s' mapped without the 'tasks' name; treating it as the tasks table.",
                only.name,
            )
            return only
        raise UserException(
            f"Multiple input tables mapped but none is named '{TASKS_TABLE_NAME}'. "
            f"Map exactly one table with destination '{TASKS_TABLE_NAME}', or none for config-prompt mode."
        )

    def _load_config_prompt_mode(self) -> list[Task]:
        """One task built from the config-level ``task`` block."""
        if self._config.task_id_filter is not None:
            logging.info("task_id_filter is set but no tasks table is mapped; ignoring it (config-prompt mode).")
        prompt = (self._config.task.prompt or "").strip()
        if not prompt:
            raise UserException(
                "No tasks table mapped and config 'task.prompt' is empty; provide a prompt or map a tasks table."
            )
        system_prompt = self._config.task.system_prompt or self._config.system_prompt
        return [Task(task_id="config-task", prompt=prompt, system_prompt=system_prompt)]

    def _load_tasks_table_mode(self, table: TableDefinition) -> list[Task]:
        """One Task per CSV row; validate required columns and id uniqueness."""
        tasks: list[Task] = []
        seen_ids: set[str] = set()
        with open(table.full_path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            self._validate_columns(reader.fieldnames, table.name)
            for line_no, row in enumerate(reader, start=2):  # row 1 is the header
                task = self._row_to_task(row, line_no)
                if task.task_id in seen_ids:
                    raise UserException(
                        f"Duplicate task_id '{task.task_id}' in tasks table; task_id must be unique."
                    )
                seen_ids.add(task.task_id)
                tasks.append(task)
        if not tasks:
            raise UserException("The tasks table is empty; it must contain at least one task row.")
        return tasks

    @staticmethod
    def _validate_columns(fieldnames: list[str] | None, table_name: str) -> None:
        present = set(fieldnames or [])
        missing = [c for c in REQUIRED_TASK_COLUMNS if c not in present]
        if missing:
            raise UserException(
                f"Tasks table '{table_name}' is missing required column(s): {', '.join(missing)}."
            )

    def _row_to_task(self, row: dict[str, str], line_no: int) -> Task:
        task_id = (row.get("task_id") or "").strip()
        if not task_id:
            raise UserException(f"Empty 'task_id' in tasks table at row {line_no}.")
        prompt = (row.get("prompt") or "").strip()
        if not prompt:
            raise UserException(f"Empty 'prompt' for task_id '{task_id}' (tasks table row {line_no}).")
        extra = {k: v for k, v in row.items() if k and k not in KNOWN_TASK_COLUMNS}
        return Task(
            task_id=task_id,
            prompt=prompt,
            system_prompt=(row.get("system_prompt") or "").strip() or self._config.system_prompt,
            model=(row.get("model") or "").strip() or None,
            max_turns=self._parse_int(row.get("max_turns"), "max_turns", task_id),
            max_budget_usd=self._parse_float(row.get("max_budget_usd"), "max_budget_usd", task_id),
            output_table=(row.get("output_table") or "").strip() or None,
            extra=extra,
        )

    @staticmethod
    def _parse_int(value: str | None, column: str, task_id: str) -> int | None:
        value = (value or "").strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            raise UserException(f"Invalid integer for '{column}' on task_id '{task_id}': {value!r}.")

    @staticmethod
    def _parse_float(value: str | None, column: str, task_id: str) -> float | None:
        value = (value or "").strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            raise UserException(f"Invalid number for '{column}' on task_id '{task_id}': {value!r}.")

    def _apply_task_id_filter(self, tasks: list[Task]) -> list[Task]:
        """Keep only the rows whose task_id matches the filter (spec §2.3.1)."""
        selected = self._config.selected_task_ids()
        if selected is None:
            return tasks
        wanted = set(selected)
        kept = [t for t in tasks if t.task_id in wanted]
        if not kept:
            available = ", ".join(t.task_id for t in tasks) or "(none)"
            raise UserException(
                f"task_id_filter {selected} matched no rows in the tasks table. Available task_id(s): {available}."
            )
        return kept
