# Self-healing proxy supervision on all three OSes

The Proxy must outlive crashes without operator attention on Linux, macOS, and native
Windows; the SessionStart hook is warn-only (supervision is the OS's job, not the
plugin's):

- **Linux**: systemd user unit — `Restart=always`, `WantedBy=default.target`.
- **macOS**: launchd LaunchAgent — `KeepAlive=true`.
- **Windows**: a real Windows Service via **WinSW** wrapping the conda env's
  `python.exe -m chinamaxM.proxy`, with failure-recovery=restart. Admin install is
  accepted. WinSW over the alternatives: single self-contained exe + declarative XML,
  actively maintained, native recovery config and log rotation. **Rejected**: NSSM
  (upstream dead since 2017, UAC/AppData quirks) and pywin32 native services
  (pythonservice.exe + conda pathing is fragile and would put a Windows-only code path
  inside the proxy itself).

All units are installed/updated/started by `/setup` under consent; teardown removes them.
`/doctor` verifies unit presence, enablement, running state, and a live port (FAIL
level). The conda env (`chinamaxM`, Python 3.12, aiohttp + pinned litellm) is a gated
prerequisite exactly like the old doctor's miniconda Phase A machinery, whose PATTERN is
reused (the old repo is a separate checkout — no cross-repo code import).

**Amended 2026-08-13 (operator grilling: service account, reboot scope, identifier
scope).**

- **WinSW service account = the installing user, never LocalSystem.** The default
  LocalSystem account resolves a different profile and cannot see the user-scoped
  Overlay and Key files (CONTEXT.md "Overlay", "Key file"); "Admin install is accepted"
  means elevation for the install itself, not a system service identity. The password
  travels in-memory only, applied via `sc.exe config … password=`, never serialized or
  logged; collecting it is Setup's consent-time concern.
- **Reboot survival on Linux is real, via linger**: `WantedBy=default.target` starts at
  LOGIN; Setup's consented apply therefore runs `loginctl enable-linger <user>`
  (idempotent) so headless reboot survival holds — cross-ref ADR 0005 (amended
  2026-08-13). macOS LaunchAgents remain login-scoped; that per-OS scope is stated
  honestly in the ops docs.
- **Identifier scope**: unit/plist/XML file names, `RunAtLoad=true`, and install paths
  are deliberately PLAN-level decisions (ops-01), not ADR policy; this ADR pins
  behavior (restart policies, service account, linger) only.

**Amended 2026-08-14 (setup auto-acquires WinSW; reverses "operator-supplied only").** The
v0.1 decision — recorded in `SupervisionConfig.winsw_exe_path` ("The operator-supplied WinSW
executable (Windows only) … Never auto-downloaded (v0.1)") and in the Setup docs ("setup does
NOT yet provide a way to supply a WinSW executable, so on Windows the service step FAILS") —
is **reversed**: `/setup` now ACQUIRES WinSW automatically. The Windows service step resolves
the WinSW exe in this order (idempotent, fails CLOSED): (1) an operator `--winsw-exe <path>`
override (offline/custom hosts); (2) an exe already present at
`<log_dir>/service/chinamaxM-service.exe` (a prior install — never re-downloaded); (3)
otherwise download the PINNED official release (winsw v2.12.0 asset `WinSW-x64.exe`) and
verify its SHA-256 against a hardcoded PUBLIC checksum — any mismatch or download failure
aborts the service step with NO exe placed (an unverified binary is never installed; per the
pinned apply order this also aborts the env flip + probes, while re-diagnose + report still
run). An optional `--winsw-service-password-file <path>` supplies the service-account
password (read once from the file, applied via `sc.exe config … password=`, never logged,
never serialized into the XML — unchanged from the 2026-08-13 amendment). Auto-download needs
network at apply; `--winsw-exe` is the offline escape hatch. This Windows path is
mocked-tested only — it is NOT live-verified on a real Windows host in this build.

**Amended 2026-08-14 (setup auto-bootstraps Miniconda on all three OSes; reverses the
gated-prerequisite stance).** The original decision above — "The conda env (`chinamaxM`,
Python 3.12, aiohttp + pinned litellm) is a gated prerequisite exactly like the old doctor's
miniconda Phase A machinery, whose PATTERN is reused (the old repo is a separate checkout — no
cross-repo code import)." — is **reversed** for the conda-absent case: `/setup` now INSTALLS
Miniconda when `conda` is absent instead of gate-failing with "install it yourself, then
re-run". Per-OS mechanics (ported verbatim from the old plugin's `doctor`): POSIX downloads the
official installer with `curl` and runs `bash …/.chinamaxM-miniconda.sh -b -u -p ~/miniconda3`
(`-b -u` reuses an existing `~/miniconda3`, so a re-run is idempotent — the same `~/miniconda3`
probe that resolves a pre-existing anaconda/miniforge conda); Windows runs the `.exe`
JustMe/silent installer (`/InstallationType=JustMe /RegisterPython=0 /AddToPath=0 /S
/D=%USERPROFILE%\miniconda3`, x86_64 asset). Both then run `conda init` (`bash` on Linux; `bash
zsh` on macOS; `cmd.exe powershell bash` on Windows), which EDITS the operator's shell startup
files — disclosed in the plan step title and the setup docs. The installer is
`Miniconda3-latest-*` with **NO version pin and NO checksum** — an explicit inheritance of the
old plugin's accepted tradeoff, and a deliberate divergence from this repo's pinned + SHA-256
WinSW acquisition (see the amendment above). The bootstrap runs as a normal digest-bound,
consent-gated apply step (its URL + argv commands ride the descriptor, so the digest binds the
exact download), and it resolves conda by absolute path so the same apply pass can create the
env and pip-install into it. A POSIX machine on a CPU architecture absent from the arch map
falls back to an advice-only gate (no URL); Windows never gate-fails on arch (always the x86_64
asset). Like the WinSW path, the Windows bootstrap is mocked-tested only — NOT live-verified on
a real Windows host in this build (`start /wait` may not propagate the installer's exit code to
`%ERRORLEVEL%`).

**Amended 2026-08-14 (setup DETECTS prerequisites and EMITS agent-run Rectification rows;
reverses the engine-self-install stance immediately above).** The amendment just above —
"`/setup` now INSTALLS Miniconda when `conda` is absent instead of gate-failing … The bootstrap
runs as a normal digest-bound, consent-gated apply step (its URL + argv commands ride the
descriptor …), and it resolves conda by absolute path so the same apply pass can create the env
and pip-install into it." — is **reversed**. That model was architecturally wrong: the setup
ENGINE runs under an ambient Python, so having the engine itself download and run the Miniconda
installer does NOT work on a bare Windows/macOS box with no Python, and it never installed Git
Bash (which the Codex hooks need on Windows). The corrected model — copied faithfully from the
original plugin's `doctor` (a separate checkout; no cross-repo code import) — is: setup's Python
only **DETECTS** the Platform Prerequisites (`bash` on Linux/macOS; `git`/`bash`/`cygpath` from
Git for Windows on Windows; Miniconda on all three) and **EMITS** per-tool agent-run
**Rectification rows** (`{name, summary, commands, run_policy, shell, install_location}`, plus
`missing_tools` on the Git row). When any Prerequisite is missing, `--plan-only` PAUSES (Phase A):
it surfaces the `prerequisite_fixes` rows and emits NO mutating plan and NO plan digest. The HOST
agent — via the `/chinamaxM:setup` command / `chinamaxM-setup` skill protocol — runs those rows
IN ORDER after the operator types "approve" (dispatched by `run_policy`: `agent` = run;
`privileged` = `sudo -n true` gate then run, else hand to the operator; `operator` = advice-only),
each through the row's `shell` (`cmd` via `cmd /c`; `powershell`/`native` natively — never Git
Bash; `bash` via bash), stop-on-first-failure; then the launcher is re-run once, and only with
every Prerequisite present is the normal mutating plan emitted. The **engine NEVER downloads or
runs an installer.** Consequences: **no ambient-Python dependency** — a bare Windows box uses the
zero-state cmd.exe fallback (`winget install --id Git.Git …`, `curl.exe … Miniconda3-latest-
Windows-x86_64.exe`, `start /wait … /InstallationType=JustMe /S /D=%USERPROFILE%\miniconda3`,
`conda.exe init cmd.exe powershell bash`) when the launcher itself cannot start. **Git for Windows
is now bootstrapped** (its bash/cygpath are what the Codex `commandWindows` hook shims need). The
Windows `bash`/`cygpath` prerequisite is detected in the standard Git-for-Windows install roots
ONLY — never PATH — because Git for Windows leaves them off PATH by default, so a bash that IS on
PATH is almost certainly WSL's `System32\bash.exe`, not Git Bash; the `codex_hook_bash.cmd` shim
resolves Git Bash the SAME tree-only way (no `where bash` PATH fallback), so the bash the doctor
green-lights is exactly the one the hooks run.
Miniconda is still `Miniconda3-latest-*` with **NO version pin and NO checksum** (the old plugin's
accepted tradeoff, inherited verbatim); an unsupported CPU architecture yields an advice-only
miniconda row (no URL). The Windows rows remain **mocked-only** — NOT live-verified on a real
Windows host in this build. In the same spirit of Host parity, the warn-only SessionStart check is
now **host-aware**: the Codex Host has no `settings.json` flip, so its SessionStart warns iff a
generated `model_providers.chinamaxM-<profile>` entry is present in `~/.codex/config.toml` AND the
Registry Proxy port is dead, pointing the operator at the `chinamaxM-doctor` skill (the Claude
`ANTHROPIC_BASE_URL`-flip path is unchanged). Both remain strictly fail-open.

