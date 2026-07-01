"""
osprey.ui.diff_utils
~~~~~~~~~~~~~~~~~~~~
Shared word-level inline diff HTML rendering utility.

Used by both :mod:`~osprey.ui.result_panel` and
:mod:`~osprey.ui.preview_panel` so that the diff visualization is
identical in both panels without code duplication.

Word-level (token-level) diffing is used instead of character-level
diffing to prevent spurious sub-word matches.  For example, replacing
"osprey" with "falcon" shares the character 'o'; a character-level
differ would produce the confusing ``falco~~sprey~~n`` instead of the
correct ``~~osprey~~falcon``.

Tokenisation rule: ``re.findall(r'\\w+|\\W+', text)`` splits each line
into alternating word-tokens (alphanumeric + underscore) and
non-word-tokens (spaces, punctuation, etc.).  The differ operates on
these tokens so each word is treated as an atomic unit.
"""

from __future__ import annotations

import html as _html
import re
import difflib


def inline_diff_html(
    old: str,
    new: str,
    *,
    old_color: str,
    new_color: str,
    base_color: str,
) -> str:
    """Render a word-level diff between *old* and *new* as an HTML snippet.

    - Equal token runs  → ``base_color`` span.
    - Deleted token runs → ``old_color`` + ``text-decoration:line-through``.
    - Inserted token runs → ``new_color`` span.

    Trailing newlines (``\\r\\n``) are stripped from both inputs before
    comparison so the caller does not have to normalise line endings.

    Word-level tokenisation ensures that words like "osprey" and "falcon"
    are compared atomically, yielding ``~~osprey~~falcon`` rather than
    the character-spliced ``falco~~sprey~~n``.
    """
    old_s = old.rstrip("\r\n")
    new_s = new.rstrip("\r\n")

    # Tokenise: each word or run of non-word chars becomes one atom.
    old_tokens: list[str] = re.findall(r"\w+|\W+", old_s) or [""]
    new_tokens: list[str] = re.findall(r"\w+|\W+", new_s) or [""]

    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    parts: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            parts.append(
                f'<span style="color:{base_color};">'
                f'{_html.escape("".join(old_tokens[i1:i2]))}</span>'
            )
        elif tag == "replace":
            parts.append(
                f'<span style="color:{old_color};text-decoration:line-through;">'
                f'{_html.escape("".join(old_tokens[i1:i2]))}</span>'
                f'<span style="color:{new_color};">'
                f'{_html.escape("".join(new_tokens[j1:j2]))}</span>'
            )
        elif tag == "delete":
            parts.append(
                f'<span style="color:{old_color};text-decoration:line-through;">'
                f'{_html.escape("".join(old_tokens[i1:i2]))}</span>'
            )
        elif tag == "insert":
            parts.append(
                f'<span style="color:{new_color};">'
                f'{_html.escape("".join(new_tokens[j1:j2]))}</span>'
            )
    return "".join(parts)
