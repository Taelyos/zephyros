"""The configuration schema (EXG-50, US-E11-02): one invalid document per rule."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from zephyros.core.config import (
    CONFIG_SCHEMA_VERSION,
    SCHEMA,
    ConfigError,
    parse_config,
)
from zephyros.core.schema import Array, Choice, Field, Flag, Integer, Number, Table, Text

DIGEST = "ghcr.io/example/app@sha256:" + "b" * 64
NOTIFICATION_SECRET = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret


def minimal() -> dict[str, Any]:
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "images": {"instance": DIGEST, "guard": DIGEST},
    }


def with_value(path: str, value: object) -> dict[str, Any]:
    document = minimal()
    node: dict[str, Any] = document
    *parents, leaf = path.split(".")
    for name in parents:
        node = node.setdefault(name, {})
    node[leaf] = value
    return document


def test_a_minimal_file_starts_with_the_defaults() -> None:
    config = parse_config(minimal())
    assert config.hub.agent_port == 8080
    assert config.hub.operator_bind == "127.0.0.1"
    assert config.paths.filter_socket == "/run/zephyros/filter.sock"
    assert config.limits.memory_mib == 1024
    assert config.limits.cpus == 2.0
    assert config.tokens.lifetime_seconds == 14400
    assert config.network.instance_pool == "10.200.0.0/16"
    assert config.network.resolvers == ("9.9.9.9",)
    assert config.gateway.provider == "stub"
    assert config.gateway.upstream_url is None
    assert config.notifications.url == ""
    assert config.notifications.secret == ""
    assert config.images.instance == DIGEST


def test_the_name_resolvers_are_read_as_written() -> None:
    document = with_value("network.resolvers", ["1.1.1.1", "8.8.8.8"])
    assert parse_config(document).network.resolvers == ("1.1.1.1", "8.8.8.8")


def test_a_name_resolver_of_a_protected_network_is_refused() -> None:
    document = with_value("network.resolvers", ["192.168.7.7"])
    with pytest.raises(ConfigError) as caught:
        parse_config(document)
    assert [problem.key for problem in caught.value.problems] == ["network.resolvers[192.168.7.7]"]
    assert "protected network" in str(caught.value)


def test_a_full_file_is_read_as_written() -> None:
    document = minimal()
    document.update(
        hub={
            "agent_port": 9000,
            "operator_port": 9001,
            "main_agent_port": 9002,
            "operator_bind": "127.0.0.2",
            "main_agent_bind": "::1",
        },
        paths={
            "data_dir": "/data",
            "filter_socket": "/s/f.sock",
            "operator_token_file": "/t/o",
            "main_agent_token_file": "/t/m",
        },
        runtime={"core_network": "core", "hub_container": "hub"},
        network={"instance_pool": "10.50.0.0/20", "instance_prefix": 27},
        limits={
            "cpus": 1,
            "memory_mib": 512,
            "pids": 128,
            "guard_memory_mib": 24,
            "turn_seconds": 600,
        },
        tokens={"lifetime_seconds": 1200, "rotate_before_seconds": 120},
        gateway={
            "provider": "openai-compatible",
            "upstream_url": "http://127.0.0.1:9100",
            "credential_file": "/t/key",
            "model": "served-model",
            "upstream_model": "deepseek-v4.1-flash",
            "models": ["deepseek-v4.1-flash"],
        },
    )
    config = parse_config(document)
    assert (config.hub.agent_port, config.hub.main_agent_bind) == (9000, "::1")
    assert config.limits.cpus == 1.0
    assert config.gateway.upstream_url == "http://127.0.0.1:9100"
    assert config.gateway.credential_file == "/t/key"
    assert config.runtime.hub_container == "hub"
    assert config.network.instance_prefix == 27


def _leaf_cases() -> list[tuple[str, object]]:
    """For every leaf of the schema, values that violate it, derived from the specification."""
    cases: list[tuple[str, object]] = []

    def walk(table: Table, prefix: str) -> None:
        for field in table.fields:
            path = f"{prefix}{field.name}"
            spec = field.spec
            if isinstance(spec, Table):
                walk(spec, path + ".")
                cases.append((path, "not a table"))
                continue
            cases.append((path, [1, 2]))
            if isinstance(spec, Array):
                cases.extend([(path, "not a list"), (path, [])])
            if isinstance(spec, Integer):
                cases.extend(
                    [
                        (path, spec.minimum - 1),
                        (path, spec.maximum + 1),
                        (path, True),
                        (path, "7"),
                        (path, 1.5),
                    ]
                )
            elif isinstance(spec, Number):
                cases.extend([(path, spec.minimum - 1), (path, spec.maximum + 1), (path, True)])
            elif isinstance(spec, Flag):
                cases.append((path, 1))
            elif isinstance(spec, Choice):
                cases.append((path, "not-an-option"))
            elif isinstance(spec, Text):
                cases.append((path, 12))
                if spec.kind is not None:
                    cases.append((path, ""))
                if spec.kind is not None:
                    cases.append((path, "☃ not valid for this kind"))

    walk(SCHEMA.root, "")
    return [case for case in cases if case[0] != "schema_version" or case[1] != ""]


@pytest.mark.parametrize(("path", "value"), _leaf_cases())
def test_every_violation_refuses_and_names_its_key(path: str, value: object) -> None:
    document = with_value(path, value)
    with pytest.raises(ConfigError) as caught:
        parse_config(document)
    keys = [problem.key for problem in caught.value.problems]
    named = any(key == path or key.startswith(f"{path}[") for key in keys)
    assert named
    assert path in str(caught.value)


def test_the_cases_cover_every_leaf_of_the_schema() -> None:
    covered = {path for path, _ in _leaf_cases()}
    leaves: set[str] = set()

    def walk(table: Table, prefix: str) -> None:
        for field in table.fields:
            if isinstance(field.spec, Table):
                walk(field.spec, f"{prefix}{field.name}.")
            else:
                leaves.add(f"{prefix}{field.name}")

    walk(SCHEMA.root, "")
    assert leaves
    assert leaves <= covered


@pytest.mark.parametrize("path", ["schema_version", "images", "images.instance", "images.guard"])
def test_a_missing_required_key_is_named(path: str) -> None:
    document = minimal()
    node: dict[str, Any] = document
    *parents, leaf = path.split(".")
    for name in parents:
        node = node[name]
    del node[leaf]
    with pytest.raises(ConfigError) as caught:
        parse_config(document)
    assert path in [problem.key for problem in caught.value.problems]


@pytest.mark.parametrize(
    "section", ["", "hub", "paths", "runtime", "images", "network", "limits", "tokens", "gateway"]
)
def test_an_unknown_key_is_refused_in_every_table(section: str) -> None:
    document = minimal()
    if section:
        document.setdefault(section, {})["surprise"] = 1
    else:
        document["surprise"] = 1
    with pytest.raises(ConfigError) as caught:
        parse_config(document)
    expected = f"{section}.surprise" if section else "surprise"
    assert expected in [problem.key for problem in caught.value.problems]


def test_rules_across_keys() -> None:
    cases: list[tuple[dict[str, Any], str]] = [
        (with_value("hub.operator_port", 8080), "hub.operator_port"),
        (with_value("hub.main_agent_port", 8081), "hub.main_agent_port"),
        (with_value("tokens.lifetime_seconds", 600), "tokens.lifetime_seconds"),
        (with_value("tokens.rotate_before_seconds", 20000), "tokens.rotate_before_seconds"),
        (with_value("network.instance_prefix", 24), "network.instance_prefix"),
    ]
    for document, key in cases:
        if key == "network.instance_prefix":
            document["network"]["instance_pool"] = "10.200.0.0/24"
        with pytest.raises(ConfigError) as caught:
            parse_config(document)
        assert key in [problem.key for problem in caught.value.problems], key


def test_an_openai_compatible_provider_needs_its_url_credential_file_and_models() -> None:
    document = with_value("gateway.provider", "openai-compatible")
    with pytest.raises(ConfigError) as caught:
        parse_config(document)
    assert {p.key for p in caught.value.problems} == {
        "gateway.upstream_url",
        "gateway.credential_file",
        "gateway.models",
    }


def test_the_gateway_refuses_a_model_the_configuration_does_not_name() -> None:
    """The owner's model policy: only the models gateway.models names may serve, and any other
    one refuses the start (the first real test runs on one model alone)."""
    document = with_value("gateway.provider", "openai-compatible")
    document["gateway"].update(
        {
            "upstream_url": "https://provider.example.test/v1",
            "credential_file": "/t/key",
            "upstream_model": "claude-opus-5-5",
            "models": ["deepseek-v4.1-flash"],
        }
    )
    with pytest.raises(ConfigError) as caught:
        parse_config(document)
    assert [p.key for p in caught.value.problems] == ["gateway.upstream_model"]
    assert "not one of gateway.models" in caught.value.problems[0].message


def test_a_gateway_serving_a_named_model_starts() -> None:
    document = with_value("gateway.provider", "openai-compatible")
    document["gateway"].update(
        {
            "upstream_url": "https://opencode.ai/zen/go/v1",
            "credential_file": "/t/key",
            "upstream_model": "deepseek-v4.1-flash",
            "models": ["deepseek-v4.1-flash"],
        }
    )
    config = parse_config(document)
    assert config.gateway.upstream_model == "deepseek-v4.1-flash"
    assert config.gateway.models == ("deepseek-v4.1-flash",)


def test_the_notifications_are_read_as_written() -> None:
    document = with_value("notifications.url", "https://main.example.test/zephyros")
    document["notifications"]["secret"] = NOTIFICATION_SECRET
    config = parse_config(document)
    assert config.notifications.url == "https://main.example.test/zephyros"
    assert config.notifications.secret == NOTIFICATION_SECRET


def test_a_notification_configuration_needs_both_its_keys() -> None:
    alone = with_value("notifications.secret", NOTIFICATION_SECRET)
    with pytest.raises(ConfigError) as caught:
        parse_config(alone)
    assert "notifications.url" in [problem.key for problem in caught.value.problems]
    url_only = with_value("notifications.url", "https://main.example.test/zephyros")
    with pytest.raises(ConfigError) as caught:
        parse_config(url_only)
    assert "notifications.secret" in [problem.key for problem in caught.value.problems]


def test_a_notification_url_must_be_http_and_its_secret_long_enough() -> None:
    document = with_value("notifications.url", "ftp://main.example.test/zephyros")
    document["notifications"]["secret"] = "short"  # pragma: allowlist secret
    with pytest.raises(ConfigError) as caught:
        parse_config(document)
    assert {p.key for p in caught.value.problems} == {
        "notifications.url",
        "notifications.secret",
    }


def test_a_refusal_names_every_faulty_key_at_once() -> None:
    document = minimal()
    document["hub"] = {"agent_port": 80, "surprise": True}
    document["limits"] = {"memory_mib": "lots"}
    with pytest.raises(ConfigError) as caught:
        parse_config(document)
    message = str(caught.value)
    for key in ("hub.agent_port", "hub.surprise", "limits.memory_mib"):
        assert key in message


def test_a_newer_major_version_of_the_schema_is_refused() -> None:
    document = minimal()
    document["schema_version"] = "2.0.0"
    with pytest.raises(ConfigError) as caught:
        parse_config(document)
    assert [problem.key for problem in caught.value.problems] == ["schema_version"]
    assert "2.0.0" in str(caught.value)
    document["schema_version"] = "1.7.3"
    assert parse_config(document).hub.agent_port == 8080


def test_a_document_that_is_not_a_table_is_refused() -> None:
    with pytest.raises(ConfigError):
        parse_config(["x"])
    with pytest.raises(ConfigError):
        parse_config(None)


def test_the_published_schema_matches_the_specification() -> None:
    exported = SCHEMA.json_schema()
    assert exported["x-zephyros-schema-version"] == CONFIG_SCHEMA_VERSION
    assert exported["required"] == ["schema_version", "images"]
    assert exported["additionalProperties"] is False
    assert len(exported["x-zephyros-rules"]) == 9  # type: ignore[arg-type]  # reason: the export holds a list


def test_the_defaults_do_not_alias_the_schema() -> None:
    first = SCHEMA.with_defaults(copy.deepcopy(minimal()))
    first["hub"]["agent_port"] = 1  # type: ignore[index]  # reason: the filled data holds tables
    second = SCHEMA.with_defaults(minimal())
    assert second["hub"]["agent_port"] == 8080  # type: ignore[index]  # reason: the filled data holds tables


def test_field_declaration_is_typed() -> None:
    field = Field("n", Integer(1, 2), "d", default=1)
    assert field.default == 1


def test_the_gateway_refuses_any_model_outside_the_owner_policy() -> None:
    """Owner's model policy: DeepSeek V4.1 Flash, through OpenCode, for every role. Naming any
    other model in gateway.models refuses the start, even if it is the one served."""
    document = with_value("gateway.provider", "openai-compatible")
    document["gateway"].update(
        {
            "upstream_url": "https://opencode.ai/zen/go/v1",
            "credential_file": "/t/key",
            "upstream_model": "claude-opus-5-5",
            "models": ["claude-opus-5-5"],
        }
    )
    with pytest.raises(ConfigError) as caught:
        parse_config(document)
    assert "gateway.upstream_model" in [p.key for p in caught.value.problems]


def test_a_github_forge_without_a_token_file_is_refused() -> None:
    with pytest.raises(ConfigError) as caught:
        parse_config(with_value("forge.provider", "github"))
    assert "forge.token_file" in [problem.key for problem in caught.value.problems]


def test_a_github_forge_reads_its_token_file_from_the_configuration() -> None:
    document = with_value("forge.provider", "github")
    document["forge"]["token_file"] = "/run/zephyros-credentials/forge.key"
    config = parse_config(document)
    assert config.forge.provider == "github"
    assert config.forge.token_file == "/run/zephyros-credentials/forge.key"


def test_the_session_header_is_unset_by_default_and_read_when_written() -> None:
    assert parse_config(minimal()).gateway.session_header is None
    document = with_value("gateway.session_header", "x-opencode-session")
    assert parse_config(document).gateway.session_header == "x-opencode-session"
