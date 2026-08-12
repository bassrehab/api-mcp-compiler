"""Human review report for a semantic plan.

The approval gate depends on a human, and a human cannot approve what they cannot read. This
renders every proposed decision, its rationale and its confidence into Markdown, ordered
deterministically so two runs over the same plan produce the same document and a reviewer can
diff one review against the next.
"""

from __future__ import annotations

from api_mcp_compiler.models import (
    ApiSemanticIR,
    DecisionKind,
    DecisionOrigin,
    PlanDecision,
    ToolPlan,
)

_SECTION_TITLES = {
    DecisionKind.RENAME: "Renames",
    DecisionKind.RECLASSIFY: "Surface kind changes",
    DecisionKind.OMIT: "Proposed omissions",
    DecisionKind.GROUP: "Grouping",
    DecisionKind.PROJECT: "Output projections",
    DecisionKind.COMPOSE: "Composite workflows",
    DecisionKind.APPROVE: "Approvals",
}

#: Sections a reviewer should read first, because acting on them changes what an agent can do.
_ORDER = (
    DecisionKind.OMIT,
    DecisionKind.COMPOSE,
    DecisionKind.RECLASSIFY,
    DecisionKind.RENAME,
    DecisionKind.PROJECT,
    DecisionKind.GROUP,
    DecisionKind.APPROVE,
)


def _escape(text: str) -> str:
    """Neutralise pipes so a rationale cannot break the table it sits in."""
    return text.replace("|", "\\|").replace("\n", " ")


def _rows(decisions: list[PlanDecision]) -> list[str]:
    rows: list[str] = []
    for item in sorted(decisions, key=lambda entry: (entry.target, entry.rationale)):
        change = ""
        if item.previous_value or item.proposed_value:
            change = f"`{item.previous_value or '-'}` to `{item.proposed_value or '-'}`"
        elif item.members:
            change = ", ".join(f"`{member}`" for member in item.members)
        status = "applied" if item.applied else "proposed, not applied"
        rows.append(
            f"| `{item.target}` | {change} | {item.origin.value} | {item.confidence} | "
            f"{status} | {_escape(item.rationale)} |"
        )
    return rows


def review_report(ir: ApiSemanticIR, plan: ToolPlan) -> str:
    """Render a deterministic Markdown review of a plan's decisions."""
    lines: list[str] = [
        f"# Tool surface review: {ir.service.title}",
        "",
        "Every decision below is a proposal unless it is marked applied. A planner decision",
        "carries a confidence below 1.0; a decision a reviewer already recorded carries 1.0.",
        "",
        "## Summary",
        "",
        "| Item | Count |",
        "|---|---|",
        f"| Source operations | {len(ir.operations)} |",
        f"| Planned artifacts | {len(plan.artifacts)} |",
        f"| Decisions | {len(plan.decisions)} |",
        f"| Awaiting a reviewer | "
        f"{sum(1 for item in plan.decisions if not item.applied)} |",
        f"| Blocking ambiguities | {len(ir.blocking_ambiguities)} |",
        "",
        "## Planned surface",
        "",
        "| Name | Kind | Risk | Group | Suitability | Review |",
        "|---|---|---|---|---|---|",
    ]
    for artifact in sorted(plan.artifacts, key=lambda item: item.name):
        score = "n/a" if artifact.confidence is None else f"{artifact.confidence}"
        lines.append(
            f"| `{artifact.name}` | {artifact.kind.value} | {artifact.risk.value} | "
            f"{artifact.group or '-'} | {score} | {artifact.review_status.value} |"
        )

    for kind in _ORDER:
        decisions = [item for item in plan.decisions if item.kind is kind]
        if not decisions:
            continue
        lines.extend(
            [
                "",
                f"## {_SECTION_TITLES[kind]}",
                "",
                "| Target | Change | Origin | Confidence | Status | Rationale |",
                "|---|---|---|---|---|---|",
                *_rows(decisions),
            ]
        )

    outstanding = [item for item in plan.decisions if not item.applied]
    lines.extend(["", "## What a reviewer must decide", ""])
    if not outstanding:
        lines.append("Every proposal has been recorded in the overlay.")
    else:
        for item in sorted(outstanding, key=lambda entry: (entry.kind.value, entry.target)):
            lines.append(f"- **{item.kind.value}** `{item.target}`: {_escape(item.rationale)}")

    human = [item for item in plan.decisions if item.origin is DecisionOrigin.HUMAN]
    lines.extend(
        [
            "",
            f"Decisions already accepted by a reviewer: {len(human)}.",
            "",
        ]
    )
    return "\n".join(lines)
