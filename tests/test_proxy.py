"""Proxy tests: loopback bind, passthrough, routing, streaming, and the Relay branch."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import socket
from dataclasses import replace

import pytest
import yarl
from aiohttp import web

from chinamaxM.keyfiles import KeyFileReader, scaffold_key_file
from chinamaxM.proxy import LOOPBACK_HOST, create_app
from chinamaxM.registry import Profile, Registry, load_registry
from chinamaxM.relay import serialize_body

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _clean_response_headers(headers) -> dict[str, str]:
    """Return response headers minus hop-by-hop and the proxy's own Date/Server."""
    drop = _HOP_BY_HOP | {"date", "server"}
    return {name.lower(): value for name, value in headers.items() if name.lower() not in drop}


async def test_proxy_binds_loopback_and_registry_port():
    """AC-2: the Proxy binds 127.0.0.1 only, on the Registry port."""
    probe = socket.socket()
    probe.bind((LOOPBACK_HOST, 0))
    port = probe.getsockname()[1]
    probe.close()

    registry = replace(load_registry(), port=port)
    app = create_app(registry, upstream_base=f"http://{LOOPBACK_HOST}:9")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=LOOPBACK_HOST, port=registry.port)
    await site.start()
    try:
        addresses = runner.addresses
        assert addresses, "no bound server sockets"
        for address in addresses:
            assert address[0] == "127.0.0.1"
            assert address[1] == registry.port
        connection = socket.create_connection(("127.0.0.1", registry.port), timeout=5)
        connection.close()
    finally:
        await runner.cleanup()


async def test_unmatched_messages_pass_through_byte_identical(proxy_client, fake_provider):
    """AC-3: prefixless /v1/messages reaches the upstream body- and header-identical."""
    body = json.dumps(
        {
            "model": "claude-opus-5",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
                    ],
                }
            ],
        }
    ).encode()
    fake_provider.respond(
        status=200, headers={"content-type": "application/json", "x-echo": "42"}, body=b'{"ok":true}'
    )

    sent = {
        "anthropic-beta": "prompt-caching-2024-07-31",
        "authorization": "Bearer sk-test",
        "anthropic-version": "2023-06-01",
    }
    response = await proxy_client.post(
        "/v1/messages",
        data=body,
        headers=sent,
        skip_auto_headers=["User-Agent", "Accept", "Accept-Encoding", "Content-Type"],
    )
    received_body = await response.read()

    # Request reached the upstream byte-identical.
    assert len(fake_provider.requests) == 1
    recorded = fake_provider.requests[0]
    assert recorded.method == "POST"
    assert recorded.body == body

    # Host was regenerated to the upstream authority (a verbatim-forwarded inbound Host
    # would fail this); every explicit header forwarded verbatim.
    assert recorded.headers["Host"] == yarl.URL(fake_provider.url).authority
    for name, value in sent.items():
        assert recorded.headers[name] == value
    # The Proxy added no headers the client did not send.
    for banned in ("User-Agent", "Accept", "Accept-Encoding", "Content-Type"):
        assert banned not in recorded.headers
    # Received == sent (explicit) plus at most recomputed Host/Content-Length.
    leftover = [n.lower() for n in recorded.headers if n.lower() not in ("host", "content-length")]
    assert sorted(leftover) == sorted(sent)

    # Response reached the client byte-identical (header equality minus Date/Server).
    assert response.status == 200
    assert received_body == b'{"ok":true}'
    assert _clean_response_headers(response.headers) == {
        "content-type": "application/json",
        "x-echo": "42",
        "content-length": str(len(b'{"ok":true}')),
    }


async def test_count_tokens_unmatched_passes_through(proxy_client, fake_provider):
    """AC-3: prefixless /v1/messages/count_tokens passes through unchanged."""
    body = json.dumps({"model": "claude-opus-5", "messages": []}).encode()
    fake_provider.respond(
        status=200, headers={"content-type": "application/json"}, body=b'{"input_tokens":3}'
    )

    response = await proxy_client.post(
        "/v1/messages/count_tokens", data=body, skip_auto_headers=["Content-Type"]
    )

    assert response.status == 200
    assert await response.read() == b'{"input_tokens":3}'
    assert fake_provider.requests[-1].body == body
    assert fake_provider.requests[-1].raw_path == "/v1/messages/count_tokens"


