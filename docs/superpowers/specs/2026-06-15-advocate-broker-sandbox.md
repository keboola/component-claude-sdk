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
>
> **Revision 2026-06-16d (on-platform probe findings — PIVOTS the security model):** On-platform probes
> (cf-dev jobs 47975338 / 47976834 / 47978513, branch images advocate-broker-21/22/23) DISPROVED the
> §5.1 floor as previously written. Key findings:
> (1) the job-queue runner forces **euid=1000, cap_eff=0** even with `USER root` in the Dockerfile —
> `setuid` fails, there is no root→agent UID-drop;
> (2) `unshare(CLONE_NEWNET)` and `iptables` (CAP_NET_ADMIN) are both unavailable — no in-container
> hard network egress control;
> (3) seccomp AF_INET deny breaks the loopback TCP connection the bundled `claude` CLI uses for
> `ANTHROPIC_BASE_URL` — a net-kill seccomp is not usable;
> (4) `ptrace_scope=1` confirmed — same-uid descendant CANNOT ptrace/read `/proc/<parent>/mem`
> of the Advocate (verified EPERM), but `/proc/<parent>/environ` IS readable (not Yama-gated);
> (5) E2B/VM backend deprioritised by the user.
>
> The design therefore pivots to **Broker V0: single-UID credential broker (non-root)** — see §4, §5,
> §6, §9, §11 below. The boundary is no longer a UID drop + network kill; it is cleared agent env +
> unlinked config + ptrace-scope memory protection + env-scrub + loopback-TCP credential injection.

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
a trusted parent ("Advocate") holds all secrets and injects them into every upstream call; the LLM
loop runs in a child process with **no secrets**, reaching the Advocate's credential brokers over
loopback TCP. (The original design used a unix-domain socket + seccomp network kill; on-platform
probes in 2026-06-16d revised this to loopback TCP + soft egress — see §4, §5, §12.6.)
Security stops depending on `claude-sdk` behaving for credential safety.

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
discipline (tight `allowed_tools`, no shell when secrets are present). The Broker V0 architecture
(§5.1) makes the boundary **independent of `claude-sdk`** for credential theft — even with a shell,
the agent holds no reusable credentials and cannot read the config file.

> **Note (2026-06-16d):** The table above reflects the original design intent. The on-platform probe
> (§12.6) changed the implementation: the channel is loopback TCP (not UDS), and seccomp AF_INET
> deny is NOT used in V0 (it breaks the loopback TCP transport). "Constrain egress" is therefore
> advisory/soft in V0 — the hard network kill requires a VM backend (§11).

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

**Broker V0 — single UID, loopback-TCP channel**

Both the Advocate (parent) and the agent (child) run as the platform-assigned euid 1000. There is no
UID-drop boundary. The boundary is: cleared agent env + unlinked config file + ptrace-scope memory
protection + loopback-TCP credential injection.

```
┌─ ONE component container (ephemeral batch job, euid=1000 throughout) ───┐
│  ADVOCATE  — parent process, TRUSTED (Keboola-authored)                 │
│   • reads /data/config.json (#anthropic_key, #github_token, MCP #secrets)│
│   • IMMEDIATELY unlinks /data/config.json after reading                 │
│   • holds KBC_TOKEN (passed via inherited fd after env-scrub re-exec)   │
│   • HAS network; performs ALL outbound calls                            │
│   • runs: Anthropic proxy, MCP proxy, GitHub/HTTP tool executor         │
│   • enforces the Contract gate (§7): deterministic scope/destination    │
│     allowlist (POC). Experimental extras: §7.4                          │
│   • does Storage I/O: reads /data/in, writes /data/out                   │
│        │  ONLY channel = loopback TCP 127.0.0.1:<port>                   │
│        ▼                                                                  │
│  ┌─ AGENT — child, ZERO-TRUST ─────────────────────────────────────┐    │
│  │   • claude-sdk = pure agent loop engine                          │    │
│  │   • same euid 1000 as parent (no UID-drop — platform prevents it)│    │
│  │   • cleared env: no KBC_TOKEN, no #secrets, only routing values  │    │
│  │     (ANTHROPIC_BASE_URL, MCP_PROXY_URL, workspace paths)         │    │
│  │   • cannot read /data/config.json (unlinked before agent spawned) │    │
│  │   • cannot ptrace / read parent memory (ptrace_scope=1, verified) │    │
│  │   • NOTE: /proc/<advocate>/environ IS readable — see §5 env-scrub │    │
│  │   • workspace = /tmp/agent + /skills (read-only)                  │    │
│  │   • reaches Anthropic / MCP / GitHub ONLY via loopback-TCP proxy  │    │
│  │   • full container network retained (no hard egress kill in V0)   │    │
│  └──────────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────────┘
         outbound (Advocate + agent) ──▶ Anthropic API / GitHub / MCP
         (agent bypassing proxy not prevented in V0 — soft boundary)
```

