# Implementation Plan — keboola.app-claude-sdk

> Spec: `docs/superpowers/specs/2026-06-14-claude-sdk-design.md` (read it first — this plan executes that design).
> Branch: `initial-implementation` (do not touch `main`).
> Executed via `superpowers:subagent-driven-development`: one fresh subagent per task, reviewed between
> tasks. Implementation tasks → `component-develop` (delegates schema/UI to `component-build-ui`);
> test tasks → `component-test` / `generate-vcr-tests`. The lifecycle tracker
> (`docs/superpowers/keboola.app-claude-sdk-lifecycle.md`) gates the Phase 4 / Phase 5 milestones — tick
> those on the verifier, not on the last task here.
>
> **Standing constraints for every task:** never read/print secret VALUES (`secrets.json`; the
> PreToolUse hook enforces it — reference key NAMES only); `run()` stays a thin orchestrator (<30 lines)
> with logic in private methods; typed Pydantic config (no raw `dict`/`Any` for structured data,
> `extra` set explicitly, no model `debug` field); `ruff check src/ tests/` clean after each task;
> scratch in `/tmp`, never `/data/out/tables/`; `UserException` (exit 1) for user-actionable errors.

## Phase 4 — Implementation (owner: `component-develop` / `component-build-ui`)

### Task 4.0 — Infra: pin SDK, Python-only Dockerfile, deps
**Owner:** `component-develop` (+ `component-defaults` for template conformance).
- Add `claude-agent-sdk==0.2.101` to `pyproject.toml` `dependencies` (hard `==` pin). Keep
  `requires-python = ~=3.14.0` (SDK allows `>=3.10`). Keep `keboola-http-client` (used by
  `testConnection`); drop `keboola-utils` if unused.
- `uv lock` to refresh `uv.lock` with the SDK.
- Confirm the Dockerfile stays **Python-only** (no Node/npm) — the SDK bundles the CLI
  (`manylinux_2_17_x86_64` wheel). Leave the scaffold's multi-stage `python:3.14-slim` + `uv` shape
  intact; only ensure `src/` modules are copied.
- Quick container sanity (Phase 7 will do the real check): note R1 (bundled-binary-on-slim) for the
  smoke test; do **not** add the Node fallback unless R1 fires.
**Done when:** `uv sync` resolves with the pinned SDK; `uv.lock` updated; Dockerfile has no Node;
`ruff` clean. **Verify:** `uv run python -c "import claude_agent_sdk; print(claude_agent_sdk.__version__)"`
prints `0.2.101`.

### Task 4.1 — Pydantic configuration model (`src/configuration.py`)
**Owner:** `component-develop`.
- Replace the scaffold model with the typed tree from spec §5: `Configuration` + nested
  `McpServerConfig` (stdio/http/sse discriminated on `type`, each with `#`-secret fields via
  `Field(alias="#...")`), `PluginsConfig` (`marketplaces`, `install`, `update_on_run`), `TaskConfig`
  (`prompt`, `system_prompt`), `OutputConfig` (`default_incremental`).
- `#anthropic_key` (alias) **required**; `#github_token` optional; `model` enum (default
  `claude-opus-4-8`), `fallback_model` optional, `max_turns` int default 20, `max_budget_usd` float
  default 5.0, `effort` optional enum, `permission_mode` enum default `dontAsk`, `allowed_tools`/
  `disallowed_tools` lists default `[]`, `system_prompt` optional, `settings_json` optional,
  `setting_sources` default `[]`, `github_enabled`/`workspace_input_files` bools default false.
- **`task_id_filter`** (spec §2.3.1): optional `str | list[str] | None`, default `None` (= all rows).
  A field validator normalises a bare string to a one-element list and an empty string/empty list to
  `None`. Expose a typed accessor `selected_task_ids() -> list[str] | None` for `TaskSource`.
- `model_config` sets `extra="ignore"`, `populate_by_name=True`. **No `debug` field** (platform
  handles it). Validation errors → `UserException` (reuse the reference's `__init__` try/except
  pattern). A **partial** instantiation path must allow `testConnection` with only `#anthropic_key`.
- Per-task override clamp helper: `effective_budget(task_budget) = min(task_budget or config, config)`.
**Done when:** model parses a representative `config.json`; missing `#anthropic_key` → `UserException`;
`ruff` clean. **Verify:** a small `tests/unit/test_configuration.py` round-trips a sample config and
asserts the alias + required behaviour.

