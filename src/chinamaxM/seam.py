"""The LiteLLM Seam: pure Responses<->Anthropic translation with the deterministic shim.

Codex speaks the OpenAI Responses dialect; the six shipped Profiles speak Anthropic
Messages (ADR 0002). This module translates a Responses request into an Anthropic Messages
egress body and translates the provider's Anthropic response (streaming and non-streaming)
back into a Responses event sequence. LiteLLM 1.96.2 is the request-side translator (used as
a pure transformation library, never as the HTTP client); the response side is a
deterministic state machine mapping Anthropic content blocks directly to Responses output
items. Every function and class here is PURE — no I/O — so the Proxy owns the wire and this
module stays reusable and testable (proxy-04 consumes the strip records via the log sink).

The mandatory shim wraps the transforms (ADR 0002): PRE strips non-``function`` tool types
and the Responses ``reasoning`` param, drops ``store``/``previous_response_id``, and
synthesizes ``max_tokens``; POST strips the injected ``type:"custom"`` tool discriminator
and merges the Profile thinking policy / Scrub / extras through the proxy-02 relay pipeline;
message boundaries follow SAME-TYPE RUN GROUPING (ADR 0002 as amended) so translated
history stays strictly append-only. Thinking signatures round-trip through the Responses
reasoning item's opaque ``encrypted_content`` as a versioned envelope.

See the dated implementation note at the end of
``docs/plan/chinamaxm-proxy-03-responses-seam.md`` for the confirmed litellm 1.96.2 symbols
and the process-global tool-call state analysis.
"""

from __future__ import annotations

import base64
import binascii
import inspect
import json
import os
import threading
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, Iterator

# --------------------------------------------------------------------- litellm gate
#
# ``LITELLM_LOCAL_MODEL_COST_MAP`` MUST be set before litellm is imported (ADR 0002); the
# pinned version is verified so a broken env fails loudly at boot rather than mid-request.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

#: The exactly-pinned litellm version (ADR 0002; PyPI releases are non-monotonic).
REQUIRED_LITELLM_VERSION = "1.96.2"

try:
    _installed_litellm = metadata.version("litellm")
except metadata.PackageNotFoundError as exc:  # pragma: no cover - exercised in a subprocess
    raise RuntimeError(
        f"chinamaxM requires litellm=={REQUIRED_LITELLM_VERSION} but it is not installed"
    ) from exc

if _installed_litellm != REQUIRED_LITELLM_VERSION:  # pragma: no cover - subprocess test
    raise RuntimeError(
        f"chinamaxM requires litellm=={REQUIRED_LITELLM_VERSION} but found "
        f"{_installed_litellm}"
    )

try:
    import litellm
except ImportError as exc:  # pragma: no cover - exercised in a subprocess
    raise RuntimeError(
        f"chinamaxM could not import the pinned litellm=={REQUIRED_LITELLM_VERSION}"
    ) from exc

# Knobs are hard-set at import (ADR 0002): telemetry off, quiet, and prompt caching must
# stay False forever (it injects a moving cache_control breakpoint — the cache poison).
litellm.telemetry = False
litellm.suppress_debug_info = True
if litellm.enable_anthropic_prompt_caching is not False:  # pragma: no cover - guard
    raise RuntimeError(
        "chinamaxM requires litellm.enable_anthropic_prompt_caching to remain False"
    )

from litellm.llms.anthropic.chat.transformation import (  # noqa: E402  (after the gate)
    AnthropicConfig as _AnthropicConfig,
)
from litellm.responses.litellm_completion_transformation.transformation import (  # noqa: E402
    LiteLLMCompletionResponsesConfig as _ResponsesConfig,
)

from chinamaxM.registry import Profile  # noqa: E402
from chinamaxM.relay import mutate_body  # noqa: E402  (proxy-02 relay pipeline reuse)


def _assert_signature(func: Any, required: set[str]) -> None:
    """Assert ``func`` accepts every parameter in ``required`` (fail loud on upstream drift)."""
    params = set(inspect.signature(func).parameters)
    missing = required - params
    if missing:  # pragma: no cover - guards against a silent litellm upgrade
        raise RuntimeError(
            f"litellm {_installed_litellm} changed {func.__qualname__}; missing "
            f"parameters: {', '.join(sorted(missing))}"
        )


_assert_signature(
    _ResponsesConfig.transform_responses_api_request_to_chat_completion_request,
    {"model", "input", "responses_api_request"},
)
_assert_signature(
    _AnthropicConfig.map_openai_params,
    {"non_default_params", "optional_params", "model", "drop_params"},
)
_assert_signature(
    _AnthropicConfig.transform_request,
    {"model", "messages", "optional_params", "litellm_params", "headers"},
)

#: One lock serializes the (synchronous) litellm transform calls. aiohttp runs one event
#: loop so calls are already atomic per request; this is defense-in-depth against the
#: module-global ``TOOL_CALLS_CACHE`` if the transforms are ever driven off-loop.
_TRANSFORM_LOCK = threading.Lock()

