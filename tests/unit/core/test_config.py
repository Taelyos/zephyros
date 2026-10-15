"""The configuration schema (EXG-50, US-E11-02): one invalid document per rule."""

from __future__ import annotations

import copy
import json
from pathlib import Path
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
    assert (config.limits.memory_mib, config.limits.turn_seconds) == (1536, 7200)
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


WILDCARD = "0.0.0.0"  # noqa: S104  # reason: a value under test, not a bind


@pytest.mark.parametrize("key", ["hub.operator_bind", "hub.main_agent_bind"])
@pytest.mark.parametrize(
    "value", [WILDCARD, "::", "::ffff:0.0.0.0", "10.200.4.3", "::ffff:10.200.4.3", "anywhere"]
)
def test_the_operator_and_main_agent_never_listen_on_an_instance_network(
    key: str, value: str
) -> None:
    """A wildcard listens on every network of the hub, an address of the pool is one of an
    instance network: both are refused (ATK-13, ADR 0025)."""
    with pytest.raises(ConfigError) as caught:
        parse_config(with_value(key, value))
    assert key in [problem.key for problem in caught.value.problems]


@pytest.mark.parametrize("value", ["core", "127.0.0.1", "::1", "172.18.0.2", "10.201.0.2"])
def test_the_operator_and_main_agent_listen_on_core_or_an_address_off_the_pool(
    value: str,
) -> None:
    config = parse_config(with_value("hub.operator_bind", value))
    assert config.hub.operator_bind == value


def test_the_channel_tokens_live_ninety_days_by_default() -> None:
    config = parse_config(minimal())
    assert config.tokens.channel_lifetime_days == 90
    assert config.paths.control_socket == "/var/lib/zephyros/control.sock"
    assert (
        parse_config(with_value("tokens.channel_lifetime_days", 7)).tokens.channel_lifetime_days
        == 7
    )


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
    """The model policy is gateway.models: a configuration that names one model alone (the real
    runs named deepseek-v4.1-flash alone) refuses to start with any other."""
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
    assert [str(problem) for problem in caught.value.problems] == [
        "gateway.upstream_model: 'claude-opus-5-5' is not one of gateway.models "
        "(deepseek-v4.1-flash): the gateway serves only the models its configuration names"
    ]


def test_without_an_upstream_model_the_refusal_names_the_model_key() -> None:
    """gateway.upstream_model is unset, so gateway.model is what goes to the provider: the
    refusal names that key, the one to fix, and not a key the file does not hold."""
    document = with_value("gateway.provider", "openai-compatible")
    document["gateway"].update(
        {
            "upstream_url": "https://provider.example.test/v1",
            "credential_file": "/t/key",
            "model": "served-model",
            "models": ["deepseek-v4.1-flash", "other-model"],
        }
    )
    with pytest.raises(ConfigError) as caught:
        parse_config(document)
    assert [str(problem) for problem in caught.value.problems] == [
        "gateway.model: 'served-model' goes to the provider as it is (gateway.upstream_model "
        "is unset) and is not one of gateway.models (deepseek-v4.1-flash, other-model): the "
        "gateway serves only the models its configuration names"
    ]


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


# ----------------------------------------------------------------------------- reasoning (ADR 0032)
def openai_gateway(**keys: object) -> dict[str, Any]:
    document = with_value("gateway.provider", "openai-compatible")
    document["gateway"].update(
        {
            "upstream_url": "https://provider.example.test/v1",
            "credential_file": "/t/key",
            "model": "served-model",
            "models": [str(keys.get("upstream_model", "served-model"))],
            **keys,
        }
    )
    return document


@pytest.mark.parametrize(
    ("keys", "echo"),
    [
        ({"upstream_model": "deepseek-v4.1-flash"}, True),
        ({"upstream_model": "DeepSeek-V4-Pro"}, True),
        ({"upstream_model": "mimo-v2.5-pro"}, True),
        ({"upstream_model": "Kimi-K2-Thinking"}, True),
        ({"upstream_model": "plain-model"}, False),
        ({}, False),  # served-model goes to the provider as it is
        ({"upstream_url": "https://api.deepseek.com/v1"}, True),
        ({"upstream_url": "https://api.moonshot.ai/v1"}, True),
        ({"upstream_url": "https://notmoonshot.ai/v1"}, False),
        ({"upstream_model": "deepseek-v4.1-flash", "reasoning_echo": False}, False),
        ({"upstream_model": "plain-model", "reasoning_echo": True}, True),
    ],
)
def test_the_reasoning_goes_back_to_a_provider_of_a_family_that_asks_for_it(
    keys: dict[str, object], echo: bool
) -> None:
    """Unset, gateway.reasoning_echo follows the model at the provider or its host, never the
    name instances ask for (served-model); set, it is what the file says."""
    config = parse_config(openai_gateway(**keys))
    assert config.gateway.model == "served-model"
    assert config.gateway.reasoning_echo is echo


