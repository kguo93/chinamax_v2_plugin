"""The Relay branch: pure body mutation, order-stable serialization, and the forwarder.

A Profile-prefixed Anthropic-dialect request is forwarded to the Profile's
Anthropic-compatible endpoint (ADR 0001 as amended): the Profile's own prefix is stripped,
the Claude-flavored ``thinking``/``output_config`` form removed, Scrub fields dropped
(``cache_control`` recursively at any depth), the Profile's thinking policy merged at the
body root, ``request_extras`` merged last, and a Dispatch marker override applied last of
all (ADR 0004 as amended 2026-08-19). Every mutation is a deterministic function of
``(Profile, body)`` so the serialized request prefix stays byte-stable turn over turn
(ADR 0002 guard; ADR 0003 pins the canonical order).

:func:`mutate_body`, :func:`resolve_dispatch_marker`, and :func:`serialize_body` are pure;
:func:`forward_relay` takes the already-mutated egress body and the upstream path as
parameters so proxy-04 can reuse it for count_tokens.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re

import aiohttp
import yarl
from aiohttp import web
from multidict import CIMultiDict

from chinamaxM.registry import Profile

#: Egress ``anthropic-version`` applied when the inbound request carries none.
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

#: The Dispatch marker line minted by the dispatch surfaces: ``[chinamaxm model=<model>]``
#: (ADR 0004 as amended 2026-08-19). Line-anchored; the capture is GREEDY to the line-end
#: bracket so a bracket-suffixed model ID (``deepseek-v4-pro[1m]``) survives intact.
_DISPATCH_MARKER = re.compile(r"^\[chinamaxm model=(.+)\]$", re.MULTILINE)

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

    The canonical order (ADR 0003 as amended): strip the Profile's OWN prefix from
    ``model`` → strip the Claude ``thinking``/``output_config`` form → drop Scrub fields
    (``cache_control`` recursively) → merge the thinking policy (``extra_body`` sub-keys and
    the remainder both merged at the body root) → merge ``request_extras`` last
    (replace-on-conflict, root merge) → apply the Dispatch marker override last (ADR 0004
    as amended 2026-08-19). Operates on a deep copy and inserts copies of the Profile
    sub-objects, so Registry state is never aliased.

    Args:
        profile: The routed Profile.
        body: The parsed inbound request body.

    Returns:
        A new mutated body dict.
    """
    result = copy.deepcopy(body)

    # (0) Strip the Profile prefix; the provider sees the bare model string verbatim. Only
    #     the routed Profile's own prefix is stripped, so a bare model that happens to
    #     contain ``/`` (a Seam-borne Responses model) is never mangled.
    model = result.get("model")
    if isinstance(model, str) and model.startswith(f"{profile.name}/"):
        result["model"] = model[len(profile.name) + 1 :]

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

    # (5) Dispatch marker: a per-dispatch model override in the spawn prompt substitutes
    #     the egress model. Applied HERE — the one shared funnel — so the messages relay,
    #     count_tokens, and the Seam always egress (and count) the same model.
    override = resolve_dispatch_marker(result)
    if override is not None:
        result["model"] = override

    return result


def resolve_dispatch_marker(body: dict) -> str | None:
    """Return the Dispatch marker's model string, or ``None`` when the body carries none.

    Scans ``messages`` in order, user-role messages only; each message's string content (or
    the ``text`` fields of its dict blocks, joined) is searched line-anchored for
    ``[chinamaxm model=<model>]``. The FIRST match in message order wins and scanning stops;
    the marker text is never stripped from the body. Defensive on shapes: non-list
    ``messages``, non-dict messages, and text-less blocks are skipped, never raised on.

    Args:
        body: A parsed Anthropic-dialect request body.

    Returns:
        The captured model string verbatim (never validated), or ``None``.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [
                block.get("text")
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
        else:
            continue
        match = _DISPATCH_MARKER.search("\n".join(texts))
        if match:
            return match.group(1)
    return None


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
    egress_body: dict,
    profile: Profile,
    key: str,
    session: aiohttp.ClientSession,
    upstream_path: str,
    *,
    usage_tee: object = None,
) -> web.StreamResponse:
    """Auth-swap and forward one relay request; stream the response back verbatim.

    Args:
        request: The inbound request (source of headers and client disconnect).
        egress_body: The already-mutated egress body (the caller ran :func:`mutate_body`
            exactly once, so the logged model and the sent bytes always agree).
        profile: The routed Profile supplying the endpoint.
        key: The Profile's injected API key (already resolved non-empty).
        session: The app-scoped upstream ``ClientSession`` (``auto_decompress=False``).
        upstream_path: The path appended to the Profile ``base_url`` (e.g. ``/v1/messages``).
        usage_tee: An optional passive tee (proxy-04's ``AnthropicUsageTee``) handed the
            upstream status/content-type and every response chunk for usage accounting. It
            never alters the relayed bytes; ``None`` disables the tee entirely.

    Returns:
        The streamed upstream response, or the pinned 502 when the upstream connection
        fails before any response headers are committed downstream. A mid-stream failure
        (provider abort or client disconnect) returns the already-committed response so the
        caller can log the upstream status with the usage merged so far.
    """
    out_bytes = serialize_body(egress_body)
    url = _relay_url(profile.base_url, upstream_path)
    headers = _relay_request_headers(request.headers, key)

    client_response: web.StreamResponse | None = None
    try:
        async with session.request(
            "POST", url, headers=headers, data=out_bytes, allow_redirects=False
        ) as upstream:
            if usage_tee is not None:
                usage_tee.begin(upstream.status, upstream.headers.get("Content-Type", ""))
            client_response = web.StreamResponse(status=upstream.status)
            for name, value in _relay_response_headers(upstream.headers):
                client_response.headers.add(name, value)
            await client_response.prepare(request)
            async for chunk in upstream.content.iter_any():
                if usage_tee is not None:
                    usage_tee.feed(chunk)  # captured before the write, bytes never altered
                await client_response.write(chunk)
            await client_response.write_eof()
            if usage_tee is not None:
                usage_tee.finish()
            return client_response
    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
        if usage_tee is not None:
            usage_tee.finish()
        if client_response is not None and client_response.prepared:
            # Headers already committed downstream: a mid-stream failure (provider abort or
            # client disconnect) can only truncate — the connection simply terminates, no
            # status rewrite; the caller logs the upstream status with usage merged so far.
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


def make_relay_request_headers(inbound: CIMultiDict, key: str) -> CIMultiDict:
    """Build the relay egress headers (auth swapped, version defaulted, identity encoding).

    A public alias over the relay branch's header policy so the Responses Seam egress
    (proxy-03) reuses the exact same auth-swap / hop-by-hop handling instead of a third copy.
    """
    return _relay_request_headers(inbound, key)


def relay_upstream_url(base_url: str, upstream_path: str) -> yarl.URL:
    """Join a Profile ``base_url`` with an upstream path (public alias reused by proxy-03)."""
    return _relay_url(base_url, upstream_path)
