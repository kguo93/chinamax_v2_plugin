# Symmetric host surfaces: /task, /setup, /doctor, /profiles, warn-only hook

Claude and Codex ship symmetric skills/commands/hooks (sketch D18); asymmetries are
allowed only as Host-specific workarounds inside a symmetric surface (e.g. ADR 0007's
steering mediation, Codex's delegation-authorization language — spawn_agent's schema
actively discourages unprompted delegation, so the task skill MUST carry explicit "you
are authorized and required to delegate" wording; headless dispatch closes stdin,
`codex exec` blocks on an open pipeline stdin).

- **`/chinamaxM:task`** — `profile=<name> [model=<listed>] [name=<worker>] <prompt…>`.
  No default Profile; an unlisted model errors with the valid list. Spawns the matching
  Generated agent as a **named background subagent** (default name `<profile>-<n>`),
  which is the address for steering/resume (ADR 0007/0008). Completion arrives as the
  native task notification; the Relay rules of ADR 0007 apply. Never passes a model
  override on the spawn call (ADR 0004).
- **`/chinamaxM:setup`** — mutating, consent-gated: diagnose → plan → approve → apply →
  re-diagnose → report. Applies: miniconda gate; conda env `chinamaxM` + aiohttp +
  pinned litellm; proxy file; OS service install/start (ADR 0009); user-level
  `ANTHROPIC_BASE_URL` env block in `~/.claude/settings.json` (Claude side; Codex needs
  no global flip); scaffold BOTH key files (Codex file only when `~/.codex` exists —
  ADR 0006); generate Claude agents; generate Codex artifacts when `~/.codex` exists
  (ADR 0004) and validate with `codex exec --strict-config`; instruct a session restart
  after regeneration. **Live paid probes run here only, opt-in at the consent pause**:
  per Profile, one minimal end-to-end `/v1/messages` through the Anthropic ingress PLUS
  one minimal request through the Responses ingress (exercising the real LiteLLM Seam)
  when `~/.codex` exists; skipping never blocks setup. Teardown mode removes the env
  flip and the service.
- **`/chinamaxM:doctor`** — pure diagnosis: free, local, never spends tokens, never
  mutates, probes only Anthropic-surface endpoints. FAIL level (nonzero exit): Registry
  parse/overlay merge; conda env + aiohttp/litellm import under the env python; service
  installed+enabled+running; port listening; env flip present; Generated agents in sync
  with the Registry on both Hosts; `codex exec --strict-config` passes when `~/.codex`
  exists. WARN level (report, exit zero): key PRESENT/MISSING per Profile in both key
  files; `CLAUDE_CODE_SUBAGENT_MODEL` set; the kimi-k3 Responses bug if seen — concretely:
  the Seam degrading to a chat-completions egress (litellm's missing moonshot Responses
  config, issue #33921; ADR 0002 — warn, never exit); pinned-CLI-version drift (Codex
  0.147.0 baseline — re-verify the live-verified facts on version bumps).
- **`/chinamaxM:profiles`** — lists resolved Profiles, models, dialects, key var names +
  PRESENT/MISSING per Host file.
- **SessionStart hook** — warn-only: if the env flip is present but the proxy port is
  dead, emit a systemMessage pointing at `/chinamaxM:doctor`. Supervision itself is the
  OS's job (ADR 0009).

**Amended 2026-08-13 (operator grilling: dispatch never validates models; doctor/setup
roster updates).**

