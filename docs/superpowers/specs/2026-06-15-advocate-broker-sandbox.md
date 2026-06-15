# Advocate Broker + In-Container Sandbox — Security Design Spec

> Type: security architecture (amends the base design spec)
> Component ID: keboola.app-claude-sdk
> Status: draft for review
> Date: 2026-06-15
> Branch: feat/advocate-broker (from initial-implementation)
> Amends: `docs/superpowers/specs/2026-06-14-claude-sdk-design.md` §2.4, §2.9, §6.5, R7
> Relationship to platform-level work: this is the **plan-v3 Two-Process model collapsed into a
> single Keboola component container**. It does not require new platform infrastructure.

---

## 1. Why this document exists

The base design spec (§6.5) states: *"The Keboola container is the sandbox (process isolation,
read-only image, controlled egress); the SDK provides no OS-level sandbox."* That stance puts the
**decrypted secrets and the untrusted LLM loop in the same process space**. For a component whose
whole job is to feed attacker-influenceable content (GitHub issues/PRs, repo contents, table data,
MCP tool results) into an autonomous agent, that is the classic prompt-injection blast-radius
problem:

- `#anthropic_key`, `#github_token`, per-MCP-server secrets, and the platform-injected `KBC_TOKEN`
  all live in the same container the agent controls.
- The agent runs headless with a non-prompting `permission_mode` and (when GitHub is enabled, or via
  `allowed_tools` / `settings_json` passthrough) has a shell. `env | grep -i token` + exfiltration is
  trivial for a hijacked agent.
- The base spec's only secret control (R7) is **log/transcript scrubbing** — that defends the
  *logging* vector, not the *agent-exfiltration* vector. A hijacked agent reads the raw value and
  sends it anywhere; scrubbing never sees it.

This spec replaces "the container is the sandbox" with a **real intra-container privilege boundary**:
a trusted parent ("Advocate") holds all secrets and does all network I/O; the LLM loop runs in a
nested sandbox with **no secrets and no network**, reaching the world only through the Advocate over a
unix-domain socket. Security stops depending on `claude-sdk` behaving.

This is the conclusion of a design exploration (see §13 for the discarded alternatives and why).

---

## 1.1 Relationship to the PR #1 security review (jordanrburger, 2026-06-15)

This spec lands on the initial-implementation branch as the architecture for the **"brokering
approach"** Jordan offered to pair on in his PR #1 review. His review and this spec agree on the
diagnosis ("lethal trifecta by construction": secrets in the agent's env + untrusted input + open
exfiltration) and on the direction; this spec turns the direction into a concrete, kernel-enforced
shape.

| Jordan's proposal (PR #1 review) | This spec |
|---|---|
| Broker third-party tokens (GitHub/MCP) behind a local proxy/auth helper so the agent never sees the raw value | The **Advocate** is that broker (§4, §8), reached over a UDS |
| Run the tool subprocess with a minimal env — no `os.environ` passthrough | Cleared agent env + fix `plugin_manager`'s `{**os.environ, **env}` leak (§5.1, §14) |
| Constrain egress to needed endpoints | seccomp `AF_INET` deny — a hard egress kill, stronger than allow-listing (§5.1) |
| Tactical fixes: drop bare `Bash`; narrow env; scrub output tables; gate/remove `bypassPermissions` | Subsumed — this model makes shell/permission-mode **irrelevant to credential safety** (the agent has no secrets and no network). They remain good belt-and-suspenders and are tracked as immediate hardening in the plan. |

**One refinement to his review.** Jordan notes *"the Anthropic key must be in the container (SDK reads
`ANTHROPIC_API_KEY`), so 'no tokens' isn't achievable."* With `ANTHROPIC_BASE_URL` pointed at the
Advocate's UDS proxy, the **agent box can hold a dummy key** and the real `#anthropic_key` stays only
in the Advocate (§8) — so even the Anthropic credential need not be the real value in the agent's
environment.

**Key delta vs. the review's "more defensible shape":** Jordan's shape still relies on `claude-sdk`
discipline (tight `allowed_tools`, no shell when secrets are present). The in-container sandbox
(§5.1) makes the boundary **independent of `claude-sdk`** — even with a shell and
`bypassPermissions`, the agent has nothing to steal and nowhere to send it.

---

## 2. Threat model

**Primary threat: prompt injection via untrusted external payload.** Assume the LLM *will* be
hijacked. A GitHub issue body, PR comment, file in the repo, table cell, or MCP tool result contains
instructions that turn the agent against us.

**Assets to protect, in priority order:**

