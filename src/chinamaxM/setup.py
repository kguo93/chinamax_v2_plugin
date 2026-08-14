"""The single mutating surface: consent-gated ``diagnose → plan → approve → apply →
re-diagnose → report`` (ADR 0005), plus a teardown mode through the same gate.

The engine COMPOSES the machinery of the blocking slices — the hosts-04 doctor engine
(diagnose / re-diagnose), the hosts-01 generators, the proxy-02 Key-file scaffold, the
ops-01 supervision functions, and the :mod:`chinamaxM.settings_json` env-flip editor — and
mutates nothing until an operator approves the rendered plan by its digest.

Bootstrap: this module and its diagnose/plan phases run under the HOST's ambient
interpreter (the ``chinamaxM`` conda env is an apply TARGET, never a prerequisite), so it
imports nothing that needs the env's dependencies (aiohttp / litellm / PyYAML / tomlkit) at
module load. Agent generation, which needs PyYAML + tomlkit, runs as a subprocess under the
conda env (:func:`_run_generation` re-entered via ``--_run-generation``); tests inject an
in-process generator instead.

No key value can reach ANY output — the exclusion is STRUCTURAL, not a scrub: the engine
never opens a Key file for its values (only :func:`scaffold_key_file` writes names-only), it
sends no key on a probe (the Proxy injects keys), and probe failures render only the parsed
error ``type``/``message`` (or a byte count), never a raw body.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import platform as platform_mod
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from chinamaxM import settings_json

# NOTE (Bootstrap): every OTHER chinamaxM import is LAZY — performed inside the method or
# function that uses it — so ``import chinamaxM.setup`` and the ``--plan-only`` plan/render
# path run under a bare ambient interpreter that lacks the env-only deps (aiohttp / litellm /
# PyYAML / tomlkit). settings_json is stdlib-only and safe to import at module load.

#: The conda env Setup creates/targets and the Python it pins (ADR 0009 / ADR 0002).
CONDA_ENV = "chinamaxM"
PY_VERSION = "3.12"
_LITELLM_PIN = "litellm[proxy]==1.96.2"

#: Miniconda Rectification source — ``latest`` over HTTPS, NO version pin and NO checksum
#: (ADR 0009 as amended 2026-08-14). The engine NEVER downloads or runs an installer: these
#: feed the agent-run miniconda Rectification ROW the Host executes on approval (ported from
#: the old plugin's ``doctor``). Inherits the old plugin's accepted no-pin/no-checksum
#: tradeoff verbatim — a deliberate divergence from this repo's pinned + SHA-256 WinSW
#: acquisition. One installer per (Platform, normalized arch); Windows always uses x86_64.
_MINICONDA_URL_BASE = "https://repo.anaconda.com/miniconda/"
#: platform.machine() normalization (matched on ``machine.lower()``): any value absent from
#: the Platform's map is an unsupported architecture → an advice-only row (never a 404-bound
#: URL). Windows never fails on arch.
_LINUX_MINICONDA_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
_DARWIN_MINICONDA_ARCH = {"arm64": "arm64", "x86_64": "x86_64"}

# ── Git for Windows Rectification source ────────────────────────────────────────────────
# Codex hooks need Git Bash on Windows, so setup bootstraps Git for Windows too. Git for
# Windows scatters these across its install tree and, with the recommended installer option,
# leaves bash/cygpath OFF PATH — so the status probe checks the install roots on disk ONLY,
# never PATH. A ``bash`` that IS on PATH is almost certainly NOT Git Bash: Windows 10+ ships
# WSL's ``System32\bash.exe`` on PATH, and the Codex ``commandWindows`` shim
# (``scripts/codex_hook_bash.cmd``) runs the hooks under Git Bash SPECIFICALLY — so the tool
# the doctor checks for MUST be the exact one the shim resolves (the standard-root Git Bash),
# or the doctor would green-light a WSL bash the hooks cannot use. Roots: system-wide
# %ProgramFiles%\Git, per-user %LocalAppData%\Programs\Git.
_GIT_FOR_WINDOWS_URL = "https://git-scm.com/download/win"

_GIT_FOR_WINDOWS_EXES: dict[str, tuple[str, ...]] = {
    "git": ("cmd/git.exe", "bin/git.exe", "mingw64/bin/git.exe"),
    "bash": ("bin/bash.exe", "usr/bin/bash.exe"),
    "cygpath": ("usr/bin/cygpath.exe",),
}

# ── Linux bash Rectification ────────────────────────────────────────────────────────────
# First package manager on PATH wins; each row installs bash and runs only when passwordless
# sudo works (the skill's ``sudo -n true`` gate), else operator-run.
_LINUX_BASH_PACKAGE_MANAGERS = (
    ("apt-get", "sudo apt-get install -y bash"),
    ("dnf", "sudo dnf install -y bash"),
    ("yum", "sudo yum install -y bash"),
    ("pacman", "sudo pacman -S --noconfirm bash"),
    ("zypper", "sudo zypper install -y bash"),
    ("apk", "sudo apk add bash"),
)

# The winget-absent Windows Git fallback: fail-loud so stop-on-first-failure can catch an
# installer failure. ``$ErrorActionPreference='Stop'`` aborts on a download error, and
# ``Start-Process -Wait -PassThru`` + ``exit $p.ExitCode`` propagates the installer's own
# exit code (``Start-Process -Wait`` alone does not). One raw string: the line carries both
# ``"`` and ``'`` plus native ``\`` path separators.
_WINDOWS_GIT_POWERSHELL_FALLBACK = r'''powershell -NoProfile -Command "$ErrorActionPreference='Stop';$a=(Invoke-RestMethod https://api.github.com/repos/git-for-windows/git/releases/latest).assets|Where-Object name -like '*64-bit.exe'|Select-Object -First 1;Invoke-WebRequest $a.browser_download_url -OutFile $env:TEMP\chinamaxM-git.exe;$p=Start-Process -Wait -PassThru $env:TEMP\chinamaxM-git.exe -ArgumentList '/VERYSILENT','/NORESTART','/CURRENTUSER';exit $p.ExitCode"'''

#: The Proxy service entry (argv after the env Python) — ADR 0009.
PROXY_ENTRY = ("-m", "chinamaxM.proxy")

#: Restart notice emitted after (re)generating agents (ADR 0004 pickup rule).
_RESTART_INSTRUCTION = (
    "Restart any open Claude Code / Codex sessions so the regenerated Worker agents are "
    "picked up."
)

# Subprocess timeouts (seconds).
_CONDA_TIMEOUT = 120.0
_CONDA_CREATE_TIMEOUT = 900.0
_PIP_TIMEOUT = 900.0
_GENERATE_TIMEOUT = 180.0
_CODEX_TIMEOUT = 60.0

# Readiness poll (a cold litellm import routinely exceeds 5s — deadline 30s, backoff).
_READINESS_DEADLINE = 30.0
_READINESS_INITIAL_DELAY = 0.5
_READINESS_MAX_DELAY = 3.0

# Live-probe timeouts and the bounded failure-body read.
_PROBE_CONNECT_TIMEOUT = 10.0
_PROBE_TOTAL_TIMEOUT = 30.0
_PROBE_MAX_READ = 4096


class SetupError(Exception):
    """Raised on a runner guard violation or a failed apply operation."""


# --------------------------------------------------------------------------- data types


@dataclass
class ProbeResponse:
    """One probe's raw HTTP outcome (never a Key value — the Proxy holds keys)."""

    status: int
    body: bytes


@dataclass
class ProbeResult:
    """A per-Profile per-ingress probe verdict for the report."""

    profile: str
    ingress: str
    ok: bool
    status: int | None
    detail: str
    usage: dict | None


@dataclass
class StepResult:
    """The outcome of one applied (or skipped/aborted) plan step."""

    id: str
    status: str  # "ok" / "failed" / "skipped" / "aborted"
    detail: str


@dataclass
class PlanStep:
    """One structured plan step. ``descriptor`` (not the rendered prose) feeds the digest."""

    id: str
    kind: str  # "mutating" / "diagnostic"
    action: str
    title: str
    targets: list[str]
    descriptor: dict
    run: Callable[["SetupEngine"], str] | None = None
    gate_fail: bool = False
    gate_detail: str = ""


@dataclass
class Plan:
    """A rendered plan: its diagnosis, structured steps, preconditions, and digest.

    On the Phase-A prerequisite pause (ADR 0009 as amended 2026-08-14) ``prerequisite_fixes``
    carries the agent-run Rectification rows and ``steps``/``digest`` stay empty — the mutating
    plan is only ever emitted once every Platform Prerequisite is present.
    """

    kind: str
    preconditions: dict
    before_findings: list
    steps: list[PlanStep]
    digest: str
    registry_error: str | None = None
    prerequisite_fixes: list = field(default_factory=list)