- **`/task` model semantics reversed.** The original read: "`profile=<name>
  [model=<listed>] [name=<worker>] <prompt…>`. No default Profile; an unlisted model
  errors with the valid list." There is no listed-model concept anymore (ADR 0003):
  `model=<any string>` is accepted VERBATIM and never validated. An unknown **profile**
  still errors with the valid Profile list; a missing `model=` means the Profile's
  `default_model`. Dispatch rewrites the Generated agent's model line (ADR 0004), spawns
  it, and any provider-side failure (unknown model included) relays back to the
  dispatching session verbatim as the worker's error — never swallowed, never rewritten.
  A custom `name=` must be `<profile>-` plus a NON-EMPTY suffix, both hosts.
- **Doctor roster adjustments**: the generated-agent sync check compares content
  excluding the dispatch-mutable model line (ADR 0004), reporting the current model as
  info; the conda check requires litellm `== 1.96.2` EXACTLY (version equality, not mere
  import — it operationalizes ADR 0002's pin); the roster adds the JSONL log-path print
  (ADR 0010) that hosts-04's rendering and issue AC-7 rely on; and a Linux linger line
  (`loginctl show-user <user> --property=Linger`) reports PRESENT/ABSENT as info.
- **`codex exec --strict-config` token question (probe evidence 2026-08-13)**: the
  config-ERROR path is verified tokenless on 0.147.0 — an unknown-field override exits 1
  at config parse with no API call. The PASS path (valid config) must be confirmed
  tokenless during implementation before this check ships in the doctor FAIL roster;
  if it cannot be, the check demotes to setup-only (setup is consent-gated and may spend).
- **Setup gains a Linux linger step**: the apply list includes `loginctl enable-linger
  <user>` (idempotent, skipped when already on, shown in the consent plan like every
  other mutation) so the ops PRD's "survives reboots" claim is literally true headless;
  macOS LaunchAgents remain login-scoped (per-OS scope stated honestly in ops docs).
- `/profiles` lists resolved Profiles with their `default_model` (no model lists exist).

**Amended 2026-08-14 (setup gains a Phase-A Platform-Prerequisite pause; SessionStart hook is
host-aware with a Codex twin — cross-ref ADR 0009 as amended 2026-08-14).**

- **`/setup` gains a Phase-A Platform-Prerequisite pause.** The original setup bullet read
  "Applies: **miniconda gate**; conda env `chinamaxM` + aiohttp + pinned litellm; …". That single
  miniconda gate is superseded by a per-Platform Prerequisite pause that runs BEFORE the mutating
  plan: setup DETECTS the Platform Prerequisites (`bash`; Git for Windows' `git`/`bash`/`cygpath`
  on Windows; Miniconda's `conda`) and, when any is missing, `--plan-only` PAUSES — surfacing
  agent-run **Rectification rows** (serialized under `prerequisite_fixes`) and emitting NO
  mutating plan and NO plan digest. The Host agent runs those rows on "approve" (dispatched by
  each row's `run_policy`, through its `shell`, stop-on-first-failure), then re-runs the launcher
  once; only with every Prerequisite present does the normal consent-gated plan
  (env/deps/generate/service/flip + opt-in probes) appear. The setup engine NEVER downloads or
  runs an installer — the bootstrap mechanics (per-OS commands, the Windows zero-state cmd.exe
  fallback, Git for Windows so the Codex hooks have Git Bash, no-pin/no-checksum Miniconda,
  unsupported-arch advice) live in **ADR 0009 (as amended 2026-08-14)**, which this
  cross-references and whose "miniconda gate" wording it supersedes.
- **SessionStart hook is now host-aware, with a Codex twin.** The original bullet described a
  single Claude path ("if the env flip is present but the proxy port is dead, emit a
  systemMessage pointing at `/chinamaxM:doctor`"). That Claude path is unchanged, but the hook now
  resolves the Host from its environment and adds a **Codex twin**: Codex has no `settings.json`
  flip, so its SessionStart warns iff chinamaxM is wired into Codex (a generated
  `model_providers.chinamaxM-<profile>` entry in `~/.codex/config.toml`) AND the Registry Proxy
  port is dead, pointing the operator at the `chinamaxM-doctor` skill. Both remain strictly
  warn-only and fail-open; supervision is still the OS's job (ADR 0009). Registered symmetrically
  in `hooks/hooks.json` and `hooks/codex-hooks.json` (the latter with the Git-Bash
  `commandWindows` shim).

**Amended 2026-08-14 (all command surfaces launch via the Launcher shim — cross-ref ADR 0009 as
amended 2026-08-14).** Every command/skill surface — `/setup`, `/doctor`, `/profiles`, and
`/task`'s `set_model` rewrite — now invokes `scripts/chinamaxM <subcommand>` instead of a
hardcoded `python3 -m …` (setup), `conda run -n chinamaxM python -m …` (doctor/profiles), or bare
`python -m …` (task's `set_model`) line. `commands/setup.md`'s `allowed-tools` is narrowed from
`Bash` to `Bash(${CLAUDE_PLUGIN_ROOT}/scripts/chinamaxM:*)`. The interpreter-rung mechanics (the
shared `scripts/_interpreter.sh` discovery order, the macOS operator kick-back, the Windows
zero-state re-run) live in ADR 0009 (as amended 2026-08-14), which this cross-references.

**Amended 2026-08-18 (operator grilling: every surface is Host-scoped; Host-resolution
ladder; probes cover both ingresses; teardown gains the Codex unwire — cross-ref ADRs
0004/0006/0009 as amended 2026-08-18).** The symmetric surfaces are now **Host-scoped**:
each surface acts for the single Host that invoked it, resolved once per process by the
**Host-resolution ladder** — explicit `--host claude|codex` → the `CHINAMAXM_HOST` env
marker → Codex plugin evidence → Claude plugin evidence (Codex wins ties: it exposes
Claude-compatible aliases) → hard error, never a guess. The shipped command/skill
surfaces pass `--host` explicitly (Claude command files `--host claude`, Codex skill
files `--host codex`), mirroring the original plugin's pattern. The marker is
deliberately `CHINAMAXM_HOST`, disjoint from the original plugin's `CHINAMAX_*`
namespace, so a coexisting v1 install (whose surfaces export `CHINAMAX_HOST`) can never
steer v2's resolution.

- **Setup is Host-scoped.** The original roster read "scaffold BOTH key files (Codex
  file only when `~/.codex` exists — ADR 0006); generate Claude agents; generate Codex
  artifacts when `~/.codex` exists (ADR 0004) and validate with `codex exec
  --strict-config`", and the Claude `settings.json` env flip ran unconditionally.
  **Reversed**: setup plans the shared infrastructure from EITHER Host (conda env,
  pinned deps, recorded interpreter, Proxy service install/update, Linux linger,
  readiness — each already idempotent/create-only, so a second Host's run emits
  SKIP/UPDATE rows and installs nothing twice) plus ONLY the invoking Host's wiring:
  a Claude run = Claude Key file + Claude Generated agents + the `ANTHROPIC_BASE_URL`
  flip; a Codex run = Codex Key file + Codex provider entries/role TOMLs +
  `codex exec --strict-config` validation. The `~/.codex`-existence gate is retired; a
  dual-Host operator runs `/setup` once inside each Host; no cross-host hint rows exist.
- **Live probes are NOT host-scoped — the Responses gate is reversed.** The original
  read "PLUS one minimal request through the Responses ingress … when `~/.codex`
  exists". **Reversed**: probes are a whole-Proxy verification — per Profile, one
  Anthropic-ingress probe AND one Responses-ingress probe (through the real LiteLLM
  Seam), from EITHER Host's setup, unconditionally (the Proxy always serves both
  ingresses).
- **Doctor is Host-scoped.** The FAIL/WARN roster covers the shared infrastructure
  (Registry, env + deps, service, port, log-path/linger info) plus the invoking Host's
  wiring only — Claude: flip, Claude agent sync, Claude Key names,
  `CLAUDE_CODE_SUBAGENT_MODEL`; Codex: strict-config, role/provider sync, Codex Key
  names, pinned-Codex-CLI-version drift. The other Host's artifacts, keys, and sync
  NEVER appear in the report; the Seam-level kimi-k3 WARN is shared and stays on both.
- **`/profiles` is Host-scoped**: key PRESENT/MISSING for the invoking Host's Key file
  only.
- **Teardown: symmetric unwire, shared always removed.** The Claude flip removal keeps
  its only-when-ours guard; teardown gains the missing Codex twin — on a Codex-host
  teardown, remove the generated `model_providers.chinamaxM-<profile>` entries from
  `~/.codex/config.toml` (only ours, same guard spirit). The shared service + recorded
  interpreter are ALWAYS removed regardless of the other Host's wiring (teardown is a
  machine-level Proxy uninstall; the report says so when the other Host was wired).
- **SessionStart adopts the same ladder** (minus the flag), replacing its Claude-first
  default-claude sniffing; being warn-only it stays fail-open — unresolvable means exit
  silently, never guess (ADR 0009 as amended 2026-08-18).

**Amended 2026-08-18 (Worker instance-name grammar — cross-ref ADR 0004 as amended
2026-08-18).** Two naming sentences here are superseded. The original `/task` bullet read
"Spawns the matching Generated agent as a **named background subagent** (default name
`<profile>-<n>`)", and the 2026-08-13 amendment read "A custom `name=` must be `<profile>-`
plus a NON-EMPTY suffix, both hosts." **Reversed**: the default name is now
`chinamaxm-<profile>-<task-slug>` (numeric `chinamaxm-<profile>-<n>` fallback), and a custom
`name=` must be `chinamaxm-<profile>-` plus a non-empty lowercase `[a-z0-9-]` suffix, both
Hosts. The symmetric-surface guarantee is unchanged — both task surfaces carry the identical
grammar; only the grammar itself moved. See ADR 0004 (as amended 2026-08-18) for the matcher
and rationale, and ADR 0007 (as amended 2026-08-18) for the report-delivery duty added to the
same surfaces in this pass.