### Task 4.2 — Task source (`src/tasks.py`)
**Owner:** `component-develop`.
- `Task` dataclass (`task_id, prompt, system_prompt, model, max_turns, max_budget_usd, output_table,
  extra: dict`).
- `TaskSource.load(configuration, input_tables) -> list[Task]`:
  - **config-prompt mode** when no input table named `tasks` (and not exactly-one-fallback): one Task
    from `config.task`. `task_id_filter` is ignored here (info log if set) — spec §2.3.1.
  - **tasks-table mode** when a `tasks`-named table exists (or exactly one input table → accept + log
    assumption): one Task per CSV row; validate required columns `task_id`,`prompt`
    (missing → `UserException` naming the column); unknown columns → `extra` (JSON blob appended to the
    prompt envelope). `task_id` uniqueness enforced.
  - **Row selector (`task_id_filter`, spec §2.3.1):** after building the row list, if
    `configuration.selected_task_ids()` is not `None`, keep only rows whose `task_id` is in that set
    (exact string equality, preserve file order). **No surviving row → `UserException` (exit 1)** whose
    message names the requested filter value(s) and lists the available `task_id`s. Default (`None`) =
    keep all rows.
**Done when:** both modes produce correct `list[Task]`; missing required column → exit 1; a
`task_id_filter` selects only its row(s); a non-matching filter → exit 1 with a helpful message;
`ruff` clean.

### Task 4.3 — Plugin manager (`src/plugin_manager.py`)
**Owner:** `component-develop`.
- `PluginManager.prepare(plugins_config, env) -> list[SdkPluginConfig]`:
  - mkdir `/tmp/claude-home`; set env `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_PLUGIN_CACHE_DIR`,
    `CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE=1`.
  - per marketplace: `claude plugin marketplace add <source> [--scope ...]`; if `update_on_run`:
    `claude plugin marketplace update`.
  - per install: `claude plugin install <plugin>@<marketplace>`.
  - resolve cache paths (`claude plugin marketplace list --json`) → return
    `[{"type":"local","path": ...}]`.
  - all `subprocess.run` with captured output **logged with secret-scrubbing**; non-zero → `UserException`.
- No-op cleanly when no plugins configured (returns `[]`).
**Done when:** unit test with `subprocess` mocked exercises add/update/install ordering and the no-op
path; `ruff` clean. **Note:** real non-interactive behaviour (R2) is validated at Phase 7 S4.

### Task 4.4 — Claude runner: the SDK boundary (`src/claude_runner.py`)
**Owner:** `component-develop`.
- `ClaudeRunResult` dataclass: `success, result_text, total_cost_usd, duration_ms, num_turns,
  session_id, subtype, is_error, usage, model_usage, api_error_status`.
- `ClaudeRunner.build_options(task, config, plugin_paths, env) -> ClaudeAgentOptions` mapping every
  field per spec §5.1 (model/fallback/max_turns/max_budget_usd(clamped)/effort/permission_mode/
  allowed+disallowed tools/system_prompt(per-task override)/mcp_servers(dict, secrets→env or headers)/
  plugins/setting_sources/settings/cwd(/tmp workspace)/env). Set `permission_mode="dontAsk"` default
  explicitly. GitHub toggle → ensure `Bash` + `Bash(gh *)`/`Bash(git *)` allow entries and token in env.
- `async run_task(task, options, on_message) -> ClaudeRunResult`: `async for message in query(prompt,
  options)`, call `on_message(message)` for each (the transcript tee), capture terminal
  `ResultMessage`; map budget/turn-cap `subtype` to `success=False`. Handle `result_message is None`.
- **This is the single seam tests monkeypatch.** Keep `query` imported at module top so tests can patch
  `claude_runner.query`.
**Done when:** options-builder unit test asserts the full mapping incl. budget clamp, `dontAsk`,
secret→env/header wiring; `ruff` clean.

### Task 4.5 — Transcript writer (`src/transcript_writer.py`)
**Owner:** `component-develop`.
- `TranscriptWriter` opens a per-task JSONL file under `/data/out/files/claude_session_<task_id>.jsonl`
  and writes a `.manifest` with `write_always: true` + tags. `on_message(message)` serializes each SDK
  message to one JSON line (file) **and** appends a structured row to the in-memory `claude_sessions`
  buffer (columns per spec §2.6.1, incl. `raw_json`).
- After a task: append a `claude_runs` row from `ClaudeRunResult`.
- `flush()`: write `/data/out/tables/claude_sessions.csv` + `claude_runs.csv` with **authoritative
  `schema` manifests**, `has_header=True`, `write_always: true`, `incremental: true`, PKs per spec.
  `claude_runs` gets real numeric types (cost/turns/duration); `claude_sessions` mostly STRING + the
  numeric `seq`.
