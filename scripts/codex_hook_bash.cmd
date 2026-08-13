@echo off
setlocal
rem Windows Git-Bash entry for the Codex hooks (old-repo precedent): every codex-hooks.json
rem commandWindows enters Git Bash and execs "$PLUGIN_ROOT/scripts/<hook-basename>" so the
rem POSIX shim runs unchanged. The hook basename arrives as %~1.
rem
rem Resolve Git Bash from the default Git for Windows install roots FIRST (each root read
rem via a quoted `set`, and SKIPPED when its var is unset), then PATH as a fallback —
rem root-first keeps a stray WSL bash on PATH from shadowing the real Git Bash. Block-free
rem `if` lines keep the literal (x86) parens out of every parenthesized construct, a
rem cmd.exe parse hazard. Ambient CHINAMAXM_BASH is cleared so it cannot silently steer.
set "CHINAMAXM_BASH="
set "R=%ProgramW6432%"
if not defined CHINAMAXM_BASH if defined R if exist "%R%\Git\bin\bash.exe" set "CHINAMAXM_BASH=%R%\Git\bin\bash.exe"
if not defined CHINAMAXM_BASH if defined R if exist "%R%\Git\usr\bin\bash.exe" set "CHINAMAXM_BASH=%R%\Git\usr\bin\bash.exe"
set "R=%ProgramFiles%"
if not defined CHINAMAXM_BASH if defined R if exist "%R%\Git\bin\bash.exe" set "CHINAMAXM_BASH=%R%\Git\bin\bash.exe"
if not defined CHINAMAXM_BASH if defined R if exist "%R%\Git\usr\bin\bash.exe" set "CHINAMAXM_BASH=%R%\Git\usr\bin\bash.exe"
set "R=%ProgramFiles(x86)%"
if not defined CHINAMAXM_BASH if defined R if exist "%R%\Git\bin\bash.exe" set "CHINAMAXM_BASH=%R%\Git\bin\bash.exe"
if not defined CHINAMAXM_BASH if defined R if exist "%R%\Git\usr\bin\bash.exe" set "CHINAMAXM_BASH=%R%\Git\usr\bin\bash.exe"
set "R=%LOCALAPPDATA%"
if not defined CHINAMAXM_BASH if defined R if exist "%R%\Programs\Git\bin\bash.exe" set "CHINAMAXM_BASH=%R%\Programs\Git\bin\bash.exe"
if not defined CHINAMAXM_BASH if defined R if exist "%R%\Programs\Git\usr\bin\bash.exe" set "CHINAMAXM_BASH=%R%\Programs\Git\usr\bin\bash.exe"
if not defined CHINAMAXM_BASH where bash >nul 2>nul && set "CHINAMAXM_BASH=bash"
if not defined CHINAMAXM_BASH echo chinamaxM: Git Bash not found in the default Git for Windows install roots or on PATH; install Git for Windows from https://git-scm.com/download/win 1>&2
if not defined CHINAMAXM_BASH exit /b 127
"%CHINAMAXM_BASH%" -lc 'root="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"; cp=$(command -v cygpath); if [ -n "$cp" ]; then root=$(cygpath -u -- "$root"); fi; exec "$root/scripts/%~1"'
