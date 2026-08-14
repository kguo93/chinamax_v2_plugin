"""Distribution manifests + packaging: pinned identities, version sync, secret sweep.

This is the executable form of ADR 0012's version-bump discipline: it proves the four
version-bearing locations agree and that every ADR-pinned identity literal is present,
including the EXACT top-level and entry key sets (which is what proves no Claude-only
field ever leaks into the Codex catalog). It also runs the repository's secret hygiene
gate — an entropy-shaped, pure-function matcher swept over every tracked and
not-yet-ignored file.

The matcher's pattern strings and this module's synthetic positive samples are all
assembled from concatenated fragments, so neither the checker's own source nor these
tests can ever trip the sweep that scans them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The one description string shared by every ``description`` field across the manifests
#: and pyproject (CONTEXT.md vocabulary).
SHARED_DESCRIPTION = (
    "Expose non-Claude worker models as native subagents of Claude Code and Codex, "
    "routed through one local Proxy."
)
#: The canonical GitHub repository URL (homepage/repository in the Codex plugin manifest).
GITHUB_URL = "https://github.com/kguo93/chinamax_v2_plugin"
#: Kevin Guo, the single author/owner across every manifest.
AUTHOR = {"name": "Kevin Guo"}
#: The synchronized version every version-bearing location must carry.
VERSION = "0.1.0"
#: Kebab-case grammar the Codex plugin spec and the old-repo test both require of every
#: plugin and marketplace name (``chinamaxm`` / ``chinamaxm-plugin`` pass; a styled
#: ``chinamaxM`` fails). This is the naming gate — ``claude plugin validate --strict``
#: cannot run clean here because the repo-root CLAUDE.md always warns, so the gate lives
#: in this test instead.
KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Manifest paths (relative to the repo root).
CLAUDE_PLUGIN = REPO_ROOT / ".claude-plugin" / "plugin.json"
CLAUDE_MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
CODEX_PLUGIN = REPO_ROOT / ".codex-plugin" / "plugin.json"
CODEX_MARKETPLACE = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
PYPROJECT = REPO_ROOT / "pyproject.toml"
GITIGNORE = REPO_ROOT / ".gitignore"
README = REPO_ROOT / "README.md"


def _load_json(path: Path) -> dict:
    """Parse a JSON manifest file, returning its top-level object."""
    return json.loads(path.read_text(encoding="utf-8"))


def _load_pyproject() -> dict:
    """Parse ``pyproject.toml`` and return its ``[project]`` table."""
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


# --------------------------------------------------------------------------- #
# Secret hygiene matcher — a pure function over strings.
#
# Fragment-built so the checker's own source can never self-match. A match needs either
# a token-boundary secret-key prefix followed by 20+ base64url characters, or an
# env-var name ending in the key token, an equals sign, and 8+ non-space value chars.
# --------------------------------------------------------------------------- #
_SK_FRAG = "sk" + "-"
_AK_FRAG = "API" + "_" + "KEY"
# Token-boundary lookbehind so embedded runs (task-, desk-, risk-) never match.
_SK_RE = re.compile(r"(?<![A-Za-z0-9_-])" + _SK_FRAG + r"[A-Za-z0-9_-]{20,}")
_AK_RE = re.compile(r"[A-Za-z0-9_]*" + _AK_FRAG + r"[ \t]*=[ \t]*\S{8,}")


def _has_secret(text: str) -> bool:
    """Return True iff the text contains a key-shaped string (heuristic gate)."""
    return bool(_SK_RE.search(text) or _AK_RE.search(text))


def _git_listed_files() -> list[str] | None:
    """Return tracked + not-yet-ignored file paths, or None if git is unavailable.

    Uses ``git ls-files -z --cached --others --exclude-standard`` so unstaged additions
    are covered while ignored paths stay exempt. The subprocess exit status is checked.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return [chunk.decode("utf-8") for chunk in result.stdout.split(b"\x00") if chunk]


