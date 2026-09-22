"""OS-native Proxy supervision: systemd user / launchd / WinSW machinery.

Pure supervision machinery for the local reverse Proxy (ADR 0009 as amended). Platform
detection selects one of three managers; each renders its unit artifact from resolved
values only, then drives install/update/teardown as *converge* operations and exposes
four independent status primitives (``installed``/``enabled``/``running``/``port_live``)
that Doctor consumes. Every service-manager invocation goes through an injectable runner
so macOS/Windows are exercised with mocked commands and Linux with a real systemd unit.

Public surface (pinned so hosts-04/05 consume a stable interface):

* :class:`SupervisionConfig` — the resolved input, validated on construction.
* :func:`install`, :func:`update`, :func:`teardown`, :func:`status` — each takes an
  optional ``platform`` override and an injectable ``runner``.
* :func:`render` — the artifact bytes + install path, for content tests and diagnosis.
* :func:`ensure_winsw_exe` — resolve the Windows WinSW exe (operator override →
  already-installed → pinned, SHA-256-verified download; fails CLOSED on mismatch).
* :func:`port_live` — a standalone loopback readiness probe.
* :func:`enable_linger` / :func:`linger_enabled` — ``loginctl`` wrappers (no-op with a
  diagnosable status off-Linux).
* :class:`SupervisionError` — raised on any command failure or validation fault, carrying
  the command, exit code, and stderr.

The WinSW artifact NEVER contains a ``<password>`` element (ADR 0009 as amended); a
Windows service password travels in-memory only, applied after install via
``sc.exe config … password=`` through the runner.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import plistlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from chinamaxM.generate import _atomic_write
from chinamaxM.keyfiles import resolve_host_root

__all__ = [
    "SupervisionConfig",
    "SupervisionStatus",
    "SupervisionError",
    "RenderedArtifact",
    "install",
    "update",
    "teardown",
    "status",
    "render",
    "ensure_winsw_exe",
    "port_live",
    "enable_linger",
    "linger_enabled",
]

#: The runner protocol: an argv list in, ``(exit_code, stdout, stderr)`` out.
Runner = Callable[[Sequence[str]], "tuple[int, str, str]"]

#: Loopback host for the port-live probe (the Proxy is never exposed off-host, ADR 0001).
LOOPBACK_HOST = "127.0.0.1"

#: Default subprocess timeout (seconds) for the production runner.
_RUNNER_TIMEOUT = 30.0

#: Pinned identifiers (unit/plist/service names are plan-level, not ADR — see ADR 0009).
_UNIT_NAME = "chinamaxM.service"
_TIMER_NAME = "chinamaxM.timer"
_SESSION_TARGET = "gnome-session.target"
_DESCRIPTION = "chinamaxM local reverse proxy"
_LAUNCHD_LABEL = "com.chinamaxM.proxy"
_WINSW_SERVICE_ID = "chinamaxM"
_WINSW_XML_NAME = "chinamaxM-service.xml"
_WINSW_EXE_NAME = "chinamaxM-service.exe"

#: Pinned official WinSW release auto-acquired when no exe is supplied (ADR 0009 as
#: amended): winsw/winsw v2.12.0, asset ``WinSW-x64.exe``.
WINSW_DOWNLOAD_URL = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"

#: SHA-256 of that pinned asset (from the official GitHub release). The auto-download fails
#: CLOSED on any mismatch. This is a PUBLIC checksum, never a secret.
WINSW_SHA256 = "05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da"

#: Read timeout (seconds) for the WinSW auto-download.
_WINSW_DOWNLOAD_TIMEOUT = 120.0

#: ``sys.platform`` / override tokens → the supported OS key.
_PLATFORM_ALIASES = {
    "linux": "linux",
    "darwin": "darwin",
    "windows": "windows",
    "win32": "windows",
}

#: systemd ``is-active`` stdout tokens (stable across versions; exit codes are not —
#: an unknown unit reports ``inactive`` with exit 4 on this host, ``3`` on others).
_ACTIVE_TRUE = frozenset({"active"})
_ACTIVE_FALSE = frozenset(
    {"inactive", "failed", "deactivating", "activating", "reloading", "maintenance"}
)

#: systemd ``is-enabled`` stdout tokens; ``not-found`` is what a missing unit reports.
_ENABLED_TRUE = frozenset({"enabled", "enabled-runtime"})
_ENABLED_FALSE = frozenset(
    {
        "disabled",
        "static",
        "masked",
        "masked-runtime",
        "linked",
        "linked-runtime",
        "alias",
        "indirect",
        "generated",
        "transient",
        "bad",
        "not-found",
    }
)

#: stderr fragments that turn any manager query into a raise, never a False boolean
#: (a missing --user bus, an authorization/permission failure — ADR 0009 error contract).
_RAISE_MARKERS = (
    "Failed to connect to",
    "Permission denied",
    "Access denied",
    "Interactive authentication required",
)

#: stderr fragments that mark a teardown stop/disable/bootout as already-at-target.
_ABSENT_MARKERS = ("does not exist", "not loaded", "No such file", "not found", "not-found")

#: ``sc.exe`` exit code for "the specified service does not exist as an installed service".
_SC_NOT_INSTALLED = 1060


class SupervisionError(RuntimeError):
    """Raised on a failed service-manager command or a config validation fault.

    Attributes:
        command: The argv that failed, or ``None`` for a validation fault.
        exit_code: The process exit code, or ``None`` when unavailable.
        stderr: The captured stderr, or ``None``.
    """

    def __init__(
        self,
        command: Sequence[str] | None = None,
        exit_code: int | None = None,
        stderr: str | None = None,
        *,
        message: str | None = None,
    ) -> None:
        self.command = list(command) if command is not None else None
        self.exit_code = exit_code
        self.stderr = stderr
        if message is None:
            message = (
                f"command {self.command!r} failed (exit {exit_code}): "
                f"{(stderr or '').strip()}"
            )
        super().__init__(message)


@dataclass(frozen=True)
class SupervisionStatus:
    """The four independent supervision status booleans, never conflated (ADR 0009)."""

    installed: bool
    enabled: bool
    running: bool
    port_live: bool
    waiting_reason: str = ""
    failure_reason: str = ""


@dataclass(frozen=True)
class RenderedArtifact:
    """A rendered unit artifact: its install path and its exact bytes."""

    path: Path
    content: bytes
    companions: tuple[RenderedArtifact, ...] = ()

    @property
    def artifacts(self) -> tuple[RenderedArtifact, ...]:
        """Return the primary artifact followed by any companion units."""
        return (self, *self.companions)


def default_log_dir() -> Path:
    """Return the canonical log directory ``<claude-root>/chinamaxM`` (ADR 0006 chain)."""
    return resolve_host_root("claude") / "chinamaxM"


@dataclass
class SupervisionConfig:
    """The resolved supervision input, validated on construction.

    Attributes:
        python_path: The conda env's Python executable (must exist).
        entry: The full argv after the Python executable — production
            ``["-m", "chinamaxM.proxy"]``; must be a non-empty list of strings.
        port: The Registry-controlled Proxy port, 1–65535 (ADR 0001, default 8402).
        log_dir: The log/artifact base directory; defaults to :func:`default_log_dir`.
        winsw_exe_path: The resolved WinSW executable (Windows only); when set it must
            exist. Setup resolves it via :func:`ensure_winsw_exe` (operator override →
            already-installed → pinned, SHA-256-verified download; ADR 0009 as amended).
        service_password: An optional in-memory Windows service-account password, applied
            after install via ``sc.exe config`` — NEVER serialized into any artifact.

    Raises:
        SupervisionError: On an unsupported platform, an out-of-range port, an empty
            entry, a missing python/WinSW path, or a control character in any resolved
            parameter (no unit/plist/XML serializer can represent them safely).
    """

    python_path: str | os.PathLike[str]
    entry: Sequence[str]
    port: int
    log_dir: str | os.PathLike[str] | None = None
    winsw_exe_path: str | os.PathLike[str] | None = None
    service_password: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Normalize path fields and validate every resolved parameter."""
        _resolve_platform(None)  # the current platform must be supported

        self.python_path = str(self.python_path)
        self.entry = list(self.entry)
        self.log_dir = Path(self.log_dir) if self.log_dir is not None else default_log_dir()
        if self.winsw_exe_path is not None:
            self.winsw_exe_path = Path(self.winsw_exe_path)

        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise SupervisionError(message="port must be an integer in 1..65535")
        if not 1 <= self.port <= 65535:
            raise SupervisionError(message=f"port {self.port} is out of range 1..65535")

        if not self.entry or not all(isinstance(item, str) for item in self.entry):
            raise SupervisionError(message="entry must be a non-empty list of strings")

        _reject_control("python_path", self.python_path)
        for item in self.entry:
            _reject_control("entry", item)
        _reject_control("log_dir", str(self.log_dir))
        if self.winsw_exe_path is not None:
            _reject_control("winsw_exe_path", str(self.winsw_exe_path))
        if self.service_password is not None:
            _reject_control("service_password", self.service_password)

        if not Path(self.python_path).is_file():
            raise SupervisionError(
                message=f"python_path is not an existing file: {self.python_path!r}"
            )
        if self.winsw_exe_path is not None and not self.winsw_exe_path.is_file():
            raise SupervisionError(
                message=f"winsw_exe_path is not an existing file: {self.winsw_exe_path!r}"
            )


