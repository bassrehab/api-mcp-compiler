"""Operation-per-tool baseline planner.

This planner exists only to give evaluation a controlled comparison point. It performs no
semantic design: no renaming, grouping, composition, schema simplification or omission. Its
output is deliberately a faithful mirror of the IR, so that any measured difference against
the semantic planner is attributable to semantic design rather than to incidental
differences in how the two surfaces were built.

Every artifact is emitted as `proposed`, and the risk class is the identity mapping of the
operation's side-effect class. The baseline never assigns `privileged`.
"""

from __future__ import annotations

from api_mcp_compiler.models import (
    SIDE_EFFECT_TO_RISK,
    ApiSemanticIR,
    ArtifactKind,
    Derivation,
    OperationIR,
    PlannerKind,
    Provenance,
    ReviewStatus,
    ToolArtifact,
    ToolPlan,
)

PLANNER_RULE_PREFIX = "baseline.operation_per_tool"

_RATIONALE = (
    "Operation-per-tool baseline. Emitted for controlled comparison against the semantic "
    "planner; not an approved production design."
)


def _artifact(operation: OperationIR) -> ToolArtifact:
    """Wrap one IR operation as one proposed tool artifact."""
    pointer = operation.source_pointer
    description = operation.description or operation.intent
    return ToolArtifact(
        artifact_id=f"tool:{operation.operation_id}",
        kind=ArtifactKind.TOOL,
        name=operation.operation_id,
        description=description,
        source_operations=[operation.operation_id],
        risk=SIDE_EFFECT_TO_RISK[operation.side_effect],
        review_status=ReviewStatus.PROPOSED,
        rationale=_RATIONALE,
        confidence=operation.confidence,
        provenance=[
            Provenance(
                field="artifact_id",
                source_pointer=pointer,
                derivation=Derivation.NORMALIZED,
                rule=f"{PLANNER_RULE_PREFIX}.artifact_id",
            ),
            Provenance(
                field="kind",
                source_pointer=pointer,
                derivation=Derivation.DEFAULT,
                rule=f"{PLANNER_RULE_PREFIX}.kind.tool",
            ),
            Provenance(
                field="name",
                source_pointer=pointer,
                derivation=Derivation.NORMALIZED,
                rule=f"{PLANNER_RULE_PREFIX}.name.operation_id",
            ),
            Provenance(
                field="description",
                source_pointer=pointer,
                derivation=Derivation.NORMALIZED,
                rule=f"{PLANNER_RULE_PREFIX}.description.operation_description"
                if operation.description
                else f"{PLANNER_RULE_PREFIX}.description.operation_intent",
            ),
            Provenance(
                field="source_operations",
                source_pointer=pointer,
                derivation=Derivation.NORMALIZED,
                rule=f"{PLANNER_RULE_PREFIX}.source_operations",
            ),
            Provenance(
                field="risk",
                source_pointer=pointer,
                derivation=Derivation.NORMALIZED,
                rule=f"{PLANNER_RULE_PREFIX}.risk.side_effect_identity",
            ),
            Provenance(
                field="review_status",
                source_pointer=pointer,
                derivation=Derivation.DEFAULT,
                rule=f"{PLANNER_RULE_PREFIX}.review_status.proposed",
            ),
            Provenance(
                field="rationale",
                source_pointer=pointer,
                derivation=Derivation.DEFAULT,
                rule=f"{PLANNER_RULE_PREFIX}.rationale",
            ),
        ],
    )


def operation_per_tool(ir: ApiSemanticIR) -> list[ToolArtifact]:
    """Return one proposed tool artifact per IR operation, in source order."""
    return [_artifact(operation) for operation in ir.operations]


def plan_baseline(ir: ApiSemanticIR) -> ToolPlan:
    """Build the complete baseline tool plan document for a service.

    The plan carries the source digest so a reviewer can tell whether it was compiled from
    the specification revision currently on disk.
    """
    return ToolPlan(
        service_id=ir.service.service_id,
        planner=PlannerKind.BASELINE,
        source_digest=ir.service.source_digest,
        artifacts=operation_per_tool(ir),
    )
