# Changelog

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
  so `/proc/<advocate>/environ` no longer exposes the Storage token.

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

### Changed

- Component boot sequence (`src/component.py`) wired into the full Broker V0
  sequence: config parse → config scrub → contract derivation → AdvocateServer
  start → cleared-env agent spawn → task loop → output promotion → server stop.

### Removed

- Temporary on-platform diagnostic mode (`__advocate_runtime_probe` config gate
  and `_run_advocate_probe` method in `component.py`, `src/advocate/runtime_probe.py`,
  and its tests). Findings are permanently recorded in the spec (§12.6). The
  re-runnable diagnostic lives in `scripts/sandbox_probe.py`.
- `scripts/spike_uds_transport.py` — UDS transport spike (finding: loopback TCP
  is the right transport; captured in spec §12.6).
- `scripts/Dockerfile.probe` — probe Dockerfile (superseded by the inline
  `docker run` one-liner in `scripts/sandbox_probe.py`).
