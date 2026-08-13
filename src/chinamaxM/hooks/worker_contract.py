"""Worker-contract hook: inject the final-report contract and the verbatim Relay rule.

One module serves two pinned Claude events (ADR 0005/0007), branching on the stdin
``hook_event_name`` and always writing ``{"hookSpecificOutput": {...}}`` to stdout with
exit 0:

* **SubagentStart** (worker-side): its ``additionalContext`` is delivered to the spawned
  Worker. When the top-level ``agent_type`` names a generated Worker, inject
  :data:`WORKER_CONTRACT` — the numbered final-report duty.
* **PreToolUse** on the Agent tool (parent-side): fires in the dispatching parent's turn
  on the spawn call. When ``tool_input.subagent_type`` names a generated Worker, inject
  :data:`RELAY_RULE` — the verbatim, no-attribution Relay rule (the same string the
  ``/chinamaxM:task`` command carries as its operative first copy).

Matching is anchored and Registry-derived via the single shared
:func:`chinamaxM.generate.matches_generated_agent` seam (never re-implemented here). The
Registry loads per invocation; the test-only ``CHINAMAXM_OVERLAY_PATH`` env var maps onto
the loader's explicit ``overlay_path`` argument (unset ⇒ the loader's default). Fail-open
rule: any internal fault (bad stdin, a missing or malformed Registry) writes a stderr
diagnostic, exits 0, and injects nothing.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from chinamaxM.generate import matches_generated_agent
from chinamaxM.registry import load_registry

#: The numbered, token-lean Worker contract injected into a spawned Worker at
#: SubagentStart. This enumerated text IS the canonical contract (ADR 0007): the Worker
#: ends with one complete final report, and its final message alone is what the parent
#: Relays verbatim.
WORKER_CONTRACT = (
    "chinamaxM Worker contract:\n"
    "1. Do the task the parent gave you — nothing more, nothing less.\n"
    "2. End with ONE complete, self-contained final report as your last message: what "
    "you did, the outcome, and every path or result the operator needs.\n"
    "3. Never end with a question unless you are genuinely blocked and cannot proceed.\n"
    "4. Your final message IS the deliverable — the parent prints it verbatim, so it "
    "must stand alone."
)

#: The one canonical Relay rule injected into the dispatching parent at
#: PreToolUse(Agent). The ``/chinamaxM:task`` command carries this exact string as its
#: operative first copy (drift-guarded by the command-text test), so hook delivery
#: reinforces rather than solely carries it. Contains the ADR 0007 invariants: verbatim,
#: no attribution, as your own output, arrival order, report-is-the-deliverable.
RELAY_RULE = (
    "Relay rule: when a chinamaxM Worker's final report arrives, print it verbatim as "
    "your own output — no attribution, no summarizing, no re-doing the work. Print each "
    "Worker's report in arrival order. The report is the deliverable; the operator reads "
    "it as though you did the work yourself."
)


def _profile_names() -> list[str]:
    """Load the Registry (honoring the test-only Overlay override) and return its names.

    Returns:
        The resolved Registry's Profile names, in load order.
    """
    raw = os.environ.get("CHINAMAXM_OVERLAY_PATH")
    overlay_path = Path(raw) if raw else None
    registry = load_registry(overlay_path=overlay_path)
    return list(registry.profiles)


def _emit(event_name: str, context: str) -> None:
    """Write the pinned ``additionalContext`` injection JSON to stdout."""
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": context,
                }
            }
        )
        + "\n"
    )


def main() -> int:
    """Read one hook event from stdin, inject when it matches, and always exit 0.

    Returns:
        ``0`` unconditionally. A non-matching event injects nothing; any internal fault
        emits a stderr diagnostic and still returns 0 (fail-open).
    """
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("hook event must be a JSON object")
        event_name = event.get("hook_event_name")

        if event_name == "SubagentStart":
            agent_type = event.get("agent_type")
            if isinstance(agent_type, str) and matches_generated_agent(
                agent_type, _profile_names()
            ):
                _emit("SubagentStart", WORKER_CONTRACT)
        elif event_name == "PreToolUse":
            if event.get("tool_name") != "Agent":
                return 0
            tool_input = event.get("tool_input")
            subagent_type = (
                tool_input.get("subagent_type") if isinstance(tool_input, dict) else None
            )
            if isinstance(subagent_type, str) and matches_generated_agent(
                subagent_type, _profile_names()
            ):
                _emit("PreToolUse", RELAY_RULE)
    except Exception as exc:  # noqa: BLE001 - never block a tool call
        print(
            f"chinamaxM worker_contract hook: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
