---
name: chinamaxM-setup
description: Set up chinamaxM — consent-gated diagnose → plan → approve → apply → re-diagnose → report. The one mutating surface.
---

# Set up chinamaxM (Codex)

The Codex twin of `/chinamaxM:setup`. This is the ONLY surface that mutates the machine, and
it mutates NOTHING until the operator approves the rendered plan by its digest.

Setup runs under the host's ambient Python (the `chinamaxM` conda env is what it CREATES, so
it cannot require it). Launch it with the plugin checkout's `src/` on `PYTHONPATH`, closing
stdin on any headless run (`codex exec` blocks on an open pipeline stdin):

`PYTHONPATH=<plugin-checkout>/src python3 -m chinamaxM.setup --plan-only < /dev/null`

## Flow — follow these steps in order

1. Run the command above. It DIAGNOSES the install, renders the ordered plan of every
   intended mutation, and prints a `Plan digest:` line. It exits 0 and changes nothing.
2. Show the operator the full plan and ask two SEPARATE questions (two distinct consents):
   - Approve applying this plan? (required to proceed)
   - Also run live paid probes? (a distinct opt-in — one minimal request per Profile per
     ingress; it spends tokens)
3. Only after the operator approves, apply with the digest from step 1 — append `--probes`
   only if they opted into probes:

   `PYTHONPATH=<plugin-checkout>/src python3 -m chinamaxM.setup --apply --plan-digest <digest> < /dev/null`

   Apply re-checks the plan digest and preconditions and ABORTS without mutating if anything
   drifted since step 1. Show the operator the full report verbatim — do not summarize away
   failures.

After (re)generating agents, tell the operator to restart open host sessions so the new
Worker agents are picked up.

## Teardown

Removes ONLY the env flip and the service; keys, agents, and Linux linger are left in place:

`PYTHONPATH=<plugin-checkout>/src python3 -m chinamaxM.setup --teardown < /dev/null`  (renders plan + digest)
`PYTHONPATH=<plugin-checkout>/src python3 -m chinamaxM.setup --teardown --plan-digest <digest> < /dev/null`  (applies)

## Notes

- Concurrency: single-operator only — never run setup concurrently (no cross-process lock in v0.1).
- No command path ever prints an API-key value; probe failures show only the parsed error type/message.
- Windows: supported. The service step acquires the WinSW wrapper automatically — it uses an
  operator-supplied `--winsw-exe <path>` if given, else a WinSW exe already installed under the
  service dir, else it downloads the pinned, SHA-256-verified official WinSW release (failing
  CLOSED on any checksum mismatch — an unverified binary is never installed; auto-download
  needs network at apply, and `--winsw-exe` is the offline escape hatch). Optionally add
  `--winsw-service-password-file <path>` to supply the service-account password (read from the
  file, never logged). NOTE: the Windows path is NOT live-verified on a real Windows host in
  this build (mocked-tested only).