1. **Reusable secrets** — `#anthropic_key`, `#github_token`, per-MCP-server secrets, `KBC_TOKEN`.
   Theft = durable, replayable credential in attacker's hands.
2. **Data the credentials grant** — repo contents, project data reachable via MCP/Storage.
3. **Harmful authorized actions** — pushing malicious code, publishing, deleting.

**Out of scope (accepted residual):** a kernel/sandbox **escape** via a 0-day local privilege
escalation. That is a far higher bar than prompt injection; see §11. For customer-facing multi-tenant
exposure, §11 routes to a VM-grade backend instead.

**Non-negotiable product constraint:** setup must stay trivial and the agent must stay autonomous
(the component exists to run automatic agent teams over GitHub via conditional flows, chaining session
JSONL between agents). Security must live in the **architecture/platform side, paid once**, not as a
per-agent configuration tax. See §10.

---

## 3. Why in-container (and not a separate service)

A Keboola component is an **ephemeral batch container**: the Job Queue starts the image for one job,
it reads `/data/in` + `/data/config.json`, writes `/data/out`, and exits. It is not a persistent
service and has no inbound network.

Critically, **the platform decrypts `#`-secrets into `/data/config.json` and injects `KBC_TOKEN` into
the env *before the container starts*.** There is no platform mechanism to deliver a secret to a
sidecar but not to the component. Therefore:

- **"Token-out-of-the-box" is structurally impossible as a component** — the box is where Keboola puts
  the decrypted secret. (This is the same root cause that makes plan-v3 abandon the component model.)
- A genuine trust boundary is achievable only by either (a) building a separate platform service
  (= plan-v3, months of new infra), or (b) creating the boundary **inside the one container** with OS
  primitives.

This spec takes (b): collapse the two processes into one container. The secrets stay in the container
(in the trusted parent), but the **agent** is placed behind a kernel-enforced boundary with no
secrets and no network. Against the prompt-injection threat this yields the same property as plan-v3
("ZERO secrets / ZERO internet" for the agent) without new infra.

---

## 4. Architecture

```
┌─ ONE component container (ephemeral batch job) ─────────────────────────┐
│  ADVOCATE  — parent process, TRUSTED (Keboola-authored)                 │
│   • reads /data/config.json (#anthropic_key, #github_token, MCP #secrets)│
│   • holds KBC_TOKEN (env)                                                │
│   • HAS network; performs ALL outbound calls                            │
│   • runs: Anthropic proxy, MCP servers, GitHub/HTTP tool executor       │
│   • enforces the Contract gate (§7): deterministic + provenance +       │
│     bit-budget + restrict-only LLM advocate                             │
│   • does Storage I/O: reads /data/in, writes /data/out                   │
│        │  ONLY channel = AF_UNIX socket (UDS)                            │
│        ▼                                                                  │
│  ┌─ SANDBOXED AGENT — child, ZERO-TRUST ───────────────────────────┐    │
│  │   • claude-sdk = pure agent loop engine                          │    │
│  │   • unprivileged UID (dropped from parent)                       │    │
│  │   • seccomp: socket(AF_INET/AF_INET6) denied → NO network        │    │
│  │   • cannot read /data/config.json (uid + chmod 600)              │    │
│  │   • cleared env (no KBC_TOKEN, no secrets)                        │    │
│  │   • cannot ptrace / read parent memory (cross-uid)              │    │
│  │   • workspace = /tmp/agent + mounted /skills (read-only)         │    │
│  │   • reaches Anthropic / MCP / GitHub ONLY via the UDS → Advocate │    │
│  └──────────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────────┘
         outbound (only the Advocate) ──▶ Anthropic API / GitHub / MCP
```

Two processes, one container. The Advocate is the only thing with secrets and network; the agent is a
brain with no hands of its own — every hand is a request to the Advocate, which the Advocate may
refuse, scope, meter, and log.

---

## 5. Isolation mechanisms

### 5.1 Guaranteed floor (build on this — works regardless of the infra unknowns)

Confirmed against `kbc-stacks`: job pods have **no `securityContext`** (component runs as the image's
UID, potentially root, no enforced `runAsNonRoot`), **no `seccompProfile`/AppArmor/SELinux** set, and
**self-imposed seccomp (`prctl(PR_SET_NO_NEW_PRIVS)` + `seccomp(SECCOMP_SET_MODE_FILTER)`) always
works.** NetworkPolicy exists only for the sandbox namespace (Jupyter/RStudio), **not** for
job-queue-jobs — so platform egress control is not available to us and we must enforce it ourselves.