async def test_non_messages_paths_pass_through(proxy_client, fake_provider):
    """AC-4: non-messages paths pass through with methods and query strings intact."""
    fake_provider.respond(status=200, body=b"models-list")
    response = await proxy_client.get("/v1/models?limit=5")
    assert response.status == 200
    assert await response.read() == b"models-list"
    recorded = fake_provider.requests[-1]
    assert recorded.method == "GET"
    assert recorded.raw_path == "/v1/models?limit=5"

    fake_provider.respond(status=201, body=b"created")
    payload = b'{"anything":"goes"}'
    response = await proxy_client.post(
        "/v1/unknown-future-endpoint", data=payload, skip_auto_headers=["Content-Type"]
    )
    assert response.status == 201
    assert await response.read() == b"created"
    recorded = fake_provider.requests[-1]
    assert recorded.method == "POST"
    assert recorded.raw_path == "/v1/unknown-future-endpoint"
    assert recorded.body == payload


async def test_profile_prefix_routes(make_proxy, make_fake_provider, fake_provider, claude_home):
    """AC-5: a known prefix routes to that Profile's provider; unknown ⇒ 404; prefixless ⇒ Default.

    Routing is proven by WHICH fake provider captured the request (two Profiles with
    distinct base_urls), not a debug header — the anthropic-dialect branch now relays.
    """
    provider_a = await make_fake_provider()
    provider_b = await make_fake_provider()
    provider_a.respond(status=200, body=b"from-a")
    provider_b.respond(status=200, body=b"from-b")

    base = load_registry()
    alpha = Profile(
        name="alpha", dialect="anthropic", base_url=provider_a.url,
        api_key_env="ALPHA_API_KEY", default_model="a-1",
    )
    beta = Profile(
        name="beta", dialect="anthropic", base_url=provider_b.url,
        api_key_env="BETA_API_KEY", default_model="b-1",
    )
    registry = Registry(port=base.port, profiles={"alpha": alpha, "beta": beta})
    claude_home.write_keys({"ALPHA_API_KEY": "ka", "BETA_API_KEY": "kb"})
    client = await make_proxy(registry, claude_home=str(claude_home.root))

    async def post_model(model):
        return await client.post(
            "/v1/messages", data=json.dumps({"model": model}).encode(), skip_auto_headers=["Content-Type"]
        )

    # A known prefix routes to THAT Profile's provider; the rest is never validated.
    response = await post_model("alpha/anything-at-all")
    assert response.status == 200
    assert await response.read() == b"from-a"
    assert len(provider_a.requests) == 1 and provider_b.requests == []
    assert json.loads(provider_a.requests[-1].body)["model"] == "anything-at-all"

    # A different prefix routes to the other provider (routing is Registry-derived).
    response = await post_model("beta/x")
    assert response.status == 200
    assert await response.read() == b"from-b"
    assert len(provider_b.requests) == 1 and len(provider_a.requests) == 1

    # Unknown profile ⇒ local 404 naming every valid Profile; nothing forwarded.
    response = await post_model("unknownprof/x")
    assert response.status == 404
    payload = await response.json()
    assert payload["error"]["type"] == "unknown_profile"
    for name in registry.profiles:
        assert name in payload["error"]["message"]

    # Profile matching is case-sensitive.
    response = await post_model("Alpha/x")
    assert response.status == 404

    # No slash ⇒ Default branch (reaches upstream_base), never routed to a Profile.
    fake_provider.respond(status=200, body=b"defaulted")
    response = await post_model("deepseek-v4-pro")
    assert response.status == 200
    assert await response.read() == b"defaulted"
    assert fake_provider.requests[-1].body == json.dumps({"model": "deepseek-v4-pro"}).encode()
    # The Default branch left the per-Profile providers untouched.
    assert len(provider_a.requests) == 1 and len(provider_b.requests) == 1


async def test_streaming_passthrough_byte_faithful(proxy_client, fake_provider):
    """AC-6: gated SSE forwards incrementally and byte-faithfully."""
    chunks = [b"event: a\ndata: 1\n\n", b"event: b\ndata: 2\n\n", b"event: c\ndata: 3\n\n"]
    fake_provider.respond_stream(chunks, status=200, headers={"content-type": "text/event-stream"})

    response = await proxy_client.post(
        "/v1/messages",
        data=json.dumps({"model": "claude-opus-5"}).encode(),
        skip_auto_headers=["Content-Type"],
    )
    assert response.status == 200

    received = []
    for index in range(len(chunks)):
        event = await asyncio.wait_for(response.content.readuntil(b"\n\n"), timeout=10)
        received.append(event)
        fake_provider.release(index)

    assert b"".join(received) == b"".join(chunks)


