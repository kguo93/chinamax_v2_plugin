"""The local reverse Proxy: Anthropic + Responses ingresses, router, Default + Relay + Seam.

A prefixless request rides the Default branch (byte-for-byte passthrough to
api.anthropic.com — ADR 0001 as amended). A ``<profile>/<model>`` prefix naming an
Anthropic-dialect Profile rides the Relay branch (prefix stripped, auth swapped, Scrub,
thinking normalization, extras merged — ADRs 0001/0003/0006; see :mod:`chinamaxM.relay`);
a ``responses``-dialect Profile still answers 501 on the Anthropic ingress, as does a
prefixed count_tokens (proxy-04). A slash-prefixed unknown profile answers a local 404
naming the valid Profile list; every other path and method passes through unconditionally,
streamed.

The Responses ingress ``POST /openai/<profile>/responses`` crosses the LiteLLM Seam
(:mod:`chinamaxM.seam`): the Codex Responses request is translated to an Anthropic Messages
egress body (key injected from the Codex Key file), forwarded with the relay machinery, and
the provider's Anthropic response/SSE is translated back to a Responses event sequence. An
unknown profile ⇒ 404 with the valid list, a ``responses``-dialect Profile ⇒ 501, and any
other method or path under ``/openai/…`` ⇒ the same 404 — never the Default branch.

Per-Host Key files are scaffolded at startup (:mod:`chinamaxM.keyfiles`): the Claude file
always, the Codex file only when its resolved root already exists.

Run as a service with ``python -m chinamaxM.proxy`` (ADR 0009): loads the Registry,
binds ``127.0.0.1`` on the Registry port.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import time
from dataclasses import dataclass

import aiohttp
import yarl
from aiohttp import web
from multidict import CIMultiDict

from chinamaxM import observability, seam
from chinamaxM.keyfiles import KEY_FILE_NAME, KeyFileReader, resolve_host_root, scaffold_key_file
from chinamaxM.registry import Profile, Registry, load_registry
from chinamaxM.relay import (
    forward_relay,
    make_relay_request_headers,
    mutate_body,
    relay_upstream_url,
    serialize_body,
)

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
    log_sink: object = None,
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
        log_sink: An injectable callable receiving one finalized JSONL line per worker
            request on BOTH branches (ADR 0010). ``None`` (the production default) builds the
            append-only :class:`~chinamaxM.observability.JsonlRequestLog` over the resolved
            Claude root; a test passes its own callable to capture lines. Sink exceptions are
            always caught so observability never fails a valid response.

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
    app["codex_keys"] = KeyFileReader(app["codex_root"] / KEY_FILE_NAME)
    if log_sink is None:
        writer = observability.JsonlRequestLog(
            observability.resolve_log_path(app["claude_root"])
        )
        app["log_writer"] = writer
        app["log_sink"] = writer
    else:
        app["log_writer"] = None
        app["log_sink"] = log_sink if callable(log_sink) else (lambda record: None)
    #: Per-Profile negative cache of count_tokens support, keyed (name, base_url); ADR 0010.
    app["count_tokens_unsupported"] = set()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_post("/v1/messages", _messages_handler)
    app.router.add_post("/v1/messages/count_tokens", _count_tokens_handler)
    app.router.add_route("*", "/openai/{tail:.*}", _openai_handler)
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
    """Close the app-scoped upstream ``ClientSession`` and the request-log descriptor."""
    await app["session"].close()
    if app["log_writer"] is not None:
        app["log_writer"].close()


@dataclass
class _Completion:
    """Mutable accumulator for one worker request's JSONL line (ADR 0010).

    Fields fill in as the request proceeds; :func:`_finalize` writes exactly one line in a
    cancellation-safe try/finally. A ``status`` left ``None`` means the handler was cancelled
    before any status was known (a client disconnect before the upstream responded) and is
    logged as 499.
    """

    ingress: str
    profile: str | None = None
    upstream: str | None = None
    model: str | None = None
    status: int | None = None
    usage: dict | None = None
    stripped_tools: list | None = None
    count_tokens_path: str | None = None


def _finalize(request: web.Request, comp: _Completion, start: float) -> None:
    """Assemble and write one JSONL line; the sink is best-effort and never fails a request.

    ``stripped_tools`` appears only when the Seam dropped tool types; ``count_tokens_path``
    only on count_tokens lines that forwarded or estimated. ``usage`` is the provider object
    verbatim, or ``null``.
    """
    line: dict = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "ingress": comp.ingress,
        "model": comp.model,
        "profile": comp.profile,
        "upstream": comp.upstream,
        "status": comp.status if comp.status is not None else 499,
        "latency": time.monotonic() - start,
        "usage": comp.usage,
    }
    if comp.stripped_tools:
        line["stripped_tools"] = comp.stripped_tools
    if comp.count_tokens_path is not None:
        line["count_tokens_path"] = comp.count_tokens_path
    try:
        request.app["log_sink"](line)
    except Exception:  # observability must never corrupt a valid response
        pass


def _stripped_model(body: bytes) -> str | None:
    """Return the forwarded model (Profile prefix stripped) from a routed request body."""
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    model = payload.get("model") if isinstance(payload, dict) else None
    if not isinstance(model, str):
        return None
    return model.split("/", 1)[1] if "/" in model else model


async def _messages_handler(request: web.Request) -> web.StreamResponse:
    """Route POST /v1/messages by Profile prefix; Anthropic matches ride the Relay branch.

    The body is buffered (routed paths must peek the model string); a Default match
    forwards those same bytes UNLOGGED, an Anthropic-dialect match relays, a
    ``responses``-dialect match logs a local 501 (proxy-03). Every matched request appends
    exactly one JSONL line, finalized in a cancellation-safe try/finally.

    Returns:
        The upstream passthrough, a relayed response, a local 404, a 401, or a 501 marker.
    """
    start = time.monotonic()
    body = await request.read()
    registry = request.app["registry"]
    kind, profile = _route(body, registry)
    if kind == "default":
        return await _forward(request, body=body)
    if kind == "unknown":
        return _unknown_profile_response(registry)
    comp = _Completion(
        ingress="anthropic",
        profile=profile.name,
        upstream=str(relay_upstream_url(profile.base_url, "/v1/messages")),
        model=_stripped_model(body),
    )
    try:
        if profile.dialect != "anthropic":
            comp.status = 501
            return _not_implemented_response(
                "relay not yet implemented", profile, request.app["routing_debug"]
            )
        return await _relay(request, body, profile, comp)
    finally:
        _finalize(request, comp, start)


async def _count_tokens_handler(request: web.Request) -> web.StreamResponse:
    """Route POST /v1/messages/count_tokens: forward on the Relay branch with an estimator.

    A Default match passes through UNLOGGED; a ``responses``-dialect match logs a local 501.
    An Anthropic-dialect match forwards the mutated body to the Profile's count_tokens
    endpoint and synthesizes ``ceil(chars/4)`` on 404/405/501, a malformed 2xx, or a
    transport failure (ADR 0010 as amended). Every matched request appends one JSONL line.

    Returns:
        The upstream passthrough, a local 404, a relayed/estimated count, a 401, or a 501.
    """
    start = time.monotonic()
    body = await request.read()
    registry = request.app["registry"]
    kind, profile = _route(body, registry)
    if kind == "default":
        return await _forward(request, body=body)
    if kind == "unknown":
        return _unknown_profile_response(registry)
    comp = _Completion(
        ingress="anthropic",
        profile=profile.name,
        upstream=str(relay_upstream_url(profile.base_url, "/v1/messages/count_tokens")),
        model=_stripped_model(body),
    )
    try:
        if profile.dialect != "anthropic":
            comp.status = 501
            return _not_implemented_response(
                "count_tokens relay not yet implemented", profile, request.app["routing_debug"]
            )
        return await _count_tokens(request, body, profile, comp)
    finally:
        _finalize(request, comp, start)


async def _relay(
    request: web.Request, body: bytes, profile: Profile, comp: _Completion
) -> web.StreamResponse:
    """Inject the Profile's key from the Claude Key file and forward on the Relay branch.

    A missing or empty key fails CLOSED with a 401 naming the variable and Key file; nothing
    is sent upstream. Usage is extracted by a passive tee over the provider-side Anthropic
    response/SSE (relayed bytes never altered) and logged only for a 2xx upstream status.
    """
    reader = request.app["claude_keys"]
    key = reader.get(profile.api_key_env)
    if not key:
        comp.status = 401
        return _auth_error_response(profile.api_key_env, reader.path)
    tee = observability.AnthropicUsageTee()
    response: web.StreamResponse | None = None
    try:
        response = await forward_relay(
            request, body, profile, key, request.app["session"], "/v1/messages", usage_tee=tee
        )
        return response
    finally:
        # Record status/usage from the tee even under cancellation (a client disconnect
        # mid-stream cancels the handler, so ``forward_relay`` never returns): the upstream
        # status is already known once headers arrived, with the usage merged so far.
        if tee.status is not None:
            comp.status = tee.status
            if 200 <= tee.status < 300:
                comp.usage = tee.usage
        elif response is not None:
            comp.status = response.status
        # else: cancelled before any upstream status ⇒ comp.status stays None ⇒ logged 499.


async def _count_tokens(
    request: web.Request, body: bytes, profile: Profile, comp: _Completion
) -> web.StreamResponse:
    """Forward count_tokens to the Profile endpoint, or synthesize the estimate on a gap.

    Reuses the relay serializer/headers so the counted body is exactly what messages will
    send (serialized once — the estimate counts those same bytes). A cached-unsupported
    Profile skips the upstream attempt; a fresh 404/405/501 or malformed 2xx populates that
    negative cache; a transport failure estimates WITHOUT caching; any other upstream status
    passes through verbatim.
    """
    reader = request.app["claude_keys"]
    key = reader.get(profile.api_key_env)
    if not key:
        comp.status = 401
        return _auth_error_response(profile.api_key_env, reader.path)

    egress_bytes = serialize_body(mutate_body(profile, json.loads(body)))
    cache = request.app["count_tokens_unsupported"]
    cache_key = (profile.name, profile.base_url)
    if cache_key in cache:
        return _estimated_count(comp, egress_bytes)

    url = relay_upstream_url(profile.base_url, "/v1/messages/count_tokens")
    headers = make_relay_request_headers(request.headers, key)
    try:
        async with request.app["session"].request(
            "POST", url, headers=headers, data=egress_bytes, allow_redirects=False
        ) as upstream:
            status = upstream.status
            raw = await upstream.read()
            content_type = upstream.headers.get("Content-Type")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        # Transport failure ⇒ estimate, but NEVER cache as unsupported (ADR 0010).
        return _estimated_count(comp, egress_bytes)

    if status in (404, 405, 501):
        cache.add(cache_key)
        return _estimated_count(comp, egress_bytes)
    if 200 <= status < 300:
        if observability.valid_count_tokens_body(raw) is None:
            cache.add(cache_key)  # malformed 2xx ⇒ unsupported
            return _estimated_count(comp, egress_bytes)
        comp.status = status
        comp.count_tokens_path = "upstream"
        return _count_tokens_passthrough(status, content_type, raw)
    # Any other upstream status (non-2xx besides 404/405/501, redirects) relays verbatim.
    comp.status = status
    return _count_tokens_passthrough(status, content_type, raw)


def _estimated_count(comp: _Completion, egress_bytes: bytes) -> web.Response:
    """Answer count_tokens with the synthesized ``ceil(chars/4)`` estimate (status 200)."""
    comp.status = 200
    comp.count_tokens_path = "estimated"
    tokens = observability.estimate_input_tokens(egress_bytes)
    return web.json_response({"input_tokens": tokens}, status=200)


def _count_tokens_passthrough(
    status: int, content_type: str | None, raw: bytes
) -> web.Response:
    """Relay an upstream count_tokens response verbatim (status + body + content type)."""
    headers = {"Content-Type": content_type} if content_type else None
    return web.Response(status=status, body=raw, headers=headers)


async def _openai_handler(request: web.Request) -> web.StreamResponse:
    """Route the Responses ingress; only POST ``/openai/<profile>/responses`` is served.

    An unknown profile ⇒ 404 with the valid list; a ``responses``-dialect Profile ⇒ 501; any
    other method or path under ``/openai/…`` ⇒ the same local 404 — never the Default branch.

    Returns:
        The translated Responses answer, a local 404, a 501, or an OpenAI-shaped 401.
    """
    registry = request.app["registry"]
    tail = request.match_info["tail"]
    parts = tail.split("/")
    if request.method != "POST" or len(parts) != 2 or parts[1] != "responses":
        return _unknown_profile_response(registry)
    profile = registry.profiles.get(parts[0])
    if profile is None:
        return _unknown_profile_response(registry)
    if profile.dialect != "anthropic":
        return _not_implemented_response(
            "responses-dialect relay not yet implemented", profile, request.app["routing_debug"]
        )
    return await _seam_dispatch(request, profile)


async def _seam_dispatch(request: web.Request, profile: Profile) -> web.StreamResponse:
    """Translate one Responses request across the Seam and forward it to the provider.

    Injects the Profile key from the Codex Key file (Responses ingress → Codex file); a
    missing key fails CLOSED with an OpenAI-shaped 401 naming the variable and Key file. A
    deterministic Seam validation failure answers with its OpenAI-shaped status body. Every
    dispatched request appends exactly one JSONL line, finalized in a try/finally.
    """
    start = time.monotonic()
    comp = _Completion(
        ingress="responses",
        profile=profile.name,
        upstream=str(relay_upstream_url(profile.base_url, "/v1/messages")),
    )
    try:
        reader = request.app["codex_keys"]
        key = reader.get(profile.api_key_env)
        if not key:
            comp.status = 401
            return _openai_auth_error(profile.api_key_env, reader.path)

        raw = await request.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            comp.status = 400
            body = seam.openai_error_body(
                "request body must be valid JSON", "invalid_request_error"
            )
            return web.json_response(body, status=400)
        if isinstance(payload, dict) and isinstance(payload.get("model"), str):
            comp.model = payload["model"]

        stream = bool(isinstance(payload, dict) and payload.get("stream"))
        try:
            anthropic_body, meta = seam.responses_to_anthropic(profile, payload, stream=stream)
        except seam.SeamError as exc:
            comp.status = exc.status
            return web.json_response(exc.body, status=exc.status)

        comp.model = anthropic_body["model"]
        if meta.stripped_tools:
            comp.stripped_tools = list(meta.stripped_tools)

        if stream:
            return await _seam_stream(request, profile, key, anthropic_body, comp)
        return await _seam_nonstream(request, profile, key, anthropic_body, comp)
    finally:
        _finalize(request, comp, start)


def _seam_egress(request: web.Request, profile: Profile, key: str, anthropic_body: dict):
    """Open the Seam egress request context to the Profile's Anthropic endpoint."""
    session: aiohttp.ClientSession = request.app["session"]
    url = relay_upstream_url(profile.base_url, "/v1/messages")
    headers = make_relay_request_headers(request.headers, key)
    timeout = aiohttp.ClientTimeout(
        total=None, connect=30, sock_read=seam.UPSTREAM_READ_TIMEOUT
    )
    return session.request(
        "POST",
        url,
        headers=headers,
        data=serialize_body(anthropic_body),
        allow_redirects=False,
        timeout=timeout,
    )


