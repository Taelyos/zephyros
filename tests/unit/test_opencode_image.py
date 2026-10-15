"""The OpenCode instance image (ADR 0029): what it pins, and the configuration OpenCode gets in a
turn. The configuration is read here as OpenCode reads it: comments dropped, {env:NAME} replaced
by the environment of the turn. tests/runtime/test_opencode.py checks, against OpenCode itself,
that it resolves the file to the same values."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_instance_image import _pins
from zephyros.adapters.opencode.engine import OpenCodeEngine
from zephyros.core.containers import Policy, proxy_environment, turn_spec
from zephyros.core.ports.agent_engine import Side

ROOT = Path(__file__).resolve().parents[2]
IMAGE = ROOT / "images" / "instance-opencode"
DOCKERFILE = (IMAGE / "Dockerfile").read_text(encoding="utf-8")
FLAT = DOCKERFILE.replace("\\\n", " ")
HUB = "http://10.200.0.19:8080"
TOKEN = "zph_c_" + "T" * 43
TURN = {"ZEPHYROS_HUB_URL": HUB, "ZEPHYROS_MODEL": "deepseek-v4.1-flash", "ZEPHYROS_TOKEN": TOKEN}
ENTRYPOINT_SETS = {
    "ZEPHYROS_REASONING_FIELD": "",
    "ZEPHYROS_CONTEXT_WINDOW": "256000",
    "ZEPHYROS_OPENCODE_INPUT": "240000",
}
"""What the entrypoint of the image sets, with the defaults of the hub (ADR 0032, ADR 0033)."""
ENV = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def raw() -> str:
    lines = (IMAGE / "opencode.jsonc").read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("//"))


def resolved(environment: dict[str, str]) -> dict[str, Any]:
    environment = {**ENTRYPOINT_SETS, **environment}
    text = ENV.sub(lambda m: environment.get(m[1], ""), raw())
    found: dict[str, Any] = json.loads(text)
    return found


def leaves(node: object, path: str = "") -> list[tuple[str, object]]:
    if isinstance(node, dict):
        return [item for key, value in node.items() for item in leaves(value, f"{path}/{key}")]
    if isinstance(node, list):
        return [item for i, value in enumerate(node) for item in leaves(value, f"{path}/{i}")]
    return [(path, node)]


# ----------------------------------------------------------------------------- the image
def test_the_image_installs_the_same_pinned_wheels_as_the_kit() -> None:
    mine = _pins(IMAGE / "requirements-tests.txt")
    kit = _pins(ROOT / "images" / "kit-tests" / "requirements.txt")
    assert mine == kit
    assert "--require-hashes" in FLAT
    assert "--only-binary :all:" in FLAT


def test_the_image_bakes_opencode_1_19_0_checked_by_its_sha512_for_each_platform() -> None:
    packages = re.findall(r"package=(opencode-linux-[a-z0-9-]+)", FLAT)
    digests = re.findall(r"sha512=([0-9a-f]+)", FLAT)
    assert packages == ["opencode-linux-x64-baseline-musl", "opencode-linux-arm64-musl"]
    assert [len(d) for d in digests] == [128, 128]
    assert '"https://registry.npmjs.org/$package/-/$package-1.19.0.tgz"' in FLAT
    assert 'echo "$sha512  /tmp/opencode.tgz" | sha512sum -c -' in FLAT
    instructions = [line for line in FLAT.splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in instructions if re.search(r"\b(npm|npx|bun)\s", line)]


def test_the_image_runs_unprivileged_with_the_configuration_out_of_its_reach() -> None:
    assert "USER 10000:10000" in DOCKERFILE
    assert "COPY --chmod=0644 opencode.jsonc /etc/opencode/opencode.jsonc" in DOCKERFILE
    assert 'ENTRYPOINT ["/opt/zephyros/entrypoint.sh"]' in DOCKERFILE
    assert "HOME=/opt/data" in FLAT


def test_the_configuration_of_the_agent_is_read_from_empty_directories_of_the_root() -> None:
    """The configuration of OpenCode's user and the ".opencode" of its home are never on the
    home volume, where a turn could leave what the next one loads."""
    assert "XDG_CONFIG_HOME=/opt/opencode/config" in FLAT
    assert "OPENCODE_TEST_HOME=/opt/opencode/home" in FLAT
    assert "mkdir -p /opt/opencode/config/opencode /opt/opencode/home" in FLAT
    assert "touch /opt/opencode/config/opencode/.gitignore" in FLAT
    assert "COPY --chmod=0644 session.py /opt/zephyros/session.py" in DOCKERFILE
    instructions = [line for line in FLAT.splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in instructions if "chown" in line and "/opt/opencode" in line]


@pytest.mark.parametrize(
    "flag",
    [
        "OPENCODE_DISABLE_AUTOUPDATE=1",
        "OPENCODE_DISABLE_MODELS_FETCH=1",
        "OPENCODE_DISABLE_SHARE=1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS=1",
        "OPENCODE_PURE=1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD=1",
        "OPENCODE_DISABLE_CLAUDE_CODE=1",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS=1",
        "OPENCODE_DISABLE_PROJECT_CONFIG=1",
    ],
)
def test_what_opencode_would_fetch_or_send_by_itself_is_off(flag: str) -> None:
    assert flag in FLAT


# ----------------------------------------------------------------------------- the configuration
def test_the_configuration_reads_three_variables_of_the_turn_and_those_of_the_entrypoint() -> None:
    assert set(ENV.findall(raw())) == {*TURN, *ENTRYPOINT_SETS}


def test_the_model_has_the_window_of_the_hub_and_compacts_where_hermes_does() -> None:
    """ADR 0033: OpenCode compacts at its input limit less the 16 000 tokens of the answer."""
    config = resolved(
        {
            **TURN,
            "ZEPHYROS_CONTEXT_WINDOW": "1000000",
            "ZEPHYROS_OPENCODE_INPUT": str(900_000 + 16_000),
        }
    )
    limit = config["provider"]["zephyros"]["models"]["deepseek-v4.1-flash"]["limit"]
    assert limit == {"context": 1_000_000, "input": 916_000, "output": 16_000}


@pytest.mark.parametrize("model", ["deepseek-v4.1-flash", "served-model"])
def test_the_reasoning_goes_back_in_its_field_when_the_entrypoint_names_it(model: str) -> None:
    """ADR 0032: OpenCode keeps the reasoning of each answer in the field the gateway carries,
    whatever the model is named; an empty field keeps none, where OpenCode would otherwise
    decide from a name that holds "deepseek"."""
    turn = {**TURN, "ZEPHYROS_MODEL": model}
    for field, expected in (("reasoning_content", "reasoning_content"), ("", "")):
        config = resolved({**turn, "ZEPHYROS_REASONING_FIELD": field})
        assert config["provider"]["zephyros"]["models"][model]["interleaved"] == expected


def test_the_model_goes_to_the_gateway_of_the_hub_with_the_project_token() -> None:
    config = resolved(TURN)
    assert config["enabled_providers"] == ["zephyros"]
    assert list(config["provider"]) == ["zephyros"]
    provider = config["provider"]["zephyros"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"  # bundled in OpenCode: no install
    assert {k: provider["options"][k] for k in ("baseURL", "apiKey")} == {
        "baseURL": f"{HUB}/v1",
        "apiKey": TOKEN,
    }
    assert list(provider["models"]) == ["deepseek-v4.1-flash"]
    assert config["model"] == config["small_model"] == "zephyros/deepseek-v4.1-flash"


def test_the_one_mcp_server_is_the_hub_with_the_project_token() -> None:
    config = resolved(TURN)
    assert config["mcp"] == {
        "zephyros": {
            "type": "remote",
            "url": f"{HUB}/mcp",
            "enabled": True,
            "oauth": False,
            "headers": {"Authorization": f"Bearer {TOKEN}"},
        }
    }


def test_the_token_is_the_one_secret_and_goes_to_the_hub_only() -> None:
    config = resolved(TURN)
    holding = [path for path, value in leaves(config) if isinstance(value, str) and TOKEN in value]
    assert holding == ["/provider/zephyros/options/apiKey", "/mcp/zephyros/headers/Authorization"]
    urls = [value for _, value in leaves(config) if isinstance(value, str) and "://" in value]
    assert all(url.startswith(HUB) for url in urls if not url.startswith("https://opencode.ai/"))
    assert [url for url in urls if url.startswith("https://opencode.ai/")] == [
        "https://opencode.ai/config.json"  # the name of the schema, never fetched
    ]


def test_a_long_answer_of_the_gateway_is_waited_for_and_never_asked_twice() -> None:
    """The gateway answers once the provider has answered whole, and gives up after
    gateway.timeout_seconds: OpenCode waits longer than the longest the schema allows, so a call
    the gateway is still carrying is never abandoned and asked again (paid twice). OpenCode
    1.19.0 sets no limit on a whole request unless `timeout` says one."""
    schema = json.loads((ROOT / "schemas" / "configuration.schema.json").read_text("utf-8"))
    longest = schema["properties"]["gateway"]["properties"]["timeout_seconds"]["maximum"]
    options = resolved(TURN)["provider"]["zephyros"]["options"]
    assert options["headerTimeout"] > longest * 1000
    assert options["chunkTimeout"] > longest * 1000
    assert "timeout" not in options


def test_every_tool_is_allowed_and_nothing_is_shared_updated_or_added() -> None:
    config = resolved(TURN)
    assert config["permission"] == "allow"
    assert config["share"] == "disabled"
    assert config["autoupdate"] is False
    assert config["plugin"] == []
    assert (config["lsp"], config["formatter"], config["snapshot"]) == (False, False, False)


def test_the_hub_address_of_a_real_turn_fills_the_configuration() -> None:
    """The environment the template gives a turn fills every variable the file reads."""
    policy = Policy(
        guard_image="g@sha256:" + "a" * 64,
        instance_image="i@sha256:" + "b" * 64,
        hub_container="zephyros-hub",
        hub_port=8080,
        model="deepseek-v4.1-flash",
        cpus=1.0,
        memory_mib=512,
        pids=64,
        guard_memory_mib=32,
        network_pool="10.200.0.0/16",
        network_prefix=28,
        contractor_engine="opencode",
        opencode_image="o@sha256:" + "c" * 64,
    )
    spec = turn_spec(
        policy, OpenCodeEngine(), "p_abcdef12", Side.CONTRACTOR, "10.200.0.19", TOKEN, "Go."
    )
    environment = dict(entry.split("=", 1) for entry in spec["Env"])
    assert set(TURN) <= set(environment)
    assert set(environment) - set(TURN) == {
        *proxy_environment(policy, "10.200.0.19", TOKEN),
        "ZEPHYROS_REASONING_ECHO",  # read by the entrypoint, which sets the field
        "ZEPHYROS_CONTEXT_WINDOW",
        "ZEPHYROS_COMPACTION_TOKENS",  # read by the entrypoint, which sets the input limit
        "ZEPHYROS_SESSION",  # read by the entrypoint
    }
    assert resolved(environment) == resolved(TURN)
