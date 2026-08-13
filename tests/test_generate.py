"""Generation-engine tests: Registry-in → artifact-files-out against temp roots.

Every test runs under pytest ``tmp_path`` Host roots injected through the canonical-chain
env overrides (``CHINAMAXM_CLAUDE_HOME`` / ``CHINAMAXM_CODEX_HOME``). An autouse guard
patches ``Path.home`` and ``os.path.expanduser`` to raise, so the whole module is proof
that no test touches a real home directory (issue AC-7).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
import tomlkit
import yaml

from chinamaxM.generate import (
    CLAUDE_MARKER,
    CODEX_MARKER,
    WORKER_INSTRUCTIONS,
    GenerationError,
    detect_drift,
    expected_artifacts,
    regenerate,
    resolve_roots,
    set_model,
)
from chinamaxM.keyfiles import resolve_host_root
from chinamaxM.registry import Profile, Registry, load_registry


# --------------------------------------------------------------------------- guards


@pytest.fixture(autouse=True)
def _no_real_home(monkeypatch):
    """Fail any test that resolves a real home via ``Path.home`` or ``expanduser``."""

    def boom(*_args, **_kwargs):
        raise AssertionError("test resolved a real home directory")

    monkeypatch.setattr(Path, "home", boom)
    monkeypatch.setattr(os.path, "expanduser", boom)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Inject temp Host roots via the canonical-chain env overrides; return the mapping."""
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    monkeypatch.setenv("CHINAMAXM_CLAUDE_HOME", str(claude))
    monkeypatch.setenv("CHINAMAXM_CODEX_HOME", str(codex))
    return {"claude": claude, "codex": codex}


# ----------------------------------------------------------------------- helpers


def _frontmatter(text: str) -> dict:
    """Parse the YAML frontmatter block of a Claude agent markdown file."""
    lines = text.split("\n")
    assert lines[0] == "---", "agent file must open with a frontmatter fence"
    close = lines.index("---", 1)
    return yaml.safe_load("\n".join(lines[1:close]))


def _minimal_profile(name: str) -> Profile:
    """Build a minimal valid Profile with the given (possibly illegal) name."""
    return Profile(
        name=name,
        dialect="anthropic",
        base_url="https://example.invalid/anthropic",
        api_key_env="EXAMPLE_API_KEY",
        default_model="m",
    )


# ----------------------------------------------------------------- Claude agents


def test_claude_agentset_for_seed_registry(roots):
    """AC-1: six-Profile seed ⇒ six agents, prefixed model, no tools key for null tools."""
    regenerate(load_registry(), roots)
    agents = roots["claude"] / "agents"
    assert {p.name for p in agents.iterdir()} == {
        "deepseek.md",
        "mimo.md",
        "glm.md",
        "minimax.md",
        "kimi.md",
        "qwen.md",
    }
    text = (agents / "deepseek.md").read_text()
    front = _frontmatter(text)
    assert front["name"] == "deepseek"
    assert front["model"] == "deepseek/deepseek-v4-pro[1m]"
    assert front["description"].startswith("chinamaxM worker on ")
    assert "tools" not in front
    body = text.split("---\n", 2)[2]
    assert body.startswith(CLAUDE_MARKER + "\n")
    assert WORKER_INSTRUCTIONS in body


def test_profile_tools_override(roots):
    """AC-1: a Profile overriding tools emits exactly that list."""
    base = load_registry()
    profile = replace(base.profiles["deepseek"], tools=["Read", "Bash"])
    registry = Registry(port=base.port, profiles={"deepseek": profile})
    regenerate(registry, roots)
    front = _frontmatter((roots["claude"] / "agents" / "deepseek.md").read_text())
    assert front["tools"] == ["Read", "Bash"]


# --------------------------------------------------------------- Codex providers


_CONFIG_FIXTURE = """# user codex config
model = "gpt-5"
approval_policy = "on-request"

[tui]
theme = "dark"

[history]
persistence = "save-all"

[[mcp_servers]]
command = "one"

[[mcp_servers]]
command = "two"
"""