async def test_responses_profile_routes_501(make_proxy, fake_provider, tmp_path):
    """AC-1/PRD-21: a responses-dialect Profile parses but routes to 501, never Default."""
    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "name": "respprof",
                        "dialect": "responses",
                        "base_url": "https://api.resp.example",
                        "api_key_env": "RESP_API_KEY",
                        "default_model": "resp-1",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded = load_registry(overlay_path=overlay)
    assert loaded.profiles["respprof"].dialect == "responses"

    client = await make_proxy(loaded)
    response = await client.post(
        "/v1/messages", data=json.dumps({"model": "respprof/x"}).encode(), skip_auto_headers=["Content-Type"]
    )

    assert response.status == 501
    assert (await response.json())["error"]["type"] == "not_implemented"
    assert fake_provider.requests == []


async def test_malformed_json_body_passes_through(proxy_client, fake_provider):
    """AC-3: an unparseable body and a model-less body both ride the Default branch."""
    fake_provider.respond(status=200, body=b"ok")
    bad = b"{ this is not json"
    response = await proxy_client.post("/v1/messages", data=bad, skip_auto_headers=["Content-Type"])
    assert response.status == 200
    assert fake_provider.requests[-1].body == bad

    fake_provider.respond(status=200, body=b"ok2")
    no_model = json.dumps({"messages": []}).encode()
    response = await proxy_client.post("/v1/messages", data=no_model, skip_auto_headers=["Content-Type"])
    assert response.status == 200
    assert fake_provider.requests[-1].body == no_model


async def test_encoded_response_passes_through(proxy_client, fake_provider):
    """AC-3/AC-6: a gzip-encoded upstream response forwards raw (auto_decompress=False)."""
    raw = b'{"message":"hello world"}'
    encoded = gzip.compress(raw)
    fake_provider.respond(
        status=200,
        headers={"content-type": "application/json", "content-encoding": "gzip"},
        body=encoded,
    )

    response = await proxy_client.post(
        "/v1/messages",
        data=json.dumps({"model": "claude-opus-5"}).encode(),
        skip_auto_headers=["Content-Type"],
        auto_decompress=False,
    )

    assert response.status == 200
    assert response.headers["Content-Encoding"] == "gzip"
    received = await response.read()
    assert received == encoded
    assert gzip.decompress(received) == raw


def test_hermetic_guard_blocks_nonloopback_and_clears_keys():
    """AC-7: provider keys are cleared and non-loopback resolution/connect is blocked."""
    for var in (
        "DEEPSEEK_API_KEY",
        "MIMO_API_KEY",
        "GLM_API_KEY",
        "MINIMAX_API_KEY",
        "KIMI_API_KEY",
        "QWEN_API_KEY",
    ):
        assert var not in os.environ

    with pytest.raises(OSError):
        socket.getaddrinfo("api.anthropic.com", 443)

    probe = socket.socket()
    try:
        with pytest.raises(OSError):
            probe.connect(("8.8.8.8", 53))
    finally:
        probe.close()

    # Loopback still resolves.
    socket.getaddrinfo("127.0.0.1", 80)


# ------------------------------------------------------------------- Relay branch (proxy-02)

#: Key values written into the Claude Key file for relay tests (one per seed Profile).
_ALL_KEYS = {
    "DEEPSEEK_API_KEY": "k-deepseek",
    "MIMO_API_KEY": "k-mimo",
    "GLM_API_KEY": "k-glm",
    "MINIMAX_API_KEY": "k-minimax",
    "KIMI_API_KEY": "k-kimi",
    "QWEN_API_KEY": "k-qwen",
}

