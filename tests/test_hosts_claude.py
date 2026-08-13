"""Claude-side dispatch surface: command text, hook registration, and LIVE hook runs.

The hook tests (2, 3, 4, 8) are behavioral: they execute the EXACT command string
registered in ``hooks/hooks.json`` (``${CLAUDE_PLUGIN_ROOT}`` expanded) via a subprocess,
feed crafted stdin JSON, and assert on the real exit code and stdout injection — never the
Python module directly. ``CHINAMAXM_PYTHON`` is pinned to this interpreter so the shim's
rung-1 resolution runs fast and deterministically, and ``CHINAMAXM_OVERLAY_PATH`` pins the
Registry Overlay so matching is isolated from any real ``~/.claude``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from chinamaxM.hooks.worker_contract import RELAY_RULE, WORKER_CONTRACT

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
COMMAND_FILE = REPO_ROOT / "commands" / "task.md"
SHIM = REPO_ROOT / "scripts" / "worker_contract_hook"

#: The exact usage line the command must carry (frontmatter hint + body).
USAGE_LINE = "profile=<name> [model=<any string>] [name=<worker>] <prompt…>"

#: Known Claude Code hook event names — the registration allowlist (test 7).
_EVENT_ALLOWLIST = frozenset(
    {
        "SessionStart",
        "SessionEnd",
        "Stop",
        "SubagentStart",
        "SubagentStop",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Notification",
        "PreCompact",
    }
)

#: Resume verbs that must never surface as a usage-line option or a command/skill file.
_RESUME_VERBS = ("resume", "reattach", "recover", "reseed", "re-seed")

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


def _command_for(event_name: str) -> str:
    """Return the single registered command string for a hook event."""
    hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    groups = hooks["hooks"][event_name]
    assert len(groups) == 1, groups
    assert len(groups[0]["hooks"]) == 1, groups
    return groups[0]["hooks"][0]["command"]


def _run(command: str, stdin_text: str, *, overlay_path: str) -> subprocess.CompletedProcess:
    """Run a registered command string against crafted stdin with a pinned interpreter."""
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)
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


# ------------------------------------------------------------------- test 1 (command)


def test_task_command_text_invariants():
    """AC-1: the command file pins every dispatch rule and the verbatim Relay first copy."""
    text = COMMAND_FILE.read_text(encoding="utf-8")
    lower = text.lower()

    # Frontmatter: argument-hint, allowed-tools keeping Agent AND Bash, $ARGUMENTS.
    assert "argument-hint:" in text
    allowed = next(line for line in text.splitlines() if line.startswith("allowed-tools:"))
    assert "Agent" in allowed and "Bash" in allowed
    assert "$ARGUMENTS" in text

    # The exact usage line, and the no-default-profile rule.
    assert USAGE_LINE in text
    assert "no default profile" in lower

    # Unknown-PROFILE error names the valid Profile list (never a model list).
    assert "valid Profile list" in text

    # Model pass-through: never validated, rewrite-helper invocation, verbatim provider error.
    assert "never validated" in lower
    assert "python -m chinamaxM.set_model claude <profile> <model>" in text
    assert "verbatim as the Worker's error" in text

    # Named background spawn: default `<profile>-<n>`; custom name `<profile>-` + non-empty.
    assert "<profile>-<n>" in text
    assert "<profile>-" in text
    assert "non-empty suffix" in lower

    # No spawn-time model param, with the enum-lock + resolution-order reason.
    assert "never pass a" in lower
    assert "enum-lock" in lower
    assert "resolution order" in lower

    # The Relay-rule first copy is verbatim-equal to the hook module's canonical constant.
    assert RELAY_RULE in text

    # Negative: no reversed-semantics model-refusal vocabulary.
    assert "unlisted" not in lower
    assert "valid model list" not in lower


# ------------------------------------------------------------- test 2 (contract inject)


def test_contract_hook_injects_on_worker_spawn(zed_overlay):
    """AC-2 (LIVE): SubagentStart injects the numbered contract for generated Workers."""
    command = _command_for("SubagentStart")
    for agent_type in ("deepseek", "deepseek-1", "zed-1"):
        event = {"hook_event_name": "SubagentStart", "agent_id": "x", "agent_type": agent_type}
        result = _run(command, json.dumps(event), overlay_path=zed_overlay)
        assert result.returncode == 0, result.stderr
        out = _injected(result)
        assert out["hookEventName"] == "SubagentStart"
        context = out["additionalContext"]
        assert context == WORKER_CONTRACT
        for number in ("1.", "2.", "3.", "4."):
            assert number in context


# -------------------------------------------------------------- test 3 (non-workers)


def test_contract_hook_ignores_non_workers(missing_overlay):
    """AC-2 (LIVE): non-Worker SubagentStart events inject nothing (anchored matching)."""
    command = _command_for("SubagentStart")
    # Explore is a built-in; `glmax` starts with profile `glm` but not `glm-`; `akimi`
    # contains `kimi` but is not `kimi-` prefixed — all discriminate anchored from substring.
    for agent_type in ("Explore", "glmax", "akimi"):
        event = {"hook_event_name": "SubagentStart", "agent_id": "x", "agent_type": agent_type}
        result = _run(command, json.dumps(event), overlay_path=missing_overlay)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "", result.stdout


# ----------------------------------------------------------------- test 4 (relay rule)


def test_relay_rule_injected_at_spawn(missing_overlay):
    """AC-3 (LIVE): PreToolUse(Agent) injects the verbatim Relay rule for a Worker spawn."""
    command = _command_for("PreToolUse")

    worker = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "deepseek", "name": "deepseek-1", "prompt": "x"},
    }
    result = _run(command, json.dumps(worker), overlay_path=missing_overlay)
    assert result.returncode == 0, result.stderr
    out = _injected(result)
    assert out["hookEventName"] == "PreToolUse"
    context = out["additionalContext"]
    assert context == RELAY_RULE
    assert "verbatim" in context
    assert "no attribution" in context
    assert "as your own output" in context

    # A non-Worker subagent_type ⇒ silent.
    explore = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "Explore", "prompt": "x"},
    }
    result = _run(command, json.dumps(explore), overlay_path=missing_overlay)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", result.stdout

    # A non-Agent tool_name carrying a subagent_type ⇒ silent (module-level gate).
    other = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"subagent_type": "deepseek", "command": "echo hi"},
    }
    result = _run(command, json.dumps(other), overlay_path=missing_overlay)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", result.stdout


# ------------------------------------------------------------- test 5 (token-lean text)


def test_contract_token_lean():
    """AC-4: the injected contract is ≤ 120 words with every rule numbered."""
    numbers = re.findall(r"(?m)^(\d+)\.", WORKER_CONTRACT)
    assert numbers == ["1", "2", "3", "4"]
    assert len(WORKER_CONTRACT.split()) <= 120


# ------------------------------------------------------------------- test 6 (steering)


def test_steer_documented_no_resume_machinery():
    """AC-5: steer/continue via the Worker name; dead-session resume refused, no machinery."""
    text = COMMAND_FILE.read_text(encoding="utf-8")
    lower = text.lower()

    # Steer and continue by messaging the Worker's name.
    assert "SendMessage" in text
    assert "steer" in lower and "continue" in lower

    # The dead-session refusal is present (it MAY name the dissolved resume verb).
    assert "dead-session recovery is refused" in lower
    assert "no resume machinery" in lower

    # SHAPE assertions: the usage line carries no resume verb ...
    usage = next(
        line for line in text.splitlines() if USAGE_LINE in line and line.strip().startswith("`")
    )
    assert all(verb not in usage.lower() for verb in _RESUME_VERBS)

    # ... and no command/skill file implements a resume verb.
    surface_files: list[Path] = []
    for directory in (REPO_ROOT / "commands", REPO_ROOT / "skills"):
        if directory.is_dir():
            surface_files.extend(p for p in directory.rglob("*") if p.is_file())
    for path in surface_files:
        assert all(verb not in path.stem.lower() for verb in _RESUME_VERBS), path


# ---------------------------------------------------------------- test 7 (registration)


def test_hooks_registered():
    """AC-6: hooks.json registers the two events with exact matcher/timeout and live shims."""
    hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    events = set(hooks["hooks"])
    assert events <= _EVENT_ALLOWLIST, events
    assert {"SubagentStart", "PreToolUse"} <= events

    # SubagentStart carries NO static matcher (Overlay Profiles are dynamic).
    subagent_group = hooks["hooks"]["SubagentStart"][0]
    assert "matcher" not in subagent_group

    # PreToolUse's matcher is exactly "Agent".
    assert hooks["hooks"]["PreToolUse"][0]["matcher"] == "Agent"

    # Both entries pin timeout 10; every referenced script exists, is executable, and
    # resolves after plugin-root expansion.
    for event_name in ("SubagentStart", "PreToolUse"):
        for group in hooks["hooks"][event_name]:
            for hook in group["hooks"]:
                assert hook["type"] == "command"
                assert hook["timeout"] == 10
                path = Path(
                    hook["command"]
                    .replace("${CLAUDE_PLUGIN_ROOT}", str(REPO_ROOT))
                    .strip()
                    .strip('"')
                )
                assert path.exists(), path
                assert os.access(path, os.X_OK), path


# ------------------------------------------------------------------- test 8 (fail-open)


def test_hook_fail_open(tmp_path, missing_overlay):
    """AC-2/AC-3 (LIVE): malformed stdin or a malformed Registry ⇒ exit 0, no injection."""
    command = _command_for("SubagentStart")

    # Malformed stdin JSON.
    result = _run(command, "{ not valid json", overlay_path=missing_overlay)
    assert result.returncode == 0
    assert result.stdout.strip() == "", result.stdout
    assert result.stderr.strip() != ""

    # A well-formed event but a malformed Overlay JSON (Registry load raises).
    bad_overlay = tmp_path / "bad.json"
    bad_overlay.write_text("{ this is not json", encoding="utf-8")
    event = {"hook_event_name": "SubagentStart", "agent_id": "x", "agent_type": "deepseek"}
    result = _run(command, json.dumps(event), overlay_path=str(bad_overlay))
    assert result.returncode == 0
    assert result.stdout.strip() == "", result.stdout
    assert result.stderr.strip() != ""
