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
