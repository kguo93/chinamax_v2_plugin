---
description: Dispatch a task to a chinamaxM worker model as a named background subagent.
argument-hint: "profile=<name> [model=<any string>] [name=<worker>] <prompt…>"
allowed-tools: Agent, Bash
---

Dispatch one chinamaxM Worker. Usage:

`profile=<name> [model=<any string>] [name=<worker>] <prompt…>`

## Parse the arguments

- `profile=<name>` is REQUIRED. There is no default profile.
- Grammar: split leading `key=value` tokens off the front; the FIRST token that is not a
  `key=value` pair starts the prompt, and everything after it is prompt text — never
  parsed as an option. A duplicate key is an error. An empty prompt is an error.
- If `profile` is missing or names a profile that is not in the Registry, STOP and error,
  naming the valid Profile list (resolve it from the Registry — do not hardcode it).

## Resolve the model (NEVER validated)

The model string is NEVER validated and NEVER refused — the provider is the sole
authority.

- `model=` OMITTED ⇒ dispatch on the Profile's `default_model`, which the Generated agent
  already pins in its frontmatter. Do nothing extra.
- `model=<any string>` GIVEN ⇒ rewrite the Generated agent's model line in place FIRST,
  by running this via Bash, then spawn:

  `"${CLAUDE_PLUGIN_ROOT}/scripts/chinamaxM" set_model claude <profile> <model>`

  This makes the agent frontmatter `model: <profile>/<model>` (the Profile prefix routes
  on the Anthropic ingress). The rewrite→spawn window is per-dispatch and accepted;
  SessionStart regeneration resets the line to the default.
- A provider-side failure inside the Worker (an unknown model string included) is the
  Worker's final report: it relays back to you verbatim as the Worker's error — never
  swallow it, never rewrite it.

## Spawn the Worker

Make exactly one INLINE background `Agent` call with `subagent_type: "<profile>"` (the
bare Profile Generated agent — the ONLY artifact) and a `name`:

- Default `name` is `chinamaxm-<profile>-<task-slug>`, where `<task-slug>` is a short
  lowercase kebab slug (2–4 words) you derive from the prompt, so the name references
  chinamaxM and the task rather than a bare index (e.g.
  `chinamaxm-deepseek-repo-summary`). If that exact name is already in use this session —
  counting BOTH running and completed Workers as occupied (completed Workers stay
  addressable) — append `-2`, `-3`, … until it is free. If the prompt yields no sensible
  slug, fall back to `chinamaxm-<profile>-<n>`, where `<n>` is the lowest positive integer
  not already in use this session (same running-and-completed occupancy rule).
- A custom `name=` MUST be `chinamaxm-<profile>-` followed by a NON-EMPTY suffix,
  lowercase `[a-z0-9-]`; a duplicate custom name is an error. (A named spawn surfaces the
  NAME, not the subagent type, to the contract hook, and the hook matches on the
  `chinamaxm-<profile>-` prefix — so the name must carry it or the Worker contract never
  fires.)
- Append this delivery directive to the Worker's spawn prompt: when your work is complete,
  SendMessage to `main` with your complete final report in the message body. (The
  hook-injected Worker contract carries the same duty; stating it in the dispatch prompt is
  belt-and-braces, so the report never sits unseen in the Worker's own thread.)

HARD RULE: NEVER pass a `model` param on the `Agent` spawn call. The spawn `model`
parameter is enum-locked, and in the model-resolution order a per-spawn param would
override the frontmatter model line you just rewrote. Routing is structural through the
frontmatter alone; the spawn call carries no model.

## Relay the result

Relay rule: when a chinamaxM Worker's final report arrives, print it verbatim as your own output — no attribution, no summarizing, no re-doing the work. Print each Worker's report in arrival order. The report is the deliverable; the operator reads it as though you did the work yourself.

## Steer and continue

For as long as this main session lives, message the Worker by its `name` (SendMessage) to
steer it mid-run or to continue it after it completes — it behaves like any native
background subagent.

Dead-session recovery is refused: once the main session that spawned a Worker has ended,
the Worker is gone (ADR 0008). There is no resume machinery — do not improvise one. If a
task must outlive this session, dispatch it fresh from the new session.

## Raw dispatch request

$ARGUMENTS
