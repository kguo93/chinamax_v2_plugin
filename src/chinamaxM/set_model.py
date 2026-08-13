"""``python -m chinamaxM.set_model <claude|codex> <profile> <model>`` entry point.

The dispatch conduit consumed by hosts-02 (Claude) and hosts-03 (Codex): it rewrites only
the model line of one generated artifact. The real logic lives in
:func:`chinamaxM.generate.set_model`; this module is the thin CLI wrapper.
"""

from __future__ import annotations

import sys

from .generate import GenerationError, set_model

_USAGE = "usage: python -m chinamaxM.set_model <claude|codex> <profile> <model>"


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and rewrite the model line, returning a process exit code.

    Args:
        argv: The argument list (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success, ``1`` on a generation error, ``2`` on bad usage.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] not in ("claude", "codex"):
        print(_USAGE, file=sys.stderr)
        return 2
    host, profile, model = args
    try:
        path = set_model(host, profile, model)
    except GenerationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(str(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