@dataclass
class Report:
    """The final report: before/after findings, step outcomes, probes, restart notice."""

    kind: str
    exit_code: int
    rejected: str | None = None
    before_findings: list = field(default_factory=list)
    step_results: list = field(default_factory=list)
    after_findings: list = field(default_factory=list)
    probe_results: list = field(default_factory=list)
    probes_skipped: str | None = None
    restart_instruction: str | None = None


# --------------------------------------------------------------------------- runner


class _Runner:
    """The ONE execution abstraction; the digest-bound approval is enforced HERE.

    Every operation is labeled ``diagnostic`` or ``mutating``. A ``mutating`` operation
    invoked before :meth:`approve` HARD-ERRORS (before the action runs and before it is even
    recorded), so the diagnose and plan phases can only ever run diagnostic ops and create,
    write, or delete nothing.
    """

    def __init__(self) -> None:
        self._approved = False
        self.record: list[tuple[str, str]] = []

    @property
    def approved(self) -> bool:
        """Whether the operator has approved the plan (mutating ops are then permitted)."""
        return self._approved

    def approve(self) -> None:
        """Permit mutating operations from here on."""
        self._approved = True

    def run(self, kind: str, label: str, action: Callable[[], object]) -> object:
        """Run one labeled operation, hard-erroring on a pre-approval mutating op."""
        if kind not in ("diagnostic", "mutating"):
            raise SetupError(f"unknown operation kind {kind!r}")
        if kind == "mutating" and not self._approved:
            raise SetupError(f"mutating operation {label!r} invoked before approval")
        self.record.append((kind, label))
        return action()


# --------------------------------------------------------------------------- default seams


def _default_run(
    argv: list[str], *, timeout: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run an external command with stdin closed and a bounded timeout (doctor's shape)."""
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


class _SubprocessConda:
    """The default conda seam: every conda action goes through the injected runner.

    ``conda`` is resolved by ABSOLUTE PATH (``~/miniconda3`` first, then ``shutil.which``) so a
    just-bootstrapped ``~/miniconda3`` is picked up within the same apply pass and a pre-existing
    anaconda/miniforge conda on PATH still counts. Not memoized — post-bootstrap resolution must
    be able to flip within one process (ported from the old plugin's ``doctor._find_conda``).
    """

    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess[str]],
        *,
        home: Path,
        platform: str,
    ) -> None:
        self._run = run
        self._home = home
        self._platform = platform

    def _conda_bin(self) -> str | None:
        """Absolute conda launcher (``~/miniconda3`` first, PATH fallback); None when absent."""
        base = self._home / "miniconda3"
        if self._platform.startswith("win"):
            candidates = [base / "Scripts" / "conda.exe", base / "condabin" / "conda.bat"]
        else:
            candidates = [base / "bin" / "conda"]
        for candidate in candidates:
            if self._is_conda_executable(candidate):
                return str(candidate)
        return shutil.which("conda")

    def _is_conda_executable(self, path: Path) -> bool:
        """Whether ``path`` is an existing executable, keyed on the injected Platform."""
        if not path.is_file():
            return False
        if self._platform.startswith("win"):
            return path.suffix.lower() in {".exe", ".bat", ".cmd"}
        return os.access(path, os.X_OK)

    def _try(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str] | None:
        try:
            return self._run(argv, timeout=timeout)
        except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
            return None

    def available(self) -> bool:
        """Whether ``conda`` is resolvable (``~/miniconda3`` first, then PATH)."""
        return self._conda_bin() is not None

    def env_exists(self) -> bool:
        """Whether the ``chinamaxM`` conda env exists."""
        cp = self._try([self._conda_bin() or "conda", "env", "list", "--json"], timeout=_CONDA_TIMEOUT)
        if not cp or cp.returncode != 0:
            return False
        try:
            data = json.loads(cp.stdout)
        except ValueError:
            return False
        return any(Path(p).name == CONDA_ENV for p in data.get("envs", []))

    def env_python_version(self) -> str | None:
        """The env's ``major.minor`` Python version, or ``None`` when unresolvable."""
        cp = self._try(
            [self._conda_bin() or "conda", "run", "-n", CONDA_ENV, "python", "-c",
             "import sys;print('%d.%d' % sys.version_info[:2])"],
            timeout=_CONDA_TIMEOUT,
        )
        if not cp or cp.returncode != 0:
            return None
        lines = cp.stdout.strip().splitlines()
        return lines[-1].strip() if lines else None

    def env_python_path(self) -> str:
        """The env's Python executable path (the service ExecStart)."""
        cp = self._try(
            [self._conda_bin() or "conda", "run", "-n", CONDA_ENV, "python", "-c", "import sys;print(sys.executable)"],
            timeout=_CONDA_TIMEOUT,
        )
        if not cp or cp.returncode != 0:
            raise SetupError(f"could not resolve the {CONDA_ENV} env Python")
        lines = cp.stdout.strip().splitlines()
        if not lines:
            raise SetupError(f"could not resolve the {CONDA_ENV} env Python")
        return lines[-1].strip()

    def create(self) -> None:
        """Create the env at the pinned Python (only ever called when it is absent)."""
        cp = self._run(
            [self._conda_bin() or "conda", "create", "-y", "-n", CONDA_ENV, f"python={PY_VERSION}"],
            timeout=_CONDA_CREATE_TIMEOUT,
        )
        if cp.returncode != 0:
            raise SetupError(f"conda create failed (exit {cp.returncode})")

    def pip_install(self, plugin_root: Path) -> None:
        """Editable-install the plugin (pulling aiohttp + the pinned litellm from pyproject)."""
        cp = self._run(
            [self._conda_bin() or "conda", "run", "-n", CONDA_ENV, "pip", "install", "-e", str(plugin_root)],
            timeout=_PIP_TIMEOUT,
        )
        if cp.returncode != 0:
            raise SetupError(f"pip install failed (exit {cp.returncode})")


def _default_http(
    url: str, body: dict, *, connect_timeout: float, total_timeout: float
) -> ProbeResponse:
    """POST ``body`` as JSON with stdlib urllib (no aiohttp — the engine runs pre-env)."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"content-type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=total_timeout) as response:
            return ProbeResponse(response.status, response.read(_PROBE_MAX_READ + 1))
    except urllib.error.HTTPError as exc:
        payload = exc.read(_PROBE_MAX_READ + 1) if exc.fp is not None else b""
        return ProbeResponse(exc.code, payload)
    except Exception:  # noqa: BLE001 - a connection failure is a probe failure, never a crash
        return ProbeResponse(0, b"")


def _plugin_checkout_root() -> Path:
    """Resolve the plugin checkout root from this package's own ``__file__`` (Bootstrap)."""
    return Path(__file__).resolve().parents[2]


# ----------------------------------------------------------- prerequisite detection helpers
# Ported from the old plugin's ``doctor`` (the engine only DETECTS prerequisites; the Host
# agent runs the emitted Rectification rows on approval — ADR 0009 as amended 2026-08-14).


def _is_executable(path: str, platform: str) -> bool:
    """Report whether a path is an absolute, existing, executable file.

    On Windows the check is by ``.exe``/``.bat``/``.cmd`` suffix (a POSIX test host has no
    exec bit on a Windows binary); elsewhere it is the POSIX ``X_OK`` bit. ``platform`` is the
    injected engine Platform, so the probe never reads this process's real ``sys.platform``.
    """
    if not path:
        return False
    is_win = platform.startswith("win")
    absolute = ntpath.isabs(path) if is_win else os.path.isabs(path)
    if not absolute or not os.path.isfile(path):
        return False
    if is_win:
        return Path(path).suffix.lower() in {".exe", ".bat", ".cmd"}
    return os.access(path, os.X_OK)


def _git_for_windows_roots() -> list[Path]:
    """Default Git for Windows install roots: system-wide, then per-user."""
    roots: list[Path] = []
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(var, "").strip()
        if base:
            roots.append(Path(base) / "Git")
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        roots.append(Path(local) / "Programs" / "Git")
    # %ProgramW6432% usually aliases %ProgramFiles% on 64-bit Windows.
    return list(dict.fromkeys(roots))


def _windows_tool_present(name: str, platform: str) -> bool:
    """True only when the tool resolves inside a Git for Windows install root — no PATH fallback.

    There is deliberately NO ``shutil.which`` PATH fallback: Git for Windows leaves
    ``bash``/``cygpath`` off PATH by default, so a ``bash`` on PATH is almost certainly WSL's
    ``System32\\bash.exe`` (on PATH on Windows 10+), NOT the Git Bash the Codex
    ``commandWindows`` shim runs. Accepting a PATH bash here would report the requirement met
    while the hooks ran under the wrong bash. Detection therefore matches exactly what
    ``scripts/codex_hook_bash.cmd`` resolves: the four standard Git for Windows roots only.
    """
    return any(
        _is_executable(str(root / rel), platform)
        for root in _git_for_windows_roots()
        for rel in _GIT_FOR_WINDOWS_EXES[name]
    )