async def _seam_nonstream(
    request: web.Request, profile: Profile, key: str, anthropic_body: dict, comp: _Completion
) -> web.StreamResponse:
    """Forward a non-streaming Seam request and translate the complete Anthropic response.

    Records the upstream status and the provider usage object VERBATIM (``None`` when the
    provider returned no usage) onto ``comp`` for the JSONL line.
    """
    try:
        async with _seam_egress(request, profile, key, anthropic_body) as upstream:
            raw = await upstream.read()
            if upstream.status >= 400:
                comp.status = upstream.status
                return web.json_response(
                    seam.anthropic_error_to_openai(raw), status=upstream.status
                )
            try:
                anthropic_response = json.loads(raw)
            except ValueError:
                comp.status = 502
                return web.json_response(
                    seam.openai_error_body("upstream returned malformed JSON", "api_error"),
                    status=502,
                )
            responses_object, _ = seam.anthropic_response_to_responses(
                anthropic_response, request_model=anthropic_body["model"]
            )
            comp.status = upstream.status
            usage = anthropic_response.get("usage")
            comp.usage = usage if isinstance(usage, dict) else None
            return web.json_response(responses_object, status=200)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        comp.status = 502
        return web.json_response(
            seam.openai_error_body("upstream connection failed", "api_error"), status=502
        )


