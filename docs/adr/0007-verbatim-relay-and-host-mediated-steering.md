# Verbatim no-attribution relay and Host-mediated steering

**Relay.** A Worker must end with a complete final report, and its parent must print that
report **verbatim as its own final output** — no "worker X says", no summarizing, no
re-doing the work: the operator reads it as though the parent did the work itself. This
is enforced by a hook-injected Worker contract on BOTH Hosts (bridge-style contracts +
hooks, carried from the old plugin's proven fidelity mechanism), not by a results skill —
none exists. The rationale is Codex-shaped but applied symmetrically: Codex's default
transcript shows only opaque `collab:` lines, leaving an unconstrained parent free to
paraphrase; the contract removes that freedom. The Worker's own transcript (Claude
subagent JSONL; Codex per-child rollout JSONL, whose `session_meta` records the resolved
provider) remains the audit trail when paraphrase is suspected.

**Steering.** The user-facing surface is symmetric — "message the worker" — per D18;
mediation differs by Host:

- **Claude**: native and direct. A Worker is a named background subagent; SendMessage to
  its name steers it or resumes it after completion.
- **Codex**: parent-mediated, because Codex rejects direct operator→child input at the
  app-server layer by design (PR #27173). The task skill instructs the parent to relay
  operator messages via `send_input` (with `interrupt=true` for a true mid-turn abort —
  `<turn_aborted>` lands in the child transcript, live-verified on 0.147.0) or
  `followup_task` for a new turn. This is an accepted platform asymmetry inside the
  symmetric surface. Watch: restoration of direct operator→subagent input
  (openai/codex#34591) would erase it.

**Amended 2026-08-13 (steer verb confirmed).** The old repo's working Codex adapter used
`send_message` for exact-addressed messages; this ADR pins `send_input`, live-verified on
0.147.0 — exactly the CLI version installed today — so `send_input` stands.
`send_message` is recorded as historical: possibly the same tool renamed, possibly a
distinct exact-addressed-message tool. hosts-03's live verification exercises
`send_input` for real during implementation and this ADR is amended only if that fails.
Additionally, per the 2026-08-13 grilling: a provider-side error inside a Worker's loop
(unknown model string included) is a FINAL REPORT for relay purposes — the parent
surfaces the provider's error verbatim to the operator/main session, never swallows or
rewrites it (cross-ref ADR 0001's unrewritten error relay).

**Amended 2026-08-18 (report DELIVERY is a Host-specific asymmetry inside the symmetric
Relay surface — push on Claude, pull on Codex).** The verbatim-Relay rule above governs how
the parent PRINTS a report; it never said how the report REACHES the parent, and a dispatched
Claude Worker was observed ending its turn with the report sitting only in its own subagent
thread, never delivered to `main`. Delivery is now pinned, and it differs by Host exactly as
steering does:

- **Claude (push).** The hook-injected `WORKER_CONTRACT_CLAUDE` gains item 5: the Worker
  delivers its final report by `SendMessage` to `main`, the report in the message body, never
  assuming main saw its thread. `commands/task.md` additionally has the dispatching parent
  append the same directive to the Worker's spawn prompt — the same canonical-string-plus-
  command-copy pattern the Relay rule already uses.
- **Codex (pull).** A Codex child cannot push to its parent (PR #27173, the same constraint
  behind the steering asymmetry), and Codex 0.147.0 exposes no wait/collect tool. So
  `WORKER_CONTRACT_CODEX` keeps the four shared duties with NO push item, and
  `skills/chinamaxM-task/SKILL.md` instructs the parent to PULL the Worker's final message
  from the child rollout JSONL under `$CODEX_HOME/sessions/` (this ADR's audit trail) and
  Relay it. Restating the report via `followup_task` is banned — it spends provider tokens
  and risks a paraphrase that breaks the verbatim guarantee.

The contract is selected by a Host token each shim passes to the shared
`worker_contract.py` module (`claude` / `codex`, default `claude`). The Relay rule itself
stays Host-independent and byte-identical on both surfaces. Cross-ref ADR 0004/0005 (as
amended 2026-08-18) for the Worker instance-name grammar changed in the same pass.
