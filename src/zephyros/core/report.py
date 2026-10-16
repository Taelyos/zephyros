"""The evidence report of a delivery: what was verified, how, and what the gate decided.

It is attached to the pull request (EXG-14), which is sometimes public, so every secret is masked
(EXG-49): the secrets the hub knows of, and anything shaped like a token. It holds text that the
delivery chose (paths, the output of its tests): such text is shown as code, never as markup.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from zephyros.core.context import CONTEXT_FLOOR, FLOOR_REASON
from zephyros.core.gate import GateResult
from zephyros.core.masking import mask_text
from zephyros.core.obstacles import Obstacle
from zephyros.core.ports.agent_engine import Side
from zephyros.core.ports.delivery import Delivery
from zephyros.core.ports.verification import Outcome, TimeLimit
from zephyros.core.tools import TOOLS_PATH, ToolsDigest

LOG_EXCERPT = 4000
LISTED_TESTS = 50
"""The most tests that never ran the report names one by one; it counts the others."""
LISTED_OBSTACLES = 50
"""The most obstacles the report names one by one, the latest; it counts the others."""
INSPECTIONS_HEADING = "## Inspections recorded after the gate"


@dataclass(frozen=True, slots=True)
class Cost:
    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ReportInput:
    project_name: str
    delivery: Delivery
    contract_version: int
    contract_hash: str
    kit: str
    gate: GateResult
    outcomes: Mapping[str, Outcome]
    log: str
    cost: Cost
    produced_at: str
    produced_by: str
    checks_run: bool = False
    simulation: bool = False
    """The simulated model provider carried this delivery (EXG-46): the report says so."""
    flagged_paths: tuple[str, ...] = ()
    """The client's own files the delivery changes (ADR 0020): flagged, never refused."""
    acceptance_files: tuple[tuple[str, str], ...] = ()
    """The files of the Owner side in the tree verified, with the digest of their content."""
    unnamed_acceptance_files: tuple[str, ...] = ()
    """Those that no criterion names. The kit runs no test from them; the report marks them and
    names the tests that never ran, never a file."""
    never_run_tests: tuple[str, ...] = ()
    """The tests the kit found in the files it ran and did not run, since no criterion names
    them (`path::name`, as the kit names them)."""
    timeouts: Mapping[str, TimeLimit] = dataclasses.field(default_factory=dict)
    """The tests that reached a time limit (AN-023): the report says "timed out", not only the
    outcome the gate counts."""
    stopped_at: int | None = None
    """The limit of the verification, when the run reached it and was stopped."""
    missing_acceptance_files: tuple[str, ...] = ()
    """The files a criterion names that neither the Owner side nor the base holds (ATK-16)."""
    set_aside_paths: tuple[str, ...] = ()
    """Those of the missing files the delivery brought: its versions were set aside, unrun."""
    built_by: tuple[tuple[str, int], ...] = ()
    """The agent engines of the Contractor's turns recorded before the delivery came in, each
    with its version and how many turns it ran (ADR 0029)."""
    tools: ToolsDigest | None = None
    """The tools the sandbox mounted, as the hub measured them around the run (ADR 0030)."""
    tools_of_turns: tuple[tuple[str, int], ...] = ()
    """The tools the turns of both sides found before the delivery came in, in the order of the
    turns: each fingerprint (or "none"), with how many turns in a row found it."""
    context_of_turns: tuple[tuple[int, int, int, int], ...] = ()
    """The context the agents of both sides worked with in the turns before the delivery came
    in (ADR 0033): each (context, window of the model, compaction point, turns), as the starts
    recorded it."""
    obstacles: tuple[Obstacle, ...] = ()
    """The obstacles of the project when the report was written, each as it stood then
    (ADR 0034)."""


def mask(text: str, secrets: Iterable[str] = ()) -> str:
    """`text` with every known secret and every token-shaped string replaced (EXG-49)."""
    return mask_text(text, secrets)


def _code(text: str) -> str:
    """Text shown as inline code: no backtick, no line break, no control character inside."""
    cleaned = "".join("?" if ord(c) < 32 or c in "`|" else c for c in text)
    return f"`{cleaned[:200]}`"


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def _plain(text: str) -> str:
    """A text of the hub's own (names, reasons), safe in a table cell."""
    return text.replace("|", "/").replace("\n", " ")


