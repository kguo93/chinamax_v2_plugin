# profiles.json v2 — one Registry, six shipped Profiles, thinking normalization first-class

One Registry is the single source of truth feeding Proxy routing, agent generation on
both Hosts, key scaffolding, and doctor checks: shipped defaults inside the plugin, user
Overlay at `~/.claude/chinamaxM/profiles.json` (same merge semantics as the old plugin).

**Schema** (per Profile):

```jsonc
{
  "port": 8402,
  "profiles": [
    {
      "name": "deepseek",
      "dialect": "anthropic",              // "anthropic" | "responses" (no chat-completions — ADR 0002)
      "base_url": "https://api.deepseek.com/anthropic",
      "api_key_env": "DEEPSEEK_API_KEY",
      "default_model": "deepseek-v4-pro[1m]",
      "context_window": {"deepseek-v4-pro[1m]": 1000000},  // optional per-model map; pins Codex role metadata
      "thinking": {"extra_body": {"reasoning": {"effort": "max"}}},
      "scrub": ["cache_control", "output_config"],
      "request_extras": {},                // merged LAST, replace-on-conflict; guarded — see Amended 2026-08-13
      "tools": null                        // null ⇒ generation OMITS the tools key; the host grants its standard set
    }
  ]
}
```

**Six Profiles ship**, carried verbatim from the old plugin's live-verified registry
(base URL, model, key var, reasoning policy all proven in daily use):

| profile | anthropic endpoint | default model | key var | thinking policy |
|---|---|---|---|---|
| deepseek | api.deepseek.com/anthropic | deepseek-v4-pro[1m] | DEEPSEEK_API_KEY | extra_body.reasoning.effort=max |
| mimo | api.xiaomimimo.com/anthropic | mimo-v2.5 | MIMO_API_KEY | extra_body.reasoning_effort=high |
| glm | api.z.ai/api/anthropic | glm-5.2 | GLM_API_KEY | thinking:{type:enabled} |
| minimax | api.minimax.io/anthropic | MiniMax-M3[1m] | MINIMAX_API_KEY | thinking:{type:adaptive} |
| kimi | api.moonshot.ai/anthropic | kimi-k3 | KIMI_API_KEY | extra_body.reasoning_effort=max |
| qwen | dashscope-intl.aliyuncs.com/apps/anthropic | qwen3.8-max | QWEN_API_KEY | thinking:{type:enabled} |

All six rows were re-live-verified 2026-08-13 by the seam canary (ADR 0002): thinking
policy accepted on every endpoint, verbatim history replay accepted, prefix-cache reads
paid. Provider quirks recorded there: deepseek/glm/minimax sign thinking blocks,
kimi/mimo/qwen don't; mimo may emit text before thinking; qwen's cache granularity is
coarser. Qwen gotcha (the OLD REPO's ADR 0001, `anthropic-messages-wire-format` there —
not this repo's ADR 0001): an explicit `thinking.budget_tokens` must be
strictly less than request `max_tokens` or the endpoint 400s; the shipped budget-less
`{"type":"enabled"}` sidesteps this.

**Thinking normalization is first-class**: the Proxy strips the Claude-flavored
`thinking`/`output_config` form and substitutes the Profile's `thinking` policy on every
worker-bound request. A second model of a provider needing different shaping is a
per-model override map inside the Profile, never a new Profile.

**Scrub is load-bearing for cost**, not just compatibility: Claude Code's `cache_control`
breakpoints move between turns, and these providers cache by exact byte prefix (DeepSeek:
automatic, cache-hit input ~10x cheaper — old repo evidence). Forwarding them would both
vary the body per turn and be ignored upstream, so `cache_control` is scrubbed
deterministically on every relay branch. All relay-branch mutations (scrub, thinking
substitution, extras merge) are deterministic functions of the Profile so the serialized
request prefix stays byte-stable turn over turn (guard: ADR 0002; tests: ADR 0011).

**Superseded from the sketch**: D16's per-host `codex_base_url`/`codex_models[]`/
`codex_default_model` are deleted — Codex egress no longer touches provider Responses
surfaces (ADR 0002), and the Anthropic-surface IDs (`[1m]` suffixes included) are the
only IDs anywhere. The sketch's per-profile `tool_scrub` is also gone — replaced by the
global function-tools-only Seam rule (ADR 0002).

**Amended 2026-08-13 (operator grilling: `models[]` and `match` retired; full seed
registry pinned; mutation order and extras guard recorded).**

- **`models[]` and `match` are removed from the schema.** The original schema carried
  `"match": ["deepseek-*"]  // globs; exact models[] entries match first` and
  `"models": ["deepseek-v4-pro[1m]", "deepseek-v4-flash"]`, and this ADR closed with
  "`models[]` drives agent generation; `match` globs route unlisted models too (routing
  works, spawning doesn't)." All of that is **reversed**: routing is profile-prefix keyed
  (ADR 0001), generation emits ONE artifact per Profile pinned to `default_model`
  (ADR 0004), and **no model list is ever consulted or verified at dispatch or anywhere
  else** — model strings pass through verbatim and the provider is the sole authority.
  `default_model` (scalar) and the optional `context_window` per-model-string map are the
  only model-bearing Registry fields.
- **Full seed registry pinned.** The six-Profile table above, plus: all six ship
  `"dialect": "anthropic"`, `"scrub": ["cache_control", "output_config"]`,
  `"request_extras": {}`, `"tools": null`, and `base_url` carries the `https://` scheme.
  Only deepseek carries a `context_window` map (`{"deepseek-v4-pro[1m]": 1000000}`). The
  uniform scrub for the five non-deepseek Profiles is hereby pinned policy (it was
  previously a plan extrapolation). This table + paragraph ARE the shipped
  `profiles.json` seed; the proxy-01 plan copies it verbatim.
- **Canonical mutation order** on worker-bound requests: **strip → scrub →
  thinking-merge → extras-merge** (extras still merged LAST, replace-on-conflict). All
  four stages are deterministic functions of the Profile (prefix-stability guard,
  ADR 0002).
- **`request_extras` guard**: extras must never re-add a scrubbed field (`cache_control`
  above all), and reserved request keys are rejected at Registry load — carrying forward
  the old plugin's `RESERVED_REQUEST_KEYS` protection.
- **Profile names are reserved-checked at Registry load** on both hosts (ADR 0004's
  reserved lists): a shipped or Overlay Profile whose name collides is refused with a
  clear error, never generated.
- The Overlay path resolves under the canonical root chain of ADR 0006 (as amended
  2026-08-13), not a hardcoded `~/.claude`.

**Amended 2026-08-19 (mimo default model changed).**

- The mimo Profile's shipped `default_model` was `mimo-v2.5-pro`; it is now
  **`mimo-v2.5`**. The table above carries the new pin. Endpoint, key var, thinking
  policy, and scrub are unchanged; the 2026-08-13 live-verification note above was
  performed against the old pin.
