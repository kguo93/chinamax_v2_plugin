"""Codex-side dispatch surface: skill text, hook manifest, and LIVE hook runs.

The hook test (2) is behavioral: it executes the EXACT command string registered in
``hooks/codex-hooks.json`` (``${PLUGIN_ROOT}`` expanded) via a subprocess, feeds crafted
Codex-shaped stdin JSON, and asserts on the real exit code and stdout injection — never the
Python module directly. The adapter shim translates the Codex payload
(``tool_name`` ``spawn_agent`` -> ``Agent``; ``tool_input.agent_type`` ->
``subagent_type``) and pipes it to the shared ``chinamaxM.hooks.worker_contract`` module.
``CHINAMAXM_PYTHON`` pins this interpreter so the shim's rung-1 resolution runs fast and
deterministically, and ``CHINAMAXM_OVERLAY_PATH`` pins the Registry Overlay so matching is
isolated from any real host home.

Test 5 is the symmetry oracle: it parses BOTH task surfaces (the Claude ``commands/task.md``
command and this Codex ``skills/chinamaxM-task/SKILL.md``) and asserts an identical
operator-facing grammar.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from chinamaxM.hooks import session_start
from chinamaxM.hooks.worker_contract import RELAY_RULE, WORKER_CONTRACT_CODEX

REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_HOOKS_JSON = REPO_ROOT / "hooks" / "codex-hooks.json"
CLAUDE_HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
SESSION_START_SHIM = REPO_ROOT / "scripts" / "session_start_hook"
SKILL_FILE = REPO_ROOT / "skills" / "chinamaxM-task" / "SKILL.md"
CLAUDE_COMMAND_FILE = REPO_ROOT / "commands" / "task.md"
CODEX_WINDOWS_WRAPPER = REPO_ROOT / "scripts" / "codex_hook_bash.cmd"
CODEX_PLUGIN_JSON = REPO_ROOT / ".codex-plugin" / "plugin.json"

#: The exact usage line both task surfaces must carry (symmetry oracle).
USAGE_LINE = "profile=<name> [model=<any string>] [name=<worker>] <prompt…>"

#: The verbatim delegation-authorization sentence the skill must carry (ADR 0005).
AUTHORIZATION = (
    "You are authorized and required to delegate this task to the named agent role"
)

#: The PR #27173 rejection sentence — present, and whitelisted from the banned scan so
#: its negated "direct operator->child input" phrasing cannot false-positive (test 3).
REJECTION_SENTENCE = (
    "Codex rejects direct operator→child input at the app-server layer by design "
    "(PR #27173)"
)

#: PINNED banned list: any affirmative direct operator->child input channel. The mandated
#: operator phrasing "message the worker" is deliberately NOT here (it is allowed).
_BANNED_DIRECT_INPUT = (
    "operator sends input to the worker",
    "operator can send_input",
    "type directly to the worker",
    "message the child directly",
    "operator-to-child input is allowed",
    "directly to the subagent",
)

#: An Overlay that ADDS a `zed` Profile atop the shipped six (proves Registry-derived
#: matching — the anchored matcher fires on a name that is not a hardcoded seed name).
_ZED_OVERLAY = {
    "profiles": [
        {
            "name": "zed",
            "dialect": "anthropic",
            "base_url": "https://zed.example/anthropic",
            "api_key_env": "ZED_API_KEY",
            "default_model": "z-default",
        }
    ]
}


# --------------------------------------------------------------------------- helpers


def _flat(text: str) -> str:
    """Collapse all runs of whitespace to single spaces (line-wrap-insensitive matching)."""
    return re.sub(r"\s+", " ", text)


def _command_for(event_name: str) -> str:
    """Return the single registered command string for a Codex hook event."""
    hooks = json.loads(CODEX_HOOKS_JSON.read_text(encoding="utf-8"))
    groups = hooks["hooks"][event_name]
    assert len(groups) == 1, groups
    assert len(groups[0]["hooks"]) == 1, groups
    return groups[0]["hooks"][0]["command"]


def _run(command: str, stdin_text: str, *, overlay_path: str) -> subprocess.CompletedProcess:
    """Run a registered command string against crafted stdin with a pinned interpreter."""
    env = os.environ.copy()
    env["PLUGIN_ROOT"] = str(REPO_ROOT)
    env["CHINAMAXM_PYTHON"] = sys.executable
    env["CHINAMAXM_OVERLAY_PATH"] = overlay_path
    return subprocess.run(
        command,
        shell=True,
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _injected(result: subprocess.CompletedProcess) -> dict:
    """Parse the ``hookSpecificOutput`` object from a hook's stdout."""
    payload = json.loads(result.stdout)
    return payload["hookSpecificOutput"]


