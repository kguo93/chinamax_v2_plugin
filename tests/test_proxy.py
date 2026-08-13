"""Proxy tests: loopback bind, byte-for-byte passthrough, routing, streaming (AC-2..7)."""

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

from chinamaxM.proxy import LOOPBACK_HOST, create_app
from chinamaxM.registry import Profile, Registry, load_registry

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


async def test_profile_prefix_routes(make_proxy, fake_provider):
    """AC-5: known prefix routes+strips, unknown ⇒ 404+list, prefixless ⇒ Default."""
    base = load_registry()
    profiles = dict(base.profiles)
    profiles["zed"] = Profile(
        name="zed",
        dialect="anthropic",
        base_url="https://api.zed.example/anthropic",
        api_key_env="ZED_API_KEY",
        default_model="zed-1",
    )
    registry = Registry(port=base.port, profiles=profiles)
    client = await make_proxy(registry, routing_debug=True)

    async def post_model(model, cli=client):
        return await cli.post(
            "/v1/messages", data=json.dumps({"model": model}).encode(), skip_auto_headers=["Content-Type"]
        )

    # Known seed prefix; the rest is never validated.
    response = await post_model("deepseek/anything-at-all")
    assert response.status == 501
    assert response.headers["X-Chinamaxm-Matched-Profile"] == "deepseek"

    # Overlay-added prefix routes too (routing is Registry-derived, not hardcoded).
    response = await post_model("zed/x")
    assert response.status == 501
    assert response.headers["X-Chinamaxm-Matched-Profile"] == "zed"

    # Unknown profile ⇒ local 404 naming every valid Profile.
    response = await post_model("unknownprof/x")
    assert response.status == 404
    payload = await response.json()
    assert payload["error"]["type"] == "unknown_profile"
    for name in registry.profiles:
        assert name in payload["error"]["message"]

    # Profile matching is case-sensitive.
    response = await post_model("Deepseek/x")
    assert response.status == 404

    # No slash ⇒ Default branch (reaches the upstream), never routed.
    fake_provider.respond(status=200, body=b"defaulted")
    response = await post_model("deepseek-v4-pro")
    assert response.status == 200
    assert await response.read() == b"defaulted"
    assert fake_provider.requests[-1].body == json.dumps({"model": "deepseek-v4-pro"}).encode()

    # The debug header is ABSENT under the default constructor.
    plain = await make_proxy(registry)
    response = await post_model("deepseek/x", cli=plain)
    assert response.status == 501
    assert "X-Chinamaxm-Matched-Profile" not in response.headers


async def test_matched_returns_501_marker(proxy_client):
    """AC-5: matched Profiles ⇒ 501 with the exact not_implemented JSON body."""
    for model in ("deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro[1m]"):
        response = await proxy_client.post(
            "/v1/messages", data=json.dumps({"model": model}).encode(), skip_auto_headers=["Content-Type"]
        )
        assert response.status == 501
        assert response.content_type == "application/json"
        assert await response.json() == {
            "error": {"type": "not_implemented", "message": "relay not yet implemented"}
        }


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
