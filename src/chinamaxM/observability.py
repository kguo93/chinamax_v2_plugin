"""Observability: the append-only JSONL request log and count_tokens support pieces.

Every worker-branch request appends one JSONL line to
``<claude root>/chinamaxM/requests.jsonl`` (ADR 0010; the root resolves through ADR 0006's
canonical chain). The Default branch is never logged (ADR 0010: "not ours to observe"), so
nothing here is referenced from that code path. Provider usage is extracted by a passive
TEE over the provider-side Anthropic response/SSE — the relayed bytes are never altered —
and count_tokens falls back to a ``ceil(chars/4)`` estimate when the Profile's endpoint has
no support (ADR 0010 as amended).

The pure helpers (:func:`resolve_log_path`, :func:`estimate_input_tokens`,
:func:`valid_count_tokens_body`, :func:`sse_event_usage`) carry no litellm dependency — the
SSE parser is imported lazily inside :class:`AnthropicUsageTee` so Doctor (hosts-04) can
consume :func:`resolve_log_path` without pulling the translation engine.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any

#: Directory (beneath the resolved Claude Host root) holding the request log.
LOG_DIR_NAME = "chinamaxM"

#: Basename of the append-only JSONL request log (ADR 0010).
LOG_FILE_NAME = "requests.jsonl"


def resolve_log_path(claude_root: str | os.PathLike[str]) -> Path:
    """Return the request-log path beneath a resolved Claude Host root.

    Args:
        claude_root: The Claude Host root (already resolved via ADR 0006's chain).

    Returns:
        ``<claude_root>/chinamaxM/requests.jsonl`` (which may not exist yet).
    """
    return Path(claude_root) / LOG_DIR_NAME / LOG_FILE_NAME


class JsonlRequestLog:
    """Append-only JSONL writer: one ``os.write`` per record, best-effort, never fatal.

    The parent directory is created and the file opened (``O_APPEND``) lazily on the first
    write and the descriptor is kept for the process lifetime. Each record is one
    ``os.write`` of ``json.dumps(..., separators=(",", ":"))`` plus a trailing newline, so a
    line is atomic within the single supervised Proxy process (ADR 0009). A failed write
    (permissions, disk) warns on stderr ONCE per distinct failure and never fails the worker
    request.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None
        self._warned: set[str] = set()

    def write(self, record: dict) -> None:
        """Append one record as a single JSONL line; swallow and warn on any write failure."""
        try:
            if self._fd is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fd = os.open(
                    self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
                )
            line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
            os.write(self._fd, line)
        except OSError as exc:
            self._warn_once(str(exc))

    def close(self) -> None:
        """Close the kept descriptor (called on app cleanup)."""
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:  # pragma: no cover - close failures are not actionable
                pass
            self._fd = None

    def _warn_once(self, message: str) -> None:
        """Warn on stderr the first time a distinct failure message is seen."""
        if message not in self._warned:
            self._warned.add(message)
            print(f"chinamaxM: request-log write failed: {message}", file=sys.stderr)

    def __call__(self, record: dict) -> None:
        """Allow the writer to serve directly as the app's log sink."""
        self.write(record)


def sse_event_usage(event: dict) -> dict | None:
    """Return the usage sub-object carried by an Anthropic SSE event, or ``None``.

    ``message_start`` carries ``message.usage`` (``input_tokens`` + cache fields);
    ``message_delta`` carries a top-level ``usage`` (``output_tokens``). Every other event
    (and any malformed sentinel) carries no usage.
    """
    etype = event.get("type")
    if etype == "message_start":
        message = event.get("message")
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            return message["usage"]
        return None
    if etype == "message_delta":
        usage = event.get("usage")
        if isinstance(usage, dict):
            return usage
        return None
    return None


class AnthropicUsageTee:
    """Passively extract provider usage from a relayed Anthropic response (bytes untouched).

    The caller feeds every upstream body chunk; the tee never alters or withholds a byte. A
    ``text/event-stream`` response is parsed incrementally (events split across transport
    chunks are handled, unparseable events skipped) and every usage-carrying event is
    shallow-merged in arrival order (later keys override, unknown keys preserved); a JSON
    (non-streaming) response is buffered and its ``usage`` object is taken VERBATIM. A stream
    with no usage-carrying event, or a body with no usage object, yields ``None``.
    """

    def __init__(self) -> None:
        self.status: int | None = None
        self._mode: str | None = None
        self._parser: Any = None
        self._json = bytearray()
        self._usage: dict | None = None

    def begin(self, status: int, content_type: str) -> None:
        """Record the upstream status and select SSE vs JSON mode from the content type."""
        self.status = status
        if "text/event-stream" in (content_type or "").lower():
            from chinamaxM.seam import AnthropicSSEParser  # lazy: keeps helpers litellm-free

            self._mode = "sse"
            self._parser = AnthropicSSEParser()
        else:
            self._mode = "json"

    def feed(self, chunk: bytes) -> None:
        """Consume one upstream body chunk (SSE parsed incrementally, JSON buffered)."""
        if self._mode == "sse":
            for event in self._parser.feed(chunk):
                part = sse_event_usage(event)
                if part is not None:
                    self._merge(part)
        elif self._mode == "json":
            self._json += chunk

    def finish(self) -> None:
        """Parse a buffered JSON body's ``usage`` (no-op for SSE mode)."""
        if self._mode == "json" and self._json:
            try:
                body = json.loads(bytes(self._json))
            except ValueError:
                return
            if isinstance(body, dict) and isinstance(body.get("usage"), dict):
                self._usage = body["usage"]

    def _merge(self, part: dict) -> None:
        """Shallow-merge one usage-carrying event's fields (later keys override)."""
        merged = self._usage or {}
        merged.update(part)
        self._usage = merged

    @property
    def usage(self) -> dict | None:
        """The extracted provider usage object, or ``None`` when none was seen."""
        return self._usage


def estimate_input_tokens(egress_bytes: bytes) -> int:
    """Estimate input tokens as ``ceil(chars / 4)`` over the egress body's CHARACTER count.

    The character count decodes the serialized egress bytes as UTF-8 first, so multi-byte
    text is never over-counted by byte length (ADR 0010 as amended).
    """
    chars = len(egress_bytes.decode("utf-8"))
    return math.ceil(chars / 4)


def valid_count_tokens_body(raw: bytes) -> dict | None:
    """Return the parsed body when it is a valid count_tokens answer, else ``None``.

    A valid answer is a JSON object carrying a non-negative INTEGER ``input_tokens``
    (booleans excluded). Anything else — non-JSON, a non-object, a missing/typed/negative
    ``input_tokens`` — is malformed and returns ``None`` (the estimator takes over).
    """
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    value = body.get("input_tokens")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return body
