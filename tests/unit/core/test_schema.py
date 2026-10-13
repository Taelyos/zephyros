"""The declarative schema: every kind of specification, in both directions."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from zephyros.core.schema import (
    Choice,
    Field,
    Flag,
    Integer,
    Number,
    Problem,
    Rule,
    Schema,
    Table,
    Text,
)

DIGEST = "ghcr.io/example/app@sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("kind", "good", "bad"),
    [
        ("name", "zephyros-core", "Zephyros"),
        ("name", "a1_b", "1abc"),
        ("version", "1.0.0", "1.0"),
        ("version", "0.12.3", "01.0.0"),
        ("image-digest", DIGEST, "ghcr.io/example/app:latest"),
        ("image-digest", "app@sha256:" + "0" * 64, "app@sha256:" + "0" * 63),
        ("absolute-path", "/var/lib/zephyros", "relative/path"),
        ("absolute-path", "/a/b", "/a/../b"),
        ("ipv4-network", "10.200.0.0/16", "10.200.0.1/16"),
        ("ipv4-network", "192.168.0.0/24", "fd00::/8"),
        ("ip-address", "127.0.0.1", "localhost"),
        ("ip-address", "::1", "999.1.1.1"),
        ("http-url", "https://api.example.com/v1", "ftp://example.com"),
        (
            "http-url",
            "http://127.0.0.1:9000",
            "https://user:pw@example.com",  # pragma: allowlist secret
        ),  # pragma: allowlist secret
    ],
)
def test_text_kinds(kind: str, good: str, bad: str) -> None:
    spec = Text(kind)
    assert spec.check(good, "k") == []
    problems = spec.check(bad, "k")
    assert [p.key for p in problems] == ["k"]
    assert problems[0].message


def test_text_kinds_reject_remaining_forms() -> None:
    assert Text("absolute-path").check("/a\x00b", "k")
    assert Text("http-url").check("https://example.com/?q=1", "k")
    assert Text("http-url").check("https://example.com/#f", "k")
    assert Text("http-url").check("http://[::1", "k")
    assert Text("http-url").check("http:///path", "k")


def test_text_length_and_type() -> None:
    spec = Text(min_length=2, max_length=4)
    assert spec.check("abc", "k") == []
    assert "at least 2" in spec.check("a", "k")[0].message
    assert "at most 4" in spec.check("abcde", "k")[0].message
    assert "string" in spec.check(3, "k")[0].message
    with pytest.raises(ValueError, match="unknown text kind"):
        Text("nope")


@pytest.mark.parametrize("value", [True, 1.5, "3", None, [3]])
def test_integer_rejects_other_types(value: object) -> None:
    assert Integer(0, 10).check(value, "k")


def test_integer_bounds() -> None:
    spec = Integer(1, 3)
    assert [spec.check(v, "k") for v in (1, 2, 3)] == [[], [], []]
    assert spec.check(0, "k")
    assert spec.check(4, "k")


def test_number_accepts_integers_and_floats_but_not_booleans() -> None:
    spec = Number(0.5, 2.0)
    assert spec.check(1, "k") == []
    assert spec.check(1.5, "k") == []
    assert spec.check(0.4, "k")
    assert spec.check(2.1, "k")
    assert spec.check(True, "k")
    assert spec.check("1", "k")


def test_flag_and_choice() -> None:
    assert Flag().check(True, "k") == []
    assert Flag().check(1, "k")
    choice = Choice(("a", "b"))
    assert choice.check("a", "k") == []
    assert "one of: a, b" in choice.check("c", "k")[0].message
    assert choice.check(1, "k")


def _table() -> Table:
    return Table(
        (
            Field("name", Text(min_length=1), "A name", required=True),
            Field("size", Integer(1, 9), "A size", default=3),
            Field("inner", Table((Field("flag", Flag(), "A flag", default=False),)), "Inner"),
        )
    )


def test_table_reports_unknown_missing_and_nested_keys() -> None:
    problems = _table().check({"size": 12, "inner": {"flag": 1, "other": 2}, "extra": 1}, "")
    assert {p.key for p in problems} == {"extra", "name", "size", "inner.flag", "inner.other"}
    messages = {p.key: p.message for p in problems}
    assert messages["extra"] == "unknown key"
    assert messages["name"] == "is required"


def test_table_must_be_a_table() -> None:
    assert _table().check([1], "section")[0].key == "section"
    assert "array" in _table().check([1], "section")[0].message
    assert "table" in Text().check({"a": 1}, "k")[0].message
    assert "number" in Text().check(1.5, "k")[0].message


def test_table_fills_defaults_including_missing_subtables() -> None:
    filled = _table().with_defaults({"name": "x"})
    assert filled == {"name": "x", "size": 3, "inner": {"flag": False}}
    assert _table().with_defaults({"name": "x", "inner": {"flag": True}, "size": 5}) == {
        "name": "x",
        "size": 5,
        "inner": {"flag": True},
    }


def test_a_table_refuses_duplicate_names_and_a_required_default() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        Table((Field("a", Flag(), ""), Field("a", Flag(), "")))
    with pytest.raises(ValueError, match="no default"):
        Field("a", Flag(), "", required=True, default=True)


def _rule(data: Mapping[str, object]) -> list[Problem]:
    return [Problem("b", "must be bigger than a")] if data["a"] >= data["b"] else []  # type: ignore[operator]  # reason: the test data holds integers


def test_schema_runs_rules_only_on_sound_structure() -> None:
    schema = Schema(
        identifier="urn:test",
        version="1.0.0",
        title="Test",
        root=Table(
            (Field("a", Integer(0, 9), "a", default=5), Field("b", Integer(0, 9), "b", default=1))
        ),
        rules=(Rule("b is bigger than a", _rule),),
    )
    assert schema.validate({}) == [Problem("b", "must be bigger than a")]
    assert schema.validate({"a": 1, "b": 2}) == []
    assert [p.key for p in schema.validate({"a": 99})] == ["a"]
    assert schema.validate([1])[0].key == ""
    assert str(Problem("", "x")) == "(document): x"
    assert str(Problem("a.b", "x")) == "a.b: x"
    exported = schema.json_schema()
    assert exported["x-zephyros-schema-version"] == "1.0.0"
    assert exported["x-zephyros-rules"] == ["b is bigger than a"]
    assert exported["additionalProperties"] is False


def test_json_schema_export_of_every_kind() -> None:
    table = Table(
        (
            Field("t", Text("name", min_length=1), "text", required=True),
            Field("i", Integer(1, 2), "integer", default=1),
            Field("n", Number(0.5, 1.5), "number"),
            Field("f", Flag(), "flag"),
            Field("c", Choice(("x", "y")), "choice", default="x"),
        )
    )
    exported = table.json_schema()
    assert exported["required"] == ["t"]
    properties = exported["properties"]
    assert properties["t"]["minLength"] == 1  # type: ignore[index]  # reason: shape asserted here
    assert properties["t"]["pattern"].startswith("^")  # type: ignore[index]  # reason: shape asserted here
    assert properties["i"]["default"] == 1  # type: ignore[index]  # reason: shape asserted here
    assert properties["c"]["enum"] == ["x", "y"]  # type: ignore[index]  # reason: shape asserted here
    assert properties["f"]["type"] == "boolean"  # type: ignore[index]  # reason: shape asserted here
    assert properties["n"]["type"] == "number"  # type: ignore[index]  # reason: shape asserted here
