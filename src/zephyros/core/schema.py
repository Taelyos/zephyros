"""A small declarative schema for configuration data (EXG-50, US-E11-02).

A schema is a tree of specifications. Validating a value returns every problem found, each
with the dotted key it concerns, so that a refusal can name each faulty key. The same tree
exports a JSON Schema document, which the project publishes with a version number.

Nothing here reads a file: the core receives data that an adapter parsed.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


@dataclass(frozen=True, slots=True)
class Problem:
    """One violation. `key` is dotted ("limits.memory_mib"); empty for the whole document."""

    key: str
    message: str

    def __str__(self) -> str:
        return f"{self.key or '(document)'}: {self.message}"


class Spec(ABC):
    """What a value must look like."""

    @abstractmethod
    def check(self, value: object, key: str) -> list[Problem]:
        """The problems of `value`, an empty list when it conforms."""

    @abstractmethod
    def json_schema(self) -> dict[str, object]:
        """The JSON Schema of the specification."""


def _type_name(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "table"
    if isinstance(value, Sequence):
        return "array"
    return type(value).__name__


# ----------------------------------------------------------------------------- text kinds
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_IMAGE_DIGEST = re.compile(r"^(?:[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?@)?sha256:[0-9a-f]{64}$")
"""An image by the digest of its manifest in a registry (`name@sha256:...`), or a local image by
its identifier (`sha256:...`, the digest of its configuration). Both pin the content."""


def _has_control(value: str, multiline: bool) -> bool:
    """Control characters (category Cc) and unpaired surrogates, except line breaks and tabs
    where the text may span lines."""
    for character in value:
        category = unicodedata.category(character)
        if category == "Cs":
            return True
        if category == "Cc" and not (multiline and character in "\n\t"):
            return True
    return False


def _check_name(value: str) -> str | None:
    if not _NAME.fullmatch(value):
        return (
            "must start with a lower-case letter and hold only a-z, 0-9, '_' and '-' (63 at most)"
        )
    return None


def _check_version(value: str) -> str | None:
    if not _VERSION.fullmatch(value):
        return "must be a version of the form MAJOR.MINOR.PATCH"
    return None


def _check_image_digest(value: str) -> str | None:
    if not _IMAGE_DIGEST.fullmatch(value):
        return "must reference an image by digest (name@sha256:<64 hex> or sha256:<64 hex>)"
    return None


def _check_absolute_path(value: str) -> str | None:
    if not value.startswith("/") or "\x00" in value:
        return "must be an absolute path"
    if any(part == ".." for part in value.split("/")):
        return "must not contain '..'"
    return None


def _check_ipv4_network(value: str) -> str | None:
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError:
        return "must be an IPv4 network in CIDR notation with no host bits set"
    if network.version != 4:
        return "must be an IPv4 network"
    return None


def _check_ipv4_address(value: str) -> str | None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "must be an IPv4 address"
    if address.version != 4:
        return "must be an IPv4 address"
    return None


def _check_ip_address(value: str) -> str | None:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return "must be an IP address"
    return None


CORE_ADDRESS = "core"
"""A listen address that names the hub's own address outside the instance networks, found when
it starts (ADR 0025)."""


def _check_listen_address(value: str) -> str | None:
    if value == CORE_ADDRESS:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return f'must be an IP address or "{CORE_ADDRESS}"'
    if address.is_unspecified:
        return "must not be a wildcard address: it would listen on the instance networks too"
    return None


def _check_http_url(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
    except ValueError:
        return "must be an http or https URL"
    if parts.scheme not in ("http", "https") or not hostname:
        return "must be an http or https URL"
    if parts.username is not None or parts.password is not None:
        return "must not carry credentials"
    if parts.fragment or parts.query:
        return "must not carry a query or a fragment"
    return None


_KINDS: dict[str, tuple[Callable[[str], str | None], str | None]] = {
    # kind: (check, pattern published in the JSON Schema when a regular expression says it all)
    "name": (_check_name, _NAME.pattern),
    "version": (_check_version, _VERSION.pattern),
    "image-digest": (_check_image_digest, _IMAGE_DIGEST.pattern),
    "absolute-path": (_check_absolute_path, "^/"),
    "ipv4-network": (_check_ipv4_network, None),
    "ipv4-address": (_check_ipv4_address, None),
    "ip-address": (_check_ip_address, None),
    "listen-address": (_check_listen_address, None),
    "http-url": (_check_http_url, "^https?://"),
}


@dataclass(frozen=True, slots=True)
class Text(Spec):
    """A string, optionally of a named kind."""

    kind: str | None = None
    min_length: int = 0
    max_length: int = 4096
    pattern: str | None = None
    """A regular expression the whole string must match."""
    multiline: bool = False
    """Whether line breaks and tabs are allowed. Other control characters never are."""

    def __post_init__(self) -> None:
        if self.kind is not None and self.kind not in _KINDS:
            raise ValueError(f"unknown text kind {self.kind!r}")
        if self.pattern is not None:
            re.compile(self.pattern)

    def check(self, value: object, key: str) -> list[Problem]:
        if not isinstance(value, str):
            return [Problem(key, f"must be a string, not a {_type_name(value)}")]
        if len(value) < self.min_length:
            return [Problem(key, f"must have at least {self.min_length} characters")]
        if len(value) > self.max_length:
            return [Problem(key, f"must have at most {self.max_length} characters")]
        if _has_control(value, self.multiline):
            return [Problem(key, "must not contain control characters")]
        if self.kind is not None:
            message = _KINDS[self.kind][0](value)
            if message is not None:
                return [Problem(key, message)]
        if self.pattern is not None and not re.fullmatch(self.pattern, value):
            return [Problem(key, f"must match {self.pattern}")]
        return []

    def json_schema(self) -> dict[str, object]:
        schema: dict[str, object] = {"type": "string", "maxLength": self.max_length}
        if self.min_length:
            schema["minLength"] = self.min_length
        if self.kind is not None:
            schema["x-zephyros-kind"] = self.kind
            kind_pattern = _KINDS[self.kind][1]
            if kind_pattern is not None:
                schema["pattern"] = kind_pattern
        if self.pattern is not None:
            schema["pattern"] = f"^(?:{self.pattern})$"
        return schema


@dataclass(frozen=True, slots=True)
class Integer(Spec):
    minimum: int
    maximum: int

    def check(self, value: object, key: str) -> list[Problem]:
        if isinstance(value, bool) or not isinstance(value, int):
            return [Problem(key, f"must be an integer, not a {_type_name(value)}")]
        if not self.minimum <= value <= self.maximum:
            return [Problem(key, f"must be between {self.minimum} and {self.maximum}")]
        return []

    def json_schema(self) -> dict[str, object]:
        return {"type": "integer", "minimum": self.minimum, "maximum": self.maximum}


@dataclass(frozen=True, slots=True)
class Number(Spec):
    minimum: float
    maximum: float

    def check(self, value: object, key: str) -> list[Problem]:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return [Problem(key, f"must be a number, not a {_type_name(value)}")]
        if not self.minimum <= value <= self.maximum:
            return [Problem(key, f"must be between {self.minimum} and {self.maximum}")]
        return []

    def json_schema(self) -> dict[str, object]:
        return {"type": "number", "minimum": self.minimum, "maximum": self.maximum}


@dataclass(frozen=True, slots=True)
class Flag(Spec):
    def check(self, value: object, key: str) -> list[Problem]:
        if not isinstance(value, bool):
            return [Problem(key, f"must be true or false, not a {_type_name(value)}")]
        return []

    def json_schema(self) -> dict[str, object]:
        return {"type": "boolean"}


@dataclass(frozen=True, slots=True)
class Choice(Spec):
    values: tuple[str, ...]

    def check(self, value: object, key: str) -> list[Problem]:
        if not isinstance(value, str):
            return [Problem(key, f"must be a string, not a {_type_name(value)}")]
        if value not in self.values:
            return [Problem(key, "must be one of: " + ", ".join(self.values))]
        return []

    def json_schema(self) -> dict[str, object]:
        return {"type": "string", "enum": list(self.values)}


@dataclass(frozen=True, slots=True)
class Free(Spec):
    """Any JSON object. A later step validates its content with its own specification."""

    def check(self, value: object, key: str) -> list[Problem]:
        if not isinstance(value, Mapping):
            return [Problem(key, f"must be an object, not a {_type_name(value)}")]
        return []

    def json_schema(self) -> dict[str, object]:
        return {"type": "object"}


@dataclass(frozen=True, slots=True)
class Array(Spec):
    """A list whose items all conform to one specification."""

    item: Spec
    min_items: int = 0
    max_items: int = 1000
    unique: bool = False

    def check(self, value: object, key: str) -> list[Problem]:
        if isinstance(value, str | bytes) or not isinstance(value, Sequence):
            return [Problem(key, f"must be a list, not a {_type_name(value)}")]
        if len(value) < self.min_items:
            return [Problem(key, f"must hold at least {self.min_items} item(s)")]
        if len(value) > self.max_items:
            return [Problem(key, f"must hold at most {self.max_items} item(s)")]
        problems: list[Problem] = []
        for index, item in enumerate(value):
            problems.extend(self.item.check(item, f"{key}[{index}]"))
        if not problems and self.unique and len({_freeze(item) for item in value}) != len(value):
            problems.append(Problem(key, "must not repeat an item"))
        return problems

    def json_schema(self) -> dict[str, object]:
        schema: dict[str, object] = {
            "type": "array",
            "items": self.item.json_schema(),
            "maxItems": self.max_items,
        }
        if self.min_items:
            schema["minItems"] = self.min_items
        if self.unique:
            schema["uniqueItems"] = True
        return schema


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return tuple(sorted((str(k), _freeze(v)) for k, v in value.items()))
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    spec: Spec
    description: str
    required: bool = False
    default: object = None

    def __post_init__(self) -> None:
        if self.required and self.default is not None:
            raise ValueError(f"{self.name}: a required field has no default")


@dataclass(frozen=True, slots=True)
class Rule:
    """A rule across several keys, checked once the structure is sound."""

    description: str
    check: Callable[[Mapping[str, object]], list[Problem]] = field(compare=False)


@dataclass(frozen=True, slots=True)
class Table(Spec):
    """A table with a closed set of keys."""

    fields: tuple[Field, ...]

    def __post_init__(self) -> None:
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("duplicate field names in a table")

    def check(self, value: object, key: str) -> list[Problem]:
        if not isinstance(value, Mapping):
            return [Problem(key, f"must be a table, not a {_type_name(value)}")]
        known = {f.name for f in self.fields}
        problems = [
            Problem(_join(key, name), "unknown key")
            for name in sorted(str(k) for k in value if str(k) not in known)
        ]
        for f in self.fields:
            path = _join(key, f.name)
            if f.name not in value:
                if f.required:
                    problems.append(Problem(path, "is required"))
                continue
            problems.extend(f.spec.check(value[f.name], path))
        return problems

    def with_defaults(self, value: Mapping[str, object]) -> dict[str, object]:
        """A copy of `value` with the defaults filled in. Call it on conforming data."""
        result: dict[str, object] = {}
        for f in self.fields:
            if f.name in value:
                item = value[f.name]
                if isinstance(f.spec, Table) and isinstance(item, Mapping):
                    result[f.name] = f.spec.with_defaults(item)
                else:
                    result[f.name] = item
            elif isinstance(f.spec, Table):
                result[f.name] = f.spec.with_defaults({})
            elif f.default is not None:
                result[f.name] = f.default
        return result

    def json_schema(self) -> dict[str, object]:
        properties = {f.name: _property_schema(f) for f in self.fields}
        schema: dict[str, object] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        required = [f.name for f in self.fields if f.required]
        if required:
            schema["required"] = required
        return schema


@dataclass(frozen=True, slots=True)
class Schema:
    """A versioned document schema: a root table and the rules across keys."""

    identifier: str
    version: str
    title: str
    root: Table
    rules: tuple[Rule, ...] = ()

    def validate(self, data: object) -> list[Problem]:
        """Every problem of `data`, structural first, then the rules across keys."""
        problems = self.root.check(data, "")
        if problems or not isinstance(data, Mapping):
            return problems
        filled = self.root.with_defaults(data)
        for rule in self.rules:
            problems.extend(rule.check(filled))
        return problems

    def with_defaults(self, data: Mapping[str, object]) -> dict[str, object]:
        return self.root.with_defaults(data)

    def json_schema(self) -> dict[str, object]:
        document = self.root.json_schema()
        document["$schema"] = JSON_SCHEMA_DIALECT
        document["$id"] = self.identifier
        document["title"] = self.title
        document["x-zephyros-schema-version"] = self.version
        if self.rules:
            document["x-zephyros-rules"] = [rule.description for rule in self.rules]
        return document


def _property_schema(f: Field) -> dict[str, object]:
    item = f.spec.json_schema()
    item["description"] = f.description
    if f.default is not None:
        item["default"] = f.default
    return item


def _join(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name