def _tools_lines(tools: ToolsDigest | None, sandbox: bool) -> list[str]:
    """`sandbox`: whether a sandbox ran tests with them; with no test to run, none started."""
    if tools is None:
        return []
    where = (
        "in the turns of both sides and in the sandbox"
        if sandbox
        else "in the turns of both sides (no test ran, so no sandbox started)"
    )
    return [
        f"- Project tools: {_code(tools.fingerprint)} ({tools.files} files, {tools.bytes} bytes), "
        f"mounted read-only at {_code(TOOLS_PATH)} {where}"
    ]


def _seconds(value: float) -> str:
    return f"{value:g} s" if value < 100 else f"{round(value):d} s"


def _outcome(test: str, outcome: Outcome, timeouts: Mapping[str, TimeLimit]) -> str:
    """An outcome as the report gives it: a test that reached a time limit says so, apart from
    an error or a failure (AN-023); the gate counts the outcome all the same."""
    return f"{outcome.value} (timed out)" if test in timeouts else outcome.value


def _time_limit(found: TimeLimit, stopped_at: int | None) -> str:
    if found.phase == "run":
        limit = "" if stopped_at is None else f" of {_seconds(stopped_at)}"
        return (
            f"under way for {_seconds(found.seconds)} when the run reached its limit{limit} "
            "and was stopped"
        )
    limit = "a time limit" if found.limit is None else f"its limit of {_seconds(found.limit)}"
    return f"its {_plain(found.phase)} reached {limit} after {_seconds(found.seconds)}"


def _stopped_lines(report: ReportInput) -> list[str]:
    if report.stopped_at is None:
        return []
    under_way = [test for test, found in report.timeouts.items() if found.phase == "run"]
    return [
        f"**Time limit reached:** the run was stopped at the limit of the verification, "
        f"{_seconds(report.stopped_at)} (`verification.timeout_seconds`). Nothing it printed is "
        "believed, and every test reads not_run. "
        + (
            f"Under way when it stopped: {_code(under_way[0])}."
            if under_way
            else "The kit had signed no test as under way."
        ),
        "",
    ]


def _context_lines(contexts: tuple[tuple[int, int, int, int], ...]) -> list[str]:
    if not contexts:
        return []
    return [
        "- Context of the agents: "
        + ", ".join(
            f"{tokens} tokens of a window of {window}, compacting at {compaction} "
            f"({turns} turn{'' if turns == 1 else 's'})"
            for tokens, window, compaction, turns in contexts
        )
    ]


def _obstacle_state(obstacle: Obstacle) -> str:
    if obstacle.awaiting:
        who = {"owner": "the Owner side", "operator": "the operator", "human": "the human"}
        return f"{obstacle.status.value}, awaits {who[obstacle.level.value]}"
    return obstacle.status.value


