# Phase 2 Research — `keboola.app-claude-sdk`

> **Scope.** For this component the "source/target system" is **not a REST API** — it is the
> **Claude Agent SDK** (the Python agent-loop SDK, formerly "Claude Code SDK") and how to run it
> headless inside a Keboola Docker container. This document settles the 9 research questions from the
> build-lifecycle tracker with primary-source evidence, then gives a feasibility & provisioning
> verdict.
>
> **Date:** 2026-06-14 · **Branch:** `initial-implementation` · **Owner:** `component-plan-new` (Phase 2)

## Evidence base (primary sources)

All version numbers and option shapes below were verified against live sources on 2026-06-14, not
recalled from memory:

- PyPI / GitHub: `claude-agent-sdk` (Python) — https://pypi.org/project/claude-agent-sdk/ ,
  https://github.com/anthropics/claude-agent-sdk-python
- SDK types (verbatim `ClaudeAgentOptions`, message classes):
  https://raw.githubusercontent.com/anthropics/claude-agent-sdk-python/main/src/claude_agent_sdk/types.py
- Agent SDK docs (code.claude.com): `overview`, `hosting`, `sessions`, `mcp`, `permissions`,
  `cost-tracking`, `plugins`
- Claude Code plugin/marketplace docs (code.claude.com): `plugin-marketplaces`, `plugins`,
  `discover-plugins`, `plugins-reference`, `settings`
- `claude-api` skill (Anthropic Messages API model IDs/pricing) + `keboola-context` references
- Concrete reference component: `keboola/component-agent-runner` (cloned read-only), author David
  Esner — `src/agent_client.py`, `src/component.py`, `src/configuration.py`,
  `component_config/configSchema.json`, `pyproject.toml`/`uv.lock`, `Dockerfile`

---

## Q1 — Headless container execution

