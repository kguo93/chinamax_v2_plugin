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