# --------------------------------------------------------------------------- validation


def _reject_control(label: str, value: str) -> None:
    """Reject any C0 control character (incl. newline) or DEL in a resolved parameter.

    Raises:
        SupervisionError: If ``value`` carries a character no serializer can represent.
    """
    for char in value:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            raise SupervisionError(
                message=(
                    f"{label} contains a control character ({ord(char):#04x}); it cannot "
                    f"be safely serialized into a unit/plist/XML artifact"
                )
            )


def _resolve_platform(override: str | None) -> str:
    """Map an override or ``sys.platform`` to the supported OS key, or raise.

    Raises:
        SupervisionError: On a platform other than linux/darwin/windows.
    """
    token = override if override is not None else sys.platform
    key = _PLATFORM_ALIASES.get(token)
    if key is None:
        raise SupervisionError(
            message=f"unsupported platform {token!r}; supported: linux, darwin, windows"
        )
    return key


# --------------------------------------------------------------------------- runner glue


def _default_runner(argv: Sequence[str]) -> tuple[int, str, str]:
    """Run one service-manager command via ``subprocess.run`` with a 30 s timeout."""
    completed = subprocess.run(
        list(argv), capture_output=True, text=True, timeout=_RUNNER_TIMEOUT
    )
    return completed.returncode, completed.stdout, completed.stderr


