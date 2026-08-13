# JSONL request log with usage accounting; count_tokens forwarded with estimator fallback

The host CLIs' own cost telemetry is wrong or blind for worker traffic, so the Proxy is
the accounting authority. Every worker-branch request appends one line to
`~/.claude/chinamaxM/requests.jsonl`: `ts, ingress, model, profile, upstream, status,
latency, usage {input, output, cache_read?, cache_creation?}` plus `stripped_tools` when
the Seam dropped tool types (ADR 0002). The default branch is never logged (it is not
ours to observe, and logging it would mean parsing it).

**Cache watch.** The usage cache fields are the production monitor for the prefix-cache
guard in ADR 0002: on providers with automatic prefix caching, consecutive turns of one
Worker conversation must show `cache_read`/`cached_input_tokens` ≈ the prior turn's
input. Cache-reads collapsing to ~0 across consecutive worker turns means something is
varying the request prefix (a translation regression or a non-deterministic mutation) and
is being billed as fresh input — treat as a cost bug against ADR 0002, not as provider
noise. This closes sketch V7 as observability-only: no design element depends on cache
behavior; the log proves it.

**count_tokens.** `POST /v1/messages/count_tokens` routes like messages. The Proxy
forwards it to the matched Profile's Anthropic endpoint; on 404/405/501 or a malformed
body it synthesizes `{input_tokens: ceil(serialized_chars / 4)}` instead of erroring the
host. Per-Profile support is discovered at runtime, cached in-process, and visible in the
log (which path answered) — no Registry field, no doctor probe. Closes sketch V6.

**Amended 2026-08-13 (schema precision; error-path enumeration).**

- **`usage` is the provider's usage object VERBATIM, variants preserved** — the schema
  shorthand `usage {input, output, cache_read?, cache_creation?}` above must not be read
  as normalized field names: kimi reports `cached_tokens`, qwen nests
  `prompt_tokens_details`, and the log keeps each provider's shape untouched (the cache
  watch reads whichever variant the provider emits).
- **Schema gains `count_tokens_path`** — present only on count_tokens lines, recording
  which path answered: `forwarded` or `estimator`.
- **count_tokens failure enumeration** ("never errors the host" is scoped): 404/405/501
  and malformed bodies → estimator (cached as unsupported per the runtime discovery
  above); TRANSPORT failure → estimator, but NOT cached as unsupported; any other
  upstream HTTP error passes through to the host unrewritten.
- **Prefix-era field semantics** (cross-ref ADR 0001, amended 2026-08-13): `model` logs
  the FORWARDED model string (profile prefix already stripped); `profile` carries the
  Profile name. The log path resolves under ADR 0006's canonical root chain.
