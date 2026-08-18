"""Per-Host Key files: the mtime-cached reader and the O_EXCL comments-only scaffold.

Each Host owns a ``model-keys.env`` under its root (ADR 0006). The Proxy is the only
reader of key VALUES; it injects a Profile's key on every worker-bound request, choosing
the file that matches the request's ingress Host (Anthropic ingress → Claude file). Roots
resolve through the one canonical chain shared with every Host surface (ADR 0006 as
amended), so the running Proxy and doctor/setup always agree on where keys live.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Literal, Mapping

#: The per-Host Key-file basename, beneath the resolved Host root.
KEY_FILE_NAME = "model-keys.env"

#: The Hosts a surface can act for, in the shared resolver's canonical order.
_HOSTS = ("claude", "codex")


class HostResolutionError(ValueError):
    """Raised when the invoking Host cannot be resolved (never guessed).

    Carried by every Host-scoped surface (setup / doctor / generation) so an
    ambiguous or invalid Host request fails loudly rather than silently acting for
    the wrong Host. The SessionStart hook catches it and exits silently (fail-open).
    """


def resolve_host(
    explicit: str | None = None, environ: Mapping[str, str] | None = None
) -> str:
    """Resolve the single invoking Host by the ladder (ADR 0005 as amended 2026-08-18).

    The ladder, first match wins: an explicit ``--host`` value → the ``CHINAMAXM_HOST``
    env marker → Codex plugin evidence → Claude plugin evidence → a hard error. Codex
    evidence outranks Claude's because Codex exposes Claude-compatible env aliases, so a
    Claude-first probe would misread a Codex session as Claude. The marker is
    ``CHINAMAXM_HOST`` — v1 owns ``CHINAMAX_*`` and the namespaces stay disjoint.

    Args:
        explicit: A caller-supplied Host (``--host``); validated and wins over everything.
        environ: The environment mapping to read (defaults to ``os.environ``).

    Returns:
        ``"claude"`` or ``"codex"``.

    Raises:
        HostResolutionError: On an invalid ``explicit`` / marker value, or when no rung
            of the ladder resolves.
    """
    env = os.environ if environ is None else environ

    if explicit is not None and explicit.strip():
        host = explicit.strip().lower()
        if host not in _HOSTS:
            raise HostResolutionError(f"unknown Host {explicit!r}; require --host claude|codex")
        return host

    marker = env.get("CHINAMAXM_HOST", "")
    if marker and marker.strip():
        host = marker.strip().lower()
        if host not in _HOSTS:
            raise HostResolutionError(f"invalid CHINAMAXM_HOST {marker!r}; require claude or codex")
        return host

    if any(env.get(name, "").strip() for name in ("PLUGIN_ROOT", "PLUGIN_DATA", "CODEX_HOME")):
        return "codex"
    if any(env.get(name, "").strip() for name in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_CONFIG_DIR")):
        return "claude"
    raise HostResolutionError(
        "cannot determine chinamaxM Host; pass --host claude|codex or set CHINAMAXM_HOST"
    )


def resolve_host_root(
    host: Literal["claude", "codex"], override: str | os.PathLike[str] | None = None
) -> Path:
    """Resolve a Host's root directory through the canonical chain (ADR 0006 as amended).

    The chain per Host is: explicit ``override`` → ``CHINAMAXM_CLAUDE_HOME`` /
    ``CHINAMAXM_CODEX_HOME`` → ``$CLAUDE_CONFIG_DIR`` / ``$CODEX_HOME`` → ``~/.claude`` /
    ``~/.codex``. An override replaces the whole Host root.

    Args:
        host: ``"claude"`` or ``"codex"``.
        override: An explicit root that wins over every environment variable.

    Returns:
        The resolved Host root (which may not exist yet).
    """
    if override is not None:
        return Path(override)
    if host == "claude":
        env = os.environ.get("CHINAMAXM_CLAUDE_HOME") or os.environ.get("CLAUDE_CONFIG_DIR")
        default = "~/.claude"
    else:
        env = os.environ.get("CHINAMAXM_CODEX_HOME") or os.environ.get("CODEX_HOME")
        default = "~/.codex"
    return Path(env or os.path.expanduser(default))


def _parse_key_file(text: str) -> dict[str, str]:
    """Parse env-format Key-file text into a name → value mapping.

    Parse rules (ADR 0006): full-line ``#`` comments only (no inline comments); lines
    without ``=`` ignored; whitespace around key and value stripped; a single matching
    pair of surrounding single/double quotes stripped; duplicate keys last-wins; an empty
    value is kept but treated as missing at lookup.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, raw = stripped.partition("=")
        if not separator:
            continue
        name = name.strip()
        if not name:
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[name] = value
    return values


class KeyFileReader:
    """Reads one Host's Key file with an ``(st_mtime_ns, st_size)`` cache.

    A missing or unreadable file resolves to an empty mapping and resets the cache, so the
    injector fails CLOSED (a 401) rather than reusing a stale key. Values are never logged.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache_key: tuple[int, int] | None = None
        self._values: dict[str, str] = {}

    def get(self, var: str) -> str:
        """Return the current value for ``var``, or ``""`` when absent or empty.

        Re-reads the file only when its ``(st_mtime_ns, st_size)`` changed since last read.
        """
        self._refresh()
        return self._values.get(var, "")

    def _refresh(self) -> None:
        """Reload the values if the file's ``(st_mtime_ns, st_size)`` changed."""
        try:
            info = self.path.stat()
        except OSError:
            self._cache_key = None
            self._values = {}
            return
        cache_key = (info.st_mtime_ns, info.st_size)
        if cache_key == self._cache_key:
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            self._cache_key = None
            self._values = {}
            return
        self._values = _parse_key_file(text)
        self._cache_key = cache_key


def _template_text(api_key_env_names: Iterable[str]) -> str:
    """Return the comments-only Key-file template listing each name (no values)."""
    lines = [
        "# chinamaxM per-Host API keys (ADR 0006).",
        "# Uncomment a line and paste the provider key; one VAR=value per Profile.",
        "# Values are never printed by any command — the Proxy is the only reader.",
    ]
    lines.extend(f"# {name}=" for name in dict.fromkeys(api_key_env_names))
    return "\n".join(lines) + "\n"


def scaffold_key_file(path: Path, api_key_env_names: Iterable[str]) -> str:
    """Create a mode-0600 comments-only Key file if absent; never touch an existing one.

    The template lists every ``api_key_env`` of the loaded Registry (never a hard-coded
    set) as comments and no values. Creation is ``O_EXCL`` so a concurrent creator is never
    clobbered, and the template is written with a single ``os.write`` (a truncated template
    would be cosmetic only — the reader ignores comment lines regardless).

    Args:
        path: The Key-file path.
        api_key_env_names: The Registry's ``api_key_env`` names, in load order.

    Returns:
        ``"created"`` when the file was written, ``"exists"`` when already present.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return "exists"
    try:
        os.write(descriptor, _template_text(api_key_env_names).encode("utf-8"))
    finally:
        os.close(descriptor)
    return "created"
