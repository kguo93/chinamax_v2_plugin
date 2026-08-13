"""Generation engine: resolved Registry to Host artifacts, drift, and model rewrite.

Both Hosts share this pure engine (ADR 0004 as amended 2026-08-13). From a resolved
Registry it produces, one artifact per Profile:

* a Claude agent markdown at ``<claude-root>/agents/<profile>.md`` — YAML frontmatter
  (``name``/``description``/``model``/optional ``tools``) plus a lean Worker system
  prompt under an ownership marker;
* a Codex ``[model_providers.chinamaxM-<profile>]`` entry in ``<codex-root>/config.toml``
  via TOML-preserving edits (no ``env_key`` — ADR 0006);
* a Codex role TOML at ``<codex-root>/agents/<profile>.toml``.

The model line is dispatch-mutable state: :func:`set_model` rewrites only that line, and
regeneration resets it to the Profile default. :func:`detect_drift` classifies on-disk
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
import tomllib
from pathlib import Path
from typing import Iterable, Literal, Mapping

import tomlkit
import yaml

from .keyfiles import resolve_host_root
from .registry import Profile, Registry, load_registry

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
#: and token-lean; item 4 states the complete-final-report duty hosts-02's hooks enforce.
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
    "said earlier is relayed."
)


class GenerationError(ValueError):
    """Raised by generation and :func:`set_model` on preflight or on-disk faults.

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


def _yaml_model_line(value: str) -> str:
    """Serialize a single ``model:`` frontmatter line (no trailing newline)."""
    dumped = yaml.safe_dump(
        {"model": value},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=_YAML_WIDTH,
    )
    return dumped.rstrip("\n")


def _claude_agent_content(profile: Profile) -> str:
    """Render the full Claude agent markdown for one Profile.

    Frontmatter key order is ``name``, ``description``, ``model``, then ``tools`` only
    when the Profile overrides it (a null ``tools`` omits the key so the host grants its
    standard set). All scalars pass through the YAML serializer for correct escaping.
    """
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


def expected_artifacts(registry: Registry, roots: Mapping[str, Path]) -> dict[Path, str]:
    """Compute the expected whole-file artifacts for every Profile.

    Provider entries are NOT whole-file artifacts (``config.toml`` is only partially
    ours), so they are excluded here and handled structurally by :func:`detect_drift` and
    regeneration.

    Args:
        registry: The resolved Registry.
        roots: A ``{"claude": Path, "codex": Path}`` mapping of Host roots.

    Returns:
        An ordered ``{path: content}`` mapping (Claude agents and Codex role TOMLs), with
        stable ordering and no timestamps.
    """
    claude_agents = _claude_agents_dir(roots)
    codex_agents = _codex_agents_dir(roots)
    artifacts: dict[Path, str] = {}
    for profile in registry.profiles.values():
        artifacts[claude_agents / f"{profile.name}.md"] = _claude_agent_content(profile)
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
    the Profile name) or under an operator-chosen name ``<profile>-<suffix>`` with a
    non-empty suffix (a named spawn surfaces the NAME, not the subagent type — ADR 0004 as
    amended). This is the single shared matcher the worker-contract hook reuses on both its
    SubagentStart (``agent_type``) and PreToolUse (``subagent_type``) branches, so the rule
    is never re-implemented inline.

    Matching is anchored and case-sensitive: a bare substring would misfire (``glm`` inside
    ``glmax``, ``kimi`` inside ``akimi``). Reserved Profile names cannot enter the Registry
    (ADR 0003/0004 as amended), so a built-in agent name can never match. An unrelated
    agent whose name merely starts ``<profile>-`` gets the benign contract — the accepted
    false-positive direction.

    Args:
        name: The candidate ``agent_type`` or ``subagent_type`` from a hook event.
        profile_names: The resolved Registry's Profile names.

    Returns:
        ``True`` when ``name`` equals a Profile name or ``<profile>-<non-empty suffix>``.
    """
    for profile in profile_names:
        if name == profile:
            return True
        prefix = f"{profile}-"
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


# --------------------------------------------------------------- model-line helpers


def _frontmatter_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Return ``(open_index, close_index)`` of the ``---`` frontmatter fences, or None."""
    if not lines or lines[0] != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index] == "---":
            return 0, index
    return None


