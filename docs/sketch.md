# Sketch — worker-model rebuild around a local routing proxy

**Status**: brainstorm record, 2026-08-12. This is the seed document for a future
grilling/ADR/PRD/plan cycle. It is NOT a plan and NOT a decision-of-record; where it
contradicts this repo's ADRs, that is intentional — the rebuild happens in a **new repo**
and this repo gets archived once the successor ships.

**Placeholder**: the new plugin's name is undecided; `<plugin>` is used throughout.
Candidate: `modelmux`.

---

## 1. What this is

A fresh, zero-context rebuild of the chinamax worker-model plugin. The old architecture
(a Haiku **Bridge Agent** driving a detached bash **Runtime** that hand-rolls an agentic
loop over the `anthropic` SDK) is replaced by:

1. **Native subagents as workers** — on Claude Code AND on Codex. A worker is a real,
   first-class subagent of the host CLI: identical message/tool-call format, spawnable
   by the host's own agent mechanism, @-addressable, steerable by direct message,
   background-capable, transcript-persisted, resumable.
2. **A local reverse proxy** (the only new service) that routes each API request by its
   **model string** to the right provider — Anthropic traffic passes through
   byte-for-byte; worker-model traffic is re-targeted (and, where needed, dialect-
   translated) to the provider's endpoint.
3. **One profile registry** as the single source of truth feeding: proxy routing,
   Claude agent generation, Codex agent generation, key scaffolding, and doctor checks.

The entire old Runtime disappears: no job store, no steer queue, no resume verb, no
confinement layer, no session-reap hooks, no Bridge contract, no relay long-poll. The
host CLI's native task system provides all of it.

### Why this is possible now (evidence, verified 2026-08-12)

- **Claude Code has no per-agent endpoint override** — verified against the CLI 2.1.228
  binary: the agent frontmatter schema is strict (`name, description, model, tools,
  disallowedTools, color, effort, permissionMode, mcpServers, hooks, maxTurns, skills,
  initialPrompt, memory, background, isolation, observer, observerMessage,
  observeSubagents`) with no env/baseURL/apiKey field, and `ANTHROPIC_BASE_URL` /
  `ANTHROPIC_AUTH_TOKEN` / `apiKeyHelper` are process-wide. Subagents run in-process.
