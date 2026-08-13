"""Registry loader: shipped Profile defaults merged with the user Overlay.

The Registry is the single source of truth for Proxy routing (ADR 0003 as amended
2026-08-13). Shipped defaults live as package data at ``data/profiles.json`` and are
read via :mod:`importlib.resources`; the user Overlay lives under the canonical
``$CLAUDE`` root chain (ADR 0006 as amended), then ``chinamaxM/profiles.json``. Merge
semantics mirror the old plugin's ``load_profiles``: per-Profile, top-level field
replacement keyed by ``name`` (an Overlay ``thinking`` replaces the shipped object
wholesale, never a deep merge); an Overlay row with a new name adds a Profile and must
carry the required fields.

Validation runs so every rejection names the source document plus the most specific
location available (document / header / Profile / field).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path

#: Label used in error messages for the shipped-defaults document.
SHIPPED_LABEL = "chinamaxM shipped defaults (data/profiles.json)"

#: Default Proxy port when neither shipped data nor Overlay overrides it.
DEFAULT_PORT = 8402

#: Fields a Profile row may carry. Anything else (notably the retired ``models`` and
#: ``match``) is an unknown field (ADR 0003 as amended).
_PROFILE_FIELDS = frozenset(
    {
        "name",
        "dialect",
        "base_url",
        "api_key_env",
        "default_model",
        "context_window",
        "thinking",
        "scrub",
        "request_extras",
        "tools",
    }
)

#: Required Profile fields for a new (Overlay-added) Profile.
_REQUIRED_FIELDS = ("dialect", "base_url", "api_key_env", "default_model")

#: Accepted ``dialect`` values (``responses`` parses but is 501 on every ingress until
#: a Responses-egress slice ships — ADR 0002).
_DIALECTS = ("anthropic", "responses")

#: Top-level keys the Overlay/shipped document may carry.
_HEADER_FIELDS = frozenset({"port", "profiles"})

#: Reserved Profile names (ADR 0004 as amended), matched case-insensitively: built-in
#: Claude Code agents plus the reserved Codex provider IDs.
_RESERVED_NAMES = frozenset(
    name.lower()
    for name in (
        "general-purpose",
        "Explore",
        "Plan",
        "claude",
        "statusline-setup",
        "claude-code-guide",
        "output-style-setup",
        "openai",
        "ollama",
        "lmstudio",
    )
)

#: Request keys a Profile's ``request_extras`` may never carry (top level or one level
#: inside ``extra_body``) — carried forward from the old plugin's protection.
_RESERVED_REQUEST_KEYS = frozenset(
    {
        "model",
        "max_tokens",
        "system",
        "tools",
        "messages",
        "stream",
        "timeout",
        "extra_headers",
        "extra_query",
    }
)


class RegistryError(ValueError):
    """Raised when the shipped defaults or user Overlay are malformed."""


@dataclass(frozen=True)
class Profile:
    """One provider configuration a Worker request routes to.

    Attributes:
        name: The Profile prefix used for routing.
        dialect: ``"anthropic"`` or ``"responses"``.
        base_url: The provider's Anthropic-compatible endpoint.
        api_key_env: The environment-variable name holding the provider key.
        default_model: The bare model string generation pins for this Profile.
        context_window: Optional per-model-string map to positive context sizes, or
            ``None`` when the Profile ships none.
        thinking: The Profile's Thinking-normalization policy object.
        scrub: Request fields dropped on worker-bound requests.
        request_extras: Extra request fields merged last (replace-on-conflict).
        tools: ``None`` (omit the key on generation) or a list.
    """

    name: str
    dialect: str
    base_url: str
    api_key_env: str
    default_model: str
    context_window: dict | None = None
    thinking: dict = field(default_factory=dict)
    scrub: list = field(default_factory=list)
    request_extras: dict = field(default_factory=dict)
    tools: list | None = None


@dataclass(frozen=True)
class Registry:
    """The resolved Registry: a port plus Profiles keyed by name in load order."""

    port: int
    profiles: dict[str, Profile]


def default_overlay_path() -> Path:
    """Return the Overlay path under the canonical ``$CLAUDE`` root chain.

    The root resolves ``CHINAMAXM_CLAUDE_HOME`` → ``$CLAUDE_CONFIG_DIR`` → ``~/.claude``
    (ADR 0006 as amended), then ``chinamaxM/profiles.json`` beneath it.

    Returns:
        The resolved Overlay path (which may not exist).
    """
    root = (
        os.environ.get("CHINAMAXM_CLAUDE_HOME")
        or os.environ.get("CLAUDE_CONFIG_DIR")
        or os.path.expanduser("~/.claude")
    )
    return Path(root) / "chinamaxM" / "profiles.json"


def load_registry(overlay_path: Path | None = None) -> Registry:
    """Resolve shipped defaults merged with the user Overlay into a validated Registry.

    Args:
        overlay_path: The Overlay file to merge. Defaults to
            :func:`default_overlay_path`; a missing file means shipped defaults only.

    Returns:
        The merged, validated Registry.

    Raises:
        RegistryError: If the shipped defaults or the Overlay are malformed.
    """
    if overlay_path is None:
        overlay_path = default_overlay_path()

    shipped_doc = _parse_shipped()
    port, shipped_rows = _validate_document(shipped_doc, SHIPPED_LABEL, existing=None)
    resolved: dict[str, Profile] = {row["name"]: _build_profile(row) for row in shipped_rows}

    if overlay_path.exists():
        source = str(overlay_path)
        overlay_doc = _parse_overlay(overlay_path, source)
        overlay_port, overlay_rows = _validate_document(overlay_doc, source, existing=resolved)
        if overlay_port is not None:
            port = overlay_port
        for row in overlay_rows:
            name = row["name"]
            if name in resolved:
                fields = {key: value for key, value in row.items() if key != "name"}
                resolved[name] = replace(resolved[name], **fields)
            else:
                resolved[name] = _build_profile(row)

    return Registry(port=port, profiles=resolved)


def _parse_shipped() -> object:
    """Read and JSON-decode the shipped defaults from package data."""
    text = (
        resources.files("chinamaxM")
        .joinpath("data", "profiles.json")
        .read_text(encoding="utf-8")
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - shipped data is well-formed
        raise RegistryError(f"{SHIPPED_LABEL}: malformed JSON ({exc})") from exc


def _parse_overlay(path: Path, source: str) -> object:
    """Read and JSON-decode the Overlay file, naming it on malformed JSON."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegistryError(f"{source}: malformed JSON ({exc})") from exc


