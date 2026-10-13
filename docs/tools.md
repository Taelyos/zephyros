# Tools of the hub

<!-- Generated from the registry of tools and the rights matrix: do not edit by hand. Regenerate with `python -m tools.generate_schemas`. -->

An agent reaches the hub through MCP on its agent endpoint, and the operator reaches it through the operator channel. Both use the same registry. A tool that a caller may not use is not listed to that caller, and calling it by name is refused and logged. Every call, allowed or refused, leaves exactly one record in the log.

"Own project" means the project of the caller's token. An instance that names another project in a call is refused. Unknown fields in a call are refused.

| Tool | What it does | Operator | Main agent | Owner instance | Contractor instance |
| --- | --- | --- | --- | --- | --- |
| `whoami` | Say who the hub takes the caller to be. A Contractor instance also reaches, with its project token: the model gateway at `/v1`, and the staging area of its project — `GET /staging/base` (the base to start from, as a git bundle) and `POST /staging/delivery?branch=<name>` (its work, as a git bundle). | yes | yes | yes | yes |
| `project.create` | Create a project from a name and, as inert text, a repository URL. | yes | yes | no | no |
| `project.list` | List the projects the caller may see. | yes | yes | own project | own project |
| `project.get` | Describe one project. | yes | yes | own project | own project |
| `instance.start` | Start the instance of a side of a project, with the first prompt of a turn. | yes | yes | no | no |
| `instance.stop` | Stop the instance of a side. | yes | yes | no | no |
| `instance.status` | Describe the instance of a side. | yes | yes | no | no |
| `contract.read` | Read a version of the contract (the current one by default) as data. | yes | yes | own project | own project |
| `contract.pending` | Read the changes of the contract that await a decision, each in full: its proposer, the version it starts from, its summary and its operations. Read a change before approving or rejecting it. | yes | yes | own project | no |
| `contract.propose` | Propose a change to the contract. Another principal approves or rejects it. A change that touches operator-reserved data (a requirement or constraint, a P0 criterion, a link to a P0 requirement) is refused: the operator applies those. Objects: story {type, id US-E1-01, epic E1, title, text}; criterion {type, id US-E1-01.1, story, text, verification, test file::function}; link {type, id LNK:US-E1-01>EXG-01, kind covers, source, target}; note {type, id NB-001, target, text}. | yes | yes | own project | no |
| `contract.approve` | Approve a pending change of another principal: it becomes a new version. | yes | yes | own project | no |
| `contract.reject` | Reject a pending change: the current version stays. | yes | yes | own project | no |
| `contract.apply_reserved` | Apply a change to operator-reserved data. The operator only. | yes | no | no | no |
| `contract.import` | Import the Markdown document of the project as contract objects: requirement and constraint tables, story sections with their criteria and links. The operator only. | yes | no | no | no |
| `project.prepare` | Take the base of the project's staging area from a branch of its repository on the forge. | yes | yes | no | no |
| `acceptance.put` | Keep an acceptance test of the Owner side. Every verification uses it, whatever a delivery contains. | yes | no | own project | no |
| `delivery.declare` | Declare the delivery of the branch pushed to `POST /staging/delivery?branch=<name>`: the hub replays the acceptance tests, computes the gate and decides. The Contractor only. | no | no | no | own project |
| `delivery.status` | Describe a delivery: its state, the decision, the evidence report, the pull request. | yes | yes | own project | own project |
| `inspection.record` | Record your inspection of one requirement or constraint of a decided delivery, when its methods include one that tests cannot show (inspection, analysis, demonstration): verdict conforms or does_not_conform, and what you observed. The gate decides again from the same results; until an inspection is recorded, such an object keeps the delivery from PASS. Read the delivery first (GET /staging/delivery/<delivery_id>). | yes | yes | own project | no |
| `log.read` | Read records of the log. The operator only. | yes | no | no | no |
| `log.verify` | Verify the chain of a stream. The operator only. | yes | no | no | no |

## What a Contractor instance reaches on the hub

`whoami`, `project.list`, `project.get`, `contract.read`, `delivery.declare`, `delivery.status`. It reads the contract as data and cannot write it, propose a change to it, approve one, write an acceptance test, or reach the log, the configuration or the instances. It declares a delivery and reads its state; it never records a result.

Besides these tools, a Contractor instance calls with its project token the model gateway (`/v1`) and the staging area of its project: `GET /staging/base` for the base to start from and `POST /staging/delivery?branch=<name>` to push its work, both as git bundles. The tools of the agent engine itself, inside the container, are documented with the engine integration.