The floor uses only primitives that are guaranteed by those facts:

1. **Network kill — self-imposed seccomp.** Before exec'ing the agent, install a seccomp filter that
   returns `EACCES`/`EPERM` for `socket(AF_INET, …)` and `socket(AF_INET6, …)` while allowing
   `AF_UNIX`. The filter is inherited across fork/exec under `NO_NEW_PRIVS`, so every child the agent
   spawns (node, uvx, bash) is equally netless. DNS dies with it (also a socket) — the agent does not
   need it. **Deterministic, guaranteed.**
2. **Secret-file isolation — UID + perms.** The parent runs as root, reads `/data/config.json`, then
   drops the agent to an unprivileged UID. `config.json` is root-owned `chmod 600`; the agent's UID
   cannot read it. The agent is exec'd with a **cleared environment** (no `KBC_TOKEN`, no secrets) —
   only `ORCHESTRATOR_UDS`, workspace paths, and `ANTHROPIC_BASE_URL` (see §8).
3. **Memory isolation — cross-UID, no extra primitive needed.** An unprivileged child cannot
   `ptrace` a root parent nor read `/proc/<parent>/mem` (root-owned). Secrets held in the Advocate's
   memory are therefore unreachable even without a PID namespace.
4. **Single channel — AF_UNIX only.** A unix-domain socket bind-mounted/visible to the agent is its
   sole egress. The Advocate listens on it.

This floor alone delivers the two properties that matter against prompt injection: **agent has no
network and no secrets.**

### 5.2 Optional hardening (add if the infra unknowns allow — defense-in-depth, not required)

| Primitive | Adds | Gated on (infra repo, unknown) |
|---|---|---|
| `unshare(CLONE_NEWNET)` (net namespace, no interfaces) | belt-and-suspenders egress kill on top of seccomp | container runtime + `unprivileged_userns_clone` / running as root |
| `unshare(CLONE_NEWNS)` mount ns | agent sees only `/skills` + `/tmp`; not even a path to `/data` | same |
| `unshare(CLONE_NEWPID)` PID ns | agent cannot enumerate parent process / cmdline | same |
| `bubblewrap` / `nsjail` | packages the above cleanly | needs the syscalls above + image tooling approval |

These are layered on the floor; if the runtime blocks them, the floor still stands.

---

## 6. Component boot sequence

```
1.  Component starts as ROOT (parent = Advocate).
2.  Advocate reads /data/config.json + KBC_TOKEN; validates Configuration (existing model).
3.  Advocate runs Phase 0 (clean): derives + signs the Intent Contract (§7.1) from
    system_prompt + task + flow context + declared tools. NO untrusted data yet.
4.  Advocate starts, on its side (with secrets, with network):
       - Anthropic proxy listening on the UDS  (injects #anthropic_key)
       - configured MCP servers (with their #secrets)
       - GitHub / HTTP tool executor
       - the Contract gate (§7.2)
5.  Advocate prepares the agent jail:
       - chmod 600 root-owned /data/config.json; create /tmp/agent owned by AGENT_UID
       - mount/stage read-only /skills
       - build the cleared env: { ORCHESTRATOR_UDS, ANTHROPIC_BASE_URL=<uds>, workspace paths }
6.  Advocate spawns the AGENT:
       prctl(NO_NEW_PRIVS) → install seccomp (deny AF_INET/AF_INET6)
       → [optional] unshare(NET/NS/PID) → setuid(AGENT_UID) → exec claude-sdk loop
7.  Agent loop runs. Every model call / tool call / MCP call = an RPC over the UDS.
    The Advocate gates each (§7.2), injects the right scoped credential, executes, logs, returns.
8.  Agent writes its outputs to /tmp/agent. On completion it signals the Advocate over the UDS.
9.  Advocate (trusted) promotes results to /data/out (manifests, tables, JSONL transcript),
    records cost/metering, tears down the agent, exits.
```

Storage output promotion happens in the **parent** (step 9), not the agent — the agent never holds
`KBC_TOKEN` and never writes `/data/out` directly.

---

## 7. The Advocate contract layer

The UDS is the **only** path into the trusted parent, so the Advocate treats all UDS input as
untrusted: a narrow, strictly-validated protocol plus the gate below.

### 7.1 Phase 0 — signed Intent Contract (clean, pre-contamination)

Before any untrusted byte enters the agent, the Advocate derives a contract from **trusted inputs
only** (system prompt, task definition, flow context — which repo/step —, declared tools). The
contract is auto-derived (the user does not hand-write it; see §10), signed, and **frozen**:

