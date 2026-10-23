# GitHub Multi-Repo Scope + configSchema Copy Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix configSchema.json copy/tooltip issues in the GitHub/Output/MCP/Plugins/Advanced sections, and let the Advocate broker scope the GitHub token to multiple repos and/or a whole org (`org/*`, opt-in) instead of exactly one `org/repo`, with a sync action to pick repos the token can access.

**Architecture:** `operates_on` moves from a single `str` to a `list[str]` on `Configuration`, threading through `derive_contract` (destination scoping), `gate.check_action` (glob-based scope check), and `component.py`'s `github_allowed_destinations` wiring. A new `load_github_repos` sync action calls GitHub's `/user/repos` (paginated) to populate a multi-select repo picker. All configSchema.json copy fixes are additive/renaming only, no backend change.

**Tech Stack:** Python 3.14, Pydantic v2, `keboola.component` (sync actions, `HttpClient`), `fnmatch` (stdlib), pytest.

**Branch:** `feat/github-multi-repo-ui-fixes` (based on `initial-implementation`, already checked out).

## Global Constraints

- Clean breaking change: `operates_on` becomes `list[str]` everywhere, no back-compat shim for the old single-string shape (spec: no external users yet).
- `operates_on` stays hard-required (non-empty) when `github_enabled=true` — HIGH-3 fail-closed behavior is preserved, not relaxed. "Leave empty for broad access" is not a supported behavior; docs must say so explicitly.
- `org/*` is always manually typed by the user — the `load_github_repos` sync action never returns a wildcard entry, only concrete `org/repo` values.
- The sync action paginates GitHub's `/user/repos` fully — no result cap.
- Section-level `options.tooltip` must never be used again for orientation text — use an in-section `format: "info"` note property instead (see Task 1).
- Reuse `GITHUB_API_BASE` from `advocate.brokers.github_broker` in `sync_actions.py` — do not define a second GitHub API base URL constant.

---

### Task 1: configSchema.json copy/tooltip fixes

**Files:**
- Modify: `component_config/configSchema.json`

**Interfaces:**
- Consumes: nothing (schema-only, no Python).
- Produces: nothing consumed by later tasks — this task is independent of Tasks 2-7 and can be done in either order.

This task is schema-only. There is no existing automated test that renders the UI, so verification is: (a) the file stays valid JSON, (b) a component-build-ui schema-sandbox check (Ctrl+D) confirms the info notes render under their section titles instead of on the first field.

- [ ] **Step 1: Read the current schema**

