"""The writing rules: forbidden terms flagged on requirements and criteria (EXG-29)."""

from __future__ import annotations

from zephyros.core.writing import FORBIDDEN_TERMS, forbidden_terms_in


def test_a_clean_requirement_holds_no_term() -> None:
    assert forbidden_terms_in("The system shall keep the state of each task in the hub.") == ()


def test_each_family_of_the_guide_is_matched() -> None:
    text = "It should be simple, and/or fast; if possible optimize later, e.g. the screen."
    assert forbidden_terms_in(text) == (
        "fast",
        "simple",
        "optimize",
        "if possible",
        "e.g.",
        "and/or",
        "should",
    )


def test_whole_words_only() -> None:
    assert forbidden_terms_in("A candidate cannot run it; maybe later; the myriad cases.") == ()


def test_a_multi_word_term_matches_any_spacing() -> None:
    assert forbidden_terms_in("Do it if   possible.") == ("if possible",)


def test_the_soft_modal_shall_not_is_matched() -> None:
    assert forbidden_terms_in("The agent shall not keep the token.") == ("shall not",)


GUIDE: dict[str, tuple[str, ...]] = {
    # The requirements writing guide, section 3.3, verbatim — the whole table, so the list
    # cannot drift in silence (the verifier of Z3 found three terms missing from a sample).
    "quality": (
        "bien",
        "rapide",
        "rapidement",
        "facile",
        "simple",
        "intuitif",
        "convivial",
        "ergonomique",
        "adapté",
        "approprié",
        "suffisant",
        "robuste",
        "flexible",
        "performant",
        "moderne",
        "good",
        "fast",
        "quickly",
        "easy",
        "simple",
        "intuitive",
        "user-friendly",
        "ergonomic",
        "adapted",
        "appropriate",
        "adequate",
        "sufficient",
        "robust",
        "flexible",
        "modern",
    ),
    "optimisation": ("optimiser", "minimiser", "maximiser", "optimize", "minimize", "maximize"),
    "escape": (
        "si possible",
        "dans la mesure du possible",
        "au besoin",
        "le cas échéant",
        "de préférence",
        "if possible",
        "as far as possible",
        "as needed",
        "where applicable",
        "preferably",
    ),
    "open": (
        "etc.",
        "notamment",
        "par exemple",
        "tel que",
        "etc.",
        "including",
        "such as",
        "e.g.",
        "among others",
    ),
    "combination": ("et/ou", "and/or"),
    "modal": (
        "devrait",
        "pourrait",
        "peut",
        "ne doit pas",
        "should",
        "could",
        "may",
        "can",
        "shall not",
    ),
}


def test_the_list_matches_the_guide() -> None:
    assert len(FORBIDDEN_TERMS) == len(set(FORBIDDEN_TERMS))
    expected = {term for terms in GUIDE.values() for term in terms}
    assert set(FORBIDDEN_TERMS) == expected
