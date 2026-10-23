# GitHub multi-repo scope + configSchema copy fixes

Date: 2026-07-07
Base branch: `initial-implementation`
Status: approved for implementation

## Problem

Two independent issues surfaced while reviewing the GitHub section of the
component's config UI:

1. **Copy/schema quality.** Several fields and sections are under-described or
   mis-rendered: a section-level `options.tooltip` renders on top of the first
   field instead of the section title (affects every grid/grid-strict section
   in the schema), the Repository field doesn't explain that it's required
   (not optional), the "MCP Servers" and "Plugins" sections have redundant
   inner-array titles, the Advanced section's sub-concerns are undocumented
   once the misplaced tooltip is accounted for, and SDK Version doesn't state
   the concrete pinned version anywhere except a tooltip.

2. **Single-repo limitation.** `operates_on` is a single `"org/repo"` string.
   The Advocate broker uses it to scope the injected GitHub token so a
   hijacked agent can't drive the token against arbitrary repos (HIGH-3, see
   `src/advocate/contract.py`). There's no way today to let an agent work
   across multiple repos, or across an entire org, without widening this
   scope in an unsafe way.

## Non-goals

- No change to the "leave Repository empty" behavior. It's already
  hard-required and fail-closed when `github_enabled=true`
  (`Configuration._github_enabled_needs_scoped_repo` raises `UserException`).
  This is a deliberate security property (HIGH-3) and stays as-is — the
  original idea that "empty = broad access" was a misconception, corrected
  during design.
- No backward-compatibility shim for the `operates_on` string→array change.
  This component has no external users yet; this is a clean breaking change.
- No repo-list cap in the new sync action — it paginates GitHub's
  `/user/repos` fully.

## Part 1 — Copy/schema fixes

All changes are confined to `component_config/configSchema.json` (no backend
code touched).

### 1.1 Section-tooltip placement bug

Root cause: `options.tooltip` is a **field**-label mechanism (info icon `ⓘ`
next to that field's label). It is not a valid "section header tooltip"
mechanism — the only section-header icon mechanism is `options.documentation`
(a docs-link icon, not free hover text). Every section in this schema
(`connection`, `model_budget`, `permissions`, `github`, `task_output`,
`mcp_section`, `plugins_section`, `advanced`) sets `options.tooltip` on the
section object itself, so the icon renders wherever the framework happens to
attach it — observed to land on top of the section's first field label
(`Enable GitHub`, `Default Incremental Load`, etc.) instead of the section
title.

Fix: for each of the 8 sections, remove the section-level `options.tooltip`
and add an always-visible info property inside the section, following the
existing `_setup_modes_note` pattern already used at the schema root:

```json
"_github_note": {
  "type": "string",
  "format": "info",
  "description": "<the orientation text that used to be in options.tooltip>",
  "propertyOrder": 0
}
```

This is strictly better UX than a hover tooltip for orientation text anyway —
it's visible without hovering.

### 1.2 Repository field docs

Rewrite `operates_on`'s `description` and `tooltip` (see Part 2 for the field
becoming an array) to state plainly:

- Required when GitHub is enabled — there is no "leave empty for broad
  access" option; an empty value is a hard config error.
- Accepts one or more `"org/repo"` entries, or `"org/*"` to grant the whole
  org (opt-in, broader — call out explicitly that this is a wider blast
  radius than listing individual repos).
- `org/*` is always manually typed — the repo picker (sync action) only ever
  lists concrete repos it can enumerate from the token, never a wildcard.

### 1.3 MCP Servers / Plugins double-naming

- `mcp_section.mcp_servers` (array): title `"MCP Servers"` → `"Servers"`
  (section title `"MCP Servers"` already says what it is).
- `plugins_section.plugins` (array): title `"Plugins (Advanced)"` →
  `"Marketplaces"` (drop "(Advanced)" — this is the only way to add plugins,
  not an advanced escape hatch; "Marketplaces" also better matches what the
  array actually holds — marketplace entries, each listing plugin names).