async def _seam_stream(
    request: web.Request, profile: Profile, key: str, anthropic_body: dict, comp: _Completion
) -> web.StreamResponse:
    """Forward a streaming Seam request and translate the Anthropic SSE into Responses SSE.

    Usage is extracted by a passive tee over the PROVIDER-side Anthropic SSE (never the
    client-side Responses events): every usage-carrying event is shallow-merged in arrival
    order, so a mid-stream termination still logs the usage merged so far.
    """
    parser = seam.AnthropicSSEParser()
    translator = seam.ResponsesStreamTranslator()
    usage: dict | None = None
    client_response: web.StreamResponse | None = None
    try:
        async with _seam_egress(request, profile, key, anthropic_body) as upstream:
            if upstream.status >= 400:
                raw = await upstream.read()
                comp.status = upstream.status
                return web.json_response(
                    seam.anthropic_error_to_openai(raw), status=upstream.status
                )
            comp.status = upstream.status
            client_response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
            )
            await client_response.prepare(request)
            async for chunk in upstream.content.iter_any():
                for event in parser.feed(chunk):
                    part = observability.sse_event_usage(event)
                    if part is not None:
                        usage = {**(usage or {}), **part}
                    for out in translator.handle(event):
                        await client_response.write(seam.sse_bytes(out))
            for out in translator.finish():
                await client_response.write(seam.sse_bytes(out))
            await client_response.write_eof()
            comp.usage = usage
            return client_response
    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
        comp.usage = usage
        if client_response is not None and client_response.prepared:
            for out in translator.fail("upstream stream failed"):
                try:
                    await client_response.write(seam.sse_bytes(out))
                except (aiohttp.ClientError, ConnectionError):
                    break
            await client_response.write_eof()
            return client_response
        comp.status = 502
        return web.json_response(
            seam.openai_error_body("upstream connection failed", "api_error"), status=502
        )


def _openai_auth_error(api_key_env: str, keys_path: object) -> web.Response:
    """Return the OpenAI-shaped 401 for a missing Codex Key, naming the variable and file."""
    body = seam.openai_error_body(
        f"{api_key_env} missing in {keys_path}", "authentication_error"
    )
    return web.json_response(body, status=401)


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
