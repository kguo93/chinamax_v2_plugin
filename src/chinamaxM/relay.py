"""The Relay branch: pure body mutation, order-stable serialization, and the forwarder.

A Profile-prefixed Anthropic-dialect request is forwarded to the Profile's
Anthropic-compatible endpoint (ADR 0001 as amended): the Profile prefix is stripped, the
Claude-flavored ``thinking``/``output_config`` form removed, Scrub fields dropped
(``cache_control`` recursively at any depth), the Profile's thinking policy merged at the
body root, and ``request_extras`` merged last. Every mutation is a deterministic function
of ``(Profile, body)`` so the serialized request prefix stays byte-stable turn over turn
(ADR 0002 guard; ADR 0003 pins the canonical order).

:func:`mutate_body` and :func:`serialize_body` are pure; :func:`forward_relay` takes the
upstream path as a parameter so proxy-04 can reuse it for count_tokens.
"""

from __future__ import annotations

import asyncio
import copy
import json

import aiohttp
import yarl
from aiohttp import web
from multidict import CIMultiDict

from chinamaxM.registry import Profile

#: Egress ``anthropic-version`` applied when the inbound request carries none.
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

#: RFC 7230 §6.1 hop-by-hop headers, dropped in both directions on the relay branch.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

#: Inbound headers the relay egress never forwards verbatim (auth is swapped; framing,
#: content coding, and version are recomputed or pinned), beyond the hop-by-hop set and
#: any ``Connection`` nominees.
_STRIP_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-encoding",
        "authorization",
        "x-api-key",
        "anthropic-beta",
        "anthropic-version",
        "accept-encoding",
    }
)


def mutate_body(profile: Profile, body: dict) -> dict:
    """Return the relay-mutated egress body for ``profile`` (pure; ``body`` untouched).

    The canonical order (ADR 0003 as amended): strip the Profile prefix from ``model`` →
    strip the Claude ``thinking``/``output_config`` form → drop Scrub fields
    (``cache_control`` recursively) → merge the thinking policy (``extra_body`` sub-keys and
    the remainder both merged at the body root) → merge ``request_extras`` last
    (replace-on-conflict, root merge). Operates on a deep copy and inserts copies of the
    Profile sub-objects, so Registry state is never aliased.

    Args:
        profile: The routed Profile.
        body: The parsed inbound request body.

    Returns:
        A new mutated body dict.
    """
    result = copy.deepcopy(body)

    # (0) Strip the Profile prefix; the provider sees the bare model string verbatim.
    model = result.get("model")
    if isinstance(model, str) and "/" in model:
        result["model"] = model.split("/", 1)[1]

    # (1) Strip the Claude-flavored thinking / output_config form from the body root.
    result.pop("thinking", None)
    result.pop("output_config", None)

    # (2) Scrub: drop each named field at the top level; remove cache_control recursively.
    for field_name in profile.scrub:
        if field_name == "cache_control":
            _strip_cache_control(result)
        else:
            result.pop(field_name, None)

    # (3) Merge the Profile thinking policy: extra_body sub-keys and the remainder both at
    #     the body root (so glm/minimax/qwen re-add a bare ``thinking`` field at the end).
    policy = copy.deepcopy(profile.thinking)
    extra_body = policy.pop("extra_body", None)
    if isinstance(extra_body, dict):
        result.update(extra_body)
    result.update(policy)

    # (4) Merge request_extras last, replace-on-conflict, at the body root.
    result.update(copy.deepcopy(profile.request_extras))

    return result


def _strip_cache_control(node: object) -> None:
    """Recursively delete every ``cache_control`` key inside ``node`` (dicts and lists).

    The containing block is preserved — only the ``cache_control`` key is removed, at any
    depth (system blocks, message content, blocks nested in ``tool_result`` content, and
    ``tools[]`` entries).
    """
    if isinstance(node, dict):
        node.pop("cache_control", None)
        for value in node.values():
            _strip_cache_control(value)
    elif isinstance(node, list):
        for item in node:
            _strip_cache_control(item)


