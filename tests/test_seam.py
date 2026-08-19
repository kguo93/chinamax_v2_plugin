"""Seam tests: Responses ingress translation, streaming, signatures, and startup gates.

These exercise proxy-03's Responses ingress end-to-end at the HTTP surface (ADR 0011): a
fake Codex Responses client drives ``/openai/<profile>/responses`` and the shared fake
Anthropic provider stands in for the Profile endpoint. LiteLLM runs for real (never mocked);
the tripwire fixture in ``conftest`` fails any test that reaches a banned transport entry.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

from chinamaxM.registry import Profile, Registry

JSON_HEADERS = {"Content-Type": "application/json"}
SSE_HEADERS = {"Content-Type": "text/event-stream"}


# --------------------------------------------------------------------------- builders


def anthropic_message(content: list, *, stop_reason: str = "end_turn", usage: dict | None = None) -> dict:
    """Build a complete Anthropic Messages response body."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "provider-model",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 100, "output_tokens": 20},
    }


def message_bytes(content: list, **kwargs) -> bytes:
    """Serialize an Anthropic response for the fake provider's ``respond`` body."""
    return json.dumps(anthropic_message(content, **kwargs)).encode("utf-8")


def frame(event: dict) -> bytes:
    """Frame one Anthropic SSE event as ``event:``/``data:`` bytes."""
    return f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode("utf-8")


def user_text(text: str) -> dict:
    """A Responses user message input item."""
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def assistant_text(text: str) -> dict:
    """A Responses assistant message input item."""
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def function_tool(name: str) -> dict:
    """A Responses function tool definition."""
    return {
        "type": "function",
        "name": name,
        "description": f"the {name} tool",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    }


class SSEStreamReader:
    """Read a Responses SSE response frame by frame (for causal, gated streaming tests)."""

    def __init__(self, response) -> None:
        self._response = response
        self._buffer = b""

    async def read(self, count: int) -> list[dict]:
        """Read exactly ``count`` SSE event dicts, blocking until each frame arrives."""
        events: list[dict] = []
        while len(events) < count:
            while b"\n\n" not in self._buffer:
                chunk = await self._response.content.read(128)
                if not chunk:
                    raise AssertionError(f"stream ended after {len(events)} of {count} events")
                self._buffer += chunk
            block, self._buffer = self._buffer.split(b"\n\n", 1)
            for line in block.split(b"\n"):
                if line.startswith(b"data:"):
                    events.append(json.loads(line[len(b"data:"):].strip()))
        return events


# ------------------------------------------------------------------------------ tests


async def test_seam_full_shape(make_seam_client):
    """AC-1: function tools + web_search + reasoning ⇒ valid Anthropic Messages egress."""
    client, fake = await make_seam_client("glm")
    fake.respond(headers=JSON_HEADERS, body=message_bytes([{"type": "text", "text": "ok"}]))

    response = await client.send(
        {
            "model": "glm-5.2",
            "input": [user_text("what is the weather?")],
            "tools": [function_tool("get_weather"), function_tool("get_time"), {"type": "web_search"}],
            "reasoning": {"effort": "high"},
        }
    )
    assert response.status == 200

    sent = json.loads(fake.requests[0].body)
    assert sent["thinking"] == {"type": "enabled"}  # glm Profile policy at body root
    assert "reasoning" not in sent  # the Responses reasoning param was stripped
    assert {tool["name"] for tool in sent["tools"]} == {"get_weather", "get_time"}
    for tool in sent["tools"]:
        assert "type" not in tool  # the injected type:"custom" was stripped
        assert tool["input_schema"]["type"] == "object"  # nested JSON Schema untouched
    assert "web_search" not in json.dumps(sent)  # the non-function tool never crossed


