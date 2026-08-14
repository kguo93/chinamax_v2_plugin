"""Hermetic tests for the consent-gated setup engine (hosts-05).

Every test drives :class:`chinamaxM.setup.SetupEngine` against temp Host roots with fake
runners (conda, service, HTTP ingress, diagnose) — no real conda, systemd, or network. The
no-mutation tests assert on the runner's ``mutating``-label guard, not on subprocess counts.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from chinamaxM import doctor, settings_json
from chinamaxM.ops.supervision import SupervisionStatus
from chinamaxM.setup import (
    _CONDA_INIT_TIMEOUT,
    _MINICONDA_DL_TIMEOUT,
    _MINICONDA_INSTALL_TIMEOUT,
    ProbeResponse,
    SetupEngine,
    SetupError,
    _run_generation,
    _Runner,
    _SubprocessConda,
    render_plan,
    render_report,
)

SEED_PROFILES = ("deepseek", "mimo", "glm", "minimax", "kimi", "qwen")
FLIP_URL = "http://127.0.0.1:8402"


# --------------------------------------------------------------------------- fakes


class FakeConda:
    """A fake conda seam whose state and failures are scripted per test."""

    def __init__(self, *, available=True, exists=False, python=None, pip_fails=False):
        self._available = available
        self._exists = exists
        self._python = python
        self._pip_fails = pip_fails
        self.created = 0
        self.pip_calls: list[str] = []

    def available(self):
        return self._available

    def env_exists(self):
        return self._exists

    def env_python_version(self):
        return self._python

    def env_python_path(self):
        return sys.executable  # a real, existing file so SupervisionConfig validates

    def create(self):
        self.created += 1
        self._exists = True
        self._python = "3.12"

    def pip_install(self, plugin_root):
        if self._pip_fails:
            raise RuntimeError("pip install boom")
        self.pip_calls.append(str(plugin_root))


class FakeArtifact:
    def __init__(self, path, content):
        self.path = path
        self.content = content


class FakeService:
    """A spying service seam that records install/update/teardown calls."""

    def __init__(self, unit_path, *, content=b"unit-content", status=None):
        self.unit_path = unit_path
        self.content = content
        self.status_value = status or SupervisionStatus(False, False, False, False)
        self.installs = 0
        self.updates = 0
        self.teardowns = 0

    def render(self, cfg):
        return FakeArtifact(self.unit_path, self.content)

    def install(self, cfg):
        self.installs += 1

    def update(self, cfg):
        self.updates += 1

    def teardown(self, cfg):
        self.teardowns += 1

    def status(self, cfg):
        return self.status_value


class FakeHttp:
    """A fake ingress-shaped HTTP sender recording every probe request."""

    def __init__(self):
        self.requests: list[dict] = []
        self._scripts: list[tuple[str, int, object]] = []

    def script(self, url_substring, status, payload):
        self._scripts.append((url_substring, status, payload))

    def __call__(self, url, body, *, connect_timeout, total_timeout):
        self.requests.append({"url": url, "body": body})
        for substring, status, payload in self._scripts:
            if substring in url:
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                return ProbeResponse(status, data)
        return ProbeResponse(200, json.dumps({"usage": {"input_tokens": 1, "output_tokens": 1}}).encode())


def _ok_run(argv, *, timeout, env=None):
    """A fake subprocess runner (no real conda/codex ever runs, so no tokens are spent)."""
    return subprocess.CompletedProcess(argv, 0, "", "")


class RecordingRun:
    """A fake subprocess runner recording every argv + timeout; failures are scriptable.

    ``fail_on`` is an optional predicate over the argv list; when it returns True the call
    reports a non-zero returncode (so stop-on-first-failure paths can be exercised).
    """

    def __init__(self, fail_on=None):
        self.calls: list[dict] = []
        self._fail_on = fail_on

    def __call__(self, argv, *, timeout, env=None):
        self.calls.append({"argv": list(argv), "timeout": timeout, "env": env})
        rc = 1 if (self._fail_on is not None and self._fail_on(list(argv))) else 0
        return subprocess.CompletedProcess(argv, rc, "", "")


def _healthy_findings():
    return [doctor.Finding("service", "fail", "ok", "ok"), doctor.Finding("port", "fail", "ok", "ok")]


def _clock(step=5.0):
    counter = [0.0]

    def now():
        counter[0] += step
        return counter[0]

    return now


def make_engine(tmp_path, **overrides):
    """Build a SetupEngine over temp roots with fully faked seams."""
    claude = tmp_path / "claude"
    claude.mkdir(parents=True, exist_ok=True)
    codex = tmp_path / "codex"
    if overrides.pop("codex", True):
        codex.mkdir(parents=True, exist_ok=True)

    conda = overrides.pop("conda", None) or FakeConda()
    service = overrides.pop("service", None) or FakeService(tmp_path / "chinamaxM.service")
    http = overrides.pop("http", None) or FakeHttp()
    diagnose = overrides.pop("diagnose", None) or (lambda: _healthy_findings())
    linger_on = overrides.pop("linger_on", False)
    linger_calls: list[int] = []
    port_live = overrides.pop("port_live", None) or (lambda port: True)
    platform = overrides.pop("platform", "linux")

    engine = SetupEngine(
        claude_root=str(claude),
        codex_root=str(codex),
        plugin_root=str(tmp_path / "plugin"),
        run=overrides.pop("run", None) or _ok_run,
        conda=conda,
        diagnose=diagnose,
        generate_fn=lambda reg, roots, inc: _run_generation(reg, roots, include_codex=inc),
        service_status=service.status,
        service_install=service.install,
        service_update=service.update,
        service_teardown=service.teardown,
        service_render=service.render,
        port_live=port_live,
        enable_linger=lambda: linger_calls.append(1),
        linger_enabled=lambda: linger_on,
        http=http,
        sleep=lambda _seconds: None,
        now=_clock(),
        platform=platform,
        **overrides,
    )
    ctx = {"claude": claude, "codex": codex, "conda": conda, "service": service,
           "http": http, "linger_calls": linger_calls}
    return engine, ctx


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


# --------------------------------------------------------------------------- tests


def test_no_mutation_before_approval(tmp_path):
    """AC-1: drive to the gate, reject — the tree is byte-identical and no mutating op ran."""
    engine, ctx = make_engine(tmp_path)
    before = tree_hash(tmp_path)

    engine.build_plan()
    # The plan phase ran diagnostic ops only.
    assert any(kind == "diagnostic" for kind, _ in engine._runner.record)
    assert not any(kind == "mutating" for kind, _ in engine._runner.record)

    # Reject at the gate (a wrong digest) — no mutation, no rollback needed.
    report = engine.apply("not-the-real-digest")
    assert report.rejected
    assert report.exit_code == 1
    assert not any(kind == "mutating" for kind, _ in engine._runner.record)
    assert tree_hash(tmp_path) == before
    assert ctx["conda"].created == 0 and ctx["service"].installs == 0

    # The label guard is enforced IN the runner, not by caller discipline.
    runner = _Runner()
    ran = []
    with pytest.raises(SetupError):
        runner.run("mutating", "write-something", lambda: ran.append(1))
    assert ran == [] and runner.record == []


def test_apply_steps_against_temp_roots(tmp_path):
    """AC-2: each apply step lands against temp roots; env block + generation + service."""
    settings_path = tmp_path / "claude" / "settings.json"
    (tmp_path / "claude").mkdir()
    settings_path.write_text(
        '{\n    "model": "opus",\n    "env": {\n        "OTHER_VAR": "keep"\n    }\n}\n', encoding="utf-8"
    )

    engine, ctx = make_engine(tmp_path)
    plan = engine.build_plan()
    report = engine.apply(plan.digest)
    assert report.exit_code == 0, render_report(report)

    # Env flip written, unrelated keys + order + indent preserved.
    text = settings_path.read_text(encoding="utf-8")
    doc = json.loads(text)
    assert doc["env"]["ANTHROPIC_BASE_URL"] == FLIP_URL
    assert doc["env"]["OTHER_VAR"] == "keep" and doc["model"] == "opus"
    assert '    "model"' in text  # 4-space indent kept

    # conda create + pip recorded (create because the fixture env is absent).
    assert ctx["conda"].created == 1 and ctx["conda"].pip_calls

    # Both Key files scaffolded (Codex present).
    assert (tmp_path / "claude" / "model-keys.env").exists()
    assert (tmp_path / "codex" / "model-keys.env").exists()

    # Generation produced the full artifact set (Claude agents + Codex roles + config).
    for name in SEED_PROFILES:
        assert (tmp_path / "claude" / "agents" / f"{name}.md").exists()
        assert (tmp_path / "codex" / "agents" / f"{name}.toml").exists()
    assert (tmp_path / "codex" / "config.toml").exists()

    # Service install() called once; enable-linger recorded on Linux.
    assert ctx["service"].installs == 1 and ctx["service"].updates == 0
    assert ctx["linger_calls"] == [1]
    assert report.restart_instruction and "Restart" in render_report(report)


def test_apply_skips_when_env_exists_and_updates_service(tmp_path):
    """AC-2 variants: create skipped for an existing 3.12 env; update() for a changed unit."""
    (tmp_path / "claude").mkdir()
    unit = tmp_path / "chinamaxM.service"
    unit.write_bytes(b"stale-different-bytes")  # on disk differs from the rendered content
    conda = FakeConda(exists=True, python="3.12")
    service = FakeService(unit, content=b"unit-content")
    engine, ctx = make_engine(tmp_path, conda=conda, service=service, linger_on=True)

    plan = engine.build_plan()
    report = engine.apply(plan.digest)
    assert report.exit_code == 0, render_report(report)
    assert ctx["conda"].created == 0  # existing 3.12 env → create skipped
    assert ctx["service"].updates == 1 and ctx["service"].installs == 0  # changed unit → update
    assert ctx["linger_calls"] == []  # linger already on → skipped


def test_probe_optin_separate(tmp_path):
    """AC-3: declining probes runs zero probe requests and still completes setup."""
    engine, ctx = make_engine(tmp_path)
    plan = engine.build_plan()
    report = engine.apply(plan.digest, probes=False)
    assert report.exit_code == 0
    assert ctx["http"].requests == []
    assert report.probes_skipped == "not requested"


def test_probes_minimal_and_reported(tmp_path):
    """AC-4: exactly one request per Profile per ingress, minimal, per-Profile reported."""
    http = FakeHttp()
    http.script("/openai/deepseek/", 500, {"error": {"type": "server_error", "message": "boom"}})
    engine, ctx = make_engine(tmp_path, http=http)
    plan = engine.build_plan()
    report = engine.apply(plan.digest, probes=True)

    anthropic = [r for r in http.requests if r["url"].endswith("/v1/messages")]
    responses = [r for r in http.requests if "/responses" in r["url"]]
    assert len(anthropic) == len(SEED_PROFILES)
    assert len(responses) == len(SEED_PROFILES)

    for req in anthropic:
        assert req["body"]["max_tokens"] == 1
        prefix, _slash, rest = req["body"]["model"].partition("/")
        assert prefix in SEED_PROFILES and rest  # model = <profile>/<default_model>
    for req in responses:
        assert req["body"]["max_output_tokens"] == 1
        assert "/" not in req["body"]["model"]  # Responses carries the BARE default model

    verdicts = {(p.profile, p.ingress): p.ok for p in report.probe_results}
    assert verdicts[("deepseek", "responses")] is False  # the scripted failure
    assert verdicts[("mimo", "anthropic")] is True  # a failing Profile never aborts the rest
    assert any(p.usage for p in report.probe_results if p.ok)


def test_claude_only_generation_without_codex_home(tmp_path):
    """AC-4 + Claude-only branch: no Responses probes, six agents written, ~/.codex never made."""
    engine, ctx = make_engine(tmp_path, codex=False)
    plan = engine.build_plan()
    report = engine.apply(plan.digest, probes=True)
    assert report.exit_code == 0, render_report(report)

    # Zero Responses-ingress probes; one Anthropic probe per Profile.
    assert all("/responses" not in r["url"] for r in ctx["http"].requests)
    assert len([r for r in ctx["http"].requests if r["url"].endswith("/v1/messages")]) == len(SEED_PROFILES)

    # The Claude-only branch WROTE the six agent files and fabricated NO Codex artifacts.
    for name in SEED_PROFILES:
        assert (tmp_path / "claude" / "agents" / f"{name}.md").exists()
    assert not (tmp_path / "codex").exists()  # the Codex home is never fabricated (ADR 0006)


def test_teardown_exact(tmp_path):
    """AC-5: teardown removes the env key + service; keys/agents untouched; best-effort."""
    claude = tmp_path / "claude"
    claude.mkdir()
    settings_path = claude / "settings.json"
    settings_path.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": FLIP_URL, "KEEP": "1"}}, indent=2), encoding="utf-8"
    )
    key_file = claude / "model-keys.env"
    key_file.write_text("# comment\n" + "DEEPSEEK_API_KEY=" + "canary\n", encoding="utf-8")
    agents = claude / "agents"
    agents.mkdir()
    agent = agents / "deepseek.md"
    agent.write_text("chinamaxM-generated\nbody\n", encoding="utf-8")
    key_bytes, agent_bytes = key_file.read_bytes(), agent.read_bytes()

    engine, ctx = make_engine(tmp_path)
    plan = engine.build_teardown_plan()
    report = engine.teardown(plan.digest)
    assert report.exit_code == 0

    doc = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "ANTHROPIC_BASE_URL" not in doc["env"] and doc["env"]["KEEP"] == "1"
    assert ctx["service"].teardowns == 1
    assert key_file.read_bytes() == key_bytes and agent.read_bytes() == agent_bytes

    # Absent settings file ⇒ no-op, file not created.
    engine2, ctx2 = make_engine(tmp_path / "fresh")
    plan2 = engine2.build_teardown_plan()
    engine2.teardown(plan2.digest)
    assert not (tmp_path / "fresh" / "claude" / "settings.json").exists()
    assert ctx2["service"].teardowns == 1

    # Unparseable settings ⇒ env step fails closed while service uninstall is still attempted.
    bad_root = tmp_path / "bad"
    (bad_root / "claude").mkdir(parents=True)
    (bad_root / "claude" / "settings.json").write_text("{not json", encoding="utf-8")
    engine3, ctx3 = make_engine(bad_root)
    plan3 = engine3.build_teardown_plan()
    report3 = engine3.teardown(plan3.digest)
    outcomes = {r.id: r.status for r in report3.step_results}
    assert outcomes["env-flip-remove"] == "failed"
    assert outcomes["service-teardown"] == "ok" and ctx3["service"].teardowns == 1


def test_rediagnose_in_report(tmp_path):
    """AC-6: the report embeds the doctor findings run after apply."""
    marker = "REDIAGNOSE-MARKER-9137"
    findings = [doctor.Finding("registry", "fail", "ok", marker)]
    engine, ctx = make_engine(tmp_path, diagnose=lambda: list(findings))
    plan = engine.build_plan()
    report = engine.apply(plan.digest)
    assert report.after_findings and report.after_findings[0].detail == marker
    assert marker in render_report(report)


def test_no_key_values_printed(tmp_path):
    """AC-7: a canary Key-file value never appears in the plan, report, or CLI output."""
    canary = "CANARY-SECRET-do-not-print-8842"
    # Build the Key-file line by concatenation so this test's own source carries no
    # literal API-key assignment (the ops-02 secret scanner would otherwise flag one).
    key_line = "DEEPSEEK_API_KEY=" + canary + "\n"
    claude = tmp_path / "claude"
    claude.mkdir()
    (claude / "model-keys.env").write_text(key_line, encoding="utf-8")
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "model-keys.env").write_text(key_line, encoding="utf-8")

    http = FakeHttp()
    http.script("/v1/messages", 401, {"error": {"type": "authentication_error", "message": "DEEPSEEK_API_KEY missing"}})
    http.script("/responses", 500, b"not-json-bytes")
    engine, ctx = make_engine(tmp_path, http=http)
    plan = engine.build_plan()
    report = engine.apply(plan.digest, probes=True)

    for text in (render_plan(plan), render_report(report)):
        assert canary not in text
    assert not any(canary in json.dumps(p.__dict__) for p in report.probe_results)


def test_apply_failure_aborts_rest(tmp_path):
    """Failure policy: a failing step aborts the rest; nothing rolls back; re-diagnose runs."""
    (tmp_path / "claude").mkdir()
    conda = FakeConda(pip_fails=True)
    engine, ctx = make_engine(tmp_path, conda=conda)
    plan = engine.build_plan()
    report = engine.apply(plan.digest)

    outcomes = {r.id: r.status for r in report.step_results}
    assert outcomes["conda-env"] == "ok" and outcomes["pip-install"] == "failed"
    assert outcomes["generate"] == "aborted" and outcomes["env-flip"] == "aborted"
    # record-python-path sits AFTER pip-install, so a pip failure aborts it: no python-path
    # is recorded for a chinamaxM-less env.
    assert outcomes["record-python-path"] == "aborted"
    assert not (tmp_path / "claude" / "chinamaxM" / "python-path").exists()
    assert ctx["conda"].created == 1  # the successful create is NOT rolled back
    assert not (tmp_path / "claude" / "settings.json").exists()  # env flip never written
    assert report.after_findings  # re-diagnose still ran
    assert report.exit_code == 1


def test_gate_and_ownership_policies(tmp_path):
    """AC-1/AC-5 hardening: digest gate, precondition drift, non-3.12 env, foreign flip."""
    # Wrong digest ⇒ zero mutating ops.
    engine, ctx = make_engine(tmp_path)
    engine.build_plan()
    report = engine.apply("bogus-digest")
    assert report.rejected and ctx["conda"].created == 0

    # Precondition drift between plan and apply ⇒ abort.
    root_b = tmp_path / "b"
    (root_b / "claude").mkdir(parents=True)
    engine_b, ctx_b = make_engine(root_b)
    plan_b = engine_b.build_plan()
    (root_b / "claude" / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://example.com"}}), encoding="utf-8"
    )
    report_b = engine_b.apply(plan_b.digest)
    assert report_b.rejected and ctx_b["conda"].created == 0

    # Existing env on the wrong Python ⇒ gate FAILs, env not recreated.
    root_c = tmp_path / "c"
    (root_c / "claude").mkdir(parents=True)
    conda_c = FakeConda(exists=True, python="3.11")
    engine_c, ctx_c = make_engine(root_c, conda=conda_c)
    plan_c = engine_c.build_plan()
    report_c = engine_c.apply(plan_c.digest)
    assert {r.id: r.status for r in report_c.step_results}["conda-env"] == "failed"
    assert ctx_c["conda"].created == 0 and report_c.exit_code == 1

    # Foreign ANTHROPIC_BASE_URL ⇒ env-flip step fails without overwriting; teardown leaves it.
    root_d = tmp_path / "d"
    (root_d / "claude").mkdir(parents=True)
    foreign = json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://example.com:9999"}}, indent=2)
    (root_d / "claude" / "settings.json").write_text(foreign, encoding="utf-8")
    engine_d, ctx_d = make_engine(root_d)
    plan_d = engine_d.build_plan()
    report_d = engine_d.apply(plan_d.digest)
    assert {r.id: r.status for r in report_d.step_results}["env-flip"] == "failed"
    assert json.loads((root_d / "claude" / "settings.json").read_text())["env"]["ANTHROPIC_BASE_URL"] == "http://example.com:9999"

    plan_td = engine_d.build_teardown_plan()
    report_td = engine_d.teardown(plan_td.digest)
    assert {r.id: r.status for r in report_td.step_results}["env-flip-remove"] == "skipped"
    assert json.loads((root_d / "claude" / "settings.json").read_text())["env"]["ANTHROPIC_BASE_URL"] == "http://example.com:9999"


def test_plan_only_cli_is_read_only(tmp_path, monkeypatch, capsys):
    """The frozen --plan-only flag renders a digest and exits 0 without mutating."""
    (tmp_path / "claude").mkdir()
    before = tree_hash(tmp_path)

    from chinamaxM import setup as setup_mod

    engine, _ctx = make_engine(tmp_path)
    monkeypatch.setattr(setup_mod, "SetupEngine", lambda: engine)
    code = setup_mod.main(["--plan-only"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Plan digest:" in out
    assert tree_hash(tmp_path) == before


_BOOTSTRAP_SUBPROCESS = r'''
import sys, tempfile

BLOCK = {"tomlkit", "yaml", "aiohttp", "litellm"}


class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCK:
            raise ImportError("BOOTSTRAP-BLOCKED: " + name)
        return None


sys.meta_path.insert(0, _Blocker())

# The whole point: importing setup on a bare ambient interpreter must not pull env deps.
import chinamaxM.setup as setup


class _Status:
    installed = enabled = running = False


class _Conda:
    def available(self): return True
    def env_exists(self): return True
    def env_python_version(self): return "3.12"
    def env_python_path(self): return sys.executable
    def create(self): pass
    def pip_install(self, root): pass


class _Art:
    def __init__(self, path):
        self.path = path
        self.content = b"unit"


claude = tempfile.mkdtemp()
codex = tempfile.mkdtemp()
engine = setup.SetupEngine(
    claude_root=claude, codex_root=codex, plugin_root=claude,
    run=lambda *a, **k: None, conda=_Conda(), diagnose=lambda: [],
    generate_fn=lambda reg, roots, inc: {}, service_status=lambda cfg: _Status(),
    service_install=lambda cfg: None, service_update=lambda cfg: None,
    service_teardown=lambda cfg: None,
    service_render=lambda cfg: _Art(claude + "/unit.service"),
    port_live=lambda p: True, enable_linger=lambda: None, linger_enabled=lambda: False,
    http=lambda *a, **k: None, sleep=lambda s: None, now=lambda: 0.0, platform="linux",
)
plan = engine.build_plan()          # the --plan-only diagnose+plan path
text = setup.render_plan(plan)      # the --plan-only render path
assert "Plan digest:" in text
leaked = [m for m in BLOCK if m in sys.modules]
assert not leaked, "env-only deps leaked into the --plan-only path: " + repr(leaked)
print("BOOTSTRAP-OK")
'''


def test_bootstrap_import_on_bare_interpreter():
    """Bootstrap: `import chinamaxM.setup` + the --plan-only path run with env deps blocked.

    A subprocess installs a meta_path blocker for tomlkit/PyYAML/aiohttp/litellm (the
    env-only deps), then imports the module and runs build_plan + render_plan with fake
    seams — proving the module load and the pre-env plan/render path never touch them.
    """
    result = subprocess.run(
        [sys.executable, "-c", _BOOTSTRAP_SUBPROCESS],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BOOTSTRAP-OK" in result.stdout


def test_settings_json_fail_closed(tmp_path):
    """settings_json fails closed on a malformed file and creates a missing env key."""
    path = tmp_path / "settings.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")  # non-object root
    with pytest.raises(settings_json.SettingsError):
        settings_json.read_flip(path)

    path.write_text(json.dumps({"env": []}), encoding="utf-8")  # non-dict env
    with pytest.raises(settings_json.SettingsError):
        settings_json.write_flip(path, FLIP_URL)

    # A missing env key is CREATED, not an error; a new file is mode 0600.
    fresh = tmp_path / "new" / "settings.json"
    settings_json.write_flip(fresh, FLIP_URL)
    assert json.loads(fresh.read_text())["env"]["ANTHROPIC_BASE_URL"] == FLIP_URL
    assert (fresh.stat().st_mode & 0o777) == 0o600


# --------------------------------------------------------------- Fix 2: Windows WinSW


def _real_exe(path: Path) -> Path:
    """Create and return a stand-in resolved WinSW exe (an existing file SupervisionConfig accepts)."""
    path.write_bytes(b"MZ-resolved-winsw")
    return path


def test_windows_winsw_plan_render_and_apply(tmp_path):
    """Fix 2 (Windows): plan shows the WinSW source + binds it in the digest; apply installs it."""
    (tmp_path / "claude").mkdir()
    resolved = _real_exe(tmp_path / "winsw-resolved.exe")
    ensure_calls: list = []

    def fake_ensure(service_dir, override_path=None):
        ensure_calls.append((str(service_dir), override_path))
        return resolved

    engine, ctx = make_engine(tmp_path, platform="windows", ensure_winsw=fake_ensure)

    # No override and no installed exe ⇒ the plan's WinSW source is the pinned download.
    plan = engine.build_plan()
    text = render_plan(plan)
    assert "download pinned WinSW v2.12.0 (sha256-verified)" in text
    service_step = next(s for s in plan.steps if s.id == "service")
    assert service_step.descriptor["winsw"]["kind"] == "download"  # bound in the digest

    # Apply resolves WinSW and installs the service; no linger step on Windows.
    report = engine.apply(plan.digest)
    assert report.exit_code == 0, render_report(report)
    assert ctx["service"].installs == 1 and ctx["service"].updates == 0
    assert ensure_calls and ensure_calls[0][1] is None  # no override threaded
    assert all(r.id != "linger" for r in report.step_results)
    # No password supplied ⇒ the service step notes the logon-as-service caveat.
    service_result = next(r for r in report.step_results if r.id == "service")
    assert "log on as a service" in service_result.detail


def test_windows_winsw_override_threaded(tmp_path):
    """Fix 2 (Windows): --winsw-exe is shown in the plan and threaded to ensure_winsw_exe."""
    (tmp_path / "claude").mkdir()
    resolved = _real_exe(tmp_path / "winsw-resolved.exe")
    ensure_calls: list = []

    def fake_ensure(service_dir, override_path=None):
        ensure_calls.append(override_path)
        return resolved

    override = str(tmp_path / "supplied" / "WinSW.exe")
    # A supplied service password suppresses the caveat AND must never be rendered.
    password = "PW-CANARY-do-not-log-7731"
    engine, ctx = make_engine(
        tmp_path, platform="windows", ensure_winsw=fake_ensure,
        winsw_exe=override, service_password=password,
    )

    plan = engine.build_plan()
    text = render_plan(plan)
    assert f"supplied: {override}" in text
    service_step = next(s for s in plan.steps if s.id == "service")
    assert service_step.descriptor["winsw"] == {
        "kind": "supplied", "path": override, "render": f"supplied: {override}"
    }

    report = engine.apply(plan.digest)
    assert report.exit_code == 0, render_report(report)
    assert ensure_calls == [override]  # the override threaded end-to-end
    assert ctx["service"].installs == 1

    service_result = next(r for r in report.step_results if r.id == "service")
    assert "log on as a service" not in service_result.detail  # password supplied ⇒ no caveat
    for rendered in (render_plan(plan), render_report(report)):
        assert password not in rendered  # the password is NEVER logged


def test_windows_winsw_acquisition_failure_aborts(tmp_path):
    """Fix 2 (Windows): a WinSW acquisition failure fails the service step and aborts the rest."""
    from chinamaxM.ops.supervision import SupervisionError

    (tmp_path / "claude").mkdir()

    def failing_ensure(service_dir, override_path=None):
        raise SupervisionError(message="WinSW download checksum mismatch")

    down = [
        doctor.Finding("service", "fail", "fail", "service down"),
        doctor.Finding("port", "fail", "fail", "port dead"),
    ]
    engine, ctx = make_engine(
        tmp_path, platform="windows", ensure_winsw=failing_ensure, diagnose=lambda: list(down)
    )
    plan = engine.build_plan()
    report = engine.apply(plan.digest, probes=True)

    outcomes = {r.id: r.status for r in report.step_results}
    assert outcomes["service"] == "failed"
    assert outcomes["env-flip"] == "aborted"  # per the pinned order, the flip never runs
    assert ctx["service"].installs == 0  # never installed
    assert not (tmp_path / "claude" / "settings.json").exists()  # env flip never written
    assert ctx["http"].requests == []  # probes not reached (skipped by the FAIL re-diagnose)
    assert report.probes_skipped
    assert report.after_findings and report.exit_code == 1  # re-diagnose + report still run


# ----------------------------------------------------- Fix 5: hook shim python-path record


def test_records_and_removes_python_path(tmp_path):
    """Fix 5: apply records <root>/chinamaxM/python-path; plan-only doesn't; teardown removes it."""
    (tmp_path / "claude").mkdir()
    engine, ctx = make_engine(tmp_path)
    py_file = tmp_path / "claude" / "chinamaxM" / "python-path"

    # Plan renders the record step but writes nothing (it is a mutating op — no pre-approval run).
    plan = engine.build_plan()
    ids = [s.id for s in plan.steps]
    # Ordered AFTER pip-install (so a plugin is present) and before the key scaffold.
    assert ids.index("pip-install") < ids.index("record-python-path") < ids.index("scaffold-claude-key")
    assert "Record the chinamaxM env Python" in render_plan(plan)
    assert not py_file.exists()

    # Apply records the resolved env python (FakeConda returns sys.executable) as one line.
    report = engine.apply(plan.digest)
    assert report.exit_code == 0, render_report(report)
    assert py_file.read_text(encoding="utf-8") == sys.executable + "\n"

    # Re-apply converges (byte-identical skip, still exit 0).
    report2 = engine.apply(engine.build_plan().digest)
    assert report2.exit_code == 0
    assert py_file.read_text(encoding="utf-8") == sys.executable + "\n"

    # Teardown removes the file; a second teardown (now absent) is a no-op, still exit 0.
    report_td = engine.teardown(engine.build_teardown_plan().digest)
    assert report_td.exit_code == 0
    assert not py_file.exists()
    assert {r.id: r.status for r in report_td.step_results}["python-path-remove"] == "ok"

    report_td2 = engine.teardown(engine.build_teardown_plan().digest)
    assert report_td2.exit_code == 0
    assert {r.id: r.status for r in report_td2.step_results}["python-path-remove"] == "ok"