Two processes, one container, one UID. The Advocate holds all reusable credentials and injects them
server-side over loopback TCP. The agent holds only dummy values; it reaches upstreams only through
the proxy. The network is NOT hard-killed — see §9 for what is and is not provable.

---

## 5. Isolation mechanisms

### 5.1 Broker V0 boundary — what actually holds on the platform (revised 2026-06-16d)

The on-platform probes (§12.6) proved that the root+UID-drop+seccomp-net-kill model does NOT work
on kbc-stacks: the runner forces euid=1000 with zero capabilities regardless of the image's `USER`
directive, so setuid fails, unshare fails, and seccomp AF_INET deny breaks the loopback TCP the
`claude` CLI requires. The V0 boundary is therefore different from the §12.5 local-Docker result.

**What V0 relies on (confirmed on-platform):**

1. **Config-file isolation — unlink after read.** The Advocate reads `/data/config.json` (decrypted
   secrets) and immediately unlinks it. When the agent process starts, the file does not exist; the
   agent cannot read it regardless of permissions. The agent is spawned with a **cleared environment**
   (no `KBC_TOKEN`, no `#anthropic_key`, no `#github_token`, no MCP secrets) — only routing values
   (`ANTHROPIC_BASE_URL`, `MCP_PROXY_URL`, workspace paths, a dummy Anthropic key).
2. **Memory isolation — ptrace_scope=1 (verified on-platform).** With `ptrace_scope=1`, a descendant
   process at the same UID cannot `PTRACE_ATTACH` or read `/proc/<parent>/mem` of the Advocate
   (confirmed EPERM on probe jobs). Reusable secrets held in the Advocate's heap are therefore
   unreachable by the agent even without a UID boundary.
3. **KBC_TOKEN env-scrub (required — do not skip).** `/proc/<advocate>/environ` IS readable by the
   same-uid agent (read mode is not Yama-gated). If `KBC_TOKEN` remains in the Advocate's exec-time
   environment, the agent can read it. Mitigation: before spawning the agent, the Advocate re-execs
   itself with a scrubbed environment, passing `KBC_TOKEN` back via an inherited file descriptor (not
   env), so `/proc/<advocate>/environ` no longer exposes it. **Fallback**: if env-scrub is not yet
   implemented, pair with a least-privilege Storage token so that exposure of `KBC_TOKEN` has bounded
   blast radius. The env-scrub is a required step for V0; the fallback is an interim arrangement.
4. **Server-side credential injection — loopback TCP.** Reusable credentials (`#anthropic_key`,
   `#github_token`, MCP `#secrets`) are held only in the Advocate process. The agent's env holds a
   dummy Anthropic key and routing env vars. All upstream calls are made by the Advocate, which
   injects the real credential at the proxy layer — the agent never sees the value.
5. **Deterministic contract gate.** Every brokered action is checked against the frozen contract
   (§7.2) by a deterministic rule before execution. Capability, destination, and scope must all match;
   no LLM in the path.

**What V0 does NOT provide (be explicit):**

- **Hard network kill.** The agent retains the container's full network. There is no seccomp
  AF_INET deny (breaks the loopback TCP the CLI uses), no network namespace (unshare unavailable),
  no iptables (CAP_NET_ADMIN unavailable). The agent can open arbitrary TCP connections. Controlled
  egress (§8.4) is **advisory/soft** in V0 — the agent could bypass the proxy and reach arbitrary
  hosts directly. Data exfiltration to an arbitrary host is NOT prevented.
- **UID-based secret-file isolation.** There is no UID drop; file-system isolation is by unlinking
  the config file before the agent starts, not by a permission boundary between UIDs.
