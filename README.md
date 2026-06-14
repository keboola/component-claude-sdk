Claude SDK Runner (keboola.app-claude-sdk)
==========================================

A highly configurable **Claude Agent SDK** runner inside Keboola. The component
wraps the Python `claude-agent-sdk` so you can run agentic Claude workloads over
your Keboola data: it takes prompt(s)/goal(s) (from config and/or an input
table), runs the Claude agent loop headless in the Keboola container, lets the
agent use tools (Bash/file/web, MCP servers, plugins, GitHub), and lands whatever
it produces as Keboola output tables plus an always-on JSONL session transcript.

**Table of Contents:**

[TOC]

Functionality Notes
===================

- **Headless agent loop.** Each run executes the Claude agent loop via the
  bundled Claude Code CLI (shipped inside `claude-agent-sdk`; no Node required).
- **Two input modes.** Either a single prompt from config (config-prompt mode) or
  one row per task from a mapped `tasks` input table (tasks-table mode).
- **Dynamic, writer-like output.** The agent decides at runtime which tables to
  produce; the component promotes them to Keboola output mapping with manifests.
- **Always-on transcript.** Every run writes the full session transcript (JSONL
  files + the `claude_sessions` and `claude_runs` tables) regardless of
  success/failure.
- **Runtime plugins & MCP servers.** Public and private plugin marketplaces and
  arbitrary MCP servers are installed/configured at job start (not baked into the
  image), each pinned to a ref or tracking `latest`.

Prerequisites
=============

- A funded **Anthropic API key** (`#anthropic_key`, required).
- Optionally a **GitHub token** (`#github_token`) for GitHub work and private
  plugin marketplaces.

Configuration
=============

All parameters live at config level (single configuration, no config rows). The
full parameter set and its mapping to `ClaudeAgentOptions` is documented in the
design spec (`docs/superpowers/specs/2026-06-14-claude-sdk-design.md`, §5.1).
Highlights:

- `#anthropic_key` (required), `#github_token` (optional).
- `model` / `fallback_model`, `max_turns` (default 20), `max_budget_usd`
  (default 10), `effort`.
- `permission_mode` — only the non-prompting modes `dontAsk` (default),
  `bypassPermissions`, `auto` are offered; prompting modes would hang a headless
  run.
- `allowed_tools` / `disallowed_tools`, `system_prompt`, `settings_json`,
  `setting_sources`.
- `mcp_servers` — stdio or http/sse servers with per-server secrets.
- `plugins` — public shorthand or `owner/repo`/git-URL sources, each `latest` or
  a pinned ref; private sources authenticate via `#github_token`.
- `github_enabled`, `workspace_input_files`, `output.default_incremental`.
- `task_id_filter` — in tasks-table mode, process only the matching task_id(s).
- `sdk_version` / `sdk_version_on_failure` — optionally run a newer SDK/CLI at
  runtime without rebuilding the image (`pinned` by default).

Input
=====

- **Config-prompt mode:** no input table; the prompt comes from `task.prompt`.
- **Tasks-table mode:** map one input table named `tasks` (or a single table by
  convention). Each row is one task. Columns: `task_id` (required, unique),
  `prompt` (required), and optional `system_prompt`, `model`, `max_turns`,
  `max_budget_usd`, `output_table`. Unknown columns are passed to the agent as
  per-task context.

Output
======

The component writes:

- **Agent-produced tables** — see the hand-off convention below.
- **`claude_sessions`** — one row per SDK message event (queryable transcript),
  `write_always` so it is uploaded **even on a failed job**. Each row's
  `raw_json` holds the verbatim JSONL line, making this table the durable,
  failure-proof transcript of record.
- **`claude_runs`** — one row per task with cost/turns/duration, the resolved SDK
  version and resolved plugin refs (also `write_always`).
- **JSONL file artifacts** under `out/files/` — the full-fidelity transcript.
  NOTE: Keboola file output mapping has no `write_always`, so these files are
  uploaded only on a **successful** job; the always-on durability guarantee is
  the `claude_sessions` table above.

Agent → table hand-off convention
----------------------------------

The agent writes its final output tables as **headered CSV files** into the
scratch directory **`/tmp/outputs/`**. For each `<name>.csv` it may drop a
sidecar **`<name>.csv.meta.json`** declaring intent:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    {"incremental": true, "primary_key": ["id"]}
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

After the agent loop the component promotes every `*.csv` in `/tmp/outputs/` to
`out/tables/<name>.csv` with a native (all-STRING) schema manifest,
`has_header=true`, and the declared `primary_key`/`incremental` (falling back to
`output.default_incremental`). An incremental table with no primary key is a
configuration error. The destination is never set — the component relies on the
component's default bucket.

Development
-----------

To customize the local data folder path, replace the `CUSTOM_FOLDER` placeholder with your desired path in the `docker-compose.yml` file:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    volumes:
      - ./:/code
      - ./CUSTOM_FOLDER:/data
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Clone this repository, initialize the workspace, and run the component using the following
commands:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
git clone https://github.com/keboola/component-claude-sdk component-app-claude-sdk
cd component-app-claude-sdk
docker-compose build
docker-compose run --rm dev
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Run the test suite and perform lint checks using this command:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
docker-compose run --rm test
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tests
-----

The suite has three layers. The Claude agent loop runs the `claude` CLI as a
subprocess that makes its own outbound HTTPS, so in-process HTTP recording (VCR)
cannot capture it — the agent-loop tests therefore mock the single
`ClaudeRunner._query` SDK seam with a canned, typed message stream (no network,
no subprocess):

- **`tests/unit/`** — boundary unit tests for each module (config parsing,
  output/transcript writers, plugin/SDK-version managers, the runner seam, the
  orchestrator, and the `testConnection` logic).
- **`tests/datadir/`** — datadir functional tests that drive the component
  end-to-end through the Keboola `/data` contract (real `config.json`, input
  tables, and output mapping / `.manifest` files), with the SDK boundary mocked.
  Covers config-prompt and tasks-table modes, `task_id_filter`, agent→table
  promotion, the always-on transcript tables, and the failure→exit-1 paths.
- **`tests/functional/`** — VCR functional tests for the one in-process
  Anthropic HTTP call, the `testConnection` sync action. Cassettes are recorded
  once against the real API and replayed offline; `VCR_SANITIZERS` in
  `src/component.py` scrub the key and auth headers so no secret value is stored.

To (re)record the `testConnection` cassettes against the real API, put the key
in a repo-root secrets file (gitignored, shaped `{"parameters":
{"#anthropic_key": "..."}}`) and run:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
uv run python scripts/record_cassettes.py            # skip-if-exists
uv run python scripts/record_cassettes.py --regenerate   # force re-record
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Integration
===========

For details about deployment and integration with Keboola, refer to the
[deployment section of the developer
documentation](https://developers.keboola.com/extend/component/deployment/).
