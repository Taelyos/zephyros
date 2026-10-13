"""The evidence report of a delivery: what was verified, how, and what the gate decided.

It is attached to the pull request (EXG-14), which is sometimes public, so every secret is masked
(EXG-49): the secrets the hub knows of, and anything shaped like a token. It holds text that the
delivery chose (paths, the output of its tests): such text is shown as code, never as markup.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from zephyros.core.gate import GateResult
from zephyros.core.masking import mask_text
from zephyros.core.ports.delivery import Delivery
from zephyros.core.ports.verification import Outcome

LOG_EXCERPT = 4000
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
    """The acceptance tests of the Owner side the run used, with the digest of their content."""
    unnamed_acceptance_files: tuple[str, ...] = ()
    """The acceptance test files of the Owner side that no criterion names: they never run."""
    missing_acceptance_files: tuple[str, ...] = ()
    """The files a criterion names that neither the Owner side nor the base holds (T02)."""
    set_aside_paths: tuple[str, ...] = ()
    """Those of the missing files the delivery brought: its versions were set aside, unrun."""


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


def render(report: ReportInput, secrets: Iterable[str] = (), *, with_output: bool = True) -> str:
    """The evidence report. Without the output of the run (`with_output` false), it is the
    report the Contractor reads: the output shows the Owner side's acceptance tests, the outcome
    of each test does not (T07)."""
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
    lines += [
        f"**Decision: {gate.decision.value}**, computed by the gate of the hub from the recorded "
        "results. The Contractor never renders a verdict.",
        "",
        "## Delivery",
        "",
        f"- Branch: {_code(delivery.branch)}",
        f"- Base: {_code(delivery.base[:12])}, head: {_code(delivery.head[:12])}",
        f"- Contract: version {report.contract_version} ({_code(report.contract_hash[:12])})",
        f"- Acceptance tests of the Owner side ({len(report.acceptance_files)}): "
        + (
            ", ".join(
                f"{_code(path)} ({_code(digest)})" for path, digest in report.acceptance_files
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
        tests = "<br>".join(f"{_code(t)} {o.value}" for t, o in judged.tests) or "none"
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
    lines += [f"- {_code(test)}: {outcome.value}" for test, outcome in report.outcomes.items()]
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
            "Acceptance test files that no criterion names (they never run):",
            [_code(path) for path in report.unnamed_acceptance_files],
        ),
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
    (T11)."""
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
