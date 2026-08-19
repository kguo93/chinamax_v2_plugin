"""Registry loader tests: shipped-defaults snapshot, Overlay merge, validation (AC-1)."""

from __future__ import annotations

import json

import pytest

from chinamaxM.registry import Profile, RegistryError, load_registry

#: The six shipped Profiles, snapshotted against the plan's pinned seed registry (a
#: wrong endpoint, key var, model, or thinking fragment in any Profile must fail).
_EXPECTED_PROFILES = {
    "deepseek": Profile(
        name="deepseek",
        dialect="anthropic",
        base_url="https://api.deepseek.com/anthropic",
        api_key_env="DEEPSEEK_API_KEY",
        default_model="deepseek-v4-pro[1m]",
        context_window={"deepseek-v4-pro[1m]": 1000000},
        thinking={"extra_body": {"reasoning": {"effort": "max"}}},
        scrub=["cache_control", "output_config"],
        request_extras={},
        tools=None,
    ),
    "mimo": Profile(
        name="mimo",
        dialect="anthropic",
        base_url="https://api.xiaomimimo.com/anthropic",
        api_key_env="MIMO_API_KEY",
        default_model="mimo-v2.5",
        context_window=None,
        thinking={"extra_body": {"reasoning_effort": "high"}},
        scrub=["cache_control", "output_config"],
        request_extras={},
        tools=None,
    ),
    "glm": Profile(
        name="glm",
        dialect="anthropic",
        base_url="https://api.z.ai/api/anthropic",
        api_key_env="GLM_API_KEY",
        default_model="glm-5.2",
        context_window=None,
        thinking={"thinking": {"type": "enabled"}},
        scrub=["cache_control", "output_config"],
        request_extras={},
        tools=None,
    ),
    "minimax": Profile(
        name="minimax",
        dialect="anthropic",
        base_url="https://api.minimax.io/anthropic",
        api_key_env="MINIMAX_API_KEY",
        default_model="MiniMax-M3[1m]",
        context_window=None,
        thinking={"thinking": {"type": "adaptive"}},
        scrub=["cache_control", "output_config"],
        request_extras={},
        tools=None,
    ),
    "kimi": Profile(
        name="kimi",
        dialect="anthropic",
        base_url="https://api.moonshot.ai/anthropic",
        api_key_env="KIMI_API_KEY",
        default_model="kimi-k3",
        context_window=None,
        thinking={"extra_body": {"reasoning_effort": "max"}},
        scrub=["cache_control", "output_config"],
        request_extras={},
        tools=None,
    ),
    "qwen": Profile(
        name="qwen",
        dialect="anthropic",
        base_url="https://dashscope-intl.aliyuncs.com/apps/anthropic",
        api_key_env="QWEN_API_KEY",
        default_model="qwen3.8-max",
        context_window=None,
        thinking={"thinking": {"type": "enabled"}},
        scrub=["cache_control", "output_config"],
        request_extras={},
        tools=None,
    ),
}

#: The shipped Profiles in load order.
_SEED_ORDER = ["deepseek", "mimo", "glm", "minimax", "kimi", "qwen"]

#: One valid, complete new-Profile row for Overlay add/duplicate/reserved cases.
_NEW_ROW = {
    "dialect": "anthropic",
    "base_url": "https://api.zed.example/anthropic",
    "api_key_env": "ZED_API_KEY",
    "default_model": "zed-1",
}


def _write_overlay(tmp_path, content, name="overlay.json"):
    """Write an Overlay document (dict/list ⇒ JSON dump; str ⇒ raw) and return its path."""
    path = tmp_path / name
    text = content if isinstance(content, str) else json.dumps(content)
    path.write_text(text, encoding="utf-8")
    return path


def test_registry_loads_six_seed_profiles():
    """AC-1: shipped defaults load, Overlay-less, as the exact pinned seed registry."""
    registry = load_registry()

    assert registry.port == 8402
    assert list(registry.profiles) == _SEED_ORDER
    assert registry.profiles == _EXPECTED_PROFILES


def test_overlay_merge_overrides_by_name(tmp_path):
    """AC-1: field override by name, new-Profile add, and header port override."""
    # Case 1: override one field of an existing Profile; the rest are untouched.
    override = _write_overlay(
        tmp_path,
        {"profiles": [{"name": "deepseek", "default_model": "deepseek-v4-flash"}]},
        name="override.json",
    )
    registry = load_registry(overlay_path=override)
    assert registry.profiles["deepseek"].default_model == "deepseek-v4-flash"
    # Top-level replacement only: the untouched deepseek fields stay put.
    assert registry.profiles["deepseek"].context_window == {"deepseek-v4-pro[1m]": 1000000}
    assert registry.profiles["mimo"] == _EXPECTED_PROFILES["mimo"]

    # Case 2: a new-name row ADDS a Profile after the shipped six, optionals defaulted.
    add = _write_overlay(tmp_path, {"profiles": [{"name": "zed", **_NEW_ROW}]}, name="add.json")
    registry = load_registry(overlay_path=add)
    assert list(registry.profiles)[-1] == "zed"
    zed = registry.profiles["zed"]
    assert (zed.dialect, zed.default_model) == ("anthropic", "zed-1")
    assert (zed.thinking, zed.scrub, zed.request_extras, zed.tools, zed.context_window) == (
        {},
        [],
        {},
        None,
        None,
    )

    # Case 3: an Overlay header port overrides the shipped port.
    port_overlay = _write_overlay(tmp_path, {"port": 9999, "profiles": []}, name="port.json")
    registry = load_registry(overlay_path=port_overlay)
    assert registry.port == 9999
    assert list(registry.profiles) == _SEED_ORDER


