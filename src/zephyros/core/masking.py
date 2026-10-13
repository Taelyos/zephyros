"""Masking secrets (EXG-49): the log, the evidence reports and the pull request bodies carry no
secret — neither one the hub knows of, nor anything shaped like a token.

The shapes are the ones an attacker can plant (ATK-84): provider keys, forge tokens, hub tokens,
cloud keys, private-key headers. A shape counts only where a token starts: a letter or a digit
just before it means a word goes on, and `risk-management-module` holds no key although it holds
`sk-management-module`. A separator ends a word, so a canary glued to `-`, `_`, `/` or `=` is
still masked. The known secrets — the credential file, the notification secret — are replaced
by value wherever they stand, inside a word too, longest first.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

MASK = "[masked]"

SHAPES = re.compile(
    r"(?<![A-Za-z0-9])(?:"  # where a token starts, never inside a word
    r"zph_[a-z]_[A-Za-z0-9_-]{20,}"  # tokens of the hub
    r"|sk-[A-Za-z0-9_-]{16,}"  # common provider keys
    r"|gh[pousr]_[A-Za-z0-9]{20,}"  # forge tokens
    r"|AKIA[0-9A-Z]{16}"  # cloud access keys
    r")"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"  # private-key headers, whatever the body
)


def mask_text(text: str, secrets: Iterable[str] = ()) -> str:
    """`text` with every known secret and every token-shaped string replaced."""
    for secret in sorted({s for s in secrets if len(s) >= 8}, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return SHAPES.sub(MASK, text)


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
