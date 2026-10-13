"""The writing rules of a contract: the terms that make a requirement unverifiable (EXG-29).

The list is the one of the requirements writing guide, section 3.3 — the annex "Forbidden
terms" of the contract points there. Six families: qualities without a measure, optimisations
without a threshold, escape hatches, open lists, the "and/or" combination, and soft modals.
The hub flags — never refuses — each requirement or acceptance criterion that contains one;
the Owner decides what to do with the flag.
"""

from __future__ import annotations

import re

# The letters a term may be glued to without being a word of its own, French included.
_BOUNDARY = "0-9a-zàâäçéèêëîïôöùûüÿœæ"

FORBIDDEN_TERMS: tuple[str, ...] = (
    # Quality without a measure.
    "adapté",
    "adequate",
    "adapted",
    "appropriate",
    "approprié",
    "bien",
    "convivial",
    "easy",
    "ergonomic",
    "ergonomique",
    "facile",
    "fast",
    "flexible",
    "good",
    "intuitive",
    "intuitif",
    "modern",
    "moderne",
    "performant",
    "quickly",
    "rapide",
    "rapidement",
    "robust",
    "robuste",
    "simple",
    "sufficient",
    "suffisant",
    "user-friendly",
    # Optimisation without a threshold.
    "maximize",
    "maximiser",
    "minimize",
    "minimiser",
    "optimize",
    "optimiser",
    # Escape hatches.
    "as far as possible",
    "as needed",
    "au besoin",
    "dans la mesure du possible",
    "de préférence",
    "if possible",
    "le cas échéant",
    "preferably",
    "si possible",
    "where applicable",
    # Open lists.
    "among others",
    "e.g.",
    "etc.",
    "including",
    "notamment",
    "par exemple",
    "such as",
    "tel que",
    # Combination.
    "and/or",
    "et/ou",
    # Soft modals.
    "can",
    "could",
    "devrait",
    "may",
    "ne doit pas",
    "shall not",
    "peut",
    "pourrait",
    "should",
)


def _pattern(term: str) -> str:
    body = r"\s+".join(re.escape(word) for word in term.split(" "))
    return rf"(?<![{_BOUNDARY}]){body}(?![{_BOUNDARY}])"


_MATCHERS = tuple((term, re.compile(_pattern(term))) for term in FORBIDDEN_TERMS)


def forbidden_terms_in(text: str) -> tuple[str, ...]:
    """The terms of the list `text` contains, in the order of the list. Whole words only:
    "candidate" holds no "can", "échanges" no "easy"."""
    lowered = text.lower()
    return tuple(term for term, matcher in _MATCHERS if matcher.search(lowered))