#: Anthropic Messages REQUIRES ``max_tokens``; this deterministic default is synthesized
#: when the Responses request omits ``max_output_tokens`` (prefix stability holds).
DEFAULT_MAX_TOKENS = 8192

#: Fixed upstream read timeout for a Seam stream (providers emit SSE pings, so healthy
#: gaps stay short); the Proxy applies it on the egress request.
UPSTREAM_READ_TIMEOUT = 600.0

#: Version of the reasoning-item signature envelope (decode always keeps reading v1).
ENVELOPE_VERSION = 1

#: Message roles routed to the Anthropic top-level ``system`` (never into ``messages``).
_SYSTEM_ROLES = frozenset({"system", "developer"})

#: Chat-request keys that are NOT OpenAI params (dropped before ``map_openai_params``).
_PARAM_JUNK = frozenset(
    {"messages", "model", "custom_llm_provider", "stream", "web_search_options"}
)


class SeamError(Exception):
    """A deterministic, OpenAI-shaped failure raised before anything reaches the provider.

    Attributes:
        status: The HTTP status the Responses ingress returns.
        body: The OpenAI-shaped error body ``{"error": {"message", "type"}}``.
    """

    def __init__(self, status: int, message: str, error_type: str) -> None:
        super().__init__(message)
        self.status = status
        self.body = {"error": {"message": message, "type": error_type}}


@dataclass
class SeamMeta:
    """Per-request diagnostics handed to the injectable log sink (proxy-04 consumes it).

    Attributes:
        stripped_tools: The ``type`` of every non-``function`` tool dropped by the PRE shim,
            in request order (ADR 0002 / ADR 0010).
        synthesized_max_tokens: Whether ``max_tokens`` was synthesized (truncation stays
            diagnosable).
    """

    stripped_tools: list[str] = field(default_factory=list)
    synthesized_max_tokens: bool = False


# ---------------------------------------------------------------- signature envelope


def encode_thinking_block(block: dict) -> str:
    """Encode an Anthropic thinking block into the reasoning item's opaque envelope.

    The versioned union is serialized deterministically (``sort_keys``, compact separators)
    then RFC 4648 base64 WITH padding, so the same block is byte-stable turn over turn.

    Args:
        block: An Anthropic ``thinking`` or ``redacted_thinking`` content block.

    Returns:
        The base64-encoded envelope string for ``encrypted_content``.

    Raises:
        ValueError: If ``block`` is neither a thinking nor a redacted_thinking block.
    """
    btype = block.get("type")
    if btype == "thinking":
        envelope: dict[str, Any] = {
            "v": ENVELOPE_VERSION,
            "type": "thinking",
            "thinking": block.get("thinking", ""),
        }
        signature = block.get("signature")
        if signature is not None:
            envelope["signature"] = signature
    elif btype == "redacted_thinking":
        envelope = {"v": ENVELOPE_VERSION, "type": "redacted_thinking", "data": block.get("data", "")}
    else:
        raise ValueError(f"cannot encode thinking block of type {btype!r}")
    raw = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def decode_thinking_envelope(encrypted: str) -> dict:
    """Rebuild an Anthropic thinking block from a reasoning item's envelope.

    Args:
        encrypted: The ``encrypted_content`` string from a Responses reasoning item.

    Returns:
        The reconstructed Anthropic ``thinking`` or ``redacted_thinking`` content block.

    Raises:
        SeamError: 400 when the value is foreign, corrupt, or an unknown envelope version.
    """
    try:
        raw = base64.b64decode(encrypted, validate=True)
        envelope = json.loads(raw)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise SeamError(
            400, "reasoning item encrypted_content is not a chinamaxM envelope", "invalid_request_error"
        ) from exc
    if not isinstance(envelope, dict) or envelope.get("v") != ENVELOPE_VERSION:
        raise SeamError(
            400, "reasoning item encrypted_content has an unknown envelope version", "invalid_request_error"
        )
    etype = envelope.get("type")
    if etype == "thinking":
        block: dict[str, Any] = {"type": "thinking", "thinking": envelope.get("thinking", "")}
        if "signature" in envelope:
            block["signature"] = envelope["signature"]
        return block
    if etype == "redacted_thinking":
        return {"type": "redacted_thinking", "data": envelope.get("data", "")}
    raise SeamError(
        400, "reasoning item encrypted_content has an unknown envelope type", "invalid_request_error"
    )


# ------------------------------------------------------------------ request → egress