def _invoke(runner: Runner, argv: Sequence[str]) -> tuple[int, str, str]:
    """Invoke the runner, wrapping transport faults (timeout, missing binary) as errors.

    Raises:
        SupervisionError: If the runner raises a subprocess or OS error.
    """
    argv = list(argv)
    try:
        code, out, err = runner(argv)
    except SupervisionError:
        raise
    except (subprocess.SubprocessError, OSError) as exc:
        raise SupervisionError(argv, None, str(exc)) from exc
    return code, out or "", err or ""


# --------------------------------------------------------------------------- port probe


def port_live(port: int, *, timeout: float = 1.0) -> bool:
    """Return whether a TCP connect to ``127.0.0.1:port`` succeeds within ``timeout``.

    A refused connection or a timeout returns ``False`` (the Proxy is not yet serving);
    a successful connect returns ``True``. Never raises.
    """
    try:
        with socket.create_connection((LOOPBACK_HOST, port), timeout=timeout):
            return True
    except OSError:
        return False


# ------------------------------------------------------------------------- linger helpers


def enable_linger(runner: Runner | None = None, *, platform: str | None = None) -> None:
    """Enable ``loginctl`` linger for the current user so login-scoped units survive reboot.

    Idempotent (enabling an already-lingering user succeeds). A no-op off Linux.

    Raises:
        SupervisionError: On a non-zero ``loginctl enable-linger`` exit (Linux only).
    """
    if _resolve_platform(platform) != "linux":
        return
    runner = runner or _default_runner
    argv = ["loginctl", "enable-linger", getpass.getuser()]
    code, _out, err = _invoke(runner, argv)
    if code != 0:
        raise SupervisionError(argv, code, err)


def linger_enabled(runner: Runner | None = None, *, platform: str | None = None) -> bool:
    """Return whether ``loginctl`` linger is enabled for the current user.

    ``False`` off Linux (linger is a Linux concept) and when the user has no logind
    record yet; ``True`` only when ``loginctl`` reports ``Linger=yes``.
    """
    if _resolve_platform(platform) != "linux":
        return False
    runner = runner or _default_runner
    argv = ["loginctl", "show-user", getpass.getuser(), "--property=Linger", "--value"]
    code, out, _err = _invoke(runner, argv)
    if code != 0:
        return False
    return out.strip() == "yes"


# --------------------------------------------------------------------------- base manager