#: Per-seed shape: (Profile, bare default_model, root reasoning pair or None, thinking dict
#: or None). Reasoning-at-root Profiles carry NO thinking field; the others carry only the
#: provider thinking form. Values copied from ADR 0003's table.
_SHAPE_CASES = [
    ("deepseek", "deepseek-v4-pro[1m]", ("reasoning", {"effort": "max"}), None),
    ("kimi", "kimi-k3", ("reasoning_effort", "max"), None),
    ("mimo", "mimo-v2.5", ("reasoning_effort", "high"), None),
    ("glm", "glm-5.2", None, {"type": "enabled"}),
    ("qwen", "qwen3.8-max", None, {"type": "enabled"}),
    ("minimax", "MiniMax-M3[1m]", None, {"type": "adaptive"}),
]


def _has_cache_control(node) -> bool:
    """Return whether any ``cache_control`` key appears at any depth in ``node``."""
    if isinstance(node, dict):
        return "cache_control" in node or any(_has_cache_control(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_cache_control(item) for item in node)
    return False


def _bump_mtime(path) -> None:
    """Force a distinct ``st_mtime_ns`` so the reader's (mtime, size) cache invalidates."""
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))


@pytest.mark.parametrize(
    "name, model, root_expect, thinking_expect", _SHAPE_CASES, ids=[case[0] for case in _SHAPE_CASES]
)
async def test_relay_mutations_per_profile_shape(
    make_proxy, relay_registry, claude_home, fake_provider, name, model, root_expect, thinking_expect
):
    """AC-1: each seed shape strips prefix/thinking/output_config/cache_control and applies its policy."""
    claude_home.write_keys(_ALL_KEYS)
    fake_provider.respond(status=200, body=b'{"ok":true}')
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root))

    inbound = {
        "model": f"{name}/{model}",
        "system": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
            ]},
        ],
        "thinking": {"type": "enabled", "budget_tokens": 2048},
        "output_config": {"foo": "bar"},
        "metadata": {"user_id": "u1"},
    }
    response = await client.post(
        "/v1/messages", data=json.dumps(inbound).encode(), skip_auto_headers=["Content-Type"]
    )
    assert response.status == 200

    forwarded = json.loads(fake_provider.requests[-1].body)
    # Prefix stripped; the bare default model (any [1m] suffix intact) is forwarded verbatim.
    assert forwarded["model"] == model
    # The Claude-flavored shaping is gone; metadata (in no scrub list) is untouched.
    assert "output_config" not in forwarded
    assert not _has_cache_control(forwarded)
    assert forwarded["metadata"] == {"user_id": "u1"}

    if root_expect is not None:
        root_key, root_value = root_expect
        assert forwarded[root_key] == root_value
        assert "thinking" not in forwarded
    else:
        assert forwarded["thinking"] == thinking_expect
        assert "reasoning" not in forwarded and "reasoning_effort" not in forwarded


async def test_relay_auth_swapped_and_beta_dropped(
    make_proxy, relay_registry, claude_home, codex_home, fake_provider
):
    """AC-1: inbound auth is swapped for the CLAUDE-file key; beta dropped; version defaulted/forwarded."""
    claude_home.write_keys({"DEEPSEEK_API_KEY": "claude-secret"})
    codex_home.write_keys({"DEEPSEEK_API_KEY": "codex-secret"})
    fake_provider.respond(status=200, body=b"ok")
    client = await make_proxy(
        relay_registry, claude_home=str(claude_home.root), codex_home=str(codex_home.root)
    )

    # Inbound OAuth + anthropic-beta, no anthropic-version ⇒ default applied.
    response = await client.post(
        "/v1/messages",
        data=json.dumps({"model": "deepseek/m"}).encode(),
        headers={"authorization": "Bearer oauth-token", "anthropic-beta": "prompt-caching-2024-07-31"},
        skip_auto_headers=["Content-Type"],
    )
    assert response.status == 200
    received = fake_provider.requests[-1].headers
    # Anthropic ingress selects the CLAUDE file, not the Codex file with its distinct value.
    assert received["x-api-key"] == "claude-secret"
    assert "Authorization" not in received
    assert "anthropic-beta" not in received
    assert received["anthropic-version"] == "2023-06-01"

    # A second request WITH an inbound anthropic-version sees that value forwarded.
    response = await client.post(
        "/v1/messages",
        data=json.dumps({"model": "deepseek/m"}).encode(),
        headers={"anthropic-version": "2025-04-01"},
        skip_auto_headers=["Content-Type"],
    )
    assert response.status == 200
    assert fake_provider.requests[-1].headers["anthropic-version"] == "2025-04-01"


