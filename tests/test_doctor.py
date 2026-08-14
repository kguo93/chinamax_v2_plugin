"""Doctor engine, /profiles renderer, and the LIVE SessionStart hook (hosts-04).

All roster tests start from ONE fully-valid fixture install (temp Claude/Codex roots, a
regenerated artifact set, key files, a settings env flip, a live loopback listener) and
patched runners returning healthy outputs; each case mutates exactly one aspect and asserts
exactly the intended Finding changes plus any documented dependent skips. The SessionStart
test is behavioral: it runs the EXACT registered command string via subprocess.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from chinamaxM import doctor
from chinamaxM.doctor import (
    CODEX_BASELINE,
    LITELLM_PIN,
    Finding,
    normalize_flip_url,
    render_profiles,
    render_report,
    run_doctor,
)
from chinamaxM.generate import regenerate, set_model
from chinamaxM.ops.supervision import SupervisionError, SupervisionStatus, port_live
from chinamaxM.registry import load_registry

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A key VALUE canary: every key file in the fixture holds it, and it must never appear in
#: any doctor/profiles output (ADR 0006's never-print rule).
CANARY = "sk-" + "canary-NEVER-PRINT-2f9a"  # fragment-built so the secret sweep never trips

#: The pinned CLI invocations the command files and Codex skill twins must both carry.
DOCTOR_CLI = "conda run -n chinamaxM python -m chinamaxM.doctor"
PROFILES_CLI = "conda run -n chinamaxM python -m chinamaxM.doctor --profiles"

CP = subprocess.CompletedProcess


# --------------------------------------------------------------------------- helpers


def make_run(conda: object = None, codex: object = None):
    """Build an injectable doctor runner dispatching by ``argv[0]`` (conda / codex).

    A value is returned; an ``Exception`` instance is raised (to simulate a missing binary
    or a timeout). ``None`` yields the healthy default for that tool.
    """

    def _run(argv, *, timeout, env=None):
        if argv[0] == "conda":
            if isinstance(conda, BaseException):
                raise conda
            return conda if conda is not None else CP(argv, 0, f"{LITELLM_PIN}\n", "")
        if argv[0] == "codex":
            if isinstance(codex, BaseException):
                raise codex
            return codex if codex is not None else CP(argv, 0, f"codex {CODEX_BASELINE}\n", "")
        return CP(argv, 0, "", "")

    return _run


def service_returning(installed: bool, enabled: bool, running: bool):
    """A service_status seam returning a fixed :class:`SupervisionStatus`."""
    return lambda cfg: SupervisionStatus(installed, enabled, running, True)


def _service_raises(cfg):
    """A service_status seam that raises (simulates a systemctl fault)."""
    raise SupervisionError(message="simulated systemctl fault")


def find(findings: list[Finding], fid: str) -> list[Finding]:
    """Return every finding with the given id."""
    return [f for f in findings if f.id == fid]


def one(findings: list[Finding], fid: str) -> Finding:
    """Return the single finding with the given id (asserting uniqueness)."""
    got = find(findings, fid)
    assert len(got) == 1, (fid, got)
    return got[0]


def _hash_tree(*roots: Path) -> str:
    """Hash the recursive file contents of one or more roots (order-independent by path)."""
    digest = hashlib.sha256()
    for root in roots:
        for path in sorted(Path(root).rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode("utf-8"))
                digest.update(path.read_bytes())
    return digest.hexdigest()


@dataclass
class Install:
    """A fully-valid fixture install plus its live-listener port."""

    claude: Path
    codex: Path
    overlay: Path
    port: int
    scan_clean: Path


@pytest.fixture
def install(tmp_path, monkeypatch):
    """Build a valid install: regenerated artifacts, key files, settings flip, live port."""
    monkeypatch.delenv("CLAUDE_CODE_SUBAGENT_MODEL", raising=False)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]

    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    claude.mkdir()
    codex.mkdir()
    scan_clean = tmp_path / "scan-clean"
    scan_clean.mkdir()

    overlay = claude / "chinamaxM" / "profiles.json"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(json.dumps({"port": port}), encoding="utf-8")

    registry = load_registry(overlay_path=overlay)
    regenerate(registry, {"claude": claude, "codex": codex})

    keys = "".join(f"{p.api_key_env}={CANARY}\n" for p in registry.profiles.values())
    (claude / "model-keys.env").write_text(keys, encoding="utf-8")
    (codex / "model-keys.env").write_text(keys, encoding="utf-8")

    (claude / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"}}), encoding="utf-8"
    )

    try:
        yield Install(claude, codex, overlay, port, scan_clean)
    finally:
        listener.close()


def run(inst: Install, **overrides):
    """Run the doctor over an install with healthy defaults, honoring per-test overrides."""
    kwargs = dict(
        claude_root=inst.claude,
        codex_root=inst.codex,
        overlay_path=inst.overlay,
        run=make_run(),
        port_probe=port_live,
        service_status=service_returning(True, True, True),
        scan_tree=inst.scan_clean,
    )
    kwargs.update(overrides)
    return run_doctor(**kwargs)


def set_settings(inst: Install, text: str) -> None:
    """Overwrite the fixture's settings.json with raw text."""
    (inst.claude / "settings.json").write_text(text, encoding="utf-8")


