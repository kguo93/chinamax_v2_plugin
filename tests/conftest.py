"""Shared hermetic fixtures for the chinamaxM proxy suite (ADR 0011).

Every fixture here is function-scoped so no Registry, app, or Overlay state leaks
between tests. Three autouse guards make the suite hermetic: ``HOME`` is redirected to a
temp dir (so a developer's real Overlay never contaminates a load), provider key env
vars are cleared, and a loopback-only socket guard fails any test that resolves or
connects beyond ``127.0.0.1``/``::1``/``localhost``.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from dataclasses import replace

import pytest
from aiohttp import web
from multidict import CIMultiDict

from chinamaxM.proxy import create_app
from chinamaxM.registry import Registry, load_registry

#: Provider key env vars cleared before every test (the six Profiles' keys).
_PROVIDER_KEY_VARS = (
    "DEEPSEEK_API_KEY",
    "MIMO_API_KEY",
    "GLM_API_KEY",
    "MINIMAX_API_KEY",
    "KIMI_API_KEY",
    "QWEN_API_KEY",
)

#: Host tokens the socket guard treats as loopback (plus any ``127.*`` address).
_LOOPBACK_HOSTS = frozenset({"", "127.0.0.1", "::1", "localhost", "0.0.0.0", "::"})


# --------------------------------------------------------------------------- guards


@pytest.fixture(autouse=True)
def _redirect_home(tmp_path, monkeypatch):
    """Point ``HOME`` at a temp dir and clear the Overlay root-chain env vars."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CHINAMAXM_CLAUDE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture(autouse=True)
def _clear_provider_keys(monkeypatch):
    """Clear the six provider keys and any ``ANTHROPIC_*`` credential vars."""
    for var in _PROVIDER_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    for var in list(os.environ):
        if var.startswith("ANTHROPIC_"):
            monkeypatch.delenv(var, raising=False)


def _address_host(address: object) -> str | None:
    """Return the host of a socket address, or ``None`` for non-network addresses."""
    if isinstance(address, tuple) and address:
        return address[0]
    return None