def responses_to_anthropic(
    profile: Profile, request: object, *, stream: bool
) -> tuple[dict, SeamMeta]:
    """Translate a Responses request into the Anthropic Messages egress body.

    Applies the full deterministic shim (PRE strips + synthesis, run-grouped message
    assembly via isolated litellm transforms, POST ``type:"custom"`` strip, and the
    proxy-02 thinking/Scrub/extras pipeline) and sets ``stream`` to match the ingress.

    Args:
        profile: The routed Profile supplying the thinking policy, Scrub, and extras.
        request: The parsed Responses request body (a JSON object).
        stream: Whether the inbound Responses request streams (the Proxy owns the flag).

    Returns:
        A ``(anthropic_body, meta)`` pair; ``meta`` carries the strip records for the sink.

    Raises:
        SeamError: A deterministic OpenAI-shaped 400/500 for any malformed or unsupported
            request, raised before anything reaches the provider.
    """
    if not isinstance(request, dict):
        raise SeamError(400, "request body must be a JSON object", "invalid_request_error")

    model = request.get("model")
    if not isinstance(model, str) or not model:
        raise SeamError(400, "'model' is required", "invalid_request_error")

    max_tokens, synthesized = _resolve_max_tokens(request.get("max_output_tokens"))

    input_value = request.get("input")
    if request.get("previous_response_id") is not None and not input_value:
        raise SeamError(
            400,
            "store-based continuation via previous_response_id is unsupported; resend the full input",
            "invalid_request_error",
        )
    if input_value is None:
        items: list = []
    elif isinstance(input_value, list):
        items = input_value
    else:
        raise SeamError(400, "'input' must be an array of items", "invalid_request_error")

    function_tools, stripped_tools = _split_tools(request.get("tools"))
    _validate_tool_choice(request.get("tool_choice"), function_tools, stripped_tools)

    system_blocks = _build_system(request.get("instructions"), items)
    messages = _build_messages(items)
    anthropic_params = _map_params_and_tools(model, request, function_tools)

    tools_out = anthropic_params.pop("tools", None)
    if tools_out:
        tools_out = _strip_type_custom(tools_out)

    body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if system_blocks:
        body["system"] = system_blocks
    if tools_out:
        body["tools"] = tools_out
    body.update(anthropic_params)

    # POST shim: thinking-merge + Scrub + extras via the proxy-02 relay pipeline (reused
    # verbatim). ``mutate_body``'s prefix-strip is a no-op here (the Responses model rides
    # through with no Profile prefix); restore it defensively so the model is never mangled.
    body = mutate_body(profile, body)
    body["model"] = model
    body["stream"] = bool(stream)

    return body, SeamMeta(stripped_tools=stripped_tools, synthesized_max_tokens=synthesized)


def _resolve_max_tokens(max_output_tokens: object) -> tuple[int, bool]:
    """Map ``max_output_tokens`` to Anthropic ``max_tokens`` (synthesize the default if absent)."""
    if max_output_tokens is None:
        return DEFAULT_MAX_TOKENS, True
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens <= 0:
        raise SeamError(400, "'max_output_tokens' must be a positive integer", "invalid_request_error")
    return max_output_tokens, False


def _split_tools(tools: object) -> tuple[list[dict], list[str]]:
    """Split the request tools into surviving ``function`` tools and dropped tool types."""
    if tools is None:
        return [], []
    if not isinstance(tools, list):
        raise SeamError(400, "'tools' must be an array", "invalid_request_error")
    function_tools: list[dict] = []
    stripped: list[str] = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "function":
            function_tools.append(tool)
        elif isinstance(tool, dict):
            stripped.append(str(tool.get("type")))
        else:
            raise SeamError(400, "each tool must be an object", "invalid_request_error")
    return function_tools, stripped


def _validate_tool_choice(
    tool_choice: object, function_tools: list[dict], stripped_tools: list[str]
) -> None:
    """Reject a ``tool_choice`` that names a tool the PRE shim stripped (dangling reference)."""
    if not isinstance(tool_choice, dict):
        return
    choice_type = tool_choice.get("type")
    if choice_type == "function":
        names = {tool.get("name") for tool in function_tools}
        if tool_choice.get("name") not in names:
            raise SeamError(
                400,
                f"tool_choice names function {tool_choice.get('name')!r} which is not an available tool",
                "invalid_request_error",
            )
    elif choice_type in set(stripped_tools):
        raise SeamError(
            400,
            f"tool_choice names stripped tool type {choice_type!r}",
            "invalid_request_error",
        )


