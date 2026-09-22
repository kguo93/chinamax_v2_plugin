"""Supervision-machinery tests: per-OS rendering, converge ops, and status primitives.

macOS/Windows are exercised through a scripted, recording runner with exact-command
assertions (ADR 0015 precedent); Linux gets the same PLUS the one-time live systemd
``--user`` verification performed at implementation time (issue AC-5, in the commit body).
All ops tests inject a :class:`ScriptedRunner`, so the suite never touches a real service
manager; ``port_live`` uses a real loopback socket, allowed by the conftest socket guard.
"""

from __future__ import annotations

import getpass
import os
import re
import socket
import sys
import plistlib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from chinamaxM.ops.supervision import (
    LOOPBACK_HOST,
    WINSW_SHA256,
    RenderedArtifact,
    SupervisionConfig,
    SupervisionError,
    SupervisionStatus,
    enable_linger,
    ensure_winsw_exe,
    install,
    linger_enabled,
    port_live,
    render,
    status,
    teardown,
    update,
)


# --------------------------------------------------------------------------- helpers


class ScriptedRunner:
    """A recording fake service-manager runner keyed by a distinctive argv token.

    Each response is ``(exit_code, stdout, stderr)``; a bare int or a 2-tuple fills the
    missing fields with ``""``. The first keyed token found in an argv list wins; an
    unkeyed command returns ``(0, "", "")``. Every call is recorded verbatim in ``calls``.
    """

    def __init__(self, responses: dict | None = None) -> None:
        self.calls: list[list[str]] = []
        self.responses: dict[str, tuple[int, str, str]] = {}
        for token, resp in (responses or {}).items():
            self.set(token, resp)

    def set(self, token: str, resp) -> None:
        if isinstance(resp, int):
            resp = (resp, "", "")
        elif len(resp) == 2:
            resp = (resp[0], resp[1], "")
        self.responses[token] = tuple(resp)

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        for token, resp in self.responses.items():
            if (tuple(argv[2:]) == token if isinstance(token, tuple) else token in argv):
                return resp
        return (0, "", "")


def _verb(argv: list[str]) -> str:
    """Return the action verb of a recorded systemctl/launchctl/sc.exe/WinSW call."""
    if argv[0] == "systemctl":
        return argv[2]
    if argv[0] in ("launchctl", "sc.exe"):
        return argv[1]
    return argv[1]  # a WinSW-exe call: [exe_path, verb]


#: Path-token scanner: absolute/`~`/drive-letter paths, excluding URL ``//``, word-joined
#: ``/``, and XML/close-tag ``</…>`` slashes so only real filesystem paths are captured.
_PATH_RE = re.compile(r'(?<![:\w/<])(?:[A-Za-z]:\\[^\s"<>]+|/[^\s"<>]+|~[^\s"<>]+)')


def _path_tokens(content: str) -> list[str]:
    """Return the filesystem-path-like tokens in an artifact, minus its XML prolog/DOCTYPE."""
    body = "\n".join(
        line
        for line in content.splitlines()
        if not line.lstrip().startswith(("<?", "<!"))
    )
    return _PATH_RE.findall(body)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def winsw_exe(tmp_path) -> Path:
    """An operator-supplied WinSW executable stand-in (existing file with sentinel bytes)."""
    path = tmp_path / "winsw" / "WinSW.exe"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"MZ-fake-winsw-binary")
    return path


def _config(tmp_path, **overrides) -> SupervisionConfig:
    """Build a valid SupervisionConfig with real python + a temp log dir."""
    fields = dict(
        python_path=sys.executable,
        entry=["-m", "chinamaxM.proxy"],
        port=8402,
        log_dir=tmp_path / "logs",
    )
    fields.update(overrides)
    return SupervisionConfig(**fields)


# ----------------------------------------------------- test 1: unit content per OS (AC-1)