def _extract_claude_model(text: str) -> str | None:
    """Return the frontmatter model value, or ``None`` when it cannot be found."""
    lines = text.split("\n")
    bounds = _frontmatter_bounds(lines)
    if bounds is None:
        return None
    open_index, close_index = bounds
    for line in lines[open_index + 1 : close_index]:
        if line.startswith("model:"):
            try:
                return yaml.safe_load(line)["model"]
            except Exception:
                return line[len("model:") :].strip()
    return None


def _extract_codex_model(text: str) -> str | None:
    """Return the top-level ``model`` value, or ``None`` when it cannot be found."""
    try:
        parsed = tomllib.loads(text)
    except Exception:
        return None
    value = parsed.get("model")
    return value if isinstance(value, str) else None


def _strip_claude_model_line(text: str) -> str:
    """Return the Claude agent text with its frontmatter ``model:`` line removed."""
    lines = text.split("\n")
    bounds = _frontmatter_bounds(lines)
    if bounds is None:
        return text
    open_index, close_index = bounds
    kept = [
        line
        for index, line in enumerate(lines)
        if not (open_index < index < close_index and line.startswith("model:"))
    ]
    return "\n".join(kept)


def _strip_codex_model_line(text: str) -> str:
    """Return the Codex role text with its top-level ``model =`` line removed."""
    kept = [line for line in text.split("\n") if not re.match(r"model\s*=", line)]
    return "\n".join(kept)


def _rewrite_claude_model_line(text: str, value: str) -> str:
    """Return the Claude agent text with only its frontmatter model line replaced.

    Raises:
        GenerationError: If the text has no frontmatter ``model:`` line to rewrite.
    """
    lines = text.split("\n")
    bounds = _frontmatter_bounds(lines)
    if bounds is not None:
        open_index, close_index = bounds
        for index in range(open_index + 1, close_index):
            if lines[index].startswith("model:"):
                lines[index] = _yaml_model_line(value)
                return "\n".join(lines)
    raise GenerationError("no frontmatter 'model:' line to rewrite")


def _rewrite_codex_model_line(text: str, value: str) -> str:
    """Return the Codex role text with only its top-level ``model`` value replaced.

    The tomlkit round-trip preserves every other byte and escapes the value.
    """
    doc = tomlkit.parse(text)
    doc["model"] = value
    return tomlkit.dumps(doc)


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


