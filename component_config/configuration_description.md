### Configuration

Provide your **Anthropic API key** and use **Test Connection** to validate it. Choose the **model**,
set the **Max Turns** and **Max Budget (USD)** limits that bound every run (there is no wall-clock
timeout, so keep both set), and pick a non-prompting **permission mode**.

This component has **two setup modes**:

- **Config-prompt mode** — leave the input mapping empty and enter one prompt under **Task**. One agent run.
- **Tasks-table mode** — map one input table named **`tasks`** (required columns `task_id` + `prompt`,
  optional per-row `system_prompt` / `model` / `max_turns` / `max_budget_usd` / `output_table`). Each row
  is one task. Set **Task ID Filter** to let several configs share one curated tasks table, each owning a
  row subset.

Optionally configure **allowed/disallowed tools**, **MCP servers** (stdio / HTTP / SSE) with their own
encrypted secrets, runtime **plugin** marketplaces (public or private, pinned or `latest`), **GitHub**
access (`#github_token` + **Enable GitHub**), and the runtime **SDK version**. See the documentation
for the full parameter reference and the agent-to-table output contract.