def test_unit_content_per_os(tmp_path, winsw_exe):
    """AC-1: rendered artifacts carry the ADR-pinned + plan-pinned policies per OS."""
    log_dir = tmp_path / "logs"
    cfg = _config(tmp_path, log_dir=log_dir, winsw_exe_path=winsw_exe)

    # Session-bound service and one-shot login timer; crash restarts have no sleep.
    systemd = render(cfg, platform="linux")
    assert isinstance(systemd, RenderedArtifact)
    unit = systemd.content.decode("utf-8")
    assert "Restart=always" in unit
    assert "RestartSec=3" in unit
    assert "WantedBy=" not in unit
    assert "Requisite=gnome-session.target" in unit
    assert "PartOf=gnome-session.target" in unit
    assert "ExecStartPre" not in unit
    timer = systemd.companions[0]
    assert timer.path.name == "chinamaxM.timer"
    text = timer.content.decode()
    for setting in ("OnActiveSec=60s", "AccuracySec=1s", "DefaultDependencies=no",
                    "WantedBy=gnome-session.target", "PartOf=gnome-session.target",
                    "Requisite=gnome-session.target", "RemainAfterElapse=yes"):
        assert setting in text
    assert "default.target" not in text
    assert "OnBootSec" not in text and "OnStartupSec" not in text
    assert f'ExecStart="{sys.executable}" "-m" "chinamaxM.proxy"' in unit
    assert str(log_dir) not in unit  # Linux logs to journald, never a file path

    # launchd LaunchAgent: KeepAlive true + RunAtLoad, log paths under log_dir.
    plist = plistlib.loads(render(cfg, platform="darwin").content)
    assert plist["Label"] == "com.chinamaxM.proxy"
    assert plist["KeepAlive"] is True
    assert plist["RunAtLoad"] is True
    assert plist["ProgramArguments"] == [sys.executable, "-m", "chinamaxM.proxy"]
    assert plist["StandardOutPath"] == str(log_dir / "proxy.out.log")
    assert plist["StandardErrorPath"] == str(log_dir / "proxy.err.log")

    # WinSW XML: onfailure-restart, serviceaccount (installing user, allowservicelogon),
    # conda python executable, log under log_dir, and NEVER a <password> element.
    xml_text = render(cfg, platform="windows").content.decode("utf-8")
    assert "<password" not in xml_text
    root = ET.fromstring(xml_text)
    assert root.tag == "service"
    assert root.find("id").text == "chinamaxM"
    assert root.find("executable").text == sys.executable
    assert root.find("arguments").text == "-m chinamaxM.proxy"
    assert root.find("onfailure").attrib == {"action": "restart", "delay": "2 sec"}
    assert root.find("logpath").text == str(log_dir)
    assert root.find("serviceaccount/allowservicelogon").text == "true"
    assert root.find("serviceaccount/username").text == getpass.getuser()
    assert root.find(".//password") is None


def test_rendering_escapes_spaces_and_percent(tmp_path):
    """AC-1: resolved paths with a space (and a % for systemd) escape correctly per OS."""
    weird = tmp_path / "my env"
    weird.mkdir()
    py = weird / "py%exe"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    winsw = weird / "WinSW v1.exe"
    winsw.write_bytes(b"MZ")
    log_dir = tmp_path / "log space"
    cfg = SupervisionConfig(
        python_path=py,
        entry=["-c", "import time; time.sleep(1)"],
        port=8402,
        log_dir=log_dir,
        winsw_exe_path=winsw,
    )

    # systemd: % -> %%, spaces preserved inside double quotes, the ; entry quoted whole.
    unit = render(cfg, platform="linux").content.decode("utf-8")
    quoted_py = str(py).replace("%", "%%")
    assert f'ExecStart="{quoted_py}" "-c" "import time; time.sleep(1)"' in unit

    # launchd via plistlib: values round-trip verbatim (no escaping needed by the reader).
    plist = plistlib.loads(render(cfg, platform="darwin").content)
    assert plist["ProgramArguments"] == [str(py), "-c", "import time; time.sleep(1)"]
    assert plist["StandardOutPath"] == str(log_dir / "proxy.out.log")

    # WinSW via a real XML writer: the executable + args + logpath round-trip exactly.
    root = ET.fromstring(render(cfg, platform="windows").content.decode("utf-8"))
    assert root.find("executable").text == str(py)  # space + % survive XML escaping
    assert root.find("arguments").text == '-c "import time; time.sleep(1)"'
    assert root.find("logpath").text == str(log_dir)


# --------------------------------------------------- test 2: no secrets / no foreign paths


