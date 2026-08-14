# shellcheck shell=bash
# Shared interpreter resolution for the chinamaxM plugin shims. Sourced, never run.
#
# THE interpreter-discovery order lives ONLY here, so the launcher and the hook shims
# never drift onto different pythons. Rungs, taking the FIRST that resolves:
#
#   1. the path setup records at <claude-root>/chinamaxM/python-path  (Recorded interpreter)
#   2. $CHINAMAXM_PYTHON
#   3. ~/miniconda3/envs/chinamaxM/bin/python   (python.exe on Windows)
#   ---- chinamaxm_resolve_python stops here (hook shims: fail-open conda fallback) ----
#   4. conda run -n chinamaxM python            (validated against the env; not absolute)
#   5. ~/miniconda3/bin/python (python.exe)      + src/ on PYTHONPATH   (bootstrap rung)
#   6. system python3 (POSIX) / python (Windows) + src/ on PYTHONPATH   (bootstrap rung)
#
# Rungs 5-6 break setup's bootstrap circularity: on a fresh machine with no chinamaxM env
# every conda rung fails, so without them setup could never start. Rung 5 is what makes the
# Windows "zero-state cmd.exe bootstrap, then return to step 1" re-run work with no PATH
# change. On macOS rung 6 REFUSES the Xcode CLT stub at /usr/bin/python3 (GUI-safe check)
# and kicks back to the operator to install a real Python 3 (ADR 0009).

chinamaxm_windows() {
  [ "${OS:-}" = Windows_NT ]
}

chinamaxm_macos() {
  [ "$(uname -s 2>/dev/null)" = Darwin ]
}

chinamaxm_shell_path() {
  local value="${1:-}"
  [ -n "${value}" ] || return 0
  if chinamaxm_windows && command -v cygpath >/dev/null 2>&1; then
    cygpath -u -- "${value}"
  else
    printf '%s\n' "${value}"
  fi
}