def test_the_stub_takes_no_reasoning_back_unless_the_file_says_so() -> None:
    assert parse_config(minimal()).gateway.reasoning_echo is False
    named = with_value("gateway.model", "deepseek-v4-flash")
    assert parse_config(named).gateway.reasoning_echo is False
    assert parse_config(with_value("gateway.reasoning_echo", True)).gateway.reasoning_echo is True


def test_the_reasoning_key_is_true_or_false() -> None:
    with pytest.raises(ConfigError) as caught:
        parse_config(with_value("gateway.reasoning_echo", "yes"))
    assert [problem.key for problem in caught.value.problems] == ["gateway.reasoning_echo"]


# ----------------------------------------------------------------------------- context (ADR 0033)
def test_the_agents_work_with_the_whole_declared_window() -> None:
    config = parse_config(with_value("gateway.context_window", 1_000_000))
    assert (config.gateway.context.window, config.gateway.context.tokens) == (1_000_000,) * 2
    assert config.gateway.context.compaction == 900_000


def test_without_a_declared_window_the_agents_have_256_000_tokens() -> None:
    context = parse_config(minimal()).gateway.context
    assert (context.window, context.tokens, context.compaction) == (256_000, 256_000, 224_000)


def test_the_operator_may_give_the_agents_less_than_the_window() -> None:
    document = with_value("gateway.context_window", 1_000_000)
    document["gateway"]["context_tokens"] = 400_000
    context = parse_config(document).gateway.context
    assert (context.tokens, context.compaction) == (400_000, 400_000)


def test_a_context_below_the_floor_is_accepted() -> None:
    """Accepted, with a warning on every surface (tests/unit/app/test_context_warning.py)."""
    document = with_value("gateway.context_window", 1_000_000)
    document["gateway"]["context_tokens"] = 80_000
    assert parse_config(document).gateway.context.below_floor


def test_the_agents_cannot_have_more_than_the_window() -> None:
    document = with_value("gateway.context_window", 128_000)
    document["gateway"]["context_tokens"] = 256_000
    with pytest.raises(ConfigError) as caught:
        parse_config(document)
    assert [str(problem) for problem in caught.value.problems] == [
        "gateway.context_tokens: 256000 is more than the window of the model "
        "(gateway.context_window, 128000): the provider would refuse the calls"
    ]


@pytest.mark.parametrize(
    ("key", "value"),
    [("context_window", 63_999), ("context_tokens", 31_999), ("context_window", "1M")],
)
def test_the_context_keys_are_token_counts_in_bounds(key: str, value: object) -> None:
    with pytest.raises(ConfigError) as caught:
        parse_config(with_value(f"gateway.{key}", value))
    assert [problem.key for problem in caught.value.problems] == [f"gateway.{key}"]


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
    assert len(exported["x-zephyros-rules"]) == 12  # type: ignore[arg-type]  # reason: the export holds a list


def test_the_defaults_do_not_alias_the_schema() -> None:
    first = SCHEMA.with_defaults(copy.deepcopy(minimal()))
    first["hub"]["agent_port"] = 1  # type: ignore[index]  # reason: the filled data holds tables
    second = SCHEMA.with_defaults(minimal())
    assert second["hub"]["agent_port"] == 8080  # type: ignore[index]  # reason: the filled data holds tables


def test_field_declaration_is_typed() -> None:
    field = Field("n", Integer(1, 2), "d", default=1)
    assert field.default == 1


def test_the_gateway_serves_any_model_its_configuration_names() -> None:
    """No list of models lives in the code: a person who names another model in
    gateway.models, and serves it, starts without changing the code."""
    document = with_value("gateway.provider", "openai-compatible")
    document["gateway"].update(
        {
            "upstream_url": "https://provider.example.test/v1",
            "credential_file": "/t/key",
            "upstream_model": "claude-opus-5-5",
            "models": ["claude-opus-5-5", "deepseek-v4.1-flash"],
        }
    )
    config = parse_config(document)
    assert config.gateway.upstream_model == "claude-opus-5-5"
    assert config.gateway.models == ("claude-opus-5-5", "deepseek-v4.1-flash")
    document["gateway"].update({"upstream_model": "llama-4-scout", "models": ["llama-4-scout"]})
    assert parse_config(document).gateway.upstream_model == "llama-4-scout"


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