def _build_system(instructions: object, items: list) -> list[dict]:
    """Concatenate ``instructions`` and system/developer input items into Anthropic system blocks."""
    blocks: list[dict] = []
    if isinstance(instructions, str) and instructions:
        blocks.append({"type": "text", "text": instructions})
    for item in items:
        if (
            isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("role") in _SYSTEM_ROLES
        ):
            text = _extract_text(item.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
    return blocks


def _extract_text(content: object) -> str:
    """Concatenate the text of a message item's content parts (string or list form)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _build_messages(items: list) -> list[dict]:
    """Assemble Anthropic messages by SAME-TYPE RUN GROUPING (strict append-only).

    Each ``message`` item, each maximal ``function_call`` run, and each maximal
    ``function_call_output`` run is translated by an isolated litellm transform, so a
    message is a pure function of its own items and never coalesces across a type boundary.
    Reasoning items decode to thinking blocks buffered onto the head of the next
    assistant-side message. call_ids are validated for strict pairing before anything
    reaches the provider.

    An item with no ``type`` is read as a ``message``, matching the Responses API, where
    ``type`` is optional on an input message and every other item type carries it.

    A call run's outputs are hoisted ahead of anything a Host interleaved between them
    (``_hoist_run_outputs``) before grouping, so the grouped results are always the one
    message immediately after their ``tool_use`` turn — the adjacency Anthropic enforces.

    Raises:
        SeamError: 400 on an unknown item type, an unsupported role, or a broken call_id
            pairing; 500 if an isolated transform does not yield exactly one message.
    """
    items = list(items)  # hoisting reorders in place; never mutate the caller's list
    messages: list[dict] = []
    pending_thinking: list[dict] = []
    seen_call_ids: set[str] = set()
    used_output_ids: set[str] = set()

    index = 0
    count = len(items)
    while index < count:
        item = items[index]
        if not isinstance(item, dict):
            raise SeamError(400, "each input item must be an object", "invalid_request_error")
        itype = item.get("type") or "message"  # optional on an input message item

        if itype == "message" and item.get("role") in _SYSTEM_ROLES:
            index += 1  # already folded into the top-level system
            continue

        if itype == "reasoning":
            block = _reasoning_item_to_block(item)
            if block is not None:
                pending_thinking.append(block)
            index += 1
        elif itype == "message":
            role = item.get("role")
            if role not in ("user", "assistant"):
                raise SeamError(400, f"unsupported message role {role!r}", "invalid_request_error")
            message = _run_to_message([item])
            if role == "assistant" and pending_thinking:
                message["content"] = pending_thinking + list(message.get("content", []))
                pending_thinking = []
            messages.append(message)
            index += 1
        elif itype == "function_call":
            end = _scan_function_call_run(items, index, count, seen_call_ids)
            message = _run_to_message(items[index:end])
            if pending_thinking:
                message["content"] = pending_thinking + list(message.get("content", []))
                pending_thinking = []
            messages.append(message)
            _hoist_run_outputs(items, end, {items[i]["call_id"] for i in range(index, end)})
            index = end
        elif itype == "function_call_output":
            end = _scan_function_call_output_run(
                items, index, count, seen_call_ids, used_output_ids
            )
            messages.append(_run_to_message(items[index:end]))
            index = end
        else:
            raise SeamError(400, f"unsupported input item type {itype!r}", "invalid_request_error")

    if pending_thinking:
        messages.append({"role": "assistant", "content": pending_thinking})
    return messages


def _scan_function_call_run(
    items: list, start: int, count: int, seen_call_ids: set[str]
) -> int:
    """Return the end index of a ``function_call`` run, validating unique string call_ids."""
    index = start
    while index < count and isinstance(items[index], dict) and items[index].get("type") == "function_call":
        call_id = items[index].get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise SeamError(400, "function_call needs a non-empty string call_id", "invalid_request_error")
        if call_id in seen_call_ids:
            raise SeamError(400, f"duplicate function_call call_id {call_id!r}", "invalid_request_error")
        seen_call_ids.add(call_id)
        index += 1
    return index


def _scan_function_call_output_run(
    items: list, start: int, count: int, seen_call_ids: set[str], used_output_ids: set[str]
) -> int:
    """Return the end index of a ``function_call_output`` run, validating call_id pairing."""
    index = start
    while (
        index < count
        and isinstance(items[index], dict)
        and items[index].get("type") == "function_call_output"
    ):
        call_id = items[index].get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise SeamError(
                400, "function_call_output needs a non-empty string call_id", "invalid_request_error"
            )
        if call_id not in seen_call_ids:
            raise SeamError(
                400,
                f"function_call_output call_id {call_id!r} matches no prior function_call",
                "invalid_request_error",
            )
        if call_id in used_output_ids:
            raise SeamError(
                400, f"duplicate function_call_output call_id {call_id!r}", "invalid_request_error"
            )
        used_output_ids.add(call_id)
        index += 1
    return index


def _hoist_run_outputs(items: list, start: int, run_ids: set[str]) -> None:
    """Reorder a call run's outputs ahead of items interleaved between them (in place).

    Hosts inject messages between a parallel call run's outputs (a Codex PreToolUse
    hook's additionalContext lands as a developer message there), which would split the
    output run — but Anthropic requires every ``tool_use`` of an assistant turn to be
    answered in the immediately-following user message. Hoisting keeps the output run
    contiguous; the interleaved items keep their relative order after it.
    """
    outputs: list = []
    rest: list = []
    index = start
    count = len(items)
    while index < count and len(outputs) < len(run_ids):
        item = items[index]
        if (
            isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and item.get("call_id") in run_ids
        ):
            outputs.append(item)
        else:
            rest.append(item)
        index += 1
    if outputs and rest:
        items[start:index] = outputs + rest


def _reasoning_item_to_block(item: dict) -> dict | None:
    """Decode a reasoning item's envelope to a thinking block (``None`` when it carries none)."""
    encrypted = item.get("encrypted_content")
    if encrypted is None:
        return None
    if not isinstance(encrypted, str):
        raise SeamError(400, "reasoning item encrypted_content must be a string", "invalid_request_error")
    return decode_thinking_envelope(encrypted)


def _run_to_message(run_items: list) -> dict:
    """Translate one same-type run into its single Anthropic message via litellm (isolated)."""
    with _TRANSFORM_LOCK:
        try:
            chat_request = _ResponsesConfig.transform_responses_api_request_to_chat_completion_request(
                model="chinamaxm",
                input=run_items,
                responses_api_request={},
                custom_llm_provider="anthropic",
                stream=False,
            )
            body = _AnthropicConfig().transform_request(
                model="chinamaxm",
                messages=chat_request["messages"],
                optional_params={"max_tokens": DEFAULT_MAX_TOKENS},
                litellm_params={},
                headers={},
            )
        except SeamError:
            raise
        except Exception as exc:  # litellm raised internally -> OpenAI-shaped 500
            raise SeamError(500, f"seam transform failed: {exc}", "api_error") from exc
    produced = body.get("messages") or []
    if len(produced) != 1:
        raise SeamError(
            500, "seam transform did not yield exactly one message for a run", "api_error"
        )
    return produced[0]


def _map_params_and_tools(model: str, request: dict, function_tools: list[dict]) -> dict:
    """Map the Responses sampling params + function tools to Anthropic optional params (litellm)."""
    responses_request: dict[str, Any] = {}
    for key in ("temperature", "top_p", "top_logprobs", "parallel_tool_calls"):
        if key in request:
            responses_request[key] = request[key]
    if function_tools:
        responses_request["tools"] = function_tools
    if request.get("tool_choice") is not None:
        responses_request["tool_choice"] = request["tool_choice"]

    with _TRANSFORM_LOCK:
        try:
            chat_request = _ResponsesConfig.transform_responses_api_request_to_chat_completion_request(
                model=model,
                input=[],
                responses_api_request=responses_request,
                custom_llm_provider="anthropic",
                stream=False,
            )
            non_default = {k: v for k, v in chat_request.items() if k not in _PARAM_JUNK}
            optional_params = _AnthropicConfig().map_openai_params(
                non_default_params=non_default,
                optional_params={},
                model=model,
                drop_params=True,
            )
        except Exception as exc:  # pragma: no cover - defensive
            raise SeamError(500, f"seam parameter transform failed: {exc}", "api_error") from exc
    optional_params.pop("max_tokens", None)  # the Seam owns max_tokens deterministically
    return optional_params


def _strip_type_custom(tools: list) -> list:
    """Remove the top-level ``type:"custom"`` litellm injects (never touch nested schema)."""
    stripped: list = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "custom":
            tool = {key: value for key, value in tool.items() if key != "type"}
        stripped.append(tool)
    return stripped


# ---------------------------------------------------------------- response → client


def _map_stop_reason(stop_reason: object) -> tuple[str, bool]:
    """Map an Anthropic ``stop_reason`` to a Responses status and the incomplete flag."""
    if stop_reason == "max_tokens":
        return "incomplete", True
    return "completed", False


def _map_usage(anthropic_usage: dict) -> dict:
    """Map Anthropic usage to Responses usage (``cache_read`` -> ``cached_tokens``)."""
    input_tokens = anthropic_usage.get("input_tokens") or 0
    output_tokens = anthropic_usage.get("output_tokens") or 0
    cached = anthropic_usage.get("cache_read_input_tokens") or 0
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": input_tokens + output_tokens,
    }