# ------------------------------------------------------------- test 1 (FAIL checks)


def test_all_valid_install_is_clean(install):
    """The fully-valid fixture yields no FAIL findings and exit 0 (baseline)."""
    findings, code = run(install)
    assert code == 0, [f for f in findings if f.status in ("fail", "error")]
    assert one(findings, "registry").status == "ok"
    assert one(findings, "conda").status == "ok"
    assert one(findings, "service").status == "ok"
    assert one(findings, "port").status == "ok"
    assert one(findings, "env-flip-present").status == "ok"
    assert one(findings, "env-flip-target").status == "ok"
    assert one(findings, "drift-claude").status == "ok"
    assert one(findings, "drift-codex").status == "ok"


def test_conda_missing_env(install):
    findings, code = run(install, run=make_run(conda=CP(["conda"], 1, "", "Could not find conda environment: chinamaxM")))
    f = one(findings, "conda")
    assert f.status == "fail" and "not found" in f.detail
    assert code == 1


def test_conda_import_failure(install):
    findings, _ = run(install, run=make_run(conda=CP(["conda"], 1, "", "ModuleNotFoundError: No module named 'litellm'")))
    f = one(findings, "conda")
    assert f.status == "fail" and "import failed" in f.detail


def test_conda_version_mismatch(install):
    findings, _ = run(install, run=make_run(conda=CP(["conda"], 0, "1.95.0\n", "")))
    f = one(findings, "conda")
    assert f.status == "fail" and "1.95.0" in f.detail and LITELLM_PIN in f.detail


def test_conda_not_found(install):
    findings, _ = run(install, run=make_run(conda=FileNotFoundError()))
    assert one(findings, "conda").detail == "conda not found on PATH"


@pytest.mark.parametrize(
    "installed,enabled,running",
    [(True, False, False), (True, True, False), (False, False, False)],
)
def test_service_states_fail(install, installed, enabled, running):
    findings, code = run(install, service_status=service_returning(installed, enabled, running))
    f = one(findings, "service")
    assert f.status == "fail"
    assert f.detail == f"installed={installed} enabled={enabled} running={running}"
    assert code == 1


def test_service_exception_is_error(install):
    findings, code = run(install, service_status=_service_raises)
    f = one(findings, "service")
    assert f.status == "error" and "SupervisionError" in f.detail
    assert code == 1  # a FAIL-level error flips the exit


def test_port_closed(install):
    findings, code = run(install, port_probe=lambda p: False)
    f = one(findings, "port")
    assert f.status == "fail" and str(install.port) in f.detail
    assert code == 1


def test_env_flip_absent(install):
    set_settings(install, json.dumps({"env": {}}))
    findings, code = run(install)
    assert one(findings, "env-flip-present").status == "fail"
    assert one(findings, "env-flip-target").status == "skipped"
    assert code == 1


