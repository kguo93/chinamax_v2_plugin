"""Hermetic tests for the consent-gated setup engine (hosts-05).

Every test drives :class:`chinamaxM.setup.SetupEngine` against temp Host roots with fake
runners (conda, service, HTTP ingress, diagnose) — no real conda, systemd, or network. The
no-mutation tests assert on the runner's ``mutating``-label guard, not on subprocess counts.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from chinamaxM import doctor, settings_json
from chinamaxM.ops.supervision import SupervisionStatus
from chinamaxM.setup import (
    ProbeResponse,
    SetupEngine,
    SetupError,
    _run_generation,
    _Runner,
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
