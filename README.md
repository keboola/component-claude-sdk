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
- **`claude_sessions`** — one row per SDK message event (queryable transcript).
- **`claude_runs`** — one row per task with cost/turns/duration, the resolved SDK
  version and resolved plugin refs.
- **JSONL file artifacts** under `out/files/` — the full-fidelity transcript.

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

Integration
===========

For details about deployment and integration with Keboola, refer to the
[deployment section of the developer
documentation](https://developers.keboola.com/extend/component/deployment/).
