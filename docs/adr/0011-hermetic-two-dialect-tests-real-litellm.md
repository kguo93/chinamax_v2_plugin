# Hermetic two-dialect fake providers with real LiteLLM in the loop

The test suite (in `tests/`, pytest, spiritual successor to the old repo's ADR 0011
fake-provider server) runs with no API keys and no network, yet exercises the real
translation engine:

- A **fake Anthropic-Messages provider server** stands in for both api.anthropic.com and
  the providers' `/anthropic` endpoints. It asserts **byte-for-byte passthrough including
  headers** on the default branch, and auth-swap / scrub / thinking-normalization /
  extras-merge on relay branches.
- A **fake Codex Responses client** drives the Responses ingress with real Responses-API
  request shapes (streaming, function tools, an unsuppressible `web_search` built-in),
  asserting the Seam's translation and tool-stripping behavior end-to-end.
- **LiteLLM is never mocked.** Translated branches run the real pinned litellm in-process
  against the fake Anthropic upstream; streaming SSE reassembly, tool-call mapping, and
  usage mapping are asserted on real translator output. Mocking the seam would ship the
  riskiest component untested.
- **Prefix-stability byte tests** (the ADR 0002 guard): two consecutive translated
  requests sharing conversation history must produce byte-identical common prefixes; the
  same holds for relay-branch mutations. A determinism regression here is a cost bug
  (ADR 0010's cache watch), caught before it burns tokens.
- **Seam-shim assertions** (ADR 0002): translated history stays strictly append-only
  under litellm's coalescing-prone input patterns (tool_result followed by text); the
  `"type":"custom"` discriminator never reaches the egress body; non-function tool types
  and the Responses `reasoning` param never reach litellm; thinking signatures round-trip
  intact through the Responses reasoning items.
- Generator, doctor, and setup logic are tested against temp dirs, as in the old repo.
  The live baseline behind these hermetic assertions is the 2026-08-13 six-provider
  canary recorded in ADR 0002 (thinking accepted, replay accepted, cache reads paid on
  every provider); re-run it manually if a provider's Anthropic surface changes.

**Amended 2026-08-13.** Routing assertions follow ADR 0001 as amended: profile-prefix
strip-and-forward, slash-prefixed-unknown-profile ⇒ local 404 with the valid list, and
prefixless/unparseable ⇒ byte-for-byte Default branch. No glob or model-list matching
exists to test, and no test may assert any model-string validation — model strings are
opaque pass-through everywhere.