class _Manager:
    """Shared runner glue, atomic writes, and the converge helpers for one OS."""

    def __init__(self, cfg: SupervisionConfig, runner: Runner) -> None:
        self._cfg = cfg
        self._runner = runner

    # -- runner helpers ----------------------------------------------------------------

    def _invoke(self, argv: Sequence[str]) -> tuple[int, str, str]:
        """Invoke the runner (transport faults become SupervisionError)."""
        return _invoke(self._runner, argv)

    def _run_checked(self, argv: Sequence[str], *, ok: Sequence[int] = (0,)) -> None:
        """Run a state-changing command; a non-``ok`` exit raises SupervisionError."""
        code, _out, err = self._invoke(argv)
        if code not in ok:
            raise SupervisionError(list(argv), code, err)

    def _run_tolerant(self, argv: Sequence[str]) -> None:
        """Run a teardown stop/disable; an already-at-target result is treated as success.

        Exit 0 is success; a non-zero exit is success only when stderr marks an absent
        target and carries no raise marker (missing bus / permission). Everything else
        raises, so a real failure is never coerced into a silent success.
        """
        code, _out, err = self._invoke(argv)
        if code == 0:
            return
        if any(marker in err for marker in _RAISE_MARKERS):
            raise SupervisionError(list(argv), code, err)
        if any(marker in err for marker in _ABSENT_MARKERS):
            return
        raise SupervisionError(list(argv), code, err)

    # -- filesystem helpers ------------------------------------------------------------

    def _write_if_changed(self, path: Path, content: bytes) -> bool:
        """Atomically write ``content`` unless the file already holds those exact bytes.

        Returns:
            ``True`` when the file was written, ``False`` when it was byte-identical.
        """
        try:
            if path.read_bytes() == content:
                return False
        except OSError:
            pass
        _atomic_write(path, content)
        return True

    def _remove(self, path: Path) -> bool:
        """Remove a file if present.

        Returns:
            ``True`` when a file was removed, ``False`` when it was already absent.

        Raises:
            SupervisionError: On an OS error other than a missing file.
        """
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SupervisionError(message=f"cannot remove {path}: {exc}") from exc


# --------------------------------------------------------------------------- systemd