async def test_input_item_without_a_type_is_a_message(make_seam_client):
    """A `type`-less input item translates exactly as the same item with type:"message"."""
    client, fake = await make_seam_client("glm")
    fake.respond(headers=JSON_HEADERS, body=message_bytes([{"type": "text", "text": "ok"}]))

    typed = user_text("ping")
    bare = {key: value for key, value in typed.items() if key != "type"}
    for item in (bare, typed):
        response = await client.send({"model": "glm-5.2", "input": [item]})
        assert response.status == 200

    from_bare = json.loads(fake.requests[0].body)["messages"]
    assert from_bare[0]["role"] == "user"
    assert from_bare == json.loads(fake.requests[1].body)["messages"]


async def test_stripped_tools_recorded(make_seam_client):
    """AC-2: the injectable log sink receives the stripped tool types (proxy-04's boundary)."""
    records: list[dict] = []
    client, fake = await make_seam_client("glm", log_sink=records.append)
    fake.respond(headers=JSON_HEADERS, body=message_bytes([{"type": "text", "text": "ok"}]))

    await client.send(
        {
            "model": "glm-5.2",
            "input": [user_text("hi")],
            "tools": [function_tool("get_weather"), {"type": "web_search"}],
        }
    )
    assert records, "log sink received no record"
    assert records[-1]["stripped_tools"] == ["web_search"]


async def test_streaming_translation_incremental(make_seam_client):
    """AC-3: gated Anthropic SSE ⇒ a valid incremental Responses event sequence."""
    client, fake = await make_seam_client("deepseek")

    delta_one = frame(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"q":"S'}}
    )
    split = len(delta_one) // 2  # one transport boundary lands MID-EVENT (proves reframing)
    chunks = [
        frame(
            {
                "type": "message_start",
                "message": {
                    "id": "msg_stream",
                    "model": "provider-model",
                    "usage": {
                        "input_tokens": 2048,
                        "cache_read_input_tokens": 1984,
                        "cache_creation_input_tokens": 7,
                        "output_tokens": 0,
                    },
                },
            }
        ),
        frame(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_A", "name": "get_weather", "input": {}},
            }
        ),
        delta_one[:split],
        delta_one[split:]
        + frame({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": 'F"}'}})
        + frame({"type": "content_block_stop", "index": 0})
        + frame({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 11}})
        + frame({"type": "message_stop"}),
    ]
    fake.respond_stream(chunks, headers=SSE_HEADERS, gated=True)

    response = await client.stream_response({"model": "deepseek-v4-pro[1m]", "input": [user_text("weather?")]})
    assert response.status == 200
    reader = SSEStreamReader(response)

    await fake.wait_written(0)
    lifecycle = await reader.read(2)  # observed BEFORE releasing later chunks ⇒ incremental
    assert [event["type"] for event in lifecycle] == ["response.created", "response.in_progress"]
    fake.release(0)

    await fake.wait_written(1)
    added = await reader.read(1)
    assert added[0]["type"] == "response.output_item.added"
    assert added[0]["item"]["type"] == "function_call"
    assert added[0]["item"]["call_id"] == "toolu_A"  # call_id passthrough
    item_id = added[0]["item"]["id"]
    fake.release(1)

    await fake.wait_written(2)  # the mid-event partial chunk produces no client event yet
    fake.release(2)

    tail = await reader.read(5)  # 2 arg deltas, arg done, item done, terminal
    types = [event["type"] for event in tail]
    assert types == [
        "response.function_call_arguments.delta",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    ]
    reassembled = "".join(
        event["delta"] for event in tail if event["type"] == "response.function_call_arguments.delta"
    )
    assert reassembled == '{"q":"SF"}'
    assert tail[2]["arguments"] == '{"q":"SF"}'
    assert all(event["item_id"] == item_id for event in tail[:3])  # stable item id (delta/done)
    assert tail[3]["item"]["id"] == item_id  # output_item.done carries the item, not item_id

    all_events = lifecycle + added + tail
    sequence = [event["sequence_number"] for event in all_events]
    assert sequence == sorted(sequence) and len(set(sequence)) == len(sequence)

    usage = tail[-1]["response"]["usage"]
    assert usage["input_tokens"] == 2048
    assert usage["input_tokens_details"]["cached_tokens"] == 1984
    assert usage["output_tokens"] == 11