Run: `cat component_config/configSchema.json | python3 -m json.tool > /dev/null && echo VALID`
Expected: `VALID` (confirms it's valid JSON before editing)

- [ ] **Step 2: Fix the `connection` section's tooltip**

Find:
```json
    "connection": {
      "type": "object",
      "title": "Connection & Authentication",
      "format": "grid-strict",
      "required": ["#anthropic_key"],
      "options": {
        "tooltip": "Credentials the agent run needs. The Anthropic key is required for every run; the GitHub token is only needed when you turn on GitHub or use a private plugin source. Both are encrypted at rest and never logged."
      },
      "propertyOrder": 10,
      "properties": {
        "#anthropic_key": {
```

Replace with:
```json
    "connection": {
      "type": "object",
      "title": "Connection & Authentication",
      "format": "grid-strict",
      "required": ["#anthropic_key"],
      "propertyOrder": 10,
      "properties": {
        "_connection_note": {
          "type": "string",
          "format": "info",
          "description": "Credentials the agent run needs. The Anthropic key is required for every run; the GitHub token is only needed when you turn on GitHub or use a private plugin source. Both are encrypted at rest and never logged.",
          "options": {
            "grid_columns": 12
          },
          "propertyOrder": 0
        },
        "#anthropic_key": {
```

- [ ] **Step 3: Fix the `model_budget` section's tooltip**

Find:
```json
    "model_budget": {
      "type": "object",
      "title": "Model & Budget",
      "format": "grid-strict",
      "options": {
        "tooltip": "Capability vs. cost, and the limits that hard-bound every run. There is NO wall-clock timeout, so Max Turns and Max Budget are the only stop conditions — always keep both set."
      },
      "propertyOrder": 20,
      "properties": {
        "model": {
```

Replace with:
```json
    "model_budget": {
      "type": "object",
      "title": "Model & Budget",
      "format": "grid-strict",
      "propertyOrder": 20,
      "properties": {
        "_model_budget_note": {
          "type": "string",
          "format": "info",
          "description": "Capability vs. cost, and the limits that hard-bound every run. There is NO wall-clock timeout, so Max Turns and Max Budget are the only stop conditions — always keep both set.",
          "options": {
            "grid_columns": 12
          },
          "propertyOrder": 0
        },
        "model": {
```

- [ ] **Step 4: Fix the `permissions` section's tooltip**

Find:
```json
    "permissions": {
      "type": "object",
      "title": "Permissions & Tools",
      "format": "grid-strict",
      "options": {
        "tooltip": "What the agent is allowed to do. Only non-prompting permission modes are offered — a mode that pauses for human approval would hang a headless Keboola job."
      },
      "propertyOrder": 30,
      "properties": {
        "permission_mode": {
```

Replace with:
```json
    "permissions": {
      "type": "object",
      "title": "Permissions & Tools",
      "format": "grid-strict",
      "propertyOrder": 30,
      "properties": {
        "_permissions_note": {
          "type": "string",
          "format": "info",
          "description": "What the agent is allowed to do. Only non-prompting permission modes are offered — a mode that pauses for human approval would hang a headless Keboola job.",
          "options": {
            "grid_columns": 12
          },
          "propertyOrder": 0
        },
        "permission_mode": {
```

- [ ] **Step 5: Fix the `github` section — tooltip, note, and Repository field docs**

Find the entire `github` object:
```json
    "github": {
      "type": "object",
      "title": "GitHub",
      "format": "grid-strict",
      "options": {
        "tooltip": "Let the agent work against GitHub with gh and git. When enabled, the token below is exported as GITHUB_TOKEN / GH_TOKEN and Bash(gh *) + Bash(git *) are added to Allowed Tools."
      },
      "propertyOrder": 40,
      "properties": {
        "github_enabled": {
          "type": "boolean",
          "title": "Enable GitHub",
          "description": "Let the agent run gh and git commands against GitHub.",
          "default": false,
          "options": {
            "grid_columns": 12,
            "tooltip": "When on, Bash(gh *) and Bash(git *) are added to Allowed Tools and the GitHub token is exported as GITHUB_TOKEN / GH_TOKEN so gh and git authenticate automatically. Reveals the GitHub Token field. Leave off if the agent does not need GitHub."
          },
          "propertyOrder": 1
        },
        "operates_on": {
          "type": "string",
          "title": "Repository (org/repo)",
          "description": "The single GitHub repository the agent operates on, in \"org/repo\" form. Required when GitHub is enabled.",
          "options": {
            "dependencies": {
              "github_enabled": true
            },
            "grid_columns": 12,
            "tooltip": "e.g. \"acme/widgets\". The Advocate broker scopes the GitHub token to exactly this repository, so a hijacked agent cannot drive the token against other repos. Must be exactly \"org/repo\" with no spaces."
          },
          "propertyOrder": 2
        },
        "#github_token": {
```

Replace with:
```json
    "github": {
      "type": "object",
      "title": "GitHub",
      "format": "grid-strict",
      "propertyOrder": 40,
      "properties": {
        "_github_note": {
          "type": "string",
          "format": "info",
          "description": "Let the agent work against GitHub with gh and git. When enabled, the token below is exported as GITHUB_TOKEN / GH_TOKEN and Bash(gh *) + Bash(git *) are added to Allowed Tools.",
          "options": {
            "grid_columns": 12
          },
          "propertyOrder": 0
        },
        "github_enabled": {
          "type": "boolean",
          "title": "Enable GitHub",
          "description": "Let the agent run gh and git commands against GitHub.",
          "default": false,
          "options": {
            "grid_columns": 12,
            "tooltip": "When on, Bash(gh *) and Bash(git *) are added to Allowed Tools and the GitHub token is exported as GITHUB_TOKEN / GH_TOKEN so gh and git authenticate automatically. Reveals the GitHub Token field. Leave off if the agent does not need GitHub."
          },
          "propertyOrder": 1
        },
        "operates_on": {
          "type": "array",
          "title": "Repositories",
          "description": "One or more \"org/repo\" entries, or \"org/*\" for an entire org. Required when GitHub is enabled — there is no \"leave empty for broad access\" option.",
          "items": {
            "type": "string"
          },
          "format": "select",
          "options": {
            "dependencies": {
              "github_enabled": true
            },
            "grid_columns": 12,
            "tags": true,
            "async": {
              "label": "Load Repositories",
              "action": "load_github_repos",
              "autoload": true
            },
            "tooltip": "The Advocate broker scopes the GitHub token to exactly these repos. Pick from the list (repos your token can access) or type manually. \"org/*\" grants the entire org to the agent — broader blast radius than listing individual repos, use deliberately. Each entry must be exactly \"org/repo\" or \"org/*\", no spaces."
          },
          "propertyOrder": 2
        },
        "#github_token": {
```

- [ ] **Step 6: Fix the `task_output` section's tooltip and Output Settings tooltip**

Find:
```json
    "task_output": {
      "type": "object",
      "title": "Task, Prompt & Output",
      "format": "grid",
      "options": {
        "tooltip": "The System Prompt shapes every task. Config-prompt mode: write the single prompt here. Tasks-table mode: use the Task ID Filter to pick which rows this config runs. Output settings apply to the tables the agent produces."
      },
      "propertyOrder": 50,
      "properties": {
        "system_prompt": {
```

Replace with:
```json
    "task_output": {
      "type": "object",
      "title": "Task, Prompt & Output",
      "format": "grid",
      "propertyOrder": 50,
      "properties": {
        "_task_output_note": {
          "type": "string",
          "format": "info",
          "description": "The System Prompt shapes every task. Config-prompt mode: write the single prompt here. Tasks-table mode: use the Task ID Filter to pick which rows this config runs. Output settings apply to the tables the agent produces.",
          "propertyOrder": -1
        },
        "system_prompt": {
```

Then find the nested `output` section's tooltip:
```json
        "output": {
          "type": "object",
          "title": "Output Settings",
          "description": "Defaults applied to the tables the agent produces.",
          "format": "grid-strict",
          "options": {
            "tooltip": "The agent decides at runtime which tables to create (writer-like). The component promotes each one to the component's default bucket with a manifest. A per-table sidecar (incremental / primary_key) overrides the default below for that table."
          },
          "propertyOrder": 3,
          "properties": {
            "default_incremental": {
```

Replace with:
```json
        "output": {
          "type": "object",
          "title": "Output Settings",
          "description": "Defaults applied to the tables the agent produces.",
          "format": "grid-strict",
          "propertyOrder": 3,
          "properties": {
            "_output_note": {
              "type": "string",
              "format": "info",
              "description": "The agent decides at runtime which tables to create (writer-like). The component promotes each one to the component's default bucket with a manifest. A per-table sidecar (incremental / primary_key) overrides the default below for that table.",
              "options": {
                "grid_columns": 12
              },
              "propertyOrder": 0
            },
            "default_incremental": {
```

- [ ] **Step 7: Fix the `mcp_section` section — tooltip and redundant array title**

Find:
```json
    "mcp_section": {
      "type": "object",
      "title": "MCP Servers",
      "format": "grid",
      "options": {
        "tooltip": "Extra tools from Model Context Protocol servers. Defining a server here only makes its tools VISIBLE — you must also allow-list them in Permissions & Tools above as mcp__<server name>__* (or a specific tool)."
      },
      "propertyOrder": 60,
      "properties": {
        "mcp_servers": {
          "type": "array",
          "title": "MCP Servers",
          "description": "Model Context Protocol servers to make available to the agent.",
```

Replace with:
```json
    "mcp_section": {
      "type": "object",
      "title": "MCP Servers",
      "format": "grid",
      "propertyOrder": 60,
      "properties": {
        "_mcp_section_note": {
          "type": "string",
          "format": "info",
          "description": "Extra tools from Model Context Protocol servers. Defining a server here only makes its tools VISIBLE — you must also allow-list them in Permissions & Tools above as mcp__<server name>__* (or a specific tool).",
          "propertyOrder": 0
        },
        "mcp_servers": {
          "type": "array",
          "title": "Servers",
          "description": "Model Context Protocol servers to make available to the agent.",
```

- [ ] **Step 8: Fix the `plugins_section` section — tooltip and redundant/misleading array title**

Find:
```json
    "plugins_section": {
      "type": "object",
      "title": "Plugins",
      "format": "grid",
      "options": {
        "tooltip": "Claude Code plugin marketplaces installed at job start (not baked into the image). Public sources resolve via a built-in registry; private sources need the GitHub Token in the GitHub section."
      },
      "propertyOrder": 70,
      "properties": {
        "plugins": {
          "type": "array",
          "title": "Plugins (Advanced)",
          "description": "Plugin marketplaces installed at job start; each entry is pinned or tracks latest.",
```

Replace with:
```json
    "plugins_section": {
      "type": "object",
      "title": "Plugins",
      "format": "grid",
      "propertyOrder": 70,
      "properties": {
        "_plugins_section_note": {
          "type": "string",
          "format": "info",
          "description": "Claude Code plugin marketplaces installed at job start (not baked into the image). Public sources resolve via a built-in registry; private sources need the GitHub Token in the GitHub section.",
          "propertyOrder": 0
        },
        "plugins": {
          "type": "array",
          "title": "Marketplaces",
          "description": "Plugin marketplaces installed at job start; each entry is pinned or tracks latest.",
```

- [ ] **Step 9: Fix the `advanced` section — tooltip, summary note, and SDK Version description**

Find:
```json
    "advanced": {
      "type": "object",
      "title": "Advanced",
      "format": "grid",
      "options": {
        "tooltip": "SDK settings passthrough, on-disk settings sources, the workspace file toggle, and the runtime SDK version. Most runs leave these at their defaults."
      },
      "propertyOrder": 80,
      "properties": {
        "settings_json": {
```

Replace with:
```json
    "advanced": {
      "type": "object",
      "title": "Advanced",
      "format": "grid",
      "propertyOrder": 80,
      "properties": {
        "_advanced_note": {
          "type": "string",
          "format": "info",
          "description": "Four independent settings: SDK settings passthrough (an escape hatch for options not exposed elsewhere), which on-disk settings files the agent may load, whether input-mapping files are staged into the agent's workspace, and which SDK version the run uses. Most runs leave all four at their defaults.",
          "propertyOrder": 0
        },
        "settings_json": {
```

Then find the SDK Version description:
```json
        "sdk_version": {
          "type": "string",
          "title": "SDK Version",
          "description": "pinned (default) uses the baked-in SDK; a concrete version or 'latest' installs at job start (needs HTTPS egress).",
```

Replace with:
```json
        "sdk_version": {
          "type": "string",
          "title": "SDK Version",
          "description": "pinned (default) uses the baked-in SDK, currently 0.2.101; a concrete version or 'latest' installs at job start (needs HTTPS egress).",
```

- [ ] **Step 10: Validate the JSON and check for leftover section-level tooltips**

Run:
```bash
python3 -m json.tool component_config/configSchema.json > /dev/null && echo VALID
grep -c '"tooltip"' component_config/configSchema.json
python3 -c "
import json
schema = json.load(open('component_config/configSchema.json'))
sections = ['connection', 'model_budget', 'permissions', 'github', 'task_output', 'mcp_section', 'plugins_section', 'advanced']
for name in sections:
    opts = schema['properties'][name].get('options', {})
    assert 'tooltip' not in opts, f'{name} still has a section-level tooltip'
print('OK: no section-level tooltips remain')
"
```
Expected: `VALID`, some non-zero tooltip count (field-level tooltips remain, that's correct), then `OK: no section-level tooltips remain`

- [ ] **Step 11: Commit**

```bash
git add component_config/configSchema.json
git commit -m "fix(schema): move section orientation text off broken section tooltips into info notes"
```

---

### Task 2: `Configuration.operates_on` becomes `list[str]`

**Files:**
- Modify: `src/configuration.py:241-245` (the `operates_on` field), `src/configuration.py:306-309` (the `_strip_operates_on` validator), `src/configuration.py:332-355` (the `_github_enabled_needs_scoped_repo` validator)
- Modify: `tests/unit/test_configuration.py`

**Interfaces:**
- Consumes: nothing (this is the innermost layer).
- Produces: `Configuration.operates_on: list[str]` — Task 3 (`contract.py`), Task 5 (`component.py`), and the sync action in Task 6 all read this attribute and expect a list.

- [ ] **Step 1: Write the failing tests in `tests/unit/test_configuration.py`**

Replace the existing `operates_on`-related tests (currently `test_github_enabled_requires_operates_on` through `test_operates_on_optional_when_github_disabled`, lines 50-88) with:

```python
def test_github_enabled_requires_operates_on():
    """HIGH-3: github_enabled without operates_on fails closed at config parse."""
    with pytest.raises(UserException) as exc:
        Configuration(**_base(github_enabled=True))
    assert "operates_on" in str(exc.value)


def test_github_enabled_rejects_empty_operates_on_list():
    """An explicit empty list is treated the same as absent — still fails closed."""
    with pytest.raises(UserException) as exc:
        Configuration(**_base(github_enabled=True, operates_on=[]))
    assert "operates_on" in str(exc.value)


def test_github_enabled_rejects_malformed_operates_on_entry():
    """Each entry must be 'org/repo' or 'org/*' — a bare name (no slash) is rejected."""
    with pytest.raises(UserException) as exc:
        Configuration(**_base(github_enabled=True, operates_on=["just-a-name"]))
    assert "org/repo" in str(exc.value)


def test_github_enabled_with_valid_single_repo_parses():
    cfg = Configuration(**_base(github_enabled=True, operates_on=["org/repo-X"]))
    assert cfg.github_enabled is True
    assert cfg.operates_on == ["org/repo-X"]
    # Default writable-branch scope confines the agent to its own branches.
    assert cfg.writable_branches == ["agent/*"]


def test_github_enabled_with_multiple_repos_parses():
    cfg = Configuration(**_base(github_enabled=True, operates_on=["org/repo-X", "org/repo-Y"]))
    assert cfg.operates_on == ["org/repo-X", "org/repo-Y"]


def test_github_enabled_with_org_wildcard_parses():
    cfg = Configuration(**_base(github_enabled=True, operates_on=["org/*"]))
    assert cfg.operates_on == ["org/*"]


def test_github_enabled_rejects_double_star_wildcard():
    """'org/**' is not a supported pattern — only a literal 'org/*' wildcard."""
    with pytest.raises(UserException) as exc:
        Configuration(**_base(github_enabled=True, operates_on=["org/**"]))
    assert "org/repo" in str(exc.value)


def test_operates_on_surrounding_whitespace_is_stripped_per_entry():
    cfg = Configuration(**_base(github_enabled=True, operates_on=["  org/repo-X  ", "org/repo-Y"]))
    assert cfg.operates_on == ["org/repo-X", "org/repo-Y"]


def test_operates_on_internal_whitespace_rejected():
    """A dirty value like 'a / b' must be rejected, not silently stored."""
    with pytest.raises(UserException) as exc:
        Configuration(**_base(github_enabled=True, operates_on=["a / b"]))
    assert "org/repo" in str(exc.value)


def test_operates_on_empty_string_entry_dropped_before_validation():
    """A blank entry (e.g. from a UI artifact) is dropped, not treated as a repo."""
    cfg = Configuration(**_base(github_enabled=True, operates_on=["org/repo-X", "  "]))
    assert cfg.operates_on == ["org/repo-X"]


def test_operates_on_optional_when_github_disabled():
    """operates_on stays optional (empty) when GitHub is off."""
    cfg = Configuration(**_base())
    assert cfg.github_enabled is False
    assert cfg.operates_on == []
```

Also update the two other call sites in this file that pass the old string shape:

Find (around line 269):
```python
        github={"github_enabled": True, "#github_token": "GH_NAME_ONLY", "operates_on": "org/repo-X"},
```
Replace with:
```python
        github={"github_enabled": True, "#github_token": "GH_NAME_ONLY", "operates_on": ["org/repo-X"]},
```

Find (around line 329):
```python
    cfg = Configuration(**_base(max_turns=3, github_enabled=True, operates_on="org/repo-X"))
```
Replace with:
```python
    cfg = Configuration(**_base(max_turns=3, github_enabled=True, operates_on=["org/repo-X"]))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_configuration.py -v -k operates_on`
Expected: FAIL — `operates_on` is still typed `str | None` and the validators still expect a single string, so list inputs raise a Pydantic `ValidationError` wrapped in `UserException` with an unexpected message, or the new tests' assertions on list equality fail against a string.

- [ ] **Step 3: Change the `operates_on` field declaration**

In `src/configuration.py`, find:
```python
    # --- intent contract scope (spec §10) ---
    operates_on: str | None = None
    """``org/repo`` target the agent operates on; used by the Advocate to scope the
    GitHub token to a single repository. REQUIRED when ``github_enabled`` is true —
    without it a hijacked agent could drive the real token against any repo the token
    can reach, so the broker fails closed (HIGH-3). See spec §10."""
```

Replace with:
```python
    # --- intent contract scope (spec §10) ---
    operates_on: list[str] = Field(default_factory=list)
    """One or more ``org/repo`` entries, or ``org/*`` for an entire org, that the
    agent operates on; used by the Advocate to scope the GitHub token. REQUIRED
    (non-empty) when ``github_enabled`` is true — without it a hijacked agent
    could drive the real token against any repo the token can reach, so the
    broker fails closed (HIGH-3). ``org/*`` is a deliberate, broader opt-in:
    it scopes the token to every repo under that org rather than one repo.
    See spec §10."""
```

- [ ] **Step 4: Change the `_strip_operates_on` validator to operate per-entry on a list**

Find:
```python
    @field_validator("operates_on", mode="before")
    @classmethod
    def _strip_operates_on(cls, v):
        """Trim surrounding whitespace so the stored repo scope is clean (HIGH-3)."""
        return v.strip() if isinstance(v, str) else v
```

Replace with:
```python
    @field_validator("operates_on", mode="before")
    @classmethod
    def _strip_operates_on(cls, v):
        """Trim whitespace and drop blank entries so the stored repo scope is clean (HIGH-3)."""
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list):
            return v
        return [entry.strip() for entry in v if isinstance(entry, str) and entry.strip()]
```

- [ ] **Step 5: Change `_github_enabled_needs_scoped_repo` to validate each list entry**

Find:
```python
    @model_validator(mode="after")
    def _github_enabled_needs_scoped_repo(self) -> Configuration:
        """``github_enabled`` requires a concrete ``operates_on`` repo (HIGH-3).

        Without it the GitHub token cannot be bound to a single repository, so a
        hijacked agent could drive the real token against any repo the token can
        reach. Fail closed at config time with a clear message rather than grant
        broad access.
        """
        if self.github_enabled:
            repo = self.operates_on or ""
            if not repo:
                raise UserException(
                    "github_enabled requires 'operates_on' (\"org/repo\") so the GitHub token is "
                    "scoped to a single repository — a hijacked agent must not be able to use the "
                    "token against arbitrary repos."
                )
            parts = repo.split("/")
            # Exactly two non-empty segments and no embedded whitespace — a dirty
            # value would otherwise flow into the scope/allowlist and silently
            # mismatch the real repo path.
            if len(parts) != 2 or not all(parts) or any(c.isspace() for c in repo):
                raise UserException(f"operates_on must be in 'org/repo' form (no spaces), got: {self.operates_on!r}")
        return self
```

Replace with:
```python
    @model_validator(mode="after")
    def _github_enabled_needs_scoped_repo(self) -> Configuration:
        """``github_enabled`` requires at least one ``operates_on`` entry (HIGH-3).

        Without it the GitHub token cannot be bound to any repository, so a
        hijacked agent could drive the real token against any repo the token can
        reach. Fail closed at config time with a clear message rather than grant
        broad access. Each entry must be exactly ``org/repo`` or the literal
        wildcard ``org/*`` — a dirty value would otherwise flow into the
        scope/allowlist and silently mismatch the real repo path.
        """
        if self.github_enabled:
            if not self.operates_on:
                raise UserException(
                    "github_enabled requires 'operates_on' (one or more \"org/repo\" entries, or "
                    "\"org/*\" for an entire org) so the GitHub token is scoped — a hijacked agent "
                    "must not be able to use the token against arbitrary repos."
                )
            for entry in self.operates_on:
                parts = entry.split("/")
                is_wildcard = len(parts) == 2 and parts[0] and parts[1] == "*"
                is_exact_repo = len(parts) == 2 and all(parts) and parts[1] != "*"
                if any(c.isspace() for c in entry) or not (is_wildcard or is_exact_repo):
                    raise UserException(
                        f"operates_on entries must be 'org/repo' or 'org/*' (no spaces), got: {entry!r}"
                    )
        return self
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/unit/test_configuration.py -v`
Expected: PASS (all tests in the file, including the unrelated ones that don't touch `operates_on` — this confirms the change didn't regress anything else in this file)

- [ ] **Step 7: Commit**

```bash
git add src/configuration.py tests/unit/test_configuration.py
git commit -m "feat(config): operates_on becomes list[str], supports org/* wildcard entries"
```

---

### Task 3: `derive_contract` scopes destinations for multiple repos and `org/*`

**Files:**
- Modify: `src/advocate/contract.py` (module docstring lines 25-30, `derive_contract` signature and body lines 98-165)
- Modify: `tests/unit/test_contract_gate.py` (mechanical `operates_on="X"` → `operates_on=["X"]` across the file, plus new tests)
- Modify: `tests/unit/test_phase5_boot.py:259-266`
- Modify: `tests/unit/test_phase6_jsonl_chaining.py` (helper defaults + call sites)

**Interfaces:**
- Consumes: `Configuration.operates_on: list[str]` (Task 2).
- Produces: `derive_contract(cfg, *, operates_on: list[str] | None = None) -> dict` — same return shape as before (`contract["scope"]["repos"]` is still a `list[str]`, now possibly containing `org/*` patterns). Task 4 (`gate.py`) and Task 5 (`component.py`) both call this with the new list signature.

- [ ] **Step 1: Mechanical rename of existing call sites — run these exact commands**

These three files call `derive_contract(cfg, operates_on="org/repo-X")` (a literal string) at many call sites; wrap the literal in a list everywhere it appears verbatim. This is a pure syntactic rename — no other change:

```bash
sed -i '' 's/operates_on="org\/repo-X"/operates_on=["org\/repo-X"]/g' tests/unit/test_contract_gate.py
sed -i '' 's/operates_on="org\/repo-X"/operates_on=["org\/repo-X"]/g' tests/unit/test_phase6_jsonl_chaining.py
```

Verify no literal-string call sites remain (the helper-default and local-variable spots handled in later steps use a different pattern and won't match this grep):

```bash
grep -n 'operates_on="org/repo-X"' tests/unit/test_contract_gate.py tests/unit/test_phase6_jsonl_chaining.py
```
Expected: no output (all replaced)

- [ ] **Step 2: Fix the remaining non-literal spots in `tests/unit/test_contract_gate.py`**

Find (around line 301):
```python
    def _gh_contract(self, *, operates_on: str = "org/repo-X") -> dict:
```
Replace with:
```python
    def _gh_contract(self, *, operates_on: list[str] | None = None) -> dict:
```

Then find the body of that same method:
```python
        cfg = _make_cfg(github_enabled=True)
        return derive_contract(cfg, operates_on=operates_on)
```
Replace with:
```python
        cfg = _make_cfg(github_enabled=True)
        return derive_contract(cfg, operates_on=operates_on if operates_on is not None else ["org/repo-X"])
```

- [ ] **Step 3: Fix the remaining non-literal spots in `tests/unit/test_phase6_jsonl_chaining.py`**

Find:
```python
def _make_downstream_contract(*, operates_on: str = "org/repo-X") -> tuple[dict, dict, bytes]:
```
Replace with:
```python
def _make_downstream_contract(*, operates_on: list[str] | None = None) -> tuple[dict, dict, bytes]:
```

In the same function body, find:
```python
    cfg = _Cfg(github_enabled=True)
    secret = new_invocation_secret()
    contract = derive_contract(cfg, operates_on=operates_on)
```
Replace with:
```python
    cfg = _Cfg(github_enabled=True)
    secret = new_invocation_secret()
    contract = derive_contract(cfg, operates_on=operates_on if operates_on is not None else ["org/repo-X"])
```

Find:
```python
def _build_contaminated_upstream_jsonl(*, upstream_secret: bytes, operates_on: str = "org/repo-X") -> list[dict]:
```
Replace with:
```python
def _build_contaminated_upstream_jsonl(*, upstream_secret: bytes, operates_on: list[str] | None = None) -> list[dict]:
```

In the same function body, find:
```python
    cfg_elevated = _Cfg(github_enabled=True)
    upstream_contract = derive_contract(cfg_elevated, operates_on=operates_on)
```
Replace with:
```python
    cfg_elevated = _Cfg(github_enabled=True)
    upstream_contract = derive_contract(cfg_elevated, operates_on=operates_on if operates_on is not None else ["org/repo-X"])
```

Find the local variable (around line 318):
```python
        cfg = _Cfg(github_enabled=True)
        operates_on = "org/repo-X"
```
Replace with:
```python
        cfg = _Cfg(github_enabled=True)
        operates_on = ["org/repo-X"]
```

Find the `_FakeCfg` fixture (around line 217):
```python
        class _FakeCfg:
            anthropic_key = "sk-ant-REAL-KEY-9999"
            github_token = "ghp-REAL-TOKEN"
            github_enabled = False
            mcp_servers = []
            operates_on = None
```
Replace with:
```python
        class _FakeCfg:
            anthropic_key = "sk-ant-REAL-KEY-9999"
            github_token = "ghp-REAL-TOKEN"
            github_enabled = False
            mcp_servers = []
            operates_on = []
```

- [ ] **Step 4: Fix `tests/unit/test_phase5_boot.py`**

Find (around line 259-266):
```python
    def test_operates_on_wires_into_contract(self):
        """operates_on from config narrows the contract repo scope."""
        from advocate.contract import derive_contract

        cfg = Configuration(**{"#anthropic_key": "key", "github_enabled": True, "operates_on": "org/repo-X"})
        contract = derive_contract(cfg, operates_on=cfg.operates_on)
        assert "org/repo-X" in contract["scope"]["repos"]
        assert any("org/repo-X" in d for d in contract["destinations"])
```
Replace with:
```python
    def test_operates_on_wires_into_contract(self):
        """operates_on from config narrows the contract repo scope."""
        from advocate.contract import derive_contract

        cfg = Configuration(**{"#anthropic_key": "key", "github_enabled": True, "operates_on": ["org/repo-X"]})
        contract = derive_contract(cfg, operates_on=cfg.operates_on)
        assert "org/repo-X" in contract["scope"]["repos"]
        assert any("org/repo-X" in d for d in contract["destinations"])
```

Find (around line 613):
```python
                "operates_on": "org/repo-X",
```
Replace with:
```python
                "operates_on": ["org/repo-X"],
```

- [ ] **Step 5: Run the full existing suite to confirm the mechanical rename didn't break anything (still red on the new-behavior assertions, since `derive_contract` itself hasn't changed yet)**

Run: `pytest tests/unit/test_contract_gate.py tests/unit/test_phase5_boot.py tests/unit/test_phase6_jsonl_chaining.py -v`
Expected: PASS for all of these — `derive_contract`'s current implementation already does `repos: list[str] = [operates_on] if operates_on else []` and `f"{GITHUB_API_HOST}/repos/{operates_on}"`; since every call site now passes a **list** where the function still expects a `str | None`, string formatting a list produces something like `repos/['org/repo-X']` — this WILL actually fail. This step should show failures in `test_operates_on_scopes_github_destination`, `test_repos_scope_set_when_operates_on_given`, and similar destination/scope assertions. That confirms Step 6 is necessary.

- [ ] **Step 6: Add the new multi-repo/wildcard tests to `tests/unit/test_contract_gate.py`**

Add these to `TestDeriveContract` (after `test_repos_scope_empty_without_operates_on`):

```python
    def test_multiple_repos_all_scoped_as_destinations(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X", "org/repo-Y"])
        dests = c["destinations"]
        assert f"{GITHUB_API_HOST}/repos/org/repo-X" in dests
        assert f"{GITHUB_API_HOST}/repos/org/repo-Y" in dests

    def test_multiple_repos_all_in_scope_list(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X", "org/repo-Y"])
        assert c["scope"]["repos"] == ["org/repo-X", "org/repo-Y"]

    def test_org_wildcard_scopes_destination_to_org_only(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/*"])
        dests = c["destinations"]
        assert f"{GITHUB_API_HOST}/repos/org" in dests
        # No repo-specific or double-org destination leaked in.
        assert f"{GITHUB_API_HOST}/repos/org/*" not in dests

    def test_org_wildcard_kept_as_literal_pattern_in_scope(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/*"])
        assert c["scope"]["repos"] == ["org/*"]
```

- [ ] **Step 7: Update the `contract.py` module docstring**

Find (lines 25-30):
```python
Repo scope — ``operates_on`` (spec §10):
    ``Configuration`` exposes ``operates_on: "org/repo"`` and rejects
    ``github_enabled`` without it (UserException at parse time).  ``derive_contract``
    grants GitHub capabilities ONLY when ``operates_on`` is present and scopes the
    destination to ``api.github.com/repos/<operates_on>``; without it ALL GitHub
    capabilities are withheld (fail-closed).  The repo is never inferred from the
    task prompt (untrusted once Phase 2 begins).  The gate additionally enforces
    ``scope.repos`` and ``scope.writable_branches`` on GitHub writes (see
    ``gate.py``).
```
Replace with:
```python
Repo scope — ``operates_on`` (spec §10):
    ``Configuration`` exposes ``operates_on: list[str]`` (one or more ``"org/repo"``
    entries, or ``"org/*"`` for an entire org) and rejects ``github_enabled``
    without a non-empty list (UserException at parse time).  ``derive_contract``
    grants GitHub capabilities ONLY when ``operates_on`` is non-empty and scopes
    the destinations to ``api.github.com/repos/<entry>`` per entry — an
    ``"org/*"`` entry scopes to ``api.github.com/repos/<org>`` (the broker's
    existing child-path matching then covers any repo under that org, no broker
    change needed); without any entries ALL GitHub capabilities are withheld
    (fail-closed).  The repo is never inferred from the task prompt (untrusted
    once Phase 2 begins).  The gate additionally enforces ``scope.repos`` (via
    glob matching, so ``org/*`` patterns work) and ``scope.writable_branches`` on
    GitHub writes (see ``gate.py``).
```

- [ ] **Step 8: Change `derive_contract`'s signature and destination/scope building**

Find:
```python
def derive_contract(
    cfg: _ConfigProto,
    *,
    operates_on: str | None = None,
) -> dict:
    """Build a frozen Intent Contract from trusted config inputs.

    Args:
        cfg: The validated ``Configuration`` for this invocation.  No untrusted
            data must have been processed before this is called (spec §6 step 3).
        operates_on: Optional ``org/repo`` string identifying the repository this
            agent operates on.  When not present (current POC default), the repo
            scope is empty — capability checking still applies but the destination
            allowlist cannot be narrowed to a specific repo path.  Phase 5 should
            wire this from a future ``cfg.operates_on`` field; do NOT infer it from
            the task prompt (that is untrusted once we start Phase 2).

    Returns:
        A plain dict representing the contract.  Sign it with :func:`sign_contract`
        before storing; pass the signed envelope to the gate.
    """
```
Replace with:
```python
def derive_contract(
    cfg: _ConfigProto,
    *,
    operates_on: list[str] | None = None,
) -> dict:
    """Build a frozen Intent Contract from trusted config inputs.

    Args:
        cfg: The validated ``Configuration`` for this invocation.  No untrusted
            data must have been processed before this is called (spec §6 step 3).
        operates_on: One or more ``org/repo`` entries, or ``org/*`` for an entire
            org, identifying what this agent operates on.  When empty or absent,
            the repo scope is empty — capability checking still applies but no
            GitHub destination is ever granted (fail-closed).  Never infer this
            from the task prompt (that is untrusted once we start Phase 2).

    Returns:
        A plain dict representing the contract.  Sign it with :func:`sign_contract`
        before storing; pass the signed envelope to the gate.
    """
```

Find:
```python
    if cfg.github_enabled and operates_on:
        capabilities.extend([CAP_GH_READ, CAP_GH_WRITE_BRANCH, CAP_GH_OPEN_PR, CAP_GH_COMMENT])
        destinations.append(f"{GITHUB_API_HOST}/repos/{operates_on}")
    elif cfg.github_enabled and not operates_on:
        log.warning(
            "derive_contract: github_enabled but no operates_on repo — withholding ALL "
            "GitHub capabilities (fail-closed). Set operates_on='org/repo' to grant scoped access."
        )
```
Replace with:
```python
    if cfg.github_enabled and operates_on:
        capabilities.extend([CAP_GH_READ, CAP_GH_WRITE_BRANCH, CAP_GH_OPEN_PR, CAP_GH_COMMENT])
        for entry in operates_on:
            # "org/*" scopes to the org only — the broker's existing child-path
            # matching (github_broker._path_allowed) already covers every repo
            # under that org without needing the literal "/*" suffix, and its
            # segment-boundary check already prevents "org-evil" from matching.
            org_scoped = entry[:-2] if entry.endswith("/*") else entry
            destinations.append(f"{GITHUB_API_HOST}/repos/{org_scoped}")
    elif cfg.github_enabled and not operates_on:
        log.warning(
            "derive_contract: github_enabled but no operates_on repos — withholding ALL "
            "GitHub capabilities (fail-closed). Set operates_on to grant scoped access."
        )
```

Find:
```python
    # Repo scope
    repos: list[str] = [operates_on] if operates_on else []
```
Replace with:
```python
    # Repo scope — raw patterns (including any "org/*" wildcards); gate.py does
    # glob matching against these, so the literal pattern (not the destination
    # path) is what's stored here.
    repos: list[str] = list(operates_on) if operates_on else []
```

- [ ] **Step 9: Run all three test files to verify everything passes**

Run: `pytest tests/unit/test_contract_gate.py tests/unit/test_phase5_boot.py tests/unit/test_phase6_jsonl_chaining.py -v`
Expected: PASS — all existing tests (now using list call sites) and the four new multi-repo/wildcard tests from Step 6

- [ ] **Step 10: Commit**

```bash
git add src/advocate/contract.py tests/unit/test_contract_gate.py tests/unit/test_phase5_boot.py tests/unit/test_phase6_jsonl_chaining.py
git commit -m "feat(contract): derive_contract scopes destinations for multiple repos and org/* wildcards"
```

---

### Task 4: `gate.check_action` uses glob matching for repo scope

**Files:**
- Modify: `src/advocate/gate.py:213-224` (the scope-check block inside `check_action`)
- Modify: `tests/unit/test_contract_gate.py` (new tests only — no existing test in this file exercises `org/*` scope-check behavior yet)

**Interfaces:**
- Consumes: `contract["scope"]["repos"]: list[str]` (may contain `org/*` patterns) from Task 3's `derive_contract`.
- Produces: no change to `check_action`'s public signature — same `GateAllow | GateDenial` return. This is the final consumer of the multi-repo scope; no later task depends on internals changed here.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_contract_gate.py`, in `TestCheckAction` (this class already has a `_gh_contract` helper from Task 3 that now accepts `operates_on: list[str] | None`):

```python
    def test_org_wildcard_scope_allows_any_repo_under_org(self) -> None:
        """A contract scoped to 'org/*' must allow gh.read on ANY repo under org."""
        c = self._gh_contract(operates_on=["org/*"])
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org",
            scope_repo="org/some-other-repo",
        )
        assert isinstance(result, GateAllow)

    def test_org_wildcard_scope_denies_different_org(self) -> None:
        """A contract scoped to 'org/*' must NOT allow a repo under a different org."""
        c = self._gh_contract(operates_on=["org/*"])
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org",
            scope_repo="other-org/some-repo",
        )
        assert isinstance(result, GateDenial)
        assert result.failed_check == "scope"

    def test_multi_repo_scope_allows_either_listed_repo(self) -> None:
        c = self._gh_contract(operates_on=["org/repo-X", "org/repo-Y"])
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org/repo-Y",
            scope_repo="org/repo-Y",
        )
        assert isinstance(result, GateAllow)

    def test_multi_repo_scope_denies_unlisted_repo(self) -> None:
        c = self._gh_contract(operates_on=["org/repo-X", "org/repo-Y"])
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org/repo-Z",
            scope_repo="org/repo-Z",
        )
        assert isinstance(result, GateDenial)
        assert result.failed_check == "scope"

    def test_exact_repo_scope_still_rejects_prefix_leak(self) -> None:
        """A literal 'org/repo' pattern (no wildcard chars) must not glob-match 'org/repo-evil'."""
        c = self._gh_contract(operates_on=["org/repo"])
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org/repo-evil",
            scope_repo="org/repo-evil",
        )
        assert isinstance(result, GateDenial)
        assert result.failed_check == "scope"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_contract_gate.py -v -k "wildcard_scope or multi_repo_scope or prefix_leak"`
Expected: FAIL — `test_org_wildcard_scope_allows_any_repo_under_org` and `test_multi_repo_scope_allows_either_listed_repo` fail because the current `scope_repo not in allowed_repos` is an exact-membership check, so `"org/some-other-repo" not in ["org/*"]` is `True` → wrongly denies. (The deny-case tests already pass by coincidence since exact membership also denies those — that's fine, TDD cares about the currently-wrong allow-cases going red.)

- [ ] **Step 3: Implement the glob-based scope check**

In `src/advocate/gate.py`, find:
```python
    # 3. Scope check (only when the contract has a non-empty repos list)
    if scope_repo is not None and allowed_repos:
        if scope_repo not in allowed_repos:
            log.warning(
                "gate: scope denied — repo=%r not in contract scope",
                scope_repo,
            )
            return GateDenial(
                reason=f"repository '{scope_repo}' is not in the contract scope",
                failed_check="scope",
            )
```
Replace with:
```python
    # 3. Scope check (only when the contract has a non-empty repos list). Uses
    # fnmatch so an "org/*" pattern authorizes any repo under that org, while a
    # literal "org/repo" pattern (no wildcard characters) only matches itself —
    # fnmatch treats a pattern with no special characters as an exact match.
    if scope_repo is not None and allowed_repos:
        if not _repo_allowed(scope_repo, allowed_repos):
            log.warning(
                "gate: scope denied — repo=%r not in contract scope",
                scope_repo,
            )
            return GateDenial(
                reason=f"repository '{scope_repo}' is not in the contract scope",
                failed_check="scope",
            )
```

Then add the `_repo_allowed` helper next to the existing `_branch_allowed` helper. Find:
```python
def _branch_allowed(branch: str, allowed_patterns: list[str]) -> bool:
    """Return True if ``branch`` matches at least one glob pattern in ``allowed_patterns``.

    Patterns use shell-glob semantics (``fnmatch``): ``agent/*`` matches
    ``agent/fix-123`` but not ``main`` or ``agent`` (no slash). An empty pattern
    list denies every branch (fail-closed).
    """
    return any(fnmatch.fnmatch(branch, pat) for pat in allowed_patterns)
```
Add immediately after it:
```python


def _repo_allowed(repo: str, allowed_patterns: list[str]) -> bool:
    """Return True if ``repo`` matches at least one glob pattern in ``allowed_patterns``.

    Mirrors :func:`_branch_allowed`. A literal pattern like ``"org/repo"`` (no
    wildcard characters) matches only itself via ``fnmatch`` — it does NOT
    prefix-match ``"org/repo-evil"``. A pattern like ``"org/*"`` matches any
    repo under that org. An empty pattern list denies every repo (fail-closed).
    """
    return any(fnmatch.fnmatch(repo, pat) for pat in allowed_patterns)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_contract_gate.py -v`
Expected: PASS — the full file, including the 5 new tests from Step 1 and everything from Task 3

- [ ] **Step 5: Commit**

```bash
git add src/advocate/gate.py tests/unit/test_contract_gate.py
git commit -m "feat(gate): glob-match repo scope so org/* contracts authorize any repo under that org"
```

---

### Task 5: `component.py` wires the multi-repo/wildcard destination allowlist

**Files:**
- Modify: `src/component.py:398-401`
- Modify: `tests/unit/test_claude_runner.py:60` (`test_build_options_github_tools_added`)

**Interfaces:**
- Consumes: `Configuration.operates_on: list[str]` (Task 2), `derive_contract` (Task 3, already called at `component.py:387` with no signature change needed at that call site since it already passes `operates_on=config.operates_on`).
- Produces: `github_allowed_destinations: list[str]` passed into `AdvocateServer(...)` — no downstream task depends on this beyond the existing `AdvocateServer`/`github_broker` wiring, which already accepts a list.

- [ ] **Step 1: Fix the `test_claude_runner.py` call site (mechanical — not a TDD-relevant assertion, just an existing test's config construction)**

Find (around line 60):
```python
def test_build_options_github_tools_added():
    cfg = _config(github_enabled=True, operates_on="org/repo-X")
```
Replace with:
```python
def test_build_options_github_tools_added():
    cfg = _config(github_enabled=True, operates_on=["org/repo-X"])
```

- [ ] **Step 2: Write the failing test for multi-repo/wildcard destination building**

`_run_with_broker`'s inline destination-building logic isn't directly testable without a real config.json + AdvocateServer, so it will be extracted as a static method on `Component` (next step). Write the test against that not-yet-existing method first, in a new file, `tests/unit/test_component_github_scope.py`:

```python
"""Unit tests for Component._build_github_allowed_destinations (multi-repo/org-wildcard scoping)."""

from component import Component
from configuration import Configuration


def _cfg(**overrides):
    data = {"#anthropic_key": "KEY_NAME_ONLY"}
    data.update(overrides)
    return Configuration(**data)


def test_github_disabled_yields_no_destinations():
    cfg = _cfg(github_enabled=False)
    assert Component._build_github_allowed_destinations(cfg) == []


def test_single_repo_yields_one_destination():
    cfg = _cfg(github_enabled=True, operates_on=["org/repo-X"])
    assert Component._build_github_allowed_destinations(cfg) == ["/repos/org/repo-X"]


def test_multiple_repos_yield_multiple_destinations():
    cfg = _cfg(github_enabled=True, operates_on=["org/repo-X", "org/repo-Y"])
    assert Component._build_github_allowed_destinations(cfg) == ["/repos/org/repo-X", "/repos/org/repo-Y"]


def test_org_wildcard_yields_org_only_destination():
    cfg = _cfg(github_enabled=True, operates_on=["org/*"])
    assert Component._build_github_allowed_destinations(cfg) == ["/repos/org"]


def test_mixed_repos_and_wildcard():
    cfg = _cfg(github_enabled=True, operates_on=["org/repo-X", "other-org/*"])
    assert Component._build_github_allowed_destinations(cfg) == ["/repos/org/repo-X", "/repos/other-org"]
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/unit/test_component_github_scope.py -v`
Expected: FAIL — `AttributeError: type object 'Component' has no attribute '_build_github_allowed_destinations'`

- [ ] **Step 4: Extract the destination-building logic into a static method**

In `src/component.py`, find:
```python
        if config.github_enabled and config.operates_on:
            github_allowed_destinations = [f"/repos/{config.operates_on}"]
        else:
            github_allowed_destinations = []
```
Replace with:
```python
        github_allowed_destinations = self._build_github_allowed_destinations(config)
```

Then add this static method near `_secret_values` (around line 476, same style):
```python
    @staticmethod
    def _build_github_allowed_destinations(config: Configuration) -> list[str]:
        """Path-prefix allowlist for the GitHub broker, one entry per operates_on repo.

        An "org/*" entry becomes the org-only prefix "/repos/org" — the broker's
        existing child-path matching (github_broker._path_allowed) already scopes
        that to every repo under the org without further narrowing here.
        """
        if not (config.github_enabled and config.operates_on):
            return []
        return [
            f"/repos/{entry[:-2]}" if entry.endswith("/*") else f"/repos/{entry}" for entry in config.operates_on
        ]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/unit/test_component_github_scope.py tests/unit/test_claude_runner.py -v`
Expected: PASS

- [ ] **Step 6: Run the broader test suite to catch any other consumer of the old inline shape**

Run: `pytest tests/unit/ -v -x`
Expected: PASS (this also catches `test_phase5_boot.py` and `test_phase6_jsonl_chaining.py` regressions from Task 3, and confirms nothing else in `src/component.py` or its tests references the old single-string `operates_on` shape)

- [ ] **Step 7: Commit**

```bash
git add src/component.py tests/unit/test_claude_runner.py tests/unit/test_component_github_scope.py
git commit -m "feat(component): build github_allowed_destinations from multi-repo/org-wildcard operates_on"
```

---

### Task 6: `load_github_repos` sync action

**Files:**
- Modify: `src/sync_actions.py` (add `list_github_repos`, import `GITHUB_API_BASE`)
- Modify: `tests/unit/test_sync_actions.py` (add tests)

**Interfaces:**
- Consumes: `GITHUB_API_BASE` from `advocate.brokers.github_broker` (existing constant, `"https://api.github.com"`); `SelectElement` from `keboola.component.sync_actions` (existing import pattern, see `component-build-ui` skill reference).
- Produces: `list_github_repos(github_token: str, http_client: HttpClient | None = None) -> list[SelectElement]` — Task 7 wires this into `Component.load_github_repos` sync action.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_sync_actions.py` (this file already has `FakeResponse`/`FakeHttpClient` classes for `check_anthropic_connection` — reuse them, extended with a `get_raw` method since `list_github_repos` uses `GET`, not `POST`):

```python
from sync_actions import check_anthropic_connection, list_github_repos


class FakeGetResponse:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else []

    def json(self):
        return self._json_body


class FakePaginatedHttpClient:
    """Serves one page of repos per call, based on the 'page' param."""

    def __init__(self, pages: dict[int, list[dict]], status_code: int = 200):
        self._pages = pages
        self._status_code = status_code
        self.calls = []

    def get_raw(self, endpoint_path=None, headers=None, params=None, **kwargs):
        self.calls.append({"endpoint": endpoint_path, "headers": headers, "params": params, "kwargs": kwargs})
        page = params["page"]
        return FakeGetResponse(self._status_code, self._pages.get(page, []))


class RaisingGetHttpClient:
    def get_raw(self, *args, **kwargs):
        raise ConnectionError("connection refused")


def test_list_github_repos_empty_token_raises():
    with pytest.raises(UserException):
        list_github_repos("", http_client=FakePaginatedHttpClient({}))


def test_list_github_repos_single_page():
    client = FakePaginatedHttpClient({1: [{"full_name": "acme/widgets"}, {"full_name": "acme/gadgets"}]})
    repos = list_github_repos("TOKEN_NAME_ONLY", http_client=client)
    assert [r.value for r in repos] == ["acme/widgets", "acme/gadgets"]
    assert [r.label for r in repos] == ["acme/widgets", "acme/gadgets"]
    assert client.calls[0]["headers"]["Authorization"] == "Bearer TOKEN_NAME_ONLY"


def test_list_github_repos_paginates_fully():
    """A full first page (100 entries) triggers a second page fetch; a short page stops."""
    page_1 = [{"full_name": f"acme/repo-{i}"} for i in range(100)]
    page_2 = [{"full_name": "acme/repo-100"}]
    client = FakePaginatedHttpClient({1: page_1, 2: page_2})
    repos = list_github_repos("TOKEN_NAME_ONLY", http_client=client)
    assert len(repos) == 101
    assert repos[-1].value == "acme/repo-100"
    assert len(client.calls) == 2


def test_list_github_repos_stops_on_empty_page():
    client = FakePaginatedHttpClient({1: []})
    repos = list_github_repos("TOKEN_NAME_ONLY", http_client=client)
    assert repos == []
    assert len(client.calls) == 1


def test_list_github_repos_401_raises_auth_error():
    client = FakePaginatedHttpClient({1: []}, status_code=401)
    with pytest.raises(UserException) as exc:
        list_github_repos("BAD_TOKEN", http_client=client)
    assert "authentication" in str(exc.value).lower()


def test_list_github_repos_403_raises_auth_error():
    client = FakePaginatedHttpClient({1: []}, status_code=403)
    with pytest.raises(UserException) as exc:
        list_github_repos("BAD_TOKEN", http_client=client)
    assert "authentication" in str(exc.value).lower()


def test_list_github_repos_other_error_raises():
    client = FakePaginatedHttpClient({1: []}, status_code=500)
    with pytest.raises(UserException) as exc:
        list_github_repos("TOKEN_NAME_ONLY", http_client=client)
    assert "500" in str(exc.value)


def test_list_github_repos_connection_error_becomes_user_exception():
    with pytest.raises(UserException) as exc:
        list_github_repos("TOKEN_NAME_ONLY", http_client=RaisingGetHttpClient())
    assert "Could not reach" in str(exc.value) or "GitHub" in str(exc.value)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_sync_actions.py -v -k list_github_repos`
Expected: FAIL with `ImportError: cannot import name 'list_github_repos' from 'sync_actions'`

- [ ] **Step 3: Implement `list_github_repos` in `src/sync_actions.py`**

Find the module's imports and top-level constants:
```python
from __future__ import annotations

import logging

from keboola.component.exceptions import UserException
from keboola.component.sync_actions import ValidationResult
from keboola.http_client import HttpClient

ANTHROPIC_API_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
# Cheapest model for the validation ping; 1 token keeps cost negligible.
TEST_MODEL = "claude-haiku-4-5"
# Bound the connection test so a hung endpoint fails fast rather than blocking the UI.
REQUEST_TIMEOUT_S = 15
```
Replace with:
```python
from __future__ import annotations

import logging

from keboola.component.exceptions import UserException
from keboola.component.sync_actions import SelectElement, ValidationResult
from keboola.http_client import HttpClient

from advocate.brokers.github_broker import GITHUB_API_BASE

ANTHROPIC_API_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
# Cheapest model for the validation ping; 1 token keeps cost negligible.
TEST_MODEL = "claude-haiku-4-5"
# Bound the connection test so a hung endpoint fails fast rather than blocking the UI.
REQUEST_TIMEOUT_S = 15
# GitHub REST pagination — 100 is the API's max per_page.
GITHUB_REPOS_PAGE_SIZE = 100
```

Then append this function at the end of the file:
```python
def list_github_repos(github_token: str, http_client: HttpClient | None = None) -> list[SelectElement]:
    """Paginate GET /user/repos with the configured token; returns every repo, no cap.

    Populates the "Repositories" multi-select in the config UI. Never returns an
    "org/*" wildcard entry — that pattern is always typed manually, since a repo
    listing has no natural way to represent "the whole org".
    """
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
    try:
        while True:
            response = client.get_raw(
                "/user/repos",
                headers=headers,
                params={"per_page": GITHUB_REPOS_PAGE_SIZE, "page": page},
                timeout=REQUEST_TIMEOUT_S,
            )
            if response.status_code in (401, 403):
                raise UserException("GitHub rejected the token (authentication failed). Check #github_token.")
            if response.status_code != 200:
                raise UserException(f"Could not list GitHub repositories (HTTP {response.status_code}).")
            batch = response.json()
            if not batch:
                break
            repos.extend(SelectElement(value=r["full_name"], label=r["full_name"]) for r in batch)
            if len(batch) < GITHUB_REPOS_PAGE_SIZE:
                break
            page += 1
    except UserException:
        raise
    except Exception as exc:
        raise UserException(f"Could not reach the GitHub API: {exc}") from exc

    logging.info("Loaded %d GitHub repositories for the repository picker.", len(repos))
    return repos
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_sync_actions.py -v`
Expected: PASS — all existing `check_anthropic_connection` tests plus the new `list_github_repos` tests

- [ ] **Step 5: Commit**

```bash
git add src/sync_actions.py tests/unit/test_sync_actions.py
git commit -m "feat(sync-actions): add list_github_repos, paginates GitHub /user/repos fully"
```

---

### Task 7: Wire the `load_github_repos` sync action into `Component`

**Files:**
- Modify: `src/component.py` (imports, new `@sync_action` method near `test_connection`)

**Interfaces:**
- Consumes: `list_github_repos` from Task 6.
- Produces: the `load_github_repos` sync action referenced by `component_config/configSchema.json`'s `operates_on.options.async.action` (Task 1, Step 5) — this closes the loop between schema and backend.

This codebase has no existing test that instantiates `Component` and calls a `@sync_action`-decorated method directly (verified: `grep -rn "test_connection\b" tests/unit/` only matches `test_sync_actions.py`'s test of the underlying `check_anthropic_connection` function, and an unrelated `test_partial_config_for_test_connection` in `test_configuration.py` that only tests `Configuration` parsing). The established convention is: unit-test the pure function the sync action delegates to (already done exhaustively for `list_github_repos` in Task 6), and leave the two-line `@sync_action` wrapper itself covered only by the manual schema-sandbox check (see Final Verification below) — exactly how `test_connection` is handled today. This task follows that same convention rather than inventing a new one.

- [ ] **Step 1: Wire the sync action in `src/component.py`**

Find:
```python
from sync_actions import check_anthropic_connection
```
Replace with:
```python
from sync_actions import check_anthropic_connection, list_github_repos
```

Find:
```python
    @sync_action("testConnection")
    def test_connection(self):
        """Validate #anthropic_key with a single cheap in-process API call."""
        config = Configuration(**self.configuration.parameters)
        return check_anthropic_connection(config.anthropic_key)
```
Replace with:
```python
    @sync_action("testConnection")
    def test_connection(self):
        """Validate #anthropic_key with a single cheap in-process API call."""
        config = Configuration(**self.configuration.parameters)
        return check_anthropic_connection(config.anthropic_key)

    @sync_action("load_github_repos")
    def load_github_repos(self):
        """Populate the Repositories multi-select with repos the token can access."""
        config = Configuration(**self.configuration.parameters)
        return list_github_repos(config.github_token)
```

- [ ] **Step 2: Run the entire unit test suite**

Run: `pytest tests/unit/ -v`
Expected: PASS — full suite green, confirming Tasks 1-7 are all mutually consistent. (No new unit test is added by this task itself — see the convention note above; Task 6 already covers `list_github_repos`'s behavior exhaustively, and this task is a two-line pass-through identical in shape to the already-untested `test_connection`.)

- [ ] **Step 3: Commit**

```bash
git add src/component.py
git commit -m "feat(component): wire load_github_repos sync action for the Repositories picker"
```

---

## Final verification

- [ ] Run the complete unit suite once more: `pytest tests/unit/ -v`
- [ ] Run `ruff format --check` and `ruff check` (this repo's pre-commit hooks run these automatically on commit, but verify explicitly): `ruff format --check src/ tests/ && ruff check src/ tests/`
- [ ] Validate the schema is still well-formed JSON: `python3 -m json.tool component_config/configSchema.json > /dev/null && echo VALID`
- [ ] Manual: open the config UI (Ctrl+D schema sandbox per the component-build-ui skill) and confirm:
  - The info notes render under each section title (GitHub, Output Settings, MCP Servers, Plugins, Advanced), not on top of the first field.
  - "Repositories" renders as a multi-select with a "Load Repositories" button, autoloads, and accepts manually-typed entries (test typing `org/*`).
  - "MCP Servers" section shows an inner "Servers" array (not duplicated "MCP Servers" / "MCP Servers").
  - "Plugins" section shows an inner "Marketplaces" array (not "Plugins (Advanced)").
  - SDK Version's description mentions "0.2.101" directly, not only in the tooltip.
