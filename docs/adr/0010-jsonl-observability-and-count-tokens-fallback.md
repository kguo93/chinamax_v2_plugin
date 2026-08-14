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
  which path answered: `upstream` or `estimated` (corrected 2026-08-14 to match the shipped
  `chinamaxM.observability` / proxy values — see the amendment below).
- **count_tokens failure enumeration** ("never errors the host" is scoped): 404/405/501
  and malformed bodies → estimator (cached as unsupported per the runtime discovery
  above); TRANSPORT failure → estimator, but NOT cached as unsupported; any other
  upstream HTTP error passes through to the host unrewritten.
- **Prefix-era field semantics** (cross-ref ADR 0001, amended 2026-08-13): `model` logs
  the FORWARDED model string (profile prefix already stripped); `profile` carries the
  Profile name. The log path resolves under ADR 0006's canonical root chain.

**Amended 2026-08-14 (optional `synthesized_max_tokens` field).** The schema gains an
OPTIONAL integer line field `synthesized_max_tokens`, present ONLY on Responses-ingress
(Seam) lines where the Seam synthesized the Anthropic `max_tokens` cap — i.e. the inbound
Responses request omitted `max_output_tokens`, so the Seam supplied its deterministic
`DEFAULT_MAX_TOKENS` (8192; ADR 0002). Its value is the synthesized cap actually forwarded
upstream. It is OMITTED on every other line: on Seam lines that carried an explicit
`max_output_tokens`, and on all relay / default / count_tokens lines (present-only, exactly
like `stripped_tools` / `count_tokens_path`; never a null/absent key). Rationale: closes
proxy-03's truncation-diagnosability intent — the silent cap is otherwise invisible in the
log, and `usage.output_tokens ≈ synthesized_max_tokens` ⇒ truncation is likely. No
`stop_reason` field is added; truncation stays INFERRED, not definitive (2026-08-14
decision).

This amendment also **corrects the 2026-08-13 wording** above: the `count_tokens_path`
values are `upstream` / `estimated` (as the shipped `chinamaxM.observability` / proxy set
them and the JSONL carries), NOT the `forwarded` / `estimator` the prose originally named —
a documentation typo, not a behavior change.
