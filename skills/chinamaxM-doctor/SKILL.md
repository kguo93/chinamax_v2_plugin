---
name: chinamaxM-doctor
description: Diagnose the chinamaxM installation read-only — never spends tokens, never mutates.
---

# Diagnose chinamaxM (Codex)

The Codex twin of `/chinamaxM:doctor`. Run the chinamaxM doctor and show its full report to
the operator. It is pure diagnosis: free, local, token-less, mutation-less,
Anthropic-surfaces-only.

Run this and show the operator its complete output:

`conda run -n chinamaxM python -m chinamaxM.doctor`

For a headless run, close stdin by appending `< /dev/null` (`codex exec` blocks on an open
pipeline stdin):

`conda run -n chinamaxM python -m chinamaxM.doctor < /dev/null`

The doctor exits nonzero when at least one FAIL-level check failed; WARN-only and info
findings exit zero. Report the findings as-is — do not summarize away FAIL lines.

## Degraded-launch fallback

If the command itself fails to launch — `conda` is missing, or the `chinamaxM` env does not
exist — THAT failure IS the diagnosis: a doctor that runs inside the env it diagnoses cannot
render a finding when the env is absent. Report the launch failure plainly and point the
operator at `/chinamaxM:setup`.