def test_codex_provider_entries_preserve_toml(roots):
    """AC-2: TOML-preserving edit, port from Registry, no env_key anywhere."""
    config = roots["codex"] / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(_CONFIG_FIXTURE)
    base = load_registry()
    registry = Registry(port=9107, profiles=base.profiles)
    regenerate(registry, roots)

    result = config.read_text()
    assert result.startswith(_CONFIG_FIXTURE)  # unrelated bytes untouched
    data = tomllib.loads(result)
    assert data["model"] == "gpt-5"
    assert data["tui"] == {"theme": "dark"}
    assert data["mcp_servers"] == [{"command": "one"}, {"command": "two"}]
    assert data["model_providers"]["chinamaxM-deepseek"] == {
        "name": "chinamaxM-deepseek",
        "base_url": "http://127.0.0.1:9107/openai/deepseek",
        "wire_api": "responses",
    }
    for name in ("deepseek", "mimo", "glm", "minimax", "kimi", "qwen"):
        assert "env_key" not in data["model_providers"][f"chinamaxM-{name}"]


def test_unparseable_config_aborts_before_any_write(roots):
    """AC-2/AC-3: an unparseable config.toml ⇒ raise and ZERO artifacts written."""
    config = roots["codex"] / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("this is [not valid == toml")
    with pytest.raises(GenerationError):
        regenerate(load_registry(), roots)
    assert not (roots["claude"] / "agents").exists()
    assert not (roots["codex"] / "agents").exists()


def test_reserved_name_refused(roots):
    """AC-3: a Profile named like a reserved ID on either Host ⇒ raise, no artifacts."""
    registry = Registry(
        port=8402,
        profiles={
            "openai": _minimal_profile("openai"),
            "general-purpose": _minimal_profile("general-purpose"),
        },
    )
    with pytest.raises(GenerationError) as excinfo:
        regenerate(registry, roots)
    assert "reserved" in str(excinfo.value)
    assert not (roots["claude"] / "agents").exists()
    assert not (roots["codex"] / "agents").exists()


@pytest.mark.parametrize("bad", ["dee/p", "dee.p", "dee_p", "Deep"])
def test_preflight_rejects_bad_names(roots, bad):
    """AC-3: a name with '/', '.', '_', or uppercase ⇒ raise before any write."""
    registry = Registry(port=8402, profiles={bad: _minimal_profile(bad)})
    with pytest.raises(GenerationError):
        regenerate(registry, roots)
    assert not (roots["claude"] / "agents").exists()


# --------------------------------------------------------------------- Codex roles


def test_role_tomls(roots):
    """AC-4: marker, key set, bare model, pinned context window, no reasoning effort."""
    regenerate(load_registry(), roots)
    text = (roots["codex"] / "agents" / "deepseek.toml").read_text()
    assert text.startswith(CODEX_MARKER + "\n")
    data = tomllib.loads(text)
    assert data["name"] == "deepseek"
    assert data["description"] == "chinamaxM worker on deepseek"
    assert data["model"] == "deepseek-v4-pro[1m]"  # bare, no profile prefix
    assert data["model_provider"] == "chinamaxM-deepseek"
    assert data["model_context_window"] == 1000000
    assert data["developer_instructions"] == WORKER_INSTRUCTIONS
    assert "model_reasoning_effort" not in data

    mimo = tomllib.loads((roots["codex"] / "agents" / "mimo.toml").read_text())
    assert "model_context_window" not in mimo  # Registry pins none for mimo
    assert mimo["model"] == "mimo-v2.5-pro"


# --------------------------------------------------------------------- idempotence


def test_generation_idempotent(roots):
    """AC-5: second run zero writes; a dispatch rewrite is reset to default on regen."""
    registry = load_registry()
    regenerate(registry, roots)
    tracked = list(expected_artifacts(registry, roots)) + [roots["codex"] / "config.toml"]
    before_mtimes = {p: p.stat().st_mtime_ns for p in tracked}
    before_bytes = {p: p.read_bytes() for p in tracked}

    report = regenerate(registry, roots)
    assert report["written"] == []
    assert {p: p.stat().st_mtime_ns for p in tracked} == before_mtimes
    assert {p: p.read_bytes() for p in tracked} == before_bytes

    # dispatch-style rewrite then regeneration resets ONLY that model line to default
    set_model("claude", "deepseek", "deepseek-x")
    deepseek = roots["claude"] / "agents" / "deepseek.md"
    assert _frontmatter(deepseek.read_text())["model"] == "deepseek/deepseek-x"
    untouched = roots["claude"] / "agents" / "glm.md"
    untouched_mtime = untouched.stat().st_mtime_ns

    report2 = regenerate(registry, roots)
    assert report2["written"] == [str(deepseek)]
    assert _frontmatter(deepseek.read_text())["model"] == "deepseek/deepseek-v4-pro[1m]"
    assert untouched.stat().st_mtime_ns == untouched_mtime