def test_manifest_pinned_values() -> None:
    """AC-1: every ADR 0012-pinned identity literal and exact key set is present."""
    # pyproject metadata (package identity stays chinamaxM; SPDX license per PEP 639).
    project = _load_pyproject()
    assert project["name"] == "chinamaxM"
    assert project["version"] == VERSION
    assert project["description"] == SHARED_DESCRIPTION
    assert project["license"] == "GPL-2.0-only"
    assert project["authors"] == [AUTHOR]
    assert project["requires-python"] == ">=3.12"
    # Landmine guard: the runtime dependency set is preserved intact.
    deps_joined = " ".join(dep.lower() for dep in project["dependencies"])
    for name in ("aiohttp", "litellm", "tomlkit", "pyyaml"):
        assert name in deps_joined, f"dependency {name} missing from pyproject"

    # Claude plugin manifest — exact key set {name, version, description, author}.
    claude_plugin = _load_json(CLAUDE_PLUGIN)
    assert set(claude_plugin) == {"name", "version", "description", "author"}
    assert claude_plugin["name"] == "chinamaxm"
    assert claude_plugin["version"] == VERSION
    assert claude_plugin["description"] == SHARED_DESCRIPTION
    assert claude_plugin["author"] == AUTHOR

    # Claude marketplace manifest — top-level {$schema, name, description, owner, plugins};
    # the top-level description silences `claude plugin validate`'s "No marketplace
    # description provided" warning. NO category.
    claude_market = _load_json(CLAUDE_MARKETPLACE)
    assert set(claude_market) == {"$schema", "name", "description", "owner", "plugins"}
    assert claude_market["$schema"] == "https://anthropic.com/claude-code/marketplace.schema.json"
    assert claude_market["name"] == "chinamaxm-plugin"
    assert claude_market["description"] == SHARED_DESCRIPTION
    assert claude_market["owner"] == AUTHOR
    assert len(claude_market["plugins"]) == 1
    entry = claude_market["plugins"][0]
    assert set(entry) == {"name", "description", "version", "author", "source"}
    assert entry["name"] == "chinamaxm"
    assert entry["description"] == SHARED_DESCRIPTION
    assert entry["version"] == VERSION
    assert entry["author"] == AUTHOR
    assert entry["source"] == "./"

    # Codex plugin manifest — full metadata + preserved skills + the required hooks key +
    # interface block.
    # NOTE: the top-level `hooks` key is REQUIRED — real Codex auto-discovers only the fixed
    # default `hooks/hooks.json`, so the Codex-flavored `./hooks/codex-hooks.json` must be
    # named explicitly (ADR 0012 amended 2026-08-14). The bundled plugin-creator validator
    # rejects the key but is demonstrably stricter than the real Codex CLI (not a gate).
    codex_plugin = _load_json(CODEX_PLUGIN)
    assert set(codex_plugin) == {
        "name",
        "version",
        "description",
        "homepage",
        "repository",
        "license",
        "author",
        "skills",
        "hooks",
        "interface",
    }
    assert codex_plugin["hooks"] == "./hooks/codex-hooks.json"
    assert codex_plugin["name"] == "chinamaxm"
    assert codex_plugin["version"] == VERSION
    assert codex_plugin["description"] == SHARED_DESCRIPTION
    assert codex_plugin["homepage"] == GITHUB_URL
    assert codex_plugin["repository"] == GITHUB_URL
    assert codex_plugin["license"] == "GPL-2.0"  # ADR-pinned literal, not the SPDX id.
    assert codex_plugin["author"] == AUTHOR
    assert codex_plugin["skills"] == "./skills"
    interface = codex_plugin["interface"]
    assert set(interface) == {
        "displayName",
        "shortDescription",
        "longDescription",
        "developerName",
        "category",
        "capabilities",
        "defaultPrompt",
    }
    assert interface["displayName"] == "ChinamaXM"
    assert interface["shortDescription"] == "Worker models as native subagents"
    assert interface["longDescription"] == (
        "Generate native Claude and Codex subagents for non-Claude worker models "
        "(DeepSeek, MiMo, GLM, MiniMax, Kimi, Qwen), each routed through one local "
        "Proxy to its provider."
    )
    assert interface["developerName"] == "Kevin Guo"
    assert interface["category"] == "Developer tools"
    assert interface["capabilities"] == ["Read", "Write"]
    assert interface["defaultPrompt"] == (
        "Use ChinamaXM to delegate a task to a non-Claude worker model as a native "
        "subagent."
    )


def test_version_agreement() -> None:
    """AC-2: the four version-bearing locations agree; drift fails this test."""
    versions = {
        "pyproject": _load_pyproject()["version"],
        "claude_plugin": _load_json(CLAUDE_PLUGIN)["version"],
        "claude_marketplace_entry": _load_json(CLAUDE_MARKETPLACE)["plugins"][0]["version"],
        "codex_plugin": _load_json(CODEX_PLUGIN)["version"],
    }
    assert set(versions.values()) == {VERSION}, versions


