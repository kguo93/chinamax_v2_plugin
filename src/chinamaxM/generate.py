"""Generation engine: resolved Registry to Host artifacts, and drift.

Both Hosts share this pure engine (ADR 0004 as amended 2026-08-13). From a resolved
Registry it produces, one artifact per Profile:

* a Claude agent markdown at ``<claude-root>/agents/<profile>.md`` — YAML frontmatter
  (``name``/``description``/``model``/optional ``tools``) plus a lean Worker system
  prompt under an ownership marker;
* a Codex ``[model_providers.chinamaxM-<profile>]`` entry in ``<codex-root>/config.toml``
  via TOML-preserving edits (no ``env_key`` — ADR 0006);
* a Codex role TOML at ``<codex-root>/agents/<profile>.toml``.

Generated artifacts are immutable outside generation (ADR 0004 as amended 2026-08-19):
regeneration is the ONLY edit path, and a per-dispatch model override rides the Dispatch
marker in the spawn prompt, never a file edit. :func:`detect_drift` classifies on-disk
state without ever mutating or raising, so Doctor can consume it as pure diagnosis.

Host roots resolve through the one canonical chain shared with every Host surface
(``CHINAMAXM_CLAUDE_HOME`` / ``CHINAMAXM_CODEX_HOME`` → ``$CLAUDE_CONFIG_DIR`` /
``$CODEX_HOME`` → ``~/.claude`` / ``~/.codex``), reusing
:func:`chinamaxM.keyfiles.resolve_host_root`.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Iterable, Mapping

# ``tomlkit`` and ``yaml`` are imported LAZILY inside the functions that serialize/parse
# TOML/YAML — never at module load — so a consumer running under a bare ambient interpreter
# that lacks these env-only deps (setup.py's Bootstrap contract) can import this module.

from .keyfiles import resolve_host_root
from .registry import Profile, Registry

#: Ownership marker written into every generated Claude agent (first body line).
CLAUDE_MARKER = "<!-- chinamaxM-generated: do not hand-edit -->"

#: Ownership marker written as the first line of every generated Codex role TOML.
CODEX_MARKER = "# chinamaxM-generated: do not hand-edit"

#: Raw substring searched to classify a file as generated even when it no longer parses.
MARKER_SUBSTRING = "chinamaxM-generated"

#: Prefix for every generated Codex provider ID and role ``model_provider`` reference.
PROVIDER_PREFIX = "chinamaxM-"

#: Basename of the user-level Codex config the provider entries live in.
CONFIG_FILE_NAME = "config.toml"

#: Basename of the Host ``agents`` directory that holds generated artifacts.
AGENTS_DIR_NAME = "agents"

#: A large emitter width so serialized scalars never wrap onto a second line.
_YAML_WIDTH = 1 << 30

#: Legal artifact-name grammar: the intersection of filename, TOML-key, Codex role-name,
#: and URL-path surfaces (ADR 0004 as amended) — lowercase alphanumerics and ``-`` only.
_NAME_GRAMMAR = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Stem of an operator-facing Worker INSTANCE name (the spawn ``name``), distinct from
#: the generated artifact name (the bare Profile) and from :data:`PROVIDER_PREFIX` (the
#: Codex provider ID, capital-M). The per-Host separator joins it: a named Worker spawn is
#: ``chinamaxm-<profile>-<suffix>`` on Claude and ``chinamaxm_<profile>_<suffix>`` on Codex
#: (Codex 0.147 rejects hyphenated agent names — ADR 0004 as amended 2026-08-19), each with
#: a non-empty lowercase suffix, so the name references chinamaxM and the task rather than
#: a bare Profile index.
WORKER_NAME_PREFIX = "chinamaxm"

#: The per-Host Worker instance-name separators: ``-`` on Claude, ``_`` on Codex.
_WORKER_NAME_SEPARATORS = ("-", "_")

#: Reserved Profile names on either Host, matched case-insensitively (ADR 0004 as
#: amended): built-in Claude Code agents plus the reserved Codex provider IDs. Checked in
#: preflight as defense in depth beyond the Registry loader.
_RESERVED_PROFILE_NAMES = frozenset(
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

#: The lean Worker system prompt, authored once here and used verbatim as both the Claude
#: agent body and the Codex role ``developer_instructions`` (ADR 0004 as amended). Numbered
#: and token-lean; item 4 states the complete-final-report duty hosts-02's hooks enforce,
#: and item 5 points a Worker at its Host's lazy-loaded MCP tools (ADR 0004 as amended
#: 2026-08-16) — a non-Claude model does not otherwise know to look for a deferred schema.
WORKER_INSTRUCTIONS = (
    "1. You are a chinamaxM worker subagent. Do exactly the task the parent gives "
    "you — nothing more, nothing less.\n"
    "2. Work autonomously to completion; do not pause to ask the parent for "
    "confirmation mid-task.\n"
    "3. You do not share the parent's conversation — rely only on the context in "
    "your prompt.\n"
    "4. End with ONE complete, self-contained final report as your last message: what "
    "you did, the outcome, and every path or result the parent needs. The parent "
    "prints this message verbatim as its own, so it must stand alone — nothing you "
    "said earlier is relayed.\n"
    "5. Your Host has MCP tools, and their schemas load on demand — a tool absent from "
    "your context is NOT absent from your Host. Prefer them over reinventing the same "
    "work by hand, and look one up only when the task needs it: ToolSearch on Claude, "
    "ALL_TOOLS / tools.mcp__<server>__<tool> in the exec sandbox on Codex."
)


class GenerationError(ValueError):
    """Raised by generation on preflight or on-disk faults.

    Preflight raises (reserved/illegal Profile names, an unparseable or wrongly-shaped
    ``config.toml``) fire before any write, so a Codex-side fault never leaves partial
    Claude artifacts behind. :func:`detect_drift` never raises — it reports these states.
    """


# --------------------------------------------------------------------------- roots


def resolve_roots(
    claude_override: str | os.PathLike[str] | None = None,
    codex_override: str | os.PathLike[str] | None = None,
) -> dict[str, Path]:
    """Resolve both Host roots through the canonical chain (ADR 0006 as amended).

    Args:
        claude_override: An explicit Claude root that wins over every environment var.
        codex_override: An explicit Codex root that wins over every environment var.

    Returns:
        A ``{"claude": Path, "codex": Path}`` mapping of resolved Host roots.
    """
    return {
        "claude": resolve_host_root("claude", claude_override),
        "codex": resolve_host_root("codex", codex_override),
    }


def _claude_agents_dir(roots: Mapping[str, Path]) -> Path:
    """Return the Claude ``agents`` directory beneath the resolved Claude root."""
    return Path(roots["claude"]) / AGENTS_DIR_NAME


def _codex_agents_dir(roots: Mapping[str, Path]) -> Path:
    """Return the Codex ``agents`` directory beneath the resolved Codex root."""
    return Path(roots["codex"]) / AGENTS_DIR_NAME


def _config_path(roots: Mapping[str, Path]) -> Path:
    """Return the Codex ``config.toml`` path beneath the resolved Codex root."""
    return Path(roots["codex"]) / CONFIG_FILE_NAME


# ----------------------------------------------------------------- artifact content


def _claude_agent_content(profile: Profile) -> str:
    """Render the full Claude agent markdown for one Profile.

    Frontmatter key order is ``name``, ``description``, ``model``, then ``tools`` only
    when the Profile overrides it (a null ``tools`` omits the key so the host grants its
    standard set). All scalars pass through the YAML serializer for correct escaping.
    """
    import yaml  # lazy (Bootstrap): env-only dep, never imported at module load

    front: dict[str, object] = {
        "name": profile.name,
        "description": f"chinamaxM worker on {profile.name}",
        "model": f"{profile.name}/{profile.default_model}",
    }
    if profile.tools is not None:
        front["tools"] = profile.tools
    frontmatter = yaml.safe_dump(
        front,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=_YAML_WIDTH,
    )
    return f"---\n{frontmatter}---\n{CLAUDE_MARKER}\n{WORKER_INSTRUCTIONS}\n"


def _codex_role_content(profile: Profile) -> str:
    """Render the full Codex role TOML for one Profile.

    Key order is ``name``, ``description``, ``model`` (bare default), ``model_provider``,
    ``model_context_window`` (only when the Registry pins one for the default model), then
    ``developer_instructions``. ``model_reasoning_effort`` is never emitted (ADR 0004).
    """
    import tomlkit  # lazy (Bootstrap): env-only dep, never imported at module load

    doc = tomlkit.document()
    doc.add(tomlkit.comment(CODEX_MARKER[2:]))
    doc["name"] = profile.name
    doc["description"] = f"chinamaxM worker on {profile.name}"
    doc["model"] = profile.default_model
    doc["model_provider"] = f"{PROVIDER_PREFIX}{profile.name}"
    window = None
    if profile.context_window:
        window = profile.context_window.get(profile.default_model)
    if window is not None:
        doc["model_context_window"] = window
    doc["developer_instructions"] = WORKER_INSTRUCTIONS
    return tomlkit.dumps(doc)


def _provider_fields(profile: Profile, port: int) -> dict[str, str]:
    """Return the exact expected field set of one generated provider table."""
    return {
        "name": f"{PROVIDER_PREFIX}{profile.name}",
        "base_url": f"http://127.0.0.1:{port}/openai/{profile.name}",
        "wire_api": "responses",
    }


def _provider_id(profile_name: str) -> str:
    """Return the ``model_providers`` table key for a Profile."""
    return f"{PROVIDER_PREFIX}{profile_name}"


def _provider_label(provider_id: str) -> str:
    """Return the dotted-path label used in drift/regeneration reports."""
    return f"model_providers.{provider_id}"


def expected_artifacts(
    registry: Registry, roots: Mapping[str, Path], host: str | None = None
) -> dict[Path, str]:
    """Compute the expected whole-file artifacts for every Profile, optionally per Host.

    Provider entries are NOT whole-file artifacts (``config.toml`` is only partially
    ours), so they are excluded here and handled structurally by :func:`detect_drift` and
    regeneration.

    Args:
        registry: The resolved Registry.
        roots: A ``{"claude": Path, "codex": Path}`` mapping of Host roots.
        host: ``"claude"`` → only Claude agent ``.md`` artifacts; ``"codex"`` → only Codex
            role ``.toml`` artifacts; ``None`` → both (ADR 0004 as amended 2026-08-18, the
            surfaces scope generation to the single invoking Host).

    Returns:
        An ordered ``{path: content}`` mapping (Claude agents and/or Codex role TOMLs),
        with stable ordering and no timestamps.
    """
    claude_agents = _claude_agents_dir(roots)
    codex_agents = _codex_agents_dir(roots)
    artifacts: dict[Path, str] = {}
    for profile in registry.profiles.values():
        if host in (None, "claude"):
            artifacts[claude_agents / f"{profile.name}.md"] = _claude_agent_content(profile)
        if host in (None, "codex"):
            artifacts[codex_agents / f"{profile.name}.toml"] = _codex_role_content(profile)
    return artifacts


def expected_providers(registry: Registry) -> dict[str, dict[str, str]]:
    """Compute the expected provider table field sets keyed by provider ID."""
    port = registry.port
    return {
        _provider_id(profile.name): _provider_fields(profile, port)
        for profile in registry.profiles.values()
    }


# ---------------------------------------------------------------- agent-name matching


def matches_generated_agent(name: str, profile_names: Iterable[str]) -> bool:
    """Return whether ``name`` addresses a generated Worker for some Profile.

    A generated Worker is spawned either as the bare Profile agent (its ``agent_type`` is
    the Profile name) or under an operator-chosen INSTANCE name — the Claude hyphen form
    ``chinamaxm-<profile>-<suffix>`` or the Codex underscore form
    ``chinamaxm_<profile>_<suffix>``, each with a non-empty suffix (a named spawn surfaces
    the NAME, not the subagent type — ADR 0004 as amended 2026-08-19). This is the single
    shared matcher the worker-contract hook reuses on both its SubagentStart
    (``agent_type``) and PreToolUse (``subagent_type``) branches, so the rule is never
    re-implemented inline.

    Matching is anchored and case-sensitive: the ``chinamaxm<sep><profile><sep>`` prefix
    must match exactly (with ONE separator used throughout), so ``chinamaxm-kimono-x``
    never matches Profile ``kimi`` and a bare-substring misfire is impossible. The legacy
    ``<profile>-<suffix>`` instance form (e.g. ``deepseek-1``) is NOT matched. Shipped
    Profile names are ``[a-z0-9]``-only; a Profile name containing a separator character
    would make the two grammars ambiguous and is out of contract. Reserved Profile names
    cannot enter the Registry (ADR 0003/0004), so a built-in agent name can never match.
    An unrelated agent whose name merely starts with a full prefix gets the benign
    contract — the accepted false-positive direction.

    Args:
        name: The candidate ``agent_type`` or ``subagent_type`` from a hook event.
        profile_names: The resolved Registry's Profile names.

    Returns:
        ``True`` when ``name`` equals a Profile name,
        ``chinamaxm-<profile>-<non-empty suffix>``, or
        ``chinamaxm_<profile>_<non-empty suffix>``.
    """
    for profile in profile_names:
        if name == profile:
            return True
        for sep in _WORKER_NAME_SEPARATORS:
            prefix = f"{WORKER_NAME_PREFIX}{sep}{profile}{sep}"
            if name.startswith(prefix) and len(name) > len(prefix):
                return True
    return False


# ------------------------------------------------------------------- marker helpers


def _has_marker(text: str) -> bool:
    """Return whether text carries the ownership marker substring."""
    return MARKER_SUBSTRING in text


def _read_text(path: Path) -> str | None:
    """Read a file as UTF-8, returning ``None`` when it cannot be read."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