class _SystemdManager(_Manager):
    """Linux systemd ``--user`` unit manager."""

    @property
    def artifact_path(self) -> Path:
        """The unit path under ``${XDG_CONFIG_HOME:-~/.config}/systemd/user/``."""
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        return Path(base) / "systemd" / "user" / _UNIT_NAME

    def render(self) -> bytes:
        """Render the GNOME-session-scoped service with fast crash recovery."""
        argv = [self._cfg.python_path, *self._cfg.entry]
        exec_start = " ".join(_systemd_quote(token) for token in argv)
        text = (
            "[Unit]\n"
            f"Description={_DESCRIPTION}\n"
            f"Requisite={_SESSION_TARGET}\n"
            f"After={_SESSION_TARGET}\n"
            f"PartOf={_SESSION_TARGET}\n"
            "\n"
            "[Service]\n"
            f"ExecStart={exec_start}\n"
            "Restart=always\n"
            "RestartSec=3\n"
        )
        return text.encode("utf-8")

    @property
    def timer_path(self) -> Path:
        """Return the companion login timer path."""
        return self.artifact_path.with_name(_TIMER_NAME)

    def render_timer(self) -> bytes:
        """Render a one-shot timer reset by each GNOME session."""
        return (
            "[Unit]\n"
            "Description=Start chinamaxM 60 seconds after GNOME login\n"
            "DefaultDependencies=no\n"
            f"Requisite={_SESSION_TARGET}\n"
            f"After={_SESSION_TARGET}\n"
            f"PartOf={_SESSION_TARGET}\n"
            "Conflicts=shutdown.target\n"
            "Before=shutdown.target\n\n"
            "[Timer]\n"
            "OnActiveSec=60s\n"
            "AccuracySec=1s\n"
            "RandomizedDelaySec=0\n"
            "RemainAfterElapse=yes\n"
            f"Unit={_UNIT_NAME}\n\n"
            "[Install]\n"
            f"WantedBy={_SESSION_TARGET}\n"
        ).encode("utf-8")

    def _systemctl(self, *args: str) -> list[str]:
        return ["systemctl", "--user", *args]

    def _guard(self, argv: Sequence[str], code: int, err: str) -> None:
        if any(marker in err for marker in _RAISE_MARKERS):
            raise SupervisionError(list(argv), code, err)

    def _is_enabled(self, unit: str = _TIMER_NAME) -> bool:
        argv = self._systemctl("is-enabled", unit)
        code, out, err = self._invoke(argv)
        self._guard(argv, code, err)
        token = out.strip()
        if token in _ENABLED_TRUE:
            return True
        if token in _ENABLED_FALSE or (not token and code != 0):
            return False
        raise SupervisionError(argv, code, err or out)

    def _is_active(self, unit: str = _UNIT_NAME) -> bool:
        argv = self._systemctl("is-active", unit)
        code, out, err = self._invoke(argv)
        self._guard(argv, code, err)
        token = out.strip()
        if token in _ACTIVE_TRUE:
            return True
        if token in _ACTIVE_FALSE:
            return False
        raise SupervisionError(argv, code, err or out)

    def _properties(self, unit: str) -> dict[str, str]:
        """Read lifecycle properties without conflating failures and waiting."""
        argv = self._systemctl(
            "show", unit, "--property=ActiveState,SubState,ActiveEnterTimestampMonotonic"
        )
        code, out, err = self._invoke(argv)
        if code:
            raise SupervisionError(argv, code, err)
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def _login_link(self) -> Path:
        return self.timer_path.parent / f"{_SESSION_TARGET}.wants" / _TIMER_NAME

    def _legacy_link(self) -> Path:
        return self.artifact_path.parent / "default.target.wants" / _UNIT_NAME

    def status(self) -> SupervisionStatus:
        installed = self.artifact_path.is_file() and self.timer_path.is_file()
        enabled = self._is_enabled() and self._login_link().exists()
        enabled = enabled and not self._legacy_link().is_symlink()
        running = self._is_active()
        live = port_live(self._cfg.port)
        waiting = ""
        failure = ""
        if installed and enabled:
            service = self._properties(_UNIT_NAME)
            timer = self._properties(_TIMER_NAME)
            failed = "failed" in (service.get("ActiveState"), timer.get("ActiveState"))
            if failed:
                failure = "Proxy service or GNOME login timer failed"
            else:
                if not running and not self._is_active(_SESSION_TARGET):
                    waiting = "waiting for GNOME login"
                elif not running and timer.get("SubState") == "waiting":
                    waiting = "waiting for the 60-second GNOME login timer"
                elif running and not live:
                    started = int(service.get("ActiveEnterTimestampMonotonic", "0")) / 1e6
                    if started and 0 <= time.monotonic() - started < 30:
                        waiting = "Proxy starting; waiting for its listening port"
        return SupervisionStatus(installed, enabled, running, live, waiting, failure)

    def install(self) -> None:
        """Converge session startup without interrupting a running Proxy."""
        # Disable before replacing the legacy unit's [Install] section; never --now.
        legacy = self._is_enabled(_UNIT_NAME) or self._legacy_link().is_symlink()
        if legacy:
            self._run_checked(self._systemctl("disable", _UNIT_NAME))
        wrote_service = self._write_if_changed(self.artifact_path, self.render())
        wrote_timer = self._write_if_changed(self.timer_path, self.render_timer())
        if wrote_service or wrote_timer or legacy:
            self._run_checked(self._systemctl("daemon-reload"))
        if not self._is_enabled() or not self._login_link().exists():
            self._run_checked(self._systemctl("enable", _TIMER_NAME))
        if self._is_active(_SESSION_TARGET) and not self._is_active(_TIMER_NAME):
            self._run_checked(self._systemctl("start", _TIMER_NAME))

    def update(self) -> None:
        """Repair both artifacts while preserving active service and timer processes."""
        self.install()

    def teardown(self) -> None:
        """Stop the timer first, then remove both units and legacy enablement."""
        for unit in (_TIMER_NAME, _UNIT_NAME):
            self._run_tolerant(self._systemctl("stop", unit))
            self._run_tolerant(self._systemctl("disable", unit))
        self._remove(self.timer_path)
        self._remove(self.artifact_path)
        self._run_checked(self._systemctl("daemon-reload"))


def _systemd_quote(arg: str) -> str:
    """Quote one ExecStart argument: escape ``%`` then backslash/quote, then double-quote.

    systemd treats ``%`` as a specifier lead-in (escaped ``%%``) and honors ``\\`` and
    ``\\"`` inside double quotes, so a path with spaces or metacharacters survives intact.
    """
    escaped = arg.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --------------------------------------------------------------------------- launchd


