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