# --------------------------------------------------------------------- generation seam


def _run_generation(registry, roots: Mapping[str, Path], *, include_codex: bool) -> dict:
    """Generate Worker agents — the SINGLE generation implementation (lazy generate import).

    With ``include_codex`` the full hosts-01 :func:`regenerate` runs (Claude + Codex);
    without it a Claude-only pass reuses hosts-01's ``expected_artifacts`` + marker rules so a
    Claude-only machine's ``~/.codex`` is never fabricated (ADR 0004/0006).
    """
    from chinamaxM import generate  # lazy: needs the env's PyYAML + tomlkit

    if include_codex:
        return generate.regenerate(registry, roots)

    # Reuse hosts-01's own read/marker/atomic-write helpers so this Claude-only branch
    # stays byte-for-byte identical to regenerate's Claude loop (marker classification,
    # byte-identical skip, mode-preserving atomic write, OSError-swallowing read).
    claude_dir = Path(roots["claude"]) / "agents"
    expected = generate.expected_artifacts(registry, roots)
    claude_expected = {p: c for p, c in expected.items() if p.parent == claude_dir}
    written: list[str] = []
    skipped: list[str] = []
    pruned: list[str] = []
    conflicts: list[str] = []
    for path, content in claude_expected.items():
        current = generate._read_text(path) if path.exists() else None
        if current is not None and not generate._has_marker(current):
            conflicts.append(str(path))
            continue
        if current == content:
            skipped.append(str(path))
            continue
        generate._atomic_write(path, content.encode("utf-8"))
        written.append(str(path))
    if claude_dir.is_dir():
        for entry in sorted(claude_dir.iterdir()):
            if not entry.is_file() or entry in claude_expected:
                continue
            text = generate._read_text(entry)
            if text is not None and generate._has_marker(text):
                entry.unlink()
                pruned.append(str(entry))
    return {
        "written": sorted(written),
        "skipped": sorted(skipped),
        "pruned": sorted(pruned),
        "conflicts": sorted(conflicts),
    }


