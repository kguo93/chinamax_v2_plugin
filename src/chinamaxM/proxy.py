"""The local reverse Proxy: Anthropic ingress, Profile-prefix router, Default + Relay.

A prefixless request rides the Default branch (byte-for-byte passthrough to
api.anthropic.com — ADR 0001 as amended). A ``<profile>/<model>`` prefix naming an
Anthropic-dialect Profile rides the Relay branch (prefix stripped, auth swapped, Scrub,
thinking normalization, extras merged — ADRs 0001/0003/0006; see :mod:`chinamaxM.relay`);
a ``responses``-dialect Profile still answers 501 (proxy-03), as does a prefixed
count_tokens (proxy-04). A slash-prefixed unknown profile answers a local 404 naming the
valid Profile list; every other path and method passes through unconditionally, streamed.

Per-Host Key files are scaffolded at startup (:mod:`chinamaxM.keyfiles`): the Claude file
always, the Codex file only when its resolved root already exists.

Run as a service with ``python -m chinamaxM.proxy`` (ADR 0009): loads the Registry,
binds ``127.0.0.1`` on the Registry port.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import yarl
from aiohttp import web
from multidict import CIMultiDict

from chinamaxM.keyfiles import KEY_FILE_NAME, KeyFileReader, resolve_host_root, scaffold_key_file
from chinamaxM.registry import Profile, Registry, load_registry
from chinamaxM.relay import forward_relay

#: Loopback bind address — the Proxy is never exposed off-host (ADR 0001).
LOOPBACK_HOST = "127.0.0.1"

#: Inbound body ceiling (256 MiB) — pinned by the 2026-08-13 grilling; ``[1m]``-context
#: worker bodies must not 413. A 413 at this bound is a router-issued error.
CLIENT_MAX_SIZE = 256 * 2**20

#: Default upstream for the Default branch, overridable only via the app factory.
DEFAULT_UPSTREAM = "https://api.anthropic.com"

#: RFC 7230 §6.1 hop-by-hop headers, stripped in both directions (lowercased).
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

#: Headers the client library must never add on its own — the Proxy forwards only what
#: the caller actually sent (aiohttp otherwise attaches these).
_SKIP_AUTO_HEADERS = frozenset({"User-Agent", "Accept", "Accept-Encoding", "Content-Type"})

#: The debug header carrying the matched Profile name when ``routing_debug`` is on.
_MATCHED_PROFILE_HEADER = "X-Chinamaxm-Matched-Profile"


def create_app(
    registry: Registry,
    *,
    upstream_base: str = DEFAULT_UPSTREAM,
    routing_debug: bool = False,
    claude_home: str | None = None,
    codex_home: str | None = None,
) -> web.Application:
    """Build the Proxy application from an already-loaded Registry.

    Args:
        registry: The resolved Registry supplying routing Profiles and the port.
        upstream_base: The Default-branch upstream origin (scheme + authority). No
            environment override exists — an ambient var could redirect live traffic.
        routing_debug: When true, matched 501 responses carry the
            ``X-Chinamaxm-Matched-Profile`` debug header (test-only, default off).
        claude_home: An explicit Claude Host root that wins over the environment chain
            (ADR 0006 as amended); tests inject a temp root here.
        codex_home: An explicit Codex Host root, resolved the same way.

    Returns:
        The configured, unstarted aiohttp application.
    """
    app = web.Application(client_max_size=CLIENT_MAX_SIZE)
    app["registry"] = registry
    app["upstream_base"] = upstream_base
    app["routing_debug"] = routing_debug
    app["claude_root"] = resolve_host_root("claude", claude_home)
    app["codex_root"] = resolve_host_root("codex", codex_home)
    app["claude_keys"] = KeyFileReader(app["claude_root"] / KEY_FILE_NAME)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_post("/v1/messages", _messages_handler)
    app.router.add_post("/v1/messages/count_tokens", _count_tokens_handler)
    app.router.add_route("*", "/{tail:.*}", _default_handler)
    return app


async def _on_startup(app: web.Application) -> None:
    """Create the app-scoped upstream ``ClientSession`` and scaffold the Key files."""
    app["session"] = aiohttp.ClientSession(
        auto_decompress=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        timeout=aiohttp.ClientTimeout(total=None, connect=30),
        skip_auto_headers=list(_SKIP_AUTO_HEADERS),
    )
    _scaffold_key_files(app)


def _scaffold_key_files(app: web.Application) -> None:
    """Scaffold the Key files after root resolution: Claude always, Codex if its root exists.

    The template lists the loaded Registry's ``api_key_env`` names (never a hard-coded set).
    """
    names = [profile.api_key_env for profile in app["registry"].profiles.values()]
    scaffold_key_file(app["claude_root"] / KEY_FILE_NAME, names)
    if app["codex_root"].exists():
        scaffold_key_file(app["codex_root"] / KEY_FILE_NAME, names)


async def _on_cleanup(app: web.Application) -> None:
    """Close the app-scoped upstream ``ClientSession``."""
    await app["session"].close()


async def _messages_handler(request: web.Request) -> web.StreamResponse:
    """Route POST /v1/messages by Profile prefix; Anthropic matches ride the Relay branch.

    The body is buffered (routed paths must peek the model string); a Default match
    forwards those same bytes, an Anthropic-dialect match relays, a ``responses``-dialect
    match stays 501 (proxy-03).

    Returns:
        The upstream passthrough, a relayed response, a local 404, a 401, or a 501 marker.
    """
    body = await request.read()
    kind, profile = _route(body, request.app["registry"])
    if kind == "default":
        return await _forward(request, body=body)
    if kind == "unknown":
        return _unknown_profile_response(request.app["registry"])
    if profile.dialect != "anthropic":
        return _not_implemented_response(
            "relay not yet implemented", profile, request.app["routing_debug"]
        )
    return await _relay(request, body, profile)


async def _count_tokens_handler(request: web.Request) -> web.StreamResponse:
    """Route POST /v1/messages/count_tokens; matched Profiles stay 501 (proxy-04 owns relay).

    Returns:
        The upstream passthrough, a local 404, or the 501 count_tokens marker.
    """
    body = await request.read()
    kind, profile = _route(body, request.app["registry"])
    if kind == "default":
        return await _forward(request, body=body)
    if kind == "unknown":
        return _unknown_profile_response(request.app["registry"])
    return _not_implemented_response(
        "count_tokens relay not yet implemented", profile, request.app["routing_debug"]
    )


async def _relay(request: web.Request, body: bytes, profile: Profile) -> web.StreamResponse:
    """Inject the Profile's key from the Claude Key file and forward on the Relay branch.

    A missing or empty key fails CLOSED with a 401 naming the variable and Key file;
    nothing is sent upstream.
    """
    reader = request.app["claude_keys"]
    key = reader.get(profile.api_key_env)
    if not key:
        return _auth_error_response(profile.api_key_env, reader.path)
    return await forward_relay(
        request, body, profile, key, request.app["session"], "/v1/messages"
    )


async def _default_handler(request: web.Request) -> web.StreamResponse:
    """Pass every non-routed path/method through the Default branch, streaming."""
    return await _forward(request, body=None)


def _route(body: bytes, registry: Registry) -> tuple[str, Profile | None]:
    """Classify a routed request from its body bytes.

    A non-JSON body, a non-object body, or a missing/non-string/empty ``model`` field,
    or a ``model`` with no ``/`` ⇒ Default branch. Otherwise the text before the first
    ``/`` names a Profile (exact, case-sensitive) or is unknown.

    Returns:
        ``("default", None)``, ``("unknown", None)``, or ``("matched", profile)``.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return ("default", None)
    if not isinstance(payload, dict):
        return ("default", None)
    model = payload.get("model")
    if not isinstance(model, str) or not model or "/" not in model:
        return ("default", None)
    prefix = model.split("/", 1)[0]
    profile = registry.profiles.get(prefix)
    if profile is None:
        return ("unknown", None)
    return ("matched", profile)