```jsonc
{
  "scope":        { "repos": ["org/repo-X"], "writable_branches": ["agent/*"] },  // from flow
  "capabilities": ["gh.read","gh.write_branch","gh.open_pr","gh.comment","mcp.keboola.read"],
  "destinations": ["api.github.com/org/repo-X", "anthropic(via-proxy)", "<declared MCP endpoints>"],
  "egress_budget_bits": 4096,          // repo-derived data allowed OUT to non-scope destinations
  "irreversible_gate": ["gh.merge","deploy","delete"],   // default from branch protection
  "expiry": "this_invocation"
}
```

### 7.2 Phase 1 — per-action gate (cheap/deterministic first)

Every RPC from the agent runs the gate in order; the LLM step is last and **restrict-only**:

1. **Deterministic contract check** — capability ∈ contract? destination ∈ contract? scope ok?
   No → hard deny (no LLM, fast, provable).
2. **Provenance check** — did this request arrive right after ingesting untrusted content? After
   contamination the contract is **frozen** (cannot expand destinations/capabilities).
3. **Advocate LLM (restrict-only, secret-blind)** — only for egress-shaped/sensitive actions; sees
   the action + provenance summary (never secrets); may only *further* deny, never grant beyond the
   contract.
4. **Egress bit-budget** — if the action sends repo-derived data to a non-scope destination, debit;
   deny when exhausted.

Allowed → the Advocate injects the scoped credential, performs the call itself, logs
`{action, dest, status, bytes, tokens}`, returns the result. An `action_id` per RPC gives
idempotency against a dropped-response double-execute.

**Graduated cost:** a narrow dev agent (one repo, repo-scoped token) is fully served by layer 1 —
layers 3–4 never fire. The LLM advocate + bit-budget engage only for broad-scope agents (multi-repo,
sensitive MCP). Security scales with risk, not with every agent.

### 7.3 Phase 2 — session JSONL chaining (preserves the contamination clock)

The session JSONL is **secret-free by construction** (no secrets in the agent box), so it is safe to
pass to the next agent/team. But for the next agent the inherited JSONL is **untrusted input**: agent
B's contract is derived from B's own task (trusted) and the JSONL is loaded as context **after** B's
contract is signed. If A was contaminated, its JSONL is treated as tainted by B. Provenance propagates
along the chain; injection ingested by A cannot grant new authority to B.

---

## 8. Minimizing `claude-sdk` (security must not depend on it)

The agent has no network, so `claude-sdk` cannot call Anthropic/MCP/GitHub directly. All of it routes
through the Advocate:

- **Model calls:** set `ANTHROPIC_BASE_URL` to the UDS-backed proxy. `claude-sdk` believes it is
  calling Anthropic; the Advocate injects `#anthropic_key` and forwards. The agent never holds the key.
- **MCP:** servers are launched by the **Advocate** (with their `#secrets`); the agent calls them as
  RPCs over the UDS. No raw Bearer/`env` secret in the agent box.
- **GitHub / HTTP tools:** brokered by the Advocate with scoped tokens injected server-side.

Consequence: even if `claude-sdk`'s `permission_mode`/`allowed_tools`/`settings_json` are bypassed or
buggy, there is nothing in the agent box to steal and nowhere to send it. `claude-sdk` is reduced to a
loop engine. (This also makes the Advocate engine-agnostic — a non-Claude runner plugs in the same
way.)

---

## 9. What is provable vs heuristic (honesty)

| Layer | Strength |
|---|---|
| seccomp `AF_INET` deny (network kill) | **provable** (kernel-enforced, guaranteed per kbc-stacks) |
| UID + `chmod 600` + cleared env (secret-file/env) | **provable** |
| cross-UID no-ptrace (memory) | **provable** |
| deterministic contract check (cap/dest/scope) | **provable** |
| provenance freeze (no expansion post-contamination) | **provable** |
| egress bit-budget (channel capacity cap) | **provable** (information-theoretic) |
| Advocate LLM judge | **heuristic** — restrict-only garnish, never the basis |

If the LLM judge is fooled it still cannot exceed the frozen contract (layer 1) or the bit-budget
(layer 4). The security floor is deterministic + information-theoretic; the LLM is defense-in-depth.

---

## 10. Setup UX (must stay trivial)

What the user configures — unchanged from the base spec, no new security knobs:

```jsonc
{
  "name": "PR triage agent",
  "model": "claude-opus-4-8",
  "system_prompt": "Triage and fix issues in the repo, open a PR.",
  "skills": ["agent-skill:pr-triage"],
  "operates_on": "org/repo-X"     // contract scope is derived from this + flow, not hand-written
}
```

No Bearer tokens wired by hand, no `allowed_tools` maintenance, no hand-authored contract. The
Advocate derives scope/destinations/capabilities. Tier-2 overrides (narrow the contract, force
approval on an action) are optional, for the minority of high-stakes agents.

**Design rule: secure by architecture, permissive by configuration.** The agent is born fully
autonomous within a blast radius the platform bounds invisibly.

---

## 11. Residual risk & the V2 path

- The boundary is **kernel-enforced but intra-container**. Against prompt injection it is strong (no
  network, no secrets for the agent). Against a **kernel/sandbox escape** (0-day LPE) it does not
  hold — that compromises the whole container including the parent's secrets. Accepted for **internal
  dev agents**; it is a far higher bar than prompt injection.
- **Customer-facing / multi-tenant (V2):** route to a VM-grade backend. `kbc-stacks` exposes
  `runtimeBackendType: e2bSandbox` (E2B microVM) as the strongest existing isolation — if a job can
  opt into it, that gives a VM boundary **without building plan-v3**. This is the recommended V2
  direction (open question §12.2).

---

## 12. Open questions for the platform / infra team (non-blocking)

The floor (§5.1) needs none of these; they only decide whether we add §5.2 hardening or the V2 path.

1. **Namespace feasibility** — container runtime (runc/gVisor/Kata), `kernel.unprivileged_userns_clone`
   on job nodes, and any `PodSecurityAdmission` policy (baseline/restricted) on `job-queue-jobs`.
   (Evidence that components can run as root suggests PSA is **not** `restricted`, but confirm.)
2. **E2B backend opt-in** — can a job-queue job run with `runtimeBackendType: e2bSandbox`? This is the
   strongest existing isolation for the V2 / customer-facing case.
3. **Image tooling** — any objection to shipping `bubblewrap`/`nsjail` in the image (and which build)?
4. **Precedent** — does any existing component already do nested in-container sandboxing we should
   follow instead of reinventing?

---

## 13. Discarded alternatives (and why)

- **Log/transcript scrubbing only (base spec R7):** defends the logging vector, not agent
  exfiltration. Necessary but not sufficient.
- **"Vault": lock secrets during the LLM run, unlock after:** fails — the agent needs the secrets
  *during* the run (that is the threat window), and there is no privilege boundary between component
  and agent in one process. Time-gating ≠ privilege separation.
- **LLM judge / "captcha for LLMs" as the gate:** circular if intent is self-declared at request time
  (the hijacked agent controls both action and justification); no capability gap between peer LLMs.
  Salvaged only as the **restrict-only, provenance-fed, post-frozen-contract** layer (§7.2 step 3).
- **Capability-grammar / hologram (synthetic-data) / quorum:** strong for "pure planner" workloads but
  over-constrain an autonomous dev agent and raise setup friction — wrong trade for this product.
- **Separate Advocate service / full plan-v3 now:** the correct *platform* answer, but it abandons the
  component model and is months of new infra. This spec delivers the same anti-injection property
  in-component; plan-v3 / E2B backend remain the V2 upgrade.

---

## 14. Code impact (initial-implementation → this branch)

| Area | Change |
|---|---|
| `src/component.py` | Split `run()` into **Advocate parent** (secrets, Storage I/O, gate, teardown) and **agent spawn** (seccomp + UID-drop + exec). Stop building the agent env with secrets (`_build_env`); build a **cleared** env + `ANTHROPIC_BASE_URL`=UDS. |
| `src/claude_runner.py` | Point the SDK at the UDS proxy; drop direct secret injection into agent env. |
| `src/plugin_manager.py` | Stop `{**os.environ, **env}` subprocess inheritance (secret leak); plugin install moves to the Advocate side or a netless, secret-free path. |
| new `src/advocate/` | UDS server, Anthropic proxy, MCP/GitHub brokers, the contract gate (§7), sandbox launcher (seccomp/uid/optional-namespaces). |
| `Dockerfile` | (optional) add `bubblewrap`/`nsjail` for §5.2; ensure parent can run as root and drop UID. |
| tests | datadir/VCR unchanged for the happy path; add: seccomp-blocks-INET, agent-cannot-read-config, gate denies off-contract dest, provenance-freeze, bit-budget. |

---

*End of spec. Companion implementation plan: `docs/superpowers/plans/2026-06-15-advocate-broker.md`.*