- **KBC_TOKEN environ protection without env-scrub.** If env-scrub is not implemented, the agent
  can read `KBC_TOKEN` from `/proc/<advocate>/environ`. Do not ship without env-scrub or a
  least-privilege token.
- **ptrace_scope guarantee.** V0 depends on the runtime maintaining `ptrace_scope >= 1`. If a
  future platform change sets it to 0, the memory isolation breaks. This is documented as a runtime
  dependency; escalate if it changes.

### 5.2 Optional hardening (V1+ — blocked on platform capabilities)

These are NOT available on the current kbc-stacks runtime (euid=1000, cap_eff=0) and are deferred:

| Primitive | Adds | Blocked by |
|---|---|---|
| `unshare(CLONE_NEWNET)` (net namespace) | hard egress kill | needs CAP_SYS_ADMIN or user-ns clone; unavailable |
| `unshare(CLONE_NEWNS)` mount ns | agent sees only workspace, not /data | same |
| `unshare(CLONE_NEWPID)` PID ns | agent cannot enumerate parent cmdline | same |
| `setuid` UID-drop | file-system secret isolation by UID | euid=1000 forced; setuid fails |
| seccomp AF_INET deny | hard network kill | breaks loopback TCP used by `claude` CLI |
| `bubblewrap` / `nsjail` | packages the above | requires the above primitives |
| VM/E2B backend | full hard boundary | deprioritised by user; theoretical V2 only |

The V0 boundary does not depend on any of these.

---

## 6. Component boot sequence

**V0 — single-UID non-root boot (revised 2026-06-16d)**

```
1.  Component starts at euid=1000 (platform-assigned, cannot be changed).
    Parent = Advocate. No root privileges. No capabilities.
2.  Advocate reads /data/config.json (#anthropic_key, #github_token, MCP #secrets, config).
    Validates Configuration (existing model). Reads KBC_TOKEN from env.
3.  Advocate UNLINKS /data/config.json immediately after reading.
    (The file no longer exists on the filesystem; the agent cannot read it.)
4.  Advocate runs env-scrub re-exec (REQUIRED):
    - Passes KBC_TOKEN via an inherited fd (e.g. pipe or memfd), not env.
    - Re-execs itself with a scrubbed environment so that /proc/<advocate>/environ
      no longer exposes KBC_TOKEN or #secrets.
    (If env-scrub is deferred: accept KBC_TOKEN environ exposure and pair with a
     least-privilege Storage token as an interim mitigation.)
5.  Advocate runs Phase 0 (clean): derives + signs the Intent Contract (§7.1) from
    system_prompt + task + flow context + declared tools. NO untrusted data yet.
6.  Advocate starts, on its side (with secrets, with network):
       - Anthropic proxy listening on loopback TCP 127.0.0.1:<port> (injects #anthropic_key)
       - MCP proxy on loopback (MCP_PROXY_URL) for configured MCP servers (with their #secrets)
       - GitHub / HTTP tool executor (loopback or shim — see §8)
       - the Contract gate (§7.2)
7.  Advocate prepares the agent env:
       - create /tmp/agent workspace
       - stage read-only /skills
       - build the cleared env: {
           ANTHROPIC_BASE_URL = http://127.0.0.1:<proxy-port>,
           ANTHROPIC_API_KEY  = dummy-key,
           MCP_PROXY_URL      = http://127.0.0.1:<mcp-port>,   // or loopback path
           workspace paths,
           NO KBC_TOKEN, NO #secrets
         }
8.  Advocate spawns the AGENT:
       exec claude-sdk loop  (euid=1000, same as Advocate)
    The agent's ptrace_scope=1 (verified on-platform) prevents it from ptrace'ing
    the Advocate or reading /proc/<advocate>/mem.
9.  Agent loop runs. Every model call / MCP call = a request over loopback TCP.
    The Advocate gates each (§7.2), injects the right credential, executes, logs, returns.
10. Agent writes its outputs to /tmp/agent. On completion it notifies the Advocate
    (over the loopback channel or a signal).
11. Advocate (trusted) promotes results to /data/out (manifests, tables, JSONL transcript),
    records cost/metering, tears down the agent, exits.
```

Storage output promotion happens in the **parent** (step 11), not the agent — the agent never holds
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

