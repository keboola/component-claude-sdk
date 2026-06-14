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

How to set it up
================

This component runs in one of **two input modes**. The whole configuration page
(auth, model, tools, MCP servers, plugins, budget) describes the **shared
environment** every task runs in; only the *prompt* differs between the two
modes.

### Mode 1 — Config-prompt (a single prompt)

The simplest path. Leave the **input mapping empty** and write one prompt.

1. Set **Anthropic API Key** (`#anthropic_key`) and click **Test Connection**.
2. Pick a **Model** and set **Max Turns** + **Max Budget (USD)** — these two
   together hard-bound the run (there is no wall-clock timeout).
3. Under **Task**, write the **Prompt** — be explicit about the table(s) you want
   the agent to produce (e.g. *"write a CSV named `order_summary` with …"*).
4. Run. One agent task executes; its output tables and the transcript tables land
   in Storage.

### Mode 2 — Tasks-table (one row per task)

For batches of tasks driven by data.

1. Set up the shared environment as in Mode 1 (auth, model, budget, tools, …) —
   but leave the **Task** prompt empty.
2. **Map exactly one input table** whose destination name is **`tasks`** (if only
   one table is mapped, any name is accepted and the assumption is logged).
3. Each **row is one agent task**, run sequentially in file order. Columns:

   | Column | Required | Meaning |
   |---|---|---|
   | `task_id` | **yes** | Unique id; correlation key in the transcript/sessions tables. |
   | `prompt` | **yes** | The agent goal for this row. Empty → that row fails (exit 1). |
   | `system_prompt` | no | Per-row system prompt; overrides the config-level one. |
   | `model` | no | Per-row model id; falls back to the config `model`. |
   | `max_turns` | no | Per-row turn cap; falls back to the config value. |
   | `max_budget_usd` | no | Per-row budget; **clamped down** to the config ceiling. |
   | `output_table` | no | Hint for the agent's primary output table name. |

   Unknown extra columns are passed to the agent as per-task JSON context.
4. Optionally set **Task ID Filter** to process only some rows — see below.

#### One shared tasks table, many configs (`task_id_filter`)

Map **one curated `tasks` table into several configs** and give each config a
different **Task ID Filter** (a `task_id` or comma-separated list). Each config
then owns its own row(s) — per-row ownership and independent
scheduling/retry **without** the overhead of Keboola config rows. Empty filter =
all rows. A filter matching no row **fails the job with a clear error** (a typo is
loud, not silent). Exact `task_id` matching; ignored in config-prompt mode.

Configuration reference
=======================

All parameters live at config level (single configuration, no config rows). Full
parameter → `ClaudeAgentOptions` mapping is in the design spec
(`docs/superpowers/specs/2026-06-14-claude-sdk-design.md`, §5.1).

**Secrets**

- `#anthropic_key` — **required**. Injected as `ANTHROPIC_API_KEY`.
- `#github_token` — optional. Injected as `GITHUB_TOKEN` + `GH_TOKEN`; needed only
  when **Enable GitHub** is on or a **private** plugin source is used. Fine-grained
  PAT (Contents r/w, Pull requests r/w) or a classic `repo`-scope token.
- Per-MCP-server secrets — put a `#`-prefixed key in a stdio server's `env` or an
  HTTP/SSE server's `headers` (e.g. `{"Authorization": "Bearer …"}`).

**Model & budget**

- `model` (default `claude-opus-4-8`) / `fallback_model` (optional).
- `max_turns` (default 20) and `max_budget_usd` (default 10) — **always keep both
  set**; they are the only stop conditions. Per-task overrides are clamped to the
  budget ceiling. `effort` (optional) trades quality for cost.

**Permissions & tools**

- `permission_mode` (default `dontAsk`) — only **non-prompting** modes are
  offered: `dontAsk` (deny anything not allow-listed), `bypassPermissions`
  (auto-approve all; deny rules still apply — use deliberately), `auto`
  (classifier-gated). Prompting modes (`default` / `acceptEdits` / `plan`) are
  rejected because they would **hang a headless job** until it times out.
- `allowed_tools` / `disallowed_tools` — built-ins `Read, Write, Edit, Bash, Glob,
  Grep, WebFetch, WebSearch` (scoped Bash like `Bash(git *)` works); MCP tools as
  `mcp__<server>__<tool>` or `mcp__<server>__*`. In `dontAsk` mode the agent can
  use **only** allow-listed tools; a deny rule always wins.
- `system_prompt`, `settings_json`, `setting_sources` (advanced passthrough).

**MCP servers** (`mcp_servers`)

- `stdio` (in-container subprocess: `command`/`args`/`env`) or `http`/`sse`
  (remote: `url`/`headers`). Defining a server only makes its tools *visible* — you
  must also allow-list them in `allowed_tools`.

**Plugins** (`plugins`) — installed at job start, nothing baked into the image

- `source`: a public shorthand (e.g. `superpowers`), an `owner/repo`, a git URL, or
  a `marketplace.json` URL. **Private** sources (e.g. a private CF Kit repo) set
  `private: true` and authenticate via `#github_token`.
- `version`: a **pinned** tag/SHA/branch (reproducible) or **`latest`** (re-pull
  newest each run, not reproducible). The resolved version is recorded in
  `claude_runs`.
- `plugins`: the plugin name(s) to install (or `["*"]` / empty for all).

**GitHub & workspace**

- `github_enabled` (default false) — adds `Bash(gh *)`/`Bash(git *)` to the
  allow-list and exports the token; needs `#github_token`.
- `workspace_input_files` (default false) — stages `/data/in/files/` into the
  agent's working directory.

**SDK version** (advanced)

- `sdk_version` (default `pinned`) — `pinned` uses the baked-in `claude-agent-sdk`
  (offline-safe, deterministic). A concrete version (e.g. `0.2.105`) or `latest`
  pip-installs at job start (**needs HTTPS egress**; `latest` is non-reproducible).
  The bundled CLI moves with the package, so there is no SDK/CLI skew.
- `sdk_version_on_failure` (default `fail`) — `fail` raises if a non-pinned install
  fails (no silent downgrade); `fallback_pinned` warns and uses the baked version.

Future UI enhancement
---------------------

The configuration form groups the flat root fields into labelled sections, and the
already-nested blocks (`task`, `output`, each `mcp_servers[]` / `plugins[]` entry)
use a multi-column grid. The **root-level** scalars (e.g. `model` / `fallback_model`
/ `max_turns` / `max_budget_usd` / `effort`) are single-column because Keboola's
`grid-strict` multi-column layout only works on a sub-object. Laying those out as
side-by-side grid rows is possible by wrapping them in a sub-object with
`format: "grid-strict"` plus a back-compat `model_validator(mode="before")` flatten
in `src/configuration.py`. That is **deferred** because it changes the persisted
config shape (all existing configs/tests/cassettes assume the flat shape) — it is a
config-model change that needs explicit owner approval before it ships.

Input
=====

- **Config-prompt mode:** no input table; the prompt comes from `task.prompt`.
- **Tasks-table mode:** map one input table named `tasks` (one row per task) — see
  the [How to set it up](#how-to-set-it-up) table above for the column contract.

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