def _is_loopback_host(host: object) -> bool:
    """Return whether a host token is loopback (or a bind wildcard)."""
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str):
        return False
    host = host.strip("[]")
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch):
    """Fail any test that resolves or connects beyond loopback (no new dependency)."""
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_loopback_host(host):
            raise OSError(f"socket guard: blocked non-loopback getaddrinfo for {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(self, address):
        host = _address_host(address)
        if host is not None and not _is_loopback_host(host):
            raise OSError(f"socket guard: blocked non-loopback connect to {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        host = _address_host(address)
        if host is not None and not _is_loopback_host(host):
            raise OSError(f"socket guard: blocked non-loopback connect_ex to {address!r}")
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


# ---------------------------------------------------------------------- fake upstream


class RecordedRequest:
    """One request the fake upstream received, captured verbatim."""

    def __init__(self, method: str, raw_path: str, headers: CIMultiDict, body: bytes):
        self.method = method
        self.raw_path = raw_path
        self.headers = headers
        self.body = body


class FakeProvider:
    """A scriptable, recording stand-in for an Anthropic-Messages upstream.

    It records every request (raw body, header multidict, raw path + query, method) and
    replays either a single scripted response (optionally pre-encoded) or a gated SSE
    stream that releases chunk ``N+1`` only after the test signals it observed chunk N.
    """

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.chat_completions_hits: list[str] = []
        self.url = ""
        self._mode = "response"
        self._status = 200
        self._headers: CIMultiDict = CIMultiDict()
        self._body: bytes = b""
        self._chunks: list[bytes] = []
        self._gated = True
        self._gates: list[asyncio.Event] = []
        self._written: list[asyncio.Event] = []

    def respond(
        self, *, status: int = 200, headers: dict | None = None, body: bytes = b""
    ) -> None:
        """Script a single (possibly pre-encoded) response for the next request."""
        self._mode = "response"
        self._status = status
        self._headers = CIMultiDict(headers or {})
        self._body = body

    def respond_stream(
        self,
        chunks: list[bytes],
        *,
        status: int = 200,
        headers: dict | None = None,
        gated: bool = True,
    ) -> None:
        """Script an SSE response; when ``gated`` use :meth:`release` to advance chunk by chunk.

        ``gated=False`` writes every chunk immediately (for Seam tests that read the whole
        translated stream, where per-chunk gating would deadlock the reader).
        """
        self._mode = "stream"
        self._status = status
        self._headers = CIMultiDict(headers or {})
        self._chunks = list(chunks)
        self._gated = gated
        self._gates = [asyncio.Event() for _ in chunks]
        self._written = [asyncio.Event() for _ in chunks]

    async def wait_written(self, index: int, *, timeout: float = 10.0) -> None:
        """Wait until the fake has written chunk ``index`` (bounded, to fail fast)."""
        await asyncio.wait_for(self._written[index].wait(), timeout=timeout)

    def release(self, index: int) -> None:
        """Signal that the test observed chunk ``index`` so the fake may send the next."""
        self._gates[index].set()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        self.requests.append(
            RecordedRequest(request.method, request.raw_path, request.headers.copy(), body)
        )
        if "chat/completions" in request.raw_path:
            # ADR 0002: chat-completions egress is banned; the fixture refuses it so any
            # Seam degradation to that wire path fails loudly (proxy-03 test 8).
            self.chat_completions_hits.append(request.raw_path)
            return web.Response(status=400, body=b"chat-completions egress is banned")
        if self._mode == "response":
            return web.Response(status=self._status, headers=self._headers, body=self._body)

        response = web.StreamResponse(status=self._status, headers=self._headers)
        await response.prepare(request)
        for index, chunk in enumerate(self._chunks):
            await response.write(chunk)
            self._written[index].set()
            if self._gated and index < len(self._chunks) - 1:
                await asyncio.wait_for(self._gates[index].wait(), timeout=10.0)
        await response.write_eof()
        return response


@pytest.fixture
def make_fake_provider(aiohttp_server):
    """Return an async factory that starts an additional recording fake upstream."""

    async def _make() -> FakeProvider:
        fake = FakeProvider()
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", fake._handle)
        server = await aiohttp_server(app)
        fake.url = str(server.make_url("/")).rstrip("/")
        return fake

    return _make


@pytest.fixture
async def fake_provider(make_fake_provider) -> FakeProvider:
    """Start the recording fake upstream and return its handle."""
    return await make_fake_provider()


# ------------------------------------------------------------------------- Key files


class HostHome:
    """A temp Host root with helpers to write and mutate its Key file (ADR 0006)."""

    def __init__(self, root) -> None:
        self.root = root
        self.keys_path = root / "model-keys.env"

    def write_keys(self, mapping: dict[str, str]) -> None:
        """Write a ``VAR=value`` Key file from a mapping (one line per entry)."""
        self.write_text("".join(f"{name}={value}\n" for name, value in mapping.items()))

    def write_text(self, text: str) -> None:
        """Write raw text as the Key file."""
        self.keys_path.write_text(text, encoding="utf-8")


@pytest.fixture
def claude_home(tmp_path) -> HostHome:
    """A created, empty Claude Host root for injecting Key files into relay tests."""
    home = HostHome(tmp_path / "claude-home")
    home.root.mkdir()
    return home


@pytest.fixture
def codex_home(tmp_path) -> HostHome:
    """A created, empty Codex Host root (proves ingress→file selection in relay tests)."""
    home = HostHome(tmp_path / "codex-home")
    home.root.mkdir()
    return home


# ------------------------------------------------------------------------- proxy app


@pytest.fixture
def make_proxy(aiohttp_client, fake_provider):
    """Return an async factory building a proxy TestClient over the fake upstream."""

    async def _make(
        registry: Registry | None = None,
        *,
        routing_debug: bool = False,
        claude_home: str | None = None,
        codex_home: str | None = None,
        log_sink: object = None,
    ):
        if registry is None:
            registry = load_registry()
        app = create_app(
            registry,
            upstream_base=fake_provider.url,
            routing_debug=routing_debug,
            claude_home=claude_home,
            codex_home=codex_home,
            log_sink=log_sink,
        )
        return await aiohttp_client(app)

    return _make


@pytest.fixture
def relay_registry(fake_provider):
    """The shipped Registry with every Profile's ``base_url`` pointed at the fake upstream.

    Relay forwarding targets ``profile.base_url``, so the seed endpoints (real, non-loopback)
    are rewritten to the fake provider — otherwise the loopback socket guard would fire.
    """
    base = load_registry()
    profiles = {
        name: replace(profile, base_url=fake_provider.url)
        for name, profile in base.profiles.items()
    }
    return Registry(port=base.port, profiles=profiles)


@pytest.fixture
async def proxy_client(make_proxy):
    """A proxy TestClient over the shipped Registry (no routing debug)."""
    return await make_proxy()


# ------------------------------------------------------------- Responses Seam (proxy-03)


def parse_responses_sse(text: str) -> list[dict]:
    """Parse a Responses SSE payload (``event:``/``data:`` frames) into event dicts."""
    events: list[dict] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data_lines = [
            line[len("data:"):].lstrip()
            for line in block.split("\n")
            if line.startswith("data:")
        ]
        if data_lines:
            events.append(json.loads("\n".join(data_lines)))
    return events


class FakeCodexClient:
    """A fake Codex Responses client driving the Responses ingress of a proxy TestClient."""

    def __init__(self, client, profile_name: str) -> None:
        self._client = client
        self.path = f"/openai/{profile_name}/responses"

    async def send(self, body: dict):
        """POST a non-streaming Responses request; return the aiohttp client response."""
        return await self._client.post(self.path, json=body)

    async def send_streaming(self, body: dict) -> list[dict]:
        """POST a streaming Responses request; return the parsed Responses event sequence."""
        response = await self._client.post(self.path, json={**body, "stream": True})
        assert response.status == 200, await response.text()
        text = await response.text()
        return parse_responses_sse(text)

    async def stream_response(self, body: dict):
        """POST a streaming Responses request; return the raw response (for status checks)."""
        return await self._client.post(self.path, json={**body, "stream": True})


@pytest.fixture
def make_seam_client(make_proxy, relay_registry, codex_home, fake_provider):
    """Return a factory building a fake Codex client over a Seam-configured proxy.

    The proxy routes every Profile's ``base_url`` at the shared fake provider (relay
    registry) and reads the Codex Key file from a temp Codex Host root.
    """

    async def _make(profile_name: str = "glm", *, keys: dict | None = None, log_sink=None):
        profile = relay_registry.profiles[profile_name]
        codex_home.write_keys(keys or {profile.api_key_env: "sk-codex-value"})
        client = await make_proxy(
            relay_registry, codex_home=str(codex_home.root), log_sink=log_sink
        )
        return FakeCodexClient(client, profile_name), fake_provider

    return _make


@pytest.fixture(autouse=True)
def _ban_litellm_transport(monkeypatch):
    """Tripwire: make the four banned litellm transport entry points raise if ever called.

    Product code must never invoke ``litellm.responses``/``aresponses``/
    ``anthropic_messages``/``acompletion`` (ADR 0002); the Seam uses pure transforms only.
    """
    import litellm

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("banned litellm transport entry point was invoked")

    for name in ("responses", "aresponses", "anthropic_messages", "acompletion"):
        monkeypatch.setattr(litellm, name, _forbidden, raising=False)