def serialize_body(body: dict) -> bytes:
    """Serialize a mutated body to compact UTF-8 JSON, preserving inbound key order."""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


async def forward_relay(
    request: web.Request,
    body: bytes,
    profile: Profile,
    key: str,
    session: aiohttp.ClientSession,
    upstream_path: str,
) -> web.StreamResponse:
    """Mutate, auth-swap, and forward one relay request; stream the response back verbatim.

    Args:
        request: The inbound request (source of headers and client disconnect).
        body: The inbound request body bytes (a valid JSON object — routing guaranteed it).
        profile: The routed Profile supplying the endpoint and mutation policy.
        key: The Profile's injected API key (already resolved non-empty).
        session: The app-scoped upstream ``ClientSession`` (``auto_decompress=False``).
        upstream_path: The path appended to the Profile ``base_url`` (e.g. ``/v1/messages``).

    Returns:
        The streamed upstream response, or the pinned 502 when the upstream connection
        fails before any response headers are committed downstream.
    """
    out_bytes = serialize_body(mutate_body(profile, json.loads(body)))
    url = _relay_url(profile.base_url, upstream_path)
    headers = _relay_request_headers(request.headers, key)

    client_response: web.StreamResponse | None = None
    try:
        async with session.request(
            "POST", url, headers=headers, data=out_bytes, allow_redirects=False
        ) as upstream:
            client_response = web.StreamResponse(status=upstream.status)
            for name, value in _relay_response_headers(upstream.headers):
                client_response.headers.add(name, value)
            await client_response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await client_response.write(chunk)
            await client_response.write_eof()
            return client_response
    except (aiohttp.ClientError, asyncio.TimeoutError):
        if client_response is not None and client_response.prepared:
            # Headers already committed downstream: a mid-stream failure can only
            # truncate — the connection simply terminates, no status rewrite.
            return client_response
        return _upstream_failed_response()


def _relay_url(base_url: str, upstream_path: str) -> yarl.URL:
    """Join the Profile ``base_url`` (trailing slash stripped) with ``upstream_path``."""
    return yarl.URL(base_url.rstrip("/") + upstream_path, encoded=True)


def _hop_by_hop(headers: CIMultiDict) -> set[str]:
    """Return the hop-by-hop names: the RFC 7230 set plus every ``Connection`` nominee."""
    hop = set(_HOP_BY_HOP)
    for value in headers.getall("Connection", []):
        for token in value.split(","):
            token = token.strip().lower()
            if token:
                hop.add(token)
    return hop


def _relay_request_headers(inbound: CIMultiDict, key: str) -> CIMultiDict:
    """Build the relay egress headers: auth swapped, beta dropped, version defaulted.

    Strips the hop-by-hop set and ``Connection`` nominees plus the auth/framing/coding
    headers, then pins ``x-api-key``, ``anthropic-version`` (the inbound value if present,
    else the default), and ``Accept-Encoding: identity``. Every other inbound header
    (repeats included) forwards unchanged.
    """
    drop = _hop_by_hop(inbound) | _STRIP_REQUEST_HEADERS
    out: CIMultiDict = CIMultiDict()
    for name, value in inbound.items():
        if name.lower() not in drop:
            out.add(name, value)
    out.add("anthropic-version", inbound.get("anthropic-version", DEFAULT_ANTHROPIC_VERSION))
    out.add("x-api-key", key)
    out.add("accept-encoding", "identity")
    return out


def _relay_response_headers(headers: CIMultiDict) -> list[tuple[str, str]]:
    """Return upstream response header pairs minus hop-by-hop and the framing length.

    ``content-type`` and ``content-encoding`` pass through; ``content-length`` and
    ``transfer-encoding`` (hop-by-hop) are dropped so the downstream framing is recomputed.
    """
    drop = _hop_by_hop(headers) | {"content-length"}
    return [(name, value) for name, value in headers.items() if name.lower() not in drop]


def _upstream_failed_response() -> web.Response:
    """Return the pinned 502 for an upstream connection failure on the relay branch."""
    body = {"error": {"type": "api_error", "message": "upstream connection failed"}}
    return web.json_response(body, status=502)
