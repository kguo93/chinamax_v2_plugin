# Anthropic-native everywhere; embedded LiteLLM is the sole translator; OpenAI dialect = Responses only

**Reverses three sketch decisions by operator directive (grilled 2026-08-13).** The
sketch's D12 said "in-house translation module (no LiteLLM)"; D15 made the Codex path a
pure Responses relay to provider `/responses` surfaces; D19 shipped no translator at all.
All three are overridden:

1. **Anthropic Messages is the native dialect at all times** — internally and on egress.
   Every worker-bound request terminates at the Profile's Anthropic-compatible endpoint
   (all six shipped Profiles have live-verified ones — ADR 0003). Provider `/responses`
   surfaces are **never used**: not every provider implements them (kimi-k3's is
   unimplemented — Moonshot 403s, account-gated), and relying on them made provider
   support ragged (mimo's gateway 400s, deepseek's server-side gates).
2. **LiteLLM, embedded as a Python library in the proxy process, is the only translation
   engine — used as a pure TRANSFORMATION library, never as the HTTP client.** LiteLLM's
   Responses⇄Anthropic mapping functions do the dialect work (probe-verified pure and
   deterministic on 1.96.2); the PROXY builds the egress body and owns the wire on every
   branch, with the same machinery as the relay branch. This split is forced by live
   evidence, not taste: driving litellm as transport (`litellm.anthropic_messages` /
   `aresponses` end-to-end) silently DROPS the reasoning configuration of 4 of the 6
   Profiles (deepseek/mimo/kimi `extra_body` extras vanish from the wire; minimax
   adaptive thinking refused by litellm's own model-capability map; a `reasoning_effort`
   kwarg is rewritten into `thinking:{budget_tokens:4096}`, which breaks qwen's
   deliberately budget-less policy) — no escape hatch exists at any setting. Pin
   **`litellm[proxy]==1.96.2` EXACTLY** (base install crashes on streaming via a
   proxy-extra import chain; PyPI releases are non-monotonic, so ranged constraints
   resolve unpredictably); `telemetry=False`, `suppress_debug_info=True`,
   `LITELLM_LOCAL_MODEL_COST_MAP=True`, and `enable_anthropic_prompt_caching` stays
   **False** forever — it injects a MOVING `cache_control` breakpoint that rewrites the
   last message every turn, exactly the cache-poison the old repo rejected. No in-house
   dialect module, no separate LiteLLM proxy service (two failure domains, and its
   passthrough would kill ADR 0001's byte-for-byte default branch). Doctor FAILs when
   the pinned litellm can't import.
3. **The only OpenAI dialect anywhere is the Responses API** — chat-completions is
   deprecated and banned in this codebase. Today translation happens at exactly one
   Seam: the Codex ingress (Responses in ⇄ Anthropic out). A future provider with no
   Anthropic endpoint (`dialect: "responses"`) would be the same Seam mirrored on
   egress; a chat-completions egress will not be built.

**Seam tool rule (conservative).** Only plain function tools cross a Seam — each host
loads its own function-tool set, which translates cleanly. Every other tool type
(Codex's unsuppressible `web_search` built-in, code_interpreter, anything future) is
stripped for all Profiles before translation, so the model never sees it and never calls
it. Stripping is logged (ADR 0010). No per-profile tool_scrub exists (ADR 0003).

**The Seam shim (mandatory, deterministic).** Around litellm's transforms the proxy
applies: PRE — strip non-function tool types and the Responses `reasoning` param (both
hard-error inside litellm otherwise; the Profile's thinking policy replaces the latter);
POST — strip the `"type":"custom"` discriminator litellm injects into translated
function tools (live-confirmed 400 on DeepSeek: only `web_search_*` variants are legal
tool types there); pin message boundaries 1:1 so translated history stays STRICTLY
APPEND-ONLY (litellm's own bridge coalesces adjacent items — an already-sent
`[tool_result]` message grows into `[tool_result, text]` next turn — which moves the
cache-divergence point earlier than the append seam); round-trip thinking signatures
through the Responses reasoning item's opaque content so provider replay stays verbatim
(deepseek/glm/minimax sign their thinking blocks; kimi/mimo/qwen don't — replay was
accepted live in both cases, so preservation is required where present, harmless where
absent).

**Prefix-cache stability guard (binding, live-proven 2026-08-13).** Providers bill
cached prefix reads ~10x cheaper than fresh input (old-repo evidence, DeepSeek automatic
64-token-unit caching). A translator that serializes the same history differently turn
over turn silently converts those reads into fresh-input billing. Therefore: the
translated request pipeline MUST be deterministic and prefix-stable — same conversation
history ⇒ byte-identical serialized prefix — with no per-request varying fields (ids,
timestamps, version strings) entering the egress body. **Canary evidence (live, all six
providers, 2026-08-13)**: proxy-built bodies with the Profile's thinking policy at body
root and turn-1 history replayed verbatim (thinking blocks and signatures included) were
accepted everywhere and paid out prefix-cache reads on turn 2 — deepseek 2048/~2132,
glm 1984/~2035, minimax 2181/~2248, kimi 2048/~2191, mimo 1984/~2051, qwen 1024/~2111
(partial cache granularity) — with thinking active on every provider. Enforced three
ways: the Seam shim above, hermetic byte-identity tests (ADR 0011), and the JSONL
cache-read watch in production (ADR 0010). The guard outranks translator convenience.

**Amended 2026-08-13 (Seam boundary rule precision).** The shim clause above, "pin
message boundaries 1:1", is superseded by the **same-type-run grouping rule**: parallel
tool calls require all of one assistant turn's `tool_use` blocks in one message and
their `tool_result`s in the next user message (Anthropic adjacency); run grouping
preserves strict append-only while cross-type merging stays banned. Additionally, the
canary-evidence paragraph is to be extended on the next live run to record explicitly
whether consecutive same-role messages and parallel tool-call shapes are accepted on all
six gateways (the 2026-08-13 canary proved verbatim replay, not those shapes).

**Amended 2026-08-18 (the Seam is never stricter than the API it emulates).** An input
item that carries no `type` is read as a `message`, not rejected. The Responses API makes
`type` optional on an input message — every other item type (`reasoning`,
`function_call`, `function_call_output`) carries it, so the omission is unambiguous — and
a Seam that 400s where the emulated API accepts is a chinamaxM bug, not strictness. Found
by the setup live probe (ADR 0005), which was itself sending the loose shape
`{"role":"user","content":"ping"}` and so 400'd on all six Profiles while real Codex
traffic passed; the probe now sends Codex's own wire shape (typed message, `input_text`
content), so a green probe proves the path Codex actually uses. Grouping is unaffected: a
defaulted item joins the same-type run it would have joined had the `type` been written
out, so append-only and prefix stability hold.

**Amended 2026-08-18 (output runs are hoisted past interleaved items — same-type-run
grouping alone proved insufficient).** The 2026-08-13 amendment above read: "parallel
tool calls require all of one assistant turn's `tool_use` blocks in one message and
their `tool_result`s in the next user message (Anthropic adjacency); run grouping
preserves strict append-only". Run grouping scanned strictly CONSECUTIVE items, so a
Host-injected message BETWEEN two of a run's outputs split the output run and the Seam
emitted `assistant[tool_use A, tool_use B] → user[result A] → user[result B]` — exactly
what the adjacency rule forbids. Live failure 2026-08-18 08:03:17: a Codex deepseek
Worker's parallel `exec_command` turn had the operator's PreToolUse hook
additionalContext land as a developer message between the outputs (child rollout
`…01a013e5-2ef2…`), and DeepSeek 400'd with `messages.4: tool_use ids were found
without tool_result blocks immediately after`. **Extension**: before grouping, a call
run's outputs are hoisted ahead of ANY items interleaved among them (system/developer
AND user-role messages alike, per the 2026-08-18 grilling) so the grouped results are
always the one message immediately after their `tool_use` turn; the interleaved items
keep their relative order after it — a folded system/developer message still lands in
top-level `system`, a user message becomes its own message following the results, never
merged into them. The hoist is a deterministic pure function of the item-list prefix, so
append-only and prefix-cache stability hold; run isolation (one litellm transform per
run) is unchanged.

**Kimi-k3 Responses bug (now precisely located).** litellm has NO Responses config for
moonshot: a Responses-path call for kimi silently degrades to a **chat-completions**
egress (untracked in upstream issue #33921; kimi-k3 absent from litellm's model map).
Chat-completions being banned here, that degradation is the bug the doctor watches for:
if the Seam would ever produce a chat-completions egress, doctor WARNs and continues —
never a fatal exit (ADR 0005). chinamaxM's own kimi path is unaffected (the Seam targets
kimi's Anthropic endpoint directly — live-canaried, cache reads confirmed). Watch:
Moonshot enabling `/responses`, litellm PR #33922.
