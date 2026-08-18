# chinamaxM
Use Chinese AI models — DeepSeek, MiMo, GLM, MiniMax, Kimi, Qwen — as native
background subagents inside Claude Code or Codex.

Every worker request goes through one small local reverse proxy that routes it to the
right provider. Claude itself is untouched and keeps talking straight to Anthropic. The M
stands for improved.

## Requirements

- **Prerequisites installed for you, on approval.** Setup DETECTS the platform tools it needs —
  `bash`, Miniconda (a dedicated `chinamaxM` Python 3.12 conda env runs the proxy), and, on
  Windows, Git for Windows (its Git Bash is what the Codex hooks use). If any is missing, setup
  PAUSES and shows you the exact install commands — nothing is installed until you type
  `approve`, and setup then runs them for you. A `miniconda` step runs `conda init`, which edits
  your shell startup files. On unsupported CPU architectures setup falls back to telling you to
  install Miniconda yourself.
- **No pre-installed Python needed on Windows; macOS needs one.** The launcher resolves
  its own interpreter (setup-recorded env python → conda → bootstrap fallbacks). On a bare
  Windows box it bootstraps Git for Windows and Miniconda straight from `cmd.exe` (winget +
  the official Miniconda installer), then continues. On macOS, install a real Python 3
  first (`xcode-select --install`, Homebrew, or python.org) — setup will not install it
  for you.
- **Claude Code and/or Codex** — the plugin installs on either host.
- Linux, macOS, or Windows.

## Commands on both hosts

Every command below is a Claude Code slash command. On Codex the same thing exists as a
skill with the same name and arguments, just without the leading slash:

| Claude Code | Codex |
|---|---|
| `/chinamaxm:setup` | `chinamaxM-setup` skill |
| `/chinamaxm:task` | `chinamaxM-task` skill |
| `/chinamaxm:doctor` | `chinamaxM-doctor` skill |
| `/chinamaxm:profiles` | `chinamaxM-profiles` skill |

The rest of this README uses the Claude Code form.

## 1. Install the plugin

chinamaxM ships from GitHub. Add the marketplace, then install. The install id is the same
on both hosts: `chinamaxm@chinamaxm-plugin`. GitHub is the only install/update source on
both hosts — the `origin` git remote is only an rpi4 git backup mirror, never an install
source.

**Claude Code**
```
claude plugin marketplace add kguo93/chinamax_v2_plugin
claude plugin install chinamaxm@chinamaxm-plugin
```

**Codex**
```
codex plugin marketplace add kguo93/chinamax_v2_plugin
codex plugin add chinamaxm@chinamaxm-plugin
```

## 2. Run setup

Installing only adds the commands. `/chinamaxm:setup` wires everything up. It is the only
command that changes your machine, and it changes **nothing** until you approve.

**Prerequisites first.** If a platform tool is missing (`bash`, Miniconda, or — on Windows —
Git for Windows), setup pauses and shows you the exact install commands. Reply `approve` and it
runs them for you in order (stopping on the first failure), then re-checks. The engine never
downloads or runs an installer on its own — you approve each step. On macOS, setup needs a real
Python 3 already installed (the Apple CLT stub is refused) — install one first if the launcher
says so.

Once the prerequisites are in place, setup runs in two passes:

1. **Plan.** It inspects your install and prints an ordered list of every change it wants
   to make, plus a plan digest. Nothing is touched yet.
2. **Apply.** After you approve, it applies that exact plan. In one pass it:
   - creates the `chinamaxM` conda env and installs the proxy into it,
   - scaffolds your API-key file (empty, comments only),
   - generates one worker subagent per model,
   - installs the proxy as a self-restarting OS service (see below),
   - points `ANTHROPIC_BASE_URL` at the local proxy so worker requests get routed.

You get two separate yes/no questions: approve the plan, and — optionally — run live paid
probes that send one tiny real request per model to confirm the keys work. Probes spend a
few tokens, so they are off unless you opt in.