class _LaunchdManager(_Manager):
    """macOS launchd LaunchAgent manager (per-user GUI domain)."""

    @property
    def artifact_path(self) -> Path:
        """The LaunchAgent plist under ``~/Library/LaunchAgents/``."""
        return Path(os.path.expanduser("~/Library/LaunchAgents")) / f"{_LAUNCHD_LABEL}.plist"

    def render(self) -> bytes:
        """Render the LaunchAgent plist (KeepAlive, RunAtLoad, log paths under log_dir)."""
        plist = {
            "Label": _LAUNCHD_LABEL,
            "ProgramArguments": [self._cfg.python_path, *self._cfg.entry],
            "KeepAlive": True,
            "RunAtLoad": True,
            "StandardOutPath": str(self._cfg.log_dir / "proxy.out.log"),
            "StandardErrorPath": str(self._cfg.log_dir / "proxy.err.log"),
        }
        return plistlib.dumps(plist)

    # -- status primitives -------------------------------------------------------------

    def _uid(self) -> int:
        return os.getuid()

    def _domain(self) -> str:
        return f"gui/{self._uid()}"

    def _target(self) -> str:
        return f"gui/{self._uid()}/{_LAUNCHD_LABEL}"

    def _print(self) -> tuple[bool, bool]:
        """Return ``(loaded, running)`` from ``launchctl print``.

        A non-zero exit means the label is not bootstrapped (loaded=False); a positive
        ``pid`` line in the output means a running instance.
        """
        argv = ["launchctl", "print", self._target()]
        code, out, _err = self._invoke(argv)
        if code != 0:
            return False, False
        match = re.search(r"pid = (\d+)", out)
        running = bool(match and int(match.group(1)) > 0)
        return True, running

    def status(self) -> SupervisionStatus:
        loaded, running = self._print()
        return SupervisionStatus(
            installed=self.artifact_path.exists(),
            enabled=loaded,
            running=running,
            port_live=port_live(self._cfg.port),
        )

    # -- converge operations -----------------------------------------------------------

    def install(self) -> None:
        """Write the plist and converge to loaded + running (bootstrap/kickstart)."""
        # launchd never creates the StandardOut/ErrPath parents; a missing log_dir makes
        # the job KeepAlive restart-loop, so create it before the plist is written.
        self._cfg.log_dir.mkdir(parents=True, exist_ok=True)
        self._write_if_changed(self.artifact_path, self.render())
        loaded, running = self._print()
        if not loaded:
            self._run_checked(
                ["launchctl", "bootstrap", self._domain(), str(self.artifact_path)]
            )
        elif not running:
            self._run_checked(["launchctl", "kickstart", self._target()])

    def update(self) -> None:
        """Apply-and-restart: rewrite the plist, bootout if loaded, then bootstrap."""
        self._cfg.log_dir.mkdir(parents=True, exist_ok=True)  # see install(): log paths
        self._write_if_changed(self.artifact_path, self.render())
        loaded, _running = self._print()
        if loaded:
            self._run_checked(["launchctl", "bootout", self._target()])
        self._run_checked(
            ["launchctl", "bootstrap", self._domain(), str(self.artifact_path)]
        )

    def teardown(self) -> None:
        """Converge to absent: bootout if loaded, then remove the plist."""
        loaded, _running = self._print()
        if loaded:
            self._run_tolerant(["launchctl", "bootout", self._target()])
        self._remove(self.artifact_path)


# --------------------------------------------------------------------------- WinSW


def _default_winsw_download(url: str) -> bytes:
    """Download the pinned WinSW release and return its bytes (the production fetch seam)."""
    with urllib.request.urlopen(url, timeout=_WINSW_DOWNLOAD_TIMEOUT) as response:
        return response.read()