Bottom line: the POC's security = broker (token-out) + credential-cleared agent env + unlinked config
+ ptrace-scope memory protection + env-scrub + deterministic scope/destination allowlist. Everything
above is future work, clearly labelled.

---

## 8. Transport: agent↔Advocate channel is loopback TCP (revised 2026-06-16d)

**The channel is loopback TCP, not a Unix-domain socket.** The bundled `claude` CLI uses TCP for
`ANTHROPIC_BASE_URL`; `cc+unix://` and `ANTHROPIC_UNIX_SOCKET` (the claude-ssh channel) are not
the right mechanism here (the binary probe confirmed `ANTHROPIC_UNIX_SOCKET` is for the SSH remote
use-case, not the local broker case). The Advocate listens on `127.0.0.1:<ephemeral-port>`.

Transport shape per channel:

- **Anthropic (model calls):** `ANTHROPIC_BASE_URL=http://127.0.0.1:<proxy-port>`,
  `ANTHROPIC_API_KEY=dummy-key` in the agent env. The Advocate proxy strips the dummy key, injects
  `#anthropic_key`, and forwards to `api.anthropic.com`. **Upstream must be pinned** — the proxy
  must never forward to a host supplied by the agent.
- **MCP:** `MCP_PROXY_URL` (or `MCP_PROXY_PATH`) in the agent env points at the Advocate loopback
  port. The CLI routes MCP traffic there; the Advocate dispatches to real MCP servers with their
  `#secrets` injected server-side. **Status: mechanism confirmed via binary string probe; end-to-end
  wiring not yet validated — Phase 5 must confirm MCP tool calls reach the proxy.**
- **GitHub:** `HTTPS_PROXY=http://127.0.0.1:<proxy-port>` for the agent process, with a thin shim
  (or HTTP-over-loopback proxy) that the Advocate serves. Alternative: a `gh`-shim binary on the
  agent's `PATH` that translates `gh api …` calls to loopback RPCs at `/v1/github`. **Status:
  unconfirmed — Phase 5 must validate.**

Consequence: even if `claude-sdk`'s `permission_mode`/`allowed_tools`/`settings_json` are bypassed or
buggy, the agent holds no reusable credentials. `claude-sdk` is reduced to a loop engine. (This also
makes the Advocate engine-agnostic — a non-Claude runner plugs in the same way.)

**Important V0 caveat:** the loopback TCP channel is not the agent's only network path — the agent
has full container network access. A sophisticated hijacked agent could bypass the proxy and call
upstreams directly (with a leaked dummy key, not the real key). The broker protects credential
theft, not all data exfiltration. See §9 and §8.3–8.4.

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
| Anthropic `#anthropic_key` | loopback-TCP proxy injects the key; agent holds a **dummy** key; upstream pinned to `api.anthropic.com` | **Yes** — static, never in the box | proxy MUST pin its own upstream and never forward to an agent-supplied host, or the real key leaks |
| Keboola `KBC_TOKEN` | parent does all Storage I/O (`/data/in`→`/data/out`); Keboola MCP brokered; env-scrub prevents environ exposure | **Yes** — agent env cleared; env-scrub required | highest blast radius (whole project) → pair with a **least-privilege Storage token**; env-scrub is required, not optional |
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

**V0 caveat on egress allowlist enforcement:** "blocking direct sockets" in the opt-in tightening
path requires the same in-container mechanism as hard egress (seccomp or netns) — which the on-platform
probe showed is unavailable. In V0, the egress allowlist is therefore **advisory/soft**: the proxy
denies out-of-allowlist destinations when the agent routes through it, but the agent is not forced
to use the proxy and could open a raw socket to bypass it. Honest characterization: the allowlist
closes the egress hole against an agent that honours the proxy (the common case for an LLM agent
driven by tool calls) but does not hold against a deliberately evasive exploit. The VM/E2B path
(§11) is the only V0-to-hard upgrade path.

---

## 9. What is provable vs heuristic (honesty) — revised 2026-06-16d

**What Broker V0 actually relies on (provable, on-platform verified):**

