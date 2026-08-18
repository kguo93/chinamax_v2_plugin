# Workers are native host subagents, generated one per Profile, never hand-maintained

A Worker is a first-class subagent of the host CLI — spawnable by the host's own agent
mechanism, addressable, background-capable, transcript-persisted — not a Bridge-driven
external Runtime. Agent files are thin, setup-GENERATED artifacts, regenerated on
Registry change; hand edits are drift, which `/doctor` flags (regeneration is the only
edit path).

**Claude Code**: one `.md` per Profile × listed model. Naming: the default model gets the
bare Profile name (`deepseek`), alternates get `<profile>--<model-slug>`
(`deepseek--v4-flash`). ~10 lines: name, description ("chinamaxM worker on
<provider>/<model>"), `model: <string>` — the routing key, passed to the wire verbatim
(frontmatter `model` is a free-form string and is the ONLY per-agent model channel) —
tools (standard set **including Agent**, so Workers may spawn Explore subagents;
per-Profile `tools` override), and a lean worker system prompt. Two hard constraints from
the model-resolution order (env → per-spawn param → frontmatter → inherit): the dispatch
must NEVER pass a model override on the Agent tool call (the param is enum-locked anyway),
and `CLAUDE_CODE_SUBAGENT_MODEL` must stay unset — it would silently hijack every
Worker's model (doctor WARNs when set).

**Codex**: per Profile, a `[model_providers.chinamaxM-<profile>]` entry in **user-level**
`~/.codex/config.toml` only (`model_provider*` sits on Codex's project-local config
denylist), with `base_url = "http://127.0.0.1:8402/openai/<profile>"`,
`wire_api = "responses"`, no `env_key` (ADR 0006), never the reserved IDs
openai/ollama/lmstudio. Plus one role TOML `~/.codex/agents/<profile>[--<model-slug>].toml`
per Profile × listed model: `model = <the same Anthropic-surface ID>`, `model_provider =
"chinamaxM-<profile>"` (source-confirmed sticky-override semantics), pinned
`model_context_window` from the Registry's per-model map (kills the "fallback metadata"
guesswork), lean worker instructions. Role names: ASCII letters/digits/space/-/_ only.
`multi_agent` is stable and default-on; roles auto-discover from `$CODEX_HOME/agents/`.

**No confinement story**: Workers get the standard toolset and native permissions
(`permissionMode`) only — the old realpath/denylist/read-only layer is deliberately dead.

**Pickup after regeneration** (closes sketch V4): `/setup` always instructs the operator
to restart open host sessions after (re)generating agents — correct on every CLI version
regardless of any hot-reload behavior, and the only cost is one restart notice. Doctor's
drift check catches the stale-session case either way.

**Amended 2026-08-13 (operator grilling: one artifact per Profile; dispatch-time model
rewrite; reserved names; field-list corrections).**

- **One artifact per Profile.** The original decision read: "one `.md` per Profile ×
  listed model. Naming: the default model gets the bare Profile name (`deepseek`),
  alternates get `<profile>--<model-slug>` (`deepseek--v4-flash`)" and, on Codex, "one
  role TOML `~/.codex/agents/<profile>[--<model-slug>].toml` per Profile × listed model".
  **Reversed**: `models[]` no longer exists (ADR 0003), so generation emits exactly ONE
  Claude agent `.md` named `<profile>` and ONE Codex role TOML `<profile>.toml` per
  Profile. The `--<model-slug>` naming and slug machinery are retired. Operator-chosen
  worker names at dispatch are `<profile>-<suffix>` with a NON-EMPTY lowercase
  `[a-z0-9-]` suffix, on both hosts.
- **The model line is dispatch-mutable state.** The Claude artifact pins
  `model: <profile>/<default_model>` (profile-prefix routing, ADR 0001); the Codex role
  TOML pins the bare `default_model` (its Profile rides the provider entry's ingress
  path). A dispatch with an explicit model REWRITES that artifact's model line —
  `<profile>/<M>` on Claude, bare `M` on Codex — then spawns; the string is never
  validated, and a wrong model fails at the provider with the error relayed to main.
  Consequences: (a) "regeneration is the only edit path" gains exactly one sanctioned
  sibling — the dispatch-time model-line rewrite; (b) the drift check compares generated
  content EXCEPT the model value, which doctor reports as info, never drift;
  (c) regeneration (SessionStart hook, setup) resets model lines to `default_model`;
  (d) the host MUST re-read the artifact at spawn time — hosts-02 live-verifies this
  linchpin before anything else is built on it; the rewrite→spawn race window is
  per-dispatch and accepted.
- **Reserved names, both hosts, enforced at Registry load (cross-ref 0003).** Claude
  side: a Profile may not be named like a built-in Claude Code agent —
  reserved list: `general-purpose`, `Explore`, `Plan`, `claude`, `statusline-setup`,
  `claude-code-guide`, `output-style-setup` (case-insensitive). Codex side: the reserved
  provider IDs `openai`/`ollama`/`lmstudio` (already pinned above), and generation
  refuses to overwrite a pre-existing role TOML it did not generate. Collisions refuse
  loudly; the shipped six are safe.