async def test_relay_cache_control_never_reaches_provider(
    make_proxy, relay_registry, claude_home, fake_provider
):
    """AC-2: cache_control at every depth is scrubbed; the containing blocks survive."""
    claude_home.write_keys(_ALL_KEYS)
    fake_provider.respond(status=200, body=b"ok")
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root))

    inbound = {
        "model": "deepseek/m",
        "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
        "tools": [{"name": "t", "description": "d", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": [
                    {"type": "text", "text": "r", "cache_control": {"type": "ephemeral"}}
                ]},
            ]},
            {"role": "user", "content": [
                {"type": "text", "text": "last", "cache_control": {"type": "ephemeral"}}
            ]},
        ],
    }
    response = await client.post(
        "/v1/messages", data=json.dumps(inbound).encode(), skip_auto_headers=["Content-Type"]
    )
    assert response.status == 200

    forwarded = json.loads(fake_provider.requests[-1].body)
    assert not _has_cache_control(forwarded)
    # Only the cache_control key was removed — every containing block is preserved.
    assert forwarded["system"][0] == {"type": "text", "text": "s"}
    assert forwarded["tools"][0] == {"name": "t", "description": "d"}
    assert forwarded["messages"][0]["content"][0]["content"][0] == {"type": "text", "text": "r"}
    assert forwarded["messages"][1]["content"][0] == {"type": "text", "text": "last"}


async def test_missing_key_401_names_var_and_file(
    make_proxy, relay_registry, claude_home, fake_provider
):
    """AC-3: a missing key ⇒ 401 naming the variable and Key file; nothing sent upstream."""
    claude_home.write_keys({"MIMO_API_KEY": "present"})  # deepseek's key is absent
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root))

    response = await client.post(
        "/v1/messages", data=json.dumps({"model": "deepseek/m"}).encode(), skip_auto_headers=["Content-Type"]
    )
    assert response.status == 401
    payload = await response.json()
    assert payload["error"]["type"] == "authentication_error"
    assert "DEEPSEEK_API_KEY" in payload["error"]["message"]
    assert str(claude_home.keys_path) in payload["error"]["message"]
    assert fake_provider.requests == []


async def test_key_mtime_reload(make_proxy, relay_registry, claude_home, fake_provider):
    """AC-4: a key edit takes effect without restart; emptying it fails closed again."""
    claude_home.write_text("")  # no key yet
    fake_provider.respond(status=200, body=b"ok")
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root))

    async def relay():
        return await client.post(
            "/v1/messages", data=json.dumps({"model": "deepseek/m"}).encode(), skip_auto_headers=["Content-Type"]
        )

    # No key ⇒ 401, nothing forwarded.
    assert (await relay()).status == 401
    assert fake_provider.requests == []

    # Write the key (distinct mtime) ⇒ forwarded, no restart.
    claude_home.write_keys({"DEEPSEEK_API_KEY": "sk-now"})
    _bump_mtime(claude_home.keys_path)
    assert (await relay()).status == 200
    assert len(fake_provider.requests) == 1
    assert fake_provider.requests[-1].headers["x-api-key"] == "sk-now"

    # Empty the file again ⇒ fail-closed 401 (reverse transition).
    claude_home.write_text("")
    _bump_mtime(claude_home.keys_path)
    assert (await relay()).status == 401
    assert len(fake_provider.requests) == 1