def _generation_subprocess_main() -> int:
    """The ``--_run-generation`` entry: run generation under the env, print a JSON report."""
    from chinamaxM.generate import resolve_roots
    from chinamaxM.registry import default_overlay_path, load_registry

    include_codex = os.environ.get("CHINAMAXM_SETUP_INCLUDE_CODEX") == "1"
    try:
        registry = load_registry(overlay_path=default_overlay_path())
        roots = resolve_roots()
        report = _run_generation(registry, roots, include_codex=include_codex)
    except Exception as exc:  # noqa: BLE001 - report failure to the parent, never traceback
        sys.stderr.write(f"generation failed: {type(exc).__name__}: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(report) + "\n")
    return 0


# --------------------------------------------------------------------------- engine


class SetupEngine:
    """The consent-gated setup/teardown engine. Every root and command is injectable."""

    def __init__(
        self,
        *,
        claude_root: str | os.PathLike[str] | None = None,
        codex_root: str | os.PathLike[str] | None = None,
        overlay_path: str | os.PathLike[str] | None = None,
        plugin_root: str | os.PathLike[str] | None = None,
        run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        diagnose: Callable[[], list] | None = None,
        prerequisites: Callable[[], "dict[str, bool]"] | None = None,
        generate_fn: Callable[[object, Mapping[str, Path], bool], dict] | None = None,
        conda: object | None = None,
        service_status: Callable[[object], object] | None = None,
        service_install: Callable[[object], None] | None = None,
        service_update: Callable[[object], None] | None = None,
        service_teardown: Callable[[object], None] | None = None,
        service_render: Callable[[object], object] | None = None,
        port_live: Callable[[int], bool] | None = None,
        enable_linger: Callable[[], None] | None = None,
        linger_enabled: Callable[[], bool] | None = None,
        http: Callable[..., ProbeResponse] | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], float] | None = None,
        platform: str | None = None,
        home: str | os.PathLike[str] | None = None,
        machine: str | None = None,
        winsw_exe: str | os.PathLike[str] | None = None,
        service_password: str | None = None,
        ensure_winsw: Callable[..., object] | None = None,
    ) -> None:
        # Bootstrap: import the composed modules lazily (never at module load).
        from chinamaxM.keyfiles import resolve_host_root
        from chinamaxM.observability import resolve_log_path
        from chinamaxM.ops import supervision
        from chinamaxM.registry import load_registry

        self._claude_root = Path(claude_root) if claude_root is not None else resolve_host_root("claude")
        self._codex_root = Path(codex_root) if codex_root is not None else resolve_host_root("codex")
        self._overlay_path = (
            Path(overlay_path)
            if overlay_path is not None
            else self._claude_root / "chinamaxM" / "profiles.json"
        )
        self._plugin_root = Path(plugin_root) if plugin_root is not None else _plugin_checkout_root()
        self._platform = platform or sys.platform
        self._home = Path(home) if home is not None else Path.home()
        self._machine = machine if machine is not None else platform_mod.machine()
        self._settings_path = self._claude_root / "settings.json"
        self._log_dir = resolve_log_path(self._claude_root).parent

        self._run = run or _default_run
        self._conda = conda or _SubprocessConda(self._run, home=self._home, platform=self._platform)
        self._generate_fn = generate_fn or self._subprocess_generate
        self._port_live = port_live or supervision.port_live
        self._enable_linger = enable_linger or (lambda: supervision.enable_linger(platform=self._platform))
        self._linger_enabled = linger_enabled or (lambda: supervision.linger_enabled(platform=self._platform))
        self._service_status = service_status or (lambda cfg: supervision.status(cfg, platform=self._platform))
        self._service_install = service_install or (lambda cfg: supervision.install(cfg, platform=self._platform))
        self._service_update = service_update or (lambda cfg: supervision.update(cfg, platform=self._platform))
        self._service_teardown = service_teardown or (lambda cfg: supervision.teardown(cfg, platform=self._platform))
        self._service_render = service_render or (lambda cfg: supervision.render(cfg, platform=self._platform))
        self._winsw_exe = str(winsw_exe) if winsw_exe is not None else None
        self._service_password = service_password
        self._ensure_winsw = ensure_winsw or (
            lambda service_dir, override_path=None: supervision.ensure_winsw_exe(
                service_dir, override_path=override_path
            )
        )
        self._http = http or _default_http
        self._sleep = sleep or time.sleep
        self._now = now or time.monotonic
        self._diagnose = diagnose or self._default_diagnose
        # Prerequisite detection is injectable (ADR 0011) so the suite can drive each
        # Platform's rows without probing the real host; production uses the live probe.
        self._prerequisites = prerequisites or self._prerequisite_status

        self._runner = _Runner()
        try:
            self._registry = load_registry(overlay_path=self._overlay_path)
            self._registry_error: str | None = None
        except Exception as exc:  # noqa: BLE001 - an unreadable Registry blocks planning cleanly
            self._registry = None
            self._registry_error = f"{type(exc).__name__}: {exc}"

    # -- default seams that need engine state ---------------------------------------

    def _default_diagnose(self) -> list:
        """Run the hosts-04 doctor roster read-only and return its findings."""
        from chinamaxM import doctor

        findings, _exit = doctor.run_doctor(
            claude_root=str(self._claude_root),
            codex_root=str(self._codex_root),
            overlay_path=str(self._overlay_path),
            run=self._run,
            port_probe=self._port_live,
            service_status=self._service_status,
        )
        return findings

    def _subprocess_generate(self, registry, roots: Mapping[str, Path], include_codex: bool) -> dict:
        """Run agent generation under the conda env (re-enter ``--_run-generation``)."""
        env = dict(os.environ)
        env["CHINAMAXM_CLAUDE_HOME"] = str(roots["claude"])
        env["CHINAMAXM_CODEX_HOME"] = str(roots["codex"])
        env["CHINAMAXM_SETUP_INCLUDE_CODEX"] = "1" if include_codex else "0"
        cp = self._run(
            ["conda", "run", "-n", CONDA_ENV, "python", "-m", "chinamaxM.setup", "--_run-generation"],
            timeout=_GENERATE_TIMEOUT,
            env=env,
        )
        if cp.returncode != 0:
            raise SetupError(f"agent generation failed (exit {cp.returncode})")
        lines = cp.stdout.strip().splitlines()
        try:
            return json.loads(lines[-1]) if lines else {}
        except ValueError:
            return {}

    # -- helpers --------------------------------------------------------------------

    def _flip_url(self) -> str:
        return f"http://127.0.0.1:{self._registry.port}"

    def _is_our_flip(self, value: object) -> bool:
        """Whether an ``ANTHROPIC_BASE_URL`` value is the flip Setup itself writes."""
        from chinamaxM import doctor

        if not isinstance(value, str):
            return False
        target = doctor.normalize_flip_url(value)
        return bool(
            target
            and target.scheme == "http"
            and target.is_loopback
            and target.port == self._registry.port
            and not target.path
            and not target.query
        )

    def _status_cfg(self) -> SupervisionConfig:
        """A config for status/render/teardown (its ``python_path`` is never executed)."""
        from chinamaxM.ops.supervision import SupervisionConfig

        return SupervisionConfig(
            python_path=sys.executable, entry=list(PROXY_ENTRY),
            port=self._registry.port, log_dir=self._log_dir,
        )

    def _install_cfg(self, winsw_exe_path: str | os.PathLike[str] | None = None) -> SupervisionConfig:
        """A config for install/update carrying the env Python as the service ExecStart.

        On Windows ``winsw_exe_path`` is the WinSW exe resolved by :func:`ensure_winsw_exe`
        and ``service_password`` (if supplied) rides in-memory only; both are ``None`` on
        Linux/macOS.
        """
        from chinamaxM.ops.supervision import SupervisionConfig

        return SupervisionConfig(
            python_path=self._conda.env_python_path(), entry=list(PROXY_ENTRY),
            port=self._registry.port, log_dir=self._log_dir,
            winsw_exe_path=winsw_exe_path, service_password=self._service_password,
        )

    def _winsw_source(self) -> dict:
        """Classify the WinSW acquisition source for the plan (read-only; NEVER downloads).

        Mirrors :func:`ensure_winsw_exe`'s branch selection without side effects so the plan
        render + digest bind the source ``--apply`` will actually acquire.
        """
        from chinamaxM.ops import supervision

        if self._winsw_exe is not None:
            return {"kind": "supplied", "path": self._winsw_exe,
                    "render": f"supplied: {self._winsw_exe}"}
        service_dir = self._service_artifact_path().parent
        installed = service_dir / supervision._WINSW_EXE_NAME
        if installed.is_file():
            return {"kind": "already-installed", "path": str(installed),
                    "render": f"already installed: {installed}"}
        return {"kind": "download", "url": supervision.WINSW_DOWNLOAD_URL,
                "render": (
                    f"download pinned WinSW v2.12.0 (sha256-verified) from "
                    f"{supervision.WINSW_DOWNLOAD_URL}"
                )}

    def _service_artifact_path(self) -> Path:
        return Path(self._service_render(self._status_cfg()).path)

    def _python_path_file(self) -> Path:
        """The hook shims' rung-2 interpreter record: ``<claude-root>/chinamaxM/python-path``."""
        return self._log_dir / "python-path"

    # -- prerequisite detection + Rectification rows (ADR 0009 as amended 2026-08-14) -------
    # The engine only DETECTS the Platform Prerequisites (bash / Git for Windows / Miniconda)
    # and EMITS agent-run Rectification rows; the Host runs them on approval. The engine NEVER
    # downloads or runs an installer. Ported from the old plugin's ``doctor`` (verbatim command
    # strings), adapted to the engine's injected ``self._platform``/``self._machine`` so the
    # suite drives every Platform hermetically.

    def _prerequisite_status(self) -> dict[str, bool]:
        """External Prerequisite tools setup requires, by Platform.

        Linux and macOS: ``bash`` (on PATH) and ``miniconda`` (conda resolvable via the conda
        seam's ``~/miniconda3``-first ``_conda_bin``). Windows: ``git``/``bash``/``cygpath`` in the
        Git for Windows install tree ONLY (never PATH — a PATH bash is WSL's, not the Git Bash the
        Codex shim runs), followed by ``miniconda``; the Git row must precede the miniconda row
        because the miniconda row's ``conda init ... bash`` needs the bash the Git row provides.
        Off-matrix Platforms return ``{}``.
        """
        if self._platform.startswith("win"):
            status = {name: _windows_tool_present(name, self._platform) for name in _GIT_FOR_WINDOWS_EXES}
            status["miniconda"] = self._conda.available()
            return status
        if self._platform.startswith("darwin"):
            return {"bash": shutil.which("bash") is not None, "miniconda": self._conda.available()}
        if self._platform.startswith("linux"):
            return {"bash": shutil.which("bash") is not None, "miniconda": self._conda.available()}
        return {}

    def _prerequisite_fixes(self, prerequisites: dict[str, bool]) -> list[dict]:
        """Rows for each missing Prerequisite: name, summary, commands, run_policy, shell, install_location.

        Plain dicts (repo convention). ``run_policy`` is ``"agent"`` (the agent runs the
        commands), ``"privileged"`` (run only when ``sudo -n true`` succeeds, else handed to the
        operator), or ``"operator"`` (advice-only, ``commands`` empty). ``shell`` names the
        interpreter the commands are written for so the adapter never guesses: Windows ``"cmd"``
        rows run via ``cmd /c`` and ``"powershell"``/``"native"`` rows run natively — neither is
        ever pasted into Git Bash. Emission order is Prerequisite order: on Windows the Git for
        Windows row precedes the miniconda row (whose ``conda init ... bash`` needs the Git bash).
        """
        missing = [name for name, present in prerequisites.items() if not present]
        if not missing:
            return []
        rows: list[dict] = []
        if self._platform.startswith("win"):
            git_missing = [name for name in ("git", "bash", "cygpath") if name in missing]
            if git_missing:
                rows.append(self._windows_git_row(git_missing))
            if "miniconda" in missing:
                rows.append(self._windows_miniconda_row())
            return rows
        if self._platform.startswith("darwin"):
            if "bash" in missing:
                rows.append(self._darwin_bash_row())
            if "miniconda" in missing:
                rows.append(self._miniconda_posix_row("darwin"))
            return rows
        if self._platform.startswith("linux"):
            if "bash" in missing:
                rows.append(self._linux_bash_row())
            if "miniconda" in missing:
                rows.append(self._miniconda_posix_row("linux"))
            return rows
        return rows

    def _linux_bash_row(self) -> dict:
        """The linux bash Rectification row: first package manager on PATH, else advice."""
        for manager, command in _LINUX_BASH_PACKAGE_MANAGERS:
            if shutil.which(manager):
                return {
                    "name": "bash",
                    "summary": (
                        f"install bash with {manager}. The skill runs it automatically only when "
                        "passwordless sudo works (sudo -n true); otherwise run it yourself."
                    ),
                    "commands": [command],
                    "run_policy": "privileged",
                    "shell": "bash",
                    "install_location": "system package manager",
                }
        return {
            "name": "bash",
            "summary": "install bash with your distribution's package manager",
            "commands": [],
            "run_policy": "operator",
            "shell": "bash",
            "install_location": "system package manager",
        }

    def _darwin_bash_row(self) -> dict:
        """The macOS bash Rectification row: brew install when brew exists, else advice."""
        if shutil.which("brew"):
            return {
                "name": "bash",
                "summary": "install a current bash with brew install bash",
                "commands": ["brew install bash"],
                "run_policy": "agent",
                "shell": "bash",
                "install_location": "Homebrew",
            }
        return {
            "name": "bash",
            "summary": "install Homebrew or the Xcode Command Line Tools, then brew install bash",
            "commands": [],
            "run_policy": "operator",
            "shell": "bash",
            "install_location": "Homebrew",
        }

    def _windows_git_row(self, missing_tools: list[str]) -> dict:
        """The single deduped Git for Windows Rectification row (the git/bash/cygpath trio).

        winget present → one ``winget install --id Git.Git`` line (one UAC click); winget absent
        → the fail-loud per-user PowerShell fallback, whose summary names
        ``https://git-scm.com/download/win`` as the manual alternative (GitHub API rate-limiting
        can break the automated line). Both run natively, never Git Bash.
        """
        if shutil.which("winget"):
            return {
                "name": "Git for Windows",
                "missing_tools": missing_tools,
                "summary": (
                    "install Git for Windows with winget (one UAC consent click); provides "
                    "git, bash, and cygpath. Run natively, not in Git Bash."
                ),
                "commands": [
                    "winget install --id Git.Git -e --silent "
                    "--accept-source-agreements --accept-package-agreements"
                ],
                "run_policy": "agent",
                "shell": "native",
                "install_location": r"Program Files\Git",
            }
        return {
            "name": "Git for Windows",
            "missing_tools": missing_tools,
            "summary": (
                "install Git for Windows per-user via the PowerShell fallback (winget "
                f"absent); if the GitHub API is rate-limited or blocked, install manually "
                f"from {_GIT_FOR_WINDOWS_URL}. Provides git, bash, and cygpath. Run "
                "natively, not in Git Bash."
            ),
            "commands": [_WINDOWS_GIT_POWERSHELL_FALLBACK],
            "run_policy": "agent",
            "shell": "powershell",
            "install_location": r"%LocalAppData%\Programs\Git",
        }

    def _windows_miniconda_row(self) -> dict:
        """The Windows miniconda Rectification row (cmd.exe syntax; run via ``cmd /c``).

        The JustMe installer reuses an existing per-user install, so a re-run is safe. Known
        live-Windows risk (mocked-only): ``start /wait`` does not reliably propagate the
        installer's exit code to ``%ERRORLEVEL%`` on all Windows versions, so
        stop-on-first-failure may not catch a failed silent install.
        """
        commands = [
            r'curl.exe -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe -o "%TEMP%\chinamaxM-miniconda.exe"',
            r'start /wait "" "%TEMP%\chinamaxM-miniconda.exe" /InstallationType=JustMe /RegisterPython=0 /AddToPath=0 /S /D=%USERPROFILE%\miniconda3',
            r'"%USERPROFILE%\miniconda3\Scripts\conda.exe" init cmd.exe powershell bash',
        ]
        return {
            "name": "miniconda",
            "summary": (
                "install Miniconda into %USERPROFILE%\\miniconda3 with curl.exe (cmd.exe "
                "syntax; run each line via cmd /c, never Git Bash). The JustMe installer "
                "reuses an existing per-user install. The final command runs conda init, "
                "which EDITS your cmd/powershell profiles and registry. Requires curl.exe."
            ),
            "commands": commands,
            "run_policy": "agent",
            "shell": "cmd",
            "install_location": r"%USERPROFILE%\miniconda3",
        }

    def _miniconda_posix_row(self, platform_kind: str) -> dict:
        """The linux/macOS miniconda Rectification row (bash syntax), or advice on an
        unsupported architecture.

        ``-b -u`` reuses an existing/partial ``$HOME/miniconda3`` so a re-run after an
        interrupted install is idempotent. The final command runs conda init, which MODIFIES the
        operator's shell startup files. curl is the row's dependency. An architecture absent from
        the normalization map yields an advice-only row with an empty ``commands`` and no
        download URL (never a 404-bound URL).
        """
        machine = self._machine.lower()
        if platform_kind == "darwin":
            arch = _DARWIN_MINICONDA_ARCH.get(machine)
            os_tag, init_shells, startup_files = "MacOSX", "bash zsh", "~/.bashrc and ~/.zshrc"
        else:
            arch = _LINUX_MINICONDA_ARCH.get(machine)
            os_tag, init_shells, startup_files = "Linux", "bash", "~/.bashrc"
        if arch is None:
            return {
                "name": "miniconda",
                "summary": (
                    f"unsupported CPU architecture {self._machine!r}; install Miniconda "
                    f"manually from {_MINICONDA_URL_BASE}, then re-run setup"
                ),
                "commands": [],
                "run_policy": "operator",
                "shell": "bash",
                "install_location": "$HOME/miniconda3",
            }
        installer = f"Miniconda3-latest-{os_tag}-{arch}.sh"
        url = f"{_MINICONDA_URL_BASE}{installer}"
        commands = [
            f'curl -fsSL {url} -o "$HOME/.chinamaxM-miniconda.sh"',
            'bash "$HOME/.chinamaxM-miniconda.sh" -b -u -p "$HOME/miniconda3"',
            f'"$HOME/miniconda3/bin/conda" init {init_shells}',
        ]
        return {
            "name": "miniconda",
            "summary": (
                f"install Miniconda ({installer}) into $HOME/miniconda3 with curl; -b -u "
                "reuses an existing $HOME/miniconda3 so a re-run is idempotent. The final "
                f"command runs conda init {init_shells}, which MODIFIES your shell startup "
                f"files ({startup_files}). Requires curl."
            ),
            "commands": commands,
            "run_policy": "agent",
            "shell": "bash",
            "install_location": "$HOME/miniconda3",
        }

    def _read_settings_flip_state(self) -> list:
        try:
            value = settings_json.read_flip(self._settings_path)
        except settings_json.SettingsError:
            return ["unparseable"]
        return ["absent"] if value is None else ["present", value]

    def _service_state(self) -> list | None:
        try:
            st = self._service_status(self._status_cfg())
        except Exception:  # noqa: BLE001 - a status fault is diagnostic, never a crash
            return None
        return [bool(st.installed), bool(st.enabled), bool(st.running)]

    def _registry_digest(self) -> str:
        profiles = [
            {
                "name": p.name, "dialect": p.dialect, "base_url": p.base_url,
                "api_key_env": p.api_key_env, "default_model": p.default_model,
                "context_window": p.context_window, "thinking": p.thinking,
                "scrub": p.scrub, "request_extras": p.request_extras, "tools": p.tools,
            }
            for p in self._registry.profiles.values()
        ]
        canonical = json.dumps({"port": self._registry.port, "profiles": profiles}, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _read_preconditions(self) -> dict:
        """Read the drift-relevant machine state ONCE through the runner (diagnostic ops)."""
        r = self._runner
        is_linux = self._platform.startswith("linux")
        return {
            "conda_available": r.run("diagnostic", "pc:conda-available", self._conda.available),
            "conda_env_exists": r.run("diagnostic", "pc:conda-env-exists", self._conda.env_exists),
            "conda_env_python": r.run("diagnostic", "pc:conda-env-python", self._conda.env_python_version),
            "codex_home_exists": r.run("diagnostic", "pc:codex-home", self._codex_root.exists),
            "settings_flip": r.run("diagnostic", "pc:settings-flip", self._read_settings_flip_state),
            "service": r.run("diagnostic", "pc:service", self._service_state),
            "linger_enabled": r.run("diagnostic", "pc:linger", self._linger_enabled) if is_linux else False,
            "registry_port": self._registry.port,
            "registry_digest": r.run("diagnostic", "pc:registry-digest", self._registry_digest),
        }

    def _compute_digest(self, preconditions: dict, steps: list[PlanStep]) -> str:
        """Hash the CANONICAL STRUCTURED plan (preconditions + per-step descriptors)."""
        payload = {
            "preconditions": preconditions,
            "steps": [{"id": s.id, **s.descriptor} for s in steps],
        }
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _artifact_paths(self, codex_present: bool) -> list[str]:
        paths = [str(self._claude_root / "agents" / f"{p.name}.md") for p in self._registry.profiles.values()]
        if codex_present:
            paths += [str(self._codex_root / "agents" / f"{p.name}.toml") for p in self._registry.profiles.values()]
            paths.append(str(self._codex_root / "config.toml"))
        return sorted(paths)

    def _scaffold_step(self, step_id: str, host: str, path: Path) -> PlanStep:
        exists = path.exists()
        names = tuple(p.api_key_env for p in self._registry.profiles.values())
        return PlanStep(
            id=step_id, kind="mutating", action="SKIP" if exists else "CREATE",
            title=f"Scaffold the {host} Key file (comments only, mode 0600)", targets=[str(path)],
            descriptor={"op": "scaffold-key", "target": str(path)},
            run=lambda e, p=path, n=names: e._apply_scaffold(p, n),
        )

    def _conda_create_step(self) -> PlanStep:
        """The create-the-env step (the env-absent branch; conda is a satisfied Prerequisite)."""
        return PlanStep(
            id="conda-env", kind="mutating", action="CREATE",
            title=f"Create conda env {CONDA_ENV} (python {PY_VERSION})", targets=[],
            descriptor={"op": "conda-create", "env": CONDA_ENV, "python": PY_VERSION},
            run=lambda e: e._apply_conda_create(),
        )

    # -- plan phase -----------------------------------------------------------------

    def build_plan(self) -> Plan:
        """Diagnose then compute the structured setup plan + digest (mutates NOTHING).

        Phase-A pause (ADR 0009 as amended 2026-08-14): when a Platform Prerequisite (bash /
        Git for Windows / Miniconda) is MISSING, return a plan carrying ONLY the agent-run
        ``prerequisite_fixes`` rows — no mutating steps and no digest. The Host installs them on
        approval; only a re-run with every Prerequisite present emits the normal mutating plan.
        The engine never installs a Prerequisite itself.
        """
        self._runner = _Runner()
        before = self._runner.run("diagnostic", "diagnose", self._diagnose)
        if self._registry is None:
            return Plan("setup", {}, before, [], "", self._registry_error)
        prerequisites = self._prerequisites()
        if prerequisites and not all(prerequisites.values()):
            return Plan(
                "setup", {}, before, [], "",
                prerequisite_fixes=self._prerequisite_fixes(prerequisites),
            )
        preconditions = self._read_preconditions()
        steps = self._build_setup_steps(preconditions)
        return Plan("setup", preconditions, before, steps, self._compute_digest(preconditions, steps))

    def _build_setup_steps(self, pc: dict) -> list[PlanStep]:
        from chinamaxM.keyfiles import KEY_FILE_NAME

        steps: list[PlanStep] = []
        codex_present = pc["codex_home_exists"]

        # (a) conda env — Miniconda is a Platform Prerequisite (ADR 0009 as amended
        # 2026-08-14): build_plan PAUSES on a missing miniconda Prerequisite before reaching
        # here, so ``conda`` is always resolvable at this point. Create-only-when-absent (never
        # auto-recreate); a wrong-Python env gate-fails.
        if not pc["conda_env_exists"]:
            steps.append(self._conda_create_step())
        elif pc["conda_env_python"] != PY_VERSION:
            steps.append(PlanStep(
                id="conda-env", kind="mutating", action="GATE-FAIL",
                title=f"conda env {CONDA_ENV}", targets=[],
                descriptor={"op": "gate_fail", "reason": "wrong-python", "found": pc["conda_env_python"]},
                gate_fail=True,
                gate_detail=(
                    f"conda env {CONDA_ENV} runs python {pc['conda_env_python']} (need {PY_VERSION}); "
                    "remove it and re-run setup — it is never auto-recreated"
                ),
            ))
        else:
            steps.append(PlanStep(
                id="conda-env", kind="mutating", action="SKIP",
                title=f"conda env {CONDA_ENV} present (python {PY_VERSION})", targets=[],
                descriptor={"op": "conda-skip"},
            ))

        # (a2) editable install with the pinned dependencies.
        steps.append(PlanStep(
            id="pip-install", kind="mutating", action="INSTALL",
            title=f"pip install -e the plugin (aiohttp + {_LITELLM_PIN})",
            targets=[str(self._plugin_root)],
            descriptor={"op": "pip-install", "target": str(self._plugin_root), "pin": _LITELLM_PIN},
            run=lambda e: e._apply_pip_install(),
        ))

        # (a3) record the resolved env Python for the hook shims' rung-2 — placed AFTER
        # pip-install so the recorded interpreter always points to an env where chinamaxM is
        # installed (a pip-install failure aborts BEFORE this runs, so we never record a
        # chinamaxM-less env; the shims fall to the slow `conda run` rung until a successful
        # apply). Dead until setup writes it, else every hook spawn hits that slow rung.
        steps.append(PlanStep(
            id="record-python-path", kind="mutating", action="RECORD",
            title="Record the chinamaxM env Python for the hook shims",
            targets=[str(self._python_path_file())],
            descriptor={"op": "record-python-path", "target": str(self._python_path_file())},
            run=lambda e: e._apply_record_python_path(),
        ))

        # (b) scaffold Key files (Codex only when the Codex home exists — ADR 0006).
        steps.append(self._scaffold_step("scaffold-claude-key", "Claude", self._claude_root / KEY_FILE_NAME))
        if codex_present:
            steps.append(self._scaffold_step("scaffold-codex-key", "Codex", self._codex_root / KEY_FILE_NAME))

        # (c) generation (Claude always; Codex + strict-config only with the Codex home).
        steps.append(PlanStep(
            id="generate", kind="mutating", action="GENERATE",
            title="Generate Worker agents" + ("" if codex_present else " (Claude only — no Codex home)"),
            targets=self._artifact_paths(codex_present),
            descriptor={"op": "generate", "include_codex": codex_present, "artifacts": self._artifact_paths(codex_present)},
            run=lambda e, inc=codex_present: e._apply_generate(inc),
        ))
        if codex_present:
            steps.append(PlanStep(
                id="codex-validate", kind="mutating", action="VALIDATE",
                title="Validate Codex config (codex exec --strict-config)",
                targets=[str(self._codex_root / "config.toml")],
                descriptor={"op": "codex-strict-config", "codex_home": str(self._codex_root)},
                run=lambda e: e._apply_codex_validate(),
            ))

        # (d) service unit + install/update, then Linux linger.
        service_title = "Write the Proxy service unit and install (or update) it"
        service_descriptor = {
            "op": "service", "entry": list(PROXY_ENTRY),
            "port": self._registry.port, "log_dir": str(self._log_dir),
        }
        if self._platform.startswith("win"):
            # Windows acquires WinSW; the source rides the descriptor so the digest binds it
            # and the title so the plan render shows it (ADR 0009 as amended).
            winsw = self._winsw_source()
            service_descriptor["winsw"] = winsw
            service_title += f" — WinSW source: {winsw['render']}"
        steps.append(PlanStep(
            id="service", kind="mutating", action="INSTALL/UPDATE",
            title=service_title,
            targets=[str(self._service_artifact_path())],
            descriptor=service_descriptor,
            run=lambda e: e._apply_service(),
        ))
        if self._platform.startswith("linux"):
            already = pc["linger_enabled"]
            steps.append(PlanStep(
                id="linger", kind="mutating", action="SKIP" if already else "ENABLE-LINGER",
                title="Enable systemd linger (headless reboot survival)", targets=[],
                descriptor={"op": "enable-linger", "already": already},
                run=None if already else (lambda e: e._apply_linger()),
            ))

        # (e) readiness poll (diagnostic — a timeout is "may still be starting", never down).
        steps.append(PlanStep(
            id="readiness", kind="diagnostic", action="POLL",
            title=f"Wait for the Proxy on 127.0.0.1:{self._registry.port}", targets=[],
            descriptor={"op": "readiness-poll", "port": self._registry.port},
            run=lambda e: e._apply_readiness(),
        ))

        # (f) env flip LAST — never overwrite a foreign ANTHROPIC_BASE_URL.
        steps.append(self._env_flip_step(pc["settings_flip"]))
        return steps

    def _env_flip_step(self, flip_state: list) -> PlanStep:
        url = self._flip_url()
        target = str(self._settings_path)
        if flip_state[0] == "unparseable":
            return PlanStep(
                id="env-flip", kind="mutating", action="GATE-FAIL",
                title="Set the ANTHROPIC_BASE_URL env flip", targets=[target],
                descriptor={"op": "gate_fail", "reason": "settings-unparseable", "target": target},
                gate_fail=True,
                gate_detail=f"settings.json is unparseable — fix it manually, then re-run setup: {target}",
            )
        if flip_state[0] == "present" and not self._is_our_flip(flip_state[1]):
            return PlanStep(
                id="env-flip", kind="mutating", action="GATE-FAIL",
                title="Set the ANTHROPIC_BASE_URL env flip", targets=[target],
                descriptor={"op": "gate_fail", "reason": "foreign-base-url", "target": target},
                gate_fail=True,
                gate_detail=(
                    "ANTHROPIC_BASE_URL is already set to a non-chinamaxM value; refusing to "
                    f"overwrite it. Point it at {url} yourself or clear it, then re-run setup"
                ),
            )
        old = flip_state[1] if flip_state[0] == "present" else None
        return PlanStep(
            id="env-flip", kind="mutating", action="FLIP",
            title=f"Set ANTHROPIC_BASE_URL = {url} in settings.json", targets=[target],
            descriptor={"op": "env-flip", "target": target, "old": old, "new": url},
            run=lambda e, u=url: e._apply_env_flip(u),
        )

    # -- apply phase ----------------------------------------------------------------

    def apply(self, plan_digest: str, *, probes: bool = False) -> Report:
        """Verify the digest + preconditions, then apply in order and re-diagnose."""
        plan = self.build_plan()
        rejected = self._gate(plan, plan_digest)
        if rejected is not None:
            return Report("setup", 1, rejected=rejected, before_findings=plan.before_findings)
        self._runner.approve()
        step_results = self._apply_steps(plan, abort_on_fail=True)
        after = self._diagnose()
        probe_results, probes_skipped = self._maybe_probe(after, probes)
        exit_code = 0 if all(r.status != "failed" for r in step_results) else 1
        return Report(
            "setup", exit_code, before_findings=plan.before_findings, step_results=step_results,
            after_findings=after, probe_results=probe_results, probes_skipped=probes_skipped,
            restart_instruction=_RESTART_INSTRUCTION,
        )

    def _gate(self, plan: Plan, plan_digest: str) -> str | None:
        """Return a rejection reason (or ``None``) — no mutation has occurred at this point."""
        if plan.registry_error:
            return f"Registry unreadable: {plan.registry_error}"
        if plan.digest != plan_digest:
            return "plan digest mismatch — re-run `--plan-only` and approve the current plan"
        if self._read_preconditions() != plan.preconditions:
            return "preconditions drifted between plan and apply — re-run `--plan-only`"
        return None

    def _apply_steps(self, plan: Plan, *, abort_on_fail: bool) -> list[StepResult]:
        results: list[StepResult] = []
        aborted = False
        for step in plan.steps:
            if aborted:
                results.append(StepResult(step.id, "aborted", "a prior step failed"))
                continue
            if step.gate_fail:
                results.append(StepResult(step.id, "failed", step.gate_detail))
                if abort_on_fail:
                    aborted = True
                continue
            if step.run is None:
                results.append(StepResult(step.id, "skipped", step.title))
                continue
            try:
                detail = self._runner.run(step.kind, step.id, lambda s=step: s.run(self))
                results.append(StepResult(step.id, "ok", str(detail)))
            except Exception as exc:  # noqa: BLE001 - a step fault aborts the rest, never crashes
                results.append(StepResult(step.id, "failed", f"{type(exc).__name__}: {exc}"))
                if abort_on_fail and step.kind == "mutating":
                    aborted = True
        return results

    # -- apply actions --------------------------------------------------------------

    def _apply_conda_create(self) -> str:
        self._conda.create()
        return f"created conda env {CONDA_ENV} (python {PY_VERSION})"

    def _apply_pip_install(self) -> str:
        self._conda.pip_install(self._plugin_root)
        return f"editable-installed the plugin into {CONDA_ENV}"

    def _apply_record_python_path(self) -> str:
        """Record the resolved env Python at ``<claude-root>/chinamaxM/python-path``.

        This is the hook shims' rung-2 interpreter (a single ``<abs-python>\\n`` line);
        without it every hook spawn falls to the slow ``conda run`` last resort. Written
        atomically and skipped when byte-identical (idempotent; ADR 0009 / hosts-02 handoff).
        """
        from chinamaxM.generate import _atomic_write

        target = self._python_path_file()
        content = (self._conda.env_python_path() + "\n").encode("utf-8")
        if target.exists() and target.read_bytes() == content:
            return f"env Python already recorded at {target}"
        _atomic_write(target, content)
        return f"recorded env Python at {target}"

    def _apply_scaffold(self, path: Path, names: tuple[str, ...]) -> str:
        from chinamaxM.keyfiles import scaffold_key_file

        status = scaffold_key_file(path, list(names))
        return f"Key file {status}: {path}"

    def _apply_generate(self, include_codex: bool) -> str:
        roots = {"claude": self._claude_root, "codex": self._codex_root}
        report = self._generate_fn(self._registry, roots, include_codex)
        written = report.get("written", []) if isinstance(report, dict) else []
        return f"generated agents (written={len(written)}, include_codex={include_codex})"

    def _apply_codex_validate(self) -> str:
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self._codex_root)
        cp = self._run(["codex", "exec", "--strict-config"], timeout=_CODEX_TIMEOUT, env=env)
        if cp.returncode != 0:
            raise SetupError(f"codex exec --strict-config failed (exit {cp.returncode})")
        return "Codex config validated (strict-config)"

    def _apply_service(self) -> str:
        winsw_exe_path = None
        caveat = ""
        if self._platform.startswith("win"):
            # Acquire WinSW now (operator override → already-installed → pinned checksummed
            # download); any acquisition failure raises SupervisionError and aborts this step.
            service_dir = self._service_artifact_path().parent
            winsw_exe_path = self._ensure_winsw(service_dir, self._winsw_exe)
            if self._service_password is None:
                # No --winsw-service-password-file: nothing sets sc.exe password=. The service
                # account must be allowed to log on as a service (ADR 0009 as amended).
                caveat = (
                    " (no --winsw-service-password-file supplied; the service account must be "
                    "allowed to log on as a service — a passwordless local account may fail to "
                    "start the service)"
                )
        cfg = self._install_cfg(winsw_exe_path)
        rendered = self._service_render(cfg)
        path = Path(rendered.path)
        on_disk = path.read_bytes() if path.exists() else None
        if on_disk is not None and on_disk != rendered.content:
            self._service_update(cfg)
            return "service updated (unit artifact changed)" + caveat
        self._service_install(cfg)
        return "service installed, enabled, and started" + caveat

    def _apply_linger(self) -> str:
        self._enable_linger()
        return "systemd linger enabled"

    def _apply_readiness(self) -> str:
        deadline = self._now() + _READINESS_DEADLINE
        delay = _READINESS_INITIAL_DELAY
        while True:
            if self._port_live(self._registry.port):
                return f"Proxy accepting connections on 127.0.0.1:{self._registry.port}"
            remaining = deadline - self._now()
            if remaining <= 0:
                return "Proxy not yet listening — it may still be starting (cold litellm import can exceed 30s)"
            self._sleep(min(delay, remaining))
            delay = min(delay * 2, _READINESS_MAX_DELAY)

    def _apply_env_flip(self, url: str) -> str:
        settings_json.write_flip(self._settings_path, url)
        return f"set ANTHROPIC_BASE_URL = {url} in {self._settings_path}"

    # -- probes ---------------------------------------------------------------------

    def _maybe_probe(self, after_findings: list, probes: bool) -> tuple[list, str | None]:
        if not probes:
            return [], "not requested"
        for finding in after_findings:
            if (
                getattr(finding, "id", None) in ("service", "port")
                and getattr(finding, "level", None) == "fail"
                and getattr(finding, "status", None) in ("fail", "error")
            ):
                return [], f"skipped — re-diagnose reported a FAIL-level {finding.id} finding"
        return self._run_probes(), None

    def _run_probes(self) -> list[ProbeResult]:
        results: list[ProbeResult] = []
        port = self._registry.port
        codex_present = self._codex_root.exists()
        for profile in self._registry.profiles.values():
            results.append(self._probe(
                profile.name, "anthropic", f"http://127.0.0.1:{port}/v1/messages",
                {"model": f"{profile.name}/{profile.default_model}", "max_tokens": 1,
                 "messages": [{"role": "user", "content": "ping"}]},
            ))
            if codex_present:
                results.append(self._probe(
                    profile.name, "responses", f"http://127.0.0.1:{port}/openai/{profile.name}/responses",
                    {"model": profile.default_model, "max_output_tokens": 1,
                     "input": [{"role": "user", "content": "ping"}]},
                ))
        return results

    def _probe(self, profile_name: str, ingress: str, url: str, body: dict) -> ProbeResult:
        try:
            resp = self._http(url, body, connect_timeout=_PROBE_CONNECT_TIMEOUT, total_timeout=_PROBE_TOTAL_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - a failing Profile never aborts the rest
            return ProbeResult(profile_name, ingress, False, None, f"probe error: {type(exc).__name__}", None)
        if resp.status == 0:
            return ProbeResult(profile_name, ingress, False, None, "no response (connection failed)", None)
        if 200 <= resp.status < 300:
            return ProbeResult(profile_name, ingress, True, resp.status, "ok", _extract_usage(resp.body))
        return ProbeResult(profile_name, ingress, False, resp.status, _render_probe_failure(resp.status, resp.body), None)

    # -- teardown -------------------------------------------------------------------

    def build_teardown_plan(self) -> Plan:
        """Diagnose then compute the two-step teardown plan + digest (mutates NOTHING)."""
        self._runner = _Runner()
        before = self._runner.run("diagnostic", "diagnose", self._diagnose)
        if self._registry is None:
            return Plan("teardown", {}, before, [], "", self._registry_error)
        preconditions = self._read_preconditions()
        steps = self._build_teardown_steps(preconditions)
        return Plan("teardown", preconditions, before, steps, self._compute_digest(preconditions, steps))

    def _build_teardown_steps(self, pc: dict) -> list[PlanStep]:
        target = str(self._settings_path)
        flip_state = pc["settings_flip"]
        if flip_state[0] == "absent":
            action, run = "SKIP", None
        elif flip_state[0] == "unparseable":
            action, run = "REMOVE", (lambda e: e._apply_remove_flip())
        elif self._is_our_flip(flip_state[1]):
            action, run = "REMOVE", (lambda e: e._apply_remove_flip())
        else:
            action, run = "LEAVE-FOREIGN", None
        steps = [PlanStep(
            id="env-flip-remove", kind="mutating", action=action,
            title="Remove the ANTHROPIC_BASE_URL env flip (only when it is ours)", targets=[target],
            descriptor={"op": "remove-env-flip", "target": target, "state": flip_state[0]},
            run=run,
        )]
        steps.append(PlanStep(
            id="service-teardown", kind="mutating", action="UNINSTALL",
            title="Uninstall the Proxy supervision service",
            targets=[str(self._service_artifact_path())],
            descriptor={"op": "service-teardown", "port": self._registry.port},
            run=lambda e: e._apply_service_teardown(),
        ))
        steps.append(PlanStep(
            id="python-path-remove", kind="mutating", action="REMOVE",
            title="Remove the recorded chinamaxM env Python path (hook shim rung-2)",
            targets=[str(self._python_path_file())],
            descriptor={"op": "remove-python-path", "target": str(self._python_path_file())},
            run=lambda e: e._apply_remove_python_path(),
        ))
        return steps

    def teardown(self, plan_digest: str) -> Report:
        """Verify the digest + preconditions, then run the two best-effort teardown steps."""
        plan = self.build_teardown_plan()
        rejected = self._gate(plan, plan_digest)
        if rejected is not None:
            return Report("teardown", 1, rejected=rejected, before_findings=plan.before_findings)
        self._runner.approve()
        step_results = self._apply_steps(plan, abort_on_fail=False)  # best-effort independent
        after = self._diagnose()
        exit_code = 0 if all(r.status != "failed" for r in step_results) else 1
        return Report(
            "teardown", exit_code, before_findings=plan.before_findings,
            step_results=step_results, after_findings=after,
        )

    def _apply_remove_flip(self) -> str:
        status = settings_json.remove_flip(self._settings_path, self._is_our_flip)
        return f"env flip {status}"

    def _apply_service_teardown(self) -> str:
        self._service_teardown(self._status_cfg())
        return "service uninstalled"

    def _apply_remove_python_path(self) -> str:
        """Remove the recorded env-Python file; an absent file is a no-op, never an error."""
        target = self._python_path_file()
        try:
            target.unlink()
            return f"removed {target}"
        except FileNotFoundError:
            return "no recorded python-path to remove"
        except OSError as exc:
            raise SetupError(f"could not remove {target}: {exc}") from exc


# --------------------------------------------------------------------------- rendering


def _extract_usage(body: bytes) -> dict | None:
    """Return the response's ``usage`` object when present (never a Key value)."""
    try:
        doc = json.loads(body)
    except ValueError:
        return None
    usage = doc.get("usage") if isinstance(doc, dict) else None
    return usage if isinstance(usage, dict) else None


def _render_probe_failure(status: int, body: bytes) -> str:
    """Render a probe failure from a BOUNDED read: parsed error fields, or a byte count."""
    bounded = body[:_PROBE_MAX_READ]
    try:
        doc = json.loads(bounded)
    except ValueError:
        return f"HTTP {status}: unparseable upstream error ({len(body)} bytes)"
    error = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(error, dict) and ("type" in error or "message" in error):
        return f"HTTP {status}: {error.get('type', '?')}: {error.get('message', '')}"
    return f"HTTP {status}: unparseable upstream error ({len(body)} bytes)"


def render_plan(plan: Plan) -> str:
    """Render the plan for operator review (prose only — the digest binds the structure)."""
    from chinamaxM import doctor

    lines = [f"chinamaxM {plan.kind} plan", ""]
    if plan.registry_error:
        lines.append(f"REGISTRY ERROR: {plan.registry_error}")
        lines.append("Cannot plan until the Registry loads — fix it and re-run.")
        return "\n".join(lines)
    if plan.prerequisite_fixes:
        # Phase-A pause: render the agent-run Rectification rows and NOTHING else — no steps,
        # no digest. The mutating plan appears only on a re-run with every Prerequisite present.
        lines.append("Current diagnosis:")
        lines.append(doctor.render_report(plan.before_findings))
        lines.append("")
        lines.append(
            "Missing platform prerequisites — setup PAUSED before any change. Install these "
            "first (the Host runs them on approval; the engine never installs a prerequisite):"
        )
        for row in plan.prerequisite_fixes:
            lines.append(
                f"  {row['name']} [run_policy={row['run_policy']}, shell={row['shell']}, "
                f"install into {row['install_location']}]"
            )
            lines.append(f"      {row['summary']}")
            for command in row["commands"]:
                lines.append(f"      $ {command}")
            if not row["commands"]:
                lines.append("      (install manually — no automatic command)")
        lines.append("")
        lines.append(
            'Reply "approve" to install these, anything else to stop. After they are installed, '
            "re-run --plan-only; only then is the mutating plan and its digest emitted."
        )
        return "\n".join(lines)
    lines.append("Current diagnosis:")
    lines.append(doctor.render_report(plan.before_findings))
    lines.append("")
    lines.append("Planned steps (applied strictly in this order; the first failure aborts the rest):")
    for index, step in enumerate(plan.steps, 1):
        lines.append(f"  {index}. [{step.action}] {step.title}")
        for target in step.targets:
            lines.append(f"       target: {target}")
        if step.gate_fail:
            lines.append(f"       BLOCKED: {step.gate_detail}")
    lines.append("")
    if plan.kind == "setup":
        lines.append("Live probes are a SEPARATE opt-in: pass --probes to send one minimal request")
        lines.append("per Profile per ingress (they spend tokens). Omit it to run zero probes.")
        lines.append("")
        apply_cmd = f"python -m chinamaxM.setup --apply --plan-digest {plan.digest} [--probes]"
    else:
        apply_cmd = f"python -m chinamaxM.setup --teardown --plan-digest {plan.digest}"
    lines.append(f"Plan digest: {plan.digest}")
    lines.append(f"To apply, re-run:  {apply_cmd}")
    lines.append("Concurrency: single-operator only — never run setup concurrently.")
    return "\n".join(lines)


def render_report(report: Report) -> str:
    """Render the final report (findings before/after, step outcomes, probes, restart notice)."""
    from chinamaxM import doctor

    lines = [f"chinamaxM {report.kind} report"]
    if report.rejected:
        lines.append("")
        lines.append(f"ABORTED (no changes made): {report.rejected}")
        return "\n".join(lines)
    lines.append("")
    lines.append("Diagnosis before:")
    lines.append(doctor.render_report(report.before_findings))
    lines.append("")
    lines.append("Applied steps:")
    for result in report.step_results:
        lines.append(f"  [{result.status.upper()}] {result.id}: {result.detail}")
    lines.append("")
    lines.append("Diagnosis after (re-diagnose):")
    lines.append(doctor.render_report(report.after_findings))
    if report.probes_skipped:
        lines.append("")
        lines.append(f"Live probes: {report.probes_skipped}")
    elif report.probe_results:
        lines.append("")
        lines.append("Live probes:")
        for probe in report.probe_results:
            usage = f" usage={probe.usage}" if probe.usage else ""
            lines.append(
                f"  [{'PASS' if probe.ok else 'FAIL'}] {probe.profile} via {probe.ingress} "
                f"ingress: {probe.detail}{usage}"
            )
    if report.restart_instruction:
        lines.append("")
        lines.append(report.restart_instruction)
    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI entry


def _read_service_password(path: str) -> str:
    """Read the Windows service-account password from a file (never shell history / logs).

    Strips a trailing newline so a normal ``echo``-created file validates — a
    SupervisionConfig rejects control characters (newline included) in the password.

    Raises:
        SetupError: If the file cannot be read.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise SetupError(f"could not read --winsw-service-password-file {path!r}: {exc}") from exc
    return text.rstrip("\r\n")


def main(argv: list[str] | None = None) -> int:
    """CLI: ``--plan-only`` (default) renders; ``--apply``/``--teardown`` need ``--plan-digest``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--_run-generation" in argv:
        return _generation_subprocess_main()

    parser = argparse.ArgumentParser(
        prog="python -m chinamaxM.setup",
        description="chinamaxM consent-gated setup (diagnose → plan → approve → apply → re-diagnose → report).",
    )
    parser.add_argument("--plan-only", action="store_true", help="render the plan and exit 0 without mutating")
    parser.add_argument("--apply", action="store_true", help="apply the approved plan (needs --plan-digest)")
    parser.add_argument("--teardown", action="store_true", help="plan/apply teardown (remove the flip + service)")
    parser.add_argument("--plan-digest", default=None, help="the digest printed by --plan-only")
    parser.add_argument("--probes", action="store_true", help="opt into live paid probes at apply time")
    parser.add_argument(
        "--winsw-exe", default=None,
        help="Windows: an operator-supplied WinSW executable (overrides the pinned download)",
    )
    parser.add_argument(
        "--winsw-service-password-file", default=None,
        help="Windows: file holding the service-account password (read once, never logged)",
    )
    args = parser.parse_args(argv)

    engine_kwargs: dict[str, object] = {}
    if args.winsw_exe is not None:
        engine_kwargs["winsw_exe"] = args.winsw_exe
    if args.winsw_service_password_file is not None:
        engine_kwargs["service_password"] = _read_service_password(args.winsw_service_password_file)
    engine = SetupEngine(**engine_kwargs)

    if args.teardown:
        if args.plan_digest and not args.plan_only:
            report = engine.teardown(args.plan_digest)
            sys.stdout.write(render_report(report) + "\n")
            return report.exit_code
        sys.stdout.write(render_plan(engine.build_teardown_plan()) + "\n")
        return 0

    if args.apply:
        if not args.plan_digest:
            sys.stderr.write("--apply requires --plan-digest <digest> from a --plan-only run\n")
            return 2
        report = engine.apply(args.plan_digest, probes=args.probes)
        sys.stdout.write(render_report(report) + "\n")
        return report.exit_code

    sys.stdout.write(render_plan(engine.build_plan()) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI entry
    raise SystemExit(main())