# ------------------------------------------------------- Conda bootstrap (Miniconda auto-install)


def _bootstrap_step(plan):
    return next(s for s in plan.steps if s.id == "conda-bootstrap")


@pytest.mark.parametrize(
    "platform,machine,os_tag,arch,init_shells",
    [
        ("linux", "x86_64", "Linux", "x86_64", ["bash"]),
        ("linux", "aarch64", "Linux", "aarch64", ["bash"]),
        ("darwin", "arm64", "MacOSX", "arm64", ["bash", "zsh"]),
    ],
)
def test_conda_bootstrap_posix_plan_emission(tmp_path, platform, machine, os_tag, arch, init_shells):
    """conda absent ⇒ a BOOTSTRAP step whose descriptor is the exact per-OS installer plan."""
    engine, _ctx = make_engine(
        tmp_path, conda=FakeConda(available=False),
        platform=platform, machine=machine, home=tmp_path,
    )
    plan = engine.build_plan()
    step = _bootstrap_step(plan)
    prefix = tmp_path / "miniconda3"
    installer = tmp_path / ".chinamaxM-miniconda.sh"
    url = f"https://repo.anaconda.com/miniconda/Miniconda3-latest-{os_tag}-{arch}.sh"
    assert step.action == "BOOTSTRAP"
    assert step.descriptor["url"] == url
    assert step.descriptor["prefix"] == str(prefix)
    assert step.descriptor["commands"] == [
        ["curl", "-fsSL", url, "-o", str(installer)],
        ["bash", str(installer), "-b", "-u", "-p", str(prefix)],
        [str(prefix / "bin" / "conda"), "init", *init_shells],
    ]
    # The env cannot exist yet ⇒ a CREATE step follows the bootstrap.
    ids = [s.id for s in plan.steps]
    assert ids.index("conda-bootstrap") < ids.index("conda-env")
    assert next(s for s in plan.steps if s.id == "conda-env").action == "CREATE"


