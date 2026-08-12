"""Granting approval by class rather than one tool at a time.

The emission gate is a safety property, and so is the effort it takes to pass it. A reviewer
clicking through twenty-three read tools individually is doing data entry, not governance, and
a gate that is drudgery is one that gets bypassed wholesale rather than carefully.

So approval is granted over a selection a person can actually reason about: every read, one
group, one risk class. What the selection covered is reported back, because a reviewer
who cannot see what they just approved has not really approved it.

Selecting is deliberately not the same as approving everything. There is no flag here that
approves a whole surface without naming what class it belongs to.
"""

from __future__ import annotations

from dataclasses import dataclass

from api_mcp_compiler.models import (
    ArtifactKind,
    OverlayEntry,
    ReviewStatus,
    RiskClass,
    ToolArtifact,
    ToolOverlay,
    ToolPlan,
)


class ApprovalSelectionError(ValueError):
    """Raised when a selection names nothing, or names something that does not exist."""


@dataclass(frozen=True)
class ApprovalOutcome:
    """What an approval covered, so a reviewer can see what they granted."""

    overlay: ToolOverlay
    approved: list[str]
    already_approved: list[str]
    untouched: list[str]


def _selected(
    plan: ToolPlan,
    risk: RiskClass | None,
    group: str | None,
    names: list[str],
) -> list[ToolArtifact]:
    """Resolve a selection, refusing one that matches nothing."""
    if risk is None and group is None and not names:
        raise ApprovalSelectionError(
            "name what is being approved: --risk, --group, or one or more tool names. There "
            "is deliberately no flag that approves a surface without saying what class of "
            "thing it is."
        )
    candidates = [item for item in plan.artifacts if item.kind is not ArtifactKind.OMITTED]
    if risk is not None:
        candidates = [item for item in candidates if item.risk is risk]
    if group is not None:
        candidates = [item for item in candidates if item.group == group]
    if names:
        wanted = set(names)
        known = {item.name for item in plan.artifacts}
        unknown = sorted(wanted - known)
        if unknown:
            raise ApprovalSelectionError(
                f"no artifact named {', '.join(unknown)} in this plan. Run `report` to see "
                "what the surface actually contains."
            )
        candidates = [item for item in candidates if item.name in wanted]
    if not candidates:
        raise ApprovalSelectionError(
            "that selection matches nothing in this plan, so approving it would record a "
            "decision about no tool at all."
        )
    return candidates


def approve(
    plan: ToolPlan,
    *,
    overlay: ToolOverlay | None = None,
    risk: RiskClass | None = None,
    group: str | None = None,
    names: list[str] | None = None,
) -> ApprovalOutcome:
    """Record approval for a class of artifacts, returning the overlay to save.

    An existing overlay is extended rather than replaced, so an earlier decision is never lost
    by approving something else later.
    """
    selection = _selected(plan, risk, group, list(names or []))
    by_operation = {
        item.source_operations[0]: item for item in selection if item.source_operations
    }

    existing = list(overlay.entries) if overlay else []
    index = {item.operation_id: item for item in existing}
    approved: list[str] = []
    already: list[str] = []

    for operation_id, artifact in by_operation.items():
        current = index.get(operation_id)
        if current is not None and current.review_status is ReviewStatus.APPROVED:
            already.append(artifact.name)
            continue
        if current is None:
            index[operation_id] = OverlayEntry(
                operation_id=operation_id, review_status=ReviewStatus.APPROVED
            )
        else:
            index[operation_id] = current.model_copy(
                update={"review_status": ReviewStatus.APPROVED}
            )
        approved.append(artifact.name)

    updated = ToolOverlay(
        service_id=overlay.service_id if overlay else plan.service_id,
        source_digest=overlay.source_digest if overlay else plan.source_digest,
        entries=sorted(index.values(), key=lambda item: item.operation_id),
        composites=list(overlay.composites) if overlay else [],
    )
    covered = {item.name for item in selection}
    untouched = sorted(
        item.name
        for item in plan.artifacts
        if item.name not in covered and item.review_status is ReviewStatus.PROPOSED
    )
    return ApprovalOutcome(
        overlay=updated,
        approved=sorted(approved),
        already_approved=sorted(already),
        untouched=untouched,
    )