| Layer | Mechanism | Strength | Condition |
|---|---|---|---|
| Reusable credential theft prevention | Config file unlinked before agent spawn; agent env cleared; loopback-TCP proxy injects real key server-side; agent holds only dummy values | **Provable** — agent process cannot access the credential file or the values by any standard path | proxy must pin its own upstream; env-scrub must be implemented for KBC_TOKEN |
| Memory isolation (Advocate heap secrets) | `ptrace_scope=1` on-platform (verified): descendant cannot PTRACE_ATTACH or read `/proc/<advocate>/mem` | **Provable** subject to runtime dependency | depends on runtime keeping `ptrace_scope >= 1`; escalate if changed |
| KBC_TOKEN environ isolation | Advocate env-scrub re-exec: KBC_TOKEN passed via inherited fd, stripped from exec-time env; `/proc/<advocate>/environ` no longer exposes it | **Provable** when env-scrub is implemented; **NOT provable** before it | env-scrub is a required step |
| Deterministic contract check | Every brokered action checked against frozen contract (cap / destination / scope); hard deny on mismatch; no LLM in the path | **Provable** — deterministic rule, auditable | gate correctness depends on contract derivation logic |

**What Broker V0 does NOT provide (be explicit — do not overclaim):**

| Gap | Why | Mitigation |
|---|---|---|
| Hard network egress kill | No seccomp AF_INET deny (breaks loopback TCP); no netns (unshare unavailable); no iptables (CAP_NET_ADMIN unavailable) | Egress allowlist is advisory/soft (§8.4); VM/E2B (§11) for hard kill |
| Forced proxy (agent cannot bypass) | Agent has full container network; can open direct TCP sockets | Same |
| KBC_TOKEN environ isolation (without env-scrub) | `/proc/<advocate>/environ` is readable by same-uid agent | Implement env-scrub (§5.1 point 3); interim: least-privilege Storage token |
| UID-based file isolation | No UID drop (euid=1000 forced by platform); config isolation is by unlink, not permissions | Unlink must happen before agent spawn; do not re-create config.json |
| ptrace_scope guarantee | V0 depends on runtime keeping ptrace_scope >= 1; a platform change to 0 breaks memory isolation | Document as runtime dependency; verify in Phase 0 probe |
| Arbitrary MCP / third-party secret protection | Only the three brokered credentials (Anthropic, KBC, GitHub) are never in the agent box; all other secrets are not covered | Egress allowlist (§8.4); doc limitation in §8.1 |

That is the full honest picture. The §7.4 items (bit-budget, LLM judge, runtime provenance) are
**not** relied on — they are future research and nothing in V0 depends on them.

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

## 11. Residual risk & the V2 path (revised 2026-06-16d)

**V0 is a soft in-container boundary, not a hard sandbox.** This is the honest assessment:

- **Credential theft (the three brokered paths):** strongly mitigated by V0. A hijacked agent
  cannot obtain `#anthropic_key`, `#github_token`, or (with env-scrub) `KBC_TOKEN` by any standard
  path. This is the primary protection V0 delivers.
- **Data exfiltration to arbitrary hosts:** NOT prevented. The agent has full container network.
  It can POST data to any host. The egress allowlist (§8.4) is soft/advisory — an evasive exploit
  bypasses it. For a trust-me agent (the current LLM-driven use case) the proxy-enforced allowlist
  is a meaningful barrier; for an active exploit it is not.
- **Authority abuse within allowed scope:** NOT prevented by V0. A hijacked agent with a valid
  brokered GitHub token can still do whatever the token permits within the contract scope. The
  deterministic gate bounds scope to what is declared; it does not prevent the agent from exhausting
  that scope maliciously (e.g., opening hundreds of PRs to declared repos).
- **KBC_TOKEN exposure without env-scrub:** NOT prevented until env-scrub is implemented. The
  agent can read `/proc/<advocate>/environ`. Do not ship without env-scrub or a least-privilege
  token.
- **ptrace_scope regression:** if the platform sets `ptrace_scope=0`, memory isolation breaks and
  the agent can read the Advocate's heap (containing decrypted secrets). V0 depends on
  `ptrace_scope >= 1`; verify on every platform/runtime change.
- **Kernel/sandbox escape (0-day LPE):** as before, not in scope — this compromises the whole
  container including the parent. Accepted for internal dev agents. It is a far higher bar than
  prompt injection.

