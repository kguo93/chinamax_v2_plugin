---
description: List the resolved chinamaxM Profiles and per-Host key presence (never a key value).
allowed-tools: Bash
---

List the resolved chinamaxM Profiles and show the output to the operator. It prints each
Profile's default model (plus the current model line when a dispatch rewrite made it differ),
dialect, key variable name, and PRESENT/MISSING per Host key file. It NEVER prints a key
value.

Run this via Bash and show the operator its complete output:

`"${CLAUDE_PLUGIN_ROOT}/scripts/chinamaxM" doctor --profiles`

An unreadable Registry prints one error line and exits nonzero.

## Degraded-launch fallback

If the command itself fails to launch — no interpreter resolves (the `chinamaxM` env does not
exist and no fallback rung matches) — report that plainly and point the operator at `/chinamaxM:setup`.
