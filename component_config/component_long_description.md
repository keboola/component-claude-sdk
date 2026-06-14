A highly configurable runner for the **Claude Agent SDK** inside Keboola. It wraps the Python
`claude-agent-sdk` so you can run agentic Claude workloads over your project data: supply
prompt(s)/goal(s) from configuration and/or an input table, run the Claude agent loop headless in
the Keboola container, let the agent use tools (Bash, file, web, MCP servers, plugins, GitHub), and
land whatever it produces as Keboola output tables.

### What it does

- **Two input modes.** Run a single prompt from configuration (config-prompt mode), or map a `tasks`
  input table and run one agent task per row (tasks-table mode), with an optional `task_id_filter` to
  let several configs share one curated tasks table.
- **Dynamic, writer-like output.** The agent decides at runtime which tables to produce; the
  component promotes them to Keboola output mapping with `.manifest` files (native types,
  primary keys, incremental load), into the component's default bucket.
- **Always-on transcript.** Every run writes the full SDK session transcript — the `claude_sessions`
  and `claude_runs` tables (uploaded even on a failed job) plus full-fidelity JSONL file artifacts on
  success — so each run is fully auditable.
- **Runtime plugins & MCP servers.** Public and private plugin marketplaces and arbitrary MCP servers
  (stdio / HTTP / SSE) are installed and configured at job start, each pinned to a ref or tracking
  `latest`; nothing is baked into the image.
- **Cost & permission controls.** Every run is bounded by `max_turns` and a hard `max_budget_usd`
  ceiling; only non-prompting permission modes are offered so a headless job never hangs on an
  approval prompt.

### Authentication

You bring your own funded **Anthropic API key** (`#anthropic_key`, required); an optional
**GitHub token** (`#github_token`) enables GitHub work and private plugin marketplaces. MCP servers
carry their own per-server encrypted secrets. All authentication is headless — plain encrypted
configuration values, no admin or OAuth step.

See the [documentation](https://github.com/keboola/component-claude-sdk/blob/main/README.md) for the
full configuration surface, the input-table contract, and the agent→table hand-off convention.