def _sha256_hex(data: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def ensure_winsw_exe(
    service_dir: str | os.PathLike[str],
    *,
    override_path: str | os.PathLike[str] | None = None,
    downloader: Callable[[str], bytes] = _default_winsw_download,
    hasher: Callable[[bytes], str] = _sha256_hex,
) -> Path:
    """Resolve the WinSW executable for the Windows service (idempotent, fails CLOSED).

    Resolution order:

    1. ``override_path`` — an operator-supplied WinSW exe (offline/custom hosts). It must be
       an existing file, else :class:`SupervisionError`; returned unchanged, no download.
    2. An exe already at ``<service_dir>/chinamaxM-service.exe`` — a prior install, returned
       unchanged and never re-downloaded.
    3. Otherwise download the pinned official winsw release from :data:`WINSW_DOWNLOAD_URL`,
       verify its SHA-256 against :data:`WINSW_SHA256`, and place it atomically at
       ``<service_dir>/chinamaxM-service.exe`` (WinSW bundled mode requires the exe basename
       to match the XML basename ``chinamaxM-service``). A checksum MISMATCH raises and
       writes NO exe — an unverified service binary is never installed.

    ``downloader`` (``url -> bytes``) and ``hasher`` (``bytes -> hex sha256``) are injectable
    so tests never touch the network.

    Args:
        service_dir: The WinSW service directory (``<log_dir>/service``).
        override_path: An operator-supplied WinSW exe that wins over auto-acquisition.
        downloader: The fetch seam; defaults to a urllib download of the pinned URL.
        hasher: The digest seam; defaults to SHA-256.

    Returns:
        The path to the resolved WinSW executable.

    Raises:
        SupervisionError: On a missing override, a download/transport failure, or a SHA-256
            mismatch (fail closed — no exe is placed).
    """
    if override_path is not None:
        path = Path(override_path)
        if not path.is_file():
            raise SupervisionError(
                message=f"--winsw-exe path is not an existing file: {str(path)!r}"
            )
        return path

    service_dir = Path(service_dir)
    installed = service_dir / _WINSW_EXE_NAME
    if installed.is_file():
        return installed

    try:
        payload = downloader(WINSW_DOWNLOAD_URL)
    except OSError as exc:  # urllib.error.URLError is an OSError subclass
        raise SupervisionError(
            message=f"failed to download WinSW from {WINSW_DOWNLOAD_URL}: {exc}"
        ) from exc
    digest = hasher(payload)
    if digest != WINSW_SHA256:
        raise SupervisionError(
            message=(
                f"WinSW download checksum mismatch (expected {WINSW_SHA256}, got "
                f"{digest}); refusing to install an unverified service binary"
            )
        )
    _atomic_write(installed, payload)  # temp file + os.replace; never a partial exe
    return installed


class _WinswManager(_Manager):
    """Windows WinSW-wrapped service manager (admin install accepted, ADR 0009)."""

    def _service_dir(self) -> Path:
        return self._cfg.log_dir / "service"

    @property
    def artifact_path(self) -> Path:
        """The WinSW XML under ``<log_dir>/service/`` (installed = this file present)."""
        return self._service_dir() / _WINSW_XML_NAME

    def _exe_path(self) -> Path:
        return self._service_dir() / _WINSW_EXE_NAME

    def render(self) -> bytes:
        """Render the WinSW XML (onfailure=restart, serviceaccount, NEVER a <password>)."""
        service = ET.Element("service")
        ET.SubElement(service, "id").text = _WINSW_SERVICE_ID
        ET.SubElement(service, "name").text = _WINSW_SERVICE_ID
        ET.SubElement(service, "description").text = _DESCRIPTION
        ET.SubElement(service, "executable").text = self._cfg.python_path
        ET.SubElement(service, "arguments").text = _winsw_arguments(self._cfg.entry)
        ET.SubElement(service, "onfailure", {"action": "restart", "delay": "2 sec"})
        ET.SubElement(service, "logpath").text = str(self._cfg.log_dir)
        ET.SubElement(service, "log", {"mode": "roll"})
        account = ET.SubElement(service, "serviceaccount")
        ET.SubElement(account, "username").text = getpass.getuser()
        ET.SubElement(account, "allowservicelogon").text = "true"
        body = ET.tostring(service, encoding="unicode")
        return ('<?xml version="1.0" encoding="utf-8"?>\n' + body).encode("utf-8")

    # -- status primitives -------------------------------------------------------------

    def _sc(self, *args: str) -> list[str]:
        return ["sc.exe", *args, _WINSW_SERVICE_ID]

    def _query_state(self) -> tuple[bool, bool]:
        """Return ``(registered, running)`` from ``sc.exe query``.

        Exit 1060 means the service is not installed; any other non-zero exit raises.
        """
        argv = self._sc("query")
        code, out, err = self._invoke(argv)
        if code == _SC_NOT_INSTALLED:
            return False, False
        if code != 0:
            raise SupervisionError(argv, code, err)
        return True, "RUNNING" in out

    def _enabled(self) -> bool:
        """Whether ``sc.exe qc`` reports a start type other than DISABLED."""
        argv = self._sc("qc")
        code, out, err = self._invoke(argv)
        if code == _SC_NOT_INSTALLED:
            return False
        if code != 0:
            raise SupervisionError(argv, code, err)
        return "DISABLED" not in out

    def status(self) -> SupervisionStatus:
        _registered, running = self._query_state()
        return SupervisionStatus(
            installed=self.artifact_path.exists(),
            enabled=self._enabled(),
            running=running,
            port_live=port_live(self._cfg.port),
        )

    # -- converge operations -----------------------------------------------------------

    def _require_winsw_exe(self) -> Path:
        if self._cfg.winsw_exe_path is None:
            raise SupervisionError(
                message="winsw_exe_path is required to install/update the Windows service"
            )
        return self._cfg.winsw_exe_path

    def _write_artifacts(self) -> None:
        """Write the XML and copy the WinSW exe beside it (basenames must match)."""
        source = self._require_winsw_exe()
        self._write_if_changed(self.artifact_path, self.render())
        self._write_if_changed(self._exe_path(), source.read_bytes())

    def _config_password(self) -> None:
        """Apply the in-memory service password via ``sc.exe config`` (never serialized)."""
        if self._cfg.service_password is not None:
            self._run_checked(
                ["sc.exe", "config", _WINSW_SERVICE_ID, "password=", self._cfg.service_password]
            )

    def install(self) -> None:
        """Write artifacts and converge to installed + running (install/config/start)."""
        self._write_artifacts()
        registered, running = self._query_state()
        exe = str(self._exe_path())
        if not registered:
            self._run_checked([exe, "install"])
            self._config_password()
            self._run_checked([exe, "start"])
        elif not running:
            self._run_checked([exe, "start"])

    def update(self) -> None:
        """Apply-and-restart: stop+uninstall (locks the exe) BEFORE replacing, then reinstall."""
        exe = str(self._exe_path())
        registered, _running = self._query_state()
        if registered:
            self._run_tolerant([exe, "stop"])
            self._run_tolerant([exe, "uninstall"])
        self._write_artifacts()
        self._run_checked([exe, "install"])
        self._config_password()
        self._run_checked([exe, "start"])

    def teardown(self) -> None:
        """Converge to absent: stop+uninstall (or sc.exe delete when the XML is gone)."""
        exe = str(self._exe_path())
        registered, _running = self._query_state()
        if self.artifact_path.exists():
            if registered:
                self._run_tolerant([exe, "stop"])
                self._run_tolerant([exe, "uninstall"])
        elif registered:
            self._run_tolerant(["sc.exe", "delete", _WINSW_SERVICE_ID])
        self._remove(self.artifact_path)
        self._remove(self._exe_path())


def _winsw_arguments(entry: Sequence[str]) -> str:
    """Join entry tokens into a WinSW ``<arguments>`` string, quoting tokens with spaces."""
    return " ".join(f'"{token}"' if " " in token else token for token in entry)


# --------------------------------------------------------------------------- dispatch


_MANAGERS: dict[str, type[_Manager]] = {
    "linux": _SystemdManager,
    "darwin": _LaunchdManager,
    "windows": _WinswManager,
}


def _make_manager(
    cfg: SupervisionConfig, platform: str | None, runner: Runner | None
) -> _Manager:
    """Build the manager for the resolved platform with the injected or default runner."""
    key = _resolve_platform(platform)
    return _MANAGERS[key](cfg, runner or _default_runner)


def install(
    cfg: SupervisionConfig, *, platform: str | None = None, runner: Runner | None = None
) -> None:
    """Converge: write the unit artifact and reconcile manager state to enabled + running."""
    _make_manager(cfg, platform, runner).install()


def update(
    cfg: SupervisionConfig, *, platform: str | None = None, runner: Runner | None = None
) -> None:
    """Update artifacts; Linux preserves active processes, other OSes restart."""
    _make_manager(cfg, platform, runner).update()


def teardown(
    cfg: SupervisionConfig, *, platform: str | None = None, runner: Runner | None = None
) -> None:
    """Converge to absent: stop/disable/unload and remove the artifact in reverse order."""
    _make_manager(cfg, platform, runner).teardown()


def status(
    cfg: SupervisionConfig, *, platform: str | None = None, runner: Runner | None = None
) -> SupervisionStatus:
    """Return the four independent status primitives for the resolved platform."""
    return _make_manager(cfg, platform, runner).status()


def render(cfg: SupervisionConfig, *, platform: str | None = None) -> RenderedArtifact:
    """Render the unit artifact (install path + bytes) without touching the manager."""
    manager = _make_manager(cfg, platform, None)
    companions = ()
    if isinstance(manager, _SystemdManager):
        companions = (RenderedArtifact(manager.timer_path, manager.render_timer()),)
    return RenderedArtifact(manager.artifact_path, manager.render(), companions)