**Hard boundary = VM (deprioritised by user).** The "ZERO secrets / ZERO internet" agent boundary
described in earlier spec drafts requires a VM-grade backend. `kbc-stacks` exposes
`runtimeBackendType: e2bSandbox` (E2B microVM) as the strongest existing isolation — the user has
deprioritised this path. It remains the only route from V0's soft boundary to a hard guarantee and
is recorded here as theoretical V2. Do not design current code around it.

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

### 12.5 Phase 0 findings — local Docker probe (2026-06-16)

> Environment: `python:3.14-slim`, run as root, `--read-only --tmpfs /tmp:exec`.
> Machine: `aarch64` (Docker Desktop / macOS host). Script: `scripts/sandbox_probe.py`.
> This is the LOCAL DOCKER result — the platform (kbc-stacks) run is separate.

**Floor verdict: HOLDS. Proceed to Phase 1.**

| Check | Result | Notes |
|---|---|---|
| `seccomp` blocks `socket(AF_INET)` | **PASS** | Child gets EACCES (errno 13) |
| `AF_UNIX` works under seccomp | **PASS** | Full socketpair roundtrip succeeds |
| seccomp inherited across `exec` | **PASS** | Grandchild python -c sees the block |
| root → `setuid(65534)` drop | **PASS** | euid=65534 confirmed in child |
| unpriv child cannot read root-owned 600 file | **PASS** | PermissionError as expected |

**§5.2 namespace probes (optional hardening):**

| Namespace | Available | Notes |
|---|---|---|
| `CLONE_NEWNET` | No (EPERM) | Docker Desktop default seccomp profile blocks `unshare` |
| `CLONE_NEWNS` | No (EPERM) | Same |
| `CLONE_NEWPID` | No (EPERM) | Same |

Namespace unavailability is expected for Docker Desktop on macOS; the production `kbc-stacks` runtime
(runc on Linux) is the relevant environment — probe must be rerun there (cf. open question 1 above).
The floor does **not** depend on namespaces.

**seccomp implementation choice:**

`pyseccomp` did not install with `libseccomp2` alone (no prebuilt wheel for Python 3.14 on aarch64);
`pip install pyseccomp` fails without a C toolchain. **Decision: use `ctypes`-based raw BPF builder**
(zero runtime dependency, no system packages, no image size impact). The probe's
`_build_bpf_program()` + `install_seccomp_filter()` are the prototype for Phase 1's
`src/advocate/sandbox.py::build_seccomp_filter()`.

Raw probe output:

```json
{
  "machine": "aarch64",
  "euid": 0,
  "seccomp_impl": "ctypes-bpf",
  "pyseccomp_probe": {"available": false, "conclusion": "no prebuilt wheel; ctypes-bpf is the right choice"},
  "seccomp_inet_blocked":     {"pass": true},
  "af_unix_ok":               {"pass": true},
  "seccomp_exec_inherited":   {"pass": true},
  "setuid_drop_ok":           {"pass": true},
  "unpriv_cannot_read_config":{"pass": true},
  "unshare": {"net": {"available": false}, "mount": {"available": false}, "pid": {"available": false}},
  "floor_holds": true,
  "floor_summary": "PASS"
}
```

> **NOTE (2026-06-16d):** The local Docker result above is **superseded by the on-platform findings
> in §12.6** for the actual design. The local probe ran as root (euid=0) and could setuid + use
> seccomp AF_INET deny without issue; kbc-stacks forces euid=1000 and the model pivots entirely.
> The ctypes-BPF seccomp implementation remains useful for future hardening if/when the platform
> allows it, but is NOT in the V0 critical path.

---

### 12.6 On-platform runtime findings (2026-06-16) — DESIGN-AUTHORITY RECORD

> Environment: kbc-stacks job-queue runner (production).
> Probe jobs: 47975338, 47976834, 47978513 (branch images advocate-broker-21/22/23).
> These findings OVERRIDE the local Docker probe in §12.5 and drive the V0 design.

**Summary: the root+UID-drop+seccomp-net-kill floor is INVALID on the real platform.**