# ------------------------------------------------------------------ drift detection


def test_drift_stale_missing_foreign(roots):
    """AC-6: hand-edit ⇒ stale, delete ⇒ missing, extra marker file ⇒ foreign + prune."""
    registry = load_registry()
    regenerate(registry, roots)
    claude_agents = roots["claude"] / "agents"
    codex_agents = roots["codex"] / "agents"

    deepseek = claude_agents / "deepseek.md"
    deepseek.write_text(deepseek.read_text() + "\nhand edit\n")  # marker intact
    (codex_agents / "mimo.toml").unlink()
    rogue = claude_agents / "rogue.md"
    rogue.write_text(f"---\nname: rogue\n---\n{CLAUDE_MARKER}\nx\n")

    drift = detect_drift(registry, roots)
    assert str(deepseek) in drift["stale"]
    assert str(codex_agents / "mimo.toml") in drift["missing"]
    assert str(rogue) in drift["foreign"]

    report = regenerate(registry, roots)
    assert str(rogue) in report["pruned"]
    assert not rogue.exists()
    assert str(deepseek) in report["written"]
    assert (codex_agents / "mimo.toml").exists()
    clean = detect_drift(registry, roots)
    assert clean["stale"] == clean["missing"] == clean["foreign"] == clean["conflicts"] == []


def test_drift_model_line_excepted(roots):
    """AC-6: a dispatch-style model-line rewrite is NOT stale, surfaced in model_lines."""
    registry = load_registry()
    regenerate(registry, roots)
    set_model("claude", "deepseek", "deepseek-x")
    set_model("codex", "deepseek", "deepseek-y")

    drift = detect_drift(registry, roots)
    md = str(roots["claude"] / "agents" / "deepseek.md")
    toml = str(roots["codex"] / "agents" / "deepseek.toml")
    assert md not in drift["stale"]
    assert toml not in drift["stale"]
    assert drift["model_lines"][md] == "deepseek/deepseek-x"
    assert drift["model_lines"][toml] == "deepseek-y"


def test_drift_registry_change(roots):
    """AC-6: context-window change ⇒ stale; add ⇒ missing; remove ⇒ foreign."""
    registry = load_registry()
    regenerate(registry, roots)

    changed = replace(
        registry.profiles["deepseek"], context_window={"deepseek-v4-pro[1m]": 2_000_000}
    )
    reg_cw = Registry(port=registry.port, profiles={**registry.profiles, "deepseek": changed})
    assert str(roots["codex"] / "agents" / "deepseek.toml") in detect_drift(reg_cw, roots)["stale"]

    added = Registry(
        port=registry.port, profiles={**registry.profiles, "zed": _minimal_profile("zed")}
    )
    drift_add = detect_drift(added, roots)
    assert str(roots["claude"] / "agents" / "zed.md") in drift_add["missing"]
    assert "model_providers.chinamaxM-zed" in drift_add["missing"]

    removed = Registry(
        port=registry.port,
        profiles={k: v for k, v in registry.profiles.items() if k != "qwen"},
    )
    drift_rm = detect_drift(removed, roots)
    assert str(roots["claude"] / "agents" / "qwen.md") in drift_rm["foreign"]
    assert str(roots["codex"] / "agents" / "qwen.toml") in drift_rm["foreign"]
    assert "model_providers.chinamaxM-qwen" in drift_rm["foreign"]


def test_drift_conflict_markerless(roots):
    """AC-6: a marker-less file at an expected path ⇒ conflict, left untouched by regen."""
    registry = load_registry()
    regenerate(registry, roots)
    deepseek = roots["claude"] / "agents" / "deepseek.md"
    deepseek.write_text("user authored, no marker\n")
    original = deepseek.read_bytes()

    assert str(deepseek) in detect_drift(registry, roots)["conflicts"]
    report = regenerate(registry, roots)
    assert str(deepseek) in report["conflicts"]
    assert deepseek.read_bytes() == original