def test_conda_bootstrap_windows_plan_emission(tmp_path):
    """Windows (machine AMD64): the exact curl.exe/JustMe-silent/conda-init argv, x86_64 asset."""
    engine, _ctx = make_engine(
        tmp_path, conda=FakeConda(available=False),
        platform="windows", machine="AMD64", home=tmp_path,
    )
    plan = engine.build_plan()
    step = _bootstrap_step(plan)
    prefix = tmp_path / "miniconda3"
    installer = tmp_path / "chinamaxM-miniconda.exe"
    url = "https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe"
    assert step.action == "BOOTSTRAP"
    assert step.descriptor["url"] == url
    assert step.descriptor["commands"] == [
        ["curl.exe", "-fsSL", url, "-o", str(installer)],
        ["cmd", "/c", "start", "/wait", "", str(installer),
         "/InstallationType=JustMe", "/RegisterPython=0", "/AddToPath=0", "/S", f"/D={prefix}"],
        [str(prefix / "Scripts" / "conda.exe"), "init", "cmd.exe", "powershell", "bash"],
    ]
    assert next(s for s in plan.steps if s.id == "conda-env").action == "CREATE"


def test_conda_bootstrap_apply_runs_commands_in_order(tmp_path, monkeypatch):
    """Apply issues the exact download→install→init argv in order, each with its own timeout."""
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))  # in-process PATH prepend auto-restored
    run = RecordingRun()
    engine, ctx = make_engine(
        tmp_path, conda=FakeConda(available=False),
        platform="linux", machine="x86_64", home=tmp_path, run=run,
    )
    plan = engine.build_plan()
    report = engine.apply(plan.digest)
    assert report.exit_code == 0, render_report(report)

    prefix = tmp_path / "miniconda3"
    installer = tmp_path / ".chinamaxM-miniconda.sh"
    url = "https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh"
    assert [c["argv"] for c in run.calls[:3]] == [
        ["curl", "-fsSL", url, "-o", str(installer)],
        ["bash", str(installer), "-b", "-u", "-p", str(prefix)],
        [str(prefix / "bin" / "conda"), "init", "bash"],
    ]
    assert [c["timeout"] for c in run.calls[:3]] == [
        _MINICONDA_DL_TIMEOUT, _MINICONDA_INSTALL_TIMEOUT, _CONDA_INIT_TIMEOUT,
    ]
    # After the fresh install the env-create step runs next (FakeConda records the create).
    assert ctx["conda"].created == 1 and ctx["conda"].pip_calls
    assert {r.id: r.status for r in report.step_results}["conda-bootstrap"] == "ok"