# ------------------------------------------------------------------- atomic writing


def _atomic_write(path: Path, data: bytes) -> None:
    """Write bytes atomically via a same-directory temp file and ``os.replace``.

    Preserves an existing file's permission bits (``config.toml`` sits next to secrets);
    a new file is created mode-0644. The write is all-or-nothing per file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = 0o644
    descriptor, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


# ----------------------------------------------------------------------- preflight


def _validate_profile_names(registry: Registry) -> None:
    """Refuse reserved or illegally-named Profiles before any artifact is written.

    Raises:
        GenerationError: On a reserved name (either Host) or a name outside the grammar
            ``^[a-z0-9][a-z0-9-]*$``.
    """
    for name in registry.profiles:
        if name.lower() in _RESERVED_PROFILE_NAMES:
            raise GenerationError(
                f"profile name {name!r} is reserved on Claude or Codex and cannot be "
                f"generated (ADR 0004 as amended)"
            )
        if not _NAME_GRAMMAR.match(name):
            raise GenerationError(
                f"profile name {name!r} is not a legal artifact name; the grammar is "
                f"^[a-z0-9][a-z0-9-]*$ (lowercase alphanumerics and '-' only)"
            )


def _parse_config_or_raise(config_path: Path) -> tomlkit.TOMLDocument | None:
    """Parse the Codex ``config.toml`` and validate the shape of our owned tables.

    Args:
        config_path: The ``config.toml`` path (may be absent).

    Returns:
        The parsed document, or ``None`` when the file is absent.

    Raises:
        GenerationError: If the file cannot be read or parsed, if ``model_providers`` is
            not a table, or if any ``chinamaxM-`` entry is not a table.
    """
    import tomlkit  # lazy (Bootstrap): env-only dep, never imported at module load

    if not config_path.exists():
        return None
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GenerationError(f"{config_path}: cannot read config.toml ({exc})") from exc
    try:
        doc = tomlkit.parse(text)
    except Exception as exc:
        raise GenerationError(f"{config_path}: unparseable TOML ({exc})") from exc

    providers = doc.get("model_providers")
    if providers is not None:
        if not hasattr(providers, "items"):
            raise GenerationError(f"{config_path}: 'model_providers' is not a table")
        for key in providers:
            if key.startswith(PROVIDER_PREFIX) and not hasattr(providers[key], "items"):
                raise GenerationError(
                    f"{config_path}: 'model_providers.{key}' is not a table"
                )
    return doc


# --------------------------------------------------------------- provider reconcile


def _reconcile_providers(
    doc: tomlkit.TOMLDocument, registry: Registry
) -> tuple[list[str], list[str], list[str]]:
    """Reconcile the owned provider tables in a parsed document in place.

    Owned tables reconcile by exact key set: a stale table (wrong key set — a smuggled
    ``env_key`` included — or wrong value) is rewritten to exactly the expected fields;
    a byte-correct table is left untouched for idempotence; a ``chinamaxM-`` table with
    no expected counterpart is pruned.

    Returns:
        ``(written, skipped, pruned)`` lists of provider labels.
    """
    import tomlkit  # lazy (Bootstrap): env-only dep, never imported at module load

    expected = expected_providers(registry)
    written: list[str] = []
    skipped: list[str] = []
    pruned: list[str] = []

    providers = doc.get("model_providers")
    if providers is None:
        if not expected:
            return written, skipped, pruned
        providers = tomlkit.table(is_super_table=True)
        doc["model_providers"] = providers

    for provider_id, fields in expected.items():
        existing = providers.get(provider_id)
        if existing is not None and hasattr(existing, "items") and dict(existing) == fields:
            skipped.append(_provider_label(provider_id))
            continue
        table = tomlkit.table()
        for key, value in fields.items():
            table[key] = value
        providers[provider_id] = table
        written.append(_provider_label(provider_id))

    for key in list(providers):
        if key.startswith(PROVIDER_PREFIX) and key not in expected:
            del providers[key]
            pruned.append(_provider_label(key))

    return written, skipped, pruned


# ----------------------------------------------------------------- drift detection


def detect_drift(
    registry: Registry, roots: Mapping[str, Path], host: str | None = None
) -> dict[str, object]:
    """Classify on-disk state against the expected artifact set without mutating.

    Staleness is STRICT whole-content equality, model line included (ADR 0004 as amended
    2026-08-19: the artifact is immutable outside generation, so any divergence is drift).
    This function never raises on bad on-disk state (Doctor is pure diagnosis);
    unparseable or wrongly-shaped ``config.toml`` states are reported under ``conflicts``.

    Args:
        registry: The resolved Registry.
        roots: A ``{"claude": Path, "codex": Path}`` mapping of Host roots.
        host: ``"claude"`` inspects only the Claude agents; ``"codex"`` inspects only the
            Codex role TOMLs + provider tables; ``None`` inspects both. Doctor scopes this
            to the invoking Host (ADR 0005 as amended 2026-08-18).

    Returns:
        ``{"missing", "stale", "foreign", "conflicts"}`` sorted string lists. File
        artifacts are labelled by absolute path; provider tables by
        ``model_providers.chinamaxM-<profile>``.
    """
    missing: list[str] = []
    stale: list[str] = []
    foreign: list[str] = []
    conflicts: list[str] = []

    expected = expected_artifacts(registry, roots, host)
    expected_paths = set(expected)

    for path, content in expected.items():
        if not path.exists():
            missing.append(str(path))
            continue
        current = _read_text(path)
        if current is None or not _has_marker(current):
            conflicts.append(str(path))
            continue
        if current != content:
            stale.append(str(path))

    scan_dirs: list[Path] = []
    if host in (None, "claude"):
        scan_dirs.append(_claude_agents_dir(roots))
    if host in (None, "codex"):
        scan_dirs.append(_codex_agents_dir(roots))
    for directory in scan_dirs:
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if not entry.is_file() or entry in expected_paths:
                continue
            text = _read_text(entry)
            if text is not None and _has_marker(text):
                foreign.append(str(entry))

    if host in (None, "codex"):
        _classify_providers(registry, roots, missing, stale, foreign, conflicts)

    return {
        "missing": sorted(missing),
        "stale": sorted(stale),
        "foreign": sorted(foreign),
        "conflicts": sorted(conflicts),
    }


def _classify_providers(
    registry: Registry,
    roots: Mapping[str, Path],
    missing: list[str],
    stale: list[str],
    foreign: list[str],
    conflicts: list[str],
) -> None:
    """Classify the owned provider tables into the shared drift buckets."""
    config_path = _config_path(roots)
    try:
        doc = _parse_config_or_raise(config_path)
    except GenerationError as exc:
        conflicts.append(str(exc))
        return

    providers = doc.get("model_providers") if doc is not None else None
    expected = expected_providers(registry)

    for provider_id, fields in expected.items():
        label = _provider_label(provider_id)
        existing = None if providers is None else providers.get(provider_id)
        if existing is None:
            missing.append(label)
        elif dict(existing) != fields:
            stale.append(label)

    if providers is not None:
        for key in providers:
            if key.startswith(PROVIDER_PREFIX) and key not in expected:
                foreign.append(_provider_label(key))


# --------------------------------------------------------------------- regeneration


def regenerate(
    registry: Registry, roots: Mapping[str, Path], host: str | None = None
) -> dict[str, list[str]]:
    """Converge the invoking Host's on-disk artifacts to the expected set, then report.

    Preflight (name validation, and — for the Codex side — a ``config.toml`` parse) runs
    first, so no file is written when preflight would fail. Only marker-bearing files and
    ``chinamaxM-`` tables are ever overwritten, deleted, or pruned; a conflict path (an
    expected path occupied by a marker-less file) is skipped and re-reported. Writes are
    byte-identical-skipped, atomic, and mode-preserving.

    Args:
        registry: The resolved Registry.
        roots: A ``{"claude": Path, "codex": Path}`` mapping of Host roots.
        host: ``"claude"`` writes ONLY the Claude agent ``.md``s (never touching the Codex
            root); ``"codex"`` writes ONLY the provider entries + role TOMLs (never touching
            the Claude agents dir); ``None`` writes both. The surfaces scope this to the
            single invoking Host (ADR 0004/0005/0006 as amended 2026-08-18).

    Returns:
        ``{"written", "skipped", "pruned", "conflicts"}`` sorted string lists.

    Raises:
        GenerationError: On any preflight fault (before any write occurs).
    """
    import tomlkit  # lazy (Bootstrap): env-only dep, never imported at module load

    _validate_profile_names(registry)
    do_claude = host in (None, "claude")
    do_codex = host in (None, "codex")

    config_path = _config_path(roots)
    # Parse (and shape-validate) the Codex config only when the Codex side is in scope, so a
    # Claude-host regeneration never reads or requires ``~/.codex/config.toml``.
    config_doc = _parse_config_or_raise(config_path) if do_codex else None

    written: list[str] = []
    skipped: list[str] = []
    pruned: list[str] = []
    conflicts: list[str] = []

    expected = expected_artifacts(registry, roots, host)
    expected_paths = set(expected)

    for path, content in expected.items():
        current = _read_text(path) if path.exists() else None
        if current is not None and not _has_marker(current):
            conflicts.append(str(path))
            continue
        if current == content:
            skipped.append(str(path))
            continue
        _atomic_write(path, content.encode("utf-8"))
        written.append(str(path))

    scan_dirs: list[Path] = []
    if do_claude:
        scan_dirs.append(_claude_agents_dir(roots))
    if do_codex:
        scan_dirs.append(_codex_agents_dir(roots))
    for directory in scan_dirs:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_file() or entry in expected_paths:
                continue
            text = _read_text(entry)
            if text is not None and _has_marker(text):
                entry.unlink()
                pruned.append(str(entry))

    if do_codex:
        config_existed = config_doc is not None
        if config_doc is None:
            config_doc = tomlkit.document()
        table_written, table_skipped, table_pruned = _reconcile_providers(config_doc, registry)
        new_text = tomlkit.dumps(config_doc)
        original_text = config_path.read_text(encoding="utf-8") if config_existed else None
        if new_text != original_text:
            _atomic_write(config_path, new_text.encode("utf-8"))
            written.extend(table_written)
        else:
            skipped.extend(table_skipped)
        pruned.extend(table_pruned)

    return {
        "written": sorted(written),
        "skipped": sorted(skipped),
        "pruned": sorted(pruned),
        "conflicts": sorted(conflicts),
    }


# ----------------------------------------------------------------- provider unwire


def remove_provider_entries(roots: Mapping[str, Path]) -> dict[str, list[str]]:
    """Strip every generated ``chinamaxM-`` provider table from the Codex ``config.toml``.

    The Codex-side teardown unwire (ADR 0005 as amended 2026-08-18). Reuses this module's
    marker-safe, mode-preserving atomic write: only tables whose key carries the
    :data:`PROVIDER_PREFIX` are deleted, so a foreign ``model_providers`` entry and every
    other byte of ``config.toml`` survive the round-trip. An absent file, or a config with
    no matching entries, is a no-op.

    Args:
        roots: A ``{"claude": Path, "codex": Path}`` mapping of Host roots.

    Returns:
        ``{"removed": [...]}`` sorted ``model_providers.chinamaxM-<profile>`` labels that
        were actually deleted (empty when nothing matched).
    """
    import tomlkit  # lazy (Bootstrap): env-only dep, never imported at module load

    config_path = _config_path(roots)
    text = _read_text(config_path)
    if text is None:
        return {"removed": []}
    try:
        doc = tomlkit.parse(text)
    except Exception:  # noqa: BLE001 - an unparseable config is left untouched, never a crash
        return {"removed": []}

    providers = doc.get("model_providers")
    removed: list[str] = []
    if providers is not None and hasattr(providers, "items"):
        for key in list(providers):
            if str(key).startswith(PROVIDER_PREFIX):
                del providers[key]
                removed.append(_provider_label(key))

    if removed:
        new_text = tomlkit.dumps(doc)
        if new_text != text:
            _atomic_write(config_path, new_text.encode("utf-8"))
    return {"removed": sorted(removed)}
