# docs/ — inventory

| Path | What it is |
|---|---|
| `CLAUDE.md` | docs conventions: ADR formatting rules + read/edit routing tables |
| `AGENTS.md` | stub pointing Codex/other agents at `CLAUDE.md` |
| `repo-map.md` | this inventory |
| `sketch.md` | 2026-08-12 brainstorm seed for the rebuild — NOT decision-of-record; superseded by the ADRs where they conflict |
| `IMPLEMENTATION.md` | post-review execution order + protocol for implementing the 11 plans (strictly sequential; per-plan fresh zero-context Opus xhigh implementer + separate Opus `/review` verifier; one commit per plan; `/close-issues` per plan) — all preflight decisions resolved 2026-08-13 and recorded in §0 |
| `adr/` | decisions of record (table below) |
| `agents/` | Local issue-tracker conventions trio (issue-tracker.md, triage-labels.md, domain.md) seeded by /to-plan |
| `plan/` | frozen implementation plans, one per issue: 4× chinamaxm-proxy-*, 5× chinamaxm-hosts-*, 2× chinamaxm-ops-* (mirrored to ~/.claude/plans/); source PRDs/issues live in ../.scratch/chinamaxm-{proxy,hosts,ops}/ (untracked) |

## adr/

| # | File | Decides |
|---|---|---|
| 0001 | `0001-model-string-routed-local-proxy.md` | One aiohttp proxy at 127.0.0.1:8402; the model string's `<profile>/` prefix is the routing key (amended 2026-08-13 — no glob matching; strip-and-forward, never validated); prefixless → byte-for-byte Anthropic passthrough; two ingresses |
| 0002 | `0002-anthropic-native-litellm-sole-translator.md` | Anthropic Messages is the native dialect everywhere; embedded LiteLLM is the only translator; OpenAI dialect = Responses only; function-tools-only Seam; prefix-cache stability guard; kimi-k3 warn |
| 0003 | `0003-profiles-json-v2-registry.md` | Registry schema (amended 2026-08-13: no models[]/match — default_model scalar only, full seed pinned, extras guard); six shipped Profiles; thinking normalization first-class; scrub as cache protection |
| 0004 | `0004-native-subagents-generated-per-profile.md` | Workers = native host subagents; ONE generated artifact per Profile (amended 2026-08-13); dispatch-mutable model line; reserved names both hosts; restart-after-regeneration |
| 0005 | `0005-symmetric-host-surfaces.md` | /task /setup /doctor /profiles + SessionStart hook; doctor FAIL/WARN roster; setup consent flow + opt-in dual-ingress live probes |
| 0006 | `0006-per-host-key-files-proxy-injection.md` | Per-Host model-keys.env files; proxy-side key injection; no Codex env_key (supersedes sketch D17) |
| 0007 | `0007-verbatim-relay-and-host-mediated-steering.md` | Hook-injected Worker contract; verbatim no-attribution Relay; Codex parent-mediated steering |
| 0008 | `0008-worker-resume.md` | Worker resume = live-session continuity (native both Hosts); dead-session recovery out of scope; Codex re-attach gotchas |
| 0009 | `0009-self-healing-supervision-three-oses.md` | systemd user unit / launchd KeepAlive / WinSW Windows Service; setup-installed, doctor-verified |
| 0010 | `0010-jsonl-observability-and-count-tokens-fallback.md` | JSONL request log incl. cache fields (V7 closed); count_tokens forward-with-estimator-fallback (V6 closed) |
| 0011 | `0011-hermetic-two-dialect-tests-real-litellm.md` | Hermetic fake providers both dialects; LiteLLM never mocked; prefix-stability byte tests |
| 0012 | `0012-github-canonical-distribution-dual-manifests.md` | Plugin identity (amended 2026-08-13: kebab `chinamaxm`/`chinamaxm-plugin`; package stays `chinamaxM`)/version/license; dual-manifest shapes; GitHub canonical; rpi4 git-backup-only |