**Package + version.** Python package is **`claude-agent-sdk`** (NOT `claude-code-sdk`, NOT raw
`anthropic`). Latest published ≈ **0.2.10x** (June 2026); Python **3.10+**. **The SDK bundles a native
Claude Code binary for the host platform — no separate `@anthropic-ai/claude-code` npm package and no
Node.js are required for the spawned CLI** (hosting doc, verbatim: *"Both SDK packages bundle a native
Claude Code binary for the host platform, so no separate Claude Code or Node.js install is needed for
the spawned CLI… The bundled binary is pinned to the SDK package version, so updating the SDK is how
you update the CLI"*). **Pin a specific `claude-agent-sdk==X.Y.Z` in `pyproject.toml`** — that single
pin fixes both the Python layer and the CLI.

> ⚠️ **Divergence from the reference repo.** `component-agent-runner` pins the *old* `claude-agent-sdk==0.1.18`
> and its Dockerfile installs Node + `npm install -g @anthropic-ai/claude-code`. On modern 0.2.x that
> npm step is **obsolete** — the binary is bundled. Our Dockerfile should be Python-only (uv), no Node.

**Subprocess model (the key runtime fact).** `query()` spawns a `claude` CLI subprocess and talks to
it over **stdio** — so it is headless-native and needs **no TTY**. One session = one subprocess that
owns a shell, a working directory, and the on-disk JSONL transcript. It runs to completion and exits;
the prompt is a function argument (not stdin), and messages come back as typed objects (not parsed
stdout).

**Entry point + options (verbatim `ClaudeAgentOptions` from types.py).** Use the async
`query(prompt=..., options=ClaudeAgentOptions(...))` generator (single-shot) wrapped in one
`asyncio.run(...)` inside the sync `run()`. `ClaudeSDKClient` is the multi-turn alternative; we want
single-shot. Fields that matter for us (all confirmed present):

```python
ClaudeAgentOptions(
    model: str | None,                       # e.g. "claude-opus-4-8" — bare ID, no date suffix
    system_prompt: str | SystemPromptPreset | SystemPromptFile | None,
    max_turns: int | None,                   # bound the agent loop (default None = unbounded)
    max_budget_usd: float | None,            # HARD cost cap (result subtype error_max_budget_usd)
    permission_mode: PermissionMode | None,  # see Q8
    allowed_tools: list[str],                # see Q8
    disallowed_tools: list[str],
    mcp_servers: dict[str, McpServerConfig] | str | Path,  # see Q3
    plugins: list[SdkPluginConfig],          # see Q4 — local paths only
    setting_sources: list[SettingSource] | None,           # ["user"|"project"|"local"] or None
    skills: list[str] | Literal["all"] | None,
    cwd: str | Path | None,                  # working dir = agent's filesystem sandbox root
    add_dirs: list[str | Path],
    env: dict[str, str],                     # injected into the subprocess (API key, GH token, …)
    can_use_tool: CanUseTool | None,         # programmatic per-call permission callback
    hooks: dict[HookEvent, list[HookMatcher]] | None,  # PreToolUse/PostToolUse gating
    agents: dict[str, AgentDefinition] | None,
    sandbox: SandboxSettings | None,
    effort: EffortLevel | None,              # low|medium|high|xhigh|max
    thinking: ThinkingConfig | None,
    fallback_model: str | None,
    session_store: SessionStore | None,      # mirror transcripts off local disk if needed
    output_format: dict[str, Any] | None,    # structured output (json_schema)
    cli_path: str | Path | None, settings: str | None, extra_args: dict[str, str | None],
    include_partial_messages: bool, ...      # (other fields omitted)
)
```

**No-TTY env.** Only `ANTHROPIC_API_KEY` is strictly required (Q7). Recommended for a container:
relocate the writable home with **`CLAUDE_CONFIG_DIR`** (Q2/Q4), and for multi-tenant hygiene
`setting_sources=[]` + `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. The hosting doc resourcing baseline is
**1 GiB RAM, 5 GiB disk, 1 CPU per agent** (a floor, not a ceiling — memory grows with session
length/tool activity).

**max-turns + model.** `max_turns` default is `None` (unbounded) — **we must set it explicitly** so a
run can't loop forever. Model is a bare ID string; default to **`claude-opus-4-8`** per CF
conventions (or expose a dropdown — Sonnet 4.6 for cheaper runs). There is **no top-level session
timeout** (documented "Known limitation") — bound runs with `max_turns` + `max_budget_usd`.

---

## Q2 — Session JSONL transcript (always-on debug output)

**On-disk path (verbatim, sessions doc):** transcripts are written to
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`, **or
`$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/*.jsonl` if `CLAUDE_CONFIG_DIR` is set**, where
`<encoded-cwd>` is *"the absolute working directory with every non-alphanumeric character replaced by
`-`"* (e.g. `/data/work` → `-data-work`). The SDK writes it automatically. Each line is one JSON
event (system/init, user, assistant w/ content blocks, tool_use/tool_result, result w/ cost+usage).

**How to capture ALL lines reliably (always-on debug requirement).** Two complementary mechanisms,
both confirmed:

1. **Read the on-disk JSONL after the run.** We control `cwd` (and can set `CLAUDE_CONFIG_DIR`) so we
   know exactly where the file lands. The SDK exposes helper functions to enumerate/read sessions on
   disk: **`list_sessions()`** and **`get_session_messages()`** (Python). The `session_id` to locate
   the file is on the init `SystemMessage.data["session_id"]` and on the final `ResultMessage.session_id`.
2. **Tee the live stream.** Serialize every message yielded by `query()` to a JSONL artifact as it
   arrives. This survives even partial runs.

**Recommended pattern:** capture the streamed messages to a debug JSONL as they arrive (so it exists
even on failure), AND, after the loop, locate the SDK's own
`$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/<session_id>.jsonl` and copy it out. Write the debug output
with **`write_always: true`** on its output-mapping entry (keboola-context `output-mapping.md`) so it
is uploaded to Storage **even when the job fails** — that is exactly the "regardless of
success/failure" requirement. (`/data/out/state.json` and scratch belong in `/tmp`, never
`/data/out/tables/` — see Q6.)

