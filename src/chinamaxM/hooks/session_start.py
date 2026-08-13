"""SessionStart hook: warn-only when the env flip is present but the Proxy port is dead.

Supervision is the OS's job (ADR 0009); this hook only nudges the operator toward
``/chinamaxM:doctor`` at session start when ``ANTHROPIC_BASE_URL`` points at a local Proxy
port that is not responding. It ignores stdin entirely, never blocks, and never exits
nonzero — ANY internal fault (missing/malformed settings, socket exception, import error)
is swallowed to a silent exit 0.

Behavior: read the settings env block from the resolved Claude root; if
``ANTHROPIC_BASE_URL`` is present AND its host normalizes to loopback (the shared
:func:`chinamaxM.doctor.normalize_flip_url` — a foreign or malformed URL is not chinamaxM's
flip, so no DNS lookup and no outbound connect), TCP-connect to that host:port with a
one-second timeout. A failed connect prints exactly one JSON object whose ``systemMessage``
points at ``/chinamaxM:doctor``; a present flip on a live port, an absent flip, a
non-loopback URL, or any error all print nothing.
"""

from __future__ import annotations

import json
import socket
import sys

#: The one warn-only message, pointing the operator at the doctor command.
_SYSTEM_MESSAGE = (
    "chinamaxM: ANTHROPIC_BASE_URL points at a local Proxy port that is not responding. "
    "Run /chinamaxM:doctor to diagnose (supervision should restart it automatically)."
)


def main() -> int:
    """Emit the warn-only message iff the flip is present but its loopback port is dead.

    Returns:
        ``0`` unconditionally (fail-open: never blocks a session, never exits nonzero).
    """
    try:
        from chinamaxM.doctor import normalize_flip_url
        from chinamaxM.keyfiles import resolve_host_root

        settings_path = resolve_host_root("claude") / "settings.json"
        doc = json.loads(settings_path.read_text(encoding="utf-8"))
        env = doc.get("env") if isinstance(doc, dict) else None
        url = env.get("ANTHROPIC_BASE_URL") if isinstance(env, dict) else None
        if not isinstance(url, str) or not url:
            return 0  # flip absent

        target = normalize_flip_url(url)
        if target is None or not target.is_loopback or target.host is None or target.port is None:
            return 0  # not chinamaxM's flip (non-loopback / malformed) — no connect attempted

        try:
            with socket.create_connection((target.host, target.port), timeout=1.0):
                return 0  # port alive — nothing to warn about
        except OSError:
            pass  # connect failed — the Proxy port is dead

        sys.stdout.write(json.dumps({"systemMessage": _SYSTEM_MESSAGE}) + "\n")
    except Exception:  # noqa: BLE001 - a warn-only hook must never block or fail a session
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
