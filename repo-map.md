# chinamax_v2_plugin — inventory

ADR-round repo for **chinamaxM**, the worker-model native-subagent plugin (successor to
`~/chinamax_plugin`). The proxy service and Host surfaces are under implementation
(proxy-01/02/03/04, hosts-01 generators, hosts-02 Claude dispatch + worker-contract hooks,
hosts-03 Codex dispatch skill + Codex hook adapter, and ops-01 systemd/launchd/WinSW
supervision landed): the `docs/adr/` decisions
of record plus the `src/`/`tests/` code and the plugin
`commands/`/`skills/`/`hooks/`/`scripts/`/`.codex-plugin/` surfaces that realize them.

| Path | What it is |
|---|---|
| `CLAUDE.md` | root conventions (secrets rule) |
| `AGENTS.md` | stub pointing Codex/other agents at `CLAUDE.md` |
| `CONTEXT.md` | the domain glossary — Proxy, Ingress, Profile, Worker, Relay, Seam, … |
| `repo-map.md` | this inventory |
| `pyproject.toml` | package metadata + `test` extra (aiohttp, litellm pin, pytest) |
| `docs/` | sketch seed + conventions + `docs/adr/` decisions of record (see `docs/repo-map.md`) |
| `src/chinamaxM/` | the proxy package: `registry.py` (Registry loader), `proxy.py` (aiohttp app + router + Default/Relay/Responses ingresses + per-request JSONL logging + count_tokens forward/estimator), `relay.py` (relay mutation/serialize/forwarder, optional usage tee), `keyfiles.py` (per-Host Key-file reader + O_EXCL scaffold), `seam.py` (LiteLLM Seam: Responses⇄Anthropic translation + streaming state machine), `observability.py` (append-only JSONL request log, provider-usage tee, count_tokens estimator, log-path resolver), `doctor.py` (static no-chat-completions scan), `generate.py` (Registry→Claude agents + Codex providers/roles generation engine, drift detection, marker-safe regeneration, `set_model`), `set_model.py` (`python -m` model-line-rewrite dispatch conduit), `hooks/worker_contract.py` (SubagentStart Worker-contract + PreToolUse(Agent) verbatim-Relay injector; `matches_generated_agent` anchored matcher lives in `generate.py`), `ops/supervision.py` (OS-native Proxy supervision — systemd user / launchd / WinSW template rendering, converge install/update/teardown, and installed/enabled/running/port-live status primitives through an injectable runner; standalone `port_live` + `loginctl` linger helpers), `data/profiles.json` (shipped seed Registry) |
| `commands/` | plugin slash-commands: `task.md` (`/chinamaxM:task` Claude dispatch protocol — profile-required, model never validated, named background spawn, verbatim Relay first copy) |
| `skills/` | Codex plugin skills: `chinamaxM-task/SKILL.md` (Codex dispatch twin — same `profile=`/`model=`/`name=` grammar, delegation-authorization sentence, `spawn_agent` of the bare role, `< /dev/null` headless close, parent-mediated steer via `send_input`/`followup_task`, ADR 0008 resume gotchas, verbatim Relay first copy) |
| `hooks/` | `hooks.json` (Claude: SubagentStart no-matcher + PreToolUse(Agent) → `scripts/worker_contract_hook`) and `codex-hooks.json` (Codex: SubagentStart no-matcher + PreToolUse(spawn_agent) → `scripts/codex_worker_contract_hook`, with `commandWindows` Git-Bash shims); both additive, `timeout: 10`; hosts-04 appends SessionStart |
| `scripts/` | `worker_contract_hook` (Claude shim) and `codex_worker_contract_hook` (Codex adapter shim: translates `spawn_agent`→`Agent` + `tool_input.agent_type`→`subagent_type`, then pipes to the shared module) — executable, same pinned interpreter order (`CHINAMAXM_PYTHON` → setup-recorded path → `conda run` last resort); `codex_hook_bash.cmd` (Windows Git-Bash wrapper for the Codex commandWindows shims) |
| `.codex-plugin/` | `plugin.json` — minimal Codex plugin manifest skeleton pointing `hooks` at `./hooks/codex-hooks.json` and `skills` at `./skills` (owned/extended by ops-02) |
| `tests/` | hermetic pytest suite: `conftest.py` (fake provider + fake Codex client + guards + fixtures), `test_registry.py`, `test_proxy.py` (passthrough + routing + relay), `test_seam.py` (Responses ingress + Seam translation + streaming + startup gates), `test_observability_count_tokens.py` (JSONL line fields + usage tee + count_tokens forward/estimator/cache), `test_generate.py` (generation engine: Claude agents/Codex providers/roles + drift classification + set_model, against temp roots), `test_hosts_claude.py` (`/task` command-text invariants + hooks.json registration + LIVE worker-contract/Relay hook invocation), `test_hosts_codex.py` (Codex skill-text invariants + LIVE codex-hooks shim invocation w/ payload translation + Claude↔Codex surface symmetry + `.codex-plugin` registration), `test_supervision.py` (per-OS unit rendering + space/% escaping, mocked-runner converge ops with exact-command assertions incl. WinSW stop/uninstall-before-replace + `sc.exe config … password=`, distinct status primitives with a real closed-port socket, config validation + linger helpers) |