chinamaxm_absolute() {
  local value="${1:-}"
  if [ -z "${value}" ]; then
    return 1
  fi
  if chinamaxm_windows; then
    case "${value}" in
      [A-Za-z]:[\\/]*|[\\/][\\/]*) return 0 ;;
      /*) return 0 ;;
    esac
    return 1
  fi
  [ "${value#/}" != "${value}" ]
}

# The claude-root record the hook shims and setup already share:
# <claude-root>/chinamaxM/python-path, written by setup's record-python-path step
# (setup.py _python_path_file). One file for both Hosts.
chinamaxm_recorded_python_file() {
  printf '%s\n' "${CHINAMAXM_CLAUDE_HOME:-${CLAUDE_CONFIG_DIR:-${HOME:-}/.claude}}/chinamaxM/python-path"
}

# Print conda's command path (absolute Windows Scripts\conda.exe preferred over PATH so a
# just-bootstrapped ~/miniconda3 is found), or print nothing and fail.
chinamaxm_conda_cmd() {
  local home conda_cmd
  conda_cmd="$(command -v conda || true)"
  home="$(chinamaxm_shell_path "${USERPROFILE:-${HOME:-}}")"
  if chinamaxm_windows && [ -x "${home}/miniconda3/Scripts/conda.exe" ]; then
    conda_cmd="${home}/miniconda3/Scripts/conda.exe"
  fi
  [ -n "${conda_cmd}" ] && printf '%s\n' "${conda_cmd}"
}

# Rungs 1-3: print an absolute executable python, or return 1 to signal the caller to fall
# through (launcher: conda run / bootstrap rungs; hook shims: fail-open conda run).
chinamaxm_resolve_python() {
  local recorded_file recorded
  recorded_file="$(chinamaxm_recorded_python_file)"
  if [ -f "${recorded_file}" ]; then
    recorded="$(head -n1 "${recorded_file}" 2>/dev/null | tr -d '\r\n' || true)"
    recorded="$(chinamaxm_shell_path "${recorded}")"
    if chinamaxm_absolute "${recorded}" && [ -x "${recorded}" ]; then
      printf '%s\n' "${recorded}"
      return 0
    fi
  fi

  local explicit
  explicit="$(chinamaxm_shell_path "${CHINAMAXM_PYTHON:-}")"
  if chinamaxm_absolute "${explicit}" && [ -x "${explicit}" ]; then
    printf '%s\n' "${explicit}"
    return 0
  fi

  local home
  home="$(chinamaxm_shell_path "${USERPROFILE:-${HOME:-}}")"
  if chinamaxm_windows; then
    if [ -x "${home}/miniconda3/envs/chinamaxM/python.exe" ]; then
      printf '%s\n' "${home}/miniconda3/envs/chinamaxM/python.exe"
      return 0
    fi
  else
    if [ -x "${home}/miniconda3/envs/chinamaxM/bin/python" ]; then
      printf '%s\n' "${home}/miniconda3/envs/chinamaxM/bin/python"
      return 0
    fi
  fi

  return 1
}

# macOS kick-back: never exec the Xcode CLT stub (ADR 0009). Writes to stderr itself.
chinamaxm_report_no_python() {
  cat >&2 <<'EOF'
chinamaxM: no usable Python 3 found — cannot start.

macOS does not ship a real Python 3: /usr/bin/python3 is an Xcode Command Line
Tools stub, not an interpreter. Install a real Python 3 yourself, then re-run
/chinamaxM:setup. Any one of these:

  * Command Line Tools:  xcode-select --install
  * Homebrew:            brew install python3
  * python.org:          the macOS universal2 installer

chinamaxM will not proceed until a real python3 resolves on PATH.
EOF
}

# Resolve the interpreter and exec `python -m <module> "$@"`. Requires
# CHINAMAXM_SCRIPT_DIR to be the absolute path to this scripts/ directory.
chinamaxm_exec() {
  local module="$1"
  shift
  local plugin_root py home
  plugin_root="$(dirname "${CHINAMAXM_SCRIPT_DIR}")"
  home="$(chinamaxm_shell_path "${USERPROFILE:-${HOME:-}}")"

  if py="$(chinamaxm_resolve_python)"; then
    exec "${py}" -m "${module}" "$@"
  fi

  local conda_cmd
  conda_cmd="$(chinamaxm_conda_cmd || true)"
  if [ -n "${conda_cmd}" ] && "${conda_cmd}" run -n chinamaxM python -c '' >/dev/null 2>&1; then
    # --no-capture-output connects stdio directly (plain `conda run` swallows stdin).
    exec "${conda_cmd}" run --no-capture-output -n chinamaxM python -m "${module}" "$@"
  fi

  local path_sep python_cmd bootstrap_python py_path
  if chinamaxm_windows; then
    path_sep=';'
    python_cmd=python
    bootstrap_python="${home}/miniconda3/python.exe"
  else
    path_sep=':'
    python_cmd=python3
    bootstrap_python="${home}/miniconda3/bin/python"
  fi
  if [ -x "${bootstrap_python}" ]; then
    exec env "PYTHONPATH=${plugin_root}/src${PYTHONPATH:+${path_sep}${PYTHONPATH}}" "${bootstrap_python}" -m "${module}" "$@"
  fi
  # macOS: detect GUI-safely (command -v + xcode-select -p; never EXECUTE python3 to probe —
  # running the CLT stub can pop the installer). A python3 resolving anywhere other than
  # /usr/bin/python3 is accepted; the stub is accepted only with the CLT present.
  if chinamaxm_macos; then
    py_path="$(command -v "${python_cmd}" 2>/dev/null || true)"
    if [ -z "${py_path}" ] || { [ "${py_path}" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; }; then
      chinamaxm_report_no_python
      exit 1
    fi
  fi
  exec env "PYTHONPATH=${plugin_root}/src${PYTHONPATH:+${path_sep}${PYTHONPATH}}" "${python_cmd}" -m "${module}" "$@"
}
