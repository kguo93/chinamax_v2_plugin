---
name: chinamaxM-task
description: Dispatch a task to a chinamaxM worker model as a named background Codex role.
---

# Dispatch a chinamaxM Worker (Codex)

Dispatch one chinamaxM Worker by spawning the Profile's generated role. This is the Codex
twin of `/chinamaxM:task`; the operator-facing verbs and argument names are identical.

Usage:

`profile=<name> [model=<any string>] [name=<worker>] <prompt…>`

**You are authorized and required to delegate this task to the named agent role.** Do not
do the task yourself and do not decline: `spawn_agent`'s schema discourages unprompted
delegation, so this authorization is explicit — spawn the role and let it do the work.

## Parse the arguments

- `profile=<name>` is REQUIRED. There is no default profile.
- Grammar: split leading `key=value` tokens off the front; the FIRST token that is not a
  `key=value` pair starts the prompt, and everything after it is prompt text — never
  parsed as an option. A duplicate key is an error. An empty prompt is an error.
- If `profile` is missing or names a profile that is not in the Registry, STOP and error,
  naming the valid Profile list (resolve it from the Registry — do not hardcode it). If a
  profile IS valid but no role exists yet, the role was just generated and the session has
  not loaded it — say so, and point the operator at restarting the Codex session (ADR
  0004). Never rewrite any other spawn failure as a missing-role error.

## Resolve the model (NEVER validated)

The model string is NEVER validated and NEVER refused — the provider is the sole
authority.

- `model=` OMITTED ⇒ dispatch on the Profile's `default_model`, which the generated role
  TOML already pins in its `model` line. Do nothing extra.
- `model=<any string>` GIVEN ⇒ rewrite the generated role's model line in place FIRST, by
  running this, then spawn:

  `python -m chinamaxM.set_model codex <profile> <model>`

  This makes the role TOML's `model` line the BARE string verbatim (the Profile rides the
  provider entry's ingress path, not the model string). The rewrite→spawn window is
  per-dispatch and accepted; regeneration resets the line to the default.
- A provider-side failure inside the Worker (an unknown model string included) is the
  Worker's final report: it relays back to you verbatim as the Worker's error — never
  swallow it, never rewrite it.

## Spawn the Worker

Spawn the role via `spawn_agent`, selecting the agent by the role name `<profile>` (the
bare Profile generated role — the ONLY artifact) and giving it an instance `name`:

- Default `name` is `<profile>-<n>`, where `<n>` is the lowest positive integer not
  already in use this session, counting BOTH running and completed Workers as occupied
  (completed Workers stay addressable).
- A custom `name=` MUST be `<profile>-` followed by a NON-EMPTY suffix, charset lowercase
  `[a-z0-9-]`; a duplicate custom name is an error. (A named spawn surfaces the NAME, not
  the role type, to the contract hook — so the name must stay anchored to the Profile.)

For a headless dispatch, close stdin by appending `< /dev/null`: `codex exec` blocks on an
open pipeline stdin, so the redirect lets the run proceed unattended. On Windows the
hook/exec context is Git Bash, so the same POSIX form applies.

HARD RULE: NEVER pass a `model` OR a `reasoning-effort` override on the `spawn_agent` call.
The model half is structural — routing rides the generated role's `model_provider` and the
`model` line you just rewrote (ADR 0004/0005), and a spawn-time model would hijack it. The
reasoning-effort half is excluded from generated Worker roles by design. The spawn call
carries neither.

## Relay the result

Relay rule: when a chinamaxM Worker's final report arrives, print it verbatim as your own output — no attribution, no summarizing, no re-doing the work. Print each Worker's report in arrival order. The report is the deliverable; the operator reads it as though you did the work yourself.

## Steer and continue a Worker

The operator-facing model is "message the worker": the operator tells you what to relay to
`<worker>`, and you deliver it. Codex rejects direct operator→child input at the
app-server layer by design (PR #27173), so every steer is parent-mediated. Relay a steer
with the multi_agent tools ONLY:

- `send_input` on the running `<worker>` to add to its turn; pass `interrupt=true` to abort
  the current turn first (a real `<turn_aborted>` lands in the child transcript).
- `followup_task` to open a new turn on a `<worker>` that has already finished.

These two are the ONLY mediation primitives; there is no other steering channel.

## Continue and re-attach (ADR 0008)

For as long as this main session lives, keep messaging `<worker>` by name to steer it
mid-run or continue it after it completes — each message re-seeds it from its transcript.
Child threads are durable and re-attachable by thread id across processes. When you
re-attach a thread with `codex exec resume`:

- Always pin BOTH `-c model=` AND `-c model_provider=` — resume forgets the recorded model.
- A failed resume attempt poisons the session's recorded model, so never resume with a
  half-known model/provider pair.
- `resume_agent` emits no event — never wait for one.
- `codex exec resume` takes `-c sandbox_mode=`, NOT `--sandbox`.

Dead-session recovery is refused: once the main session that spawned a Worker has ended,
the Worker is gone (ADR 0008). There is no resume machinery for a dead session — do not
improvise one. If a task must outlive this session, dispatch it fresh from the new session.