def test_the_gateway_waits_ten_minutes_by_default_and_reads_its_timeout() -> None:
    """A long answer of a real model takes minutes: the second real run lost a turn at 120 s."""
    assert parse_config(minimal()).gateway.timeout_seconds == 600
    assert parse_config(with_value("gateway.timeout_seconds", 900)).gateway.timeout_seconds == 900
    for wrong in (5, 3601):
        with pytest.raises(ConfigError) as caught:
            parse_config(with_value("gateway.timeout_seconds", wrong))
        assert [problem.key for problem in caught.value.problems] == ["gateway.timeout_seconds"]


def test_the_session_header_is_unset_by_default_and_read_when_written() -> None:
    assert parse_config(minimal()).gateway.session_header is None
    document = with_value("gateway.session_header", "x-opencode-session")
    assert parse_config(document).gateway.session_header == "x-opencode-session"


def test_the_clients_contract_file_is_protected_by_default_and_the_list_is_read() -> None:
    """ADR 0020: a delivery that changes one of these paths is flagged."""
    assert parse_config(minimal()).verification.protected_paths == ("CONTRACT.md",)
    document = with_value("verification.protected_paths", ["CONTRACT.md", "spec/"])
    assert parse_config(document).verification.protected_paths == ("CONTRACT.md", "spec/")
    for wrong in (["../escape"], ["/etc/passwd"], ["a" * 151]):
        with pytest.raises(ConfigError) as caught:
            parse_config(with_value("verification.protected_paths", wrong))
        assert [problem.key for problem in caught.value.problems] == [
            "verification.protected_paths[0]"
        ]


CONFIGURATION_PAGE = Path(__file__).parents[3] / "docs" / "configuration.md"