@pytest.mark.parametrize("name", ["deepseek", "mimo", "glm", "minimax", "kimi", "qwen"])
async def test_relay_prefix_stability(make_proxy, relay_registry, claude_home, fake_provider, name):
    """AC-5: two relay turns share a byte-identical prefix through turn-1's last message; determinism."""
    claude_home.write_keys(_ALL_KEYS)
    fake_provider.respond(status=200, body=b"ok")
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root))

    model = f"{name}/m"
    turn1 = {
        "model": model,
        "system": [{"type": "text", "text": "sys"}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "u1", "cache_control": {"type": "ephemeral"}}]},
        ],
    }
    turn2 = {
        "model": model,
        "system": [{"type": "text", "text": "sys"}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "u1"}]},  # breakpoint moved off this block
            {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
            {"role": "user", "content": [{"type": "text", "text": "u2", "cache_control": {"type": "ephemeral"}}]},
        ],
    }

    async def relay(payload):
        return await client.post(
            "/v1/messages", data=json.dumps(payload).encode(), skip_auto_headers=["Content-Type"]
        )

    assert (await relay(turn1)).status == 200
    assert (await relay(turn2)).status == 200
    assert (await relay(turn1)).status == 200  # verbatim resend

    s1 = fake_provider.requests[0].body
    s2 = fake_provider.requests[1].body
    s3 = fake_provider.requests[2].body

    # Determinism: the verbatim turn-1 resend is byte-identical.
    assert s3 == s1

    # The scrubbed first message serializes identically in both turns; the common prefix
    # extends through the end of turn-1's last (only) message object, then diverges exactly
    # at the messages-array boundary (turn-1 closes it, turn-2 appends the next message).
    scrubbed_msg0 = serialize_body({"role": "user", "content": [{"type": "text", "text": "u1"}]})
    boundary = s1.index(scrubbed_msg0) + len(scrubbed_msg0)
    assert s1[:boundary] == s2[:boundary]
    assert s1[boundary:boundary + 1] == b"]"
    assert s2[boundary:boundary + 1] == b","


async def test_relay_streaming_faithful(make_proxy, relay_registry, claude_home, fake_provider):
    """AC-6: a gated SSE stream relays incrementally and byte-faithfully on the relay branch."""
    claude_home.write_keys(_ALL_KEYS)
    chunks = [b"event: a\ndata: 1\n\n", b"event: b\ndata: 2\n\n", b"event: c\ndata: 3\n\n"]
    fake_provider.respond_stream(chunks, status=200, headers={"content-type": "text/event-stream"})
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root))

    response = await client.post(
        "/v1/messages", data=json.dumps({"model": "deepseek/m"}).encode(), skip_auto_headers=["Content-Type"]
    )
    assert response.status == 200

    received = []
    for index in range(len(chunks)):
        event = await asyncio.wait_for(response.content.readuntil(b"\n\n"), timeout=10)
        received.append(event)
        fake_provider.release(index)
    assert b"".join(received) == b"".join(chunks)


def test_scaffold_o_excl_comments_only(tmp_path, monkeypatch):
    """AC-7: scaffold is O_EXCL, mode-0600, comments-only, Registry-driven, and values-free."""
    base = load_registry()
    custom = Profile(
        name="custom", dialect="anthropic", base_url="https://x",
        api_key_env="CUSTOM_API_KEY", default_model="c-1",
    )
    profiles = {**base.profiles, "custom": custom}
    names = [profile.api_key_env for profile in profiles.values()]  # seven distinct names

    monkeypatch.setenv("DEEPSEEK_API_KEY", "SECRET-should-not-appear")

    for root in (tmp_path / "claude", tmp_path / "codex"):
        path = root / "model-keys.env"
        assert scaffold_key_file(path, names) == "created"
        assert path.stat().st_mode & 0o777 == 0o600
        text = path.read_text()
        for var in names:
            assert var in text
        assert "SECRET-should-not-appear" not in text
        # Comments only ⇒ the reader parses zero active keys.
        assert KeyFileReader(path).get("DEEPSEEK_API_KEY") == ""
        # A second call no-ops with "exists" and leaves the bytes unchanged.
        before = path.read_bytes()
        assert scaffold_key_file(path, names) == "exists"
        assert path.read_bytes() == before


async def test_relay_extras_merged_last_replace_on_conflict(
    make_proxy, claude_home, fake_provider, tmp_path
):
    """AC-1: request_extras merge last — overriding a thinking key and re-adding a stripped field."""
    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        json.dumps({"profiles": [{
            "name": "xtra",
            "dialect": "anthropic",
            "base_url": "https://placeholder",
            "api_key_env": "XTRA_API_KEY",
            "default_model": "x-1",
            "thinking": {"extra_body": {"reasoning_effort": "high"}},
            "scrub": ["cache_control"],
            "request_extras": {"reasoning_effort": "low", "output_config": {"a": 1}},
        }]}),
        encoding="utf-8",
    )
    loaded = load_registry(overlay_path=overlay)  # also proves the extras guard ALLOWS this shape
    profiles = dict(loaded.profiles)
    profiles["xtra"] = replace(profiles["xtra"], base_url=fake_provider.url)
    registry = Registry(port=loaded.port, profiles=profiles)

    claude_home.write_keys({"XTRA_API_KEY": "k"})
    fake_provider.respond(status=200, body=b"ok")
    client = await make_proxy(registry, claude_home=str(claude_home.root))

    inbound = {"model": "xtra/x-1", "output_config": {"inbound": True}, "messages": []}
    response = await client.post(
        "/v1/messages", data=json.dumps(inbound).encode(), skip_auto_headers=["Content-Type"]
    )
    assert response.status == 200

    forwarded = json.loads(fake_provider.requests[-1].body)
    # Extras win over the thinking policy; the stripped field is re-added with the extras value.
    assert forwarded["reasoning_effort"] == "low"
    assert forwarded["output_config"] == {"a": 1}
    # The re-added field is appended after the thinking-policy key.
    raw = fake_provider.requests[-1].body
    assert raw.index(b'"output_config"') > raw.index(b'"reasoning_effort"')