**Amended 2026-08-14 (the launcher is a shim with pinned interpreter rungs; macOS zero-state is
operator kick-back).** The amendment above concluded "**no ambient-Python dependency**", but that
held only for the bare-Windows zero-state cmd.exe fallback: the launcher ITSELF was still
`python3 -m chinamaxM.setup` run under an ambient interpreter — broken on a bare macOS (only the
Xcode Command Line Tools stub at `/usr/bin/python3` exists, which errors or pops a GUI installer)
and on the Windows re-run (`python3` never exists there; conda ships `python.exe`, installed
`/AddToPath=0`). That residual ambient-`python3` launcher is **reversed**.

The launcher is now the `scripts/chinamaxM` shim sourcing `scripts/_interpreter.sh`, THE single
home of interpreter discovery (copied from the original plugin's `_interpreter.sh` pattern — a
separate checkout, no cross-repo code import), with the pinned rungs, first that resolves winning:
recorded `<claude-root>/chinamaxM/python-path` → `$CHINAMAXM_PYTHON` → `~/miniconda3/envs/chinamaxM`
python → validated `conda run -n chinamaxM` (`--no-capture-output`) → base `~/miniconda3` python +
`src/` on `PYTHONPATH` → ambient `python3` (POSIX) / `python` (Windows) + `src/` on `PYTHONPATH`.
The base-`~/miniconda3` rung is what completes the Windows zero-state re-run with no PATH change.
Making the recorded path win **reverses** the hook shims' previous env-var-first order (they
resolved `$CHINAMAXM_PYTHON` before the recorded path) — deliberate, because one shared order for
the launcher and all three hook shims beats three private ones.