def _key_table(text: str) -> dict[str, list[str]]:
    """The rows of the table "Every key and its default", by key: [key, default, values]."""
    section = text.split("## Every key and its default\n", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    cells = [[cell.strip() for cell in row.strip("|").split("|")] for row in rows]
    return {row[0].strip("`"): row for row in cells}


def test_the_configuration_page_gives_the_version_and_every_key_with_its_default() -> None:
    """docs/configuration.md once said 1.7.0 for a 1.12.0 schema and left keys out: the page
    names the version the code speaks, and its table gives every key with its default."""
    text = CONFIGURATION_PAGE.read_text(encoding="utf-8")
    assert f"version {CONFIG_SCHEMA_VERSION}." in text
    assert f'schema_version = "{CONFIG_SCHEMA_VERSION}"' in text
    rows = _key_table(text)
    leaves: dict[str, Field] = {}

    def walk(table: Table, prefix: str) -> None:
        for field in table.fields:
            if isinstance(field.spec, Table):
                walk(field.spec, f"{prefix}{field.name}.")
            else:
                leaves[f"{prefix}{field.name}"] = field

    walk(SCHEMA.root, "")
    assert sorted(rows) == sorted(leaves)
    for path, field in leaves.items():
        _, default, values = rows[path]
        if field.required:
            assert default == "required", path
        elif field.default is None:
            assert default == "unset", path
        else:
            assert default == f"`{json.dumps(field.default)}`", path
        spec = field.spec
        if isinstance(spec, Integer | Number):
            assert values.startswith(f"{spec.minimum} to {spec.maximum}"), path
        if isinstance(spec, Choice):
            assert all(f"`{value}`" in values for value in spec.values), path


# ----------------------------------------------------------------------------- egress (ADR 0024)
def test_an_instance_is_regulated_by_default_and_reaches_the_package_registries_only() -> None:
    config = parse_config(minimal())
    assert config.network.egress == "regulated"
    assert config.network.allowed_hosts == (
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
    )
    assert "github.com" not in config.network.allowed_hosts
    assert config.hub.egress_port == 8083


@pytest.mark.parametrize("level", ["regulated", "sealed", "open"])
def test_each_egress_level_is_read_as_written(level: str) -> None:
    assert parse_config(with_value("network.egress", level)).network.egress == level


def test_the_allowed_hosts_are_read_as_written() -> None:
    document = with_value("network.allowed_hosts", ["pypi.org", "github.com"])
    assert parse_config(document).network.allowed_hosts == ("pypi.org", "github.com")


@pytest.mark.parametrize(
    "host",
    [
        "93.184.216.34",  # an address is not a name: a host is allowed by its name
        "*.example.org",  # no wildcard: each host is named
        "PyPI.org",  # one spelling, so that the list and the log compare as written
        "pypi.org.",
        "https://pypi.org",
        "pypi.org:443",
        "localhost",
        "-bad.example.org",
    ],
)
def test_an_allowed_host_that_is_not_a_plain_host_name_is_refused(host: str) -> None:
    with pytest.raises(ConfigError) as caught:
        parse_config(with_value("network.allowed_hosts", [host]))
    assert [problem.key for problem in caught.value.problems] == ["network.allowed_hosts[0]"]


def test_the_egress_port_differs_from_the_other_ports_of_the_hub() -> None:
    with pytest.raises(ConfigError) as caught:
        parse_config(with_value("hub.egress_port", 8080))
    assert [problem.key for problem in caught.value.problems] == ["hub.egress_port"]


# ----------------------------------------------------------------------------- ADR 0029
def test_the_contractor_runs_hermes_unless_the_file_says_otherwise() -> None:
    config = parse_config(minimal())
    assert config.instances.contractor_engine == "hermes"
    assert config.images.opencode is None


def test_a_contractor_that_runs_opencode_takes_the_opencode_image() -> None:
    document = with_value("instances.contractor.engine", "opencode")
    document["images"]["opencode"] = DIGEST
    config = parse_config(document)
    assert config.instances.contractor_engine == "opencode"
    assert config.images.opencode == DIGEST


def test_a_contractor_that_runs_opencode_without_its_image_is_refused() -> None:
    with pytest.raises(ConfigError) as caught:
        parse_config(with_value("instances.contractor.engine", "opencode"))
    assert [p.key for p in caught.value.problems] == ["images.opencode"]


@pytest.mark.parametrize(
    ("path", "value", "faulty"),
    [
        ("instances.contractor.engine", "claude", "instances.contractor.engine"),
        ("instances.owner.engine", "opencode", "instances.owner"),  # the Owner runs Hermes
        ("instances.contractor.image", DIGEST, "instances.contractor.image"),
        ("images.opencode", "opencode:latest", "images.opencode"),
    ],
)
def test_an_engine_or_an_image_outside_the_schema_is_refused(
    path: str, value: object, faulty: str
) -> None:
    with pytest.raises(ConfigError) as caught:
        parse_config(with_value(path, value))
    assert [p.key for p in caught.value.problems] == [faulty]


# ----------------------------------------------------------------------------- tools (ADR 0030)
def test_a_file_without_tools_names_none() -> None:
    assert parse_config(minimal()).tools.source is None


@pytest.mark.parametrize(
    "source", ["/srv/zephyros-tools", "/opt/sim-2.1/build_x86_64", "/home/op/tools@v3+fix"]
)
def test_a_tools_directory_is_an_absolute_plain_path(source: str) -> None:
    assert parse_config(with_value("tools.source", source)).tools.source == source


@pytest.mark.parametrize(
    "source",
    [
        "/",  # the whole machine
        "tools",  # relative
        "/srv/tools/",  # a trailing slash
        "/srv//tools",
        "/srv/./tools",
        "/srv/../etc",
        "/srv/tools/..",
        "/srv/my tools",  # a space
        "/srv/tools:/etc",  # a colon would end a mount option
        "/srv/tools,ro=false",
        '/srv/"tools"',
        "/srv/tools\\x",
    ],
)
def test_a_tools_directory_that_is_not_a_plain_absolute_path_is_refused(source: str) -> None:
    with pytest.raises(ConfigError) as refused:
        parse_config(with_value("tools.source", source))
    assert [problem.key for problem in refused.value.problems] == ["tools.source"]


def test_the_tools_table_takes_no_other_key() -> None:
    document = with_value("tools.source", "/srv/tools")
    document["tools"]["target"] = "/elsewhere"
    with pytest.raises(ConfigError, match=r"tools\.target"):
        parse_config(document)


@pytest.mark.parametrize(("seconds", "valid"), [(7200, True), (86400, True), (86401, False)])
def test_a_verification_may_last_up_to_a_day(seconds: int, valid: bool) -> None:
    """ADR 0030: an acceptance test may play several runs of a simulator, so a verification
    may last hours; the bound is the one of a turn."""
    document = with_value("verification.timeout_seconds", seconds)
    if valid:
        assert parse_config(document).verification.timeout_seconds == seconds
    else:
        with pytest.raises(ConfigError, match=r"verification\.timeout_seconds"):
            parse_config(document)
