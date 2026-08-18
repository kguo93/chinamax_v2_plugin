---
description: Diagnose the chinamaxM installation read-only — never spends tokens, never mutates.
allowed-tools: Bash
---

Run the chinamaxM doctor and show its full report to the operator. It is pure diagnosis:
free, local, token-less, mutation-less, Anthropic-surfaces-only.

Run this via Bash and show the operator its complete output:

`"${CLAUDE_PLUGIN_ROOT}/scripts/chinamaxM" doctor --host claude`

The doctor exits nonzero when at least one FAIL-level check failed; WARN-only and info
findings exit zero. Report the findings as-is — do not summarize away FAIL lines.

## Degraded-launch fallback

If the command itself fails to launch — no interpreter resolves (the `chinamaxM` env does not
exist and no fallback rung matches) — THAT failure IS the diagnosis: a doctor that runs inside the env it diagnoses cannot
render a finding when the env is absent. Report the launch failure plainly and point the
operator at `/chinamaxM:setup` to create the environment and install the Proxy.
