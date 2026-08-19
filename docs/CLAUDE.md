# docs/ — conventions

This file is **conventions only**: how to format an ADR, and the **routing tables** that
say which ADR(s) to **read** before a change and which to **amend** after it. Inventory
lives in the sibling `./repo-map.md`; domain vocabulary lives in `../CONTEXT.md` — use its
terms (Proxy, Ingress, Profile, Profile prefix, Worker, Relay, Seam, Default branch) in
code, docs, and commits.

`docs/sketch.md` is the 2026-08-12 brainstorm seed, NOT decision-of-record; where the ADRs
contradict it (LiteLLM vs in-house translation, Codex Responses relay, per-host model
lists), the ADRs won — deliberately, per the 2026-08-13 grilling. Ground truth once code
exists is the product code + active manifests; until then it is these ADRs.

## ADR formatting rules

- **Filename**: `NNNN-kebab-slug.md`, four-digit zero-padded, sequential, no gaps. The
  next number is the highest in `adr/` plus one (currently → `0013`).
- **Heading**: a bare prose title, no leading number (match the old repo's ADRs).
- **Amend in place; never fork a decision**: when a later decision changes an ADR, add a
  dated `**Amended <date>**` paragraph *in that same file*. When the change **reverses**
  the original, quote the original decision before overriding it. If the slug now
  contradicts the content, `git mv` it to a truthful slug. **Never create a new ADR file
  for a reversal or amendment.**
- Create a *new* ADR only for a decision genuinely orthogonal to every existing one.
- A grilling or decision that contradicts an existing ADR → edit that ADR (and
  `../CONTEXT.md` if a glossary term shifts); never leave a stale decision beside the new
  one.

## Read routing — which ADR to read for which purpose

| If your change / question touches… | Read |
|---|---|
| Proxy routing: Profile-prefix rule, default-branch byte-for-byte passthrough, ingress paths, unknown-profile 404, relay header policy, port/bind | **0001** |
| Dialect policy: Anthropic-native rule, LiteLLM as sole translator, Responses-only OpenAI dialect, tool-type stripping at the Seam, same-type-run grouping, prefix-cache stability guard | **0002** |
| Registry: profiles.json v2 schema (default_model scalar; no models[]/match), the six shipped Profiles + full seed pin, overlay merge, thinking normalization, scrub, extras guard | **0003** |
| Workers as native subagents: ONE generated Claude agent .md / Codex role TOML per Profile, immutable outside generation, the Dispatch marker (per-dispatch model override), Worker instance-name grammars, reserved names, regeneration/strict drift, what project doc a Worker inherits per Host (AGENTS.md / CLAUDE.md) | **0004** |
| Host command surfaces: /task /setup /doctor /profiles, Host-scoped surfaces + the Host-resolution ladder (`--host` / `CHINAMAXM_HOST`), host-aware SessionStart hook (Claude + Codex), doctor roster + warn/fail semantics, setup consent flow incl. the Phase-A Platform-Prerequisite pause / Rectification-row protocol (bootstrap mechanics live in **0009**) + live probes | **0005** |
| API keys: per-host model-keys.env files, proxy-side injection, scaffolding | **0006** |
| Result relay + steering: worker contract hooks, verbatim no-attribution relay, Codex parent-mediated steer | **0007** |
| Worker resume: live-session continuity, out-of-scope dead-session recovery, Codex thread re-attach | **0008** |
| Proxy supervision on Linux/macOS/Windows: systemd/launchd/WinSW, self-healing, Platform-Prerequisite detection + agent-run Rectification rows (bash / Git for Windows / Miniconda; engine never installs), warn-only host-aware SessionStart (Claude `ANTHROPIC_BASE_URL` flip + Codex `config.toml` provider) | **0009** |
| Observability: JSONL request log, usage/cache accounting, count_tokens fallback | **0010** |
| Tests: hermetic fake providers, real-litellm-in-loop, prefix-stability byte tests | **0011** |
| Distribution: GitHub canonical source, dual-manifest sync, versioning, rpi4 git-only backup | **0012** |

## Edit routing

- A change inside one theme → amend that theme's ADR in place (dated paragraph).
- A change spanning two ADRs → amend the primary one and cross-reference the other by
  number.
- New ADR checklist: sequential number; update `./repo-map.md`'s ADR table; add this
  file's routing rows; update any superseded ADR's status note.