# ------------------------------------------------------- Dispatch marker (ADR 0004 amended)


def _marker_body(model: str, marker_model: str | None, **extra) -> bytes:
    """Build a relay body whose first user message opens with a Dispatch marker line."""
    text = "do the task"
    if marker_model is not None:
        text = f"[chinamaxm model={marker_model}]\n\n{text}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
        **extra,
    }
    return json.dumps(payload).encode()


async def test_marker_substitutes_egress_model_from_request_one(
    make_proxy, relay_registry, claude_home, fake_provider
):
    """A Dispatch marker substitutes the egress model on the FIRST request; the marker text
    stays in the forwarded body; the JSONL line logs the EFFECTIVE model."""
    claude_home.write_keys(_ALL_KEYS)
    fake_provider.respond(status=200, body=b'{"ok":true}')
    lines: list[dict] = []
    client = await make_proxy(
        relay_registry, claude_home=str(claude_home.root), log_sink=lines.append
    )

    response = await client.post(
        "/v1/messages",
        data=_marker_body("deepseek/deepseek-v4-pro[1m]", "deepseek-v4-flash"),
        skip_auto_headers=["Content-Type"],
    )
    assert response.status == 200

    forwarded = json.loads(fake_provider.requests[-1].body)
    assert forwarded["model"] == "deepseek-v4-flash"
    # The marker is kept in the forwarded body, not stripped.
    assert "[chinamaxm model=deepseek-v4-flash]" in forwarded["messages"][0]["content"][0]["text"]
    # JSONL logs the effective egress model (ADR 0010 as amended 2026-08-19).
    assert len(lines) == 1
    assert lines[0]["model"] == "deepseek-v4-flash"


async def test_marker_greedy_capture_keeps_bracket_suffix(
    make_proxy, relay_registry, claude_home, fake_provider
):
    """A ``[1m]``-suffixed model ID survives the marker capture intact (greedy to line end)."""
    claude_home.write_keys(_ALL_KEYS)
    fake_provider.respond(status=200, body=b"ok")
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root))

    response = await client.post(
        "/v1/messages",
        data=_marker_body("deepseek/m", "deepseek-v4-pro[1m]"),
        skip_auto_headers=["Content-Type"],
    )
    assert response.status == 200
    assert json.loads(fake_provider.requests[-1].body)["model"] == "deepseek-v4-pro[1m]"


async def test_marker_first_match_wins(make_proxy, relay_registry, claude_home, fake_provider):
    """Two markers across user messages: the FIRST in message order wins; scanning stops."""
    claude_home.write_keys(_ALL_KEYS)
    fake_provider.respond(status=200, body=b"ok")
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root))

    payload = {
        "model": "deepseek/m",
        "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "[chinamaxm model=never-assistant]"}]},
            {"role": "user", "content": [{"type": "text", "text": "[chinamaxm model=first-wins]\n\ntask"}]},
            {"role": "user", "content": "[chinamaxm model=second-loses]"},
        ],
    }
    response = await client.post(
        "/v1/messages", data=json.dumps(payload).encode(), skip_auto_headers=["Content-Type"]
    )
    assert response.status == 200
    assert json.loads(fake_provider.requests[-1].body)["model"] == "first-wins"