| Finding | Result | Impact on design |
|---|---|---|
| Effective UID | **euid=1000**, cap_eff=0 (zero capabilities) — even with `USER root` in the Dockerfile; the runner overrides it | No root→agent UID-drop possible; setuid(AGENT_UID) fails |
| CAP_SYS_ADMIN / CAP_NET_ADMIN | **Absent** (cap_eff=0) | `unshare(CLONE_NEWNET)` fails; `iptables` unavailable |
| `unshare(CLONE_NEWNET)` | **EPERM** | No network namespace; no per-UID egress |
| seccomp AF_INET deny on agent | **NOT USABLE** — the bundled `claude` CLI uses loopback TCP for `ANTHROPIC_BASE_URL`; blocking AF_INET kills model calls | Cannot use seccomp net-kill as designed; transport pivots to loopback TCP |
| `ptrace_scope` | **= 1** (confirmed) — same-uid descendant CANNOT `PTRACE_ATTACH` or read `/proc/<parent>/mem` (EPERM verified) | Memory isolation holds without UID boundary; this is V0's memory protection |
| `/proc/<parent>/environ` | **Readable** by same-uid agent (not Yama-gated) | KBC_TOKEN in Advocate exec-time env is exposed → env-scrub required |
| Keboola component-custom-python Dockerfile | Contains comment: "Keboola running containers with -u 1000:1000" | Confirms euid=1000 is a platform-wide policy, not a transient config |

**Conclusions recorded for Phase 5 implementer:**

1. Drop `setuid`, `prctl(NO_NEW_PRIVS)`, seccomp AF_INET deny, unshare from the V0 implementation.
   They either fail or break the transport. The `sandbox.py` module built in Phase 1 needs to be
   simplified accordingly (see plan §15, task #15).
2. The Advocate and agent run as the same euid=1000. The only boundary is: unlink config.json +
   clear agent env + env-scrub the Advocate + loopback-TCP credential injection + ptrace_scope=1
   memory protection.
3. Implement env-scrub (re-exec Advocate with scrubbed env, KBC_TOKEN via inherited fd) before
   spawning the agent. This is a security requirement in V0, not optional.
4. The egress allowlist (§8.4) is soft in V0. Be honest about this in any external communication.
5. The loopback TCP transport is confirmed for Anthropic (ANTHROPIC_BASE_URL); MCP (MCP_PROXY_URL)
   and GitHub (shim or HTTPS_PROXY) are the mechanism, but end-to-end wiring must be validated in
   Phase 5.

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

## 14. Code impact (initial-implementation → this branch) — revised 2026-06-16d

| Area | Change |
|---|---|
| `src/component.py` | Split `run()` into **Advocate parent** (reads config, unlinks config.json, env-scrub re-exec, starts loopback-TCP proxy/brokers, spawns agent with cleared env, promotes outputs to /data/out) and **agent exec** (no setuid, no seccomp, just exec with cleared env). |
| `src/claude_runner.py` | Point the SDK at the loopback-TCP proxy (`ANTHROPIC_BASE_URL=http://127.0.0.1:<port>`, `ANTHROPIC_API_KEY=dummy`); drop direct secret injection into agent env. |
| `src/plugin_manager.py` | Stop `{**os.environ, **env}` subprocess inheritance (secret leak); plugin install moves to the Advocate side (before agent spawn, with secrets still available). |
| new `src/advocate/` | Loopback-TCP server, Anthropic proxy (injects `#anthropic_key`, pins upstream), MCP proxy (injects MCP `#secrets`), GitHub broker/shim (injects `#github_token`), contract gate (§7), env-scrub re-exec helper. **Drop:** UDS server, seccomp/UID sandbox launcher — replaced by loopback-TCP + cleared env. |
| `src/advocate/sandbox.py` (Phase 1 artifact) | Simplify or remove the seccomp/setuid launcher: those mechanisms do not work on the platform (§12.6). Keep the module as a placeholder for future V1+ hardening but do not invoke it in the V0 critical path. |
| `Dockerfile` | No setuid/UID-drop setup needed; no bubblewrap/nsjail. Ensure the Advocate reads config.json and unlinks it before agent spawn. |
| tests | datadir/VCR unchanged for the happy path; add: config-file-unlinked-before-agent-start, agent-env-has-no-secrets (no KBC_TOKEN, no #keys), advocate-environ-does-not-expose-kbc-token (env-scrub), proxy-injects-real-key-not-dummy, gate-denies-off-contract-dest. **Remove:** seccomp-blocks-INET (mechanism not used in V0), agent-cannot-read-config-by-uid (unlink is the mechanism now). |

---

*End of spec. Companion implementation plan: `docs/superpowers/plans/2026-06-15-advocate-broker.md`.*
