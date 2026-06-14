### Configuration

Provide your Anthropic API key and use **Test Connection** to validate it. Choose the model, set the
`max_turns` and `max_budget_usd` limits that bound every run, and pick a non-prompting permission mode.

In **config-prompt mode** enter a single prompt under **Task**. In **tasks-table mode** map a `tasks`
input table (one row per task) and optionally set `task_id_filter` to process only specific rows.

Optionally configure allowed/disallowed tools, MCP servers (stdio / HTTP / SSE) with their own
secrets, runtime plugin marketplaces, GitHub access (`#github_token` + **Enable GitHub**), and the
runtime SDK version. See the documentation for the full parameter reference.