def _unknown_profile_response(registry: Registry) -> web.Response:
    """Return the local 404 for a slash-prefixed but unknown Profile."""
    known = ", ".join(registry.profiles)
    body = {
        "error": {
            "type": "unknown_profile",
            "message": f"unknown profile prefix; known profiles: {known}",
        }
    }
    return web.json_response(body, status=404)


def _not_implemented_response(
    message: str, profile: Profile, routing_debug: bool
) -> web.Response:
    """Return the 501 marker for a matched Profile whose branch is not yet implemented."""
    body = {"error": {"type": "not_implemented", "message": message}}
    response = web.json_response(body, status=501)
    if routing_debug:
        response.headers[_MATCHED_PROFILE_HEADER] = profile.name
    return response


def _auth_error_response(api_key_env: str, keys_path: object) -> web.Response:
    """Return the 401 for a missing/empty Profile key, naming the variable and Key file."""
    body = {
        "error": {
            "type": "authentication_error",
            "message": f"{api_key_env} missing in {keys_path}",
        }
    }
    return web.json_response(body, status=401)


async def _forward(request: web.Request, *, body: bytes | None) -> web.StreamResponse:
    """Forward one request to the upstream and stream the response back byte-for-byte.

    Args:
        request: The inbound request.
        body: Pre-buffered request bytes (routed Default branch), or ``None`` to stream
            the request body straight from ``request.content`` (non-routed paths).

    Returns:
        The streamed upstream response, or a synthesized 502 if the upstream fails
        before its response headers are committed downstream.
    """
    session: aiohttp.ClientSession = request.app["session"]
    url = _upstream_url(request.app["upstream_base"], request.raw_path)
    headers = _forward_request_headers(request.headers)

    if body is not None:
        data: object = body
    elif request.body_exists:
        data = request.content
    else:
        data = None

    client_response: web.StreamResponse | None = None
    try:
        async with session.request(
            request.method,
            url,
            headers=headers,
            data=data,
            allow_redirects=False,
        ) as upstream:
            client_response = web.StreamResponse(status=upstream.status)
            for name, value in _response_header_pairs(upstream.headers):
                client_response.headers.add(name, value)
            await client_response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await client_response.write(chunk)
            await client_response.write_eof()
            return client_response
    except (aiohttp.ClientError, asyncio.TimeoutError):
        if client_response is not None and client_response.prepared:
            # Headers already committed downstream: a mid-stream failure can only
            # truncate — close the connection, no status rewrite.
            return client_response
        return _bad_gateway_response()


