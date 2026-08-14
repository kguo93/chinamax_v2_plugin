---
description: Set up chinamaxM — detect the platform prerequisites (bash / Git for Windows / Miniconda) and pause for approval before installing any that are missing, then the consent-gated diagnose → plan → approve → apply → re-diagnose → report. The one mutating surface.
allowed-tools: Bash
---

Set up (or tear down) the chinamaxM installation. This is the ONLY surface that mutates the
machine, and it mutates NOTHING until the operator approves.

Setup runs under the host's ambient Python (the `chinamaxM` conda env is what it CREATES, so
it cannot require it). Launch it with the plugin source on the path:

`PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/src" python3 -m chinamaxM.setup --plan-only`

## Flow — follow these steps in order

1. Run the command above. `--plan-only` DIAGNOSES the install and prints ONE of two things — it
   exits 0 and changes nothing either way:
   - a **prerequisite pause** — a "Missing platform prerequisites" section and NO `Plan digest:`
     line — when a Platform Prerequisite (`bash` / Git for Windows / Miniconda) is missing; or
   - the **normal plan** — an ordered step list ending in a `Plan digest:` line — when every
     prerequisite is present.

### A. The plan-only output is a prerequisite pause (no `Plan digest:` line)

A Platform Prerequisite is missing. The engine NEVER downloads or runs an installer — the host
does, on approval. Do not summarize; the operator acts on the specific rows.

2. Show each rectification row: name, summary, install_location, run_policy, commands. Warn that
   a `miniconda` row runs `conda init`, which edits shell startup files.
3. Ask exactly: reply "approve" to install these, anything else to stop.
4. Reply is not "approve" → stop. Report that no prerequisite was installed. (Do NOT claim
   nothing changed — the earlier diagnose already probed state.)
5. Reply is "approve" → for each row IN ORDER, dispatch by `run_policy`, running every command
   through the row's `shell` (a `cmd` row via `cmd /c`; a `powershell`/`native` row natively —
   never in Git Bash; a `bash` row via bash):
   - `agent` → run its commands.
   - `privileged` → run `sudo -n true`; on success run the commands; on failure ask the operator
     to run the shown command themselves (type `! <command>` in the prompt), then wait.
   - `operator` → hand the summary to the operator, wait for them to install manually, run none
     of the (empty) commands.
   Stop-on-first-failure: if any command exits non-zero, run no remaining command in that row
   (no `conda init`) AND stop the whole flow — attempt no later row, do not re-run. Report the
   exact failed command and its exit status.
   Each command still triggers the host's normal permission prompt; the word "approve" is textual
   consent, not a bypass. Do not widen `allowed-tools`.
6. Re-run `--plan-only` once (a fresh Bash call). Still paused → report and stop. Never loop.
   Otherwise the normal plan now prints — continue with section B.

Windows-only: if the launcher itself cannot start (bash or python missing), run these natively
in cmd.exe, then return to step 1:

```text
winget install --id Git.Git -e --silent --accept-source-agreements --accept-package-agreements
curl.exe -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe -o "%TEMP%\chinamaxM-miniconda.exe"
start /wait "" "%TEMP%\chinamaxM-miniconda.exe" /InstallationType=JustMe /RegisterPython=0 /AddToPath=0 /S /D=%USERPROFILE%\miniconda3
"%USERPROFILE%\miniconda3\Scripts\conda.exe" init cmd.exe powershell bash
```

### B. The plan-only output is the normal plan (ends in a `Plan digest:` line)

7. Show the operator the full plan and ask two SEPARATE questions (two distinct consents):
   - Approve applying this plan? (required to proceed)
   - Also run live paid probes? (a distinct opt-in — one minimal request per Profile per
     ingress; it spends tokens)
8. Only after the operator approves, apply with the digest from step 1 — append `--probes` only
   if they opted into probes:

   `PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/src" python3 -m chinamaxM.setup --apply --plan-digest <digest>`

   Apply re-checks the plan digest and preconditions and ABORTS without mutating if anything
   drifted since step 1. Show the operator the full report (diagnosis before/after, per-step
   outcomes, probe results) verbatim — do not summarize away failures.

After (re)generating agents, tell the operator to restart open host sessions so the new Worker
agents are picked up.

## Teardown

Removes ONLY the env flip and the service; keys, agents, and Linux linger are left in place:

`PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/src" python3 -m chinamaxM.setup --teardown`  (renders plan + digest)
`PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/src" python3 -m chinamaxM.setup --teardown --plan-digest <digest>`  (applies)

## Notes

- Concurrency: single-operator only — never run setup concurrently (no cross-process lock in v0.1).
- No command path ever prints an API-key value; probe failures show only the parsed error type/message.
- Prerequisites: setup DETECTS `bash` / Git for Windows / Miniconda and, when any is missing,
  PAUSES with agent-run rectification rows before any mutation — the engine never downloads or
  runs an installer, and needs no pre-installed Python (on a bare Windows box the zero-state block
  above bootstraps Git and Miniconda from cmd.exe). Git for Windows is bootstrapped because the
  Codex hooks need its bash. A `miniconda` row installs `Miniconda3-latest-*` (NO version pin, NO
  checksum) and runs `conda init`, which EDITS your shell startup files (both disclosed in the
  row's summary); it is idempotent (reuses an existing `~/miniconda3`). On an unsupported CPU
  architecture the miniconda row is advice-only — install Miniconda yourself, then re-run.
- Windows: supported. The service step acquires the WinSW wrapper automatically — it uses an
  operator-supplied `--winsw-exe <path>` if given, else a WinSW exe already installed under the
  service dir, else it downloads the pinned, SHA-256-verified official WinSW release (failing
  CLOSED on any checksum mismatch — an unverified binary is never installed; auto-download needs
  network at apply, and `--winsw-exe` is the offline escape hatch). Optionally add
  `--winsw-service-password-file <path>` to supply the service-account password (read from the
  file, never logged). NOTE: the Windows path is NOT live-verified on a real Windows host in this
  build (mocked-tested only).
