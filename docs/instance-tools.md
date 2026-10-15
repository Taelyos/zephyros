# The tools of an instance

What an agent inside an instance can reach. This is the documented deliverable of US-E1-07 and the subject of ATK-08: a Contractor instance is assumed to be fully manipulated, and the contract must stay out of reach of every tool it holds.

## The tools of the agent engine

The instance image runs the pinned Hermes Agent (see ADR 0001 and the pin in `images/instance/Dockerfile`) with the configuration of `images/instance/seed.yaml`. In that configuration **25 tools** are offered to the model, in these toolsets:

| Toolset | Tools |
| --- | --- |
| file | `read_file`, `write_file`, `patch`, `search_files` |
| terminal | `terminal`, `process_manage` |
| code_execution | `execute_code` |
| web | `web_search`, `web_extract` |
| browser | `browser_vault_enter_code`, `browser_vault_fill`, `browser_vault_list`, `browser_vault_save_login`, `browser_vault_unlock` |
| browser-use | `browser_exec` |
| vision | `vision_analyze` |
| tts | `text_to_speech` |
| skills | `skill_manage`, `skill_view`, `skills_list` |
| memory | `memory` |
| session_search | `session_search` |
| todo | `todo_list` |
| clarify | `clarify` |
| delegation | `delegate_task` |

The seed configuration disables tool search; no plugin, no kanban toolset, no shell hook and no MCP server other than the hub is configured. An instance may still install an extension of its own inside its container (ATK-77): that stays inside the container, and none of it is part of this list.

Every tool runs without asking and without being refused ([ADR 0031](decisions/0031-the-agent-has-every-power-inside-its-instance.md)): a turn is a one-shot chat that nobody answers, and what protects is around the agent, not in it. The managed scope of the image (`images/instance/managed.yaml`, which the agent's own configuration cannot override) turns the approvals of Hermes off in every context a turn can be in: a command Hermes would flag (`python3 -c`, a here-document, `curl | sh`, a recursive delete), `execute_code`, and the writes of the file tools, `AGENTS.md` and `CLAUDE.md` included. The image also lets the file tools write outside the home of Hermes (`HERMES_WRITE_SAFE_ROOT` empty), so they write the work directory. What Hermes still refuses whatever its configuration: a few catastrophic commands (a recursive delete of `/`, of a system directory or of the home, formatting a file system, a fork bomb, stopping the machine), the deletion of its own interpreter, and a write by its file tools to its own configuration or to a system path; the shell can still do the last two where the user of the instance may write.

To reproduce the list for a given instance image, run the engine's own offline report (no network call, no model):

```bash
docker run --rm --network none <instance image> sh -c '
mkdir -p /tmp/h && cp /opt/zephyros/seed.yaml /tmp/h/config.yaml
HERMES_HOME=/tmp/h ZEPHYROS_MODEL=m ZEPHYROS_HUB_URL=http://10.0.0.3:8080 \
ZEPHYROS_TOKEN=zph_c_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA \
/opt/hermes/.venv/bin/hermes prompt-size --json' | grep -o '"count": [0-9]*'
```

`tests/runtime/test_hermes.py` runs this report against the image built by `scripts/deploy.sh` and fails when its result differs from the table above, so the list cannot drift silently.

### A Contractor that runs OpenCode

When `instances.contractor.engine` is `opencode` ([ADR 0029](decisions/0029-the-contractor-can-be-a-third-party-coding-agent.md)), the Contractor instance runs OpenCode 1.19.0 (`images/instance-opencode`) with the managed configuration of `images/instance-opencode/opencode.jsonc`. Its agent `build`, the one a turn runs, offers the model **11 tools**: `bash`, `edit`, `glob`, `grep`, `question`, `read`, `skill`, `task`, `todowrite`, `webfetch`, `write`. Every one is allowed without asking, save `question`, which a session of `opencode run` denies: nobody is there to answer. The hub's tools come from the one MCP server of the configuration and reach the model as `zephyros_<tool>` (`zephyros_contract_read`, `zephyros_delivery_declare`, …). No plugin, no language server and no other MCP server is configured, and neither the configuration of a delivered repository nor what a turn leaves in its home is read: OpenCode reads the configuration of its user from an empty directory of the read-only root.

To reproduce the list (no network call, no model):

```bash
docker run --rm --network none \
  -e ZEPHYROS_HUB_URL=http://10.0.0.3:8080 -e ZEPHYROS_MODEL=m \
  -e ZEPHYROS_TOKEN=zph_c_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA \
  <opencode image> debug agent build
```

`tests/runtime/test_opencode.py` runs it against the image built by `scripts/deploy.sh` and fails when its result differs from this list. The attack of ATK-08, every tool invoked with the path of the contract, runs against Hermes; against OpenCode, the runtime tests run its shell and the hub's tools, and the guarantees below do not depend on which engine holds the tools.

## What an instance reaches on the network

The level is the operator's choice, `network.egress` in the [configuration](configuration.md) ([ADR 0024](decisions/0024-three-levels-of-network-access.md)); the network guard of the instance enforces it, below the agent, and the agent cannot change it.

| Level | What a turn reaches |
| --- | --- |
| `regulated` (default) | The hub's agent endpoint, and the hosts of `network.allowed_hosts` on port 443 through the egress proxy of the hub, which the turn finds in `HTTPS_PROXY` (by default the package registries: `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`; not GitHub). Nothing else, name resolution included: the proxy resolves the names. Every destination asked for is in the hub's log |
| `sealed` | The hub's agent endpoint, nothing else |
| `open` | The hub's agent endpoint and every public destination |

No level reaches a protected network. A tool whose HTTP client follows `HTTPS_PROXY` reaches the allowed hosts at `regulated`; one that does not (a browser, say) reaches nothing but the hub. The web tools of Hermes (`web_search`, `web_extract`, the browser) are listed above because the engine offers them, not because they reach the web: at `regulated` and `sealed` they cannot fetch an arbitrary page.

## Python and pytest

`python3`, `python` and `pytest` on the `PATH` of a turn are the image's own Python (3.14) with pytest and its wheels preinstalled, at the versions and hashes of the `tests` verification kit (`images/instance/requirements-tests.txt`, kept equal to the kit's by a unit test). They work offline, so a `sealed` instance runs the tests it writes. The kit runs Python 3.12: a test that depends on the minor version of Python may behave differently in the instance and in the sandbox. Hermes itself runs from its own virtual environment, which this does not touch. The OpenCode image is built on the same Python 3.12 as the kit, with the same pinned wheels (`images/instance-opencode/requirements-tests.txt`).

