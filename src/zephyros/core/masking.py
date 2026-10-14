"""Masking secrets (EXG-49): the log, the evidence reports and the pull request bodies carry no
secret — neither one the hub knows of, nor anything shaped like a token.

The shapes are the ones an attacker can plant (ATK-84): provider keys, forge tokens, hub tokens,
cloud keys, private-key headers. They are masked wherever they stand, with one exception: words
end with "sk-" ("risk-", "disk-", "task-"), so a provider key counts only where a token starts.
A letter or a digit just before it means a word goes on — `risk-management-module` holds no
key — unless it ends an escape that test output shows before a value: `\\n`, `\\t`, `\\r`,
`%3D`, or an ANSI colour code. The known secrets — the credential file, the notification
secret — are replaced by value wherever they stand, inside a word too, longest first.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

MASK = "[masked]"

SHAPES = re.compile(
    r"zph_[a-z]_[A-Za-z0-9_-]{20,}"  # tokens of the hub
    r"|(?P<provider>sk-[A-Za-z0-9_-]{16,})"  # common provider keys
    r"|gh[pousr]_[A-Za-z0-9]{20,}"  # forge tokens
    r"|AKIA[0-9A-Z]{16}"  # cloud access keys
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"  # private-key headers, whatever the body
)

_ESCAPE_BEFORE = re.compile(
    r"(?:\\[nrt]"  # a control character, as pytest and a traceback print it
    r"|%[0-9A-Fa-f]{2}"  # percent-encoding
    r"|(?:\x1b|\\x1b|\\033|\\e|\\u001b)\[[0-9;]*m"  # an ANSI colour code, sent or written
    r")$"
)
_LOOK_BACK = 16
"""Enough characters to hold the longest escape above."""


def _replace(match: re.Match[str]) -> str:
    if match.group("provider") is None:
        return MASK
    start, text = match.start(), match.string
    before = text[start - 1] if start else ""
    if not (before.isascii() and before.isalnum()):
        return MASK
    if _ESCAPE_BEFORE.search(text[max(0, start - _LOOK_BACK) : start]):
        return MASK
    return match.group(0)  # the end of a word, as in "risk-management-module"


def mask_text(text: str, secrets: Iterable[str] = ()) -> str:
    """`text` with every known secret and every token-shaped string replaced."""
    for secret in sorted({s for s in secrets if len(s) >= 8}, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return SHAPES.sub(_replace, text)


def mask_value(value: object, secrets: Iterable[str] = ()) -> object:
    """The same, over a structure: every string inside, keys included."""
    if isinstance(value, str):
        return mask_text(value, secrets)
    if isinstance(value, Mapping):
        return {
            mask_text(str(key), secrets): mask_value(item, secrets) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [mask_value(item, secrets) for item in value]
    return value
