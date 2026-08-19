"""Read-only diagnosis surfaces: the ``/doctor`` roster and the ``/profiles`` renderer.

Two surfaces live here (ADR 0005): a doctor engine that runs the full FAIL/WARN/info
roster and renders findings with a nonzero exit only on a FAIL, and a profiles renderer.
Both are pure diagnosis — the engine never mutates and never makes a token-spending or
non-Anthropic-surface egress call (ADR 0005). ``python -m chinamaxM.doctor`` runs the
roster; ``--profiles`` renders the roster of Profiles instead.

The static no-chat-completions scan (:func:`find_chat_completions_references`) was created
here by proxy-03 (ADR 0002); the kimi WARN check reuses it (one scan, two consumers). The
flip-URL normalizer (:func:`normalize_flip_url`) is the ONE shared function the doctor's
env-flip TARGET check and the SessionStart hook's gate both use.

Every external invocation (``conda run``, ``codex --version``) closes stdin, is bounded by
a timeout, and maps a missing binary / permission / timeout to a clean Finding detail —
never a traceback. Every runner is injectable so tests patch them. Per-check isolation
guarantees an unexpected exception in one check yields a content-free ``error`` Finding at
that check's level rather than crashing the engine or leaking file content.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from chinamaxM.keyfiles import (
    KEY_FILE_NAME,
    HostResolutionError,
    KeyFileReader,
    resolve_host,
    resolve_host_root,
)
from chinamaxM.registry import Registry, RegistryError, load_registry

#: The banned URL fragment: a chat-completions WIRE path (never a litellm symbol name,
#: which uses the ``chat_completion`` underscore form). Assembled from parts so this
#: scanner's own source never trips its own scan.
_CHAT_COMPLETIONS_URL = "chat" + "/" + "completions"


def find_chat_completions_references(tree_root: Path) -> list[tuple[Path, int, str]]:
    """Return every chat-completions URL reference under a source tree.

    Scans the ``*.py`` files beneath ``tree_root`` for the banned chat-completions WIRE URL
    fragment. The underscore form (litellm's ``..._to_chat_completion_request`` symbols) is
    NOT matched — only the slashed URL path is a violation.

    Args:
        tree_root: The root of the tree to scan (the caller scopes this to ``src/chinamaxM``,
            excluding litellm site-packages and repo docs).

    Returns:
        A list of ``(path, line_number, line_text)`` for each offending line, in file and
        line order (empty when the tree is clean).
    """
    findings: list[tuple[Path, int, str]] = []
    for path in sorted(tree_root.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable file is not a reference
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if _CHAT_COMPLETIONS_URL in line:
                findings.append((path, number, line.strip()))
    return findings


# --------------------------------------------------------------------------- constants

#: The conda env the Proxy and its dependencies live in.
CONDA_ENV = "chinamaxM"

#: The one bounded conda import/version probe (ADR 0002 pins litellm ``== 1.96.2`` EXACTLY;
#: ``LITELLM_LOCAL_MODEL_COST_MAP=True`` in the subprocess env keeps litellm's import from
#: fetching anything on a real machine — proxy-03's before-import knob).
_CONDA_PROBE = "import aiohttp, litellm, importlib.metadata; print(importlib.metadata.version('litellm'))"

#: The litellm version ADR 0002 pins exactly.
LITELLM_PIN = "1.96.2"

#: The Codex CLI baseline whose facts were live-verified (ADR 0005 as amended).
CODEX_BASELINE = "0.147.0"

#: Bounded subprocess timeouts (seconds): conda import is heavy, codex --version is light.
_CONDA_TIMEOUT = 120.0
_CODEX_TIMEOUT = 60.0

#: stderr fragments that mark a conda failure as a missing env (vs an import failure).
_CONDA_ENV_MARKERS = (
    "could not find conda environment",
    "environmentlocationnotfound",
    "not a conda environment",
    "no environment named",
)

#: The loopback host tokens the flip normalizer and hook accept (ADR 0005).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: The detail every registry-dependent check reports when the Registry failed to load.
_REGISTRY_UNREADABLE = "registry unreadable"

#: The runner protocol: ``run(argv, *, timeout, env=None)`` returning an object carrying
#: ``returncode``/``stdout``/``stderr`` (``subprocess.CompletedProcess`` by default). It may
#: raise ``FileNotFoundError``/``PermissionError``/``subprocess.TimeoutExpired``.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


# ----------------------------------------------------------------------------- Finding


@dataclass(frozen=True)
class Finding:
    """One diagnosis observation.

    Attributes:
        id: A stable identifier for the finding (e.g. ``"service"``).
        level: The configured severity — ``"fail"`` / ``"warn"`` / ``"info"``.
        status: The observed outcome — ``"ok"`` / ``"fail"`` / ``"warn"`` / ``"skipped"`` /
            ``"error"``.
        detail: A content-free human-readable detail (never a key value, never raw
            exception text or a traceback).
    """

    id: str
    level: str
    status: str
    detail: str


# ------------------------------------------------------------------- flip-URL normalizer


@dataclass(frozen=True)
class FlipTarget:
    """A parsed ``ANTHROPIC_BASE_URL`` value (the env flip), normalized for comparison."""

    scheme: str
    host: str | None
    port: int | None
    path: str
    query: str
    is_loopback: bool


def normalize_flip_url(url: str) -> FlipTarget | None:
    """Parse an env-flip URL into a :class:`FlipTarget`, or ``None`` when unparseable.

    Setup only ever writes ``http://127.0.0.1:<port>``. This ONE shared function backs both
    the doctor's env-flip TARGET check (which additionally compares the port to the Registry
    and rejects a non-``http`` scheme or an extra path/query) and the SessionStart hook's
    gate (which connects only when the host is loopback). It never resolves DNS or connects —
    it only parses.

    Args:
        url: The raw ``ANTHROPIC_BASE_URL`` value.

    Returns:
        A :class:`FlipTarget`, or ``None`` if the value cannot be parsed at all.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        try:
            port = parts.port
        except ValueError:
            port = None
        return FlipTarget(
            scheme=parts.scheme.lower(),
            host=host,
            port=port,
            path=parts.path,
            query=parts.query,
            is_loopback=host in _LOOPBACK_HOSTS,
        )
    except Exception:  # noqa: BLE001 - a bad URL is not a chinamaxM flip, never a crash
        return None