def test_conda_bootstrap_apply_stops_on_first_failure(tmp_path, monkeypatch):
    """A non-zero installer return fails the bootstrap step and aborts the rest; init never runs."""
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    run = RecordingRun(fail_on=lambda argv: argv[:1] == ["bash"])  # the installer command fails
    engine, ctx = make_engine(
        tmp_path, conda=FakeConda(available=False),
        platform="linux", machine="x86_64", home=tmp_path, run=run,
    )
    plan = engine.build_plan()
    report = engine.apply(plan.digest)

    outcomes = {r.id: r.status for r in report.step_results}
    assert outcomes["conda-bootstrap"] == "failed"
    assert outcomes["conda-env"] == "aborted" and outcomes["env-flip"] == "aborted"
    assert ctx["conda"].created == 0  # never reached the env-create step
    # Only curl + the failing installer ran; conda init was never issued.
    assert [c["argv"][0] for c in run.calls] == ["curl", "bash"]
    assert not (tmp_path / "claude" / "settings.json").exists()
    assert report.exit_code == 1


def test_conda_bootstrap_unsupported_arch_gates(tmp_path):
    """POSIX arch off the map ⇒ advice-only gate, no BOOTSTRAP step, no leaked installer URL."""
    engine, ctx = make_engine(
        tmp_path, conda=FakeConda(available=False),
        platform="linux", machine="ppc64le", home=tmp_path,
    )
    plan = engine.build_plan()
    assert not any(s.action == "BOOTSTRAP" for s in plan.steps)
    gate = _bootstrap_step(plan)
    assert gate.gate_fail and gate.action == "GATE-FAIL"
    assert "unsupported CPU architecture" in gate.gate_detail and "ppc64le" in gate.gate_detail
    assert "Miniconda3-latest" not in gate.gate_detail
    assert "Miniconda3-latest" not in render_plan(plan)

    report = engine.apply(plan.digest)
    assert {r.id: r.status for r in report.step_results}["conda-bootstrap"] == "failed"
    assert ctx["conda"].created == 0 and report.exit_code == 1