def test_codex_catalog_shape() -> None:
    """AC-1: the Codex catalog carries no Claude-only field (exact key sets)."""
    catalog = _load_json(CODEX_MARKETPLACE)
    assert set(catalog) == {"name", "interface", "plugins"}
    assert catalog["name"] == "chinamaxm-plugin"
    assert catalog["interface"] == {"displayName": "chinamaxM"}
    assert len(catalog["plugins"]) == 1
    entry = catalog["plugins"][0]
    assert set(entry) == {"name", "source", "policy", "category"}
    assert entry["name"] == "chinamaxm"
    assert entry["source"] == {"source": "local", "path": "./"}
    assert entry["policy"] == {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}
    assert entry["category"] == "Developer tools"
    # No version/author anywhere in the Codex catalog (proven by the exact key sets above).
    assert "version" not in catalog and "author" not in catalog
    assert "version" not in entry and "author" not in entry


def test_kebab_case_identities() -> None:
    """Naming gate: every plugin and marketplace name is kebab-case (chinamaxM fails)."""
    names = [
        _load_json(CLAUDE_PLUGIN)["name"],
        _load_json(CLAUDE_MARKETPLACE)["name"],
        _load_json(CLAUDE_MARKETPLACE)["plugins"][0]["name"],
        _load_json(CODEX_PLUGIN)["name"],
        _load_json(CODEX_MARKETPLACE)["name"],
        _load_json(CODEX_MARKETPLACE)["plugins"][0]["name"],
    ]
    for name in names:
        assert KEBAB_RE.match(name), f"{name!r} is not kebab-case"
    # The pinned identities the naming gate resolved to (ADR 0012 as amended).
    assert set(names) == {"chinamaxm", "chinamaxm-plugin"}
    # The gate must actually reject the styled package identity.
    assert not KEBAB_RE.match("chinamaxM")


def test_gitignore_covers_byproducts() -> None:
    """AC-5: build/scratch byproduct patterns are all present in .gitignore."""
    lines = {
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    expected = {
        ".scratch/",
        "__pycache__/",
        "*.pyc",
        "*.egg-info/",
        ".pytest_cache/",
        "build/",
        "dist/",
        "*.swp",
        "*.swo",
        "docs/plan/",
    }
    assert expected <= lines, expected - lines


def test_no_secret_patterns_tracked() -> None:
    """AC-5: matcher self-check, then zero key-shaped strings across the repo files."""
    # Pure-function self-check on in-memory synthetic strings (all fragment-built).
    sk_sample = _SK_FRAG + "a" * 24
    sk_ant_sample = _SK_FRAG + "ant" + "-" + "b" * 30
    ak_line = "DEEPSEEK_" + _AK_FRAG + "=" + "x" * 12
    ak_empty = "DEEPSEEK_" + _AK_FRAG + "="
    assert _has_secret(sk_sample)
    assert _has_secret(sk_ant_sample)
    assert _has_secret(ak_line)
    assert not _has_secret(ak_empty)
    # Embedded runs must not be mistaken for a key prefix.
    assert not _has_secret("a task" + "-" + "b" * 30)

    if shutil.which("git") is None:
        pytest.skip("git not available")
    files = _git_listed_files()
    if files is None:
        pytest.skip("not a git checkout")

    offenders: list[str] = []
    for rel in files:
        try:
            text = (REPO_ROOT / rel).read_bytes().decode("utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable file — skip.
        if _has_secret(text):
            offenders.append(rel)
    assert offenders == [], f"key-shaped strings found in: {offenders}"


def test_readme_names_sources() -> None:
    """AC-6: README names the GitHub source, install/upgrade ids, and rpi4-git-only."""
    readme = README.read_text(encoding="utf-8")
    for needle in (
        "chinamaxm@chinamaxm-plugin",
        "kguo93/chinamax_v2_plugin",
        "claude plugin marketplace add kguo93/chinamax_v2_plugin",
        "claude plugin install chinamaxm@chinamaxm-plugin",
        "codex plugin marketplace add kguo93/chinamax_v2_plugin",
        "codex plugin add chinamaxm@chinamaxm-plugin",
        "claude plugin update chinamaxm@chinamaxm-plugin",
        "codex plugin marketplace upgrade",
        "rpi4",
        "git backup",
    ):
        assert needle in readme, f"README missing: {needle!r}"
    # The stale old source and the rpi4 SSH remote URL must never appear.
    assert "kguo93/chinamax_plugin" not in readme
    assert "klg2138@rpi4" not in readme