def test_drift_provider_tables(roots):
    """AC-6: provider-table stale/missing/foreign + env_key removal on regen."""
    registry = load_registry()
    regenerate(registry, roots)
    config = roots["codex"] / "config.toml"
    doc = tomlkit.parse(config.read_text())
    doc["model_providers"]["chinamaxM-deepseek"]["env_key"] = "SNEAK"
    doc["model_providers"]["chinamaxM-glm"]["base_url"] = "http://evil"
    del doc["model_providers"]["chinamaxM-kimi"]
    rogue = tomlkit.table()
    rogue["name"] = "chinamaxM-rogue"
    doc["model_providers"]["chinamaxM-rogue"] = rogue
    config.write_text(tomlkit.dumps(doc))

    drift = detect_drift(registry, roots)
    assert "model_providers.chinamaxM-deepseek" in drift["stale"]
    assert "model_providers.chinamaxM-glm" in drift["stale"]
    assert "model_providers.chinamaxM-kimi" in drift["missing"]
    assert "model_providers.chinamaxM-rogue" in drift["foreign"]

    regenerate(registry, roots)
    data = tomllib.loads(config.read_text())
    assert "env_key" not in data["model_providers"]["chinamaxM-deepseek"]
    assert (
        data["model_providers"]["chinamaxM-glm"]["base_url"]
        == f"http://127.0.0.1:{registry.port}/openai/glm"
    )
    assert "chinamaxM-kimi" in data["model_providers"]
    assert "chinamaxM-rogue" not in data["model_providers"]


# ----------------------------------------------------------------------- roots


def test_codex_home_override_precedence(tmp_path, monkeypatch):
    """AC-7: CHINAMAXM_CODEX_HOME wins over an ambient CODEX_HOME (precedence proof)."""
    override = tmp_path / "override-codex"
    ambient = tmp_path / "ambient-codex"
    monkeypatch.setenv("CHINAMAXM_CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("CHINAMAXM_CODEX_HOME", str(override))
    monkeypatch.setenv("CODEX_HOME", str(ambient))

    assert resolve_host_root("codex") == override
    regenerate(load_registry(), resolve_roots())
    assert (override / "agents" / "deepseek.toml").exists()
    assert not ambient.exists()


# --------------------------------------------------------------------- set_model


def test_set_model_rewrites_model_line(roots):
    """set_model rewrites ONLY the model line on both Hosts; never validates the string."""
    registry = load_registry()
    regenerate(registry, roots)
    md = roots["claude"] / "agents" / "deepseek.md"
    toml = roots["codex"] / "agents" / "deepseek.toml"

    before_md = md.read_text()
    assert set_model("claude", "deepseek", "deepseek-x") == md
    after_md = md.read_text()
    assert _frontmatter(after_md)["model"] == "deepseek/deepseek-x"
    md_diff = [
        (a, b) for a, b in zip(before_md.split("\n"), after_md.split("\n")) if a != b
    ]
    assert len(before_md.split("\n")) == len(after_md.split("\n"))
    assert len(md_diff) == 1 and md_diff[0][0].startswith("model:")

    before_toml = toml.read_text()
    set_model("codex", "deepseek", "deepseek-x")
    after_toml = toml.read_text()
    assert tomllib.loads(after_toml)["model"] == "deepseek-x"  # bare
    toml_diff = [
        (a, b) for a, b in zip(before_toml.split("\n"), after_toml.split("\n")) if a != b
    ]
    assert len(before_toml.split("\n")) == len(after_toml.split("\n"))
    assert len(toml_diff) == 1 and toml_diff[0][0].startswith("model =")

    # unknown profile errors naming the valid Profile list
    with pytest.raises(GenerationError) as excinfo:
        set_model("claude", "nope", "x")
    assert "nope" in str(excinfo.value) and "deepseek" in str(excinfo.value)

    # marker-less target file refused untouched
    glm = roots["claude"] / "agents" / "glm.md"
    glm.write_text("no marker here\n")
    with pytest.raises(GenerationError):
        set_model("claude", "glm", "x")
    assert glm.read_text() == "no marker here\n"

    # arbitrary model string with spaces/quotes lands verbatim, serializer-escaped
    set_model("claude", "deepseek", 'weird "m" v')
    assert _frontmatter(md.read_text())["model"] == 'deepseek/weird "m" v'
    set_model("codex", "deepseek", 'weird "m" v')
    assert tomllib.loads(toml.read_text())["model"] == 'weird "m" v'

    # regeneration resets both model lines to the Profile default
    regenerate(registry, roots)
    assert _frontmatter(md.read_text())["model"] == "deepseek/deepseek-v4-pro[1m]"
    assert tomllib.loads(toml.read_text())["model"] == "deepseek-v4-pro[1m]"