---

## Q3 — MCP server configuration & launch

**`mcp_servers` shape (verbatim from the SDK MCP doc), per transport:**

- **stdio** (launched as an in-container subprocess):
  `{"<name>": {"command": "uvx", "args": ["some-mcp-server"], "env": {"TOKEN": "..."}}}`
- **HTTP / SSE** (remote): `{"<name>": {"type": "http"|"sse", "url": "https://…", "headers": {"Authorization": "Bearer …"}}}`

A project-root **`.mcp.json`** file uses the same schema and supports `${VAR}` expansion; the in-code
`mcp_servers` dict is the equivalent. `mcp_servers` also accepts a `str | Path` (a path to an
`.mcp.json`). `strict_mcp_config: bool` controls whether *only* the in-code config is honored.

**Token mapping.** stdio servers get secrets via `env`; HTTP/SSE servers via `headers`
(bearer). So a user-supplied MCP config → our schema → for stdio we inject the `#`-secret into the
server's `env`, for HTTP into `Authorization`.

**Tool gating.** MCP tools are named **`mcp__<server>__<tool>`** and must be opted into via
`allowed_tools` (wildcards allowed, e.g. `mcp__github__*`). Without an explicit allow entry the model
sees the tools but cannot call them.

**Reference behaviour.** `component-agent-runner` wires **only** the Keboola MCP server as stdio
(`{"command": "uvx", "args": ["keboola-mcp-server"], "env": {"KBC_STORAGE_TOKEN", "KBC_STORAGE_API_URL", "KBC_BRANCH_ID"}}`)
and has no generic-MCP support. Our component must accept **arbitrary** MCP servers (stdio + HTTP/SSE)
from config, each with its own `#`-secret(s). The Keboola Storage token + API URL come from the
common-interface env (`environment_variables.token`, stack id), as in the reference.

---

## Q4 — Runtime plugin add/update (KEY new requirement)

**What a plugin is.** A self-contained directory that can bundle **skills, agents (subagents), slash
commands (legacy), hooks, and MCP servers** (also LSP servers). On-disk layout: an optional
`.claude-plugin/plugin.json` manifest plus `skills/<name>/SKILL.md`, `agents/`, `hooks/hooks.json`,
`.mcp.json`. If the manifest is omitted, components are auto-discovered from the layout. A
**marketplace** is a catalog repo with `.claude-plugin/marketplace.json` listing plugins and their
sources (relative path / `github` / `url` / `git-subdir` / `npm`).

**Where plugins live on disk.** Marketplace registry: `~/.claude/plugins/known_marketplaces.json`;
versioned plugin cache: `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`. Both move with
**`CLAUDE_CONFIG_DIR`** (relocates `~/.claude`) and the cache specifically with
**`CLAUDE_CODE_PLUGIN_CACHE_DIR`**. → **A writable dir in the container is enough; no image rebuild.**

**THE mechanism — install + update at runtime, non-interactively.** The CLI exposes
`claude plugin …` subcommands that the doc states are *"non-interactive… for scripting and
automation… equivalent to the `/plugin marketplace` commands"* — usable headless:

```bash
# add a marketplace (GitHub owner/repo@ref, git URL#ref, remote marketplace.json URL, or local path)
claude plugin marketplace add <source> [--scope user|project|local] [--sparse <paths…>]
# install a plugin from a registered marketplace
claude plugin install <plugin>@<marketplace>
# UPDATE: re-pull marketplaces from source (new plugins + version changes)
claude plugin marketplace update [name]
claude plugin marketplace list --json          # introspect what's installed
claude plugin validate <dir>                   # validate a marketplace/plugin
```

Update/version semantics: a plugin's version resolves from `plugin.json` `version` → marketplace-entry
`version` → **git commit SHA**. For git sources with **no** `version`, *every new commit is a new
version*, so `claude plugin marketplace update` pulls the latest commit and the new version is picked
up. (Plugins are copied into the cache on install — `../` references outside the plugin dir don't
work; use `${CLAUDE_PLUGIN_ROOT}`.)