_MALFORMED_CASES = [
    ("empty_default_model", {"profiles": [{"name": "deepseek", "default_model": ""}]}, ["deepseek", "default_model"]),
    ("unknown_dialect", {"profiles": [{"name": "deepseek", "dialect": "chat"}]}, ["deepseek", "dialect"]),
    ("legacy_match", {"profiles": [{"name": "deepseek", "match": ["deepseek-*"]}]}, ["deepseek", "match", "ADR 0003"]),
    ("legacy_models", {"profiles": [{"name": "deepseek", "models": ["x"]}]}, ["deepseek", "models"]),
    ("reserved_general_purpose", {"profiles": [{"name": "general-purpose", **_NEW_ROW}]}, ["general-purpose", "reserved"]),
    ("reserved_openai", {"profiles": [{"name": "openai", **_NEW_ROW}]}, ["openai", "reserved"]),
    (
        "duplicate_within_document",
        {"profiles": [{"name": "zed", **_NEW_ROW}, {"name": "zed", **_NEW_ROW}]},
        ["duplicate", "zed"],
    ),
    ("non_object_thinking", {"profiles": [{"name": "deepseek", "thinking": "nope"}]}, ["deepseek", "thinking"]),
    ("scrub_not_list", {"profiles": [{"name": "deepseek", "scrub": "cache_control"}]}, ["deepseek", "scrub"]),
    (
        "context_window_not_positive",
        {"profiles": [{"name": "deepseek", "context_window": {"m": 0}}]},
        ["deepseek", "context_window"],
    ),
    (
        "new_profile_missing_required",
        {"profiles": [{"name": "zed", "dialect": "anthropic", "base_url": "https://x", "api_key_env": "X"}]},
        ["zed", "default_model"],
    ),
    ("bare_v1_array", [{"name": "deepseek"}], ["bare JSON array", "ADR 0003"]),
    ("malformed_json", "{ not valid json", ["malformed JSON"]),
    ("unknown_header_field", {"port": 8402, "bogus": 1, "profiles": []}, ["unknown top-level", "bogus"]),
    ("port_zero", {"port": 0, "profiles": []}, ["port", "1..65535"]),
    ("port_too_large", {"port": 70000, "profiles": []}, ["port", "1..65535"]),
    ("port_bool", {"port": True, "profiles": []}, ["port", "1..65535"]),
    ("scrub_non_string_element", {"profiles": [{"name": "deepseek", "scrub": ["cache_control", 5]}]}, ["deepseek", "scrub"]),
    ("request_extras_not_object", {"profiles": [{"name": "deepseek", "request_extras": "nope"}]}, ["deepseek", "request_extras"]),
    (
        "request_extras_reserved_key",
        {"profiles": [{"name": "deepseek", "request_extras": {"model": "x"}}]},
        ["deepseek", "reserved", "model"],
    ),
    (
        "request_extras_readds_scrubbed",
        {"profiles": [{"name": "deepseek", "request_extras": {"cache_control": {}}}]},
        ["deepseek", "scrubbed", "cache_control"],
    ),
    ("tools_not_null_or_list", {"profiles": [{"name": "deepseek", "tools": "nope"}]}, ["deepseek", "tools"]),
    ("empty_name", {"profiles": [{"name": "", **_NEW_ROW}]}, ["non-empty", "name"]),
    ("empty_base_url", {"profiles": [{"name": "deepseek", "base_url": ""}]}, ["deepseek", "base_url"]),
    ("empty_api_key_env", {"profiles": [{"name": "deepseek", "api_key_env": ""}]}, ["deepseek", "api_key_env"]),
]


@pytest.mark.parametrize(
    "case_id, content, substrings", _MALFORMED_CASES, ids=[case[0] for case in _MALFORMED_CASES]
)
def test_registry_rejects_malformed(tmp_path, case_id, content, substrings):
    """AC-1: each malformed case raises a diagnosable error naming file + location."""
    overlay_path = _write_overlay(tmp_path, content)

    with pytest.raises(RegistryError) as exc_info:
        load_registry(overlay_path=overlay_path)

    message = str(exc_info.value)
    assert str(overlay_path) in message
    for expected in substrings:
        assert expected in message, f"{case_id}: {expected!r} not in {message!r}"
