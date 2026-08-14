# scripts/ — conventions

- **Interpreter discovery lives ONLY in `_interpreter.sh`.** Never add a second
  resolution path anywhere — not in the launcher, not in a hook shim, not in a new
  script. One shared rung order is the whole reason this trio exists; a private copy is
  exactly the drift the last refactor removed.
- **Hook shims are fail-open**: always `exit 0`, never `set -e` (only `set -uo pipefail`).
  A hook fault must never block a session start or a tool call.
- **Keep `conda run --no-capture-output`** in every hook shim: plain `conda run` swallows
  stdin, so the module would read an empty event and inject nothing.
- **Bootstrap/ambient rungs (5–6) are launcher-only.** Hook shims stop at
  `chinamaxm_resolve_python` (rungs 1–3) plus the `conda run` fallback — they never fall
  through to the base-miniconda or ambient `python3`/`python` rungs.
- **`$CHINAMAXM_PYTHON` must point at an interpreter that can import `chinamaxM`.** No
  `PYTHONPATH` is set on rungs 1–4; only the bootstrap rungs (5–6) inject `src/`.
- **`codex_hook_bash.cmd` must never grow interpreter-discovery logic** — it only enters
  Git Bash and execs a shim by basename.

Inventory lives in `./repo-map.md`.
