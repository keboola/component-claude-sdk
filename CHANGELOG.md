# Changelog

## [Unreleased]

### Fixed

- **`#`-prefixed keys in env/header maps now reach the agent under their clean
  name.** Keboola decrypts the *value* of a `#`-prefixed key but keeps the key
  *name* verbatim, so `settings_json.env: {"#WEBHOOK_URL": …}` previously gave
  the agent an env var literally named `#WEBHOOK_URL` — unreachable as
  `$WEBHOOK_URL`. The configuration model now strips the `#` marker from key
  names in `settings_json.env` (object form), `mcp_servers[].env` and
  `mcp_servers[].headers`; a `#KEY`/`KEY` collision in one map is rejected with
  a clear error. Values declared secret this way are additionally scrubbed from
  captured output (`_secret_values`).

## [Unreleased] — feat/advocate-broker

### Added

- **Advocate Broker V0** — a non-root, single-UID in-container credential broker
  that prevents the LLM agent from accessing raw secrets at rest or in process
  memory (spec `docs/superpowers/specs/2026-06-15-advocate-broker-sandbox.md`).

  Core mechanism: the Advocate (parent process) holds all decrypted secrets and
  injects them per-request over a loopback-TCP proxy at `127.0.0.1:<port>`. The
  agent subprocess is spawned with a cleared environment — no real Anthropic key,
  no KBC_TOKEN, no GitHub token — and an `ANTHROPIC_BASE_URL` pointing at the
  loopback proxy. The proxy injects the real credentials server-side.

  - `src/advocate/server.py` — loopback-TCP AdvocateServer (Anthropic proxy +
    GitHub broker + MCP broker endpoints).
  - `src/advocate/anthropic_proxy.py` — SSE-streaming Anthropic proxy with
    real-key injection.
  - `src/advocate/brokers/` — GitHub and MCP tool brokers.
  - `src/advocate/contract.py` — Intent Contract derivation and HMAC signing.
  - `src/advocate/gate.py` — deterministic contract gate (no LLM in the path).
  - `src/advocate/sandbox.py` — BPF seccomp filter builder (V1+ stub, not used
    in V0; retained for future hardening, spec §12.6).
  - `src/advocate/idempotency.py` — per-invocation idempotency log.

- **Env-scrub re-exec** (`src/component.py`): KBC_TOKEN is stripped from the
  exec-time environment via `os.execve` and passed back via an inherited pipe fd,
  so `/proc/<advocate>/environ` no longer exposes the Storage token. After the
  base class captures it at construction the value is **purged from `os.environ`
  before the agent spawns**, so it is not inherited by the agent subprocess (the
  SDK transport merges `os.environ` into the agent env).

- **Atomic config.json scrub** (`src/component.py`): decrypted `#`-secret values
  are replaced with empty strings before the agent spawns; the scrubbed file is
  written atomically (temp+rename) so the keboola.component base class continues
  reading structural config without interruption.

- **Session JSONL chaining — security invariants** (tests only, spec §7.3): the
  session JSONL is secret-free by construction and is treated as untrusted input
  by the next agent. The downstream contract is derived and HMAC-signed from the
  agent's own trusted task BEFORE any inherited JSONL is loaded, so a poisoned
  upstream transcript cannot widen authority; the deterministic gate hard-denies
  every off-contract capability/destination regardless of transcript content.
  Pinned by `tests/unit/test_phase6_jsonl_chaining.py`.

- `scripts/sandbox_probe.py` — standalone Phase 0 diagnostic script to verify
  seccomp/setuid primitives and ptrace_scope on any Linux container. Re-run on
  platform/runtime changes (spec §12.6 runtime dependency).

### Security (review follow-ups — commit `d493924` review)

- **HIGH-1 — KBC_TOKEN no longer re-injected into `os.environ`.** The boot path
  set the token transiently for the base-class capture, then re-left it in the
  environment; because the SDK transport merges `os.environ` into the agent env,
  it would have leaked straight into the agent. The token is now purged from
  `os.environ` before any action runs, and the cleared agent env explicitly blanks
  `KBC_TOKEN`/`GITHUB_TOKEN`/`GH_TOKEN`.