def _obstacles_lines(obstacles: tuple[Obstacle, ...], *, owner_side: bool) -> list[str]:
    """The obstacles the agents reported, and the decisions taken on them (ADR 0034). The
    report the Contractor reads leaves out those of the Owner side, and counts them."""
    shown = [o for o in obstacles if owner_side or o.side is Side.CONTRACTOR]
    kept = len(obstacles) - len(shown)
    if not shown:
        lines = ["None reported."]
    else:
        open_ = [o for o in shown if o.awaiting]
        blocking = [o for o in open_ if o.blocking]
        lines = [
            f"{len(shown)} reported, {len(open_)} awaiting an answer"
            + (f", {len(blocking)} of them blocking" if blocking else "")
            + ".",
            "",
            "| Obstacle | Side | Nature | Blocking | State | Decisions | Title |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        latest = sorted(shown, key=lambda o: o.number)[-LISTED_OBSTACLES:]
        for obstacle in latest:
            decisions = "<br>".join(
                f"{entry.decision.value} by {_plain(entry.by)}" for entry in obstacle.entries
            )
            lines.append(
                f"| {obstacle.id} | {obstacle.side.value} | {obstacle.nature} | "
                f"{'yes' if obstacle.blocking else 'no'} | {_obstacle_state(obstacle)} | "
                f"{decisions or 'none'} | {_code(obstacle.title)} |"
            )
        if len(shown) > LISTED_OBSTACLES:
            lines += ["", f"And {len(shown) - LISTED_OBSTACLES} earlier."]
    if kept:
        lines += [
            "",
            f"{kept} obstacle{'' if kept == 1 else 's'} of the Owner side "
            f"{'is' if kept == 1 else 'are'} kept for the Owner side.",
        ]
    return lines


def _tools_flag(report: ReportInput) -> tuple[str, list[str]]:
    """The tools the turns found, when they are not the ones verified all along (AN-026). A
    change between two turns, the last of them finding the tools verified, is the expected
    course after a fix of the tools, and is said so; a change after the last turn means the
    delivery was built with other tools than the ones it was verified with. A change during the
    verification never reaches a report: its results are not kept."""
    verified = None if report.tools is None else report.tools.fingerprint
    runs = report.tools_of_turns
    if verified is None or all(found == verified for found, _turns in runs):
        return "", []
    chain = " → ".join(
        f"{_code(found)} ({turns} turn{'' if turns == 1 else 's'})" for found, turns in runs
    )
    if runs[-1][0] == verified:
        return (
            "The project tools changed between two turns before this delivery (the "
            "fingerprints the turns found, in order; the last turns found the tools verified):",
            [chain],
        )
    return (
        "The project tools changed after the last turn before this delivery: it was built with "
        "other tools than the ones it was verified with (the fingerprints the turns found, in "
        "order, then the verification's):",
        [f"{chain} → {_code(verified)} (the verification)"],
    )


def render(report: ReportInput, secrets: Iterable[str] = (), *, with_output: bool = True) -> str:
    """The evidence report. Without the output of the run (`with_output` false), it is the
    report the Contractor reads: the output shows the Owner side's acceptance tests, the outcome
    of each test does not."""
    gate = report.gate
    delivery = report.delivery
    lines = [
        f"# Evidence report: {_plain(report.project_name)}, delivery {delivery.delivery_id}",
        "",
    ]
    if report.simulation:
        lines += [
            "**SIMULATION** — carried with the simulated model provider (EXG-46): no model call "
            "left this machine.",
            "",
        ]
    below = [
        (compaction, turns)
        for _tokens, _window, compaction, turns in report.context_of_turns
        if compaction < CONTEXT_FLOOR
    ]
    if below and not report.simulation:  # the simulated provider has no context to lose
        counted = sum(turns for _compaction, turns in below)
        lowest = min(compaction for compaction, _turns in below)
        lines += [
            f"**CONTEXT BELOW THE FLOOR** — {counted} turn{'' if counted == 1 else 's'} before "
            f"this delivery compacted at {lowest} tokens or less, below {CONTEXT_FLOOR} "
            f"(ADR 0033): {FLOOR_REASON}.",
            "",
        ]
    lines += [
        f"**Decision: {gate.decision.value}**, computed by the gate of the hub from the recorded "
        "results. The Contractor never renders a verdict.",
        "",
        "## Delivery",
        "",
        f"- Branch: {_code(delivery.branch)}",
        f"- Base: {_code(delivery.base[:12])}, head: {_code(delivery.head[:12])}",
        f"- Contract: version {report.contract_version} ({_code(report.contract_hash[:12])})",
        "- Built by the agent of the Contractor: "
        + (
            ", ".join(
                f"{_code(engine)} ({turns} turn{'' if turns == 1 else 's'})"
                for engine, turns in report.built_by
            )
            or "no turn of the Contractor recorded"
        ),
        *_tools_lines(report.tools, bool(report.outcomes)),
        *_context_lines(report.context_of_turns),
        f"- Files of the Owner side in the tree verified ({len(report.acceptance_files)}): "
        + (
            ", ".join(
                f"{_code(path)} ({_code(digest)}"
                + (", named by no criterion)" if path in report.unnamed_acceptance_files else ")")
                for path, digest in report.acceptance_files
            )
            or "none"
        ),
        f"- Changed paths ({len(delivery.changed_paths)}): "
        + (", ".join(_code(p) for p in delivery.changed_paths[:50]) or "none")
        + (" ..." if len(delivery.changed_paths) > 50 else ""),
        "",
        "## Requirements",
        "",
        "| Requirement or constraint | Covered by | Priority | Methods | Acceptance tests "
        "| Verified | Why |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for judged in gate.requirements:
        tests = (
            "<br>".join(f"{_code(t)} {_outcome(t, o, report.timeouts)}" for t, o in judged.tests)
            or "none"
        )
        stories = "<br>".join(_code(s) for s in judged.stories) or "none"
        lines.append(
            f"| {judged.id} | {stories} | {judged.priority} | {', '.join(judged.methods)} | "
            f"{tests} | {'yes' if judged.verified else 'no'} | {_plain(judged.why)} |"
        )
    lines += [
        "",
        "## Gate rules",
        "",
        "| Rule | Required | Verified | Met |",
        "| --- | --- | --- | --- |",
    ]
    for check in gate.checks:
        lines.append(
            f"| {check.name} | {check.required_percent} % | {check.verified} of {check.total} | "
            f"{'yes' if check.met else 'no'} |"
        )
    lines += ["", f"## Verification (kit: {_plain(report.kit)})", ""]
    lines += _stopped_lines(report)
    for test, outcome in report.outcomes.items():
        found = report.timeouts.get(test)
        lines.append(
            f"- {_code(test)}: {outcome.value}"
            + ("" if found is None else f", **timed out**: {_time_limit(found, report.stopped_at)}")
        )
    if with_output:
        excerpt = report.log[-LOG_EXCERPT:]
        lines += ["", "Output of the run (the end of it):", "", _fence(excerpt), ""]
    else:
        lines += ["", "The output of the run is kept for the Owner side: it shows its tests.", ""]
    lines += ["## Flags", ""]
    flags = [
        (
            "The delivery changes the client's own files: the Owner side reviews these changes.",
            [_code(path) for path in report.flagged_paths],
        ),
        (
            "Tests that no criterion names, in the files the kit ran: they never ran.",
            [_code(test) for test in report.never_run_tests[:LISTED_TESTS]]
            + (
                [f"and {len(report.never_run_tests) - LISTED_TESTS} more"]
                if len(report.never_run_tests) > LISTED_TESTS
                else []
            ),
        ),
        _tools_flag(report),
        (
            "Acceptance test files that a criterion names and neither the Owner side nor the "
            "base holds (their tests cannot pass):",
            [
                _code(path)
                + (
                    " (the delivered version was set aside)"
                    if path in report.set_aside_paths
                    else ""
                )
                for path in report.missing_acceptance_files
            ],
        ),
    ]
    shown = [(intro, items) for intro, items in flags if items]
    for number, (intro, items) in enumerate(shown):
        lines += ([""] if number else []) + [intro, ""] + [f"- {item}" for item in items]
    if not shown:
        lines += ["None."]
    lines += ["", "## Obstacles", ""]
    lines += _obstacles_lines(report.obstacles, owner_side=with_output)
    lines += [
        "",
        "## Automated delivery checks",
        "",
        "Run." if report.checks_run else "Not run by this version of the hub (EXG-35).",
        "",
        "## Reservations",
        "",
    ]
    lines += [f"- {_plain(r)}" for r in gate.reservations] or ["None."]
    lines += [
        "",
        "## Cost",
        "",
        f"{report.cost.model_calls} model calls, {report.cost.prompt_tokens} prompt tokens, "
        f"{report.cost.completion_tokens} completion tokens.",
        "",
        f"Produced by {_plain(report.produced_by)} at {report.produced_at}.",
        "",
    ]
    return mask("\n".join(lines), secrets)


def inspection_addendum(
    inspections: Mapping[str, Mapping[str, str]],
    previous: str | None,
    gate: GateResult,
    secrets: Iterable[str] = (),
) -> str:
    """The addendum of a report: every inspection recorded after the gate, and the decision they
    lead to (ADR 0020). It is kept apart from the report the gate wrote and written whole at each
    inspection: nothing is ever looked for in the report, whose text the delivery partly chose
    (ATK-23)."""
    lines = [
        INSPECTIONS_HEADING,
        "",
        "| Object | Verdict | Observation | By | At |",
        "| --- | --- | --- | --- | --- |",
    ]
    for key in sorted(inspections):
        found = inspections[key]
        verdict = "conforms" if found.get("verdict") == "conforms" else "does not conform"
        lines.append(
            f"| {_plain(key)} | {verdict} | {_code(found.get('observation', ''))} | "
            f"{_plain(found.get('by', ''))} | {_plain(found.get('at', ''))} |"
        )
    lines += [
        "",
        f"**Decision now: {gate.decision.value}** (it was {_plain(previous or 'none')} before "
        "the last inspection), computed by the gate from the same results and the inspections "
        "above.",
        "",
        "Reservations now:",
        "",
    ]
    lines += [f"- {_plain(r)}" for r in gate.reservations] or ["None."]
    lines.append("")
    return mask("\n".join(lines), secrets)


def with_addendum(report: str, addendum: str | None) -> str:
    """The report as the gate wrote it, then its addendum when there is one."""
    if not addendum:
        return report
    return f"{report.rstrip(chr(10))}\n\n{addendum}"