# --------------------------------------------------------------------------- runner glue


def _default_run(
    argv: list[str], *, timeout: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one external command with stdin closed and a bounded timeout.

    ``stdin=DEVNULL`` matters: ADR 0005 documents ``codex exec`` blocking on an open
    pipeline stdin. ``FileNotFoundError``/``PermissionError``/``TimeoutExpired`` propagate to
    the caller, which maps them to a clean Finding detail.
    """
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _default_service_status(cfg: object) -> object:
    """Return ops-01's supervision status for a doctor-built config (lazy import)."""
    from chinamaxM.ops.supervision import status as _status

    return _status(cfg)


def _package_tree() -> Path | None:
    """Locate the installed ``chinamaxM`` package tree WITHOUT importing seam/litellm.

    Uses :func:`importlib.util.find_spec` on the top-level package (which does not execute
    ``chinamaxM.seam`` or litellm), so the kimi scan reads ``.py`` files as text only.
    """
    import importlib.util

    spec = importlib.util.find_spec("chinamaxM")
    if spec is not None and spec.submodule_search_locations:
        return Path(next(iter(spec.submodule_search_locations)))
    return None


# ----------------------------------------------------------------------- doctor context


@dataclass
class DoctorContext:
    """The once-resolved inputs shared by every check (the Registry loads exactly once)."""

    host: str
    claude_root: Path
    codex_root: Path
    registry: Registry | None
    registry_error: str | None
    run: Runner
    port_probe: Callable[[int], bool]
    service_status: Callable[[object], object]
    scan_tree: Path | None


# --------------------------------------------------------------------------- settings io


def _read_settings_env(claude_root: Path) -> tuple[str, dict | None, str]:
    """Read ``<claude-root>/settings.json``'s ``env`` block.

    Returns:
        ``(state, env, detail)``. ``state`` is ``"ok"`` (``env`` is a dict, possibly empty),
        ``"absent"`` (no settings.json — the flip is simply not set), or ``"error"``
        (unreadable / malformed JSON / non-object root / non-object env block, with a
        content-free ``detail``).
    """
    path = claude_root / "settings.json"
    if not path.exists():
        return "absent", {}, ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "error", None, "settings.json could not be read"
    try:
        doc = json.loads(text)
    except ValueError:
        return "error", None, "settings.json is not valid JSON"
    if not isinstance(doc, dict):
        return "error", None, "settings.json root is not a JSON object"
    env = doc.get("env", {})
    if env is None:
        env = {}
    if not isinstance(env, dict):
        return "error", None, "settings 'env' block is not a JSON object"
    return "ok", env, ""


# --------------------------------------------------------------------------- FAIL checks


def check_registry(ctx: DoctorContext) -> list[Finding]:
    """FAIL: the Registry (shipped defaults + Overlay) parsed and merged."""
    if ctx.registry is not None:
        return [Finding("registry", "fail", "ok", f"{len(ctx.registry.profiles)} profiles resolved")]
    return [Finding("registry", "fail", "fail", ctx.registry_error or "registry could not be loaded")]


def check_conda(ctx: DoctorContext) -> list[Finding]:
    """FAIL: aiohttp + litellm import under the env python, litellm pinned exactly."""
    argv = ["conda", "run", "-n", CONDA_ENV, "python", "-c", _CONDA_PROBE]
    env = dict(os.environ)
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    try:
        result = ctx.run(argv, timeout=_CONDA_TIMEOUT, env=env)
    except FileNotFoundError:
        return [Finding("conda", "fail", "fail", "conda not found on PATH")]
    except PermissionError:
        return [Finding("conda", "fail", "fail", "conda is not executable")]
    except subprocess.TimeoutExpired:
        return [Finding("conda", "fail", "fail", f"conda import probe timed out after {_CONDA_TIMEOUT:g}s")]

    if result.returncode == 0:
        out = (result.stdout or "").strip()
        version = out.splitlines()[-1].strip() if out else ""
        if version == LITELLM_PIN:
            return [Finding("conda", "fail", "ok", f"env {CONDA_ENV}: aiohttp + litellm {LITELLM_PIN} OK")]
        return [Finding("conda", "fail", "fail", f"litellm version {version!r} != required {LITELLM_PIN}")]

    stderr = (result.stderr or "").lower()
    if any(marker in stderr for marker in _CONDA_ENV_MARKERS):
        return [Finding("conda", "fail", "fail", f"conda env {CONDA_ENV!r} not found")]
    return [Finding("conda", "fail", "fail", f"aiohttp/litellm import failed under the {CONDA_ENV} env python")]


def check_service(ctx: DoctorContext) -> list[Finding]:
    """FAIL: the supervision unit is installed + enabled + running (ADR 0009)."""
    if ctx.registry is None:
        return [Finding("service", "fail", "skipped", _REGISTRY_UNREADABLE)]
    from chinamaxM.ops.supervision import SupervisionConfig

    cfg = SupervisionConfig(
        python_path=sys.executable,
        entry=["-m", "chinamaxM.proxy"],
        port=ctx.registry.port,
        log_dir=ctx.claude_root / "chinamaxM",
    )
    st = ctx.service_status(cfg)  # a raise here becomes a content-free 'error' Finding
    ok = bool(st.installed and st.enabled and st.running)
    detail = f"installed={st.installed} enabled={st.enabled} running={st.running}"
    return [Finding("service", "fail", "ok" if ok else "fail", detail)]


def check_port(ctx: DoctorContext) -> list[Finding]:
    """FAIL: something is listening on the Proxy port (any listener passes — ADR 0009)."""
    if ctx.registry is None:
        return [Finding("port", "fail", "skipped", _REGISTRY_UNREADABLE)]
    port = ctx.registry.port
    if ctx.port_probe(port):
        return [Finding("port", "fail", "ok", f"proxy port {port} is listening")]
    return [Finding("port", "fail", "fail", f"proxy port {port} is not listening")]


def check_env_flip(ctx: DoctorContext) -> list[Finding]:
    """FAIL: the ``ANTHROPIC_BASE_URL`` env flip is present (PRESENCE) and targets the Proxy
    (TARGET). PRESENCE is Registry-independent and always reports; TARGET is skipped when the
    Registry is unreadable or no flip URL is present."""
    findings: list[Finding] = []
    state, env, detail = _read_settings_env(ctx.claude_root)
    value: str | None = None
    if state == "error":
        findings.append(Finding("env-flip-present", "fail", "fail", detail))
    elif state == "absent":
        findings.append(Finding("env-flip-present", "fail", "fail", "ANTHROPIC_BASE_URL absent (no settings.json)"))
    else:
        candidate = env.get("ANTHROPIC_BASE_URL") if env is not None else None
        if isinstance(candidate, str) and candidate:
            value = candidate
            findings.append(Finding("env-flip-present", "fail", "ok", "ANTHROPIC_BASE_URL set in settings env block"))
        else:
            findings.append(
                Finding("env-flip-present", "fail", "fail", "ANTHROPIC_BASE_URL absent from settings env block")
            )

    if ctx.registry is None:
        findings.append(Finding("env-flip-target", "fail", "skipped", _REGISTRY_UNREADABLE))
    elif value is None:
        findings.append(Finding("env-flip-target", "fail", "skipped", "no ANTHROPIC_BASE_URL to check"))
    else:
        findings.append(_check_flip_target(value, ctx.registry.port))
    return findings


def _check_flip_target(value: str, port: int) -> Finding:
    """Return the env-flip TARGET Finding, naming the first mismatched part (never content)."""
    target = normalize_flip_url(value)
    if target is None:
        return Finding("env-flip-target", "fail", "fail", "ANTHROPIC_BASE_URL is not a parseable URL")
    if target.scheme != "http":
        return Finding("env-flip-target", "fail", "fail", f"flip scheme is {target.scheme!r}, expected http")
    if not target.is_loopback:
        return Finding("env-flip-target", "fail", "fail", "flip host is not loopback (127.0.0.1/localhost/::1)")
    if target.port != port:
        return Finding("env-flip-target", "fail", "fail", f"flip port {target.port} does not match Registry port {port}")
    if target.path not in ("", "/") or target.query:
        return Finding("env-flip-target", "fail", "fail", "flip URL carries an unexpected path or query")
    return Finding("env-flip-target", "fail", "ok", f"flip targets http://<loopback>:{port}")


def check_drift(ctx: DoctorContext) -> list[Finding]:
    """FAIL: the invoking Host's generated agents are byte-in-sync with the Registry.

    Host-scoped (ADR 0005 as amended 2026-08-18): a Claude doctor compares only the Claude
    agents, a Codex doctor only the Codex role TOMLs + provider tables — the other Host never
    appears. Staleness is strict whole-content equality, model line included (ADR 0004 as
    amended 2026-08-19: artifacts are immutable outside generation)."""
    if ctx.registry is None:
        return [Finding("drift", "fail", "skipped", _REGISTRY_UNREADABLE)]
    from chinamaxM.generate import detect_drift

    result = detect_drift(ctx.registry, {"claude": ctx.claude_root, "codex": ctx.codex_root}, ctx.host)
    issues = {
        bucket: result[bucket]
        for bucket in ("missing", "stale", "foreign", "conflicts")
        if result[bucket]
    }
    fid = f"drift-{ctx.host}"
    noun = "agents" if ctx.host == "claude" else "artifacts"
    label = "Claude" if ctx.host == "claude" else "Codex"

    if issues:
        return [Finding(fid, "fail", "fail", f"{label} {noun} out of sync ({_summarize(issues)})")]
    return [Finding(fid, "fail", "ok", f"{label} generated {noun} in sync")]


def _summarize(issues: dict[str, list[str]]) -> str:
    """Summarize a drift bucket map as ``bucket: count`` phrases (no paths leaked)."""
    return "; ".join(f"{bucket}: {len(items)}" for bucket, items in sorted(issues.items()))


def check_strict_config(ctx: DoctorContext) -> list[Finding]:
    """FAIL (demoted): ``codex exec --strict-config``.

    ADR 0005 as amended probe-verified only the config-ERROR path tokenless on 0.147.0; the
    PASS path was NOT confirmed to make zero token-spend / zero non-Anthropic egress / zero
    writes under CODEX_HOME, and this implementation may not run any codex command that could
    make an API call to confirm it. Per the ADR's recorded demotion clause the check ships as
    ``skipped (strict-config is setup-only)`` (Setup is consent-gated and may spend). When the
    Codex root is absent it skips with the resolved-root detail instead."""
    if not ctx.codex_root.exists():
        return [Finding("codex-strict-config", "fail", "skipped", f"codex root {ctx.codex_root} absent")]
    return [Finding("codex-strict-config", "fail", "skipped", "strict-config is setup-only")]


# --------------------------------------------------------------------------- WARN checks


def check_keys(ctx: DoctorContext) -> list[Finding]:
    """WARN: each Profile's key PRESENT/MISSING in the invoking Host's Key file (never a value).

    Host-scoped (ADR 0005/0006 as amended 2026-08-18): only the invoking Host's
    ``model-keys.env`` is opened for variable NAMES and presence — the other Host's file is
    never read."""
    if ctx.registry is None:
        return [Finding("keys", "warn", "skipped", _REGISTRY_UNREADABLE)]
    host = ctx.host
    root = ctx.claude_root if host == "claude" else ctx.codex_root
    key_path = Path(root) / KEY_FILE_NAME
    try:  # stat first: the reader maps both absent and unreadable to an empty mapping
        key_path.stat()
        file_state = "present"
    except FileNotFoundError:
        file_state = "absent"
    except OSError:
        file_state = "unreadable"
    reader = KeyFileReader(key_path) if file_state == "present" else None
    findings: list[Finding] = []
    for profile in ctx.registry.profiles.values():
        var = profile.api_key_env
        fid = f"key-{host}-{profile.name}"
        if file_state == "absent":
            findings.append(Finding(fid, "warn", "warn", f"{var} MISSING ({host} key file absent)"))
        elif file_state == "unreadable":
            findings.append(Finding(fid, "warn", "warn", f"{var} unknown ({host} key file unreadable)"))
        else:
            has_value = bool(reader.get(var))  # only the boolean is used — never the value
            state = "PRESENT" if has_value else "MISSING"
            findings.append(Finding(fid, "warn", "ok" if has_value else "warn", f"{var} {state} ({host} key file)"))
    return findings


def check_subagent_model(ctx: DoctorContext) -> list[Finding]:
    """WARN: ``CLAUDE_CODE_SUBAGENT_MODEL`` set in the process env OR the settings env block."""
    in_env = "CLAUDE_CODE_SUBAGENT_MODEL" in os.environ
    in_settings = False
    state, env, _ = _read_settings_env(ctx.claude_root)
    if state == "ok" and env is not None:
        candidate = env.get("CLAUDE_CODE_SUBAGENT_MODEL")
        in_settings = isinstance(candidate, str) and bool(candidate)
    if in_env or in_settings:
        where = "process environment" if in_env else "settings env block"
        return [
            Finding(
                "subagent-model",
                "warn",
                "warn",
                f"CLAUDE_CODE_SUBAGENT_MODEL is set (in the {where}); it would hijack every Worker's model",
            )
        ]
    return [Finding("subagent-model", "warn", "ok", "CLAUDE_CODE_SUBAGENT_MODEL is unset")]


def check_kimi(ctx: DoctorContext) -> list[Finding]:
    """WARN: no product-code chat-completions reference (the kimi-k3 Responses bug, #33921).

    Reuses :func:`find_chat_completions_references` over the located package tree WITHOUT
    importing ``chinamaxM.seam`` or litellm. A clean tree is an OK informational finding (the
    upstream bug stays visible without warning); a hit WARNs — a shipped-code tripwire, never
    a claim to observe litellm's upstream degradation itself."""
    tree = ctx.scan_tree
    if tree is None or not Path(tree).is_dir():
        return [Finding("kimi-responses-bug", "warn", "warn", "could not locate the chinamaxM package tree to scan")]
    if find_chat_completions_references(Path(tree)):
        return [
            Finding(
                "kimi-responses-bug",
                "warn",
                "warn",
                "product-code chat-completions reference found (litellm #33921, moonshot Responses gap); "
                "the Seam must never egress that dialect",
            )
        ]
    return [
        Finding(
            "kimi-responses-bug",
            "warn",
            "ok",
            "no product-code chat-completions reference — chinamaxM routing unaffected (litellm #33921 stays upstream-only)",
        )
    ]


def check_codex_version(ctx: DoctorContext) -> list[Finding]:
    """WARN: the Codex CLI version vs the ``0.147.0`` verified baseline (``codex --version``,
    tokenless). Skipped when the resolved Codex root is absent."""
    if not ctx.codex_root.exists():
        return [Finding("codex-version", "warn", "skipped", f"codex root {ctx.codex_root} absent")]
    argv = ["codex", "--version"]
    try:
        result = ctx.run(argv, timeout=_CODEX_TIMEOUT)
    except FileNotFoundError:
        return [Finding("codex-version", "warn", "warn", "could not determine Codex CLI version (codex not found)")]
    except PermissionError:
        return [Finding("codex-version", "warn", "warn", "could not determine Codex CLI version (codex not executable)")]
    except subprocess.TimeoutExpired:
        return [Finding("codex-version", "warn", "warn", "could not determine Codex CLI version (timed out)")]
    if result.returncode != 0:
        return [Finding("codex-version", "warn", "warn", "could not determine Codex CLI version")]
    match = re.search(r"\d+\.\d+\.\d+", result.stdout or "")
    if match is None:
        return [Finding("codex-version", "warn", "warn", "could not determine Codex CLI version (unparseable output)")]
    version = match.group(0)
    if version == CODEX_BASELINE:
        return [Finding("codex-version", "warn", "ok", f"Codex CLI {version} matches the verified baseline")]
    return [Finding("codex-version", "warn", "warn", f"Codex CLI {version} differs from the verified baseline {CODEX_BASELINE}")]


# --------------------------------------------------------------------------- info checks


def check_log_path(ctx: DoctorContext) -> list[Finding]:
    """info: the resolved JSONL request-log path (root override honored — ADR 0010)."""
    from chinamaxM.observability import resolve_log_path

    return [Finding("log-path", "info", "ok", f"request log: {resolve_log_path(ctx.claude_root)}")]


def check_linger(ctx: DoctorContext) -> list[Finding]:
    """info: the Linux linger state (PRESENT/ABSENT; off-Linux reports ABSENT)."""
    from chinamaxM.ops.supervision import linger_enabled

    state = "PRESENT" if linger_enabled() else "ABSENT"
    return [Finding("linger", "info", "ok", f"Linux linger: {state}")]


# --------------------------------------------------------------------------- engine

#: The roster: ``(id, level, category_phrase, scope, function)`` in report order.
#: ``category_phrase`` builds the content-free detail of an unexpected ``error`` Finding.
#: ``scope`` is the Host filter (ADR 0005 as amended 2026-08-18): ``"shared"`` checks run on
#: either Host (the two host-branching checks — drift and keys — are ``"shared"`` and scope
#: their OWN findings to ``ctx.host``); ``"claude"`` / ``"codex"`` checks run ONLY on that
#: Host, so the other Host's wiring never appears in the report.
_CHECKS: list[tuple[str, str, str, str, Callable[[DoctorContext], list[Finding]]]] = [
    ("registry", "fail", "registry parse/merge", "shared", check_registry),
    ("conda", "fail", "conda env probe", "shared", check_conda),
    ("service", "fail", "service status", "shared", check_service),
    ("port", "fail", "proxy port probe", "shared", check_port),
    ("env-flip", "fail", "settings env-flip read", "claude", check_env_flip),
    ("drift", "fail", "generated-agent drift", "shared", check_drift),
    ("codex-strict-config", "fail", "codex strict-config", "codex", check_strict_config),
    ("keys", "warn", "key-file roster", "shared", check_keys),
    ("subagent-model", "warn", "CLAUDE_CODE_SUBAGENT_MODEL", "claude", check_subagent_model),
    ("kimi-responses-bug", "warn", "kimi Responses scan", "shared", check_kimi),
    ("codex-version", "warn", "codex CLI version", "codex", check_codex_version),
    ("log-path", "info", "request-log path", "shared", check_log_path),
    ("linger", "info", "linger state", "shared", check_linger),
]


def _run_check(
    check: tuple[str, str, str, str, Callable[[DoctorContext], list[Finding]]], ctx: DoctorContext
) -> list[Finding]:
    """Run one check with isolation: an unexpected exception yields a content-free error."""
    check_id, level, phrase, _scope, func = check
    try:
        return func(ctx)
    except Exception as exc:  # noqa: BLE001 - never crash the engine, never leak content
        return [Finding(check_id, level, "error", f"{type(exc).__name__}: {phrase} check failed")]


def _exit_code(findings: list[Finding]) -> int:
    """Return nonzero iff any FAIL-level check observed ``fail`` or ``error`` (ADR 0005)."""
    for finding in findings:
        if finding.level == "fail" and finding.status in ("fail", "error"):
            return 1
    return 0


def run_doctor(
    *,
    host: str,
    claude_root: str | os.PathLike[str] | None = None,
    codex_root: str | os.PathLike[str] | None = None,
    overlay_path: str | os.PathLike[str] | None = None,
    run: Runner | None = None,
    port_probe: Callable[[int], bool] | None = None,
    service_status: Callable[[object], object] | None = None,
    scan_tree: str | os.PathLike[str] | None = None,
) -> tuple[list[Finding], int]:
    """Run the invoking Host's roster once and return ``(findings, exit_code)``.

    Host-scoped (ADR 0005 as amended 2026-08-18): shared-infrastructure checks always run;
    the invoking Host's wiring checks run; the other Host's checks are skipped entirely, so
    its wiring never appears. The Registry loads exactly once (via the canonical Overlay path
    under the resolved Claude root) and is passed to every check; a Registry failure makes
    registry-dependent checks report ``skipped (registry unreadable)`` while the env-flip
    PRESENCE sub-finding still reports. Every runner/probe/tree is injectable so tests drive
    each state hermetically.
    """
    claude = Path(claude_root) if claude_root is not None else resolve_host_root("claude")
    codex = Path(codex_root) if codex_root is not None else resolve_host_root("codex")
    overlay = Path(overlay_path) if overlay_path is not None else claude / "chinamaxM" / "profiles.json"
    run = run or _default_run
    if port_probe is None:
        from chinamaxM.ops.supervision import port_live

        port_probe = port_live
    service_status = service_status or _default_service_status
    tree = Path(scan_tree) if scan_tree is not None else _package_tree()

    try:
        registry: Registry | None = load_registry(overlay_path=overlay)
        registry_error: str | None = None
    except RegistryError as exc:
        registry = None
        registry_error = f"registry invalid: {exc}"
    except Exception as exc:  # noqa: BLE001 - any load fault is a diagnosable FAIL, not a crash
        registry = None
        registry_error = f"registry could not be loaded ({type(exc).__name__})"

    ctx = DoctorContext(host, claude, codex, registry, registry_error, run, port_probe, service_status, tree)
    findings: list[Finding] = []
    for check in _CHECKS:
        scope = check[3]
        if scope != "shared" and scope != host:
            continue
        findings.extend(_run_check(check, ctx))
    return findings, _exit_code(findings)


def render_report(findings: list[Finding]) -> str:
    """Render the findings as a plain-text report (info-level rows tagged ``INFO``)."""
    lines = ["chinamaxM doctor — read-only diagnosis", ""]
    for finding in findings:
        tag = "INFO" if finding.level == "info" else finding.status.upper()
        lines.append(f"[{tag}] {finding.id}: {finding.detail}")
    fails = [f for f in findings if f.level == "fail" and f.status in ("fail", "error")]
    lines.append("")
    if fails:
        lines.append(f"{len(fails)} FAIL finding(s) — run /chinamaxM:setup to repair the installation.")
    else:
        lines.append("No FAIL findings.")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- /profiles


def _key_present(root: Path, var: str) -> bool:
    """Return whether a Host Key file holds a non-empty value for ``var`` (never the value)."""
    return bool(KeyFileReader(Path(root) / KEY_FILE_NAME).get(var))


def render_profiles(
    *,
    host: str,
    claude_root: str | os.PathLike[str] | None = None,
    codex_root: str | os.PathLike[str] | None = None,
    overlay_path: str | os.PathLike[str] | None = None,
) -> tuple[str, int]:
    """Render the resolved Profiles roster for the invoking Host; never print a key value.

    Host-scoped (ADR 0005/0006 as amended 2026-08-18): each Profile lists ``default_model``
    (the generated artifact always pins it — ADR 0004 as amended 2026-08-19; a per-dispatch
    override rides the Dispatch marker and is never on-disk state), ``dialect``, key
    variable name, and PRESENT/MISSING for the invoking Host's Key file ONLY — the other
    Host's file never appears. An unreadable Registry prints one error line and exits
    nonzero.
    """
    claude = Path(claude_root) if claude_root is not None else resolve_host_root("claude")
    codex = Path(codex_root) if codex_root is not None else resolve_host_root("codex")
    overlay = Path(overlay_path) if overlay_path is not None else claude / "chinamaxM" / "profiles.json"
    root = claude if host == "claude" else codex

    try:
        registry = load_registry(overlay_path=overlay)
    except RegistryError as exc:
        return f"chinamaxM profiles: registry unreadable — {exc}\n", 1
    except Exception as exc:  # noqa: BLE001
        return f"chinamaxM profiles: registry unreadable ({type(exc).__name__})\n", 1

    lines = ["chinamaxM profiles", ""]
    for profile in registry.profiles.values():
        lines.append(profile.name)
        lines.append(f"  default model: {profile.default_model}")
        lines.append(f"  dialect: {profile.dialect}")
        lines.append(f"  key variable: {profile.api_key_env}")
        lines.append(f"    {host} key: {'PRESENT' if _key_present(root, profile.api_key_env) else 'MISSING'}")
        lines.append("")
    return "\n".join(lines) + "\n", 0


# --------------------------------------------------------------------------- CLI entry


def main(argv: list[str] | None = None) -> int:
    """Run the invoking Host's roster, or render ``--profiles``; return the process exit code.

    The Host is resolved by the shared ladder (ADR 0005 as amended 2026-08-18): ``--host``
    → ``CHINAMAXM_HOST`` → plugin env evidence → a hard error (stderr + exit 2).
    """
    parser = argparse.ArgumentParser(
        prog="python -m chinamaxM.doctor",
        description="chinamaxM read-only diagnosis (Host-scoped).",
    )
    parser.add_argument("--profiles", action="store_true", help="render the Profiles roster instead")
    parser.add_argument(
        "--host", default=None,
        help="claude or codex; defaults to the CHINAMAXM_HOST marker / plugin env evidence",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        host = resolve_host(args.host)
    except HostResolutionError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2
    if args.profiles:
        text, code = render_profiles(host=host)
        sys.stdout.write(text)
        return code
    findings, code = run_doctor(host=host)
    sys.stdout.write(render_report(findings))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
