# A local model-string-routed reverse proxy with a byte-for-byte Anthropic default branch

Claude Code has no per-agent endpoint override (CLI 2.1.228: strict agent frontmatter
schema, no env/baseURL/apiKey field; `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` are
process-wide; native per-agent provider routing is a filed, unshipped request —
anthropics/claude-code#38698). The gateway protocol docs bless model-string routing
through a single base URL. Codex routes per-role via `model_providers` entries. So the
plugin's only service is one local reverse proxy that both hosts point at, and **the model
string is the routing key** — there is no other per-agent signal on the wire.

**Decision.** A single aiohttp app (Python 3.12, conda env `chinamaxM`) bound to
`127.0.0.1:8402` (default; configurable in the Registry header), loopback-only, no
proxy-level auth. Two ingresses:

- **Anthropic ingress** (`/v1/…`) — what `ANTHROPIC_BASE_URL` points at. Routes
  `POST /v1/messages` and `POST /v1/messages/count_tokens` by `body.model` against
  Profile match strings (exact `models[]` entries first, then globs). Every other path
  passes through to `api.anthropic.com` unmodified.
- **Responses ingress** (`/openai/<profile>/…`) — what generated Codex provider entries
  point at. Profile identity comes from the path; no body peek. Speaks OpenAI Responses
  (Codex is `wire_api="responses"`-only; `"chat"` hard-fails config load on 0.147.0).

**Default branch = match failure.** A request whose model string (or profile path)
matches no Registry match string passes through **byte-for-byte**: body never
re-serialized, auth and `anthropic-beta` headers untouched, so subscription OAuth, prompt
caching, and future request fields survive. Claude main and native Claude subagents
always ride this branch and remain completely normal. Only matched branches rewrite
(auth swap, scrub, extras, thinking normalization, translation — ADRs 0002/0003).

Routing is **stateless per-request**: a worker's whole agentic loop lands on its provider
because every one of its requests carries its model string.

**Rejected**: per-agent env overrides (don't exist); running LiteLLM's proxy server as
the service (its passthrough re-serializes bodies — OAuth/beta-header survival unproven,
fatal for the default branch; cross-ref 0002); resurrecting the old Bridge/Runtime (the
host CLIs' native task systems provide spawn, background, steer, transcript, resume).
Watch: #38698 would let the Claude-side proxy retire; the Registry/generation design must
survive that swap.

**Amended 2026-08-13 (operator grilling: profile-prefix routing; model strings are never
gated).** The original decision read: "Routes `POST /v1/messages` and
`POST /v1/messages/count_tokens` by `body.model` against Profile match strings (exact
`models[]` entries first, then globs)" and "**Default branch = match failure.** A request
whose model string (or profile path) matches no Registry match string passes through
byte-for-byte". Both clauses are **reversed** as follows:

- **Profile-prefix rule (Anthropic ingress).** A `body.model` of the form
  `<profile>/<rest>` — split on the FIRST `/`; `<rest>` may itself contain slashes —
  where `<profile>` names a Registry Profile routes to that Profile's branch. The proxy
  STRIPS the prefix and forwards `<rest>` as the model string **verbatim, with no
  validation against any model list, ever**. The provider is the sole authority on model
  strings: a wrong `<rest>` fails at the provider, and the provider's error status/body
  relays back unrewritten so the failure surfaces to the dispatching session (cross-ref
  0007). Generated Claude artifacts pin `model: <profile>/<model>` (ADR 0004), so worker
  traffic is profile-keyed end-to-end; the Responses ingress was already profile-keyed by
  its path segment. This closes the misroute hole where a worker request with an
  unmatched model string silently rode the Default branch onto the operator's Anthropic
  account.
- **Slash-prefixed, unknown profile ⇒ local 404** naming the valid Profile list — never
  forwarded (no real Anthropic model id contains `/`). The same rule holds on the
  Responses ingress for an unknown profile path segment: local 404 with the valid list,
  never a byte-for-byte passthrough of Responses-dialect bytes to api.anthropic.com.
  This supersedes the "(or profile path)" parenthetical above; `CONTEXT.md`'s "Default
  branch" entry is amended in step.
- **Default branch = no prefix.** A `body.model` without `/`, or an unparseable or
  `model`-less body, rides the Default branch byte-for-byte, unchanged from the original
  decision. Main Claude and native Claude subagents never carry a prefix and remain
  completely normal.
- **Registry `match` globs are retired from routing**, and `models[]` is retired from the
  Registry entirely (ADR 0003). Cross-Profile tie-breaks are moot: Profile names are
  unique keys.
- **Relay-branch header policy (recorded per review):** matched branches drop
  `anthropic-beta`, forward `anthropic-version` or default it to `2023-06-01`, replace
  auth per ADR 0006, and drop hop-by-hop headers. The Default branch continues to pass
  every header untouched.
