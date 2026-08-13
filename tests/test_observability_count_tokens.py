"""Observability + count_tokens tests: the JSONL line, usage tee, and estimator fallback.

Exercised at the Proxy's HTTP surface (ADR 0011): the shared fake Anthropic provider stands
in for each Profile endpoint and either a direct ``/v1/messages`` client or the fake Codex
Responses client drives a branch. Log lines are captured through the injectable sink
(``log_sink``) except where the ADR file location / absence is itself under test, which reads
the real ``requests.jsonl``. The module name keeps the ``-k "log or count_tokens"`` filter
selecting every test.
"""

from __future__ import annotations

import asyncio
import json
import math
import socket
from dataclasses import replace

import pytest
from aiohttp import web

from chinamaxM.registry import Profile, Registry, load_registry

JSON_HEADERS = {"content-type": "application/json"}
SSE_HEADERS = {"content-type": "text/event-stream"}

#: Claude Key file contents covering every seed Profile (mirrors test_proxy).
_ALL_KEYS = {
    "DEEPSEEK_API_KEY": "k-deepseek",
    "MIMO_API_KEY": "k-mimo",
    "GLM_API_KEY": "k-glm",
    "MINIMAX_API_KEY": "k-minimax",
    "KIMI_API_KEY": "k-kimi",
    "QWEN_API_KEY": "k-qwen",
}


# --------------------------------------------------------------------------- builders


def _anthropic_json(usage: dict, *, text: str = "ok") -> bytes:
    """Serialize a complete Anthropic Messages response carrying ``usage``."""
    return json.dumps(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "provider-model",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": usage,
        }
    ).encode("utf-8")


