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

To reproduce the list for a given instance image, run the engine's own offline report (no network call, no model):

```bash
docker run --rm --network none <instance image> sh -c '
mkdir -p /tmp/h && cp /opt/zephyros/seed.yaml /tmp/h/config.yaml
HERMES_HOME=/tmp/h ZEPHYROS_MODEL=m ZEPHYROS_HUB_URL=http://10.0.0.3:8080 \
ZEPHYROS_TOKEN=zph_c_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA \
/opt/hermes/.venv/bin/hermes prompt-size --json' | grep -o '"count": [0-9]*'
```

`tests/runtime/test_hermes.py` runs this report against the image built by `scripts/deploy.sh` and fails when its result differs from the table above, so the list cannot drift silently.

## The hub surface

On the hub, a Contractor instance reaches the tools of `docs/tools.md` that a Contractor instance may use (`whoami`, `project.list`, `project.get`, `contract.read`, `delivery.declare`, `delivery.status`), the model gateway (`/v1`), and the staging area of its project (`GET /staging/base`, `POST /staging/delivery`). Every call is authenticated, scoped to the project of its token and logged.

A push names its branch in the query (`POST /staging/delivery?branch=<name>`). The name is plain: lower-case letters, digits, `.`, `_` and `-`, 63 characters at most, starting with a letter or a digit. The hub refuses a name that holds a secret it knows, or a string shaped like a credential (`sk-…`, `ghp_…`, `zph_…`), because the name goes to the forge and into the title of the pull request (ATK-84). Such a name is refused even when it is innocent: `sk-learn-upgrade-2026` reads as a provider key. The refusal shows the name masked; push the same work again under a name taken from the work, such as `add-subtraction`.

## Nothing in that list reaches the contract

The contract lives in the hub, outside every path mounted in an instance. The tools above can write only inside the container (read-only root file system, two owned volumes), the hub refuses a Contractor any write to the contract (ATK-09, ATK-10), and the network guard blocks everything but the hub's agent endpoint (ATK-13, ATK-39). ATK-08 executes the claim: it invokes every tool above with the path of the contract as an argument, in a real instance, and checks that the contract is bit-identical before and after.
