# Implementation Plan — Advocate Broker + In-Container Sandbox

> Spec: `docs/superpowers/specs/2026-06-15-advocate-broker-sandbox.md`
> Branch: feat/advocate-broker (from initial-implementation)
> Date: 2026-06-15
> Approach: TDD-first. Each phase lands with its tests green before the next.
>
> **Update 2026-06-16d — Broker V0 pivot (on-platform findings):**
> On-platform probes (cf-dev jobs 47975338 / 47976834 / 47978513) disproved the root+UID-drop+seccomp
> floor. The design pivots to **Broker V0: single-UID (euid=1000), loopback-TCP, cleared agent env,
> unlinked config, env-scrub, ptrace-scope memory protection.** No setuid, no seccomp AF_INET deny, no
> UDS server. See spec §12.6 for the full findings record and §5.1 for the revised boundary.
>
> Phase 1's sandbox launcher (`src/advocate/sandbox.py`) must be simplified: drop seccomp/setuid
> from the V0 critical path (keep the module as a stub for future V1+ hardening). The transport is
> loopback TCP throughout. Phase 15 (task #15) tracks this rework.

---

## Guiding constraints

- **Single-UID broker first, hardening later.** V0 uses only the primitives confirmed on-platform
  (cleared env + unlink + env-scrub + loopback-TCP + ptrace-scope). Seccomp, UID-drop, namespaces
  (spec §5.2) and the E2B backend (spec §11) are deferred to V1+.
- **Security must not depend on `claude-sdk`.** After this work, removing/bypassing the SDK's
  permission model changes nothing: the agent holds no reusable credentials regardless.
- **Setup UX unchanged.** No new user-facing security knobs (spec §10).
- Run the suite with the repo's runner (e.g. `.venv/bin/pytest tests/ -q`) before each push.

---

## Phase 0 — Spike: confirm the floor on the real runtime (½–1 day)

Goal: de-risk before building. Prove, inside an actual component job, that:

- [ ] a self-imposed seccomp filter (`prctl(NO_NEW_PRIVS)` + `seccomp(SET_MODE_FILTER)`) blocks
      `socket(AF_INET)` in a child and is inherited across `exec` (test: child `curl`/python socket
      fails; an `AF_UNIX` connect succeeds).
- [ ] the component can run as root and `setuid` to an unprivileged UID.
- [ ] the unprivileged child cannot read a root-owned `chmod 600 /data/config.json`.
- [ ] (probe only, do not depend on) whether `unshare(CLONE_NEWNET/NEWNS/NEWPID)` succeeds — records
      which §5.2 hardening is available.

Output: a short findings note appended to the spec §12. If the floor holds (expected per kbc-stacks),
proceed. If even self-imposed seccomp is blocked (not expected), escalate — only the E2B/plan-v3 path
remains.

---

## Phase 1 — Agent launcher (V0: cleared env + unlink + env-scrub) (TDD)

`src/advocate/sandbox.py` — spawn the agent with a cleared environment.

> **Revised 2026-06-16d:** seccomp/setuid/UDS are NOT in the V0 critical path (see plan header +
> spec §12.6). The launcher's job in V0 is: unlink config.json, perform env-scrub re-exec, build
> cleared agent env, exec the agent. The seccomp/BPF functions remain in the module as stubs for
> future V1+ use but MUST NOT be called in the V0 production path.

- [ ] `unlink_config(path)` — unlinks /data/config.json after Advocate has read it; asserts the
      file does not exist afterward.
- [ ] `env_scrub_reexec(kbc_token_fd)` — re-execs the Advocate process with a scrubbed environment,
      passing KBC_TOKEN via the inherited fd. After re-exec, `/proc/self/environ` must not contain
      KBC_TOKEN or any #secret value.
- [ ] `spawn_agent(argv, *, cleared_env, workspace)` — execs the agent with the cleared env (no
      KBC_TOKEN, no #anthropic_key, only routing values). No setuid, no seccomp.
- [ ] Tests: agent env contains no secret keys; config.json does not exist when agent starts;
      advocate environ does not expose KBC_TOKEN after env-scrub; `AF_UNIX` / loopback TCP from
      agent to proxy port works.
- [ ] Stub (do not call in V0): `build_seccomp_filter()`, `spawn_sandboxed_with_seccomp()` — kept
      for future V1+ hardening once the platform supports it.

## Phase 2 — Loopback-TCP broker + Anthropic proxy (TDD)

`src/advocate/server.py`, `src/advocate/anthropic_proxy.py`.

> **Revised 2026-06-16d:** the server listens on loopback TCP (`127.0.0.1:<ephemeral-port>`), not
> a UDS. `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>` in the agent env. The server schema remains
> narrow and strictly-validated (treat all input as untrusted).

- [ ] Loopback-TCP server with a narrow, strictly-validated request schema.
- [ ] Anthropic proxy: agent → loopback TCP → strip dummy key → inject `#anthropic_key` → upstream
      (`api.anthropic.com`, pinned) → stream back. Wire `ANTHROPIC_BASE_URL` in the agent's cleared
      env to the loopback port.
- [ ] Upstream pinning: the proxy MUST NOT forward to a host supplied by the agent; it always calls
      `api.anthropic.com` directly.
- [ ] `action_id` idempotency cache (dropped-response double-execute protection).
- [ ] Tests (VCR for upstream): agent with dummy key + cleared env completes a model turn via the
      proxy; replay of the same `action_id` returns the cached result, no second upstream call.

## Phase 3 — Tool brokers: MCP + GitHub/HTTP (TDD)

`src/advocate/brokers/`.

- [ ] MCP servers launched **on the Advocate side** with their `#secrets`; agent calls them as UDS
      RPCs. Remove raw Bearer/`env` secrets from the agent box.
- [ ] GitHub/HTTP tool executor: scoped token injected server-side; destination must match config.
- [ ] Tests: agent issues an MCP read + a GitHub action via UDS with **no secrets in its env**; an
      off-config destination is rejected (no SSRF).

## Phase 4 — Contract gate (TDD)

`src/advocate/contract.py`, `src/advocate/gate.py`.

- [ ] Phase 0 contract: derive from `system_prompt + task + flow ctx + declared tools`, sign.
      (Auto-derived; no user authoring. Static — not expanded at runtime.)
- [ ] Gate: **deterministic only** — capability/destination/scope ∈ contract? else hard deny. No LLM
      in the path (per PR #1 review; spec §7.2).
- [ ] Tests: off-contract capability/destination → hard deny; in-scope dev action (repo-scoped) passes.
- [ ] **Out of POC (spec §7.4), do not build now:** egress bit-budget, secret-blind LLM judge,
      runtime provenance-freeze. Demoted to future research; nothing depends on them.

## Phase 3 findings — CLI wiring (Phase 5 input)

> Investigated by the Phase 3 implementer.  These findings inform Phase 5's wiring work.
> Verified by grepping the bundled claude CLI binary at `/Users/matyasjirat/.local/bin/claude`
> (Mach-O arm64, strings-extracted).

### Anthropic proxy (confirmed, Phase 2)

`ANTHROPIC_UNIX_SOCKET` is present in the binary with context string:
`"process.env.ANTHROPIC_UNIX_SOCKET is set (claude ssh remote), and the local proxy is API-key-authed."`.
Setting this env var in the agent's cleared env points the CLI's Anthropic calls at the Advocate UDS.
This is the same mechanism Phase 2 already relies on.

### MCP wiring (confirmed mechanism, Phase 5 to implement)

The binary contains `MCP_PROXY_URL` and `MCP_PROXY_PATH` env var names.  The pattern
`lmK(H)` parses `"uds:<path>"`, raw Unix paths (`/…`), and Windows named pipes.

**Recommended Phase 5 approach**: For each configured MCP server, point its URL at
`uds://<advocate-socket-path>` (for remote/HTTP servers) or set `MCP_PROXY_PATH` to the socket path
(for stdio servers).  The CLI will route the MCP traffic to the Advocate UDS; the broker
dispatches to the real server with secrets injected server-side.  **NOT YET WIRED** — Phase 5 must
validate by launching the CLI with these env vars set and confirming MCP tool calls reach
`/v1/mcp` on the UDS.

### GitHub wiring (unconfirmed, Phase 5 to investigate)

The binary has `GITHUB_API_URL` (likely `gh` CLI convention) and the standard HTTP proxy vars
(`HTTP_PROXY`, `HTTPS_PROXY`).  The CLI's GitHub tooling uses `gh` / `git` CLI subprocesses;
those tools respect `GITHUB_API_URL` and `HTTPS_PROXY` for HTTP-level interception.

**Phase 5 approach (to verify)**: Set `HTTPS_PROXY=http+unix://<socket>/<path>` for the agent
process; write a thin HTTP-over-UDS proxy shim that the Advocate serves on the UDS alongside the
broker endpoints.  Alternative: a thin shim binary named `gh` on the agent's `PATH` that
translates `gh api …` calls to UDS RPCs at `/v1/github`.

The shim approach is simpler and avoids TLS interception.  Phase 5 must confirm by running
`gh api /repos/…` in the agent box with the shim and verifying the Advocate receives the RPC.

**`unix_socket_3p_under_pin` / `unix_socket_ssh_under_pin`** — these appear to be telemetry tags
for managed-policy enforcement when the CLI detects a UDS; they do not block UDS use but may emit
warnings about org-pin requirements.  Not blocking for the POC.

---

## Phase 5 — Wire `component.py` (TDD/integration)

- [ ] Split `run()` into Advocate-parent vs agent-spawn (spec §6 boot sequence).
- [ ] Replace `_build_env` (secrets → agent) with a **cleared** env + `ANTHROPIC_BASE_URL`.
- [ ] `plugin_manager`: stop `{**os.environ, **env}` inheritance; move install to a netless,
      secret-free path or to the Advocate side.
- [ ] Output promotion (`/data/out`, JSONL transcript, manifests) happens in the **parent**.
- [ ] datadir end-to-end: happy path unchanged from the user's perspective; transcript still written.

## Phase 6 — Session JSONL chaining (TDD)

- [ ] Confirm JSONL is secret-free by construction (the load-bearing property).
- [ ] Next-agent contract derived from its own task; inherited JSONL loaded as **untrusted** context
      after signing (so a poisoned transcript cannot widen the contract).
- [ ] Test: a contaminated upstream JSONL cannot grant the downstream agent off-contract authority.

## Phase 7 (deferred) — Hardening & V2

- [ ] Namespaces (§5.2) once infra confirms `unprivileged_userns_clone` + runtime + PSA.
- [ ] `bubblewrap`/`nsjail` in the image if approved.
- [ ] E2B backend (`runtimeBackendType: e2bSandbox`) for customer-facing/multi-tenant.

---

## Test ledger (the security assertions that must stay green) — revised 2026-06-16d

1. ~~agent process cannot open an `AF_INET` socket (network kill)~~ — **REMOVED V0**: seccomp
   AF_INET deny is not used in V0 (breaks loopback TCP). The agent retains container network.
2. `/data/config.json` does not exist when the agent process starts (unlink isolation)
3. agent env contains no secret keys — no `KBC_TOKEN`, no `#anthropic_key`, no `#github_token`
4. Advocate's `/proc/self/environ` does not expose `KBC_TOKEN` after env-scrub re-exec
5. agent cannot ptrace Advocate or read `/proc/<advocate>/mem` (ptrace_scope=1 verified)
6. model/MCP/GitHub all work for the agent with zero secrets in its env (functionality via
   loopback-TCP broker)
7. gate denies off-contract destination/capability (deterministic)
8. dropped-response retry does not double-execute (`action_id` idempotency)
9. contaminated session JSONL cannot expand downstream authority (chaining)

(Removed from the original ledger per PR #1 review: provenance-freeze and bit-budget assertions —
those features are out of POC scope, see spec §7.4.)
(Removed in 2026-06-16d revision: AF_INET socket block — mechanism not available in V0.)

---

## Open items before a PR

- [ ] Phase 0 findings appended to spec §12.
- [ ] Decide floor-only vs floor+namespaces for V0 (depends on Phase 0).
- [ ] CHANGELOG / README note on the new security model.
- [ ] Confirm with platform team: E2B backend opt-in (spec §12.2) for the V2 path.