### 1.4 Advanced section

Add an info note (per 1.1's pattern) up front summarizing the section's four
sub-concerns: SDK settings passthrough (`settings_json`), on-disk settings
sources (`setting_sources`), the workspace-file toggle
(`workspace_input_files`), and the runtime SDK version
(`sdk_version` / `sdk_version_on_failure`).

### 1.5 SDK Version concrete version

`sdk_version.description` changes from:

> "pinned (default) uses the baked-in SDK; a concrete version or 'latest'
> installs at job start (needs HTTPS egress)."

to:

> "pinned (default) uses the baked-in SDK (currently 0.2.101); a concrete
> version or 'latest' installs at job start (needs HTTPS egress)."

The tooltip already has this detail; the fix is surfacing it in the always-
visible description too. Keep both in sync going forward — if the baked
version changes, update both.

## Part 2 — Multi-repo + org/* wildcard scope

### 2.1 `src/configuration.py`

- `operates_on: str | None = None` → `operates_on: list[str] = Field(default_factory=list)`.
- `_strip_operates_on` validator (currently strips whitespace from a single
  string) becomes a `mode="before"` validator that: accepts a list, strips
  each entry, drops empty entries.
- `_github_enabled_needs_scoped_repo` becomes: if `github_enabled`, require
  `len(operates_on) >= 1`; for each entry, require either exact `org/repo`
  shape (two non-empty segments, no whitespace) or exact `org/*` shape
  (non-empty org segment, literal `*` as the second segment). Reuse the
  existing per-entry validation logic, just looped.
- `log_safe_summary()` — no change needed (doesn't currently include
  `operates_on`).

### 2.2 `src/advocate/contract.py`

- `derive_contract(cfg, *, operates_on: str | None = None)` →
  `derive_contract(cfg, *, operates_on: list[str] | None = None)`.
- Building `destinations` and `scope["repos"]`: loop over each entry in
  `operates_on`.
  - Entry `"org/repo"` → destination `f"{GITHUB_API_HOST}/repos/org/repo"`
    (unchanged logic, just per-entry).
  - Entry `"org/*"` → destination `f"{GITHUB_API_HOST}/repos/org"` (the
    org segment only, no `/*` suffix — `github_broker._path_allowed`'s
    existing child-path matching already scopes any `/repos/org/<anything>`
    under this without further broker changes, and its segment-boundary
    check already prevents `org-evil` from matching `org`).
  - `scope["repos"]` keeps the raw patterns (`"org/repo"` or `"org/*"`) —
    the gate does its own glob matching (see 2.3).
- Capability grant condition (`cfg.github_enabled and operates_on`) becomes
  `cfg.github_enabled and len(operates_on) > 0` (equivalent for a list, just
  explicit).
- Update the module docstring's `operates_on` description to reflect the list
  shape and `org/*` support.

### 2.3 `src/advocate/gate.py`

- `check_action`'s scope check currently does
  `if scope_repo is not None and allowed_repos: if scope_repo not in allowed_repos: <deny>`.
  Change the membership test to glob matching, mirroring the existing
  `_branch_allowed` helper:

  ```python
  def _repo_allowed(repo: str, allowed_patterns: list[str]) -> bool:
      return any(fnmatch.fnmatch(repo, pat) for pat in allowed_patterns)
  ```

  A literal `"org/repo"` pattern still only matches itself via `fnmatch`
  (no wildcard characters present); `"org/*"` matches any repo under `org`.
  Reuses the `fnmatch` import already present in this module.

### 2.4 `src/component.py`

- `derive_contract(config, operates_on=config.operates_on)` — no call-site
  change needed beyond the type now being a list.
- `github_allowed_destinations` building:
  ```python
  if config.github_enabled and config.operates_on:
      github_allowed_destinations = [
          f"/repos/{entry[:-2]}" if entry.endswith("/*") else f"/repos/{entry}"
          for entry in config.operates_on
      ]
  else:
      github_allowed_destinations = []
  ```

### 2.5 New sync action — repo picker

`src/sync_actions.py`:

```python
def list_github_repos(github_token: str, http_client: HttpClient | None = None) -> list[SelectElement]:
    """Paginate GET /user/repos with the configured token; full results, no cap."""
    if not github_token:
        raise UserException("No #github_token provided; cannot load repositories.")

    client = http_client or HttpClient(GITHUB_API_BASE)  # imported from advocate.brokers.github_broker
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    repos: list[SelectElement] = []
    page = 1
    while True:
        resp = client.get_raw(
            "/user/repos",
            headers=headers,
            params={"per_page": 100, "page": page, "affiliation": "owner,collaborator,organization_member"},
            timeout=REQUEST_TIMEOUT_S,
        )
        if resp.status_code in (401, 403):
            raise UserException("GitHub rejected the token (authentication failed). Check #github_token.")
        if resp.status_code != 200:
            raise UserException(f"Could not list GitHub repositories (HTTP {resp.status_code}).")
        batch = resp.json()
        if not batch:
            break
        repos.extend(SelectElement(value=r["full_name"], label=r["full_name"]) for r in batch)
        if len(batch) < 100:
            break
        page += 1
    return repos
```

`src/component.py`:

```python
@sync_action("load_github_repos")
def load_github_repos(self):
    config = Configuration(**self.configuration.parameters)
    return list_github_repos(config.github_token)
```

Follows the same pattern as `test_connection` / `check_anthropic_connection`.
`config.github_token` already has a safe default (`""`) per the existing
`_private_plugins_need_token` validator's needs, so this sync action doesn't
crash when the token isn't set yet — it raises a clean `UserException`
instead, per the "safe defaults for sync actions" pattern.

### 2.6 Schema change for `operates_on`

```json
"operates_on": {
  "type": "array",
  "title": "Repositories",
  "description": "One or more \"org/repo\" entries, or \"org/*\" for an entire org. Required when GitHub is enabled.",
  "items": { "type": "string" },
  "format": "select",
  "options": {
    "dependencies": { "github_enabled": true },
    "grid_columns": 12,
    "tags": true,
    "async": {
      "label": "Load Repositories",
      "action": "load_github_repos",
      "autoload": true
    },
    "tooltip": "The Advocate broker scopes the GitHub token to exactly these repos. Pick from the list (repos your token can access) or type manually. \"org/*\" grants the entire org — broader blast radius than listing individual repos, use deliberately. Must be exactly \"org/repo\" or \"org/*\", no spaces."
  },
  "propertyOrder": 2
}
```

`writable_branches`' `propertyOrder` and dependency stay unchanged (still 4,
still depends on `github_enabled`).