def detect_drift(registry: Registry, roots: Mapping[str, Path]) -> dict[str, object]:
    """Classify on-disk state against the expected artifact set without mutating.

    The model line is dispatch-mutable state, so it is excluded from the stale comparison
    and instead surfaced under ``model_lines``. This function never raises on bad on-disk
    state (Doctor is pure diagnosis); unparseable or wrongly-shaped ``config.toml`` states
    are reported under ``conflicts``.

    Args:
        registry: The resolved Registry.
        roots: A ``{"claude": Path, "codex": Path}`` mapping of Host roots.

    Returns:
        ``{"missing", "stale", "foreign", "conflicts"}`` sorted string lists plus
        ``model_lines`` mapping each present artifact path to its current model value.
        File artifacts are labelled by absolute path; provider tables by
        ``model_providers.chinamaxM-<profile>``.
    """
    missing: list[str] = []
    stale: list[str] = []
    foreign: list[str] = []
    conflicts: list[str] = []
    model_lines: dict[str, str | None] = {}

    expected = expected_artifacts(registry, roots)
    expected_paths = set(expected)

    for path, content in expected.items():
        is_codex = path.suffix == ".toml"
        if not path.exists():
            missing.append(str(path))
            continue
        current = _read_text(path)
        if current is None or not _has_marker(current):
            conflicts.append(str(path))
            continue
        model_lines[str(path)] = (
            _extract_codex_model(current) if is_codex else _extract_claude_model(current)
        )
        strip = _strip_codex_model_line if is_codex else _strip_claude_model_line
        if strip(current) != strip(content):
            stale.append(str(path))

    for directory in (_claude_agents_dir(roots), _codex_agents_dir(roots)):
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if not entry.is_file() or entry in expected_paths:
                continue
            text = _read_text(entry)
            if text is not None and _has_marker(text):
                foreign.append(str(entry))

    _classify_providers(registry, roots, missing, stale, foreign, conflicts)

    return {
        "missing": sorted(missing),
        "stale": sorted(stale),
        "foreign": sorted(foreign),
        "conflicts": sorted(conflicts),
        "model_lines": model_lines,
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


def regenerate(registry: Registry, roots: Mapping[str, Path]) -> dict[str, list[str]]:
    """Converge on-disk artifacts to the expected set, then report what changed.

    Preflight (name validation and a ``config.toml`` parse) runs first across both Hosts,
    so no Claude file is written when the Codex side would fail. Only marker-bearing files
    and ``chinamaxM-`` tables are ever overwritten, deleted, or pruned; a conflict path (an
    expected path occupied by a marker-less file) is skipped and re-reported. Writes are
    byte-identical-skipped, atomic, and mode-preserving; a rewritten model line is reset to
    the Profile default because the expected content carries the default.

    Args:
        registry: The resolved Registry.
        roots: A ``{"claude": Path, "codex": Path}`` mapping of Host roots.

    Returns:
        ``{"written", "skipped", "pruned", "conflicts"}`` sorted string lists.

    Raises:
        GenerationError: On any preflight fault (before any write occurs).
    """
    _validate_profile_names(registry)
    config_path = _config_path(roots)
    config_doc = _parse_config_or_raise(config_path)

    written: list[str] = []
    skipped: list[str] = []
    pruned: list[str] = []
    conflicts: list[str] = []

    expected = expected_artifacts(registry, roots)
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

    for directory in (_claude_agents_dir(roots), _codex_agents_dir(roots)):
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_file() or entry in expected_paths:
                continue
            text = _read_text(entry)
            if text is not None and _has_marker(text):
                entry.unlink()
                pruned.append(str(entry))

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


# ------------------------------------------------------------------ model rewrite


def set_model(host: Literal["claude", "codex"], profile: str, model: str) -> Path:
    """Rewrite only the model line of one generated artifact (the dispatch conduit).

    Resolves the Host root through the canonical chain. The model string is never
    validated — an arbitrary value (spaces, quotes) lands verbatim, serializer-escaped.
    The Claude frontmatter becomes ``model: <profile>/<model>``; the Codex role becomes
    ``model = <model>`` (bare). Every other byte is untouched; the write is atomic and
    mode-preserving. A marker-less target file is refused untouched.

    Args:
        host: ``"claude"`` or ``"codex"``.
        profile: A Profile name in the resolved Registry.
        model: The replacement model string (never validated).

    Returns:
        The path of the rewritten artifact.

    Raises:
        GenerationError: On an unknown Host, an unknown Profile, a missing artifact, or a
            marker-less target file.
    """
    if host not in ("claude", "codex"):
        raise GenerationError(f"unknown host {host!r}; expected 'claude' or 'codex'")

    registry = load_registry()
    if profile not in registry.profiles:
        valid = ", ".join(sorted(registry.profiles))
        raise GenerationError(f"unknown profile {profile!r}; valid profiles: {valid}")

    root = resolve_host_root(host)
    suffix = "md" if host == "claude" else "toml"
    path = root / AGENTS_DIR_NAME / f"{profile}.{suffix}"
    if not path.exists():
        raise GenerationError(
            f"no generated agent at {path}; run generation before setting a model"
        )
    text = _read_text(path)
    if text is None or not _has_marker(text):
        raise GenerationError(f"refusing to edit marker-less file {path}")

    if host == "claude":
        new_text = _rewrite_claude_model_line(text, f"{profile}/{model}")
    else:
        new_text = _rewrite_codex_model_line(text, model)
    _atomic_write(path, new_text.encode("utf-8"))
    return path
