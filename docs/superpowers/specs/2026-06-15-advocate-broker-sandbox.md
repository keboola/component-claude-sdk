# Advocate Broker + In-Container Sandbox — Security Design Spec

> Type: security architecture (amends the base design spec)
> Component ID: keboola.app-claude-sdk
> Status: draft for review
> Date: 2026-06-15
> Branch: feat/advocate-broker (from initial-implementation)
> Amends: `docs/superpowers/specs/2026-06-14-claude-sdk-design.md` §2.4, §2.9, §6.5, R7
> Relationship to platform-level work: this is the **plan-v3 Two-Process model collapsed into a
> single Keboola component container**. It does not require new platform infrastructure.
>
> Revision 2026-06-15b (per PR #1 review): the **POC scope is the broker + in-container sandbox +
> deterministic scope/destination allowlist** — full stop. The earlier "egress bit-budget",
> "secret-blind LLM advocate", and runtime "provenance-freeze" were overclaimed (esp. calling the
> bit-budget *information-theoretic* — it is at best a heuristic size cap, and inapplicable to a
> coding agent whose legitimate output is large). They are demoted to §7.4 as **explicitly out-of-POC,
> unvalidated experiments** and are not relied on for any security property.
>
> Revision 2026-06-16c (PR #1 discussion): reframed around **what the broker actually protects**. The
> broker is a bounded, high-value mechanism, not a general "secrets are safe" guarantee — it cleanly
> covers exactly **three** credential paths (Anthropic, Keboola Storage, GitHub **PAT**). GitHub **App**
> install tokens are only protected if the Advocate owns the token exchange; everything else (arbitrary
> third-party APIs, other MCP secrets, user-pasted secrets) is **explicitly unprotected** in the POC.
> Added the two structural limits that bound it (per-service plumbing; credential-returning auth flows)
> and elevated **controlled egress** as the general answer for everything the broker can't cover —
> including an **opt-in, default-open egress allowlist** (§8.4) that bounds where the agent can send
> without the config-hell of a fail-closed contract. The single most useful framing of this whole spec
> now lives in **§8.1–8.4** — read that first.

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
│   • enforces the Contract gate (§7): deterministic scope/destination    │
│     allowlist (POC). Experimental extras: §7.4                          │
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
  "irreversible_gate": ["gh.merge","deploy","delete"],   // default from branch protection
  "expiry": "this_invocation"
}
```

### 7.2 Phase 1 — per-action gate (POC: deterministic only)

Every RPC from the agent is checked against the frozen contract by a **deterministic** rule — no LLM
in the path:

- **capability ∈ contract?** **destination ∈ contract?** **scope ok?** No → hard deny.
- This is exactly the "constrain egress to needed endpoints / don't expose what isn't declared"
  control from the PR #1 review — enforced server-side at the broker, where the agent has no say.

Allowed → the Advocate injects the scoped credential, performs the call itself, logs
`{action, dest, status}`, returns the result. An `action_id` per RPC gives idempotency against a
dropped-response double-execute.

For a narrow dev agent (one repo, repo-scoped token) this deterministic check is the whole gate. It is
fast, auditable, and the only part the POC's security relies on.

### 7.3 Phase 2 — session JSONL chaining

The session JSONL is **secret-free by construction** (no secrets in the agent box), so it is safe to
pass to the next agent/team — that is the load-bearing property. For the next agent the inherited JSONL
is **untrusted input**: agent B's contract is derived from B's own task (trusted), and the JSONL is
loaded as context **after** B's contract is signed, so a poisoned upstream transcript cannot widen B's
contract. (No claim beyond that — see §7.4 on why we are *not* leaning on runtime taint-tracking.)

### 7.4 Experimental extras — explicitly OUT of the POC, not relied on

These were in the first draft and are **demoted** per the PR #1 review. They are recorded as possible
future research, not security guarantees, and the POC ships without them:

- **Egress "bit-budget".** Calling it *information-theoretic* was an overclaim. For a coding agent the
  legitimate output (a PR full of code) is large and data-dependent, so a "bits of real data allowed
  out" cap has nothing meaningful to measure; at best it is a heuristic output-size limit, and covert
  channels (word choice, ordering) defeat the "information-theoretic" framing. **Dropped.**
- **Secret-blind LLM "advocate" judge.** Once the floor holds (no secrets, no network) plus the
  deterministic allowlist, an extra LLM judge is gold-plating: fuzzy, gameable, and it adds
  latency/cost/complexity for little marginal security. **Not in the POC**; revisit only if a concrete
  within-scope-abuse case shows the deterministic allowlist is insufficient.
- **Runtime "provenance-freeze".** With a **static** contract derived up front (scope = repo X, never
  expanded at runtime), there is nothing to "freeze" — the property falls out of the contract being
  immutable. The dynamic taint-tracking version is unvalidated and **not** part of the POC.

Bottom line: the POC's security = broker (token-out) + in-container sandbox (no net, no secrets) +
deterministic scope/destination allowlist. Everything above is future work, clearly labelled.

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

## 8.1 What the broker actually protects (POC scope — read this first)

The broker is **not** a general "secrets are safe" mechanism, and overselling it that way is what makes
it feel like a lot of machinery for little gain. Stated honestly it is a **cheap, bounded, high-value**
control: it cleanly protects a credential only when **three** properties all hold —

1. **platform-injected** — the agent never legitimately needs to *read* the value (it just needs the
   *effect* of a call made with it);
2. **pinnable upstream** — the secret targets one known host the Advocate can hard-code;
3. **static or Advocate-owned lifecycle** — the secret is never *returned* to the agent in a response
   (see §8.2.2).

Exactly three credentials in this component satisfy all three, so **the POC builds these three brokers
and nothing else.** For each, a hijacked agent — even with a shell and full internet — leaks at most a
dummy header plus data, never a reusable credential.

| Credential | Mechanism | Protected? | Caveat |
|---|---|---|---|
| Anthropic `#anthropic_key` | UDS proxy injects the key; the agent holds a **dummy** key; upstream pinned to `api.anthropic.com` | **Yes** — static, never in the box | the proxy MUST pin its own upstream and never forward to an agent-supplied host, or the real key leaks |
| Keboola `KBC_TOKEN` | parent does all Storage I/O (`/data/in`→`/data/out`); Keboola MCP brokered to the stack `connection.*` host | **Yes** — the agent does not need it by default | highest blast radius (whole project) → pair with a **least-privilege Storage token** |
| GitHub **PAT** | token injected server-side; upstream pinned to `api.github.com` | **Yes** — static | use a **fine-grained, repo-scoped** PAT |
| GitHub **App** install token | `POST /app/installations/{id}/access_tokens` **returns the token in the response body** | **Only if** the Advocate performs the exchange itself and never forwards the token | if the agent is allowed to trigger the exchange, the install token lands in its context → **unprotected** |
| Everything else — Salesforce & arbitrary third-party APIs, other MCP server secrets, **secrets a user pastes into the prompt** | — | **No** (POC) | destination-limited only by the opt-in egress allowlist (§8.3–8.4); never theft-proof |

### 8.2 Two limits that bound the broker (why it is only three paths)

1. **Per-service plumbing does not scale.** "Inject the secret safely" requires pinning the upstream
   *and* understanding the call shape — inherently per-service. A generic inject-by-host proxy stops
   *theft* but not *authority abuse* (a hijacked agent driving the legitimate token against
   attacker-chosen operations), and still needs a host→secret map per service. So brokering is viable
   only for a small, known set — not for "an agent that can call anything."
2. **Credential-returning auth flows break outbound injection.** The broker assumes secrets flow
   *outbound only*. Refresh / login / STS / `AssumeRole` / Salesforce-`login()` / GitHub-App-token
   endpoints **return a live credential in the response**. If the Advocate forwards that response, the
   new token lands in the agent's context — the broker protected the old secret and handed over the new
   one. The only fix that preserves "no secret in the box" is for the Advocate to **own the entire auth
   lifecycle** for that service (perform the refresh/login itself, keep the result, inject it on the
   agent's subsequent calls, never forward a raw token) — which is more per-service intelligence, i.e.
   limit 1 again.

### 8.3 The general answer for everything the broker can't cover: controlled egress

Credential-hiding (the broker) is the wrong primitive for arbitrary services, returned credentials, and
user-pasted secrets. The mechanism that covers **all** of them uniformly is **controlled egress**: let
the agent hold whatever secret it needs, but enforce — *outside* the agent, via a VM + egress allowlist
(the E2B/V2 path, §11) or a forced forward-proxy it cannot bypass — that it can reach only an
allowlisted set of hosts (`api.salesforce.com` and nothing else). This protects the **send**, not the
**holding**, so it does not care what the secret is or where it came from — including refreshed tokens
and secrets a user pasted into the prompt — at the cost of a coarse per-config host allowlist instead of
per-service broker code. For a *general* runner this is the honest boundary; the broker is the right
tool only for the narrow, known three above.

**Rejected — prompt-based protection.** Injecting a secret into the agent's context wrapped in a strong
"never reveal or transmit this" instruction is **not** a security boundary: a prompt instruction is
overridden by prompt injection by definition, and the agent can still leak via encoding or any allowed
tool. It is recorded here only to be explicitly ruled out. The orthogonal damage-cap that *is* always
worth applying — independent of mechanism — is **least-privilege, short-TTL / scoped tokens**: it caps
blast radius if a secret is exposed, but does not prevent the exposure.

### 8.4 Opt-in egress allowlist (config) — permissive by default, tightened on demand

Controlled egress (§8.3) need not be all-or-nothing or deferred to V2. The pragmatic, GA-friendly form
is a **config-driven host allowlist that defaults to "all"**:

```jsonc
{
  "egress_unrestricted": true,            // UI toggle: "Allow all internet access" (default ON)
  // when false, the UI reveals a creatable host list (Keboola options.dependencies):
  "egress_allowlist": ["github.com", "*.githubusercontent.com", "api.example.com"]
}
```

**UI shape:** a single **"Allow all internet access"** boolean, default **on** (= full internet).
Turning it **off** reveals a creatable host list (an `array` field shown conditionally via
`options.dependencies`). Off with an **empty** list = maximum lockdown: the agent reaches *only* the
brokered services and nothing else. The brokered three (Anthropic, GitHub, Keboola Storage) route
through the Advocate's own pinned upstreams and are therefore **exempt from this list** — a user who
restricts egress never has to add them by hand, and flipping the toggle off must never break model
calls or the agent's GitHub/Storage tools.

- **Default = open.** With no allowlist the agent has full internet — zero config tax, GA preserved.
  This is the deliberate inverse of the rejected frozen per-job contract (§7), which fails *closed* on
  anything the user forgot to declare. Here, forgetting to configure means *more* access, not a broken
  job. "Secure by architecture, permissive by configuration" realised as a **permissive default +
  opt-in coarse allowlist** rather than a mandatory derived contract.
- **Opt-in tightening.** A user who cares sets the list once in config. When the list is non-empty the
  Advocate engages the egress chokepoint — the agent's traffic is forced through the Advocate's proxy
  and **direct sockets are blocked** (otherwise the agent just ignores the proxy) — and **denies any
  destination not on the list, returning the denial as a normal tool error to the SDK** (e.g.
  `egress denied: <host> not in allowlist`). The model sees a failed call and adapts rather than
  hanging. The expensive enforcement only runs when the user actually asks for it.
- **Enforcement granularity (do not oversell):** **host/domain** matching is enforced cleanly from the
  TLS SNI / `CONNECT` target (`github.com`, `*.example.com`). **Path-level** rules
  (`example.com/branch/*`) are **not** enforceable for HTTPS without terminating TLS at the proxy with
  an agent-trusted CA (a MITM) — so the POC supports host/domain allowlisting and treats path-level as
  out of scope (or behind explicit TLS termination).
- **What it buys / does not:** it limits *where* the agent can send, closing the **data-exfil** hole the
  permissive default otherwise leaves (a hijacked agent can no longer POST repo/table data to an
  arbitrary host — including secrets a user pasted into the prompt). It does **not** stop *authority
  abuse* within an allowed host (a malicious push to an allowed repo), and it is independent of the
  broker — the two compose: the broker keeps the three credentials out of the agent; the allowlist
  bounds where everything else can go.

Shippable in-container in this POC (forced proxy + blocked direct egress when the list is set); the
VM/E2B form (§11) is the stronger V2 of the same idea.

---

## 9. What is provable vs heuristic (honesty)

**What the POC actually relies on:**

| Layer | Strength |
|---|---|
| seccomp `AF_INET` deny (network kill) | **provable** (kernel-enforced, guaranteed per kbc-stacks) |
| UID + `chmod 600` + cleared env (secret-file/env) | **provable** |
| cross-UID no-ptrace (memory) | **provable** |
| deterministic contract check (cap/dest/scope) | **provable** |

That is the whole security basis: a hijacked agent has no network and no secrets, and may only call
destinations the deterministic allowlist already permits. No probabilistic component is load-bearing.

The §7.4 items (bit-budget, LLM judge, runtime provenance) are **not** in this table on purpose — they
are future research, not guarantees, and nothing depends on them.

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
  Considered as a restrict-only add-on, then **cut from the POC** as gold-plating (§7.4) — the
  deterministic allowlist plus the no-secrets/no-net floor is what carries the security.
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
| tests | datadir/VCR unchanged for the happy path; add: seccomp-blocks-INET, agent-cannot-read-config, agent-env-has-no-secrets, gate denies off-contract dest. |

---

*End of spec. Companion implementation plan: `docs/superpowers/plans/2026-06-15-advocate-broker.md`.*
