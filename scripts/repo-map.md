# scripts/ — inventory

| Path | What it is |
|---|---|
| `chinamaxM` | the CLI Launcher (executable): maps `{setup\|doctor}` to its module, sources `_interpreter.sh`, and `chinamaxm_exec`s the module under the resolved interpreter; argv passes through verbatim after the subcommand. Every host command/skill surface runs plugin Python through this shim |
| `_interpreter.sh` | THE shared interpreter-discovery library (sourced, NOT executable, no shebang): the pinned rungs — recorded `<claude-root>/chinamaxM/python-path` → `$CHINAMAXM_PYTHON` → `~/miniconda3/envs/chinamaxM` python → validated `conda run -n chinamaxM` → base `~/miniconda3` python + `src/` on `PYTHONPATH` → ambient `python3`/`python` + `src/`; plus the macOS CLT-stub kick-back. `chinamaxm_resolve_python` (rungs 1–3) + `chinamaxm_conda_cmd` are the hook-shim entry points; `chinamaxm_exec` adds the launcher-only bootstrap/ambient rungs |
| `session_start_hook` | SessionStart shim for BOTH Hosts (executable) → the host-aware `chinamaxM.hooks.session_start`; sources `_interpreter.sh` (rungs 1–3 + fail-open `conda run --no-capture-output`), fail-open `exit 0` |
| `worker_contract_hook` | Claude worker-contract shim (executable) → `chinamaxM.hooks.worker_contract`; sources `_interpreter.sh`, fail-open `exit 0` |
| `codex_worker_contract_hook` | Codex adapter shim (executable): translates `spawn_agent`→`Agent` + `tool_input.agent_type`→`subagent_type` via a two-stage pipeline, then pipes to the shared module; sources `_interpreter.sh`, fail-open `exit 0` |
| `codex_hook_bash.cmd` | Windows Git-Bash wrapper for the Codex `commandWindows` shims — resolves Git Bash from the Git-for-Windows install tree (never PATH) and execs a shim by basename; carries NO interpreter-discovery logic |
| `CLAUDE.md` | scripts conventions (one-resolver rule, fail-open, launcher-only bootstrap rungs) |
| `AGENTS.md` | stub pointing Codex/other agents at `CLAUDE.md` |
| `repo-map.md` | this inventory |