def _subagent_event(agent_type: str) -> str:
    """A Codex SubagentStart payload (role identity rides top-level ``agent_type``)."""
    return json.dumps(
        {"hook_event_name": "SubagentStart", "agent_id": "t1", "agent_type": agent_type}
    )


def _spawn_event(agent_type: str) -> str:
    """A Codex PreToolUse(spawn_agent) payload (role rides ``tool_input.agent_type``)."""
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_input": {"agent_type": agent_type},
        }
    )


@pytest.fixture
def missing_overlay(tmp_path) -> str:
    """A nonexistent Overlay path ⇒ the shipped six Profiles only (deterministic)."""
    return str(tmp_path / "no-such-overlay.json")


@pytest.fixture
def zed_overlay(tmp_path) -> str:
    """Write and return the path of an Overlay adding the `zed` Profile."""
    path = tmp_path / "overlay.json"
    path.write_text(json.dumps(_ZED_OVERLAY), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------- test 1 (skill text)


def test_codex_skill_delegation_language():
    """AC-1: the skill pins delegation authorization, role selection, and stdin close."""
    text = SKILL_FILE.read_text(encoding="utf-8")
    flat = _flat(text)
    lower = text.lower()

    # Frontmatter pins name + description with the operator-invocable posture (the old
    # adapter's `user-invocable: false` is NOT copied).
    assert "name: chinamaxM-task" in text
    assert re.search(r"(?m)^description:\s+\S", text)
    assert "user-invocable" not in lower

    # The verbatim delegation-authorization sentence.
    assert AUTHORIZATION in flat

    # Usage grammar + no default profile.
    assert USAGE_LINE in text
    assert "no default profile" in lower

    # Role selection: explicit model ⇒ the codex rewrite helper; never validated.
    assert "never validated" in lower
    assert '"<plugin-checkout>/scripts/chinamaxM" set_model codex <profile> <model>' in text

    # Headless stdin-close guidance.
    assert "< /dev/null" in text

    # Missing-role error names the valid Profile list (never a model list).
    assert "valid Profile list" in text

    # Provider-side failure relays verbatim as the Worker's error.
    assert "verbatim as the Worker's error" in flat

    # No model OR reasoning-effort override at spawn.
    assert "never pass a" in lower
    assert "reasoning-effort" in lower

    # Negative: no reversed-semantics model-refusal vocabulary.
    assert "unlisted" not in lower
    assert "valid model list" not in lower

    # The Relay-rule first copy is verbatim-equal to the hook module's canonical constant.
    assert RELAY_RULE in text


# ----------------------------------------------------------- test 2 (live hook, both branches)


def test_contract_in_codex_manifest(zed_overlay, missing_overlay, tmp_path):
    """AC-2 (LIVE): the registered shim injects the contract and Relay rule, fails open."""
    # The manifest registers the adapter shim, not the bare module.
    assert "codex_worker_contract_hook" in _command_for("SubagentStart")
    assert "codex_worker_contract_hook" in _command_for("PreToolUse")

    # --- SubagentStart: worker roles get the Codex contract (Registry-derived) ---
    # The Codex shim passes host `codex`, so the injected text is WORKER_CONTRACT_CODEX —
    # duties 1–4 with NO SendMessage item (the Codex parent pulls the report; ADR 0007
    # amended 2026-08-18). Names carry the canonical `chinamaxm-<profile>-` prefix.
    subagent_cmd = _command_for("SubagentStart")
    for agent_type in ("deepseek", "chinamaxm-deepseek-repo-summary", "chinamaxm-zed-x"):
        result = _run(subagent_cmd, _subagent_event(agent_type), overlay_path=zed_overlay)
        assert result.returncode == 0, result.stderr
        out = _injected(result)
        assert out["hookEventName"] == "SubagentStart"
        assert out["additionalContext"] == WORKER_CONTRACT_CODEX
        for number in ("1.", "2.", "3.", "4."):
            assert number in out["additionalContext"]
        # No push item on Codex — the parent pulls the report from the rollout.
        assert "5." not in out["additionalContext"]
        assert "SendMessage" not in out["additionalContext"]

    # Silent cases: anchored trap (`kimono` vs profile `kimi`), a built-in role, the RETIRED
    # legacy instance form (`deepseek-1`), and the anchored trap on the new prefix.
    for agent_type in ("kimono", "Explore", "deepseek-1", "chinamaxm-kimono-x"):
        result = _run(subagent_cmd, _subagent_event(agent_type), overlay_path=missing_overlay)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "", result.stdout

    # --- PreToolUse(spawn_agent): the parent gets the verbatim Relay rule ---
    spawn_cmd = _command_for("PreToolUse")
    result = _run(spawn_cmd, _spawn_event("deepseek"), overlay_path=missing_overlay)
    assert result.returncode == 0, result.stderr
    out = _injected(result)
    assert out["hookEventName"] == "PreToolUse"
    context = out["additionalContext"]
    assert context == RELAY_RULE
    assert "verbatim" in context
    assert "no attribution" in context
    assert "as your own output" in context

    # A non-worker role on the spawn call ⇒ silent.
    result = _run(spawn_cmd, _spawn_event("Explore"), overlay_path=missing_overlay)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", result.stdout

    # --- Fail-open: malformed stdin and a malformed Registry ⇒ exit 0, no injection ---
    result = _run(subagent_cmd, "{ not valid json", overlay_path=missing_overlay)
    assert result.returncode == 0
    assert result.stdout.strip() == "", result.stdout
    assert result.stderr.strip() != ""

    bad_overlay = tmp_path / "bad.json"
    bad_overlay.write_text("{ this is not json", encoding="utf-8")
    result = _run(subagent_cmd, _subagent_event("deepseek"), overlay_path=str(bad_overlay))
    assert result.returncode == 0
    assert result.stdout.strip() == "", result.stdout
    assert result.stderr.strip() != ""


# --------------------------------------------------------- test 2b (collect = pull)


def test_codex_collect_report_is_pull():
    """AC (delivery, ADR 0007 amended 2026-08-18): the Codex parent PULLS the Worker's final
    report from the child rollout and NEVER asks it to restate.

    Codex children cannot push (PR #27173), so — unlike the Claude push directive locked by
    ``test_task_command_text_invariants`` — the skill must tell the parent where to read the
    report and forbid a token-spending, paraphrase-risking restatement.
    """
    flat = _flat(SKILL_FILE.read_text(encoding="utf-8"))

    # The pull section names the rollout as the source of the final message.
    assert "Collect the report" in flat
    assert "rollout JSONL" in flat
    assert "$CODEX_HOME/sessions/" in flat

    # The restate-via-followup_task ban protects the verbatim guarantee.
    assert "followup_task" in flat
    assert "restate its report" in flat


# ------------------------------------------------------------------ test 3 (steering)


def test_steering_parent_mediated_only():
    """AC-3: parent mediation only — send_input/followup_task, no direct operator→child path."""
    text = SKILL_FILE.read_text(encoding="utf-8")
    flat = _flat(text)
    lower = flat.lower()

    # The two mediation primitives, and interrupt semantics.
    assert "send_input" in text
    assert "interrupt=true" in text
    assert "followup_task" in text

    # ONLY those two are named — the historical `send_message` verb never appears.
    assert "send_message" not in text

    # The mandated operator phrasing "message the worker" is allowed and present.
    assert "message the worker" in lower

    # The PR #27173 rejection sentence is present and whitelisted from the banned scan.
    assert REJECTION_SENTENCE in flat
    scanned = lower.replace(REJECTION_SENTENCE.lower(), "")
    for phrase in _BANNED_DIRECT_INPUT:
        assert phrase not in scanned, phrase


# --------------------------------------------------------------- test 4 (resume gotchas)


def test_resume_gotchas_present():
    """AC-4: every ADR 0008 resume gotcha is encoded by a pinned key phrase."""
    flat = _flat(SKILL_FILE.read_text(encoding="utf-8"))

    assert "-c model=" in flat  # pin the recorded model on resume
    assert "-c model_provider=" in flat  # pin the provider on resume
    assert "poisons the session's recorded model" in flat  # poisoned-session warning
    assert "resume_agent" in flat and "emits no event" in flat  # no resume event
    assert "-c sandbox_mode=" in flat and "--sandbox" in flat  # the sandbox_mode form


# ------------------------------------------------------------------ test 5 (symmetry)


def test_surface_symmetry_with_claude():
    """AC-5: the Codex skill and Claude command share the operator-facing grammar."""
    skill = SKILL_FILE.read_text(encoding="utf-8")
    command = CLAUDE_COMMAND_FILE.read_text(encoding="utf-8")

    for text in (skill, command):
        lower = text.lower()
        # Identical argument names via the shared usage line.
        assert USAGE_LINE in text
        # No default profile.
        assert "no default profile" in lower
        # Model never validated; provider errors relay verbatim.
        assert "never validated" in lower
        assert "verbatim as the Worker's error" in _flat(text)
        # The shared rewrite helper via the Launcher (host token differs per surface).
        assert '/scripts/chinamaxM" set_model' in text
        # Default and custom name rules — the canonical `chinamaxm-<profile>-` grammar.
        assert "chinamaxm-<profile>-<n>" in text
        assert "chinamaxm-<profile>-" in text
        assert "non-empty suffix" in lower
        # The verbatim Relay rule, identical on both.
        assert RELAY_RULE in text

    # The rewrite helper differs only in the host token — proving surface symmetry, not
    # a shared host.
    assert '"<plugin-checkout>/scripts/chinamaxM" set_model codex <profile> <model>' in skill
    assert '"${CLAUDE_PLUGIN_ROOT}/scripts/chinamaxM" set_model claude <profile> <model>' in command


# ------------------------------------------------------------- test 6 (manifest registration)


def test_codex_manifest_registered():
    """AC-6: skills + the required hooks key registered, with live, expandable shims."""
    plugin = json.loads(CODEX_PLUGIN_JSON.read_text(encoding="utf-8"))
    assert plugin["skills"] == "./skills"
    # Real Codex auto-discovers only the fixed default `hooks/hooks.json`, so the manifest MUST
    # name the Codex-flavored hook file explicitly (ADR 0012 amended 2026-08-14). The bundled
    # plugin-creator validator rejects the key but is not the authority — real Codex is.
    assert plugin["hooks"] == "./hooks/codex-hooks.json"

    # Resolve the declared hook manifest.
    hooks_path = CODEX_HOOKS_JSON
    assert hooks_path.exists(), hooks_path
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    events = hooks["hooks"]
    assert {"SubagentStart", "PreToolUse"} <= set(events)

    # SubagentStart carries NO static matcher; PreToolUse matches spawn_agent ONLY.
    assert "matcher" not in events["SubagentStart"][0]
    assert events["PreToolUse"][0]["matcher"] == "spawn_agent"

    for event_name in ("SubagentStart", "PreToolUse"):
        for group in events[event_name]:
            for hook in group["hooks"]:
                assert hook["type"] == "command"
                assert hook["timeout"] == 10

                # POSIX command: expand plugin-root, assert the shim exists + is executable.
                posix = (
                    hook["command"]
                    .replace("${PLUGIN_ROOT}", str(REPO_ROOT))
                    .strip()
                    .strip('"')
                )
                path = Path(posix)
                assert path.exists(), path
                assert os.access(path, os.X_OK), path

                # commandWindows present; the referenced Git-Bash wrapper exists (textual).
                cw = hook["commandWindows"]
                assert "codex_hook_bash.cmd" in cw
                assert CODEX_WINDOWS_WRAPPER.exists(), CODEX_WINDOWS_WRAPPER


# ---------------------------------------------------- test 7 (Codex SessionStart registration)


def test_codex_sessionstart_registered():
    """Codex now registers a warn-only SessionStart with the Git-Bash commandWindows shim."""
    hooks = json.loads(CODEX_HOOKS_JSON.read_text(encoding="utf-8"))
    groups = hooks["hooks"]["SessionStart"]
    assert len(groups) == 1 and len(groups[0]["hooks"]) == 1
    group = groups[0]
    hook = group["hooks"][0]

    # The pinned event matcher, timeout, and the POSIX shim (exists + executable).
    assert group["matcher"] == "startup|resume|clear|compact|fork"
    assert hook["type"] == "command" and hook["timeout"] == 10
    posix = hook["command"].replace("${PLUGIN_ROOT}", str(REPO_ROOT)).strip().strip('"')
    assert Path(posix) == SESSION_START_SHIM
    assert SESSION_START_SHIM.exists() and os.access(SESSION_START_SHIM, os.X_OK)

    # commandWindows enters Git Bash via the shared .cmd wrapper, execing the same shim basename.
    cw = hook["commandWindows"]
    assert "codex_hook_bash.cmd" in cw and "session_start_hook" in cw
    assert CODEX_WINDOWS_WRAPPER.exists(), CODEX_WINDOWS_WRAPPER


def test_codex_hook_bash_cmd_resolves_git_tree_only():
    """The Git-Bash shim resolves from the Git for Windows install roots only — no PATH fallback.

    Consistency guard: the doctor's Windows `bash` prerequisite is detected tree-only (a PATH
    bash is WSL's, not Git Bash), and this shim — the bash "actually being used" by the hooks —
    must resolve the SAME way, so the doctor never green-lights a bash the shim cannot run.
    """
    text = CODEX_WINDOWS_WRAPPER.read_text(encoding="utf-8")
    # Resolves Git Bash from the standard Git for Windows roots (system-wide + per-user), via
    # the intermediate %R% root variable (`set "R=%ProgramFiles%"` → `%R%\Git\bin\bash.exe`).
    assert 'set "R=%ProgramFiles%"' in text
    assert 'set "R=%LOCALAPPDATA%"' in text
    assert r"%R%\Git\bin\bash.exe" in text
    assert r"%R%\Programs\Git\bin\bash.exe" in text
    # No PATH fallback: no ACTIVE (non-`rem`-comment) line invokes `where bash` (a PATH bash
    # cannot be assumed Git Bash). The explanatory comment may still name it.
    active_lines = [ln for ln in text.splitlines() if not ln.strip().startswith("rem")]
    assert not any("where bash" in ln for ln in active_lines)
    # A missing Git Bash is a hard error, never a silent PATH pick.
    assert "exit /b 127" in text


def test_sessionstart_symmetry_claude_codex():
    """Claude and Codex register SessionStart with the SAME matcher and the SAME POSIX shim."""
    claude = json.loads(CLAUDE_HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]
    codex = json.loads(CODEX_HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]

    assert claude["matcher"] == codex["matcher"] == "startup|resume|clear|compact|fork"

    def _shim(entry, var):
        return entry["hooks"][0]["command"].replace(var, str(REPO_ROOT)).strip().strip('"')

    # Both resolve to the identical scripts/session_start_hook (one host-aware module).
    assert _shim(claude, "${CLAUDE_PLUGIN_ROOT}") == _shim(codex, "${PLUGIN_ROOT}") == str(SESSION_START_SHIM)
    # Only Codex carries the Git-Bash commandWindows shim (Claude runs the POSIX shim directly).
    assert "commandWindows" in codex["hooks"][0]
    assert "commandWindows" not in claude["hooks"][0]


# ------------------------------------------------ test 8 (Codex SessionStart LIVE, host-aware)


def _codex_overlay(claude_home: Path, port: int) -> None:
    """Pin the Registry port via a CHINAMAXM_CLAUDE_HOME overlay (deterministic port)."""
    (claude_home / "chinamaxM").mkdir(parents=True, exist_ok=True)
    (claude_home / "chinamaxM" / "profiles.json").write_text(
        json.dumps({"port": port}), encoding="utf-8"
    )


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run_codex_session_start(codex_root: Path, claude_home: Path) -> subprocess.CompletedProcess:
    """Run the registered Codex SessionStart command with a Codex-shaped environment."""
    hooks = json.loads(CODEX_HOOKS_JSON.read_text(encoding="utf-8"))
    command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"].replace(
        "${PLUGIN_ROOT}", str(REPO_ROOT)
    )
    env = os.environ.copy()
    # Codex environment: PLUGIN_ROOT (not CLAUDE_PLUGIN_ROOT) so the module resolves the Codex Host.
    for key in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_CONFIG_DIR"):
        env.pop(key, None)
    env["PLUGIN_ROOT"] = str(REPO_ROOT)
    env["CHINAMAXM_PYTHON"] = sys.executable
    env["CHINAMAXM_CODEX_HOME"] = str(codex_root)
    env["CHINAMAXM_CLAUDE_HOME"] = str(claude_home)  # pins the Registry overlay (Proxy port)
    return subprocess.run(
        command,
        shell=True,
        input='{"hook_event_name":"SessionStart"}',
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_codex_sessionstart_live(tmp_path):
    """AC (LIVE): warn iff chinamaxM is Codex-configured AND the Proxy port is dead; else silent."""
    provider_entry = (
        '[model_providers.chinamaxM-deepseek]\n'
        'name = "chinamaxM-deepseek"\n'
        'base_url = "http://127.0.0.1:9/openai/deepseek"\n'
        'wire_api = "responses"\n'
    )

    # Configured for Codex + a dead Proxy port ⇒ warn, pointing at the chinamaxM-doctor SKILL.
    configured = tmp_path / "codex-configured"
    configured.mkdir()
    (configured / "config.toml").write_text(provider_entry, encoding="utf-8")
    dead_home = tmp_path / "home-dead"
    _codex_overlay(dead_home, _free_port())  # nothing listening ⇒ dead
    result = _run_codex_session_start(configured, dead_home)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "chinamaxM-doctor" in payload["systemMessage"]
    assert "/chinamaxM:doctor" not in payload["systemMessage"]  # the Codex skill, not the command

    # NOT configured for Codex (no chinamaxM- provider entry) ⇒ silent, even with a dead port.
    unconfigured = tmp_path / "codex-plain"
    unconfigured.mkdir()
    (unconfigured / "config.toml").write_text('[model_providers.openai]\nname = "openai"\n', encoding="utf-8")
    result = _run_codex_session_start(unconfigured, tmp_path / "home-plain")
    assert result.returncode == 0 and result.stdout.strip() == "", result.stdout

    # No config.toml at all ⇒ silent (chinamaxM is not wired into Codex).
    empty = tmp_path / "codex-empty"
    empty.mkdir()
    result = _run_codex_session_start(empty, tmp_path / "home-empty")
    assert result.returncode == 0 and result.stdout.strip() == "", result.stdout

    # Configured + a LIVE Proxy port ⇒ silent.
    live_home = tmp_path / "home-live"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    _codex_overlay(live_home, listener.getsockname()[1])
    try:
        result = _run_codex_session_start(configured, live_home)
    finally:
        listener.close()
    assert result.returncode == 0 and result.stdout.strip() == "", result.stdout


# --------------------------------------------- test 9 (SessionStart Host-resolution ladder)


def test_sessionstart_resolve_host_ladder(monkeypatch):
    """The hook resolves the Host codex-first; unresolvable ⇒ None (silent fail-open, no guess)."""
    # Codex evidence outranks Claude aliases (Codex exposes Claude-compatible aliases) — this
    # is the fix for the old claude-first default that misread a Codex session as Claude.
    monkeypatch.setattr(os, "environ", {"PLUGIN_ROOT": "/p", "CLAUDE_PLUGIN_ROOT": "/c"})
    assert session_start._resolve_host() == "codex"
    monkeypatch.setattr(os, "environ", {"CODEX_HOME": "/x", "CLAUDE_CONFIG_DIR": "/c"})
    assert session_start._resolve_host() == "codex"
    # Claude-only evidence ⇒ claude.
    monkeypatch.setattr(os, "environ", {"CLAUDE_CONFIG_DIR": "/c"})
    assert session_start._resolve_host() == "claude"
    # No evidence at all ⇒ None (never defaults to claude).
    monkeypatch.setattr(os, "environ", {})
    assert session_start._resolve_host() is None


def test_sessionstart_main_silent_when_unresolvable(monkeypatch, capsys):
    """main() exits 0 with NO output when the Host cannot be resolved (fail-open)."""
    monkeypatch.setattr(os, "environ", {})
    assert session_start.main() == 0
    assert capsys.readouterr().out == ""


def test_codex_sessionstart_codex_first_live(tmp_path):
    """LIVE: with BOTH Codex evidence and Claude aliases set, the hook warns via the CODEX path."""
    configured = tmp_path / "codex-cfg"
    configured.mkdir()
    (configured / "config.toml").write_text(
        '[model_providers.chinamaxM-deepseek]\n'
        'name = "chinamaxM-deepseek"\n'
        'base_url = "http://127.0.0.1:9/openai/deepseek"\n'
        'wire_api = "responses"\n',
        encoding="utf-8",
    )
    dead_home = tmp_path / "home"
    _codex_overlay(dead_home, _free_port())

    hooks = json.loads(CODEX_HOOKS_JSON.read_text(encoding="utf-8"))
    command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"].replace(
        "${PLUGIN_ROOT}", str(REPO_ROOT)
    )
    env = os.environ.copy()
    env.pop("CHINAMAXM_HOST", None)
    env["PLUGIN_ROOT"] = str(REPO_ROOT)  # Codex evidence
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)  # Claude alias ALSO present — must be outranked
    env["CHINAMAXM_PYTHON"] = sys.executable
    env["CHINAMAXM_CODEX_HOME"] = str(configured)
    env["CHINAMAXM_CLAUDE_HOME"] = str(dead_home)
    result = subprocess.run(
        command, shell=True, input='{"hook_event_name":"SessionStart"}',
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "chinamaxM-doctor" in payload["systemMessage"]  # the Codex warning path fired