def test_conda_bootstrap_skipped_when_conda_available(tmp_path):
    """conda already present ⇒ no bootstrap step; the normal create step is planned instead."""
    engine, _ctx = make_engine(tmp_path)  # default FakeConda(available=True, exists=False)
    plan = engine.build_plan()
    assert not any(s.id == "conda-bootstrap" for s in plan.steps)
    assert not any(s.action == "BOOTSTRAP" for s in plan.steps)
    assert any(s.id == "conda-env" and s.action == "CREATE" for s in plan.steps)


def test_conda_bootstrap_digest_binds_machine(tmp_path):
    """The plan digest changes when the machine changes the bootstrap URL (nothing else varies)."""
    home = tmp_path / "home"
    e1, _ = make_engine(tmp_path, conda=FakeConda(available=False), platform="linux", machine="x86_64", home=home)
    e2, _ = make_engine(tmp_path, conda=FakeConda(available=False), platform="linux", machine="aarch64", home=home)
    assert e1.build_plan().digest != e2.build_plan().digest


def test_conda_bin_precedence_and_which_fallback(tmp_path, monkeypatch):
    """_SubprocessConda resolves ~/miniconda3 first (POSIX bin / Windows Scripts), else shutil.which."""
    from chinamaxM import setup as setup_mod

    # POSIX: an executable ~/miniconda3/bin/conda resolves to its absolute path, not bare "conda".
    posix_home = tmp_path / "posix"
    bin_dir = posix_home / "miniconda3" / "bin"
    bin_dir.mkdir(parents=True)
    conda_stub = bin_dir / "conda"
    conda_stub.write_text("#!/bin/sh\n")
    conda_stub.chmod(0o755)
    rec = RecordingRun()
    seam = _SubprocessConda(rec, home=posix_home, platform="linux")
    assert seam._conda_bin() == str(conda_stub)
    assert seam.available() is True
    seam.create()
    assert rec.calls[-1]["argv"][0] == str(conda_stub)  # create() invokes the absolute launcher

    # Windows: Scripts\conda.exe wins by suffix (no exec bit needed on a POSIX test host).
    win_home = tmp_path / "win"
    scripts = win_home / "miniconda3" / "Scripts"
    scripts.mkdir(parents=True)
    win_conda = scripts / "conda.exe"
    win_conda.write_bytes(b"MZ")
    win_seam = _SubprocessConda(RecordingRun(), home=win_home, platform="windows")
    assert win_seam._conda_bin() == str(win_conda)

    # Fallback: no ~/miniconda3 ⇒ shutil.which resolves conda; None ⇒ the bare "conda" argv[0].
    empty_home = tmp_path / "empty"
    empty_home.mkdir()
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: "/opt/conda/bin/conda")
    fallback = _SubprocessConda(RecordingRun(), home=empty_home, platform="linux")
    assert fallback._conda_bin() == "/opt/conda/bin/conda"
    assert fallback.available() is True

    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
    absent = _SubprocessConda(RecordingRun(), home=empty_home, platform="linux")
    assert absent._conda_bin() is None
    assert absent.available() is False
    bare_rec = RecordingRun()
    _SubprocessConda(bare_rec, home=empty_home, platform="linux").create()
    assert bare_rec.calls[-1]["argv"][0] == "conda"  # nothing resolvable ⇒ bare "conda"