async def test_streaming_text_before_thinking(make_seam_client):
    """AC-3 variant: mimo shape (text before thinking) translates with correct item order."""
    client, fake = await make_seam_client("mimo")
    events = [
        {"type": "message_start", "message": {"id": "msg_m", "model": "mimo", "usage": {"input_tokens": 5, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "thinking_delta", "thinking": "pondering"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "signature_delta", "signature": "SIG=="}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
    ]
    fake.respond_stream([frame(event) for event in events], headers=SSE_HEADERS, gated=False)

    out = await client.send_streaming({"model": "mimo-v2.5-pro", "input": [user_text("hi")]})
    added = [event["item"]["type"] for event in out if event["type"] == "response.output_item.added"]
    assert added == ["message", "reasoning"]  # arrival order preserved (text before thinking)
    assert out[-1]["type"] == "response.completed"


async def test_append_only_under_coalescing_pattern(make_seam_client):
    """AC-4: the [tool_result]-then-text shape keeps every prior message byte-identical."""
    client, fake = await make_seam_client("glm")
    fake.respond(headers=JSON_HEADERS, body=message_bytes([{"type": "text", "text": "ok"}]))

    turn_n = [
        user_text("weather?"),
        {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": '{"q":"SF"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "sunny"},
    ]
    turn_n1 = turn_n + [assistant_text("It is sunny.")]

    await client.send({"model": "glm-5.2", "input": turn_n})
    await client.send({"model": "glm-5.2", "input": turn_n1})

    messages_n = json.loads(fake.requests[0].body)["messages"]
    messages_n1 = json.loads(fake.requests[1].body)["messages"]
    assert messages_n == messages_n1[: len(messages_n)]  # strict list prefix

    serialized_n = json.dumps(messages_n, separators=(",", ":"))
    serialized_n1 = json.dumps(messages_n1, separators=(",", ":"))
    assert serialized_n1.startswith(serialized_n[:-1])  # common prefix through turn-N's last message


async def test_parallel_tool_calls_grouping(make_seam_client):
    """AC-4: a run of 2 function_calls + 2 outputs ⇒ one assistant + one user message."""
    client, fake = await make_seam_client("glm")
    fake.respond(headers=JSON_HEADERS, body=message_bytes([{"type": "text", "text": "ok"}]))

    base = [
        user_text("both please"),
        {"type": "function_call", "call_id": "c1", "name": "get_weather", "arguments": '{"q":"A"}'},
        {"type": "function_call", "call_id": "c2", "name": "get_time", "arguments": '{"q":"B"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "A-sunny"},
        {"type": "function_call_output", "call_id": "c2", "output": "B-noon"},
    ]
    await client.send({"model": "glm-5.2", "input": base})
    messages = json.loads(fake.requests[0].body)["messages"]

    assistant = [m for m in messages if m["role"] == "assistant"][0]
    tool_uses = [block for block in assistant["content"] if block["type"] == "tool_use"]
    assert [block["id"] for block in tool_uses] == ["c1", "c2"]  # one message, order + ids verbatim

    tool_result_msgs = [
        m for m in messages if m["role"] == "user" and any(b["type"] == "tool_result" for b in m["content"])
    ]
    assert len(tool_result_msgs) == 1
    results = [block for block in tool_result_msgs[0]["content"] if block["type"] == "tool_result"]
    assert [block["tool_use_id"] for block in results] == ["c1", "c2"]

    # The following turn appends without rewriting those bytes.
    await client.send({"model": "glm-5.2", "input": base + [assistant_text("done")]})
    next_messages = json.loads(fake.requests[1].body)["messages"]
    assert messages == next_messages[: len(messages)]


async def test_output_run_split_by_developer_message_regroups(make_seam_client):
    """A developer message interleaved between a run's outputs never splits the result run.

    Live shape from the 2026-08-18 Codex incident: a PreToolUse hook's additionalContext
    lands as a developer message between two parallel-call outputs; the grouped results
    must still be the single message immediately after their tool_use turn.
    """
    client, fake = await make_seam_client("deepseek")
    fake.respond(headers=JSON_HEADERS, body=message_bytes([{"type": "text", "text": "ok"}]))

    hook_notice = {
        "type": "message",
        "role": "developer",
        "content": [{"type": "input_text", "text": "STOP - hook notice"}],
    }
    base = [
        user_text("both please"),
        {"type": "function_call", "call_id": "c1", "name": "get_weather", "arguments": '{"q":"A"}'},
        {"type": "function_call", "call_id": "c2", "name": "get_time", "arguments": '{"q":"B"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "A-sunny"},
        hook_notice,
        {"type": "function_call_output", "call_id": "c2", "output": "B-noon"},
    ]
    await client.send({"model": "deepseek-v4-pro[1m]", "input": base})
    body = json.loads(fake.requests[0].body)
    messages = body["messages"]

    assistant_index = next(i for i, m in enumerate(messages) if m["role"] == "assistant")
    results_msg = messages[assistant_index + 1]
    assert results_msg["role"] == "user"
    results = [block for block in results_msg["content"] if block["type"] == "tool_result"]
    assert [block["tool_use_id"] for block in results] == ["c1", "c2"]

    # The hook text folds into the top-level system, never into a message.
    assert "hook notice" in json.dumps(body.get("system", []))
    assert all("hook notice" not in json.dumps(m) for m in messages)

    # The following turn appends without rewriting those bytes.
    await client.send({"model": "deepseek-v4-pro[1m]", "input": base + [assistant_text("done")]})
    next_messages = json.loads(fake.requests[1].body)["messages"]
    assert messages == next_messages[: len(messages)]


async def test_output_run_split_by_user_message_defers(make_seam_client):
    """A user message interleaved between a run's outputs defers to after the grouped results."""
    client, fake = await make_seam_client("deepseek")
    fake.respond(headers=JSON_HEADERS, body=message_bytes([{"type": "text", "text": "ok"}]))

    base = [
        user_text("both please"),
        {"type": "function_call", "call_id": "c1", "name": "get_weather", "arguments": '{"q":"A"}'},
        {"type": "function_call", "call_id": "c2", "name": "get_time", "arguments": '{"q":"B"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "A-sunny"},
        user_text("keep working"),
        {"type": "function_call_output", "call_id": "c2", "output": "B-noon"},
    ]
    await client.send({"model": "deepseek-v4-pro[1m]", "input": base})
    messages = json.loads(fake.requests[0].body)["messages"]

    assistant_index = next(i for i, m in enumerate(messages) if m["role"] == "assistant")
    results_msg = messages[assistant_index + 1]
    assert results_msg["role"] == "user"
    results = [block for block in results_msg["content"] if block["type"] == "tool_result"]
    assert [block["tool_use_id"] for block in results] == ["c1", "c2"]

    deferred = messages[assistant_index + 2]
    assert deferred["role"] == "user"
    assert "keep working" in json.dumps(deferred)

    # The following turn appends without rewriting those bytes.
    await client.send({"model": "deepseek-v4-pro[1m]", "input": base + [assistant_text("done")]})
    next_messages = json.loads(fake.requests[1].body)["messages"]
    assert messages == next_messages[: len(messages)]


async def test_signature_roundtrip(make_seam_client):
    """AC-5: a signed thinking block survives the Responses round-trip and replays verbatim."""
    client, fake = await make_seam_client("deepseek")
    signed = {"type": "thinking", "thinking": "deep thought", "signature": "SIGVALUE=="}
    fake.respond(headers=JSON_HEADERS, body=message_bytes([signed, {"type": "text", "text": "answer"}]))

    first = await client.send({"model": "deepseek-v4-pro[1m]", "input": [user_text("q1")]})
    body = await first.json()
    reasoning_item = [item for item in body["output"] if item["type"] == "reasoning"][0]
    encrypted = reasoning_item["encrypted_content"]
    assert encrypted  # always emitted (Codex needs it for replay)

    replay = [
        user_text("q1"),
        {"type": "reasoning", "id": reasoning_item["id"], "summary": [], "encrypted_content": encrypted},
        assistant_text("answer"),
    ]
    await client.send({"model": "deepseek-v4-pro[1m]", "input": replay})
    await client.send({"model": "deepseek-v4-pro[1m]", "input": replay})

    egress_one = fake.requests[1].body
    egress_two = fake.requests[2].body
    assert egress_one == egress_two  # two replays serialize byte-identically

    messages = json.loads(egress_one)["messages"]
    thinking_msg = [m for m in messages if any(b.get("type") == "thinking" for b in m["content"])][0]
    thinking_block = [b for b in thinking_msg["content"] if b["type"] == "thinking"][0]
    assert thinking_block["thinking"] == "deep thought"
    assert thinking_block["signature"] == "SIGVALUE=="


async def test_two_reasoning_items_restore_in_order(make_seam_client):
    """AC-5 variant: two consecutive reasoning items restore in arrival order at the head."""
    client, fake = await make_seam_client("deepseek")
    fake.respond(headers=JSON_HEADERS, body=message_bytes([{"type": "text", "text": "x"}]))

    from chinamaxM import seam

    env1 = seam.encode_thinking_block({"type": "thinking", "thinking": "first", "signature": "S1"})
    env2 = seam.encode_thinking_block({"type": "thinking", "thinking": "second", "signature": "S2"})
    items = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": env1},
        {"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": env2},
        {"type": "function_call", "call_id": "c9", "name": "get_weather", "arguments": "{}"},
    ]
    await client.send({"model": "deepseek-v4-pro[1m]", "input": items})

    messages = json.loads(fake.requests[0].body)["messages"]
    assistant = [m for m in messages if m["role"] == "assistant"][0]
    kinds = [block["type"] for block in assistant["content"]]
    assert kinds == ["thinking", "thinking", "tool_use"]
    assert assistant["content"][0]["thinking"] == "first"
    assert assistant["content"][1]["thinking"] == "second"


async def test_unsigned_thinking_roundtrip(make_seam_client):
    """AC-6: unsigned thinking round-trips; a foreign envelope ⇒ 400 with nothing upstream."""
    client, fake = await make_seam_client("kimi")
    unsigned = {"type": "thinking", "thinking": "no signature here"}
    fake.respond(headers=JSON_HEADERS, body=message_bytes([unsigned, {"type": "text", "text": "answer"}]))

    first = await client.send({"model": "kimi-k3", "input": [user_text("q")]})
    reasoning_item = [item for item in (await first.json())["output"] if item["type"] == "reasoning"][0]

    replay = [
        user_text("q"),
        {"type": "reasoning", "id": reasoning_item["id"], "summary": [], "encrypted_content": reasoning_item["encrypted_content"]},
        assistant_text("answer"),
    ]
    await client.send({"model": "kimi-k3", "input": replay})
    messages = json.loads(fake.requests[1].body)["messages"]
    thinking_block = [
        block
        for message in messages
        for block in message["content"]
        if block.get("type") == "thinking"
    ][0]
    assert thinking_block["thinking"] == "no signature here"
    assert "signature" not in thinking_block

    # A foreign / corrupt envelope is rejected before anything is sent upstream.
    before = len(fake.requests)
    bad = [
        user_text("q"),
        {"type": "reasoning", "id": "rs_bad", "summary": [], "encrypted_content": "not-a-valid-envelope"},
    ]
    response = await client.send({"model": "kimi-k3", "input": bad})
    assert response.status == 400
    assert (await response.json())["error"]["type"] == "invalid_request_error"
    assert len(fake.requests) == before  # nothing new reached the provider


def _run_boot(code: str, tmp_path) -> subprocess.CompletedProcess:
    """Import chinamaxM.seam in a subprocess with the Host roots pinned to temp dirs."""
    import os

    env = dict(os.environ)
    env["CHINAMAXM_CLAUDE_HOME"] = str(tmp_path / "claude")
    env["CHINAMAXM_CODEX_HOME"] = str(tmp_path / "codex")
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        env=env,
    )


def test_startup_pins_and_knobs(tmp_path):
    """AC-7: startup fails on a bad litellm; a healthy boot sets the knobs (subprocesses)."""
    mismatch = _run_boot(
        """
        import importlib.metadata as m
        _orig = m.version
        m.version = lambda name, *a, **k: '1.0.0' if name == 'litellm' else _orig(name, *a, **k)
        try:
            import chinamaxM.seam
            print('NO_ERROR')
        except RuntimeError:
            print('BOOT_FAILED')
        """,
        tmp_path,
    )
    assert "BOOT_FAILED" in mismatch.stdout, (mismatch.stdout, mismatch.stderr)

    absent = _run_boot(
        """
        import sys
        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name == 'litellm' or name.startswith('litellm.'):
                    raise ModuleNotFoundError('blocked')
                return None
        sys.meta_path.insert(0, Blocker())
        try:
            import chinamaxM.seam
            print('NO_ERROR')
        except (RuntimeError, ImportError):
            print('BOOT_FAILED')
        """,
        tmp_path,
    )
    assert "BOOT_FAILED" in absent.stdout, (absent.stdout, absent.stderr)

    healthy = _run_boot(
        """
        import os
        import chinamaxM.seam
        import litellm
        assert litellm.telemetry is False
        assert litellm.suppress_debug_info is True
        assert os.environ.get('LITELLM_LOCAL_MODEL_COST_MAP') == 'True'
        assert litellm.enable_anthropic_prompt_caching is False
        print('HEALTHY_OK')
        """,
        tmp_path,
    )
    assert "HEALTHY_OK" in healthy.stdout, (healthy.stdout, healthy.stderr)


async def test_no_chat_completions_egress(make_seam_client):
    """AC-8: no chat-completions path — fixture refusal, transport tripwire, static scan."""
    client, fake = await make_seam_client("glm")
    fake.respond(headers=JSON_HEADERS, body=message_bytes([{"type": "text", "text": "ok"}]))
    # The _ban_litellm_transport tripwire (conftest) fails if product code reaches one.
    response = await client.send(
        {"model": "glm-5.2", "input": [user_text("hi")], "tools": [function_tool("f")]}
    )
    assert response.status == 200
    assert fake.chat_completions_hits == []
    assert fake.requests[0].raw_path.endswith("/v1/messages")  # egress went to Anthropic Messages

    from pathlib import Path

    from chinamaxM import doctor, seam

    tree = Path(seam.__file__).parent
    assert doctor.find_chat_completions_references(tree) == []


async def test_unknown_profile_404_and_responses_dialect_501(make_proxy, relay_registry):
    """Routing: unknown profile ⇒ 404 listing valid names; responses dialect ⇒ 501."""
    client = await make_proxy(relay_registry)
    response = await client.post("/openai/nonexistent/responses", json={"model": "m", "input": []})
    assert response.status == 404
    body = await response.json()
    for name in relay_registry.profiles:
        assert name in body["error"]["message"]

    # A non-responses path under /openai never falls to the Default branch.
    other = await client.post("/openai/glm/models", json={})
    assert other.status == 404

    # An Overlay-added profile name appears in the valid list (Registry-derived, not seed).
    extended = Registry(
        port=relay_registry.port,
        profiles={
            **relay_registry.profiles,
            "zed": Profile(name="zed", dialect="anthropic", base_url="http://x", api_key_env="ZED_KEY", default_model="z"),
        },
    )
    client2 = await make_proxy(extended)
    body2 = await (await client2.post("/openai/nope/responses", json={"model": "m", "input": []})).json()
    assert "zed" in body2["error"]["message"]

    # A responses-dialect Profile is rejected with 501 (schema reserved only).
    responses_registry = Registry(
        port=8402,
        profiles={
            "future": Profile(
                name="future", dialect="responses", base_url="http://x", api_key_env="FUTURE_KEY", default_model="f"
            )
        },
    )
    client3 = await make_proxy(responses_registry)
    dialect_response = await client3.post("/openai/future/responses", json={"model": "f", "input": []})
    assert dialect_response.status == 501
    assert (await dialect_response.json())["error"]["type"] == "not_implemented"


async def test_missing_codex_key_401(make_proxy, relay_registry, codex_home):
    """The Codex Key file 401 is OpenAI-shaped and names the variable and file."""
    client = await make_proxy(relay_registry, codex_home=str(codex_home.root))  # no keys written
    response = await client.post("/openai/glm/responses", json={"model": "glm-5.2", "input": []})
    assert response.status == 401
    error = (await response.json())["error"]
    assert error["type"] == "authentication_error"
    assert "GLM_API_KEY" in error["message"]
    assert "model-keys.env" in error["message"]


async def test_nonstreaming_response_translation(make_seam_client):
    """No numbered AC: a complete Anthropic response ⇒ one complete Responses object."""
    client, fake = await make_seam_client("deepseek")
    content = [
        {"type": "thinking", "thinking": "hmm", "signature": "S=="},
        {"type": "text", "text": "the answer"},
        {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"q": "SF"}},
    ]
    usage = {"input_tokens": 300, "output_tokens": 40, "cache_read_input_tokens": 256, "cache_creation_input_tokens": 12}
    fake.respond(headers=JSON_HEADERS, body=message_bytes(content, stop_reason="tool_use", usage=usage))

    response = await client.send({"model": "deepseek-v4-pro[1m]", "input": [user_text("q")]})
    assert response.status == 200
    obj = await response.json()
    assert obj["object"] == "response"
    assert obj["status"] == "completed"
    kinds = [item["type"] for item in obj["output"]]
    assert kinds == ["reasoning", "message", "function_call"]
    function_call = obj["output"][2]
    assert function_call["call_id"] == "toolu_1"
    assert json.loads(function_call["arguments"]) == {"q": "SF"}
    assert obj["usage"]["input_tokens"] == 300
    assert obj["usage"]["input_tokens_details"]["cached_tokens"] == 256
    assert obj["usage"]["total_tokens"] == 340

    # A max_tokens stop ⇒ status incomplete with the mapped reason.
    fake.respond(
        headers=JSON_HEADERS,
        body=message_bytes([{"type": "text", "text": "truncated"}], stop_reason="max_tokens"),
    )
    truncated = await client.send({"model": "deepseek-v4-pro[1m]", "input": [user_text("again")]})
    truncated_obj = await truncated.json()
    assert truncated_obj["status"] == "incomplete"
    assert truncated_obj["incomplete_details"]["reason"] == "max_output_tokens"


# ------------------------------------- agent_message items (ADR 0002 amended 2026-08-19)


async def test_agent_message_content_shape_becomes_user_message(make_seam_client):
    """A direct-collab task delivery (agent_message with a content list) ⇒ a user message."""
    client, fake = await make_seam_client("deepseek")
    fake.respond(headers=JSON_HEADERS, body=message_bytes([{"type": "text", "text": "ok"}]))

    delivered = {
        "type": "agent_message",
        "author": "gpt-5.6-sol",
        "recipient": "chinamaxm_deepseek_task",
        "content": [{"type": "input_text", "text": "summarize the repo"}],
    }
    response = await client.send({"model": "deepseek-v4-pro[1m]", "input": [delivered]})
    assert response.status == 200

    messages = json.loads(fake.requests[0].body)["messages"]
    assert messages[0]["role"] == "user"
    assert "summarize the repo" in json.dumps(messages[0]["content"])


async def test_agent_message_string_shape_becomes_assistant_message(make_seam_client):
    """A rollout self-output (agent_message with a string message field) ⇒ an assistant message."""
    client, fake = await make_seam_client("deepseek")
    fake.respond(headers=JSON_HEADERS, body=message_bytes([{"type": "text", "text": "ok"}]))

    items = [
        user_text("do the task"),
        {"type": "agent_message", "message": "interim commentary", "phase": "commentary"},
        user_text("continue"),
    ]
    response = await client.send({"model": "deepseek-v4-pro[1m]", "input": items})
    assert response.status == 200

    messages = json.loads(fake.requests[0].body)["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert "interim commentary" in json.dumps(messages[1]["content"])


async def test_agent_message_neither_shape_and_unknown_types_400(make_seam_client):
    """An agent_message with neither shape, and any unknown item type, still 400 upstream-free."""
    client, fake = await make_seam_client("deepseek")

    response = await client.send(
        {"model": "deepseek-v4-pro[1m]", "input": [{"type": "agent_message", "phase": "x"}]}
    )
    assert response.status == 400
    assert (await response.json())["error"]["type"] == "invalid_request_error"

    response = await client.send(
        {"model": "deepseek-v4-pro[1m]", "input": [{"type": "bogus_item"}]}
    )
    assert response.status == 400
    assert fake.requests == []  # nothing reached the provider


# --------------------------------------- Dispatch marker on the Seam (ADR 0004 amended)


async def test_marker_found_past_injected_first_user_item(make_seam_client):
    """The real Codex shape: an injected first user item precedes the task, so the marker
    lands in the SECOND user message — scanning all user messages still finds it."""
    client, fake = await make_seam_client("deepseek")
    fake.respond(headers=JSON_HEADERS, body=message_bytes([{"type": "text", "text": "ok"}]))

    items = [
        user_text("<recommended_plugins>injected host preamble</recommended_plugins>"),
        user_text("[chinamaxm model=deepseek-v4-flash]\n\nsummarize the repo"),
    ]
    response = await client.send({"model": "deepseek-v4-pro[1m]", "input": items})
    assert response.status == 200

    sent = json.loads(fake.requests[0].body)
    assert sent["model"] == "deepseek-v4-flash"
    # The marker text itself still rides in the translated body.
    assert "[chinamaxm model=deepseek-v4-flash]" in json.dumps(sent["messages"])


async def test_bare_model_with_slash_never_mangled(make_seam_client):
    """A bare Responses model containing '/' crosses the Seam verbatim (the prefix strip is
    profile-scoped — this replaces the retired defensive model restore)."""
    client, fake = await make_seam_client("glm")
    fake.respond(headers=JSON_HEADERS, body=message_bytes([{"type": "text", "text": "ok"}]))

    response = await client.send({"model": "org/model-x", "input": [user_text("hi")]})
    assert response.status == 200
    assert json.loads(fake.requests[0].body)["model"] == "org/model-x"


async def test_prefix_stability_across_turns_with_marker(make_seam_client):
    """Two Seam turns with a marker present: the model substitution is deterministic and the
    translated history stays a strict byte prefix (provider cache holds)."""
    client, fake = await make_seam_client("glm")
    fake.respond(headers=JSON_HEADERS, body=message_bytes([{"type": "text", "text": "ok"}]))

    task = user_text("[chinamaxm model=glm-5.2-flash]\n\ndo the task")
    turn_n = [task]
    turn_n1 = [task, assistant_text("done step 1"), user_text("continue")]

    await client.send({"model": "glm-5.2", "input": turn_n})
    await client.send({"model": "glm-5.2", "input": turn_n1})

    body_n = json.loads(fake.requests[0].body)
    body_n1 = json.loads(fake.requests[1].body)
    assert body_n["model"] == "glm-5.2-flash"
    assert body_n1["model"] == "glm-5.2-flash"  # same first match every turn

    messages_n = body_n["messages"]
    messages_n1 = body_n1["messages"]
    assert messages_n == messages_n1[: len(messages_n)]  # strict list prefix
    serialized_n = json.dumps(messages_n, separators=(",", ":"))
    serialized_n1 = json.dumps(messages_n1, separators=(",", ":"))
    assert serialized_n1.startswith(serialized_n[:-1])
