"""SessionStart hook: warn-only when the Proxy port looks down but the Host is wired for it.

Supervision is the OS's job (ADR 0009); this hook only nudges the operator toward the doctor
surface at session start. It is HOST-AWARE (ADR 0005/0009), resolving the Host from the hook
environment — Codex sets ``PLUGIN_ROOT``/``CODEX_*``; Claude sets
``CLAUDE_PLUGIN_ROOT``/``CLAUDE_CONFIG_DIR`` — because the two Hosts wire routing differently:

* **Claude**: routing rides the ``env.ANTHROPIC_BASE_URL`` flip in ``settings.json``. Warn iff
  the flip is present AND its loopback port is dead; point at ``/chinamaxM:doctor``.
* **Codex**: there is no ``settings.json`` flip — routing rides the generated
  ``model_providers.chinamaxM-<profile>`` entries in ``~/.codex/config.toml`` that point at the
  Proxy port. Warn iff chinamaxM is configured for Codex (a generated provider entry is present)
  AND the Registry Proxy port is dead; point at the ``chinamaxM-doctor`` skill.

Both paths are strictly fail-open: the hook ignores stdin, never blocks, and never exits
nonzero — ANY internal fault (missing/malformed settings or config, a socket exception, an
import error) is swallowed to a silent exit 0.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

#: The Claude warn-only message, pointing the operator at the doctor command.
_CLAUDE_SYSTEM_MESSAGE = (
    "chinamaxM: ANTHROPIC_BASE_URL points at a local Proxy port that is not responding. "
    "Run /chinamaxM:doctor to diagnose (supervision should restart it automatically)."
)

#: The Codex warn-only message, pointing the operator at the doctor skill.
_CODEX_SYSTEM_MESSAGE = (
    "chinamaxM: the local Proxy port is not responding, but chinamaxM is configured for Codex. "
    "Run the chinamaxM-doctor skill to diagnose (supervision should restart it automatically)."
)


def main() -> int:
    """Emit the Host-appropriate warn-only message iff the Proxy port looks down.

    Returns:
        ``0`` unconditionally (fail-open: never blocks a session, never exits nonzero).
    """
    try:
        message = _codex_warning() if _resolve_host() == "codex" else _claude_warning()
        if message:
            sys.stdout.write(json.dumps({"systemMessage": message}) + "\n")
    except Exception:  # noqa: BLE001 - a warn-only hook must never block or fail a session
        return 0
    return 0


def _resolve_host() -> str:
    """Resolve the Host from the hook environment (Claude signals win over Codex signals)."""
    if os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("CLAUDE_CONFIG_DIR"):
        return "claude"
    if os.environ.get("PLUGIN_ROOT") or os.environ.get("CODEX_HOME"):
        return "codex"
    return "claude"


def _claude_warning() -> str | None:
    """Return the Claude warn message iff the settings flip is present but its port is dead."""
    from chinamaxM.doctor import normalize_flip_url
    from chinamaxM.keyfiles import resolve_host_root

    settings_path = resolve_host_root("claude") / "settings.json"
    doc = json.loads(settings_path.read_text(encoding="utf-8"))
    env = doc.get("env") if isinstance(doc, dict) else None
    url = env.get("ANTHROPIC_BASE_URL") if isinstance(env, dict) else None
    if not isinstance(url, str) or not url:
        return None  # flip absent

    target = normalize_flip_url(url)
    if target is None or not target.is_loopback or target.host is None or target.port is None:
        return None  # not chinamaxM's flip (non-loopback / malformed) — no connect attempted

    if _port_alive(target.host, target.port):
        return None  # port alive — nothing to warn about
    return _CLAUDE_SYSTEM_MESSAGE


def _codex_warning() -> str | None:
    """Return the Codex warn message iff chinamaxM is Codex-configured but its Proxy port is dead.

    "Configured for Codex" means the resolved Codex root's ``config.toml`` carries at least one
    generated ``model_providers.chinamaxM-<profile>`` entry (the routing surface the Codex Host
    rides — there is no ``settings.json`` flip). The Proxy port is the Registry ``port``.
    """
    from chinamaxM.generate import CONFIG_FILE_NAME, PROVIDER_PREFIX
    from chinamaxM.keyfiles import resolve_host_root
    from chinamaxM.registry import load_registry

    config_path = resolve_host_root("codex") / CONFIG_FILE_NAME
    if not _codex_configured(config_path, PROVIDER_PREFIX):
        return None  # chinamaxM is not wired into Codex — nothing to warn about

    port = load_registry().port
    if _port_alive("127.0.0.1", port):
        return None  # port alive — nothing to warn about
    return _CODEX_SYSTEM_MESSAGE


def _codex_configured(config_path: Path, provider_prefix: str) -> bool:
    """Return whether ``config.toml`` carries a generated ``chinamaxM-`` provider entry."""
    import tomllib

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        parsed = tomllib.loads(text)
    except Exception:  # noqa: BLE001 - an unparseable config is "not configured", never a crash
        return False
    providers = parsed.get("model_providers") if isinstance(parsed, dict) else None
    return isinstance(providers, dict) and any(
        str(key).startswith(provider_prefix) for key in providers
    )


def _port_alive(host: str, port: int) -> bool:
    """TCP-connect to ``host:port`` with a one-second timeout; True when it accepts."""
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
