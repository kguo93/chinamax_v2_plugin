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
import subprocess
import sys
from pathlib import Path

import pytest

from chinamaxM.hooks.worker_contract import RELAY_RULE, WORKER_CONTRACT

REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_HOOKS_JSON = REPO_ROOT / "hooks" / "codex-hooks.json"
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
    assert "python -m chinamaxM.set_model codex <profile> <model>" in text

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

    # --- SubagentStart: worker roles get the numbered contract (Registry-derived) ---
    subagent_cmd = _command_for("SubagentStart")
    for agent_type in ("deepseek", "deepseek-1", "zed-1"):
        result = _run(subagent_cmd, _subagent_event(agent_type), overlay_path=zed_overlay)
        assert result.returncode == 0, result.stderr
        out = _injected(result)
        assert out["hookEventName"] == "SubagentStart"
        assert out["additionalContext"] == WORKER_CONTRACT
        for number in ("1.", "2.", "3.", "4."):
            assert number in out["additionalContext"]

    # Anchored-match trap (`kimono` vs profile `kimi`) and a non-worker role ⇒ silent.
    for agent_type in ("kimono", "Explore"):
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
        # The shared rewrite helper (host token differs per surface).
        assert "python -m chinamaxM.set_model" in text
        # Default and custom name rules.
        assert "<profile>-<n>" in text
        assert "<profile>-" in text
        assert "non-empty suffix" in lower
        # The verbatim Relay rule, identical on both.
        assert RELAY_RULE in text

    # The rewrite helper differs only in the host token — proving surface symmetry, not
    # a shared host.
    assert "python -m chinamaxM.set_model codex <profile> <model>" in skill
    assert "python -m chinamaxM.set_model claude <profile> <model>" in command


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
