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


def render(report: ReportInput, secrets: Iterable[str] = ()) -> str:
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
    excerpt = report.log[-LOG_EXCERPT:]
    lines += ["", "Output of the run (the end of it):", "", _fence(excerpt), ""]
    lines += ["## Flags", ""]
    if not report.flagged_paths and not report.unnamed_acceptance_files:
        lines += ["None."]
    else:
        if report.flagged_paths:
            lines += [
                "The delivery changes the client's own files: the Owner side reviews these "
                "changes.",
                "",
            ]
            lines += [f"- {_code(path)}" for path in report.flagged_paths]
        if report.unnamed_acceptance_files:
            if report.flagged_paths:
                lines += [""]
            lines += [
                "Acceptance test files that no criterion names (they never run):",
                "",
            ]
            lines += [f"- {_code(path)}" for path in report.unnamed_acceptance_files]
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


def with_inspections(
    report: str,
    inspections: Mapping[str, Mapping[str, str]],
    previous: str | None,
    gate: GateResult,
    secrets: Iterable[str] = (),
) -> str:
    """The report of a delivery with its inspections and the decision they lead to (ADR 0020).

    The inspections come after the gate: the report keeps what the gate saw, and an addendum,
    rewritten at each inspection, lists every inspection and the decision now."""
    body = report.split("\n" + INSPECTIONS_HEADING + "\n", 1)[0].rstrip("\n")
    lines = [
        body,
        "",
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
