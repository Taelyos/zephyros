"""ATK-49 on the real organs: no git-hosting credential lives in an instance, and a direct
forge write has nowhere to go.

The instance image is probed as a turn meets it (same user, no extra environment): no `gh`,
no credential file, no configured helper, and a push fails. A second probe, run from the core
network, finds no git service: the hub's only endpoint answers 404 to the git wire protocol,
and a forge host resolves nowhere. The forge itself is a directory bound into the hub
container alone, so an instance has no forge path and no forge route to write to.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.runtime.deployment import Deployment
from tests.runtime.rig import docker

pytestmark = [pytest.mark.runtime, pytest.mark.acceptance, pytest.mark.slow]

CREDENTIALS_SCRIPT = """\
echo "gh=$(command -v gh || echo absent)"
if [ -e "$HOME/.git-credentials" ]; then echo "credentials-file=present"
else echo "credentials-file=absent"; fi
if [ -e "$HOME/.netrc" ]; then echo "netrc=present"; else echo "netrc=absent"; fi
echo "git-config=$(git config --global --list 2>/dev/null | tr '\\n' ';' || true)"
echo "credential-helper=$(git config --global --get credential.helper 2>/dev/null || echo absent)"
echo "system-helper=$(git config --system --get credential.helper 2>/dev/null || echo absent)"
echo "git-askpass=${GIT_ASKPASS:-absent}"
names=$(env | grep -iE 'token|_key|secret|gh_|github' | cut -d= -f1 | sort | tr '\\n' ',')
echo "secret-env=$names"
mkdir -p /tmp/forge-attempt && cd /tmp/forge-attempt && git init -q .
git remote add origin http://127.0.0.1:1/example.git
if git push origin HEAD:refs/heads/main 2>/dev/null; then echo "push=SUCCEEDED"
else echo "push=failed"; fi
"""

WIRE_PROBE = """\
import urllib.error
import urllib.request


def probe(name, url):
    try:
        answer = urllib.request.urlopen(url, timeout=6)
        print(f"{name}={answer.status}")
    except urllib.error.HTTPError as error:
        print(f"{name}={error.code}")
    except Exception as error:
        print(f"{name}={type(error).__name__}")


probe("hub-git", "http://HUB:8080/example/calc.git/info/refs?service=git-upload-pack")
probe("hub-git-write", "http://HUB:8080/example/calc.git/git-receive-pack")
probe("forge-elsewhere", "http://forge.example.test/example/calc.git/info/refs")
"""


@pytest.fixture(scope="module")
def deployment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Deployment]:
    made = Deployment(tmp_path_factory.mktemp("atk49"), pool="10.220.0.0/16")
    made.up()
    try:
        yield made
    finally:
        made.down()


def asked(done: object) -> dict[str, str]:
    text = str(getattr(done, "stdout", ""))
    return dict(
        line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith(" ")
    )


def test_the_instance_image_holds_no_git_hosting_credential_and_a_push_fails(
    deployment: Deployment,
) -> None:
    done = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "sh",
        deployment.instance_image(),
        "-c",
        CREDENTIALS_SCRIPT,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    answers = asked(done)
    assert answers["gh"] == "absent"
    assert answers["credentials-file"] == "absent"
    assert answers["netrc"] == "absent"
    assert answers["credential-helper"] == "absent"
    assert answers["system-helper"] == "absent"
    assert answers["git-askpass"] == "absent"
    assert answers["secret-env"] == ""
    assert "token" not in answers["git-config"].lower()
    assert answers["push"] == "failed"


def test_the_hub_serves_no_git_protocol_and_no_forge_host_answers(
    deployment: Deployment,
) -> None:
    names = docker(
        "network",
        "ls",
        "--filter",
        f"label=org.zephyros.deployment={deployment.name}",
        "--format",
        "{{.Name}}",
    )
    network = names.stdout.strip().splitlines()[0]
    code = WIRE_PROBE.replace("HUB", deployment.hub)
    done = docker(
        "run",
        "--rm",
        "--network",
        network,
        "--entrypoint",
        "/opt/hermes/.venv/bin/python",
        deployment.instance_image(),
        "-c",
        code,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    answers = asked(done)
    assert answers["hub-git"] == "404"
    assert answers["hub-git-write"] == "404"
    assert answers["forge-elsewhere"] == "URLError"