- **But frontmatter `model` is a free-form string** ("a full model ID … accepts the same
  values as the `--model` flag" — code.claude.com/docs/en/sub-agents), passed to the
  wire verbatim. The Agent *tool's* per-spawn `model` param is enum-locked
  (`sonnet|opus|haiku|fable`), so the frontmatter is the ONLY per-agent model channel.
- **The gateway protocol docs bless model-string routing** through a single
  `ANTHROPIC_BASE_URL` ("pass `anthropic-*` request headers and request body fields
  through unchanged" — code.claude.com/docs/en/llm-gateway-protocol).
- **Subagent model resolution order**: `CLAUDE_CODE_SUBAGENT_MODEL` env → per-invocation
  `model` param → frontmatter `model` → inherit. Therefore that env var must stay UNSET
  and /task must never pass a model override on the Agent tool call.
- **Codex CLI native subagents + per-role provider routing — LIVE-VERIFIED on 0.147.0
  (2026-08-12, §12)**: role TOMLs under `~/.codex/agents/` are strict-schema and accept
  `model_provider` + `model` (plus a flattened config overlay incl.
  `model_context_window`, `tools`, `sandbox_mode`); source-confirmed first-class
  ("sticky unless the role sets it", role.rs:109) and proven end-to-end (a child pinned
  to a broken provider died on that provider's URL while its parent ran DeepSeek;
  per-child `session_meta` records the resolved provider). Spawning is a model tool
  (`multi_agent_v1`: spawn_agent / wait / send_input / close_agent / resume_agent),
  gated by prompt language that must explicitly authorize delegation. `wire_api` is
  **`"responses"`-only** — `"chat"` hard-fails config load (removed ~2026-02,
  openai/codex discussion #7782). No Bridge needed.
- **Native per-agent provider routing in Claude Code is a filed, unshipped feature
  request** (openai-compat routing per agent — github.com/anthropics/claude-code
  #38698). The proxy is the correct interim; revisit each changelog.

---

## 2. Decision log (grilled 2026-08-12)

| # | Decision | Choice |
|---|---|---|
| D1 | Repo | **New repo** (this one archived after cutover) |
| D2 | Plugin name | **New name**, TBD; `<plugin>` placeholder, candidate `modelmux` |
| D3 | Runtime fate | **Drop the entire Bridge/Runtime layer**; native subagents only. Hard requirement: **workers must be resumable like a normal Claude session** (§7) |
| D4 | Spawn granularity | **Per-profile**; each profile lists multiple supported model strings with one `default_model`. Agent files are thin, doctor-GENERATED artifacts (one per profile × listed model), never hand-maintained |
| D5 | Registry | **profiles.json v2** (§5): shipped defaults + user overlay; keys stay in `~/.claude/model-keys.env`; proxy does per-profile `request_extras` merge + `scrub`; **thinking normalization per profile is first-class** |
| D6 | Env flip scope | **User-level** `~/.claude/settings.json` env block (Claude side). Codex side needs no global flip |
| D7 | Supervision | **Self-healing on all three OSes**: systemd `--user` `Restart=always` (Linux), launchd `KeepAlive` (macOS), **real Windows Service** with recovery=restart (admin + wrapper accepted). SessionStart hook is warn-only |
| D8 | Proxy runtime | **Python + aiohttp** in a **conda env** (reuse the proven miniconda doctor machinery; new env name `<plugin>`) |
| D9 | Commands | **Separate `/setup` (mutating, consented) and `/doctor` (pure diagnosis)**. Live paid probes run **in setup only, opt-in at the consent pause** |
| D10 | /task surface | As specified in §6: explicit profile (no default), model from the profile's list (default = `default_model`), named background workers, @-addressable |
| D11 | Worker tools | Standard toolset **including the Agent tool** (workers may spawn Explore subagents). **No read-only/confinement story at all** — native permissions (`permissionMode`) only |
| D12 | Dialects | Profiles carry `dialect: anthropic | openai`. **In-house translation module** (no LiteLLM) for openai-dialect providers |
| D13 | Codex | **Full symmetric support, bridgeless** (supersedes the original "drop codex" — what's dropped is the Bridge/adapters/Runtime): native Codex subagents per profile, traffic through the same local proxy, Codex main loop untouched. **Ship a proper Codex plugin again** (dual-manifest discipline carried over from the old repo) |
| D14 | Defaults bundle | Loopback-only proxy on a fixed default port (8402), no proxy auth; routes `/v1/messages` + `count_tokens` by model, passthrough otherwise; JSONL request log with usage; model strings verbatim incl. `[1m]` suffixes; new GitHub repo under kguo93, v0.1.0 |
| D15 | Codex egress (grilled 2026-08-12) | **Always through the proxy** (`base_url = 127.0.0.1:8402/openai/<profile>`); the v0.1 Codex path is a pure Responses relay — no translation module |
| D16 | Per-host model IDs (2026-08-12) | **Separate per-host lists**: `codex_models[]` + `codex_default_model` beside the Anthropic-surface `models[]`; Responses-surface IDs live-smoked per profile (§12); V5 dissolves |
| D17 | Worker auth (2026-08-12) | Codex bears keys via `env_key` → proxy forwards them on the Responses ingress; Claude side: proxy reads `model-keys.env` (mtime-cached) and swaps the Anthropic OAuth for the provider key on matched branches only |
| D18 | Host symmetry (2026-08-12) | Claude and Codex ship symmetric skills/commands/hooks; asymmetries allowed only as host-specific workarounds inside a symmetric surface |
| D19 | Kimi / translation (2026-08-12) | **No translator in v0.1** — relay + tool-scrub only; kimi's Codex path marked unsupported pending Moonshot enabling `/responses` (403 account gate); doctor surfaces + re-probes |
| D20 | Worker relay fidelity (2026-08-12) | **Contracts + hooks, bridge-style**: hook-injected worker contract forces the child to end with an explicit result report and the parent to relay it **verbatim**; **no results skill** |

---

## 3. Architecture

```
   CLAUDE CODE (one process, one ANTHROPIC_BASE_URL for everything)
   ├─ main loop                    model: claude-opus-5        ─┐
   ├─ native Claude subagents      model: sonnet/opus/…        ─┤ Anthropic ingress
   └─ <plugin> workers (generated) model: deepseek-chat, glm-… ─┘      │
                                                                       ▼
   CODEX (main loop untouched, native → OpenAI)          ┌──────────────────────────┐
   └─ <plugin> workers (generated TOML agents)           │  local proxy  127.0.0.1  │
      model_providers.<profile>.base_url ───────────────▶│  :8402 (aiohttp, conda)  │
                              OpenAI ingress             │  systemd/launchd/WinSvc  │
                                                         └──────┬─────────┬─────────┘
                             peek body.model / ingress path     │         │
              ┌─────────────────────────────┬───────────────────┘         │
              ▼                             ▼                             ▼
   no registry match              match, dialect=anthropic       match, dialect=openai
   BYTE-FOR-BYTE passthrough      relay: host+auth swap,         TRANSLATE: Anthropic⇄
   headers untouched (OAuth,      extras merge, scrub,           OpenAI chat (tools, SSE,
   anthropic-beta survive)        thinking normalize             usage), then relay
              ▼                             ▼                             ▼
   api.anthropic.com              api.deepseek.com/anthropic     provider OpenAI-compat
                                  api.z.ai/api/anthropic  etc.   endpoints  etc.
```

Key properties:

- **The model string is the routing key** (there is no other per-agent signal on the
  wire). Registry `match` globs (`deepseek-*`, `kimi-*`, `glm-*`, …) make routing
  generic across providers — requirement: any model behind an Anthropic-compatible OR
  OpenAI-compatible endpoint, never provider-specific code paths.
- **Default route is a dumb pipe.** The Anthropic branch never re-serializes bodies or
  touches auth/beta headers, so subscription OAuth, prompt caching, and future request
  fields survive untouched. Claude main + native Claude subagents remain *normal*.
- **Only matched branches rewrite.** Auth swap, `request_extras`, `scrub`, thinking
  normalization, and dialect translation happen solely on worker-bound requests.
- **Stateless per-request routing.** No session tracking; a worker's whole agentic loop
  lands on its provider because every one of its requests carries its model string.
- **The Codex branch is a Responses relay** (D15/D19). Codex speaks
  `wire_api="responses"` only; 4 of 5 seed providers serve `/responses` natively, so
  the v0.1 Codex path never translates — it forwards, strips per-profile rejected tool
  types (`tool_scrub`), and logs usage.

---

## 4. The proxy service

- **Implementation**: single aiohttp app, Python 3.12, conda env `<plugin>`
  (`conda create -n <plugin> python=3.12` + `pip install aiohttp`; miniconda is a gated
  prerequisite exactly like the old doctor's Phase A).
- **Bind**: `127.0.0.1:8402` (default; configurable in registry header). Loopback only.
  No proxy-level auth (loopback trust).
- **Ingresses**:
  - **Anthropic ingress** (`/v1/…`): what `ANTHROPIC_BASE_URL` points at. Routes
    `POST /v1/messages` and `POST /v1/messages/count_tokens` by `body.model` against
    registry `match` globs (exact `models[]` entries match first, then globs). Every
    other path (models list, telemetry, anything future) passes through to
    `api.anthropic.com` unmodified.
  - **Responses ingress** (`/openai/<profile>/…`): what each generated Codex
    `model_providers.<plugin>-<profile>.base_url` points at. Codex is
    `wire_api="responses"`-only, so this ingress receives OpenAI **Responses** dialect
    (Codex POSTs `<base_url>/responses` verbatim). Profile identity comes from the
    path — no body peek. v0.1 egress is a **pure relay** to the profile's
    `codex_base_url` (D15/D19; no translation module on this path), forwarding the
    request-borne `env_key` bearer (D17). Per-profile `tool_scrub` drops tool types
    the provider's gateway rejects (mimo 400s on Codex's unsuppressible `web_search`
    tool — stripped from the request, the model never sees it, so it never calls it).
    Endpoint probes must validate the response BODY shape, not HTTP status (z.ai
    `/api/openai/v1` returns 200 with a failure body, §12).
- **Per-profile request shaping** (worker branches only):
  - `scrub`: drop fields the provider rejects/ignores (`cache_control` blocks,
    `anthropic-beta` headers, `output_config`, host-specific fields).
  - `request_extras`: merged LAST with **replace-on-conflict** semantics.
  - **Thinking normalization** (important, per grilling): Claude Code emits
    Claude-flavored `thinking`/`output_config`; each provider has its own dialect
    (current registry evidence: glm `thinking:{type:enabled}`, minimax
    `thinking:{type:adaptive}`, deepseek `extra_body.reasoning.effort`, mimo/kimi
    `extra_body.reasoning_effort`). The proxy strips the Claude form and substitutes
    the profile's `thinking` policy; profiles may carry per-model overrides (e.g.
    reasoner vs chat variants of one provider).
- **`count_tokens`**: routed like messages; if a provider lacks it, the proxy
  synthesizes an estimate response rather than erroring the host.
- **Observability**: JSONL request log — `ts, ingress, model, profile, upstream,
  status, latency, usage {input, output, cache_read?}` — because the host CLI's own
  cost telemetry is wrong/blind for worker traffic. Doctor surfaces log location.
- **Supervision (self-healing everywhere)**:
  - Linux: systemd user unit, `Restart=always`, `WantedBy=default.target`.
  - macOS: launchd LaunchAgent, `KeepAlive=true`.
  - Windows: real Windows Service (WinSW or NSSM wrapper around the conda python;
    admin install accepted) with failure-recovery=restart.
  - All installed/updated by `/setup` with consent; `/doctor` verifies unit presence,
    enablement, and a live port.

## 5. Profile registry — profiles.json v2

Single source of truth for proxy routing, agent generation (both hosts), key
scaffolding, and doctor checks. Shipped defaults inside the plugin; user overlay at
`~/.claude/<plugin>/profiles.json` (same overlay semantics as today). API keys live in
`~/.claude/model-keys.env` — O_EXCL-scaffolded comments-only template, values never
printed by any command (names + PRESENT/MISSING only).

```jsonc
{
  "port": 8402,
  "profiles": [
    {
      "name": "deepseek",
      "dialect": "anthropic",                    // or "openai"
      "base_url": "https://api.deepseek.com/anthropic",
      "codex_base_url": "https://api.deepseek.com/v1",   // Responses-surface egress; NOT derivable from base_url (glm: chat=api/paas/v4, responses=api/v1)
      "codex_models": ["deepseek-v4-flash"],             // per-host IDs (D16); v4-pro Codex-gated server-side as of 2026-08-12
      "codex_default_model": "deepseek-v4-flash",
      "tool_scrub": [],                                  // Responses-ingress tool types to strip (mimo: ["web_search"])
      "api_key_env": "DEEPSEEK_API_KEY",
      "match": ["deepseek-*"],                   // generic prefix routing (req 5)
      "models": ["deepseek-v4-pro[1m]", "deepseek-v4-flash"],
      "default_model": "deepseek-v4-pro[1m]",
      "thinking": {"extra_body": {"reasoning": {"effort": "max"}}},
      "scrub": ["cache_control", "output_config"],
      "request_extras": {},
      "tools": null                              // null = standard set incl. Agent
    }
    // … mimo, glm, minimax, kimi seeded from the current registry
  ]
}
```

Notes:
- Seed with the five current providers (deepseek, mimo, glm, minimax, kimi), carrying
  over today's `request_extras` as the initial `thinking` policies.
- `models[]` drives agent generation; `match` globs catch anything else with the
  provider's prefix (routing works even for unlisted models; spawning does not).
- A second model of a provider needing different shaping = per-model override map
  inside the profile, not a new provider entry.
- `dialect: openai` profiles have no `base_url` (Anthropic) — the Claude-side branch
  goes through the §8 translation module to the profile's OpenAI-compatible endpoint
  (unaffected by the Codex-path decisions).
- Codex-side status per the live matrix (2026-08-12, §12): deepseek/glm/minimax
  relay-ready (`glm-5.2` @ `api.z.ai/api/v1`, `MiniMax-M3` @ `api.minimax.io/v1`);
  mimo blocked only by Codex's unsuppressible `web_search` tool (fix = `tool_scrub`);
  kimi `/responses` account-gated 403 at Moonshot — its profile ships without
  `codex_models[]`, so no Codex agents generate for it until unlocked (D19).

## 6. Host surfaces

### Claude Code plugin

- **Generated agents** (by `/setup`, regenerated on registry change; flagged drift in
  `/doctor`): one `.md` per profile × listed model. Naming: default model gets the bare
  profile name (`deepseek`), alternates get `<profile>--<model-slug>`
  (`deepseek--v4-flash`). ~10 lines each: name, description ("<plugin> worker on
  <provider>/<model>"), `model: <string>` (the routing key), tools (standard set
  **including Agent** so workers can spawn Explore subagents; per-profile `tools`
  override), and a lean worker system prompt.
- **`/​<plugin>:task`**: `profile=<name> [model=<listed>] [name=<worker>] <prompt…>`.
  No default profile. Unlisted model errors with the valid list. Spawns the matching
  generated agent as a **named background subagent** (name default `<profile>-<n>`),
  which is the @-address. Steering/chat = @name / SendMessage; completion = native
  task notification. NEVER passes a model override on the Agent tool call (enum +
  resolution-order constraint).
- **`/​<plugin>:setup`** (mutating, consent-gated, diagnose → plan → approve → apply →
  re-diagnose → report): miniconda gate; conda env + aiohttp; write proxy file; install
  + start the OS service; write user-level `ANTHROPIC_BASE_URL` env block; scaffold
  model-keys.env; generate Claude agents; generate Codex artifacts (below) when
  `~/.codex` exists; **opt-in live probes at the consent pause** (one minimal
  end-to-end /v1/messages per profile through the proxy — operator chooses to spend or
  skip). Teardown mode removes the env flip + service.
- **`/​<plugin>:doctor`** (pure diagnosis, free/local only): registry parse + overlay
  merge; key presence; conda env + import check under the env python; service
  installed/enabled/running; port listening; env flip present; generated agents in
  sync with registry (both hosts); versions. Never spends tokens, never mutates.
- **`/​<plugin>:profiles`**: list resolved profiles, models, dialects, key env-var
  names + PRESENT/MISSING.
- **SessionStart hook**: warn-only — if the env flip is present but the proxy port is
  dead, emit a systemMessage pointing at `/doctor` (supervision itself is the OS's job).

### Codex plugin (restored, bridgeless)

- Ships as a proper Codex plugin (`.codex-plugin/` + Codex marketplace entry), reviving
  the old repo's dual-manifest sync discipline — but contains **no Bridge, no Runtime,
  no adapters**: setup/doctor/task surfaces + generated-config management, kept
  symmetric with the Claude plugin per D18.
- **Generated Codex artifacts** (TOML-preserving edits, consented; all verified against
  0.147.0, §12):
  - `[model_providers.<plugin>-<profile>]` — **user-level `~/.codex/config.toml`
    only**: `model_provider`/`model_providers` sit on Codex's project-local config
    denylist (source-confirmed; silently stripped with only a startup warning).
    `base_url = "http://127.0.0.1:8402/openai/<profile>"`, `env_key = <api_key_env>`
    (D17), `wire_api = "responses"` (the only accepted value; V3 resolved). Never
    reuse the reserved IDs `openai`/`ollama`/`lmstudio`; doctor validates with
    `codex exec --strict-config`.
  - `~/.codex/agents/<profile>[--<model-slug>].toml` — one role per profile ×
    codex-listed model: `model = <codex model ID>`, `model_provider =
    "<plugin>-<profile>"` (source-confirmed sticky-override semantics), pinned
    `model_context_window` (kills the "fallback metadata" guesswork for non-OpenAI
    models), lean worker instructions. Role names: ASCII letters/digits/space/-/_
    only. No `[features]`/`[agents]` flips needed — `multi_agent` is stable and
    default-on; roles auto-discover from `$CODEX_HOME/agents/*.toml`.
- **Dispatch**: the task skill (symmetric with Claude's, D18) instructs the parent to
  spawn the role via the `multi_agent_v1` tools and MUST carry explicit "you are
  authorized and required to delegate" language — spawn_agent's schema actively
  discourages unprompted delegation. Headless dispatch closes stdin (`< /dev/null`);
  `codex exec` blocks on a pipeline's open stdin.
- **Relay fidelity (D20)**: the operator can never address a child directly — Codex
  rejects direct input to v2 subagents at the app-server layer (PR #27173) — and the
  default transcript shows only opaque `collab:` lines, leaving the parent free to
  paraphrase. So the worker contract rides hooks, bridge-style: the hook-injected
  contract orders the child to end with an explicit result report and the parent to
  relay that report **verbatim**. No results skill. Watching live = TUI `/agents`
  thread view (navigational; verbatim via raw scrollback); headless = `--json`
  `collab_tool_call` events carry each child's final message + `agents_states`, and
  each child's full transcript persists in its own rollout JSONL, whose
  `session_meta` records the resolved provider — doctor's anti-misrouting check
  (the TUI agent list today shows thread IDs only, never provider).
- **Steer / resume**: steering is parent-mediated `send_input(interrupt=true)` (a true
  mid-turn abort — `<turn_aborted>` lands in the child transcript) or `followup_task`.
  Child threads are durable across processes and re-attachable by thread id
  (`resume_agent`) with full recall; `close_agent` only frees the concurrency slot.
  Any resume wrapper MUST pin `-c model=` + `-c model_provider=` (resume forgets the
  recorded model, hard-fails on non-OpenAI providers, and a failed attempt poisons
  the session's recorded model); `codex exec resume` takes `-c sandbox_mode=`, not
  `--sandbox`; `resume_agent` emits no event — never wait for one.

## 7. Resume requirement (hard requirement from grilling)

Workers must be resumable "just like a normal claude session":

- **In-session**: native — a completed named subagent is resumed from its transcript by
  sending to its name (SendMessage semantics).
- **Across parent resume** (`claude --resume`): VERIFY that prior workers' names still
  resolve and their transcripts reattach. This is verification item V1; if native
  behavior falls short, the fallback design is a `/task resume <name>` path that
  re-seeds a fresh worker from the persisted subagent transcript JSONL (dispatcher-side
  copy, like the old Runtime's resume — but implemented as a thin skill, not a runtime).
- **Codex side: RESOLVED (live, 2026-08-12, §12)** — child threads survive process
  exit; a closed child was re-attached by raw thread id from a new `codex exec`
  ~15 minutes later and recalled its prior answer (3 completed turns across 3 OS
  processes accumulated in one rollout). Both paths work: fresh parent +
  `resume_agent(<child id>)`, and `codex exec resume <parent id>` (the parent recalls
  its children's ids unprompted). Subject to the resume gotchas in §6. V2 closed.

## 8. Translation module (dialect: openai, in-house)

Anthropic Messages ⇄ OpenAI chat-completions, both directions, streaming included:

| Anthropic | OpenAI chat |
|---|---|
| `system` (string/blocks) | leading `role:system` message |
| `messages[].content` blocks | string/parts content |
| `tools[].input_schema` | `tools[].function.parameters` |
| `tool_use` block | `assistant.tool_calls[]` (id, name, JSON-string args) |
| `tool_result` block | `role:tool` message (tool_call_id) |
| `max_tokens` | `max_tokens` / `max_completion_tokens` (per provider) |
| `stop_reason` end_turn/tool_use/max_tokens | `finish_reason` stop/tool_calls/length |
| SSE `content_block_delta` etc. | SSE `chat.completion.chunk` deltas (reassemble tool-call argument fragments) |
| `usage.{input,output}_tokens` | `usage.{prompt,completion}_tokens` |
| thinking policy (per profile) | `reasoning_effort` / provider-specific |

Risk areas (flagged for the test suite): streaming tool-call argument reassembly,
parallel tool calls, consecutive-role coalescing, image blocks (reject cleanly),
provider quirks in `finish_reason`. The hermetic test suite (spiritual successor to the
old ADR 0011 fake-provider server) ships fake providers of BOTH dialects and asserts
byte-level passthrough on the default branch.

## 9. Explicitly dropped (old plugin inventory)

Bridge Agent + contract skill; bash Runtime loop/liveness/ladder; durable job store +
heartbeats + reaps; steer queue; resume verb (superseded by §7); confinement
(realpath/denylist/read-only — replaced by native permissions only, per grilling);
status/results/profiles-as-Runtime-verbs; session registry hooks; Codex Bridge
adapters/yolo boundary/pretool hook; the `anthropic`-SDK dependency; conda env for the
*Runtime* (env survives only for the proxy service); `CHINAMAX_*` env plumbing.

## 10. Open questions & verification items (for the ADR/PRD round)

- **V1**: worker resume across `claude --resume` (native or fallback re-seed?).
- **V2 — ANSWERED 2026-08-12** (§6/§7/§12): steer = `send_input(interrupt=true)`, a
  true mid-turn abort; lifecycle events = `collab_tool_call` items (spawn_agent /
  wait / send_input / close_agent; child statuses pending_init / running / completed /
  errored); child threads durable + re-attachable by id across processes. Direct
  operator→child input is rejected by design (PR #27173) — always relay through the
  parent.
- **V3 — ANSWERED 2026-08-12** (§12): `wire_api = "responses"` is the only value on
  0.147.0; `"chat"` hard-fails config load (removed ~2026-02, discussion #7782). No
  Responses⇄chat translation ships in v0.1 (D19): deepseek/glm/minimax relay
  natively, mimo needs only `tool_scrub`, kimi is account-gated at Moonshot.
- **V4**: agent-file pickup timing in Claude Code (regeneration requires restart or
  hot-reload?) — matters only for setup UX, not for spawning.
- **V5 — DISSOLVED** by D16 (separate per-host model lists): Anthropic-surface IDs
  (incl. `[1m]`) never appear on the Responses surface; `codex_models[]` carries the
  provider's real Responses IDs (`MiniMax-M3`, not `MiniMax-M3[1m]`).
- **V6**: per-profile `count_tokens` support matrix; estimator fallback accuracy.
- **V7**: DeepSeek-side automatic prefix caching economics through the proxy (log
  usage fields and confirm cache-hit accounting; same for other providers). First
  evidence in hand: DeepSeek's Responses surface reports `cached_input_tokens`
  (63,104 of 63,816 input on a live parent turn, §12).
- **V8**: Windows service wrapper choice (WinSW vs NSSM vs pywin32) and conda-python
  service pathing on Windows.
- Naming: final plugin name; final port; marketplace naming.
- Watch: Claude Code native per-agent provider routing (#38698) — would let the
  Claude-side proxy retire; the registry/generation design should survive that swap.
- Watch (Codex, added 2026-08-12): `deepseek-v4-pro` Codex gate ("early August 2026" —
  still HTTP 400 on 08-12); Moonshot enabling `/responses` (403 account gate); Codex
  honoring `tools.web_search = false` (a real config key, dead in 0.147.0 — would free
  mimo without `tool_scrub`); restoration of direct operator→subagent input (#34591);
  `multi_agent_v2` rollout (currently off). Doctor pins the verified CLI (0.147.0) and
  re-verifies on version bumps.

## 11. Distribution

New GitHub repo under `kguo93`, version starts 0.1.0. Claude marketplace entry
(`.claude-plugin/`) + Codex marketplace entry (`.codex-plugin/`), with the dual-manifest
synchronization rules carried over from the old repo's CLAUDE.md. The old
`chinamax_plugin` repo is archived at cutover; its rpi4 backup-remote convention
carries over. Version bumps on any change to shipped components, as today.

## 12. Codex live-verification record (2026-08-12, CLI 0.147.0)

Method: binary schema probing + throwaway-`CODEX_HOME` live runs (probe agent) and
docs/source/issue research (docs agent). Live results override docs where they
conflicted — which they did on 3 of 5 providers. Real `~/.codex` never touched.

| provider | /responses | codex exec smoke | Responses base | model ID |
|---|---|---|---|---|
| deepseek | yes (Codex-adapted, 2026-07-31) | PASS | `api.deepseek.com`[`/v1`] | `deepseek-v4-flash` (`-chat`/`-reasoner` are server-side aliases of it; `v4-pro` server-gated) |
| glm | yes (docs wrong) | PASS | `api.z.ai/api/v1` (chat lives on `api/paas/v4`) | `glm-5.2` |
| minimax | yes (docs wrong) | PASS | `api.minimax.io/v1` | `MiniMax-M3` |
| mimo | yes | FAIL — gateway 400s Codex's unsuppressible `web_search` tool; `tool_scrub` is the fix | `api.xiaomimimo.com/v1` | `mimo-v2.5-pro` |
| kimi | route exists, 403 account-gated | n/a | `api.moonshot.ai/v1` | (chat surface fine: `kimi-k3`) |

Key verified facts, for the ADR/PRD round:

- Per-role `model_provider` + `model` honored end-to-end: a child pinned to a broken
  provider errored on that URL while the parent ran DeepSeek; per-child rollout
  `session_meta` records the resolved provider (the anti-misrouting hook for doctor).
- Spawn surface = `multi_agent_v1` tool namespace (spawn_agent / wait / send_input /
  close_agent / resume_agent), prompt-gated against unrequested delegation; custom
  role names + locked models are advertised inside the spawn tool schema.
- Fidelity: default `exec` transcript is near-blind (parent paraphrase only) → D20
  contracts + hooks; `--json` exposes child final messages + lifecycle events; the
  child rollout JSONL holds the full transcript (reasoning included).
- Steer = real turn abort (`<turn_aborted>` written into child history). Child threads
  durable, re-attachable by id across processes; `close_agent` non-destructive.
- Resume gotchas: resume forgets the recorded model — always pin `-c model=` +
  `-c model_provider=`; a failed resume poisons the session's recorded model;
  `codex exec resume` rejects `--sandbox`/`--skip-git-repo-check` (use
  `-c sandbox_mode=`); `resume_agent` emits no event.
- Config placement: `model_provider`/`model_providers` are project-local-denylisted
  (source: `PROJECT_LOCAL_CONFIG_DENYLIST`) — user-level `~/.codex/config.toml` only;
  project `.codex/` layers load only for trusted projects. Roles may reference
  user-level provider IDs (merged-map lookup, source-confirmed).
- Ops gotchas: `codex exec` blocks on an open pipeline stdin (`< /dev/null`); endpoint
  probes must validate response body shape (z.ai `/api/openai/v1` false-200); every
  non-OpenAI model triggers a benign "fallback metadata" warning — pin
  `model_context_window` in generated roles; `codex exec --strict-config` is the
  config validator for doctor.
