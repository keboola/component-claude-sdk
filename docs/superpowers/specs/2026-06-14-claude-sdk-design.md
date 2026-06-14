# keboola.app-claude-sdk — Design Spec

> Type: application (with writer-like dynamic output behaviour)
> Component ID: keboola.app-claude-sdk
> Status: draft
> Date: 2026-06-14
> Branch: initial-implementation
> Research foundation: `docs/superpowers/research/2026-06-14-claude-sdk-research.md` (this spec is consistent with it; the few corrections vs that summary are called out in §12).

## 1. Overview & source system

A **highly configurable Claude Agent SDK runner inside Keboola**. The component wraps the Python
**`claude-agent-sdk`** so a user can run agentic Claude workloads over their Keboola data: it takes
prompt(s)/goal(s) (from config and/or an input table), spawns the Claude agent loop headless in the
Keboola container, lets the agent use tools (Bash/file/web, MCP servers, plugins, GitHub), and lands
whatever it produces as Keboola output tables plus an always-on JSONL session transcript.

- **"Source/target system":** not a REST API — it is the **Claude Agent SDK** (the Python agent-loop
  SDK, formerly "Claude Code SDK") running headless. Primary docs: PyPI
  https://pypi.org/project/claude-agent-sdk/ , GitHub
  https://github.com/anthropics/claude-agent-sdk-python , Agent SDK docs at code.claude.com
  (`overview`, `hosting`, `sessions`, `mcp`, `permissions`, `cost-tracking`, `plugins`,
  `plugin-marketplaces`).
- **Primary use case:** run a configured Claude agent task (e.g. "analyse this table and write a
  summary table", "open a PR against repo X implementing Y", "explore the project's Keboola configs
  via MCP and produce a report") as a scheduled/triggered Keboola job, with full configurability of
  model, tools, MCP servers, plugins, permissions, GitHub access, and budget caps.
- **Net-new vs the reference component `keboola.app-agent-runner`** (read-only during research): that
  one is Keboola-MCP-only, single config prompt, Markdown log, no input tables / generic MCP / GitHub
  / dynamic outputs / JSONL / plugins / budget caps. We build fresh and add all of those.

## 2. Keboola mapping

How the Claude Agent SDK maps onto how Keboola runs a component.

### 2.1 Single config vs config rows — **single config** (decision 1)

We use a **single configuration**, not config rows. Rationale:

- Config rows are the convention for **N independent objects each with its own incremental cursor /
  per-row `state.json`** (one row per table/endpoint). Keboola runs rows **sequentially** and gives
  each its own state — that model fits an extractor pulling many objects, not an agent.
- This component has **one shared agent environment** per job: one writable `CLAUDE_CONFIG_DIR`, one
  set of installed plugins, one MCP-server set, one workspace `cwd`, one Anthropic key, one budget
  policy. Splitting that across rows would re-install plugins and re-resolve MCP per row for no benefit
  and fragment the budget cap.
- "One or more input tables carrying prompts/goals" is handled **inside a single run** by iterating
  the rows of a `tasks` input table (§2.3), not by Keboola config rows. Multiple agent tasks therefore
  share one provisioned environment but run as sequential agent invocations within the one job.
- Session transcripts are **not** a per-object incremental watermark, so the per-row-`state.json`
  machinery buys us nothing.

**Override condition:** if a user genuinely wants per-task isolation/parallelism at the platform level
(separate containers, separate budgets, independent retry), they can create multiple **configs** —
and the `task_id_filter` row selector (§2.3.1) lets each of those configs own a specific row/subset of
a **shared** `tasks` table, bridging single-config and config-rows without the config-rows overhead.
We do not ship `configRowSchema.json`; `component_config/configRowSchema.json` is removed in Phase 4.

### 2.2 Component type & output behaviour

Registered as **`application`** (already done in Phase 1). Output behaviour is **writer-like and
dynamic**: the agent decides at runtime which tables to produce. We do **not** pre-declare output
tables in the config's output mapping — we write CSV + `.manifest` pairs into `/data/out/tables/` at
runtime (`output-mapping.md`: *"every file placed under `/data/out/tables/` is uploaded"*). See §2.6.

### 2.3 Input-table contract (decision 2) — how input tables drive the run

Two input modes, mutually compatible:

- **Config-prompt mode (no input table):** the prompt/goal comes from config parameters
  (`task.prompt`, optional `task.system_prompt`). One agent run. Simplest path; this is what the
  Phase 7 first smoke scenario uses.
- **Tasks-table mode (one input table named `tasks`):** the user maps **exactly one** input table
  whose **destination is `tasks`** (we read `get_input_tables_definitions()` and select the table whose
  `.name == "tasks"`; if only one input table is mapped we accept it regardless of name and log the
  assumption). Each **row = one agent task**, executed sequentially in file order. The contract:

  | Column | Required | Semantics |
  |---|---|---|
  | `task_id` | yes | Stable identifier for the task; used as the transcript/session correlation key and in the sessions table. Must be unique within the table. |
  | `prompt` | yes | The user prompt / goal text for this task. Empty → that row is a `UserException`. |
  | `system_prompt` | no | Per-task system prompt; overrides the config-level system prompt for this row only. |
  | `model` | no | Per-task model id override (bare id, e.g. `claude-sonnet-4-6`); falls back to config `model`. |
  | `max_turns` | no | Per-task turn cap; falls back to config `max_turns`. |
  | `max_budget_usd` | no | Per-task USD cap; falls back to config `max_budget_usd`. **Never allowed to exceed** the config-level ceiling (§2.7, §9). |
  | `output_table` | no | A hint the agent is told to use as the destination table name for this task's primary output. Free-form; the agent may still create others. |

  Unknown extra columns are ignored (passed to the agent as additional task context as a JSON blob in
  the prompt envelope, so a user can carry arbitrary per-task data). Required-column absence →
  `UserException` (exit 1) naming the missing column.

**Config vs input-table split:** *environment* (auth, MCP servers, plugins, permission mode,
allowed/disallowed tools, GitHub toggle, `settings.json` passthrough, budget ceiling, output behaviour)
is **config-level** — entered once and shared by all tasks. *Per-run task content* (prompt, system
prompt, optional model/turn/budget overrides, output hint) is **row-level** in the `tasks` table, or
the single config-level `task` block when no table is mapped.

#### 2.3.1 Row selector — `task_id_filter` (decision 3a, shared-table fan-out)

A single optional config parameter, **`task_id_filter`**, picks which rows of the `tasks` table this
config processes:

- **Default (absent / empty):** process **all rows** of the `tasks` table (the behaviour above).
- **Set to a `task_id` or a list of `task_id`s:** process **only** the matching row(s), in file order;
  all other rows are skipped. Accepts a single string (`"sync-orders"`) or a list
  (`["sync-orders","summarize"]`); a bare string is normalised to a one-element list.

**First-class use case (document this):** *one shared `tasks` input table + N configs/agents, each
config sets its own `task_id_filter` to own a specific row or subset.* The same curated table of agent
tasks can be mapped into many configs; each config (or each agent/schedule) runs only the row(s) it
owns. This **bridges single-config and config-rows** — you get per-row ownership and independent
scheduling/retry of a row across configs **without** the config-rows overhead (no per-row plugin
re-install / MCP re-resolve / fragmented state).

**Semantics & edge cases:**
- `task_id_filter` is **only meaningful in tasks-table mode** — it has no effect in config-prompt mode
  (no table). If set in config-prompt mode, it is ignored with an info log.
- **No match → `UserException` (exit 1)** with a clear message naming the filter value(s) and the
  available `task_id`s, so a typo'd filter fails loudly rather than silently running nothing.
- A filter value that matches a `task_id` present but **disabled/empty-prompt** still surfaces that
  row's own validation error (empty `prompt` → `UserException` as above).
- Kept deliberately simple for v1 — **explicit `task_id` / list of `task_id`s only**, no expression or
  glob/regex filters (a documented future enhancement). Matching is exact string equality on `task_id`.

### 2.4 Secrets → `#`-prefixed config keys

- `parameters.#anthropic_key` — **required**. Decrypted by the platform at runtime → injected as
  `ANTHROPIC_API_KEY` into the SDK subprocess `env`. (Key name is fixed by the brief and the live
  `secrets.json`/VCR fixture shape; do **not** rename it.)
- `parameters.#github_token` — **optional**. Injected as both `GITHUB_TOKEN` and `GH_TOKEN` into the
  subprocess `env`; doubles for `gh`/`git` GitHub work (§5/Q5) and for cloning private plugin
  marketplaces (§2.8/Q4).
- **Per-MCP-server secrets** — optional, one or more per server. Each MCP server config carries its own
  `#`-prefixed secret field(s) (e.g. `parameters.mcp_servers[i].#token`); wired into that server's
  `env` (stdio) or `Authorization: Bearer` header (HTTP/SSE). See §2.5.

All `#` fields are `KBC::ProjectSecure::…` at rest and arrive **decrypted** at runtime
(`encryption.md`). The Pydantic model uses `Field(alias="#anthropic_key")` etc. so the model attribute
is a clean name while the JSON key keeps the `#`.

### 2.5 MCP servers → `mcp_servers`

Arbitrary user-supplied MCP servers, both transports (verified shapes, `mcp` doc / `types.py`):

- **stdio:** `{"command": "uvx", "args": [...], "env": {"TOKEN": "<#secret>", ...}}` — launched as an
  in-container subprocess; secrets go in `env`.
- **HTTP / SSE:** `{"type": "http"|"sse", "url": "https://…", "headers": {"Authorization": "Bearer <#secret>"}}`
  — remote; secrets go in `headers`.

A server's MCP **tools must be opted into** via `allowed_tools` as `mcp__<server>__<tool>` (wildcard
`mcp__<server>__*` allowed) — without an allow entry the model sees but cannot call them. We surface
the Keboola MCP server as a one-click convenience (it needs `KBC_STORAGE_TOKEN` + `KBC_STORAGE_API_URL`,
which require **`forward_token: true`** in the Dev Portal — see §2.9 and §9 risk R6), and accept fully
generic servers.

