"""chinamaxM Host hooks: stdin-JSON → stdout-JSON contract injectors.

The worker-contract hook (:mod:`chinamaxM.hooks.worker_contract`) serves two pinned
Claude events from one module: SubagentStart (inject the Worker contract into a spawned
Worker) and PreToolUse on the Agent tool (inject the verbatim Relay rule into the
dispatching parent). Both always exit 0 and fail open on any internal fault.
"""