- Best-effort copy of the SDK on-disk JSONL (`$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/<sid>.jsonl`)
  next to the streamed file; failure to find it is logged, not fatal (R3 — the tee is authoritative).
- **Secret-scrubbing** pass on serialized content (never write known secret values).
**Done when:** unit test feeds a canned message list and asserts file lines + sessions/runs rows +
`write_always` in manifests + `has_header=True`; `ruff` clean.

### Task 4.6 — Output writer for agent-produced tables (`src/output_writer.py`)
**Owner:** `component-develop`.
- Helper(s) the agent's filesystem outputs are reconciled through: scan the agent workspace / a
  declared outputs location, and for each agent-produced table write `/data/out/tables/<name>.csv` +
  authoritative `schema` manifest, `has_header=True`, PK/incremental from the agent's declared intent
  (default `incremental=config.output.default_incremental`, overwrite). All-STRING `schema` acceptable
  for agent tables (spec §2.6). Never set `destination` (defaultBucket overrides it).
- Decide the concrete agent→table convention in this task (e.g. the agent writes CSVs to a known
  `/tmp/outputs/` dir that the agent is instructed about via the prompt envelope, OR the component
  exposes an MCP/tool the agent calls). Recommended: instruct the agent (in the prompt envelope) to
  write final tables as headered CSV into `/tmp/outputs/`, then `OutputWriter` promotes each to
  `/data/out/tables/` with a manifest. Document the chosen convention in the module docstring + README.
**Done when:** unit test promotes a sample `/tmp/outputs/foo.csv` to a manifested output table;
`ruff` clean.

### Task 4.7 — Sync action: `testConnection` (`src/sync_actions.py`)
**Owner:** `component-develop`.
- `@sync_action("testConnection")`: instantiate the **partial** config (only `#anthropic_key`), make
  ONE cheap Anthropic Messages API call **in-process** via `keboola-http-client` (Haiku, 1 token) to
  validate the key; return success or a clear failure. Do **not** spawn the agent loop. This is the
  only in-process Anthropic HTTP (VCR-recordable, Task 5.3).
**Done when:** action registered + wired in `component.py` action map; unit-test stub; `ruff` clean.

### Task 4.8 — Component orchestrator (`src/component.py`)
**Owner:** `component-develop`.
- `__init__`: `super().__init__()`, build `ClaudeRunner`, `PluginManager`, `TranscriptWriter`,
  `OutputWriter`, `TaskSource` (clients in `__init__`, not `run()`).
- `run()` (thin, <30 lines) per spec §6.1: parse config → `PluginManager.prepare` → `TaskSource.load`
  → for each task `asyncio.run(runner.run_task(..., on_message=transcript.on_message))` → flush
  outputs + transcript (always) → decide exit (any task `is_error` + fail-on-error default → raise
  `UserException`; else 0). Logic in private methods (`_prepare_env`, `_run_one_task`, `_finalize`).
- Keep the `__main__` guard (UserException→exit 1, else exit 2 — already in scaffold).
- `execute_action()` dispatch covers `run` + `testConnection`.
**Done when:** `run()` is <30 lines and delegates; orchestrates a canned (mocked) run end-to-end; `ruff`
clean. **Gate:** Phase 4 lifecycle box = scoped `component-checklist-review` on architecture, typing,
configuration, error-handling, logging, output-state, infra (run cold).

### Task 4.9 — configSchema.json + UI (owner: `component-build-ui`)
**Delegated to `component-build-ui` by `component-develop`.**
- Build `component_config/configSchema.json` for the full §5.1 parameter set with §5.2 conditionals
  (`options.dependencies`, never root-level): MCP server `type` switch, `github_enabled` reveal,
  plugins advanced group, `task_id_filter` field (free-text/array near the tasks mapping, described as
  tasks-table-mode-only, empty = all rows — spec §2.3.1 / §5.2), `#`-field names matching the Pydantic
  `alias=` exactly, `format: "test-connection"` widget (auto-invokes `testConnection`), `propertyOrder`
  only on existing props, titles/descriptions on required fields.
- **Remove `component_config/configRowSchema.json`** (single config — spec §2.1). Replace the
  placeholder `sample-config/` with a realistic config-prompt-mode sample (no secret values).
- Update `component_config` descriptions / `uiOptions.md` minimally (full portal value setup is Phase 6).
**Done when:** schema validates in the schema tester; `#` names match the model aliases;
`testConnection` widget present; no `configRowSchema.json`. **Gate:** schema-ui dimension (Phase 4/6).

