"""Read-modify-write editor for the Claude ``settings.json`` env flip (ADR 0005/0006).

Only the ``env.ANTHROPIC_BASE_URL`` key is ours. Every operation fails CLOSED — it raises
:class:`SettingsError` and never rewrites — on unparseable JSON, a non-object root, a
present-but-non-dict ``env`` block, or a present-but-non-string ``ANTHROPIC_BASE_URL``; a
MISSING ``env`` key is CREATED, not an error. Writes are atomic (a temp file in the same
directory plus :func:`os.replace`), mode-preserving, and target the symlink-resolved path;
a brand-new file is created mode ``0600``. The serializer is stdlib ``json`` preserving key
order and the file's detected indent (2-space fallback).

This module never reads or writes a Key file, so no key value can pass through it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Callable

#: The single settings key this module owns.
FLIP_KEY = "ANTHROPIC_BASE_URL"

#: Mode for a settings.json this module creates from scratch.
_NEW_FILE_MODE = 0o600

#: Fallback indent (spaces) when the file's own indent cannot be detected.
_DEFAULT_INDENT = 2

_INDENT_RE = re.compile(r"\n([ \t]+)\S")


class SettingsError(Exception):
    """Raised on a fail-closed settings.json fault (never on a benign absent file/key)."""


def _resolve(path: str | os.PathLike[str]) -> Path:
    """Resolve symlinks in the target so writes replace the real file, not the link."""
    return Path(os.path.realpath(os.fspath(path)))


def _parse_or_raise(text: str, resolved: Path) -> dict:
    """Parse settings text into an object, failing closed on any malformed shape."""
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise SettingsError(f"settings.json is not valid JSON: {resolved}") from exc
    if not isinstance(doc, dict):
        raise SettingsError(f"settings.json root is not a JSON object: {resolved}")
    if "env" in doc and not isinstance(doc["env"], dict):
        raise SettingsError(f"settings.json 'env' is not an object: {resolved}")
    return doc


def _detect_indent(text: str) -> int | str:
    """Return the file's indent unit (int spaces or ``"\\t"``), or the 2-space fallback."""
    match = _INDENT_RE.search(text)
    if not match:
        return _DEFAULT_INDENT
    prefix = match.group(1)
    if prefix.startswith("\t"):
        return "\t"
    return len(prefix)


def _atomic_write(resolved: Path, data: bytes, *, mode: int) -> None:
    """Write ``data`` to ``resolved`` atomically, applying ``mode`` to the final file."""
    resolved.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(resolved.parent), prefix=".settings-", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temp_path, mode)
        os.replace(temp_path, resolved)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def read_flip(path: str | os.PathLike[str]) -> str | None:
    """Return the current ``env.ANTHROPIC_BASE_URL`` string, or ``None`` when absent.

    An absent file, a missing ``env`` block, or a missing key all resolve to ``None`` (a
    benign "no current flip" state, never an error). Fails closed only on a malformed file.

    Raises:
        SettingsError: On unparseable JSON, a non-object root, a non-dict ``env`` block, or
            a present-but-non-string ``ANTHROPIC_BASE_URL``.
    """
    resolved = _resolve(path)
    if not resolved.exists():
        return None
    doc = _parse_or_raise(resolved.read_text(encoding="utf-8"), resolved)
    env = doc.get("env")
    if not isinstance(env, dict) or FLIP_KEY not in env:
        return None
    value = env[FLIP_KEY]
    if not isinstance(value, str):
        raise SettingsError(f"settings.json '{FLIP_KEY}' is not a string: {resolved}")
    return value


def write_flip(path: str | os.PathLike[str], url: str) -> None:
    """Set ``env.ANTHROPIC_BASE_URL`` to ``url`` via an atomic read-modify-write.

    A missing file is created (mode ``0600``); a missing ``env`` block is created. Every
    other key and the file's indent + key order are preserved. Fails closed before any
    write on a malformed existing file.

    Raises:
        SettingsError: On any fail-closed condition in the existing file.
    """
    resolved = _resolve(path)
    if resolved.exists():
        text = resolved.read_text(encoding="utf-8")
        doc = _parse_or_raise(text, resolved)
        indent: int | str = _detect_indent(text)
        trailing = "\n" if text.endswith("\n") else ""
        mode = resolved.stat().st_mode & 0o777
    else:
        doc = {}
        indent = _DEFAULT_INDENT
        trailing = "\n"
        mode = _NEW_FILE_MODE

    env = doc.get("env")
    if not isinstance(env, dict):
        env = {}
        doc["env"] = env
    env[FLIP_KEY] = url

    data = (json.dumps(doc, indent=indent, ensure_ascii=False) + trailing).encode("utf-8")
    _atomic_write(resolved, data, mode=mode)


def remove_flip(path: str | os.PathLike[str], is_ours: Callable[[object], bool]) -> str:
    """Remove ``env.ANTHROPIC_BASE_URL`` only when ``is_ours`` accepts its current value.

    Returns one of ``"removed"`` (our value deleted), ``"foreign"`` (a value ``is_ours``
    rejects, left untouched), or ``"absent"`` (no file, ``env`` block, or key — a no-op that
    NEVER creates the file). Fails closed on a malformed existing file.

    Raises:
        SettingsError: On any fail-closed condition in the existing file.
    """
    resolved = _resolve(path)
    if not resolved.exists():
        return "absent"
    text = resolved.read_text(encoding="utf-8")
    doc = _parse_or_raise(text, resolved)
    env = doc.get("env")
    if not isinstance(env, dict) or FLIP_KEY not in env:
        return "absent"
    if not is_ours(env[FLIP_KEY]):
        return "foreign"

    del env[FLIP_KEY]
    indent = _detect_indent(text)
    trailing = "\n" if text.endswith("\n") else ""
    mode = resolved.stat().st_mode & 0o777
    data = (json.dumps(doc, indent=indent, ensure_ascii=False) + trailing).encode("utf-8")
    _atomic_write(resolved, data, mode=mode)
    return "removed"