### 2.6 Dynamic output tables → Keboola output mapping

Grounded in `output-mapping.md`, `default-bucket.md`, `native-data-types.md`:

- **Runtime-decided tables.** The agent (and/or the component, on the agent's behalf) writes
  `<name>.csv` + `<name>.csv.manifest` into `/data/out/tables/` at runtime via the python-component
  library `create_out_table_definition()` + `write_manifest()`. No pre-declared output mapping.
- **Destination / bucket.** We set **`defaultBucket: true`** in the Dev Portal (Phase 6) so outputs
  route to `in.c-keboola.app-claude-sdk-{configId}` and we never hard-code a bucket. With
  `default_bucket` on, any manifest `destination` is **silently overridden** — so we leave `destination`
  unset and rely on the table file name. (Decision 6a.)
- **Native types.** Dev Portal **`dataTypeSupport: authoritative`** (CF default, set in Phase 6 via
  `kbagent dev-portal patch`). We emit a **`schema`** manifest (authoritative format: `schema` with
  `data_type.base.type`), and the library auto-detects `KBC_DATA_TYPE_SUPPORT` to pick the format. For
  **agent-produced** tables we cannot always know rich types, so the default is an all-STRING
  `schema` — acceptable here because columns are agent-defined (this is the one place an all-STRING
  authoritative schema is legitimate; noted for the Phase-8 reviewer). The **fixed framework tables**
  (sessions/usage, §2.6.1) get proper native types.
- **`has_header` agreement.** We write **headered** CSVs (better for human debugging) and therefore
  pass **`has_header=True`** to `create_out_table_definition` so Storage skips the header line instead
  of ingesting it as data. The write path and the manifest must agree (native-data-types.md silent
  failure). This is verified on the Phase 7 real smoke run (datadir tests can't catch a Storage typing
  failure).
- **Incremental / PK.** Per output, `incremental` + `primary_key` come from the agent's declared intent
  (default `incremental=false` = overwrite, which is the safe default for a re-run). When an output is
  marked incremental we **require** a primary key (else unbounded append). The fixed sessions/usage
  tables are **`incremental=true` with a PK** so multiple tasks/runs accumulate.
- **Scratch / workspace.** The agent's working `cwd` is a `/tmp/...` workspace, and the writable
  `CLAUDE_CONFIG_DIR` is under `/tmp` too — **never** `/data/out/tables/` (or scratch becomes spurious
  tables). State, if any, goes to `/data/out/state.json`.

#### 2.6.1 Always-on JSONL session transcript (decision 3) — BOTH a file and a table, `write_always`

Every run writes the SDK's streaming session transcript **regardless of success/failure**, via **two
complementary sinks, both `write_always`:**

1. **Raw JSONL file artifacts → `/data/out/files/`.** As each message is yielded by `query()` we
   append it (serialized to one JSON line) to `/data/out/files/claude_session_<task_id>.jsonl`, and its
   `.manifest` sets **`write_always: true`** and tags `["claude-sdk","session-transcript"]`. After the
   loop we also locate the SDK's own on-disk transcript
   (`$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/<session_id>.jsonl`) and copy it alongside as
   `claude_session_<task_id>_sdk.jsonl` (belt-and-suspenders: the streamed tee survives partial runs;
   the on-disk file is the SDK's canonical record). The file sink preserves the **full fidelity** of
   every event including large tool payloads.
2. **A structured sessions table → `/data/out/tables/claude_sessions.csv`** (+ `.manifest`,
   `write_always: true`, `incremental: true`, PK `["task_id","session_id","seq"]`, native `schema`).
   One row per JSONL event, columns: `task_id, session_id, seq, ts, type, subtype, role, text,
   tool_name, tool_input_json, tool_result_json, is_error, raw_json`. This makes the transcript
   **queryable in Storage** without parsing files. `raw_json` carries the verbatim line so nothing is
   lost; the typed columns are the convenience projection.

We also always write a **`claude_runs.csv`** summary table (one row per task) from the final
`ResultMessage`: `task_id, session_id, success, subtype, is_error, num_turns, duration_ms,
total_cost_usd, model, sdk_version_resolved, plugins_resolved, api_error_status, result_text`.
`sdk_version_resolved` is the SDK version actually used (§2.10) and `plugins_resolved` the resolved
plugin refs (§2.8) — so a `latest` run for either is traceable. `write_always: true`,
`incremental: true`, PK `["task_id","session_id"]`, native `schema` with real numeric types. This is
the usage/cost report (§9) and the at-a-glance run outcome.

**Why both file + table:** the table is for querying/monitoring/cost-tracking in SQL; the file is the
full-fidelity debug artifact (some tool payloads are large/awkward for a table cell). The brief
requires "all the session JSONL lines … regardless of success/failure" — `write_always` on both
satisfies "regardless of failure"; the dual sink satisfies "all the lines" without lossy truncation.

### 2.7 Budget ceiling & cost

`max_budget_usd` is a **config-level hard ceiling** (default surfaced in §5) and is enforced as the
upper bound for any per-task override (§2.3). For the **Phase 7 cf-dev smoke runs** there is a separate
hard ceiling of **$10** (the build decision) — the component clamps the effective per-task budget to
`min(task_budget, config_budget)` and, when running in cf-dev smoke context, the config budget is set
≤ $10. There is **no top-level wall-clock timeout** in the SDK, so runs are bounded by `max_turns` +
`max_budget_usd` together (both always set; see §9).

### 2.8 Runtime plugins — public + private, pin or latest (decision 9, refined)

Plugins **cannot** be baked into the read-only image, so they are added/installed at job start into a
writable `/tmp/claude-home` (via `CLAUDE_CONFIG_DIR` / `CLAUDE_CODE_PLUGIN_CACHE_DIR`) and loaded into
the SDK by local path (the Python SDK only supports `{"type":"local","path": …}`). The config models
plugins as a **structured list** that handles **both public and private sources** and lets **each entry
choose a pinned ref or `latest`:**

**`plugins`** (a list; each entry):

| Field | Meaning |
|---|---|
| `source` | The marketplace source. Either a **known-public shorthand** (e.g. `superpowers`, resolved to its canonical public GitHub `owner/repo` from a small built-in registry of vetted public marketplaces) **or** an explicit source: `owner/repo` (GitHub), a git URL, or a remote `marketplace.json` URL. **Private** sources (e.g. the CF Kit's private repo) use the same `owner/repo`/URL form and authenticate via `#github_token` (§2.4) — no separate field needed. |
| `private` | bool, default inferred (`false` for shorthands, else the user can mark `true`); if `true`, requires `#github_token` to be set or it's a `UserException`. |
| `plugins` | the plugin name(s) to install from that marketplace (list[str]; or `["*"]` / omitted = install all the marketplace offers). |
| `version` | **`latest`** (default) **or a pinned ref** — a git tag, commit SHA, or branch. Per entry. |

**Runtime flow per entry** (concrete in §6.4): `claude plugin marketplace add <resolved-source>` →
if `version=latest`: `claude plugin marketplace update` (re-pull newest) ; if `version=<ref>`: add the
marketplace at that ref (the source already carries `@ref`/`#ref` so the cached version is pinned) →
`claude plugin install <plugin>@<marketplace>` → collect the resulting cache path(s) → pass as
`plugins=[{"type":"local","path": <cache-path>}]`. **Examples:** a public `{source: "superpowers",
version: "latest"}` and a private `{source: "keboola/cf-claude-code-kit", private: true, plugins:
["component-developer"], version: "v1.4.0"}` (authed by `#github_token`) can both appear in one config.

**Pin/latest semantics are unified with the SDK update model (§2.10):** `version: <ref>` =
reproducible pinned install (no `marketplace update`); `version: latest` = re-pull newest on every run
(not reproducible — the resolved ref is recorded in the run log). Private sources authenticate via
`GITHUB_TOKEN`/`GH_TOKEN` (and `GITLAB_TOKEN`/`BITBUCKET_TOKEN` if supplied) injected into the
subprocess `env`. A fetch/install failure for a user-supplied source is a clear `UserException` (the
`CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE=1` env keeps a usable stale cache when a `latest`
re-pull fails mid-run).

### 2.9 Platform env & token forwarding

### 2.9 Platform env & token forwarding

- `KBC_DATADIR` (=`/data/`), `KBC_COMPONENTID`, `KBC_CONFIGID`, `KBC_BRANCHID` (absent on default
  branch) used as normal; all handled by the python-component library.
- **Keboola MCP server** (optional convenience server) needs `KBC_STORAGE_TOKEN` + `KBC_STORAGE_API_URL`.
  Those require **`forward_token: true`** in the Dev Portal (needs Keboola approval — §9 R6). If not
  granted, the Keboola-MCP convenience is unavailable but the component still works with the
  user-supplied Anthropic key and any **non-Keboola** MCP servers. We do **not** make `forward_token`
  a hard dependency of the build.

### 2.10 Runtime SDK / CLI version (decision 1a — optional runtime upgrade)

The image **bakes `claude-agent-sdk==0.2.101`** (+ its bundled CLI) and rebuilds rarely. To let a user
move to a newer SDK/CLI **without an image rebuild**, an optional config field **`sdk_version`** controls
the SDK used at runtime:

| `sdk_version` value | Behaviour |
|---|---|
| `pinned` (**default**) | Use the image's baked `0.2.101`. **No network**, deterministic, fastest. The recommended/normal path. |
| a concrete version (e.g. `0.2.105`) | At job start, `pip install --upgrade --target /tmp/sdk-overlay "claude-agent-sdk==0.2.105"` and prepend `/tmp/sdk-overlay` to `sys.path` **before** `import claude_agent_sdk` is used for the run. |
| `latest` | Same, with `"claude-agent-sdk"` (no version) so pip resolves the newest. |

**Mechanism (concrete).** `/tmp` is writable, the image is read-only, so we install into an **overlay
dir** `/tmp/sdk-overlay` and put it first on `sys.path` (equivalently set `PYTHONPATH`) so the runtime
version shadows the baked one. **Because the SDK bundles the CLI pinned to the package version, upgrading
the package upgrades the CLI too** — one `pip install` moves both. The upgrade runs **once, before the
agent loop** (in `__init__`/early `run()`), so the rest of the run imports the resolved version. The
import of `claude_agent_sdk` in `ClaudeRunner`/`PluginManager` is deferred until after the overlay is on
the path.

**Risk caveats (explicit):**
- **Offline / registry failure.** If `sdk_version != pinned` and the `pip install` fails (no egress,
  registry down, bad version), behaviour is governed by `sdk_version_on_failure` (default **`fail`** →
  `UserException` with the pip error, so the user knows their requested version didn't load; alternative
  **`fallback_pinned`** → log a warning and continue on the baked `0.2.101`). Default is `fail` so a
  user who asked for a specific version is never silently downgraded.
- **SDK ↔ bundled-CLI skew.** A runtime-upgraded package brings its own matching bundled CLI, so the
  Python layer and CLI stay in lockstep (no skew **within** the upgraded package). The only skew risk is
  our code vs a much newer SDK API — mitigated by the single `ClaudeRunner` seam and by recommending
  `pinned` for production; `latest` is opt-in and at the user's risk (documented).
- **Egress.** Requires outbound HTTPS to the Python package registry at job start — same egress class as
  plugin/MCP fetches. In a locked-down stack with no egress, only `pinned` works (documented).
- **Determinism.** `pinned` is reproducible; `latest` is not (the resolved version can change run to
  run) — surfaced in the field description, and the **resolved SDK version is recorded** in the
  `claude_runs` table for traceability.

This unifies with the plugin pin/latest model (§2.8 / §6.4): both let the user choose a fixed version
for reproducibility or `latest` for freshness, both fetch into a writable `/tmp` location at job start,
both fail clearly on a fetch error by default.

## 3. Authentication & connection

- **Anthropic API auth:** direct Anthropic API via `ANTHROPIC_API_KEY` (the SDK/CLI reads it from the
  subprocess `env`). Key supplied by the user as `#anthropic_key` config secret (decision: user brings
  their own key — more configurable than the reference's stack-image-param approach; the brief calls
  for "secrets passed via configuration"). **Headless** — a plain secret value, no admin/UI step.
- **Model IDs:** bare strings, no date suffix. Default **`claude-opus-4-8`** (Opus 4.8; CF default,
  most capable); cheaper option **`claude-sonnet-4-6`**; **`claude-haiku-4-5`** for the cheapest.
  Exposed as a dropdown (§5).
- **GitHub auth:** optional `#github_token` (fine-grained PAT: Contents r/w, Pull requests r/w; or
  classic `repo`). Injected as `GITHUB_TOKEN`/`GH_TOKEN`; `gh`/`git` pick it up automatically.
- **Provisioning verdict (from research):** fully headless — API keys and git tokens are plain
  env/secret values; no platform-admin or vendor-UI app-registration step is required to run the agent.
  The only vendor-identity (non-admin) actions are Dev Portal property setup in Phase 6
  (`dataTypeSupport`, `defaultBucket`, `forward_token`) via `kbagent dev-portal`.
- **Blockers / access:** (a) a real **funded `ANTHROPIC_API_KEY`** is required for any end-to-end run —
  provided via local `secrets.json` for tests and a cf-dev config secret for Phase 7. (b) A **GitHub
  PAT** only if the smoke test exercises the GitHub/private-plugin path. (c) `forward_token` approval
  only if the Keboola-MCP convenience is wanted. None blocks the core build. **Bedrock/Vertex** are
  out of scope for v1 (documented future option via `ANTHROPIC_BASE_URL` /
  `CLAUDE_CODE_USE_BEDROCK`/`_VERTEX`).

## 4. Data model & "endpoints"

There is no REST API to paginate. The "data model" is the SDK interaction:

- **Entry point:** the async `query(prompt=..., options=ClaudeAgentOptions(...))` generator
  (single-shot), wrapped in one `asyncio.run(...)` per task inside the sync `run()`. (We use `query`,
  not `ClaudeSDKClient`, because each task is single-shot.)
- **Message stream (verified `__all__`/`types.py`):** `SystemMessage` (init, carries `session_id`) →
  `AssistantMessage` (content blocks: `TextBlock`, `ToolUseBlock`, `ToolResultBlock`, `ThinkingBlock`)
  → `UserMessage` → terminal **`ResultMessage`**.
- **`ResultMessage` fields (verified):** `subtype, duration_ms, duration_api_ms, is_error, num_turns,
  session_id, stop_reason, total_cost_usd, usage, result, structured_output, model_usage,
  permission_denials, errors, api_error_status, uuid`. Budget-cap hit → `subtype` is the budget-error
  subtype (it is a **subtype value**, not a separate option field — see §12 correction).
- **No pagination / rate-limit cursoring** in our code; standard Anthropic 429 `rate_limit_error`
  surfaces as a job error (§6/§9). Cost/usage come off the final `ResultMessage` (§2.6.1, §9).
- **On-disk transcript:** `$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/<session_id>.jsonl`, where
  `<encoded-cwd>` is the absolute cwd with every non-alphanumeric char replaced by `-`. SDK helpers
  `list_sessions()` / `get_session_messages()` enumerate/read it (verified exports).

## 5. Configuration & schema (design-time; JSON built by `component-build-ui` in Phase 4)

All parameters live at **config level** except the per-task `task` block (used only in
config-prompt mode; in tasks-table mode the per-task values come from the input table). Each parameter
below maps to a concrete `ClaudeAgentOptions` field or subprocess `env` var.

### 5.1 Parameter set → SDK option mapping

| Config parameter | Type / default | Maps to |
|---|---|---|
| `#anthropic_key` | secret, **required** | `env["ANTHROPIC_API_KEY"]` |
| `#github_token` | secret, optional | `env["GITHUB_TOKEN"]` + `env["GH_TOKEN"]` |
| `sdk_version` | str, default **`pinned`** (`pinned` / a version like `0.2.105` / `latest`) | runtime SDK+CLI used (§2.10): `pinned`=baked 0.2.101, else `pip install --target /tmp/sdk-overlay` before import |
| `sdk_version_on_failure` | enum, default **`fail`** (`fail` / `fallback_pinned`) | behaviour when a non-`pinned` install fails (§2.10) |
| `model` | enum, default `claude-opus-4-8` (opus-4-8 / sonnet-4-6 / haiku-4-5) | `ClaudeAgentOptions.model` |
| `fallback_model` | enum, optional | `ClaudeAgentOptions.fallback_model` |
| `max_turns` | int, default **20** | `ClaudeAgentOptions.max_turns` (set **explicitly** — SDK default `None` = unbounded) |
| `max_budget_usd` | float, default **10.0**; cf-dev smoke ceiling **10.0** | `ClaudeAgentOptions.max_budget_usd` (hard cap; per-task override clamped ≤ this) |
| `effort` | enum optional (low/medium/high/xhigh/max) | `ClaudeAgentOptions.effort` |
| `permission_mode` | enum, default **`dontAsk`**; viable values only: `dontAsk` / `bypassPermissions` / `auto` (see §6.5 — prompting modes are hidden) | `ClaudeAgentOptions.permission_mode` (always set explicitly; user-selectable) |
| `allowed_tools` | list[str], default `[]` | `ClaudeAgentOptions.allowed_tools` |
| `disallowed_tools` | list[str], default `[]` | `ClaudeAgentOptions.disallowed_tools` |
| `system_prompt` | str, optional | `ClaudeAgentOptions.system_prompt` (config-level default; per-task override from table) |
| `settings_json` | object/string, optional (passthrough) | written to a file under the writable home and passed via `ClaudeAgentOptions.settings` (path) + `setting_sources` |
| `setting_sources` | list enum, default `[]` (no ambient user/project settings) | `ClaudeAgentOptions.setting_sources` |
| `mcp_servers` | list of typed server objects (stdio/http/sse) + per-server `#secrets` | `ClaudeAgentOptions.mcp_servers` (dict) |
| `plugins` | list of entries `{source, private?, plugins[], version}` — public shorthand or `owner/repo`/git-url; `version` = `latest` (default) or a pinned tag/SHA/branch; private via `#github_token` (§2.8) | per entry: `claude plugin marketplace add` (+`marketplace update` if `latest`) + `claude plugin install`, then `plugins=[{type:local,path}]` |
| `github_enabled` | bool, default **false** | enables `Bash` + `gh`/`git` working dir + injects token; convenience toggle that allow-lists `Bash(gh *)`,`Bash(git *)` |
| `workspace_input_files` | bool, default **false** | if true, stage `/data/in/files/` into the agent `cwd` so the agent can read uploaded files; via `cwd` + `add_dirs` |
| `output.default_incremental` | bool, default **false** | default `incremental` for agent-produced tables |
| `task_id_filter` | str or list[str], optional (default: all rows) | row selector for tasks-table mode — process only the matching `task_id`(s); no match → `UserException`; ignored in config-prompt mode (§2.3.1) |
| `task.prompt` | str (config-prompt mode) | the prompt when no `tasks` table mapped |
| `task.system_prompt` | str, optional | per-run system prompt in config-prompt mode |
| `debug` | (platform-handled) | **NOT** a config-model field — the component base consumes the platform `debug` param and switches the root logger to DEBUG automatically (configuration.md). We do not add a model `debug`. |

### 5.2 Conditional UI (for `component-build-ui`)

- `github_enabled` true → reveal a note that `#github_token` is required and `Bash` is enabled.
- `mcp_servers[i].type` switches between stdio fields (`command`,`args`,`env`,`#secrets`) and
  http/sse fields (`url`,`headers`,`#secrets`) via `options.dependencies`.
- `plugins` is a collapsible advanced array; each entry has `source` (text — public shorthand or
  `owner/repo`/URL), `private` (bool; when true a note reminds `#github_token` is required), `plugins`
  (string list), and `version` (text — `latest` or a pinned tag/SHA/branch). `sdk_version` /
  `sdk_version_on_failure` sit in an advanced group with a description warning that non-`pinned` needs
  egress and `latest` is non-reproducible (§2.10).
- `task_id_filter` is a free-text / array field (a `task_id` or list of `task_id`s) presented near the
  tasks-table mapping, with a description that it only applies when a `tasks` table is mapped and that
  leaving it empty runs all rows (§2.3.1). No async/enum dropdown in v1 (the `task_id` set lives in the
  input table, not the config). `task.*` is the config-prompt-mode group.
- Use `options.dependencies` for all conditionals (never root-level `dependencies`).

### 5.3 Sync actions

- **`testConnection`** (`format: "test-connection"` widget): validate `#anthropic_key` by making a
  **single cheap Anthropic Messages API call** (one token, Haiku) directly via a tiny HTTP check —
  **not** by spawning the agent loop. Returns success/failure to the UI. (This is the one place we hit
  real Anthropic HTTP in-process, which makes it VCR-recordable — see §7.)
- No dynamic-dropdown sync actions in v1 (model list is a static enum; tool/plugin lists are
  free-form). Listed as a future enhancement.

`testConnection` requires a **partial** config (only `#anthropic_key`) — the Pydantic model must
instantiate from just that field for the sync action without requiring `task`/`prompt`.

## 6. Code architecture

`run()` is a thin orchestrator (<30 lines) delegating to private methods; clients created in
`__init__`. One Pydantic config model tree. Modules:

```
src/
  component.py            # Component(ComponentBase): __init__ wires clients; run() orchestrates
  configuration.py        # Pydantic models (Configuration + nested: McpServerConfig, PluginEntry list, TaskConfig, OutputConfig; sdk_version/permission_mode validators)
  sdk_version_manager.py  # SdkVersionManager: optional runtime pip-overlay of claude-agent-sdk (§2.10); runs FIRST, before any SDK import
  claude_runner.py        # ClaudeRunner: owns ClaudeAgentOptions build + query() loop + result capture (the SDK boundary; imports claude_agent_sdk lazily)
  plugin_manager.py       # PluginManager: writable CLAUDE_CONFIG_DIR + `claude plugin` CLI add/update/install (pin|latest per entry — §6.4)
  transcript_writer.py    # TranscriptWriter: streams JSONL file + builds claude_sessions / claude_runs tables (write_always)
  output_writer.py        # OutputWriter: agent-produced table CSV+manifest helpers (native schema, has_header, PK/incremental)
  tasks.py                # TaskSource: reads config-prompt OR the `tasks` input table into a list[Task]; applies task_id_filter
  sync_actions.py         # testConnection (cheap in-process Anthropic HTTP check)
```

### 6.1 `run()` orchestration (the shape)

1. Parse `config.json` → `Configuration` (Pydantic), raising `UserException` on validation error.
1a. `SdkVersionManager.ensure(config.sdk_version, config.sdk_version_on_failure)` (§2.10): if not
   `pinned`, pip-install into `/tmp/sdk-overlay`, prepend to `sys.path`; record the **resolved SDK
   version** for `claude_runs`. **Runs before any `claude_agent_sdk` symbol is used** (ClaudeRunner /
   PluginManager import it lazily).
2. Build the writable home + `PluginManager.prepare()` (env dirs, marketplace add/update/install) →
   returns local plugin paths.
3. `TaskSource.load()` → `list[Task]` (config-prompt single task, or rows of the `tasks` table after
   applying the `task_id_filter` row selector — §2.3.1; no filter match → `UserException`).
4. For each task: `ClaudeRunner.run_task(task, plugin_paths)` inside `asyncio.run`, teeing every
   message to `TranscriptWriter` (file + sessions rows) as it arrives; capture the final
   `ResultMessage`.
5. `OutputWriter` flushes any agent-declared tables (manifests) and `TranscriptWriter` flushes
   `claude_sessions` + `claude_runs` (always, `write_always`).
6. Decide exit: if **any** task ended with `is_error`/budget-or-turn-cap and the config says fail-on-
   task-error (default true), raise `UserException` (exit 1) with a summary; else exit 0. Transcript
   tables are already `write_always`, so they survive the failure.

### 6.2 The SDK boundary — `ClaudeRunner`

Owns building `ClaudeAgentOptions` from the merged (config + task) settings and running the
`async for message in query(...)` loop. Reuses the proven pattern from the reference
`agent_client.py`: an `AgentExecutionResult`-style dataclass capturing
`success/result_text/total_cost_usd/duration_ms/num_turns/session_id/subtype/is_error/usage`. Injects
`env` (Anthropic key, GitHub token, MCP secrets, plugin-source tokens, `CLAUDE_CONFIG_DIR`,
`CLAUDE_CODE_PLUGIN_CACHE_DIR`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`). Sets `cwd` to the `/tmp`
workspace. **This is the single function the tests mock** (§7).

### 6.3 Error handling — exit codes

- **`UserException` (exit 1):** missing/invalid config (no `#anthropic_key`, bad model enum), missing
  required `tasks` column, **`task_id_filter` matching no row** (message names the filter value(s) and
  the available `task_id`s), empty prompt, budget/turn cap hit when fail-on-error is on, Anthropic auth
  failure (401), plugin marketplace add/install failure for a user-supplied source, MCP server launch
  failure. All user-actionable, message shown in UI.
- **Unexpected (exit 2):** any other unhandled exception (SDK internal crash, unexpected message
  shape). Bubbles up via the standard `__main__` guard (already in the scaffold).
- Rate-limit 429 → `UserException` with a clear "Anthropic rate limit, retry later" message.

### 6.4 `PluginManager` mechanics (concrete)

`prepare(plugins: list[PluginEntry], env) -> list[SdkPluginConfig]`:
1. `mkdir -p /tmp/claude-home` ; set `env CLAUDE_CONFIG_DIR=/tmp/claude-home`,
   `CLAUDE_CODE_PLUGIN_CACHE_DIR=/tmp/claude-home/plugins/cache`,
   `CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE=1`.
2. **Resolve `source`** (§2.8): a known-public shorthand (e.g. `superpowers`) → its canonical public
   `owner/repo` via a small built-in registry of vetted public marketplaces; otherwise use the explicit
   `owner/repo`/git-url/marketplace.json verbatim. If `private` and no `#github_token` → `UserException`.
3. **Per entry, version-aware add:**
   - `version: <ref>` (pinned tag/SHA/branch) → `claude plugin marketplace add <source>@<ref>` (or the
     `#ref` form for a git URL) so the cached marketplace is **pinned**; do **not** run
     `marketplace update` (pinning is reproducible).
   - `version: latest` → `claude plugin marketplace add <source>` then `claude plugin marketplace
     update` to re-pull the newest commit/tag.
4. **Install:** for each requested plugin name (or all if `["*"]`/omitted):
   `claude plugin install <plugin>@<marketplace>`.
5. Resolve installed cache paths (`claude plugin marketplace list --json`) and return them as
   `[{"type":"local","path": <cache-path>}]` for `ClaudeAgentOptions.plugins`. **Record the resolved
   version/ref** per plugin for the run log (so a `latest` run is traceable).
   Private sources authenticate purely via the env tokens already injected (`GITHUB_TOKEN`/`GH_TOKEN`,
   etc.). No-op cleanly (returns `[]`) when `plugins` is empty.
All `claude plugin` invocations are `subprocess.run` with captured output logged (never echoing
secrets), non-zero exit → `UserException` naming the failing source. **Validated end-to-end at Phase 7
S4** (the non-interactive behaviour, the `@ref` pin form, and the `<encoded-cwd>` path are doc-level
claims per the research carry-forward).

### 6.5 Permission / sandbox model — `permission_mode` is user-configurable

`permission_mode` is a **user-configurable** config field over the SDK `PermissionMode` enum (verified
literal: `default | acceptEdits | plan | bypassPermissions | dontAsk | auto`), set **explicitly** on
every run (the SDK default `None` behaves like interactive `default` and would hang headless — §12).
The **default is `dontAsk`** (the safest non-prompting mode: deny anything not explicitly allow-listed,
never prompt), but the user may choose another mode.

**Headless viability — only non-prompting modes are offered.** A Keboola job is non-interactive: any
mode that pauses for an approval prompt will **hang the container until the job times out**. So:

| Mode | Headless? | Surface in UI? |
|---|---|---|
| `dontAsk` | ✅ non-prompting (deny un-allow-listed, no prompt) | ✅ **default** |
| `bypassPermissions` | ✅ non-prompting (auto-approve all; hooks + deny rules still apply) | ✅ (powerful — documented as "agent can run anything allowed; use deliberately") |
| `auto` | ✅ non-prompting (classifier-gated auto-decision, no human prompt) | ✅ |
| `acceptEdits` | ⚠️ prompts for non-edit tools → can hang | ❌ hidden |
| `default` | ❌ prompts on unmatched tools → hangs | ❌ hidden |
| `plan` | ❌ prompts on edits → hangs | ❌ hidden |

We therefore surface **only `dontAsk`, `bypassPermissions`, `auto`** in the UI enum (the three viable
non-prompting modes) and document the rationale; the Pydantic model validates against that allowed set
and rejects a prompting mode with a clear `UserException` (so a hand-edited config can't silently hang).
The **Keboola container is the sandbox** (process isolation, read-only image, controlled egress); the
SDK provides no OS-level sandbox. We pair the mode with `allowed_tools`/`disallowed_tools` and a
constrained `/tmp` `cwd`. Tool names: built-ins `Read/Write/Edit/Bash/Glob/Grep/WebFetch/WebSearch`
(scoped patterns like `Bash(git *)` supported); MCP `mcp__<server>__<tool>`.

### 6.6 Key dependencies

- **`claude-agent-sdk==0.2.101`** — pinned in the image, verified on PyPI (uploaded 2026-06-13,
  `requires-python >=3.10`, compatible with the scaffold's 3.14). **Bundles a native Claude Code CLI
  binary per platform** (confirmed: the release ships platform-specific wheels incl.
  `manylinux_2_17_x86_64`, plus the package description states the CLI is auto-bundled, no Node
  needed). → **Dockerfile is Python-only (uv), NO Node.** (See §11 infra + the §12 fallback.) The
  runtime `sdk_version` field (§2.10) can shadow this baked version with a `/tmp/sdk-overlay` install;
  the `import claude_agent_sdk` used for the run is therefore **deferred** until after the overlay (if
  any) is on `sys.path` — `SdkVersionManager` (§6 module list) owns this and is the first thing `run()`
  does.
- `keboola-component>=1.10.0` (datadir, manifests, exceptions), `pydantic>=2.11.7`.
- `uvx` (from uv, already in the image) to launch stdio MCP servers and is the `claude plugin` runtime;
  `pip` (present in the slim image) performs the optional runtime SDK overlay install.

## 7. Testing (decision 8 — the unusual part)

**The core constraint:** `query()` **spawns the `claude` CLI as a subprocess** that makes its **own**
outbound HTTPS to `api.anthropic.com`. In-process HTTP-recording (VCR/`responses`) patches the *Python*
process's HTTP stack and therefore **cannot capture** the subprocess's traffic. So we do **not** try to
VCR the agent loop. The strategy is three layers:

1. **Mock at the `claude_agent_sdk.query()` boundary (primary).** `query()` is an async generator. In
   tests we monkeypatch it (or the thin `ClaudeRunner` seam that calls it) to yield a **canned, typed
   message stream** — `SystemMessage(init, session_id=…)`, `AssistantMessage([...TextBlock/
   ToolUseBlock/ToolResultBlock...])`, terminal `ResultMessage(...)` — assembled from small fixture
   JSON files. This deterministically exercises the whole pipeline (transcript file + `claude_sessions`
   + `claude_runs` + agent-produced output tables + exit-code logic) with **no network and no
   subprocess**. Separate fixtures for: happy path, budget-cap `subtype`, `is_error` failure,
   multi-task (`tasks` table) run.
2. **Datadir tests (`keboola.datadirtest`) for config parsing & output/manifest correctness.** Each
   `tests/functional/<case>/` has a `config.json` (single merged shape, no row/root split) and expected
   `out/` files/manifests. The SDK boundary is mocked via a test conftest that installs the canned
   stream, so datadir cases assert: required-field validation → exit 1; manifest format (authoritative
   `schema`, `has_header`, PK/incremental); `write_always` on transcript tables; `claude_sessions`
   rows match the canned stream; dynamic agent tables land with correct manifests.
3. **VCR for the one real in-process HTTP call — `testConnection`.** The sync action makes a single
   Anthropic Messages API call **in-process** (not via the CLI subprocess), so it **is** VCR-recordable.
   We record one success and one auth-failure cassette with `VCR_SANITIZERS` scrubbing the
   `x-api-key`/`authorization` headers and the key value. This is the only place real HTTP VCR applies.

**How `secrets.json` is consumed given the command-line hook constraint.** The PreToolUse hook blocks
*agent* reads of `secrets.json` and any shell command containing the substring — it does **not** block
the **component process or the test runner** from opening the file at runtime (those don't echo
values). The datadir/VCR framework injects secrets the standard CF way: a fixture-prep step reads
`secrets.json` (the runner does this itself; the agent never does) and merges `parameters.#anthropic_key`
into each test case's `config.json` before the run, exactly mirroring the live `secrets.json` shape
`{"parameters": {"#anthropic_key": "…"}}`. The agent only ever references the **key name**. For
mock-boundary tests no real key is needed (the stream is canned); only the `testConnection` VCR
record/replay needs the real key, and replay uses the sanitized cassette (no key required to replay).

**Sync-action tests:** unit-test `testConnection` against the VCR cassettes (success + 401).

## 8. cf-dev smoke scenarios (Phase 7 plan) — an extensible progression

Built as an **extensible scenario set** (not one smoke test), each a kbagent-created config in **cf-dev**
with the **image tag overridden** to the `initial-implementation` build, each capped at
**`max_budget_usd ≤ $10`**. The progression mirrors the component-development phases:

1. **S1 — simple prompt → output.** Config-prompt mode, no MCP/plugins/GitHub, `model=haiku`,
   `max_turns` small, a trivial prompt that writes one small output table. Asserts: job `success`,
   `claude_sessions`/`claude_runs` populated, one agent output table present, **resolved image tag ==
   the branch build** (not a stale stable). The minimal proof.
2. **S2 — tasks-table mode.** Map a small `tasks` input table (2-3 rows), assert one run per row and
   per-task rows in `claude_runs`.
3. **S3 — MCP server.** Add one generic MCP server (e.g. a public read-only HTTP MCP) with a
   `#secret`, allow-list its tools, assert the agent calls a tool (visible in `claude_sessions`).
4. **S4 — plugin add/update at runtime (public pin + latest; private if token available).** Add a
   public marketplace shorthand at a **pinned** `version` and a second at `latest`, install a plugin
   from each, assert both load and the resolved refs land in `claude_runs.plugins_resolved`; if a
   `#github_token` is provided, add one **private** source too. Validates the §6.4 mechanism
   end-to-end — the research carry-forward to confirm here.
   - **S4b — runtime SDK overlay (§2.10).** A scenario with `sdk_version` set to a concrete newer
     version, asserting `claude_runs.sdk_version_resolved` reflects it (and that `sdk_version=pinned`
     uses the baked `0.2.101`).
5. **S5 — GitHub working.** With a scoped PAT, a read-only GitHub task (clone + summarise), assert
   success without leaking the token.

**TTY-gated steps that can't be automated yet** (acknowledged): the Dev Portal value setup
(`forward_token`, etc.) and the kbagent dev-portal writes are interactive/TTY-confirmed; S3-S5 needing
extra secrets are gated on the lead/user providing them. The harness is structured so scenarios are
added as plain config recipes; S1 is the must-pass, S2-S5 extend as credentials/approvals land.

## 9. Cost / rate-limit controls + usage reporting

- **Controls:** `max_turns` (always set; default 20) **and** `max_budget_usd` (always set; default 10,
  cf-dev ceiling 10) bound every run; `model`/`effort` tier trades cost vs capability; optional
  `PreToolUse` hook reserved for future hard guardrails (e.g. block `Bash(rm *)`). Per-task budget is
  clamped to the config ceiling. No SDK wall-clock timeout exists — turns+budget are the levers.
- **Usage reporting:** the `claude_runs` table (§2.6.1) surfaces `total_cost_usd`, `num_turns`,
  `duration_ms`, `usage`/`model_usage` (folded into the table or the JSONL) per task. Note
  `total_cost_usd` is a **client-side estimate** from the SDK's bundled price table — fine for
  surfacing/monitoring, not authoritative billing (logged with that caveat).
- **Rate limits:** Anthropic 429 `rate_limit_error` → clear `UserException`; large parallel-subagent
  fanouts can hit limits (we don't fan out by default).

## 10. Deployment & validation (CF test project)

- Phase 6 (`component-dev-portal` via `kbagent`): set `defaultBucket: true`, `dataTypeSupport:
  authoritative`, the configSchema + `testConnection` sync action, descriptions/UI options; optionally
  request `forward_token: true` (R6).
- Phase 7 (`component-test`): build an `initial-implementation` image, create the S1 config in cf-dev
  with the image tag overridden, run it, confirm `success` + the resolved image tag matches the branch
  build + the expected output/transcript tables exist (read from the platform, not trusted from the
  worker). Then extend with S2-S5 as credentials allow.

## 11. Infra (Dockerfile / pyproject) — concrete

- **Dockerfile: Python-only, no Node.** Keep the scaffold's `python:3.14-slim` + `uv` multi-stage build
  unchanged in shape; the SDK's bundled `manylinux_2_17_x86_64` CLI binary runs on the slim/glibc base.
  No `apt-get install nodejs`/`npm install -g @anthropic-ai/claude-code` (that step is **obsolete** on
  0.2.x — the reference repo's Node layer is removed for us). `uvx`/`uv` already present for stdio MCP
  servers + `claude plugin`. `/tmp` is writable in the container for `CLAUDE_CONFIG_DIR` + workspace +
  the optional `sdk-overlay` (§2.10). The runtime SDK overlay + plugin/MCP fetches need outbound HTTPS
  egress at job start; a no-egress stack runs with `sdk_version=pinned` and no remote plugins/MCP.
- **pyproject:** add `claude-agent-sdk==0.2.101` to `dependencies`; keep `requires-python = ~=3.14.0`
  (SDK allows `>=3.10`). VCR dev deps (`pytest-recording`/`vcrpy`) added in Phase 5. Remove the unused
  `keboola-http-client`/`keboola-utils` if not needed, or keep `keboola-http-client` for the
  `testConnection` HTTP check.

## 12. Corrections vs the Phase 2 research summary (verified against live 0.2.101 source)

- **`error_max_budget_usd` is NOT a field.** The research summary called `max_budget_usd` "result
  subtype `error_max_budget_usd`" / implied an `error_max_budget_usd` companion option. Verified: there
  is **only** `max_budget_usd` on `ClaudeAgentOptions`; the budget-cap-hit is signalled via the
  **`ResultMessage.subtype`** value. The spec uses subtype, not a field.
- **`permission_mode` default is `None`** (behaves like interactive `default`), so we **always set it
  explicitly**. It is user-configurable (§6.5) but the UI offers **only the non-prompting modes**
  (`dontAsk` default, `bypassPermissions`, `auto`); prompting modes would hang headless. Verified enum
  literal: `default | acceptEdits | plan | bypassPermissions | dontAsk | auto`.
- **Concrete pin = `claude-agent-sdk==0.2.101`** (the summary said "≈0.2.10x"); bundled-CLI confirmed
  via platform wheels. Node-free Dockerfile confirmed viable.
- **Node-install fallback (only if the bundled binary ever fails on the slim base):** add
  `apt-get install -y nodejs npm && npm install -g @anthropic-ai/claude-code` and point
  `ClaudeAgentOptions.cli_path` at the system `claude`. Not expected to be needed; documented for
  completeness.

## 13. Open risks & mitigations

| # | Risk | Mitigation | Owner |
|---|---|---|---|
| R1 | Bundled `manylinux_2_17` CLI binary fails to run on `python:3.14-slim` (glibc/libstdc++ mismatch). | Verify in the Phase 7 image build / a quick container smoke before S1; Node fallback (§12) ready. | Phase 4/7 |
| R2 | `claude plugin marketplace add/update/install` non-interactive behaviour differs from docs (prompts, exit codes, cache paths). | `PluginManager` captures output, fails loud as `UserException`; **validate end-to-end at Phase 7 S4** (the research carry-forward). | Phase 7 |
| R3 | `<encoded-cwd>` transcript path encoding wrong → on-disk JSONL not found. | The **streamed tee** (§2.6.1 sink 1) is authoritative and independent of the path; on-disk copy is best-effort. | Phase 4 |
| R4 | Agent-produced tables have bad/ambiguous types → Storage load fails (header-as-data, type mismatch). | `has_header=True` + headered CSV agreement; all-STRING `schema` default for agent tables; verify on the Phase 7 real run (datadir can't catch typing). | Phase 4/7 |
| R5 | Runaway cost / infinite loop. | `max_turns` + `max_budget_usd` both always set; per-task clamp to config ceiling; $10 cf-dev ceiling. | Phase 4 |
| R6 | Keboola-MCP convenience needs `forward_token: true` (Keboola approval). | Not a hard dependency — component works without it; request in Phase 6 if wanted; document. | Phase 6 |
| R7 | Secret leakage in logs/transcripts (the LIVE guardrail concern). | Never log secret values; `claude plugin`/MCP subprocess output is logged with secret-scrubbing; transcript writer scrubs known secret values; VCR sanitizers; the PreToolUse hook protects the dev loop. | Phase 4/5 |
| R8 | `query()` API shape drift on SDK upgrades. | Hard pin `==0.2.101` baked in; the single `ClaudeRunner` seam localises any future change; runtime `sdk_version=latest` is opt-in and at the user's risk (§2.10). | Phase 4 |
| R9 | Runtime SDK overlay (§2.10) fails (no egress, bad version, registry down) or a `latest` upgrade breaks our code. | Default `sdk_version=pinned` (no network); `sdk_version_on_failure=fail` (default) surfaces the pip error as `UserException` so no silent downgrade; bundled CLI moves with the package (no SDK↔CLI skew within the upgrade); resolved version recorded in `claude_runs`. | Phase 4/7 (S4b) |
| R10 | Plugin pin/latest: a `latest` re-pull fails mid-run, a pinned ref is gone, or a private source lacks the token. | `CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE=1` keeps a usable stale cache on a failed `latest` re-pull; missing token for `private:true` → `UserException`; install failure names the source; resolved refs recorded in `claude_runs.plugins_resolved`. | Phase 4/7 (S4) |
