# Worker resume: live-session continuity, not post-mortem recovery

The resumability requirement is scoped to a **live main session**: for as long as the
original main session stays active, a Worker must remain continuously messageable — steer
it mid-run, and keep messaging it after it completes (each message re-seeds it from its
transcript with full context). This is native on both Hosts and requires no plugin
machinery:

- **Claude**: a Worker is a named background subagent; sending to its name mid-run
  steers, and after completion resumes it from its transcript (SendMessage semantics).
- **Codex**: parent-mediated per ADR 0007; child threads are additionally durable and
  re-attachable by thread id across OS processes (live-verified: a closed child was
  re-attached from a new `codex exec` ~15 minutes later with full recall). Resume
  gotchas are binding on any wrapper: always pin `-c model=` + `-c model_provider=`
  (resume forgets the recorded model and a failed attempt poisons the session's recorded
  model); `codex exec resume` takes `-c sandbox_mode=`, not `--sandbox`; `resume_agent`
  emits no event — never wait for one.

**Out of scope — decided, not deferred**: recovering Workers after the original main
session itself has died. Subagents of a dead session are in general not recoverable even
when they were host-native; chinamaxM does not attempt it. The sketch's V1 (worker
reattach across `claude --resume`) and its contemplated fallback (a `/task resume`
transcript re-seed skill) are both dissolved by this scoping — no re-seed machinery
ships. If a task must survive the main session, dispatch it fresh from the new session.
