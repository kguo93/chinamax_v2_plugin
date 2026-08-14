# chinamax_v2_plugin
Use native chinese ai agents natively inside claude code/codex harness

## Install

chinamaxM is distributed from its GitHub repository. Add the marketplace, then install the
plugin on whichever Host you use. The install identifier is the same on both Hosts:
`chinamaxm@chinamaxm-plugin`.

### Claude Code

```
claude plugin marketplace add kguo93/chinamax_v2_plugin
claude plugin install chinamaxm@chinamaxm-plugin
```

### Codex

```
codex plugin marketplace add kguo93/chinamax_v2_plugin
codex plugin add chinamaxm@chinamaxm-plugin
```

## Upgrade

On Claude Code:

```
claude plugin update chinamaxm@chinamaxm-plugin
```

On Codex there is no per-plugin update subcommand — refresh the marketplace snapshot, then
re-add:

```
codex plugin marketplace upgrade
codex plugin add chinamaxm@chinamaxm-plugin
```

GitHub is the only canonical marketplace source for both Hosts. The `origin` remote is an
rpi4 git backup mirror only and is never an install or update source.

## API keys

Worker-model keys live in a per-Host env file (`~/.claude/model-keys.env`,
`~/.codex/model-keys.env`), one line per provider. Set each value yourself; the files are
never committed:

```
DEEPSEEK_API_KEY=
GLM_API_KEY=
```