All command surfaces launch through the shim now — `/setup`, `/doctor`, `/profiles`, and `/task`'s
`set_model` rewrite (the last was latently broken as a bare `python -m chinamaxM.set_model` with no
`PYTHONPATH` and no env; `set_model` itself was retired 2026-08-19 — ADR 0004 as amended — so the
launcher now maps `{setup|doctor}` only). The three hook shims (`session_start_hook`, `worker_contract_hook`,
`codex_worker_contract_hook`) share rungs 1–3 plus the `conda run` helper and keep their fail-open
`exit 0` contract and `conda run --no-capture-output` stdin semantics; hooks NEVER use the
launcher's base-miniconda or ambient bootstrap rungs (5–6).

macOS zero-state is **operator kick-back**, deliberately: the ambient rung refuses
`/usr/bin/python3` unless `xcode-select -p` succeeds (GUI-safe — the CLT stub is never EXECUTED as a
probe, since running it can pop the installer), prints install-it-yourself guidance (CLT / Homebrew
/ python.org), and exits 1. No macOS zero-state install block exists: the engine-never-installs
consent model extends to the launcher. The Windows zero-state cmd.exe block is unchanged. The POSIX
miniconda Rectification row's first command is now curl-or-wget (minimal-image Linux without curl).

**Amended 2026-08-18 (SessionStart resolves the Host by the shared ladder — cross-ref ADR 0005
as amended 2026-08-18).** The 2026-08-14 amendment above made SessionStart host-aware via env
sniffing that checked Claude evidence FIRST and defaulted to claude when nothing matched.
**Reversed in ordering and fallback**: the hook now resolves through the shared Host-resolution
ladder (flag-less: the `CHINAMAXM_HOST` marker → Codex plugin evidence → Claude plugin
evidence), Codex-first because Codex exposes Claude-compatible env aliases — the original
plugin's proven ordering. Being warn-only, the hook stays fail-open: when nothing resolves it
exits silently instead of assuming claude. Both Hosts' warn conditions themselves are
unchanged.
