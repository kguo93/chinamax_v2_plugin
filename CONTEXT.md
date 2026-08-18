# chinamaxM — Worker-Model Native-Subagent Plugin

A dual-Host plugin that exposes non-Claude worker models (DeepSeek, MiMo, GLM, MiniMax, Kimi, Qwen) as first-class native subagents of the host CLI, routed through one local reverse proxy. The M stands for improved.

## Language

### Routing

**Proxy**:
The single local reverse-proxy service that receives every host API request and routes it by model string and ingress path. The only new long-running service in the system.
_Avoid_: Runtime, Bridge, gateway (the old plugin's concepts; they no longer exist)

**Ingress**:
One of the Proxy's two receiving surfaces — the Anthropic ingress (what `ANTHROPIC_BASE_URL` points at, speaking Anthropic Messages) and the Responses ingress (what generated Codex provider entries point at, speaking OpenAI Responses).
_Avoid_: endpoint (reserve for provider-side URLs)

**Profile prefix**:
The routing key on the Anthropic ingress: a worker-bound model string is `<profile>/<model>`, split on the first `/`. The Proxy routes by the prefix, strips it, and forwards the bare model string verbatim — model strings are never validated anywhere; the provider is the sole authority, and its errors relay back unrewritten. (The Responses ingress carries the Profile in its path instead.)

**Default branch**:
The route for a request whose model string carries no Profile prefix (or whose body is unparseable or model-less): byte-for-byte passthrough to api.anthropic.com — body unserialized, auth and beta headers untouched. Claude main and native Claude subagents always ride this branch. A slash-prefixed but UNKNOWN profile — on either ingress — gets a local 404 naming the valid Profile list, never passthrough.
_Avoid_: fallback (nothing failed; this is the normal Claude path)

**Relay branch**:
The route for a Profile-prefixed request whose Profile speaks Anthropic natively: the Proxy strips the prefix, swaps auth, applies Scrub and Thinking normalization, merges request extras, and forwards to the provider's Anthropic-compatible endpoint. No dialect translation occurs.

**Translated branch**:
Any route crossing a dialect Seam. LiteLLM is the only translation engine, and the only OpenAI dialect it speaks is Responses — chat-completions is banned everywhere.

**Seam**:
A boundary where dialects differ and LiteLLM translates — today only Responses⇄Anthropic at the Codex ingress; a future Responses-only provider egress would be the same Seam mirrored. Only plain function tools cross a Seam; every other tool type is stripped and the stripping is logged.

**Prefix stability**:
The guarantee that a translated conversation serializes to byte-identical prefix bytes turn over turn, so provider-side automatic prefix caching keeps hitting. A translation that breaks Prefix stability silently converts cache reads into fresh input tokens.

### Configuration

**Registry**:
The single source of truth (`profiles.json` v2): shipped Profile defaults merged with the user Overlay. Feeds Proxy routing, agent generation on both Hosts, key scaffolding, and doctor checks.

**Profile**:
A named provider configuration in the Registry — Anthropic-compatible endpoint, key variable name, a default model, Thinking normalization policy, Scrub list, request extras. No model list exists and no model string is ever validated against the Registry. Six ship: deepseek, mimo, glm, minimax, kimi, qwen; names are reserved-checked at load on both Hosts (ADR 0004).
_Avoid_: provider (the company), model (one field of a Profile)

**Overlay**:
The user-level Registry file that overrides shipped Profile defaults, same merge semantics as the old plugin.

**Key file**:
A per-Host env-format secrets file (`~/.claude/model-keys.env`, `~/.codex/model-keys.env`) scaffolded comments-only; values are never printed, only PRESENT/MISSING. The Proxy is the only reader — it injects the key matching the request's ingress Host.

**Thinking normalization**:
Per-Profile request shaping that strips the Claude-flavored thinking/output-config form and substitutes the provider's own reasoning dialect. First-class in the Registry, seeded from the old plugin's proven request extras.

**Scrub**:
A Profile's list of request fields the provider rejects or ignores, dropped on worker-bound requests only.

### Workers

**Worker**:
A native host subagent (Claude subagent or Codex role child) generated from a Profile, whose entire agentic loop rides the Proxy to that Profile's provider. First-class in the host: spawnable, addressable, background-capable, transcript-persisted.
_Avoid_: Job, Bridge Agent, Runtime worker (old architecture)

**Worker name**:
The address of a dispatched Worker instance: `chinamaxm-<profile>-<suffix>`, where the
suffix is a short task-descriptive slug (or a numeric fallback when the task yields none).
Distinct from the Generated agent, which is named for the bare Profile. Steering and
continuation address a Worker by this name.
_Avoid_: a bare Profile-indexed name (`deepseek-1`) — retired; it named neither chinamaxM
nor the task.

**Generated agent**:
A thin, setup-generated artifact — a Claude agent `.md` or a Codex role TOML — ONE per Profile, never hand-maintained. Its model line is dispatch-mutable state: `/task model=…` rewrites it before spawning, and regeneration resets it to the Profile's default. All other drift from the Registry is a doctor finding; regeneration and the dispatch-time model-line rewrite are the only edit paths.

**Worker contract**:
The hook-injected rules riding every Worker spawn on both Hosts: the Worker must end with a complete final report, and the parent must print that report verbatim.

**Relay**:
The parent's delivery of a Worker's final response: verbatim, no attribution, no summarizing — printed as though the parent did the work itself. The transcript remains the audit trail.
_Avoid_: progress message, status update, "worker X says"

**Steer**:
A mid-run instruction to a Worker. On Claude, a direct message to the named subagent; on Codex, parent-mediated (`send_input`) because the platform rejects direct operator→child input. The user-facing surface is symmetric; the mediation is a Host-specific workaround.

**Host**:
The plugin host a request or artifact belongs to — `claude` or `codex`. Determines ingress, Key file, and generated-artifact format; it is never a provider Profile. Every command surface resolves the invoking Host once, by ladder — explicit `--host`, else the `CHINAMAXM_HOST` marker, else plugin-environment evidence with Codex evidence outranking Claude's (Codex exposes Claude-compatible aliases) — and errors rather than guesses when nothing resolves; the warn-only SessionStart hook instead stays silent.

### Operations

**Doctor**:
The pure-diagnosis command: free, local, token-less, mutation-less. Probes only Anthropic-surface endpoints; warns without exiting on upstream gaps (the kimi-k3 Responses bug), fails only on broken installation state. Host-scoped: it reports the shared infrastructure plus the invoking Host's wiring only; the other Host never appears in its report.

**Setup**:
The consent-gated mutating command: diagnose → plan → approve → apply → re-diagnose → report. The only place live paid probes run, opt-in at the consent pause. Host-scoped: it converges the shared Proxy infrastructure idempotently, then installs ONLY the invoking Host's wiring — the other Host's artifacts are never touched, so a dual-Host machine runs Setup once inside each Host.

**Prerequisite**:
An external Platform tool setup must have before it can build the `chinamaxM` conda env the Proxy runs in — `bash`, Git for Windows' `git`/`bash`/`cygpath` (Windows), Miniconda's `conda` — detected per Platform at the start of setup, before any conda-env / dependency / Key-file / service mutation (the plan's read-only diagnose still runs). Distinct from the Python dependencies installed inside the env. A missing Prerequisite PAUSES setup (Phase A) for operator approval; it is never installed silently, and the setup engine never runs an installer itself. On Windows a Prerequisite means Git for Windows' own tooling specifically — Git Bash is resolved from the Git-for-Windows install tree, never a PATH bash (which may be WSL's, not the bash the Codex hooks run).
_Avoid_: dependency (reserved for the Python packages in the env), requirement

**Rectification row**:
The plain dict setup EMITS for one missing Prerequisite on the current Platform — `{name, summary, commands, run_policy, shell, install_location}` (plus `missing_tools` on the Git for Windows row) — the single source of truth for how that Prerequisite gets installed. The Host agent runs a row's `commands` verbatim, dispatched by its `run_policy` (`agent` / `privileged` / `operator`) through its `shell` (`cmd` / `powershell` / `native` / `bash`), only after the operator approves; the setup engine never runs them. The `--plan-only` Phase-A pause carries these rows under the `prerequisite_fixes` key.
_Avoid_: calling a row a "fix" in prose (the serialized key stays `prerequisite_fixes`, but the concept is a Rectification row), installer script

**Launcher**:
The single shim every host surface runs plugin Python through. It resolves the interpreter
by one pinned rung order (shared with the hook shims), so no surface assumes an ambient
Python.
_Avoid_: "ambient Python" as a requirement (it is only the launcher's last bootstrap rung)

**Recorded interpreter**:
The interpreter path setup records after a successful apply — the first rung every shim
consults. Absent until the first successful apply.