## Testing plan

- `tests/unit/test_contract_gate.py`: existing single-`operates_on` tests
  updated to pass a one-item list; add cases for multi-repo lists and for
  `org/*` (destination scoping in `derive_contract`, glob matching in
  `gate.check_action`).
- `src/configuration.py` unit tests: empty list + `github_enabled=True` still
  raises; malformed entries (no slash, three segments, whitespace, `org/**`)
  still raise; valid multi-entry and `org/*` lists pass.
- New unit tests for `list_github_repos`: pagination across 2+ pages, empty
  result, 401/403 → `UserException`, non-200 → `UserException`.
- `component_config/configSchema.json`: manual Ctrl+D schema-sandbox check
  (per component-build-ui skill) to confirm the info-note tooltip fix
  actually renders under the section title, and the repo picker autoloads
  and offers freeform "org/*" entry via `tags: true`.
- Datadir/VCR tests that reference `operates_on` as a string fixture need
  updating to the list shape.

## Open items for the implementation plan

- `sync_actions.py` should import `GITHUB_API_BASE` from
  `advocate.brokers.github_broker` rather than defining a second GitHub API
  base URL constant (resolved during design review — `github_broker.py`
  already defines `GITHUB_API_BASE = "https://api.github.com"`).
- `writable_branches` does not get an org/* style broadening — out of scope
  here, no change requested.