def _content_block_to_output_item(block: dict, index: int, base_id: str) -> dict | None:
    """Map one Anthropic content block to a Responses output item (``None`` if unknown)."""
    btype = block.get("type")
    if btype == "text":
        return {
            "type": "message",
            "id": f"msg_{base_id}_{index}",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": block.get("text", ""), "annotations": []}],
        }
    if btype in ("thinking", "redacted_thinking"):
        return {
            "type": "reasoning",
            "id": f"rs_{base_id}_{index}",
            "summary": [],
            "encrypted_content": encode_thinking_block(block),
        }
    if btype == "tool_use":
        return {
            "type": "function_call",
            "id": f"fc_{base_id}_{index}",
            "call_id": block.get("id", ""),
            "name": block.get("name", ""),
            "arguments": json.dumps(block.get("input", {}), separators=(",", ":"), ensure_ascii=False),
            "status": "completed",
        }
    return None


def anthropic_response_to_responses(response: dict, *, request_model: str) -> tuple[dict, dict]:
    """Translate a complete Anthropic Messages response into a complete Responses object.

    Args:
        response: The provider's parsed Anthropic Messages JSON response.
        request_model: The egress model, used when the response omits one.

    Returns:
        A ``(responses_object, anthropic_usage)`` pair; the raw usage rides to the log sink
        (its ``cache_creation_input_tokens`` has no client-visible Responses field).
    """
    base_id = response.get("id") or "chinamaxm"
    output: list[dict] = []
    for index, block in enumerate(response.get("content") or []):
        item = _content_block_to_output_item(block, index, base_id)
        if item is not None:
            output.append(item)

    status, incomplete = _map_stop_reason(response.get("stop_reason"))
    responses_object: dict[str, Any] = {
        "id": f"resp_{base_id}",
        "object": "response",
        "created_at": 0,
        "status": status,
        "model": response.get("model") or request_model,
        "output": output,
        "usage": _map_usage(response.get("usage") or {}),
    }
    if incomplete:
        responses_object["incomplete_details"] = {"reason": "max_output_tokens"}
    return responses_object, response.get("usage") or {}