## Phase 5 — Tests + cassettes (owner: `component-test` / `generate-vcr-tests`)

### Task 5.1 — Mock-boundary fixtures + the canned-stream conftest
**Owner:** `component-test`.
- A `tests/conftest.py` (or helper) that monkeypatches `claude_runner.query` to yield a typed message
  stream assembled from small JSON fixtures under `tests/fixtures/streams/`: `happy/`, `budget_cap/`
  (terminal `ResultMessage.subtype` = budget error), `error/` (`is_error=True`), `multi_task/`.
- The stream builder constructs real SDK message objects (`SystemMessage`, `AssistantMessage` with
  `TextBlock`/`ToolUseBlock`/`ToolResultBlock`, `ResultMessage`).
**Done when:** a unit test drives `ClaudeRunner.run_task` through each fixture and asserts the mapped
`ClaudeRunResult`.

### Task 5.2 — Datadir tests (config parsing + output/manifest correctness)
**Owner:** `component-test`.
- `tests/functional/<case>/` cases (single merged `config.json`, no row/root split; `secrets.json`
  injected by the runner per spec §7): `happy_config_prompt`, `tasks_table_multi`,
  `tasks_table_filtered` (a `task_id_filter` selecting a subset → only those rows run; assert
  `claude_runs` has exactly the filtered `task_id`s), `task_filter_no_match` (→ exit 1 with the
  available-`task_id`s message), `missing_anthropic_key` (→ exit 1), `missing_tasks_column` (→ exit 1),
  `budget_cap`, `agent_error`.
- Each happy case asserts expected `out/files/*.jsonl`, `out/tables/claude_sessions.csv(.manifest)`,
  `claude_runs.csv(.manifest)` (authoritative `schema`, `write_always`, PK/incremental, `has_header`),
  and a promoted agent output table. Inspect a real produced CSV row, not just the manifest.
- Secret injection: the test runner reads `secrets.json` itself and merges `#anthropic_key` into each
  case's `config.json` (agent never reads it; key NAME only).
**Done when:** `uv run pytest tests/ -v` is green (paste the `N passed` line); manifests verified.

### Task 5.3 — VCR for `testConnection` (the one real in-process HTTP)
**Owner:** `generate-vcr-tests` / `component-test`.
- Record a success cassette + a 401 auth-failure cassette for the in-process Anthropic Messages call.
  `VCR_SANITIZERS` scrub `x-api-key`/`authorization` headers and the key value. Replay needs no real
  key.
- Cassette validation gate (Phase 5 DoD): grep cassettes for secret patterns (the `secrets.json`
  values, `token`/`password`/`authorization`/`api_key`, configured sanitizer targets) → clean; confirm
  success cassette = 2xx, failure cassette = 401.
**Done when:** sync-action tests pass against cassettes; cassettes verifiably sanitized.
**Gate:** Phase 5 lifecycle box = scoped `component-checklist-review` on testing, credentials,
output-state (run cold) + the cassette validation gate.

## Phases 6-8 (owners; not executed in this plan's subagent loop)

- **Phase 6 — `component-dev-portal` via `kbagent`:** set `defaultBucket: true`,
  `dataTypeSupport: authoritative`, push configSchema + `testConnection`, descriptions/UI options;
  optionally request `forward_token: true` (R6). After the bootstrap release (already done), so CI-sync
  won't overwrite. (dry-run → TTY-confirmed write; fresh GET to confirm.)
- **Phase 7 — `component-test` (tier 4):** build an `initial-implementation` image; create the **S1**
  cf-dev config (image tag overridden, `max_budget_usd ≤ $10`); run; confirm `success` + resolved
  image tag == branch build + expected output/transcript tables (read from platform). Then extend
  S2-S5 as credentials/approvals land. Validates R1 (bundled binary on slim), R2 (plugin CLI),
  R4 (Storage typing).
- **Phase 8 — `component-checklist-review`:** full CF-standards audit; no open blocking/important
  findings; then the single PR to `main`.

## Task dependency order

4.0 → 4.1 → {4.2, 4.3, 4.4, 4.5, 4.6, 4.7 can proceed once 4.1 lands; 4.4 before 4.5/4.6 consumers} →
4.8 (needs all 4.x modules) → 4.9 (schema, parallel-safe after 4.1) → 5.1 → 5.2 → 5.3.
4.2-4.7 are largely independent module builds and can be parallelised across subagents; 4.8 integrates
them.
