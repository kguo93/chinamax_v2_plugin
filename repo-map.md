# chinamax_v2_plugin — inventory

ADR-round repo for **chinamaxM**, the worker-model native-subagent plugin (successor to
`~/chinamax_plugin`). The proxy service is under implementation (proxy-01/02 landed): the
`docs/adr/` decisions of record plus the `src/`/`tests/` code that realizes them.

| Path | What it is |
|---|---|
| `CLAUDE.md` | root conventions (secrets rule) |
| `AGENTS.md` | stub pointing Codex/other agents at `CLAUDE.md` |
| `CONTEXT.md` | the domain glossary — Proxy, Ingress, Profile, Worker, Relay, Seam, … |
| `repo-map.md` | this inventory |
| `pyproject.toml` | package metadata + `test` extra (aiohttp, litellm pin, pytest) |
| `docs/` | sketch seed + conventions + `docs/adr/` decisions of record (see `docs/repo-map.md`) |
| `src/chinamaxM/` | the proxy package: `registry.py` (Registry loader), `proxy.py` (aiohttp app + router + Default/Relay/Responses ingresses), `relay.py` (relay mutation/serialize/forwarder), `keyfiles.py` (per-Host Key-file reader + O_EXCL scaffold), `seam.py` (LiteLLM Seam: Responses⇄Anthropic translation + streaming state machine), `doctor.py` (static no-chat-completions scan), `data/profiles.json` (shipped seed Registry) |
| `tests/` | hermetic pytest suite: `conftest.py` (fake provider + fake Codex client + guards + fixtures), `test_registry.py`, `test_proxy.py` (passthrough + routing + relay), `test_seam.py` (Responses ingress + Seam translation + streaming + startup gates) |