def test_no_secrets_or_foreign_paths(tmp_path, winsw_exe, monkeypatch):
    """AC-2: canary env + in-memory password never appear; every path token is resolved."""
    canary = "sk-" + "should-never-appear-canary"  # fragment-built so the secret sweep never trips
    monkeypatch.setenv("CANARY_SECRET", canary)
    log_dir = tmp_path / "chinamaxM-logs"
    cfg = SupervisionConfig(
        python_path=sys.executable,
        entry=["-m", "chinamaxM.proxy"],
        port=8402,
        log_dir=log_dir,
        winsw_exe_path=winsw_exe,
        service_password="PLACEHOLDER-SECRET-PW",
    )
    allowed_prefixes = (sys.executable, str(log_dir))

    for platform in ("linux", "darwin", "windows"):
        content = render(cfg, platform=platform).content.decode("utf-8")
        assert canary not in content
        assert "PLACEHOLDER-SECRET-PW" not in content
        assert "<password" not in content
        for token in _path_tokens(content):
            assert any(token.startswith(prefix) for prefix in allowed_prefixes), (
                platform,
                token,
            )


# ---------------------------------------------- test 3: idempotent + mocked ops (AC-3)


def test_ops_idempotent_and_mocked(tmp_path, monkeypatch):
    """Converge both units without starting the service or resetting its timer."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = _config(tmp_path)
    artifact = render(cfg, platform="linux")
    unit, timer = artifact.path, artifact.companions[0].path
    responses = {
        ("is-enabled", "chinamaxM.service"): (1, "static"),
        ("is-enabled", "chinamaxM.timer"): (1, "disabled"),
        ("is-active", "gnome-session.target"): (0, "active"),
        ("is-active", "chinamaxM.timer"): (3, "inactive"),
    }
    runner = ScriptedRunner(responses)
    install(cfg, platform="linux", runner=runner)
    assert unit.read_bytes() == artifact.content
    assert timer.read_bytes() == artifact.companions[0].content
    assert runner.calls == [
        ["systemctl", "--user", "is-enabled", "chinamaxM.service"],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "is-enabled", "chinamaxM.timer"],
        ["systemctl", "--user", "enable", "chinamaxM.timer"],
        ["systemctl", "--user", "is-active", "gnome-session.target"],
        ["systemctl", "--user", "is-active", "chinamaxM.timer"],
        ["systemctl", "--user", "start", "chinamaxM.timer"],
    ]
    link = timer.parent / "gnome-session.target.wants" / timer.name
    link.parent.mkdir()
    link.symlink_to(timer)
    responses[("is-enabled", "chinamaxM.timer")] = (0, "enabled")
    responses[("is-active", "chinamaxM.timer")] = (0, "active")
    runner = ScriptedRunner(responses)
    install(cfg, platform="linux", runner=runner)
    assert all(_verb(c).startswith("is-") for c in runner.calls)

    # A timer-only drift is repaired without resetting the countdown or Proxy.
    timer.write_text("drift")
    runner = ScriptedRunner(responses)
    update(cfg, platform="linux", runner=runner)
    assert timer.read_bytes() == artifact.companions[0].content
    assert [_verb(c) for c in runner.calls].count("daemon-reload") == 1
    assert not any(_verb(c) in ("start", "stop", "restart") for c in runner.calls)

    # Legacy boot enablement is disabled without --now, even on a running installation.
    responses[("is-enabled", "chinamaxM.service")] = (0, "enabled")
    runner = ScriptedRunner(responses)
    update(cfg, platform="linux", runner=runner)
    assert runner.calls[1] == ["systemctl", "--user", "disable", "chinamaxM.service"]
    assert not any(_verb(c) in ("start", "stop", "restart") for c in runner.calls)

    # No GNOME session: install enables future startup without starting anything.
    responses[("is-enabled", "chinamaxM.service")] = (1, "static")
    responses[("is-active", "gnome-session.target")] = (3, "inactive")
    runner = ScriptedRunner(responses)
    install(cfg, platform="linux", runner=runner)
    assert not any(_verb(c) == "start" for c in runner.calls)

    runner = ScriptedRunner()
    teardown(cfg, platform="linux", runner=runner)
    assert not unit.exists() and not timer.exists()
    assert runner.calls == [
        ["systemctl", "--user", "stop", "chinamaxM.timer"],
        ["systemctl", "--user", "disable", "chinamaxM.timer"],
        ["systemctl", "--user", "stop", "chinamaxM.service"],
        ["systemctl", "--user", "disable", "chinamaxM.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]
    absent = ScriptedRunner({"stop": (5, "", "Unit not loaded"),
                             "disable": (1, "", "Unit does not exist")})
    teardown(cfg, platform="linux", runner=absent)


def test_launchd_ops_command_sequences(tmp_path):
    """AC-3 (macOS): mocked launchctl bootstrap/kickstart/bootout with exact argv order."""
    cfg = _config(tmp_path)
    uid = os.getuid()
    plist = Path(os.environ["HOME"]) / "Library" / "LaunchAgents" / "com.chinamaxM.proxy.plist"
    domain = f"gui/{uid}"
    target = f"gui/{uid}/com.chinamaxM.proxy"

    # fresh install: not loaded ⇒ bootstrap.
    r1 = ScriptedRunner({"print": (1, "", "Could not find service")})
    install(cfg, platform="darwin", runner=r1)
    assert plist.exists()
    assert r1.calls == [
        ["launchctl", "print", target],
        ["launchctl", "bootstrap", domain, str(plist)],
    ]

    # install over loaded + running ⇒ no bootstrap/kickstart.
    r2 = ScriptedRunner({"print": (0, "pid = 4242\nstate = running")})
    install(cfg, platform="darwin", runner=r2)
    assert r2.calls == [["launchctl", "print", target]]

    # install loaded-but-not-running ⇒ kickstart.
    r3 = ScriptedRunner({"print": (0, "state = waiting")})
    install(cfg, platform="darwin", runner=r3)
    assert r3.calls == [
        ["launchctl", "print", target],
        ["launchctl", "kickstart", target],
    ]

    # update loaded ⇒ bootout + bootstrap (a running agent won't adopt a rewrite otherwise).
    r4 = ScriptedRunner({"print": (0, "pid = 5")})
    update(cfg, platform="darwin", runner=r4)
    assert r4.calls == [
        ["launchctl", "print", target],
        ["launchctl", "bootout", target],
        ["launchctl", "bootstrap", domain, str(plist)],
    ]

    # update-when-absent ⇒ bootstrap only (converges like install).
    r5 = ScriptedRunner({"print": (1, "", "not found")})
    update(cfg, platform="darwin", runner=r5)
    assert r5.calls == [
        ["launchctl", "print", target],
        ["launchctl", "bootstrap", domain, str(plist)],
    ]

    # teardown loaded ⇒ bootout + remove plist.
    r6 = ScriptedRunner({"print": (0, "pid = 9")})
    teardown(cfg, platform="darwin", runner=r6)
    assert not plist.exists()
    assert r6.calls == [
        ["launchctl", "print", target],
        ["launchctl", "bootout", target],
    ]

    # double teardown / absent ⇒ print only, no error.
    r7 = ScriptedRunner({"print": (1, "", "not found")})
    teardown(cfg, platform="darwin", runner=r7)
    assert r7.calls == [["launchctl", "print", target]]


def test_launchd_creates_log_dir(tmp_path):
    """AC-3 (macOS): install AND update create log_dir (launchd won't make the log parents)."""
    not_loaded = {"print": (1, "", "not found")}

    log_dir = tmp_path / "nested" / "logs"
    assert not log_dir.exists()
    install(_config(tmp_path, log_dir=log_dir), platform="darwin", runner=ScriptedRunner(not_loaded))
    assert log_dir.is_dir()

    other = tmp_path / "nested2" / "logs"  # a fresh dir proves the update path independently
    assert not other.exists()
    update(_config(tmp_path, log_dir=other), platform="darwin", runner=ScriptedRunner(not_loaded))
    assert other.is_dir()


def test_winsw_ops_command_sequences(tmp_path, winsw_exe):
    """AC-3 (Windows): stop/uninstall BEFORE replace; the sc.exe config password= call."""
    log_dir = tmp_path / "logs"
    cfg = SupervisionConfig(
        python_path=sys.executable,
        entry=["-m", "chinamaxM.proxy"],
        port=8402,
        log_dir=log_dir,
        winsw_exe_path=winsw_exe,
        service_password="PLACEHOLDER-PW",
    )
    service_dir = log_dir / "service"
    xml = service_dir / "chinamaxM-service.xml"
    exe = str(service_dir / "chinamaxM-service.exe")

    # fresh install: not registered (1060) ⇒ install, sc.exe config password=, start.
    r1 = ScriptedRunner({"query": (1060, "", "")})
    install(cfg, platform="windows", runner=r1)
    assert xml.exists()
    assert Path(exe).read_bytes() == winsw_exe.read_bytes()  # exe copied beside the XML
    assert r1.calls == [
        ["sc.exe", "query", "chinamaxM"],
        [exe, "install"],
        ["sc.exe", "config", "chinamaxM", "password=", "PLACEHOLDER-PW"],
        [exe, "start"],
    ]

    # install over registered + running ⇒ no state-changing calls.
    r2 = ScriptedRunner({"query": (0, "STATE : 4  RUNNING")})
    install(cfg, platform="windows", runner=r2)
    assert r2.calls == [["sc.exe", "query", "chinamaxM"]]

    # install registered-but-stopped ⇒ start only.
    r3 = ScriptedRunner({"query": (0, "STATE : 1  STOPPED")})
    install(cfg, platform="windows", runner=r3)
    assert r3.calls == [["sc.exe", "query", "chinamaxM"], [exe, "start"]]

    # update registered: stop + uninstall BEFORE the exe is replaced, then reinstall.
    Path(exe).write_bytes(b"OLD-LOCKED-EXE")
    r4 = ScriptedRunner({"query": (0, "STATE : 4  RUNNING")})
    update(cfg, platform="windows", runner=r4)
    assert Path(exe).read_bytes() == winsw_exe.read_bytes()  # replaced after stop+uninstall
    assert r4.calls == [
        ["sc.exe", "query", "chinamaxM"],
        [exe, "stop"],
        [exe, "uninstall"],
        [exe, "install"],
        ["sc.exe", "config", "chinamaxM", "password=", "PLACEHOLDER-PW"],
        [exe, "start"],
    ]

    # teardown registered + XML present ⇒ stop, uninstall, remove both artifacts.
    r5 = ScriptedRunner({"query": (0, "STATE : 4  RUNNING")})
    teardown(cfg, platform="windows", runner=r5)
    assert not xml.exists()
    assert not Path(exe).exists()
    assert r5.calls == [
        ["sc.exe", "query", "chinamaxM"],
        [exe, "stop"],
        [exe, "uninstall"],
    ]

    # teardown when nothing installed (1060, XML gone) ⇒ query only, no error.
    r6 = ScriptedRunner({"query": (1060, "", "")})
    teardown(cfg, platform="windows", runner=r6)
    assert r6.calls == [["sc.exe", "query", "chinamaxM"]]


def test_winsw_teardown_sc_delete_fallback(tmp_path, winsw_exe):
    """AC-3 (Windows): registered but XML missing ⇒ sc.exe delete fallback."""
    cfg = _config(tmp_path, winsw_exe_path=winsw_exe)
    r = ScriptedRunner({"query": (0, "STATE : 1  STOPPED")})
    teardown(cfg, platform="windows", runner=r)
    assert r.calls == [
        ["sc.exe", "query", "chinamaxM"],
        ["sc.exe", "delete", "chinamaxM"],
    ]


def test_ensure_winsw_exe(tmp_path, winsw_exe):
    """Fix 2: override → already-installed → checksummed download, fail-closed on mismatch."""
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    installed = service_dir / "chinamaxM-service.exe"

    def _forbid_download(url):
        raise AssertionError("ensure_winsw_exe downloaded when it should not have")

    # (a) override_path given → returned unchanged, no download, nothing placed at service_dir.
    got = ensure_winsw_exe(service_dir, override_path=winsw_exe, downloader=_forbid_download)
    assert got == winsw_exe
    assert not installed.exists()

    # a missing override → SupervisionError (still no download).
    with pytest.raises(SupervisionError):
        ensure_winsw_exe(service_dir, override_path=tmp_path / "nope.exe", downloader=_forbid_download)

    # (b) an exe already at service_dir → returned, no download (idempotent, untouched).
    installed.write_bytes(b"MZ-existing-winsw")
    got_b = ensure_winsw_exe(service_dir, downloader=_forbid_download)
    assert got_b == installed
    assert installed.read_bytes() == b"MZ-existing-winsw"
    installed.unlink()

    # (c) absent → download bytes whose (injected) hash == WINSW_SHA256 → placed at service_dir.
    payload = b"downloaded-winsw-bytes"
    got_c = ensure_winsw_exe(
        service_dir,
        downloader=lambda url: payload,
        hasher=lambda data: WINSW_SHA256,
    )
    assert got_c == installed
    assert installed.read_bytes() == payload
    installed.unlink()

    # (d) checksum MISMATCH → SupervisionError, NO exe placed (fail closed).
    with pytest.raises(SupervisionError):
        ensure_winsw_exe(
            service_dir,
            downloader=lambda url: b"tampered-bytes",
            hasher=lambda data: "0" * 64,
        )
    assert not installed.exists()


# ---------------------------------------- test 4: distinct status primitives (AC-4)


def test_status_primitives_distinct(tmp_path, monkeypatch):
    """AC-4: installed/enabled/running/port_live report independently; bad query raises."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    unit = tmp_path / "xdg" / "systemd" / "user" / "chinamaxM.service"
    unit.parent.mkdir(parents=True)
    unit.write_bytes(b"[Unit]\n")
    timer = unit.with_suffix(".timer")
    timer.write_bytes(b"[Timer]\n")
    link = unit.parent / "gnome-session.target.wants" / timer.name
    link.parent.mkdir()
    link.symlink_to(timer)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((LOOPBACK_HOST, 0))
    listener.listen(1)
    live_port = listener.getsockname()[1]

    bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    bound.bind((LOOPBACK_HOST, 0))  # bound but NOT listening ⇒ deterministic refused
    closed_port = bound.getsockname()[1]

    try:
        # the standalone probe: a real open listener ⇒ True, a refused port ⇒ False.
        assert port_live(live_port) is True
        assert port_live(closed_port) is False

        active = ScriptedRunner({"is-enabled": (0, "enabled"), "is-active": (0, "active")})
        inactive = ScriptedRunner(
            {"is-enabled": (1, "disabled"), "is-active": (4, "inactive")}
        )

        # artifact present, manager inactive + disabled, port refused: all four distinct.
        st = status(_config(tmp_path, port=closed_port), platform="linux", runner=inactive)
        assert st == SupervisionStatus(
            installed=True, enabled=False, running=False, port_live=False
        )

        # enabled + active but port still refused ⇒ port_live independent of manager state.
        st2 = status(_config(tmp_path, port=closed_port), platform="linux", runner=active)
        assert st2 == SupervisionStatus(
            installed=True, enabled=True, running=True, port_live=False
        )

        # same manager state, a LIVE port ⇒ only port_live flips.
        st3 = status(_config(tmp_path, port=live_port), platform="linux", runner=active)
        assert st3 == SupervisionStatus(
            installed=True, enabled=True, running=True, port_live=True
        )

        # a failing manager query outside the negative table RAISES, never a False bool.
        bus_fail = ScriptedRunner(
            {
                "is-enabled": (1, "disabled"),
                "is-active": (1, "", "Failed to connect to bus: No such file or directory"),
            }
        )
        with pytest.raises(SupervisionError):
            status(_config(tmp_path, port=closed_port), platform="linux", runner=bus_fail)

        unrecognized = ScriptedRunner(
            {"is-enabled": (1, "disabled"), "is-active": (2, "garbage-state")}
        )
        with pytest.raises(SupervisionError):
            status(_config(tmp_path, port=closed_port), platform="linux", runner=unrecognized)

        # the SAME contract holds symmetrically on the is-enabled query (queried first).
        enabled_bus_fail = ScriptedRunner(
            {"is-enabled": (1, "", "Failed to connect to bus: No such file or directory")}
        )
        with pytest.raises(SupervisionError):
            status(_config(tmp_path, port=closed_port), platform="linux", runner=enabled_bus_fail)

        enabled_unrecognized = ScriptedRunner({"is-enabled": (2, "garbage-token")})
        with pytest.raises(SupervisionError):
            status(
                _config(tmp_path, port=closed_port), platform="linux", runner=enabled_unrecognized
            )
    finally:
        listener.close()
        bound.close()


# ------------------------------------------------------- config validation + linger


def test_config_validation(tmp_path, winsw_exe):
    """SupervisionConfig validates every resolved parameter on construction."""
    base = dict(
        python_path=sys.executable,
        entry=["-m", "chinamaxM.proxy"],
        port=8402,
        log_dir=tmp_path / "logs",
    )
    SupervisionConfig(**base)  # the baseline is valid
    SupervisionConfig(**{**base, "winsw_exe_path": winsw_exe})  # an existing WinSW is fine

    for bad in ({"port": 0}, {"port": 70000}, {"port": True}, {"entry": []}):
        with pytest.raises(SupervisionError):
            SupervisionConfig(**{**base, **bad})

    # control characters / newlines in any resolved param are rejected.
    with pytest.raises(SupervisionError):
        SupervisionConfig(**{**base, "entry": ["-m", "chinamaxM.proxy\n"]})
    with pytest.raises(SupervisionError):
        SupervisionConfig(**{**base, "log_dir": f"{tmp_path}/a\nb"})

    # missing python / WinSW paths are rejected.
    with pytest.raises(SupervisionError):
        SupervisionConfig(**{**base, "python_path": str(tmp_path / "no-such-python")})
    with pytest.raises(SupervisionError):
        SupervisionConfig(**{**base, "winsw_exe_path": str(tmp_path / "no-such.exe")})


def test_linger_helpers():
    """enable_linger/linger_enabled wrap loginctl on Linux and no-op with a status off it."""
    off = ScriptedRunner()
    enable_linger(runner=off, platform="windows")  # no-op
    assert linger_enabled(runner=off, platform="darwin") is False
    assert off.calls == []

    user = getpass.getuser()
    added = ScriptedRunner()
    enable_linger(runner=added, platform="linux")
    assert added.calls == [["loginctl", "enable-linger", user]]

    yes = ScriptedRunner({"show-user": (0, "yes")})
    assert linger_enabled(runner=yes, platform="linux") is True
    no = ScriptedRunner({"show-user": (0, "no")})
    assert linger_enabled(runner=no, platform="linux") is False

    failing = ScriptedRunner({"enable-linger": (1, "", "boom")})
    with pytest.raises(SupervisionError):
        enable_linger(runner=failing, platform="linux")


@pytest.mark.parametrize(
    "session,timer_state,service_state,expected",
    [
        (False, "inactive", "inactive", "waiting for GNOME login"),
        (True, "waiting", "inactive", "waiting for the 60-second GNOME login timer"),
        (True, "elapsed", "inactive", ""),
        (True, "failed", "inactive", ""),
        (False, "inactive", "failed", ""),
    ],
)
def test_session_waiting_states(tmp_path, monkeypatch, session, timer_state, service_state, expected):
    """Only a healthy installation may report a pending session or countdown."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = _config(tmp_path)
    artifact = render(cfg, platform="linux")
    for item in artifact.artifacts:
        item.path.parent.mkdir(parents=True, exist_ok=True)
        item.path.write_bytes(item.content)
    timer = artifact.companions[0].path
    link = timer.parent / "gnome-session.target.wants" / timer.name
    link.parent.mkdir()
    link.symlink_to(timer)
    runner = ScriptedRunner({
        ("is-active", "gnome-session.target"): (0 if session else 3, "active" if session else "inactive"),
        "is-active": (3, service_state),
        "is-enabled": (0, "enabled"),
        ("show", "chinamaxM.timer", "--property=ActiveState,SubState,ActiveEnterTimestampMonotonic"):
            (0, f"ActiveState={'failed' if timer_state == 'failed' else 'active'}\nSubState={timer_state}"),
        "show": (0, f"ActiveState={service_state}"),
    })
    assert status(cfg, platform="linux", runner=runner).waiting_reason == expected
    # A legacy boot-start link invalidates even an otherwise healthy timer.
    legacy = timer.parent / "default.target.wants" / artifact.path.name
    legacy.parent.mkdir()
    legacy.symlink_to(artifact.path)
    broken = status(cfg, platform="linux", runner=runner)
    assert not broken.enabled and not broken.waiting_reason
    # Missing companion cannot be excused by an inactive graphical session.
    timer.rename(timer.with_suffix(".saved"))
    broken = status(cfg, platform="linux", runner=runner)
    assert not broken.installed and not broken.waiting_reason


def test_timer_mutation_failure_propagates(tmp_path, monkeypatch):
    """A timer enable failure cannot be reported as a successful install."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    runner = ScriptedRunner({"is-enabled": (1, "disabled"), "enable": (1, "", "Permission denied")})
    with pytest.raises(SupervisionError, match="Permission denied"):
        install(_config(tmp_path), platform="linux", runner=runner)
    assert not any(_verb(c) == "start" for c in runner.calls)