**How the Agent SDK loads plugins — important constraint.** The Python SDK's `plugins` option accepts
**only** `{"type": "local", "path": "<dir>"}` (verbatim from `types.py`: *"Currently only local
plugins are supported via the 'local' type"*; and the plugins doc: *"To use a plugin distributed
through a marketplace or remote repository, **download it first and provide the local directory
path**."*). The SDK does **not** install from a marketplace itself. Two valid runtime strategies:

- **(A) CLI install + local path (recommended for marketplaces/private sources + update support).**
  At job start, run `claude plugin marketplace add` + `claude plugin install` (with
  `CLAUDE_CONFIG_DIR`/`CLAUDE_CODE_PLUGIN_CACHE_DIR` pointed at a writable dir), then either rely on
  `setting_sources` to load them, or pass the resulting cache path
  (`$CLAUDE_CONFIG_DIR/plugins/cache/<marketplace>/<plugin>/<version>`) as a `local` plugin. The doc
  explicitly notes CLI-installed plugins live under `~/.claude/plugins/` and can be passed to the SDK
  by path. For **update**, re-run `claude plugin marketplace update` before install.
- **(B) Git-clone + local path.** `git clone` the plugin/marketplace repo into `/tmp` (or the writable
  dir) and pass `plugins=[{"type":"local","path":"/tmp/…"}]`. Update = `git pull` / re-clone at a ref.

**Auth for private plugin sources (verbatim, marketplace doc).** Background/headless install+update
*"runs at startup without credential helpers"* and uses **environment-variable tokens**:

| Provider  | Env var(s)                   | Notes |
|-----------|------------------------------|-------|
| GitHub    | `GITHUB_TOKEN` or `GH_TOKEN` | PAT or GitHub App token; **`repo` scope for private repos** |
| GitLab    | `GITLAB_TOKEN` or `GL_TOKEN` | needs ≥ `read_repository` |
| Bitbucket | `BITBUCKET_TOKEN`            | app password / repo access token |

So a private marketplace just needs the relevant `#`-secret token injected into the subprocess `env`.
Container helper env vars also exist: `CLAUDE_CODE_PLUGIN_SEED_DIR` (read-only build-time pre-populated
plugins; not our case since we can't bake them in), `CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE=1`
(keep stale cache if `git pull` fails — useful resilience), and `CLAUDE_CODE_PLUGIN_GIT_TIMEOUT_MS`
(default 120 000 ms; raise for large repos).

**Verdict on the KEY requirement:** ✅ fully feasible at runtime from a read-only image. Point
`CLAUDE_CONFIG_DIR` (and/or `CLAUDE_CODE_PLUGIN_CACHE_DIR`) at a writable dir (e.g. `/tmp/claude-home`),
inject the git provider token as a `#`-secret, run `claude plugin marketplace add/update` +
`claude plugin install` at job start, then load via `setting_sources`/local path. Update is a first-
class CLI operation.

---

## Q5 — GitHub working

Three documented ways the agent can work on GitHub; pick by need:

1. **Bash + `gh`/`git` + token env (standard, recommended).** Enable the built-in `Bash` tool and
   inject `GITHUB_TOKEN`/`GH_TOKEN` into the subprocess `env`; the agent clones, edits, commits,
   pushes, and opens PRs via `gh pr create`. `gh` reads the token automatically. Covers the full
   clone→edit→commit→push→PR flow in one tool.
2. **GitHub MCP server** (stdio `@modelcontextprotocol/server-github`, or hosted
   `https://api.githubcopilot.com/mcp/`) configured via `mcp_servers`, with the token in `env`/headers
   and `mcp__github__*` allow-listed. Better for structured GitHub API calls (issues/PRs as tools).
3. **Plain git over HTTPS** with a credential helper or token-in-URL (token-in-URL is an anti-pattern;
   not recommended).

**Credential the user must supply:** a **GitHub token** — a fine-grained PAT scoped to the target
repos with **Contents: read/write** and **Pull requests: read/write** (classic equivalent: `repo`
scope). Injected as a `#`-prefixed secret config field → subprocess `env` (`GITHUB_TOKEN`). The same
token doubles for cloning private *plugin* marketplaces (Q4). For headless container work, **(1) Bash +
`gh` + `GITHUB_TOKEN`** is the simplest and most capable default; expose (2) as an option for users who
want GitHub-as-MCP-tools.

> The reference component does **none** of this (no git tooling, no Bash/file tools, no workspace `cwd`).
> This is net-new for us — we must enable the `Bash`/file tools, set a `cwd` workspace, and inject the
> token.

---

## Q6 — Dynamic output tables → Keboola output mapping

Grounded in `keboola-context` (`output-mapping.md`, `default-bucket.md`, `native-data-types.md`):

- **Dynamic / runtime-decided tables.** Keboola does not require tables to be pre-declared in the
  config's output mapping — a component can **write CSVs into `/data/out/tables/` at runtime with a
  per-table `.manifest`** and the platform uploads them. *"Every file placed under
  `/data/out/tables/` is uploaded to Storage — not just entries explicitly listed in the output
  mapping."* So an agent that decides its own tables works by writing `<name>.csv` +
  `<name>.csv.manifest` (use the python-component `create_out_table_definition` + `write_manifest`).
- **Destination / bucket.** With **`default_bucket: true`** (Dev Portal), outputs route to
  `in.c-{componentId}-{configId}` and any manifest `destination` is **silently overridden** — so we
  either set `default_bucket` and let the platform name the bucket, or leave it off and set
  `destination` explicitly. Decide per design; do not hard-code a bucket name.
- **Primary key + incremental.** `incremental: true` + a primary key = **upsert**; `incremental:true`
  with no PK = append (unbounded growth — avoid); `incremental:false` = overwrite. Expose these per
  output (likely defaults the agent/config sets), and always set a PK when incremental.
- **Native types.** Set the Dev Portal **`dataTypeSupport`** to `authoritative` (CF default for new
  components, via `kbagent dev-portal patch`), emit a `schema` manifest, and **agree `has_header`
  with how the CSV is actually written** (header text loaded as data is the classic silent failure).
- **Scratch files.** Write scratch/work files in **`/tmp`**, never `/data/out/tables/` (or they become
  spurious tables). The agent's working `cwd` should be a `/tmp/...` workspace, distinct from
  `/data/out/`.
- **Debug JSONL output.** Mark its output-mapping entry **`write_always: true`** so it survives a
  failed job (Q2).

> The reference component writes **no tables/manifests at all** (only a Markdown log to artifacts and
> an optional Storage *file*). Dynamic-table output is net-new for us.

---

## Q7 — Anthropic API auth

**Model.** Direct Anthropic API via **`ANTHROPIC_API_KEY`** read from the subprocess `env` (the SDK/
CLI passes it through). Per `claude-api` skill, current model IDs are bare strings — **default
`claude-opus-4-8`** (Opus 4.8; $5/$25 per MTok; 1M context); cheaper option **`claude-sonnet-4-6`**
($3/$15). No date suffixes on aliases.

**Secure passing.** Store as a **`#`-prefixed encrypted config field** (e.g. `#anthropic_api_key`) →
decrypted at runtime by the platform → injected into `ClaudeAgentOptions(env={"ANTHROPIC_API_KEY": …})`.
(The reference component instead reads it from **stack-level image parameters** `#anthropic_api_key`,
so end users can't bring their own key. **Decision for us:** expose it as a config-level `#`-secret so
users supply their own key — more configurable and the brief calls for "secrets passed via
configuration." We can additionally honor an image-parameter fallback.)

**Bedrock / Vertex.** Supported by the SDK as alternatives: route via `ANTHROPIC_BASE_URL` to a proxy,
or use the provider regional endpoint (hosting doc: *"outbound HTTPS to `api.anthropic.com`, or to
your provider's regional endpoint when running on Bedrock or Vertex"*; Claude Code honors
`CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX`). **For v1, ship direct Anthropic API only**;
leave Bedrock/Vertex as a documented future option (the reference does direct-only too).

---

## Q8 — Sandbox / permission model

**`PermissionMode` values (verbatim):** `"default" | "acceptEdits" | "plan" | "bypassPermissions" |
"dontAsk" | "auto"`.

- `default` — unmatched tools hit the `can_use_tool` callback (interactive); not for headless.
- `acceptEdits` — auto-approves edits/safe FS ops.
- `plan` — explore only, edits prompt.
- `bypassPermissions` — auto-approve everything (hooks + deny rules still apply). Used by the reference
  component.
- `dontAsk` — **deny anything not explicitly in `allowed_tools`/allow rules, no prompts.** This is the
  safest headless mode: it never stalls waiting for a prompt and won't run un-allow-listed tools.
- `auto` (TS) — classifier-gated.

**Recommended for a headless Keboola run:** `permission_mode="dontAsk"` + an explicit `allowed_tools`
allow-list, rather than `bypassPermissions`. Expose to the user as (a) a permission-mode choice and
(b) allow/deny lists.

**Tool names.** Built-ins: `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `WebFetch`, `WebSearch`
(plus `Bash(git *)`-style scoped patterns; bare-name deny removes the tool from context entirely).
MCP: `mcp__<server>__<tool>` / `mcp__<server>__*`.

**Programmatic gating.** `can_use_tool: CanUseTool` callback and `hooks` (`PreToolUse`/`PostToolUse`
via `HookMatcher`) allow per-call allow/deny/transform — useful for guardrails (e.g. block
`Bash(rm *)`). There is also a `sandbox: SandboxSettings` option.

**Sandboxing reality.** The SDK provides **no OS-level filesystem/network sandbox by itself** — the
**Keboola container is the sandbox** (process isolation, the read-only image, egress). We expose
config knobs (permission mode + allow/deny + a constrained `cwd` in `/tmp`) and rely on the container
+ the allow-list for safety; never expose `bypassPermissions` as the default.

---

## Q9 — Cost / rate-limit controls

**SDK knobs (all confirmed in `types.py`):**

- **`max_turns`** — caps agent-loop round-trips (set explicitly; default unbounded). On hit, result
  subtype `error_max_turns`.
- **`max_budget_usd`** — a **hard USD cost cap** (result subtype `error_max_budget_usd`). This
  *exists* (correcting an earlier mis-read that claimed it didn't). Primary runaway-cost guard.
- **`model`** tier (Opus vs Sonnet vs Haiku) and **`effort`** (`low…max`) trade cost vs capability.
- **`thinking`** / `max_thinking_tokens` for thinking depth.

**Cost/usage reporting** on the final **`ResultMessage`**: `total_cost_usd`, `usage` (token dict),
`num_turns`, `duration_ms`, `model_usage`, `session_id`, `subtype`, `is_error`, `api_error_status`.
Note the hosting/cost docs caveat: `total_cost_usd` is a **client-side estimate** from a bundled price
table, not authoritative billing — fine for surfacing in logs, not for billing reconciliation.

**Rate limits.** Standard Anthropic API limits apply (per `claude-api` skill): 429 `rate_limit_error`
with a `retry-after` header; large parallel-subagent fanouts can hit limits (hosting "Known
limitation" — batch the work). Surface as job errors with a clear message.

**Bounding a runaway run:** set both `max_turns` **and** `max_budget_usd` from config (with sane
defaults and required caps), pick the model tier, and optionally a `PreToolUse` hook. There is no
top-level wall-clock timeout in the SDK — `max_turns` + `max_budget_usd` are the levers.

---

## What we're extending beyond `component-agent-runner` (reusable vs net-new)

**Reusable patterns:** the `query()` streaming loop wrapped in one `asyncio.run`; the
`AgentExecutionResult`-style dataclass capturing `success/result_text/total_cost_usd/duration_ms/
num_turns/session_id`; stdio MCP launched via `uvx`; standard exit-code handling (`UserException`→1,
else 2); GELF logger config.

**Net-new (this component must add):** generic MCP (stdio **and** HTTP/SSE, arbitrary servers with
per-server `#`-secrets); **input tables carrying prompts** (the reference has *no* input mapping —
prompt is a config string only); **GitHub working** (Bash/file tools + `cwd` workspace + token);
**dynamic output tables + manifests** (the reference writes no tables); **always-on JSONL session
transcript** to a `write_always` debug output (the reference only hand-builds a Markdown log and never
captures the SDK's JSONL); **runtime plugin install/update** (Q4 — entirely new); user-supplied
**Anthropic API key as a config `#`-secret**; **configurable model + `max_turns` + `max_budget_usd`**
(the reference hard-codes the model and sets no caps); use **`dontAsk`** instead of
`bypassPermissions`; **drop the obsolete Node/npm Dockerfile layer** (SDK 0.2.x bundles the CLI).

---

## Feasibility & provisioning verdict

**Overall: FEASIBLE. No blocker stops the build.** Everything the brief requires is supported by the
Agent SDK + Claude Code plugin/marketplace model and maps cleanly onto Keboola's container + output
mapping. Concretely:

- **Headless execution:** ✅ stdio subprocess, no TTY; Python-only image (no Node).
- **JSONL transcript:** ✅ on-disk under `CLAUDE_CONFIG_DIR/projects/...` + stream tee → `write_always`
  debug output.
- **Generic MCP:** ✅ stdio + HTTP/SSE via `mcp_servers`, secrets via `env`/`headers`.
- **Runtime plugin add/update (KEY):** ✅ writable `CLAUDE_CONFIG_DIR`/`CLAUDE_CODE_PLUGIN_CACHE_DIR`
  + `claude plugin marketplace add/update` + `claude plugin install`, then load by local path;
  private sources via `GITHUB_TOKEN`/`GH_TOKEN` etc.
- **GitHub working:** ✅ Bash + `gh` + `GITHUB_TOKEN` (or GitHub MCP).
- **Dynamic outputs:** ✅ runtime CSV + manifest into `/data/out/tables/`.
- **Auth, permissions, cost controls:** ✅ all available.

**Provisioning — what the user must supply / decisions surfaced to the lead** (the only items public
docs/the repo can't settle):

1. **Anthropic API key for the Phase 7 cf-dev smoke test.** A real, funded `ANTHROPIC_API_KEY` is
   **required** for any end-to-end run (the agent loop calls the live API; there is no offline mode).
   The reference component sourced it from a **stack-level image parameter** (`#anthropic_api_key`);
   for cf-dev we need a key to use. **Decision needed from the lead:** which Anthropic key / billing to
   use for the smoke test, and a small **`max_budget_usd` ceiling** for that run (e.g. a few USD) so the
   smoke test can't run away. (Local datadir/VCR tests in Phase 5 should mock/stub the SDK and not call
   the live API.)
2. **GitHub token (only if the smoke test exercises GitHub / private plugins).** If the Phase 7 run
   tests the GitHub-working or private-plugin path, the user must provide a **fine-grained GitHub PAT**
   (Contents r/w, Pull requests r/w; or `repo` scope). If the smoke test is a simpler prompt→output run,
   no GitHub token is needed. **Decision needed:** does the cf-dev smoke test include GitHub/plugins?
3. **Headless-auth vs admin-only.** Auth is **fully headless** — API keys and git tokens are plain
   env/secret values; **no platform-admin or vendor-UI app-registration step is required** to run the
   agent. The one vendor-identity (non-admin) action is flipping the Dev Portal `dataTypeSupport` to
   `authoritative` in Phase 6 (`kbagent dev-portal patch`), which the CF flow already covers.

**No open unknowns remain on the 9 core questions.** The single genuinely external dependency is
**an Anthropic API key with a cost ceiling for the cf-dev smoke test** (and optionally a GitHub PAT if
that path is smoke-tested) — both are provisioning calls for the lead, not feasibility blockers.
