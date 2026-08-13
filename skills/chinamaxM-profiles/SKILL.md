---
name: chinamaxM-profiles
description: List the resolved chinamaxM Profiles and per-Host key presence (never a key value).
---

# List chinamaxM Profiles (Codex)

The Codex twin of `/chinamaxM:profiles`. List the resolved chinamaxM Profiles and show the
output to the operator. It prints each Profile's default model (plus the current model line
when a dispatch rewrite made it differ), dialect, key variable name, and PRESENT/MISSING per
Host key file. It NEVER prints a key value.

Run this and show the operator its complete output:

`conda run -n chinamaxM python -m chinamaxM.doctor --profiles`

For a headless run, close stdin by appending `< /dev/null` (`codex exec` blocks on an open
pipeline stdin):

`conda run -n chinamaxM python -m chinamaxM.doctor --profiles < /dev/null`

An unreadable Registry prints one error line and exits nonzero.

## Degraded-launch fallback

If the command itself fails to launch — `conda` is missing, or the `chinamaxM` env does not
exist — report that plainly and point the operator at `/chinamaxM:setup`.