## The tools of the project

When the operator gives the projects a tools directory ([ADR 0030](decisions/0030-the-tools-of-a-project-are-mounted-read-only.md)), a turn finds it read-only at `/opt/project-tools`, on both sides and whatever the engine, and the sandbox of verification finds it at the same path. `/opt/project-tools/bin` is the last directory of the `PATH`: a tool there runs by its name, and a program of the image with the same name comes first. Nothing in a turn can change the directory, and the hub records its fingerprint at each start of a turn. A program of the tools must run on the C library of each image: the Hermes image is Debian (glibc), the OpenCode image and the kit are Alpine (musl) ([the tools of a project](configuration.md#the-tools-of-a-project)).

## The hub surface

On the hub, a Contractor instance reaches the tools of [`docs/tools.md`](tools.md) that a Contractor instance may use (`whoami`, `project.list`, `project.get`, `contract.read`, `delivery.declare`, `delivery.status`), the model gateway (`/v1`), the staging area of its project (`GET /staging/base`, `POST /staging/delivery`), and, at the `regulated` level, the egress proxy. An Owner instance reaches the tools an Owner instance may use, and reads the staging area (`GET /staging/base`, `GET /staging/delivery/<delivery_id>`). Every call is authenticated, scoped to the project of its token and logged. A refused call answers with its code, its cause and the corrective action ([error messages](errors.md)).

A push names its branch in the query (`POST /staging/delivery?branch=<name>`). The name is plain: lower-case letters, digits, `.`, `_` and `-`, 63 characters at most, starting with a letter or a digit. The hub refuses a name that holds a secret it knows, or a string shaped like a credential (`sk-…`, `ghp_…`, `zph_…`), because the name goes to the forge and into the title of the pull request (ATK-84). Such a name is refused even when it is innocent: `sk-learn-upgrade-2026` reads as a provider key. The refusal shows the name masked; push the same work again under a name taken from the work, such as `add-subtraction`.

## Nothing in that list reaches the contract

The contract lives in the hub, outside every path mounted in an instance. The tools above can write only inside the container (read-only root file system, two owned volumes), the hub refuses a Contractor any write to the contract (ATK-09, ATK-10), and the network guard blocks every protected network at every level (ATK-13, ATK-39). At the `regulated` and `sealed` levels it also blocks everything but the hub's agent endpoint and, at `regulated`, the egress proxy, whose allowed hosts are package registries; at `open` the public Internet is reachable, but the contract is not there. ATK-08 executes the claim: it invokes every tool above with the path of the contract as an argument, in a real instance, and checks that the contract is bit-identical before and after.