def _frame(event: dict) -> bytes:
    """Frame one Anthropic SSE event as ``event:``/``data:`` bytes."""
    return (
        f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


def _message_start(usage: dict | None) -> bytes:
    """A ``message_start`` frame with (or without) a ``message.usage`` object."""
    message: dict = {"id": "msg_s", "model": "provider-model"}
    if usage is not None:
        message["usage"] = usage
    return _frame({"type": "message_start", "message": message})


def _user_text(text: str) -> dict:
    """A Responses user message input item."""
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def _function_tool(name: str) -> dict:
    """A Responses function tool definition."""
    return {
        "type": "function",
        "name": name,
        "description": f"the {name} tool",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    }


async def _await_records(records: list, count: int, *, timeout: float = 5.0) -> None:
    """Poll a captured-line list until it holds ``count`` records (bounded, to fail fast)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while len(records) < count:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"only {len(records)} of {count} log lines after {timeout}s")
        await asyncio.sleep(0.01)


def _assert_schema(line: dict, *, ingress: str) -> None:
    """Assert the mandatory JSONL field set is present and well-typed."""
    assert isinstance(line["ts"], str) and line["ts"].endswith("+00:00")
    assert line["ingress"] == ingress
    assert "model" in line and "profile" in line and "upstream" in line
    assert isinstance(line["status"], int)
    assert isinstance(line["latency"], float) and line["latency"] >= 0
    assert "usage" in line


# ------------------------------------------------------------------------------ tests


async def test_worker_line_fields_relay(make_proxy, relay_registry, claude_home, fake_provider):
    """AC-1: one relay request logs every schema field with the provider usage verbatim."""
    claude_home.write_keys(_ALL_KEYS)
    usage = {"input_tokens": 100, "output_tokens": 11, "cache_read_input_tokens": 2048}
    fake_provider.respond(status=200, headers=JSON_HEADERS, body=_anthropic_json(usage))
    records: list[dict] = []
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root), log_sink=records.append)

    response = await client.post(
        "/v1/messages",
        data=json.dumps({"model": "deepseek/deepseek-v4-pro[1m]", "messages": []}).encode(),
        skip_auto_headers=["Content-Type"],
    )
    assert response.status == 200

    # The worker-branch egress pins Accept-Encoding: identity so usage stays parseable
    # (proxy-02 set it; this slice asserts it).
    assert fake_provider.requests[-1].headers.get("Accept-Encoding") == "identity"

    assert len(records) == 1
    line = records[0]
    _assert_schema(line, ingress="anthropic")
    assert line["model"] == "deepseek-v4-pro[1m]"  # prefix stripped
    assert line["profile"] == "deepseek"
    assert line["upstream"].endswith("/v1/messages")
    assert line["status"] == 200
    assert line["usage"] == usage  # provider object verbatim
    assert "stripped_tools" not in line  # relay branch never strips
    assert "count_tokens_path" not in line


async def test_usage_variants_carried_verbatim(make_proxy, relay_registry, claude_home, fake_provider):
    """AC-1: kimi-style and qwen-style usage variants appear untouched in the log."""
    claude_home.write_keys(_ALL_KEYS)
    records: list[dict] = []
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root), log_sink=records.append)

    kimi_usage = {
        "input_tokens": 5,
        "output_tokens": 3,
        "cached_tokens": 2,
        "output_tokens_details": {"reasoning_tokens": 1},
    }
    fake_provider.respond(status=200, headers=JSON_HEADERS, body=_anthropic_json(kimi_usage))
    await client.post(
        "/v1/messages", data=json.dumps({"model": "kimi/kimi-k3"}).encode(), skip_auto_headers=["Content-Type"]
    )

    qwen_usage = {"input_tokens": 7, "output_tokens": 4, "prompt_tokens_details": {"cached_tokens": 3}}
    fake_provider.respond(status=200, headers=JSON_HEADERS, body=_anthropic_json(qwen_usage))
    await client.post(
        "/v1/messages", data=json.dumps({"model": "qwen/qwen3.8-max"}).encode(), skip_auto_headers=["Content-Type"]
    )

    assert records[0]["usage"] == kimi_usage  # nested + unknown keys preserved
    assert records[1]["usage"] == qwen_usage


async def test_stripped_tools_logged_iff_stripped(make_seam_client):
    """AC-2: a Seam request logs stripped_tools + the full field set iff the Seam stripped."""
    seam_usage = {"input_tokens": 50, "output_tokens": 8, "cache_read_input_tokens": 10}

    records: list[dict] = []
    client, fake = await make_seam_client("glm", log_sink=records.append)
    fake.respond(headers=JSON_HEADERS, body=_anthropic_json(seam_usage))
    await client.send(
        {
            "model": "glm-5.2",
            "input": [_user_text("hi")],
            "tools": [_function_tool("get_weather"), {"type": "web_search"}],
        }
    )
    assert len(records) == 1
    line = records[0]
    _assert_schema(line, ingress="responses")
    assert line["model"] == "glm-5.2"
    assert line["profile"] == "glm"
    assert line["upstream"].endswith("/v1/messages")
    assert line["status"] == 200
    assert line["stripped_tools"] == ["web_search"]
    assert line["usage"] == seam_usage  # provider usage verbatim, non-null

    # Function-tools-only ⇒ the key is absent entirely.
    records2: list[dict] = []
    client2, fake2 = await make_seam_client("glm", log_sink=records2.append)
    fake2.respond(headers=JSON_HEADERS, body=_anthropic_json(seam_usage))
    await client2.send({"model": "glm-5.2", "input": [_user_text("hi")], "tools": [_function_tool("f")]})
    assert len(records2) == 1
    assert "stripped_tools" not in records2[0]


async def test_default_branch_never_logged(make_proxy, claude_home, fake_provider):
    """AC-3: N Default-branch requests leave the log file ABSENT (not merely empty)."""
    claude_home.write_keys(_ALL_KEYS)
    fake_provider.respond(status=200, body=b"ok")
    client = await make_proxy(load_registry(), claude_home=str(claude_home.root))  # real writer

    for _ in range(3):
        response = await client.post(
            "/v1/messages",
            data=json.dumps({"model": "claude-opus-5", "messages": []}).encode(),
            skip_auto_headers=["Content-Type"],
        )
        assert response.status == 200

    log_path = claude_home.root / "chinamaxM" / "requests.jsonl"
    assert not log_path.exists()


async def test_count_tokens_forwarded_when_supported(make_proxy, relay_registry, claude_home, fake_provider):
    """AC-4: a supported count_tokens forwards the mutated body and relays the answer."""
    claude_home.write_keys(_ALL_KEYS)
    fake_provider.respond(status=200, headers=JSON_HEADERS, body=b'{"input_tokens":123}')
    records: list[dict] = []
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root), log_sink=records.append)

    inbound = {
        "model": "deepseek/deepseek-v4-pro[1m]",
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "output_config": {"foo": "bar"},
        "messages": [{"role": "user", "content": "hi"}],
    }
    response = await client.post(
        "/v1/messages/count_tokens", data=json.dumps(inbound).encode(), skip_auto_headers=["Content-Type"]
    )
    assert response.status == 200
    assert (await response.json())["input_tokens"] == 123

    # What got counted is the mutated egress body (thinking@root, prefix stripped).
    counted = json.loads(fake_provider.requests[-1].body)
    assert counted["model"] == "deepseek-v4-pro[1m]"
    assert counted["reasoning"] == {"effort": "max"}
    assert "thinking" not in counted and "output_config" not in counted
    assert fake_provider.requests[-1].raw_path.endswith("/v1/messages/count_tokens")

    assert records[-1]["count_tokens_path"] == "upstream"
    assert records[-1]["status"] == 200
    assert records[-1]["usage"] is None  # count_tokens lines never carry usage


@pytest.mark.parametrize(
    "status, body",
    [
        (404, b"not found"),
        (405, b"nope"),
        (501, b"unsupported"),
        (200, b"{}"),
        (200, b"not json at all"),
        (200, b'{"input_tokens":"123"}'),
    ],
    ids=["404", "405", "501", "empty-object", "non-json", "string-value"],
)
async def test_count_tokens_estimator_fallback(
    make_proxy, relay_registry, claude_home, fake_provider, status, body
):
    """AC-5: 404/405/501/malformed-2xx ⇒ ceil(chars/4) estimate, status 200, path estimated."""
    claude_home.write_keys(_ALL_KEYS)
    fake_provider.respond(status=status, headers=JSON_HEADERS, body=body)
    records: list[dict] = []
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root), log_sink=records.append)

    inbound = {"model": "deepseek/m", "messages": [{"role": "user", "content": "日本語のテスト"}]}
    response = await client.post(
        "/v1/messages/count_tokens", data=json.dumps(inbound).encode(), skip_auto_headers=["Content-Type"]
    )
    assert response.status == 200

    # Expectation computed independently from the CAPTURED mutated egress body (chars, not
    # bytes — the non-ASCII text makes the two differ).
    captured = fake_provider.requests[-1].body
    expected = math.ceil(len(captured.decode("utf-8")) / 4)
    assert len(captured.decode("utf-8")) != len(captured)  # non-ASCII really is multi-byte
    assert (await response.json())["input_tokens"] == expected

    assert records[-1]["count_tokens_path"] == "estimated"
    assert records[-1]["status"] == 200


@pytest.mark.parametrize(
    "status, body",
    [(404, b"x"), (405, b"x"), (501, b"x"), (200, b"{}")],
    ids=["404", "405", "501", "malformed-2xx"],
)
async def test_count_tokens_support_cached(
    make_proxy, relay_registry, claude_home, fake_provider, status, body
):
    """AC-6: after one unsupported answer, a second call for that Profile skips the upstream."""
    claude_home.write_keys(_ALL_KEYS)
    fake_provider.respond(status=status, headers=JSON_HEADERS, body=body)
    records: list[dict] = []
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root), log_sink=records.append)

    async def count(model):
        return await client.post(
            "/v1/messages/count_tokens",
            data=json.dumps({"model": model, "messages": []}).encode(),
            skip_auto_headers=["Content-Type"],
        )

    assert (await count("deepseek/m")).status == 200
    assert len(fake_provider.requests) == 1  # first call probed upstream

    assert (await count("deepseek/m")).status == 200
    assert len(fake_provider.requests) == 1  # second call skipped upstream (cached)
    assert records[-1]["count_tokens_path"] == "estimated"

    # A different Profile is not cached and still forwards.
    assert (await count("mimo/m")).status == 200
    assert len(fake_provider.requests) == 2


async def test_streaming_usage_merged_across_events(make_proxy, relay_registry, claude_home, fake_provider):
    """AC-1: streaming usage merges message_start + message_delta; a no-usage stream ⇒ null."""
    claude_home.write_keys(_ALL_KEYS)
    records: list[dict] = []
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root), log_sink=records.append)

    chunks = [
        _message_start(
            {"input_tokens": 2048, "cache_read_input_tokens": 1984, "cache_creation_input_tokens": 7, "output_tokens": 0}
        ),
        _frame({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        _frame({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}}),
        _frame({"type": "content_block_stop", "index": 0}),
        _frame({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 11}}),
        _frame({"type": "message_stop"}),
    ]
    fake_provider.respond_stream(chunks, status=200, headers=SSE_HEADERS, gated=False)
    response = await client.post(
        "/v1/messages", data=json.dumps({"model": "deepseek/m"}).encode(), skip_auto_headers=["Content-Type"]
    )
    await response.read()
    await _await_records(records, 1)
    assert records[-1]["usage"] == {
        "input_tokens": 2048,
        "cache_read_input_tokens": 1984,
        "cache_creation_input_tokens": 7,
        "output_tokens": 11,  # message_delta overrode the message_start 0
    }

    # A stream with no usage-carrying event ⇒ usage null.
    records2: list[dict] = []
    client2 = await make_proxy(relay_registry, claude_home=str(claude_home.root), log_sink=records2.append)
    fake_provider.respond_stream(
        [
            _message_start(None),
            _frame({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            _frame({"type": "content_block_stop", "index": 0}),
            _frame({"type": "message_stop"}),
        ],
        status=200,
        headers=SSE_HEADERS,
        gated=False,
    )
    response2 = await client2.post(
        "/v1/messages", data=json.dumps({"model": "deepseek/m"}).encode(), skip_auto_headers=["Content-Type"]
    )
    await response2.read()
    await _await_records(records2, 1)
    assert records2[-1]["usage"] is None


async def test_log_location_matches_adr(make_proxy, relay_registry, claude_home, fake_provider):
    """AC-7: the request log lands at <root>/chinamaxM/requests.jsonl (real writer)."""
    claude_home.write_keys(_ALL_KEYS)
    fake_provider.respond(
        status=200, headers=JSON_HEADERS, body=_anthropic_json({"input_tokens": 1, "output_tokens": 1})
    )
    client = await make_proxy(relay_registry, claude_home=str(claude_home.root))  # real writer

    response = await client.post(
        "/v1/messages", data=json.dumps({"model": "deepseek/m"}).encode(), skip_auto_headers=["Content-Type"]
    )
    assert response.status == 200

    log_path = claude_home.root / "chinamaxM" / "requests.jsonl"
    assert log_path.exists()
    lines = [json.loads(text) for text in log_path.read_text().splitlines() if text]
    assert len(lines) == 1
    assert lines[0]["profile"] == "deepseek"
    assert lines[0]["ingress"] == "anthropic"


async def test_error_paths_logged(make_proxy, make_seam_client, relay_registry, claude_home, fake_provider):
    """AC-1: each error path logs exactly one line with its status and null/partial usage."""
    # (a) Relay 401 missing key.
    claude_home.write_text("")  # no keys
    records_a: list[dict] = []
    client_a = await make_proxy(relay_registry, claude_home=str(claude_home.root), log_sink=records_a.append)
    r = await client_a.post(
        "/v1/messages", data=json.dumps({"model": "deepseek/m"}).encode(), skip_auto_headers=["Content-Type"]
    )
    assert r.status == 401
    assert len(records_a) == 1 and records_a[0]["status"] == 401 and records_a[0]["usage"] is None
    assert fake_provider.requests == []

    # (b) Seam 400 unsupported continuation (previous_response_id with empty input).
    records_b: list[dict] = []
    client_b, _ = await make_seam_client("glm", log_sink=records_b.append)
    rb = await client_b.send({"model": "glm-5.2", "input": [], "previous_response_id": "resp_x"})
    assert rb.status == 400
    assert len(records_b) == 1 and records_b[0]["status"] == 400 and records_b[0]["usage"] is None
    assert records_b[0]["ingress"] == "responses"

    # (c) Upstream non-2xx (500) ⇒ status 500, usage null.
    claude_home.write_keys(_ALL_KEYS)
    records_c: list[dict] = []
    client_c = await make_proxy(relay_registry, claude_home=str(claude_home.root), log_sink=records_c.append)
    fake_provider.respond(status=500, headers=JSON_HEADERS, body=b'{"error":{"type":"server_error"}}')
    rc = await client_c.post(
        "/v1/messages", data=json.dumps({"model": "deepseek/m"}).encode(), skip_auto_headers=["Content-Type"]
    )
    assert rc.status == 500
    assert records_c[-1]["status"] == 500 and records_c[-1]["usage"] is None

    start_usage = {"input_tokens": 321, "cache_read_input_tokens": 300, "output_tokens": 0}

    # (d) Provider aborts mid-SSE after message_start ⇒ status 200, usage merged so far.
    records_d: list[dict] = []
    client_d = await make_proxy(relay_registry, claude_home=str(claude_home.root), log_sink=records_d.append)
    fake_provider.respond_stream([_message_start(start_usage)], status=200, headers=SSE_HEADERS, gated=False)
    rd = await client_d.post(
        "/v1/messages", data=json.dumps({"model": "deepseek/m"}).encode(), skip_auto_headers=["Content-Type"]
    )
    await rd.read()
    await _await_records(records_d, 1)
    assert records_d[-1]["status"] == 200
    assert records_d[-1]["usage"] == start_usage

    # (e) Client disconnects mid-SSE after message_start ⇒ status 200, usage merged so far.
    records_e: list[dict] = []
    client_e = await make_proxy(relay_registry, claude_home=str(claude_home.root), log_sink=records_e.append)
    fake_provider.respond_stream(
        [_message_start(start_usage), b"event: ping\ndata: {}\n\n"], status=200, headers=SSE_HEADERS, gated=True
    )
    re = await client_e.post(
        "/v1/messages", data=json.dumps({"model": "deepseek/m"}).encode(), skip_auto_headers=["Content-Type"]
    )
    assert re.status == 200
    await asyncio.wait_for(re.content.readuntil(b"\n\n"), timeout=5)  # observe message_start
    re.close()  # client disconnect
    fake_provider.release(0)  # let the fake push the next chunk into the dead client
    await _await_records(records_e, 1)
    assert records_e[-1]["status"] == 200
    assert records_e[-1]["usage"] == start_usage


async def test_count_tokens_transport_failure_estimates(
    make_proxy, claude_home, aiohttp_server, fake_provider
):
    """AC-5: a transport failure estimates (uncached) — a later bind then forwards upstream."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    base = load_registry()
    deepseek = replace(base.profiles["deepseek"], base_url=f"http://127.0.0.1:{dead_port}")
    registry = Registry(port=base.port, profiles={**base.profiles, "deepseek": deepseek})
    claude_home.write_keys(_ALL_KEYS)
    records: list[dict] = []
    client = await make_proxy(registry, claude_home=str(claude_home.root), log_sink=records.append)

    async def count():
        return await client.post(
            "/v1/messages/count_tokens",
            data=json.dumps({"model": "deepseek/m", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            skip_auto_headers=["Content-Type"],
        )

    # Nothing is listening ⇒ transport failure ⇒ estimate (a positive int), path estimated.
    r1 = await count()
    assert r1.status == 200
    first = await r1.json()
    assert isinstance(first["input_tokens"], int) and first["input_tokens"] > 0
    assert records[-1]["count_tokens_path"] == "estimated"

    # Bind a provider on the previously-dead port; the second call forwards (not cached).
    async def _handler(request):
        await request.read()
        return web.json_response({"input_tokens": 7})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", _handler)
    await aiohttp_server(app, port=dead_port)

    r2 = await count()
    assert r2.status == 200
    assert (await r2.json())["input_tokens"] == 7  # forwarded upstream, proves no negative cache
    assert records[-1]["count_tokens_path"] == "upstream"