def _validate_document(
    doc: object,
    source: str,
    existing: dict[str, Profile] | None,
) -> tuple[int | None, list[dict]]:
    """Validate one registry document (shipped or Overlay).

    Args:
        doc: The decoded JSON document.
        source: A label naming the document for error messages.
        existing: Profiles resolved so far (``None`` for the shipped document); an
            Overlay row whose name is absent here is a new Profile.

    Returns:
        A ``(port, rows)`` pair; ``port`` is ``None`` when the document omits it.

    Raises:
        RegistryError: On any malformed header or Profile row.
    """
    if isinstance(doc, list):
        raise RegistryError(
            f"{source}: expected a v2 registry object with a 'profiles' array, not a "
            f"bare JSON array (models[]/match were retired — see ADR 0003 as amended)"
        )
    if not isinstance(doc, dict):
        raise RegistryError(f"{source}: expected a JSON object with a 'profiles' array")

    unknown_header = sorted(set(doc) - _HEADER_FIELDS)
    if unknown_header:
        raise RegistryError(
            f"{source}: unknown top-level field(s): {', '.join(unknown_header)} "
            f"(allowed: port, profiles)"
        )

    port: int | None = None
    if "port" in doc:
        port = doc["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port <= 65535:
            raise RegistryError(f"{source}: 'port' must be an integer in 1..65535")

    rows = doc.get("profiles", [])
    if not isinstance(rows, list):
        raise RegistryError(f"{source}: 'profiles' must be an array")

    seen: set[str] = set()
    for index, row in enumerate(rows):
        _validate_row(row, index, source, existing, seen)

    return port, rows


def _validate_row(
    row: object,
    index: int,
    source: str,
    existing: dict[str, Profile] | None,
    seen: set[str],
) -> None:
    """Validate one Profile row in document order.

    Raises:
        RegistryError: On any malformed field.
    """
    if not isinstance(row, dict):
        raise RegistryError(f"{source}: profile at index {index} is not a JSON object")

    name = row.get("name")
    if not isinstance(name, str) or not name:
        raise RegistryError(f"{source}: profile at index {index} needs a non-empty 'name'")
    if name in seen:
        raise RegistryError(f"{source}: duplicate profile {name!r}")
    seen.add(name)

    if name.lower() in _RESERVED_NAMES:
        raise RegistryError(
            f"{source}: profile name {name!r} is reserved and may not be used "
            f"(ADR 0004 as amended)"
        )

    unknown = sorted(set(row) - _PROFILE_FIELDS)
    if unknown:
        raise RegistryError(
            f"{source}: profile {name!r} has unknown field(s): {', '.join(unknown)} "
            f"(models[]/match were retired — see ADR 0003 as amended)"
        )

    is_new = existing is None or name not in existing
    if is_new:
        missing = [field_name for field_name in _REQUIRED_FIELDS if field_name not in row]
        if missing:
            raise RegistryError(
                f"{source}: new profile {name!r} must define: {', '.join(missing)}"
            )

    _validate_fields(row, name, source)
    _validate_extras(row, name, source, existing)


def _validate_fields(row: dict, name: str, source: str) -> None:
    """Validate the shape of each present Profile field.

    Raises:
        RegistryError: On any malformed field.
    """
    for field_name in ("name", "base_url", "api_key_env", "default_model"):
        if field_name in row and (not isinstance(row[field_name], str) or not row[field_name]):
            raise RegistryError(
                f"{source}: profile {name!r} field {field_name!r} must be a non-empty string"
            )

    if "dialect" in row and row["dialect"] not in _DIALECTS:
        raise RegistryError(
            f"{source}: profile {name!r} field 'dialect' must be one of: "
            f"{', '.join(_DIALECTS)}"
        )

    if "context_window" in row:
        window = row["context_window"]
        if not isinstance(window, dict):
            raise RegistryError(
                f"{source}: profile {name!r} field 'context_window' must be a JSON object"
            )
        for key, value in window.items():
            if not isinstance(key, str):
                raise RegistryError(
                    f"{source}: profile {name!r} field 'context_window' keys must be strings"
                )
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RegistryError(
                    f"{source}: profile {name!r} field 'context_window' values must be "
                    f"positive integers"
                )

    if "thinking" in row and not isinstance(row["thinking"], dict):
        raise RegistryError(
            f"{source}: profile {name!r} field 'thinking' must be a JSON object"
        )

    if "scrub" in row:
        scrub = row["scrub"]
        if not isinstance(scrub, list) or not all(isinstance(item, str) for item in scrub):
            raise RegistryError(
                f"{source}: profile {name!r} field 'scrub' must be a list of strings"
            )

    if "request_extras" in row and not isinstance(row["request_extras"], dict):
        raise RegistryError(
            f"{source}: profile {name!r} field 'request_extras' must be a JSON object"
        )

    if "tools" in row and not (row["tools"] is None or isinstance(row["tools"], list)):
        raise RegistryError(
            f"{source}: profile {name!r} field 'tools' must be null or a list"
        )


def _validate_extras(
    row: dict,
    name: str,
    source: str,
    existing: dict[str, Profile] | None,
) -> None:
    """Guard ``request_extras`` against reserved keys and re-added scrubbed fields.

    The effective Scrub list is the row's own ``scrub`` when present, otherwise the
    already-resolved Profile's Scrub (for an Overlay override), otherwise empty.

    Raises:
        RegistryError: If ``request_extras`` carries a reserved request key or re-adds
            a field the Profile scrubs.
    """
    if "request_extras" not in row:
        return
    extras = row["request_extras"]
    if not isinstance(extras, dict):  # already reported by _validate_fields
        return

    keys = set(extras)
    if isinstance(extras.get("extra_body"), dict):
        keys |= set(extras["extra_body"])

    reserved = sorted(keys & _RESERVED_REQUEST_KEYS)
    if reserved:
        raise RegistryError(
            f"{source}: profile {name!r} 'request_extras' may not set reserved "
            f"key(s): {', '.join(reserved)}"
        )

    if "scrub" in row and isinstance(row["scrub"], list):
        effective_scrub = set(row["scrub"])
    elif existing is not None and name in existing:
        effective_scrub = set(existing[name].scrub)
    else:
        effective_scrub = set()

    re_added = sorted(keys & effective_scrub)
    if re_added:
        raise RegistryError(
            f"{source}: profile {name!r} 'request_extras' may not re-add scrubbed "
            f"field(s): {', '.join(re_added)}"
        )


def _build_profile(row: dict) -> Profile:
    """Construct a Profile from a validated, complete row (shipped or new Overlay)."""
    return Profile(
        name=row["name"],
        dialect=row["dialect"],
        base_url=row["base_url"],
        api_key_env=row["api_key_env"],
        default_model=row["default_model"],
        context_window=row.get("context_window"),
        thinking=row.get("thinking", {}),
        scrub=row.get("scrub", []),
        request_extras=row.get("request_extras", {}),
        tools=row.get("tools"),
    )
