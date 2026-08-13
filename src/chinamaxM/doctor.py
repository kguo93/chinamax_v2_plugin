"""Static diagnosis helpers for the chinamaxM source tree.

The only surface here today is the no-chat-completions scan (ADR 0002): the chat-completions
dialect is banned in product code, so this proves structurally that no ``chinamaxM`` source
references a chat-completions URL. litellm's own site-packages and this repo's docs
legitimately contain the string and are out of scope — the caller passes ONLY the
``src/chinamaxM`` tree. hosts-04's ``/doctor`` imports :func:`find_chat_completions_references`
for its own check (one scan, two consumers).
"""

from __future__ import annotations

from pathlib import Path

#: The banned URL fragment: a chat-completions WIRE path (never a litellm symbol name,
#: which uses the ``chat_completion`` underscore form). Assembled from parts so this
#: scanner's own source never trips its own scan.
_CHAT_COMPLETIONS_URL = "chat" + "/" + "completions"


def find_chat_completions_references(tree_root: Path) -> list[tuple[Path, int, str]]:
    """Return every chat-completions URL reference under a source tree.

    Scans the ``*.py`` files beneath ``tree_root`` for the banned chat-completions WIRE URL
    fragment. The underscore form (litellm's ``..._to_chat_completion_request`` symbols) is
    NOT matched — only the slashed URL path is a violation.

    Args:
        tree_root: The root of the tree to scan (the caller scopes this to ``src/chinamaxM``,
            excluding litellm site-packages and repo docs).

    Returns:
        A list of ``(path, line_number, line_text)`` for each offending line, in file and
        line order (empty when the tree is clean).
    """
    findings: list[tuple[Path, int, str]] = []
    for path in sorted(tree_root.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable file is not a reference
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if _CHAT_COMPLETIONS_URL in line:
                findings.append((path, number, line.strip()))
    return findings