- **Field-list corrections (per review):** the description reads "chinamaxM worker on
  `<profile>`" — `<provider>` was a CONTEXT.md-banned term; port 8402 in the provider
  `base_url` example is the shipped DEFAULT from the Registry header (ADR 0001/0003),
  not a constant; the role TOML also carries `name`, `description`, and
  `developer_instructions` (as the installed CLI's role TOMLs do), and the provider
  entry also carries its `name` display field; `tools: null` in the Registry means
  generation OMITS the tools frontmatter key entirely, letting the host grant its
  standard toolset (which includes Agent, so Workers may spawn Explore subagents).

**Amended 2026-08-15 (Host project-doc symmetry live-verified; no compensation needed).**
A spawned Codex role receives the workspace `AGENTS.md` in its context exactly as a
top-level thread does — verified on Codex 0.147.0 against an isolated `CODEX_HOME` with a
fresh, non-forked child (`session_meta.forked_from_id: None`), whose rollout JSONL carries
the same `# AGENTS.md instructions for <cwd>` block as its parent. Codex ignores `CLAUDE.md`
by default (`project_doc_fallback_filenames` is the opt-in knob). Claude Code symmetrically
loads the full CLAUDE.md hierarchy into every subagent except the built-in `Explore` and
`Plan`. So a Worker on either Host gets that Host's project doc ON TOP OF the shared
`WORKER_INSTRUCTIONS` generation emits, and generation must NOT try to inline project
context to compensate. When testing what a Worker can see, assert on the rollout JSONL under
`$CODEX_HOME/sessions/` — never on a child's self-report, which under-reports — and check
`forked_from_id` first, since a full-history fork confounds any inheritance test.

**Amended 2026-08-16 (Workers are pointed at their Host's lazy-loaded MCP tools).**
`WORKER_INSTRUCTIONS` gains item 5: MCP tools exist on both Hosts, their schemas load ON
DEMAND, and a Worker looks one up only when its task needs it — `ToolSearch` on Claude,
`ALL_TOOLS` / `tools.mcp__<server>__<tool>` inside the `exec` sandbox on Codex. Schemas are
NOT preloaded; a Worker is told where to look, nothing more. Both Hosts hand a Worker the
parent's MCP servers (live-verified 2026-08-15: a Claude subagent called
`mcp__codebase-memory-mcp__list_projects` successfully, and a Codex role enumerated the
same server's fourteen tools plus remote plugin tools from `ALL_TOOLS`), so the gap was
never availability — it is that a non-Claude Worker model has no reason to suspect a
deferred schema exists and will conclude it has no MCP tools. Two consequences for
generation: (a) a Profile's `tools` override is an ALLOWLIST on Claude and silently drops
every MCP tool unless it also lists `mcp__<server>` or `mcp__<server>__*` — and a
plugin-bundled server needs the longer `mcp__plugin_<plugin>_<server>__<tool>` form, which
a bare pattern never matches; (b) the generated Codex role TOML has no tool-restriction
key at all, so a Codex Worker always gets the full inherited surface. That restriction
asymmetry between the Hosts is real and remains OPEN — item 5 does not close it.

**Amended 2026-08-18 (Worker instance-name grammar carries a `chinamaxm-` prefix; default
names are task-descriptive; the shared matcher accepts the new form only).** The 2026-08-13
amendment read: "Operator-chosen worker names at dispatch are `<profile>-<suffix>` with a
NON-EMPTY lowercase `[a-z0-9-]` suffix, on both hosts." **Reversed**: a Worker INSTANCE name
(the spawn `name`, distinct from the generated artifact — still the bare `<profile>`) is now
`chinamaxm-<profile>-<suffix>` with a non-empty lowercase `[a-z0-9-]` suffix, on both Hosts.
The default name is `chinamaxm-<profile>-<task-slug>` — a short kebab slug the dispatching
Host derives from the prompt so the name references chinamaxM and the task rather than a bare
Profile index (`chinamaxm-deepseek-repo-summary`); a session collision appends `-2`, `-3`, …;
an unsloggable prompt falls back to the numeric `chinamaxm-<profile>-<n>` (lowest-unused,
counting running AND completed Workers). Consequences: (a) the single shared matcher
`matches_generated_agent` (via the `WORKER_NAME_PREFIX` constant) now accepts EXACTLY the
bare `<profile>` (the subagent_type/role on the spawn call) and
`chinamaxm-<profile>-<non-empty suffix>` — the legacy `<profile>-<suffix>` instance form
(`deepseek-1`) no longer fires the Worker contract; (b) both Host surfaces
(`commands/task.md`, `skills/chinamaxM-task/SKILL.md`) mint and accept only the new grammar;
(c) the reserved-name rule and one-artifact-per-Profile rule are untouched — only the
operator-facing INSTANCE-name grammar changed. Rationale: the contract hook matches on the
spawn NAME, and a bare-index name said nothing about chinamaxM or the task; anchoring it to
`chinamaxm-<profile>-<task-slug>` fixes both. Cross-ref ADR 0005/0007 (as amended
2026-08-18).

**Amended 2026-08-18 (generation is Host-scoped at the surface — cross-ref ADR 0005 as
amended 2026-08-18).** "Agent files are thin, setup-GENERATED artifacts" now means the
invoking Host's artifact set only: a Claude-host setup generates the Claude agent `.md`s
and never the Codex artifacts — even when `~/.codex` exists; that gate is retired — and a
Codex-host setup generates the provider entries + role TOMLs and never the Claude `.md`s.
A dual-Host machine gets each side's artifacts from that side's own setup run. The
artifact model itself is untouched (ONE artifact per Profile per Host, dispatch-mutable
model line, reserved names enforced at Registry load for both Hosts), and doctor's sync
check likewise inspects only the invoking Host's artifacts (ADR 0005).