When setup finishes, **restart any open host sessions** so the new worker subagents load.

Re-running setup is safe — it only fills in what is missing.

## 3. Add your API keys

Each worker needs its provider's key. Setup creates a per-host key file with the lines
commented out; you fill in the values. The file is never committed, and the proxy is the
only thing that reads it.

- Claude Code: `~/.claude/model-keys.env`
- Codex: `~/.codex/model-keys.env`

```
DEEPSEEK_API_KEY=...
GLM_API_KEY=...
```

Only fill in the models you will actually use. `/chinamaxm:profiles` shows which keys are
present (PRESENT/MISSING — never the value).

## How the proxy stays alive (per OS)

Setup installs the proxy as a background OS service that restarts itself after a crash, so
you never babysit it. The mechanism differs by OS:

| OS | Mechanism | Survives reboot? |
|---|---|---|
| Linux | systemd **user** service (`Restart=always`); setup also enables `loginctl` linger | Yes, even headless |
| macOS | launchd LaunchAgent (`KeepAlive=true`) | Starts when you next log in |
| Windows | Windows Service via WinSW, running as your user | Yes |

On Windows, setup fetches WinSW automatically (a pinned, checksum-verified official
release) or uses one you point it at with `--winsw-exe`. The Windows path is tested but not
yet verified on a live Windows box — treat it as beta.

## Launch a task

Dispatch a task to a worker. It runs as a **named background subagent**; when it finishes,
its report is printed straight back to you.

```
/chinamaxm:task profile=deepseek <your prompt>
```

Arguments:
- `profile=<name>` — **required**. One of: `deepseek`, `mimo`, `glm`, `minimax`, `kimi`,
  `qwen`.
- `model=<any string>` — optional. Overrides the profile's default model. The string goes
  straight to the provider and is never checked, so a typo comes back as the provider's own
  error.
- `name=<worker>` — optional. Must be `chinamaxm-<profile>-<suffix>` (e.g.
  `chinamaxm-deepseek-review`). Defaults to a task-descriptive name derived from your
  prompt, like `chinamaxm-deepseek-repo-summary`.

Examples:
```
/chinamaxm:task profile=deepseek summarize the diff on this branch
/chinamaxm:task profile=glm model=glm-4-plus name=chinamaxm-glm-tests write tests for utils.py
```

## Steer and continue a worker

While the session that launched it is alive, a worker stays addressable by its name. Just
tell the host in plain language:

- Mid-run: "tell chinamaxm-deepseek-repo-summary to also cover the error paths."
- After it finishes: "ask chinamaxm-deepseek-repo-summary to now write the tests" — it
  picks up with full context.

Same experience on both hosts. (On Codex the host relays your message to the worker behind
the scenes, because Codex won't let you message a child directly — you don't have to think
about it.)

## Resume across sessions

There is none, by design. A worker lives and dies with the session that spawned it. Once
that session ends, the worker is gone — dispatch a fresh one from the new session.

## Check the install

- `/chinamaxm:doctor` — checks everything read-only and free: conda env, dependencies,
  service running, port live. Never spends tokens. If it can't even launch (no conda, no
  env), that failure *is* the diagnosis — run setup.
- `/chinamaxm:profiles` — lists each model, its default, and which keys are set.

## Upgrade

**Claude Code**
```
claude plugin update chinamaxm@chinamaxm-plugin
```

**Codex** — no per-plugin update; refresh the marketplace and re-add:
```
codex plugin marketplace upgrade
codex plugin add chinamaxm@chinamaxm-plugin
```

## Uninstall / teardown

Run `/chinamaxm:setup` and choose teardown. It removes the `ANTHROPIC_BASE_URL` flip and
the proxy service; it leaves your keys, generated agents, and Linux linger in place. Like
setup, it shows a plan and waits for your approval before touching anything.
