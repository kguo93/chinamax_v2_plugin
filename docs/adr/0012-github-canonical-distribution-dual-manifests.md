# GitHub-canonical distribution, dual manifests, rpi4 as git backup only

**Identity.** Plugin `chinamaxM` (the M stands for improved), version starts `0.1.0`,
license GPL-2.0, repo `https://github.com/kguo93/chinamax_v2_plugin` (the `github` remote
already on this working repo). The old `chinamax_plugin` repo is archived at cutover.

**Manifests** follow the old repo's ACTUAL file shapes (the files, not its CLAUDE.md
prose, were ground truth):

| Host | Plugin manifest | Marketplace manifest | Pinned values |
|---|---|---|---|
| Claude Code | `.claude-plugin/plugin.json` | `.claude-plugin/marketplace.json` | plugin name `chinamaxM`; marketplace top-level name `chinamaxM-plugin`; entry `source: "./"` + `version`; author/owner Kevin Guo; Anthropic schema URL retained |
| Codex | `.codex-plugin/plugin.json` | `.agents/plugins/marketplace.json` | plugin name `chinamaxM`, `homepage`/`repository` = the GitHub repo URL, license GPL-2.0, `interface.displayName` `ChinamaXM`; marketplace top-level name `chinamaxM-plugin`, `interface.displayName` `chinamaxM`, entry `source {source: local, path: "./"}`, category `Developer tools`, and the exact old policy block (`installation: AVAILABLE`, `authentication: ON_INSTALL`) |

Install identifier on both Hosts: `chinamaxM@chinamaxM-plugin`. Never copy one
marketplace format over the other; the Codex catalog carries no version/author fields —
do not add Claude-only fields to it. Any change to shipped components requires a version
bump across `pyproject.toml` and both plugin manifests plus the Claude marketplace entry,
as in the old repo.

**rpi4 is git-only.** The `origin` remote (`klg2138@rpi4:...`) is a git backup mirror,
nothing more: plugin `marketplace add`/`update`/`install` must NEVER use it as a source —
GitHub is canonical for both Hosts' marketplaces, local checkout paths are for
development only. (Hardens the old repo's "don't publish rpi4 as canonical" into a hard
rule: no plugin update path may ever touch it.)

**Amended 2026-08-13 (operator decision: kebab-case plugin identity).** The original
identity read: "Plugin `chinamaxM` … marketplace top-level name `chinamaxM-plugin` …
Install identifier on both Hosts: `chinamaxM@chinamaxM-plugin`." **Reversed** — the
current Codex plugin spec and the old repo's Claude manifest test both require
kebab-case plugin names, which `chinamaxM` fails (as does `claude plugin validate
--strict`). The shipped identity is: plugin name **`chinamaxm`**, marketplace top-level
name **`chinamaxm-plugin`**, install identifier **`chinamaxm@chinamaxm-plugin`** on both
Hosts. Unchanged: the Python package stays `chinamaxM` (a valid identifier, internal
only — `src/chinamaxM`, `python -m chinamaxM.proxy`, conda env `chinamaxM`), the
`interface.displayName` values keep their styled forms (`ChinamaXM` / display names are
not regex-bound), and the generated Codex provider-entry ids keep the `chinamaxM-`
prefix (config keys, not plugin names). Every name-bearing manifest/README literal in
ops-02 follows this identity.

**Amended 2026-08-13 (manifest field notes).** (a) `pyproject.toml`'s `license` field
uses the current SPDX id **`GPL-2.0-only`** (PEP 639; recent setuptools rejects the
deprecated `GPL-2.0` there) while the Codex plugin manifest keeps the pinned literal
`GPL-2.0` — the split is deliberate, each surface speaks its own validator's dialect.
(b) `.codex-plugin/plugin.json` OMITS the old repo's `skills`/`hooks` keys until their
target files actually ship in this repo (net-new staging deviation from the old file
shape).
