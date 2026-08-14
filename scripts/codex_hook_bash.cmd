@echo off
setlocal
rem Windows Git-Bash entry for the Codex hooks (old-repo precedent): every codex-hooks.json
rem commandWindows enters Git Bash and execs "$PLUGIN_ROOT/scripts/<hook-basename>" so the
rem POSIX shim runs unchanged. The hook basename arrives as %~1.
rem
rem Resolve Git Bash from the default Git for Windows install roots ONLY (each root read via a
rem quoted `set`, and SKIPPED when its var is unset). There is deliberately NO `where bash` PATH
rem fallback: you cannot assume a PATH bash is Git Bash — Windows 10+ ships WSL's
rem `System32\bash.exe` on PATH, and running these hooks under WSL bash breaks them. This is the
rem SAME resolution the doctor's Windows bash prerequisite check uses (Git-for-Windows roots
rem only), so the bash the doctor green-lights is exactly the one this shim runs. Block-free
rem `if` lines keep the literal (x86) parens out of every parenthesized construct, a cmd.exe
rem parse hazard. Ambient CHINAMAXM_BASH is cleared so it cannot silently steer.
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
if not defined CHINAMAXM_BASH echo chinamaxM: Git Bash not found in the default Git for Windows install roots; install Git for Windows from https://git-scm.com/download/win (a bash on PATH is not used - it may be WSL, not Git Bash) 1>&2
if not defined CHINAMAXM_BASH exit /b 127
"%CHINAMAXM_BASH%" -lc 'root="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"; cp=$(command -v cygpath); if [ -n "$cp" ]; then root=$(cygpath -u -- "$root"); fi; exec "$root/scripts/%~1"'