def _upstream_url(upstream_base: str, raw_path: str) -> yarl.URL:
    """Build the upstream URL from the base authority and the inbound raw path+query.

    ``encoded=True`` keeps percent-encodings and delimiters exactly as received.
    """
    base = yarl.URL(upstream_base)
    return yarl.URL(f"{base.scheme}://{base.raw_authority}{raw_path}", encoded=True)


def _hop_by_hop(headers: CIMultiDict) -> set[str]:
    """Return the hop-by-hop header names for one message.

    The RFC 7230 §6.1 set plus every token named inside ``Connection`` headers
    (comma-separated, case-insensitive, across repeats).
    """
    hop = set(_HOP_BY_HOP)
    for value in headers.getall("Connection", []):
        for token in value.split(","):
            token = token.strip().lower()
            if token:
                hop.add(token)
    return hop


def _forward_request_headers(headers: CIMultiDict) -> CIMultiDict:
    """Copy inbound request headers minus hop-by-hop, ``Host`` and ``Content-Length``.

    ``Host`` is dropped so the client library regenerates it for the upstream URL, and
    ``Content-Length`` so the client recomputes framing for both the buffered-bytes and
    streamed-body paths (the body bytes are unchanged — only framing is recomputed).
    Everything else (auth and ``anthropic-*`` included) forwards verbatim.
    """
    hop = _hop_by_hop(headers)
    out: CIMultiDict = CIMultiDict()
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in hop or lowered in ("host", "content-length"):
            continue
        out.add(name, value)
    return out


def _response_header_pairs(headers: CIMultiDict) -> list[tuple[str, str]]:
    """Return upstream response header pairs minus hop-by-hop (``Content-Encoding`` kept)."""
    hop = _hop_by_hop(headers)
    return [(name, value) for name, value in headers.items() if name.lower() not in hop]


def _bad_gateway_response() -> web.Response:
    """Return the 502 synthesized when the upstream fails before responding."""
    body = {"error": {"type": "bad_gateway", "message": "upstream request failed"}}
    return web.json_response(body, status=502)


def main() -> None:
    """Run the Proxy as a service: load the Registry, bind loopback on its port."""
    registry = load_registry()
    app = create_app(registry)
    web.run_app(app, host=LOOPBACK_HOST, port=registry.port, print=None)


if __name__ == "__main__":
    main()