- **HIGH-2 — `ptrace_scope >= 1` is asserted at boot.** All in-memory secret
  protection rests on it; the run fails closed when `ptrace_scope=0` (dev/test
  override: `ADVOCATE_ALLOW_UNSAFE_PTRACE=1`).
- **HIGH-3 — GitHub access is repo- and branch-scoped.** `github_enabled` now
  requires `operates_on` (`"org/repo"`) — fail closed at config parse. The broker
  destination allowlist, the contract destination, and the gate's `scope_repo`
  check all bind to that single repo; ref-targeting REST writes are gated against
  `writable_branches` (default `agent/*`, so pushes to `main` are denied).
  `derive_contract` withholds all GitHub capabilities when `operates_on` is absent.
  Hardened after adversarial re-verification: the GitHub broker now rejects path
  traversal (`..`/`.`/`//`, raw and percent-decoded) at the validator — `httpx`
  collapses `..` before the request, so `/repos/org/repo/../other` would otherwise
  reach a different repo with the real PAT; Contents-API writes with no explicit
  `branch` (which default to the repo default branch, often `main`) are denied; and
  repository-settings / branch-protection writes (`PATCH /repos/{o}/{r}`,
  `…/branches/*/protection`) require a withheld `gh.admin` capability so a
  `write_branch`-scoped agent cannot re-point `default_branch` or disable protection.
  Hardened again after a second adversarial re-verification: the capability
  classifier, repo-scope, writable-branch, and destination checks now run on the
  **once-percent-decoded** path (GitHub decodes path segments once before
  routing), so an encoded letter — e.g. `PUT /repos/org/repo/pulls/42/%6Derge`
  (`%6D`=`m`) → `…/merge`, `…/branches/main/%70rotection` → `…/protection`,
  `PATCH …/git/refs/%68eads/main` → `…/heads/main` — can no longer hide a
  privileged op (`gh.merge`/`gh.admin`/push-to-`main`) behind a benign
  `gh.write_branch` classification. The raw path is still forwarded verbatim on
  the wire (so gate view == GitHub's routed path), and multiply percent-encoded
  paths are rejected at the validator (fail closed).
- **HIGH-4 — the Anthropic endpoint is gated.** `/v1/messages` runs the contract
  gate (capability `anthropic`, pinned destination) on both the structured and
  transparent-proxy paths before injecting the real key.
- **MED-1 — stdio MCP subprocess env minimised.** The MCP launcher no longer
  passes the Advocate's full `os.environ` to the subprocess (only a non-secret
  launch allowlist + the server's own `env`). stdio MCP servers remain *not*
  credential-isolated under the same-UID runtime (documented); only remote MCP is.
- **MED-3 — broker idempotency keys are server-derived** from the request content
  (GitHub: method+path+body; MCP: server+method+params), not the agent-supplied
  `action_id`, preventing cached-reply suppression of a distinct legitimate call.
- **Plugin install fix.** The GitHub token for private plugin sources is now
  injected into the install subprocess env (Advocate-side, short-lived) instead of
  being assumed present in the cleared agent env — private marketplace clones can
  authenticate again, without exposing the token to the agent.

### Changed

- Component boot sequence (`src/component.py`) wired into the full Broker V0
  sequence: ptrace_scope assertion → config parse → config scrub → contract
  derivation → AdvocateServer start → cleared-env agent spawn → task loop →
  output promotion → server stop.

### Removed

- Temporary on-platform diagnostic mode (`__advocate_runtime_probe` config gate
  and `_run_advocate_probe` method in `component.py`, `src/advocate/runtime_probe.py`,
  and its tests). Findings are permanently recorded in the spec (§12.6). The
  re-runnable diagnostic lives in `scripts/sandbox_probe.py`.
- `scripts/spike_uds_transport.py` — UDS transport spike (finding: loopback TCP
  is the right transport; captured in spec §12.6).
- `scripts/Dockerfile.probe` — probe Dockerfile (superseded by the inline
  `docker run` one-liner in `scripts/sandbox_probe.py`).