async def test_no_marker_leaves_egress_model_unchanged(
    make_proxy, relay_registry, claude_home, fake_provider
):
    """Without a marker the egress model is the prefix-stripped inbound model, unchanged.
    A marker NOT alone on its line (or mid-line) never fires (line-anchored regex)."""
    claude_home.write_keys(_ALL_KEYS)
    fake_provider.respond(status=200, body=b"ok")
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root))

    response = await client.post(
        "/v1/messages",
        data=_marker_body("deepseek/deepseek-v4-pro[1m]", None),
        skip_auto_headers=["Content-Type"],
    )
    assert response.status == 200
    assert json.loads(fake_provider.requests[-1].body)["model"] == "deepseek-v4-pro[1m]"

    # A quoted/inline mention never fires: the marker must be alone on its own line.
    fake_provider.respond(status=200, body=b"ok")
    payload = {
        "model": "deepseek/m",
        "messages": [
            {"role": "user", "content": "see the [chinamaxm model=inline-mention] syntax"}
        ],
    }
    response = await client.post(
        "/v1/messages", data=json.dumps(payload).encode(), skip_auto_headers=["Content-Type"]
    )
    assert response.status == 200
    assert json.loads(fake_provider.requests[-1].body)["model"] == "m"


async def test_default_branch_never_scanned_for_marker(proxy_client, fake_provider):
    """A prefixless body carrying a marker rides the Default branch BYTE-IDENTICAL —
    no body inspection, no model substitution (ADR 0001 guarantee untouched)."""
    body = json.dumps(
        {
            "model": "claude-opus-5",
            "messages": [
                {"role": "user", "content": "[chinamaxm model=must-not-apply]\n\nhello"}
            ],
        }
    ).encode()
    fake_provider.respond(status=200, body=b"ok")
    response = await proxy_client.post(
        "/v1/messages", data=body, skip_auto_headers=["Content-Type"]
    )
    assert response.status == 200
    assert fake_provider.requests[-1].body == body


async def test_count_tokens_counts_marker_substituted_body(
    make_proxy, relay_registry, claude_home, fake_provider
):
    """count_tokens forwards the SAME marker-substituted body messages would send, and its
    JSONL line logs the effective model."""
    claude_home.write_keys(_ALL_KEYS)
    fake_provider.respond(
        status=200, headers={"content-type": "application/json"}, body=b'{"input_tokens":7}'
    )
    lines: list[dict] = []
    client = await make_proxy(
        relay_registry, claude_home=str(claude_home.root), log_sink=lines.append
    )

    response = await client.post(
        "/v1/messages/count_tokens",
        data=_marker_body("deepseek/deepseek-v4-pro[1m]", "deepseek-v4-flash"),
        skip_auto_headers=["Content-Type"],
    )
    assert response.status == 200
    forwarded = json.loads(fake_provider.requests[-1].body)
    assert forwarded["model"] == "deepseek-v4-flash"
    assert fake_provider.requests[-1].raw_path == "/v1/messages/count_tokens"
    assert lines[0]["model"] == "deepseek-v4-flash"


async def test_relay_upstream_errors(make_proxy, relay_registry, claude_home, fake_provider):
    """Pins plan decisions: an upstream non-2xx relays verbatim; a connect failure ⇒ pinned 502."""
    claude_home.write_keys(_ALL_KEYS)

    # Case 1: a 429 with a JSON body relays verbatim (status + body).
    fake_provider.respond(
        status=429, headers={"content-type": "application/json"},
        body=b'{"error":{"type":"rate_limit_error"}}',
    )
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root))
    response = await client.post(
        "/v1/messages", data=json.dumps({"model": "deepseek/m"}).encode(), skip_auto_headers=["Content-Type"]
    )
    assert response.status == 429
    assert await response.read() == b'{"error":{"type":"rate_limit_error"}}'

    # Case 2: a connection failure ⇒ the pinned 502 body.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    base = load_registry()
    dead = replace(base.profiles["deepseek"], base_url=f"http://127.0.0.1:{dead_port}")
    registry = Registry(port=base.port, profiles={**base.profiles, "deepseek": dead})
    client = await make_proxy(registry, claude_home=str(claude_home.root))
    response = await client.post(
        "/v1/messages", data=json.dumps({"model": "deepseek/m"}).encode(), skip_auto_headers=["Content-Type"]
    )
    assert response.status == 502
    assert await response.json() == {
        "error": {"type": "api_error", "message": "upstream connection failed"}
    }
