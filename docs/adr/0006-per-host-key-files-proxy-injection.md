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

**Amended 2026-08-18 (Key-file scaffolding is Host-scoped — cross-ref ADR 0005 as amended
2026-08-18).** The original decision read "Both are O_EXCL-scaffolded comments-only
templates". **Reversed on the WHO**: setup scaffolds ONLY the invoking Host's Key file
(a Claude run → `~/.claude/model-keys.env`; a Codex run → `~/.codex/model-keys.env`),
and the `~/.codex`-existence gate is retired with it — the file exists because that
Host's setup ran, not because the home directory was spotted. Everything else stands:
O_EXCL comments-only scaffolding (a v1-populated file is never clobbered — the step
SKIPs when the file exists), the Proxy remains the only reader of key VALUES and still
loads whichever of the two files exist, choosing by the request's ingress Host, and
doctor/`/profiles` PRESENT/MISSING reporting is now per-invoking-Host (ADR 0005).

**Amended 2026-08-18 (per-OS root pinning — clarification only, no behavior change).**
A grilling questioned whether the canonical chain lands wrong on Windows/macOS (suspecting
OS-native app-data dirs). Verified against the hosts' official docs: **both Hosts keep
their app data in a home dotfolder on every OS** — neither uses the OS-native app-data
convention — so the chain above is confirmed correct and is now pinned per OS and Host:

| OS | Claude root (`~/.claude`) | Codex root (`~/.codex`) |
|---|---|---|
| Linux | `/home/<name>/.claude` | `/home/<name>/.codex` |
| macOS | `/Users/<name>/.claude` | `/Users/<name>/.codex` |
| Windows (native) | `%USERPROFILE%\.claude` | `%USERPROFILE%\.codex` |

- Explicitly **NOT** `%APPDATA%\Claude` and **NOT** `~/Library/Application Support/Claude`
  — those belong to Claude Desktop, a different product that never reads these Key files.
- WSL and native Windows are separate installs with separate homes; each resolves its own
  root through the same chain (`expanduser` in the running interpreter picks that
  platform's home, so the Key file always scaffolds where the invoking Host's app data
  actually lives).
- The hosts' own relocation variables remain the chain's third rung
  (`$CLAUDE_CONFIG_DIR` / `$CODEX_HOME`), so a deliberately moved host install is followed
  automatically on any OS.
- Evidence: code.claude.com/docs/en/claude-directory;
  developers.openai.com/codex/config-basic.