def anthropic_error_to_openai(raw: bytes) -> dict:
    """Translate an Anthropic error body into an OpenAI-shaped error object."""
    try:
        data = json.loads(raw)
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            message = error.get("message") or "upstream error"
            error_type = error.get("type") or "api_error"
        else:
            message = raw.decode("utf-8", "replace")[:500] or "upstream error"
            error_type = "api_error"
    except ValueError:
        message = raw.decode("utf-8", "replace")[:500] or "upstream error"
        error_type = "api_error"
    return {"error": {"message": message, "type": error_type}}


def openai_error_body(message: str, error_type: str) -> dict:
    """Return an OpenAI-shaped error body ``{"error": {"message", "type"}}``."""
    return {"error": {"message": message, "type": error_type}}


# --------------------------------------------------------------- streaming machinery


class AnthropicSSEParser:
    """Reassemble an Anthropic SSE byte stream into complete events (transport-agnostic).

    Tolerates an event split across transport chunks, multiple events per chunk, CRLF or LF
    framing, multi-line ``data:`` fields, and comment/heartbeat lines. Malformed event JSON
    surfaces as a ``{"type": "__malformed__"}`` sentinel so the translator fails the stream.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[dict]:
        """Consume one transport chunk and return every event it completed."""
        self._buffer += chunk.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
        events: list[dict] = []
        while "\n\n" in self._buffer:
            block, self._buffer = self._buffer.split("\n\n", 1)
            event = self._parse_block(block)
            if event is not None:
                events.append(event)
        return events

    @staticmethod
    def _parse_block(block: str) -> dict | None:
        """Parse one SSE event block into its JSON payload (``None`` for a data-less block)."""
        event_type: str | None = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                value = line[len("data:"):]
                if value.startswith(" "):
                    value = value[1:]
                data_lines.append(value)
        if not data_lines:
            return None
        try:
            payload = json.loads("\n".join(data_lines))
        except ValueError:
            return {"type": "__malformed__"}
        if isinstance(payload, dict):
            if "type" not in payload and event_type:
                payload["type"] = event_type
            return payload
        return {"type": "__malformed__"}


class _OpenBlock:
    """Mutable accumulator for one in-flight Anthropic content block."""

    def __init__(self, kind: str, output_index: int, item_id: str) -> None:
        self.kind = kind
        self.output_index = output_index
        self.item_id = item_id
        self.text = ""
        self.thinking = ""
        self.signature: str | None = None
        self.redacted_data = ""
        self.arguments = ""
        self.call_id = ""
        self.name = ""


class ResponsesStreamTranslator:
    """A per-request state machine turning Anthropic SSE events into Responses events.

    Emits the full lifecycle (``response.created`` -> ``response.in_progress`` -> per-item
    added/part/delta/done -> terminal) with a monotonic ``sequence_number`` and stable item
    ids. Terminal mapping follows ``stop_reason``; usage maps ``cache_read`` ->
    ``cached_tokens``. Non-streaming responses are built by
    :func:`anthropic_response_to_responses` from the same block mapping.
    """

    def __init__(self) -> None:
        self._seq = 0
        self._response_id = "resp_chinamaxm"
        self._model = ""
        self._next_output_index = 0
        self._open: dict[int, _OpenBlock] = {}
        self._output_items: list[dict] = []
        self._input_tokens = 0
        self._output_tokens = 0
        self._cache_read = 0
        self._cache_creation = 0
        self._stop_reason: object = None
        self.terminated = False

    @property
    def cache_creation_input_tokens(self) -> int:
        """Cache-creation tokens seen upstream (rides the log sink, never client usage)."""
        return self._cache_creation

    @property
    def usage(self) -> dict:
        """The accumulated Anthropic-shaped usage (for the log sink)."""
        return {
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "cache_read_input_tokens": self._cache_read,
            "cache_creation_input_tokens": self._cache_creation,
        }

    def handle(self, event: dict) -> list[dict]:
        """Translate one Anthropic event into zero or more Responses events."""
        etype = event.get("type")
        handler = {
            "message_start": self._on_message_start,
            "content_block_start": self._on_content_block_start,
            "content_block_delta": self._on_content_block_delta,
            "content_block_stop": self._on_content_block_stop,
            "message_delta": self._on_message_delta,
            "message_stop": self._on_message_stop,
        }.get(etype)
        if handler is not None:
            return handler(event)
        if etype in ("ping", None):
            return []
        if etype in ("error", "__malformed__"):
            return self._fail("upstream stream error")
        return []  # unknown upstream event types are ignored

    def finish(self) -> list[dict]:
        """Emit ``response.failed`` if the stream ended before ``message_stop``."""
        if self.terminated:
            return []
        return self._fail("upstream stream ended before message_stop")

    def fail(self, message: str) -> list[dict]:
        """Force a terminal ``response.failed`` (upstream/transport failure mid-stream)."""
        return self._fail(message)

    # -- internal helpers ---------------------------------------------------------

    def _emit(self, event_type: str, **fields: Any) -> dict:
        """Build one Responses event with the next monotonic ``sequence_number``."""
        fields["type"] = event_type
        fields["sequence_number"] = self._seq
        self._seq += 1
        return fields

    def _response_object(
        self, status: str, *, incomplete: bool = False, with_usage: bool = False
    ) -> dict:
        """Snapshot the Responses object for a lifecycle/terminal event."""
        obj: dict[str, Any] = {
            "id": self._response_id,
            "object": "response",
            "created_at": 0,
            "status": status,
            "model": self._model,
            "output": list(self._output_items),
            "usage": _map_usage(self.usage) if with_usage else None,
        }
        if incomplete:
            obj["incomplete_details"] = {"reason": "max_output_tokens"}
        return obj

    def _on_message_start(self, event: dict) -> list[dict]:
        message = event.get("message") or {}
        base_id = message.get("id") or "chinamaxm"
        self._response_id = f"resp_{base_id}"
        self._model = message.get("model") or ""
        usage = message.get("usage") or {}
        self._input_tokens = usage.get("input_tokens") or 0
        self._cache_read = usage.get("cache_read_input_tokens") or 0
        self._cache_creation = usage.get("cache_creation_input_tokens") or 0
        self._output_tokens = usage.get("output_tokens") or 0
        return [
            self._emit("response.created", response=self._response_object("in_progress")),
            self._emit("response.in_progress", response=self._response_object("in_progress")),
        ]

    def _on_content_block_start(self, event: dict) -> list[dict]:
        anthropic_index = event.get("index", 0)
        block = event.get("content_block") or {}
        btype = block.get("type")
        output_index = self._next_output_index
        self._next_output_index += 1
        base = self._response_id[len("resp_"):] or "chinamaxm"

        if btype == "text":
            open_block = _OpenBlock("text", output_index, f"msg_{base}_{output_index}")
            self._open[anthropic_index] = open_block
            item = {
                "type": "message",
                "id": open_block.item_id,
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            }
            return [
                self._emit("response.output_item.added", output_index=output_index, item=item),
                self._emit(
                    "response.content_part.added",
                    item_id=open_block.item_id,
                    output_index=output_index,
                    content_index=0,
                    part={"type": "output_text", "text": "", "annotations": []},
                ),
            ]
        if btype in ("thinking", "redacted_thinking"):
            open_block = _OpenBlock(btype, output_index, f"rs_{base}_{output_index}")
            open_block.redacted_data = block.get("data", "") if btype == "redacted_thinking" else ""
            self._open[anthropic_index] = open_block
            item = {"type": "reasoning", "id": open_block.item_id, "summary": [], "encrypted_content": ""}
            return [self._emit("response.output_item.added", output_index=output_index, item=item)]
        if btype == "tool_use":
            open_block = _OpenBlock("tool_use", output_index, f"fc_{base}_{output_index}")
            open_block.call_id = block.get("id", "")
            open_block.name = block.get("name", "")
            self._open[anthropic_index] = open_block
            item = {
                "type": "function_call",
                "id": open_block.item_id,
                "call_id": open_block.call_id,
                "name": open_block.name,
                "arguments": "",
                "status": "in_progress",
            }
            return [self._emit("response.output_item.added", output_index=output_index, item=item)]
        # Unknown block type: reserve nothing, ignore.
        self._next_output_index -= 1
        return []

    def _on_content_block_delta(self, event: dict) -> list[dict]:
        open_block = self._open.get(event.get("index", 0))
        if open_block is None:
            return []
        delta = event.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta" and open_block.kind == "text":
            piece = delta.get("text", "")
            open_block.text += piece
            return [
                self._emit(
                    "response.output_text.delta",
                    item_id=open_block.item_id,
                    output_index=open_block.output_index,
                    content_index=0,
                    delta=piece,
                    logprobs=[],
                )
            ]
        if dtype == "thinking_delta" and open_block.kind == "thinking":
            piece = delta.get("thinking", "")
            open_block.thinking += piece
            return [
                self._emit(
                    "response.reasoning_text.delta",
                    item_id=open_block.item_id,
                    output_index=open_block.output_index,
                    content_index=0,
                    delta=piece,
                )
            ]
        if dtype == "signature_delta" and open_block.kind == "thinking":
            open_block.signature = (open_block.signature or "") + delta.get("signature", "")
            return []
        if dtype == "input_json_delta" and open_block.kind == "tool_use":
            piece = delta.get("partial_json", "")
            open_block.arguments += piece
            return [
                self._emit(
                    "response.function_call_arguments.delta",
                    item_id=open_block.item_id,
                    output_index=open_block.output_index,
                    delta=piece,
                )
            ]
        return []

    def _on_content_block_stop(self, event: dict) -> list[dict]:
        open_block = self._open.pop(event.get("index", 0), None)
        if open_block is None:
            return []
        if open_block.kind == "text":
            item = _content_block_to_output_item(
                {"type": "text", "text": open_block.text}, open_block.output_index, "x"
            )
            item["id"] = open_block.item_id
            part = {"type": "output_text", "text": open_block.text, "annotations": []}
            self._output_items.append(item)
            return [
                self._emit(
                    "response.output_text.done",
                    item_id=open_block.item_id,
                    output_index=open_block.output_index,
                    content_index=0,
                    text=open_block.text,
                    logprobs=[],
                ),
                self._emit(
                    "response.content_part.done",
                    item_id=open_block.item_id,
                    output_index=open_block.output_index,
                    content_index=0,
                    part=part,
                ),
                self._emit("response.output_item.done", output_index=open_block.output_index, item=item),
            ]
        if open_block.kind in ("thinking", "redacted_thinking"):
            if open_block.kind == "thinking":
                block: dict[str, Any] = {"type": "thinking", "thinking": open_block.thinking}
                if open_block.signature is not None:
                    block["signature"] = open_block.signature
            else:
                block = {"type": "redacted_thinking", "data": open_block.redacted_data}
            item = {
                "type": "reasoning",
                "id": open_block.item_id,
                "summary": [],
                "encrypted_content": encode_thinking_block(block),
            }
            self._output_items.append(item)
            return [self._emit("response.output_item.done", output_index=open_block.output_index, item=item)]
        if open_block.kind == "tool_use":
            item = {
                "type": "function_call",
                "id": open_block.item_id,
                "call_id": open_block.call_id,
                "name": open_block.name,
                "arguments": open_block.arguments,
                "status": "completed",
            }
            self._output_items.append(item)
            return [
                self._emit(
                    "response.function_call_arguments.done",
                    item_id=open_block.item_id,
                    output_index=open_block.output_index,
                    name=open_block.name,
                    arguments=open_block.arguments,
                ),
                self._emit("response.output_item.done", output_index=open_block.output_index, item=item),
            ]
        return []

    def _on_message_delta(self, event: dict) -> list[dict]:
        delta = event.get("delta") or {}
        if "stop_reason" in delta:
            self._stop_reason = delta.get("stop_reason")
        usage = event.get("usage") or {}
        if usage.get("output_tokens") is not None:
            self._output_tokens = usage.get("output_tokens") or 0
        if usage.get("input_tokens") is not None:
            self._input_tokens = usage.get("input_tokens") or 0
        return []

    def _on_message_stop(self, event: dict) -> list[dict]:
        if self.terminated:
            return []
        self.terminated = True
        status, incomplete = _map_stop_reason(self._stop_reason)
        obj = self._response_object(status, incomplete=incomplete, with_usage=True)
        terminal = "response.incomplete" if incomplete else "response.completed"
        return [self._emit(terminal, response=obj)]

    def _fail(self, message: str) -> list[dict]:
        """Emit the terminal ``response.failed`` (idempotent once terminated)."""
        if self.terminated:
            return []
        self.terminated = True
        obj = self._response_object("failed", with_usage=True)
        obj["error"] = {"code": "server_error", "message": message}
        return [self._emit("response.failed", response=obj)]


def sse_bytes(event: dict) -> bytes:
    """Serialize one Responses event as an SSE frame (``event:`` + ``data:``)."""
    payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event.get('type', 'message')}\ndata: {payload}\n\n".encode("utf-8")


def iter_responses_events(anthropic_events: Iterator[dict]) -> Iterator[dict]:
    """Drive a fresh translator over a finite sequence of Anthropic events (pure helper)."""
    translator = ResponsesStreamTranslator()
    for event in anthropic_events:
        yield from translator.handle(event)
    yield from translator.finish()
