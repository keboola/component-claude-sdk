# Implementation Plan — Advocate Broker + In-Container Sandbox

> Spec: `docs/superpowers/specs/2026-06-15-advocate-broker-sandbox.md`
> Branch: feat/advocate-broker (from initial-implementation)
> Date: 2026-06-15
> Approach: TDD-first. Each phase lands with its tests green before the next.

---

## Guiding constraints

- **Floor first, hardening later.** Phases 1–4 use only the *guaranteed* primitives (seccomp +
  UID/FS + UDS). Namespaces (spec §5.2) and the E2B backend (spec §11) are deferred behind the
  non-blocking infra answers (spec §12).
- **Security must not depend on `claude-sdk`.** After this work, removing/bypassing the SDK's
  permission model changes nothing: the agent has no secrets and no network regardless.
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

## Phase 1 — Sandbox launcher (floor) (TDD)

`src/advocate/sandbox.py` — spawn a child under the floor.

- [ ] `build_seccomp_filter()` → deny `AF_INET`/`AF_INET6`, allow `AF_UNIX`. Unit test asserts the
      BPF program shape; integration test asserts a real child cannot open an inet socket.
- [ ] `spawn_sandboxed(argv, *, uid, cleared_env, workspace, uds_path)`:
      `prctl(NO_NEW_PRIVS)` → install filter → [optional namespaces if Phase 0 said yes] →
      `setuid(uid)` → `exec`.
- [ ] Tests: child env contains no secret keys; child cannot read `/data/config.json`; child cannot
      `ptrace` parent; `AF_UNIX` to `uds_path` works.

## Phase 2 — UDS broker + Anthropic proxy (TDD)

`src/advocate/server.py`, `src/advocate/anthropic_proxy.py`.

- [ ] UDS server with a **narrow, strictly-validated** request schema (treat all input as untrusted).
- [ ] Anthropic proxy: agent → UDS → inject `#anthropic_key` → upstream → stream back. Wire
      `ANTHROPIC_BASE_URL` in the agent's cleared env to the UDS.
- [ ] `action_id` idempotency cache (dropped-response double-execute protection).
- [ ] Tests (VCR for upstream): agent with no key + no network completes a model turn via the proxy;
      replay of the same `action_id` returns the cached result, no second upstream call.

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

## Test ledger (the security assertions that must stay green)

1. agent process cannot open an `AF_INET` socket (network kill)
2. agent cannot read `/data/config.json` (secret-file isolation)
3. agent env contains no secret keys (env isolation)
4. agent cannot read parent memory / ptrace (memory isolation)
5. model/MCP/GitHub all work for the agent with zero secrets in its box (functionality via broker)
6. gate denies off-contract destination/capability (deterministic)
7. dropped-response retry does not double-execute (`action_id` idempotency)
8. contaminated session JSONL cannot expand downstream authority (chaining)

(Removed from the original ledger per PR #1 review: provenance-freeze and bit-budget assertions —
those features are out of POC scope, see spec §7.4.)

---

## Open items before a PR

- [ ] Phase 0 findings appended to spec §12.
- [ ] Decide floor-only vs floor+namespaces for V0 (depends on Phase 0).
- [ ] CHANGELOG / README note on the new security model.
- [ ] Confirm with platform team: E2B backend opt-in (spec §12.2) for the V2 path.
