# Per-Host key files with proxy-side injection

Each Host owns a separate secrets file: `~/.claude/model-keys.env` (Claude) and
`~/.codex/model-keys.env` (Codex). Both are O_EXCL-scaffolded comments-only templates
listing every Profile's `api_key_env` name; no command ever prints a value — names plus
PRESENT/MISSING only. The **Proxy is the only reader**: it loads both files
(mtime-cached) and injects the Profile's key on every worker-bound request, choosing the
file that matches the request's ingress Host (Anthropic ingress → Claude file; Responses
ingress → Codex file). On the relay branch the injected key replaces the inbound
Anthropic OAuth on matched requests only; on translated branches it is passed to LiteLLM
as the egress credential.

**Supersedes sketch D17** ("Codex bears keys via env_key → proxy forwards"). Generated
Codex provider entries carry **no `env_key`**: Codex processes never need provider
variables in their environment, workers spawn even when a key is missing (they fail at
the Proxy with a clear 401 naming the missing variable and file), and the egress hop
needs the key as a call parameter regardless, so request-borne bearers would just be a
second path to test. Doctor reports PRESENT/MISSING per Profile for both files (WARN
level — a missing key degrades one Profile, it doesn't break the installation).

**Amended 2026-08-13 (operator grilling: one canonical root chain; wording fixes).**

- **Canonical root-resolution chain, shared by the Proxy and every Host surface**
  (doctor, setup, generators, key scaffolding). Per Host root:
  `explicit constructor/test override → CHINAMAXM_CLAUDE_HOME / CHINAMAXM_CODEX_HOME →
  $CLAUDE_CONFIG_DIR / $CODEX_HOME → ~/.claude / ~/.codex`. Every slice resolves through
  this ONE chain — previously the Proxy plans omitted the `$CLAUDE_CONFIG_DIR`/
  `$CODEX_HOME` steps the host surfaces honored, so on a machine with
  `CLAUDE_CONFIG_DIR` set doctor/setup would certify key files under the override root
  while the running Proxy read `~/.claude/model-keys.env` and 401'd. The key files,
  Overlay, generated artifacts, and log/service paths all resolve under this chain.
- **"The Proxy is the only reader" means the only reader of key VALUES.** Doctor and
  `/profiles` open the files solely for variable NAMES and presence; no surface but the
  Proxy's injector ever reads a value, and no command prints one.
- The clause "on translated branches it is passed to LiteLLM as the egress credential"
  is stale (ADR 0002: LiteLLM is never the HTTP client) and is **overridden**: on
  translated branches the Proxy injects the key into its OWN egress client, exactly as
  on the relay branch.