def test_env_flip_wrong_port(install):
    set_settings(install, json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{install.port + 1}"}}))
    findings, code = run(install)
    assert one(findings, "env-flip-present").status == "ok"
    target = one(findings, "env-flip-target")
    assert target.status == "fail" and str(install.port) in target.detail
    assert code == 1


def test_env_flip_non_loopback(install):
    set_settings(install, json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://10.0.0.1:{install.port}"}}))
    findings, _ = run(install)
    assert one(findings, "env-flip-present").status == "ok"
    assert "loopback" in one(findings, "env-flip-target").detail


def test_env_flip_malformed_settings(install):
    set_settings(install, "{ not valid json")
    findings, _ = run(install)
    f = one(findings, "env-flip-present")
    assert f.status == "fail" and "not valid JSON" in f.detail


def test_env_flip_root_not_object(install):
    set_settings(install, "[]")
    findings, _ = run(install)
    assert "root is not a JSON object" in one(findings, "env-flip-present").detail


def test_env_flip_env_not_dict(install):
    set_settings(install, json.dumps({"env": "nope"}))
    findings, _ = run(install)
    assert "env' block is not a JSON object" in one(findings, "env-flip-present").detail


def test_drift_claude_only(install):
    agent = install.claude / "agents" / "deepseek.md"
    agent.write_text(agent.read_text(encoding="utf-8") + "\nhand-edited body line\n", encoding="utf-8")
    findings, code = run(install)
    assert one(findings, "drift-claude").status == "fail"
    assert one(findings, "drift-codex").status == "ok"
    assert code == 1


def test_drift_codex_only(install):
    (install.codex / "agents" / "deepseek.toml").unlink()
    findings, code = run(install)
    assert one(findings, "drift-claude").status == "ok"
    assert one(findings, "drift-codex").status == "fail"
    assert code == 1


def test_strict_config_demoted_to_skipped(install):
    """The strict-config check ships skipped (setup-only) — no codex command is ever run."""
    findings, code = run(install)
    f = one(findings, "codex-strict-config")
    assert f.status == "skipped" and "setup-only" in f.detail
    assert code == 0  # a skipped FAIL-level check never flips the exit


def test_codex_root_absent_skips_codex_checks(tmp_path, install):
    absent = tmp_path / "no-codex"
    findings, _ = run(install, codex_root=absent)
    assert one(findings, "codex-strict-config").status == "skipped"
    assert str(absent) in one(findings, "codex-strict-config").detail
    assert one(findings, "codex-version").status == "skipped"
    assert one(findings, "drift-codex").status == "skipped"


def test_codex_binary_absent_with_root_present(install):
    """codex binary absent + Codex root present: strict-config skipped, version WARN."""
    findings, _ = run(install, run=make_run(codex=FileNotFoundError()))
    assert one(findings, "codex-strict-config").status == "skipped"
    version = one(findings, "codex-version")
    assert version.status == "warn" and "could not determine" in version.detail


# --------------------------------------------------------------- test 2 (WARN checks)


def test_key_missing_in_one_file(install):
    registry = load_registry(overlay_path=install.overlay)
    keys = "".join(
        f"{p.api_key_env}={CANARY}\n" for p in registry.profiles.values() if p.api_key_env != "DEEPSEEK_API_KEY"
    )
    (install.claude / "model-keys.env").write_text(keys, encoding="utf-8")
    findings, code = run(install)
    assert one(findings, "key-claude-deepseek").status == "warn"
    assert one(findings, "key-claude-kimi").status == "ok"
    assert code == 0  # WARN never flips the exit


def test_key_present_claude_missing_codex(install):
    registry = load_registry(overlay_path=install.overlay)
    keys = "".join(
        f"{p.api_key_env}={CANARY}\n" for p in registry.profiles.values() if p.api_key_env != "DEEPSEEK_API_KEY"
    )
    (install.codex / "model-keys.env").write_text(keys, encoding="utf-8")
    findings, _ = run(install)
    assert one(findings, "key-claude-deepseek").status == "ok"
    assert one(findings, "key-codex-deepseek").status == "warn"


def test_subagent_model_in_process_env(install, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL", "claude-opus-4-8")
    findings, code = run(install)
    f = one(findings, "subagent-model")
    assert f.status == "warn" and "process environment" in f.detail
    assert code == 0


def test_subagent_model_in_settings(install, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SUBAGENT_MODEL", raising=False)
    set_settings(
        install,
        json.dumps(
            {"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{install.port}", "CLAUDE_CODE_SUBAGENT_MODEL": "x"}}
        ),
    )
    findings, _ = run(install)
    f = one(findings, "subagent-model")
    assert f.status == "warn" and "settings env block" in f.detail


def test_codex_version_mismatch(install):
    findings, _ = run(install, run=make_run(codex=CP(["codex"], 0, "codex 0.145.0\n", "")))
    f = one(findings, "codex-version")
    assert f.status == "warn" and "0.145.0" in f.detail and CODEX_BASELINE in f.detail


def test_codex_version_unparseable(install):
    findings, _ = run(install, run=make_run(codex=CP(["codex"], 0, "no version here\n", "")))
    f = one(findings, "codex-version")
    assert f.status == "warn" and "could not determine" in f.detail


def test_kimi_scan_clean(install):
    findings, code = run(install)
    f = one(findings, "kimi-responses-bug")
    assert f.status == "ok" and "unaffected" in f.detail
    assert code == 0


def test_kimi_scan_hit(install, tmp_path):
    planted = tmp_path / "planted"
    planted.mkdir()
    # Build the banned slashed URL by concatenation so this test's own source never trips it.
    needle = "chat" + "/" + "completions"
    (planted / "bad.py").write_text(f'URL = "https://api.example.com/v1/{needle}"\n', encoding="utf-8")
    findings, code = run(install, scan_tree=planted)
    f = one(findings, "kimi-responses-bug")
    assert f.status == "warn" and "#33921" in f.detail
    assert code == 0  # kimi is WARN-level and never flips the exit


# -------------------------------------------------------------- test 3 (exit semantics)


def test_exit_all_ok(install):
    _, code = run(install)
    assert code == 0


def test_exit_warn_only(install):
    (install.claude / "model-keys.env").write_text("", encoding="utf-8")  # all keys MISSING (WARN)
    findings, code = run(install)
    assert any(f.level == "warn" and f.status == "warn" for f in findings)
    assert code == 0


def test_exit_one_fail(install):
    _, code = run(install, port_probe=lambda p: False)
    assert code == 1


def test_exit_warn_check_crash_stays_zero(install, monkeypatch):
    def _boom(_tree):
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(doctor, "find_chat_completions_references", _boom)
    findings, code = run(install)
    kimi = one(findings, "kimi-responses-bug")
    assert kimi.level == "warn" and kimi.status == "error"
    assert code == 0  # a crashing WARN check cannot flip the exit


# ---------------------------------------------------- test 4 (no mutation, no spend)


async def test_doctor_never_mutates_or_spends(install, fake_provider):
    before = _hash_tree(install.claude, install.codex)
    findings, _ = run(install)
    after = _hash_tree(install.claude, install.codex)
    assert before == after  # the tree is byte-identical after diagnosis
    assert fake_provider.requests == []  # no provider egress of any kind
    assert findings  # the roster actually ran


# --------------------------------------------------------------- test 5 (profiles)


def test_profiles_renderer(install):
    text, code = render_profiles(
        claude_root=install.claude, codex_root=install.codex, overlay_path=install.overlay
    )
    assert code == 0
    for name in ("deepseek", "mimo", "glm", "minimax", "kimi", "qwen"):
        assert name in text
    assert "DEEPSEEK_API_KEY" in text
    assert "claude key: PRESENT" in text
    assert CANARY not in text  # ADR 0006: never print a key value


def test_profiles_codex_root_absent(tmp_path, install):
    absent = tmp_path / "no-codex"
    text, code = render_profiles(claude_root=install.claude, codex_root=absent, overlay_path=install.overlay)
    assert code == 0
    assert f"codex key: skipped (codex root {absent} absent)" in text


def test_profiles_registry_unreadable(install):
    install.overlay.write_text("{ not json", encoding="utf-8")
    text, code = render_profiles(
        claude_root=install.claude, codex_root=install.codex, overlay_path=install.overlay
    )
    assert code == 1
    assert "registry unreadable" in text


def test_full_report_never_prints_key_value(install):
    findings, _ = run(install)
    assert CANARY not in render_report(findings)


def test_profiles_shows_current_model_after_rewrite(install, monkeypatch):
    """A dispatch model rewrite surfaces the current model line in /profiles."""
    monkeypatch.setenv("CHINAMAXM_CLAUDE_HOME", str(install.claude))
    monkeypatch.setenv("CHINAMAXM_CODEX_HOME", str(install.codex))
    set_model("claude", "deepseek", "deepseek-v4-flash")
    text, _ = render_profiles(
        claude_root=install.claude, codex_root=install.codex, overlay_path=install.overlay
    )
    assert "current model: deepseek-v4-flash" in text


# ---------------------------------------------- test 6 (SessionStart hook, LIVE)


def _session_start_command() -> str:
    """Return the registered SessionStart command with ${CLAUDE_PLUGIN_ROOT} expanded."""
    hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    return command.replace("${CLAUDE_PLUGIN_ROOT}", str(REPO_ROOT))


def _run_session_start(claude_root: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
    env["CHINAMAXM_PYTHON"] = sys.executable
    env["CHINAMAXM_CLAUDE_HOME"] = str(claude_root)
    return subprocess.run(
        _session_start_command(),
        shell=True,
        input='{"hook_event_name":"SessionStart"}',
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _write_settings(root: Path, env_block: dict | str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    body = env_block if isinstance(env_block, str) else json.dumps({"env": env_block})
    (root / "settings.json").write_text(body, encoding="utf-8")


def _dead_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_session_start_dead_port_warns(tmp_path):
    root = tmp_path / "a"
    _write_settings(root, {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{_dead_port()}"})
    result = _run_session_start(root)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert "/chinamaxM:doctor" in payload["systemMessage"]


def test_session_start_flip_absent_silent(tmp_path):
    root = tmp_path / "b"
    _write_settings(root, {})
    result = _run_session_start(root)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_session_start_port_alive_silent(tmp_path):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        root = tmp_path / "c"
        _write_settings(root, {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"})
        result = _run_session_start(root)
        assert result.returncode == 0
        assert result.stdout.strip() == ""
    finally:
        listener.close()


def test_session_start_malformed_settings_silent(tmp_path):
    root = tmp_path / "d"
    _write_settings(root, "{ not valid json")
    result = _run_session_start(root)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_session_start_non_loopback_silent(tmp_path):
    root = tmp_path / "e"
    _write_settings(root, {"ANTHROPIC_BASE_URL": "http://10.1.2.3:8402"})
    result = _run_session_start(root)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# ------------------------------------------- test 7 (log path) & test 8 (skips)


def test_doctor_surfaces_log_path(install):
    findings, _ = run(install)
    f = one(findings, "log-path")
    expected = str(install.claude / "chinamaxM" / "requests.jsonl")
    assert expected in f.detail


def test_registry_failure_skips_dependents(install):
    install.overlay.write_text("{ not json", encoding="utf-8")
    findings, code = run(install)
    assert code == 1
    assert one(findings, "registry").status == "fail"
    for fid in ("service", "port", "env-flip-target", "keys", "drift"):
        f = one(findings, fid)
        assert f.status == "skipped" and f.detail == "registry unreadable", (fid, f)
    # The PRESENCE sub-finding is Registry-independent and still reports.
    assert one(findings, "env-flip-present").status == "ok"


# --------------------------------------------------- test 9 (Codex surface twins)


def test_codex_surface_twins_exist_and_match_cli():
    doctor_skill = (REPO_ROOT / "skills" / "chinamaxM-doctor" / "SKILL.md").read_text(encoding="utf-8")
    profiles_skill = (REPO_ROOT / "skills" / "chinamaxM-profiles" / "SKILL.md").read_text(encoding="utf-8")
    doctor_cmd = (REPO_ROOT / "commands" / "doctor.md").read_text(encoding="utf-8")
    profiles_cmd = (REPO_ROOT / "commands" / "profiles.md").read_text(encoding="utf-8")

    # Same pinned CLI invocations as the Claude command files.
    assert DOCTOR_CLI in doctor_cmd and DOCTOR_CLI in doctor_skill
    assert PROFILES_CLI in profiles_cmd and PROFILES_CLI in profiles_skill

    # Codex twins carry the headless stdin-close guidance.
    assert "< /dev/null" in doctor_skill
    assert "< /dev/null" in profiles_skill

    # No key value in any surface (they are static; assert the canary is absent).
    for text in (doctor_skill, profiles_skill, doctor_cmd, profiles_cmd):
        assert CANARY not in text
        assert "sk-" not in text


# ----------------------------------------------- normalizer unit coverage


def test_normalize_flip_url_shapes():
    ok = normalize_flip_url("http://127.0.0.1:8402")
    assert ok.scheme == "http" and ok.host == "127.0.0.1" and ok.port == 8402 and ok.is_loopback
    assert normalize_flip_url("http://localhost:8402").is_loopback
    assert normalize_flip_url("http://[::1]:8402").is_loopback
    assert not normalize_flip_url("http://10.0.0.1:8402").is_loopback
    assert normalize_flip_url("http://127.0.0.1:notaport").port is None
