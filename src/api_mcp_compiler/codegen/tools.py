"""Tool descriptor generation and the emission safety gate.

The gate is the point at which the compiler's safety posture becomes behaviour rather than
metadata. A tool becomes executable only when its source operation carries no blocking
ambiguity, its risk is classified, and any write, destructive or privileged artifact has
been approved by a human.

A refused tool is still emitted, carrying its blockers. Omitting it would make it
indistinguishable from one that was never planned, which is the same silent-loss failure
the ingestion completeness sweep exists to prevent.
"""

from __future__ import annotations

from api_mcp_compiler.codegen.composite import composite_threading
from api_mcp_compiler.codegen.schema import (
    ArgumentCollisionError,
    compose_input_schema,
    compose_output_schema,
)
from api_mcp_compiler.models import (
    Ambiguity,
    ApiSemanticIR,
    ArgumentBinding,
    ConfirmationPolicy,
    DecisionKind,
    DecisionOrigin,
    Derivation,
    EmissionBlocker,
    EmissionStatus,
    OperationIR,
    PolicyManifest,
    Provenance,
    ReviewStatus,
    RiskClass,
    SideEffectClass,
    ToolArtifact,
    ToolDescriptor,
    ToolPlan,
    ToolPolicy,
    ToolSurface,
)

#: Risk classes that may never be emitted executable without an explicit human approval.
APPROVAL_REQUIRED_RISKS = frozenset(
    {RiskClass.WRITE, RiskClass.DESTRUCTIVE, RiskClass.PRIVILEGED}
)

_BLOCKER_DETAIL = {
    EmissionBlocker.BLOCKING_AMBIGUITY: (
        "the source operation carries an unresolved construct that must be settled first"
    ),
    EmissionBlocker.AWAITING_APPROVAL: (
        "a write, destructive or privileged tool requires explicit human approval"
    ),
    EmissionBlocker.REJECTED: "the plan artifact was rejected during review",
    EmissionBlocker.UNCLASSIFIED_RISK: (
        "the side effect could not be classified, so the tool cannot be shown to be safe"
    ),
    EmissionBlocker.ARGUMENT_NAME_COLLISION: (
        "two inputs compose to the same argument name, so the input schema would be lossy"
    ),
    EmissionBlocker.COMPOSITE_PENDING_CONFIRMATION: (
        "a composite spans an irreversible step and carries no confirmation requirement"
    ),
    EmissionBlocker.POLICY_UNRESOLVED: (
        "policy metadata could not be derived, so the tool cannot be shown to be governed"
    ),
}


class SurfaceGenerationError(ValueError):
    """Raised when a plan cannot be generated against the IR it references."""


def _blocking_operations(ir: ApiSemanticIR) -> dict[str, list[Ambiguity]]:
    """Map operation identifier to the blocking ambiguities that name it.

    Ambiguity fields are dotted paths such as `operations.<id>.side_effect`, so the second
    segment identifies the operation when one is named at all.
    """
    found: dict[str, list[Ambiguity]] = {}
    known = {item.operation_id for item in ir.operations}
    for item in ir.blocking_ambiguities:
        parts = item.field.split(".")
        if len(parts) >= 2 and parts[0] == "operations" and parts[1] in known:
            found.setdefault(parts[1], []).append(item)
    return found


def _document_level_blockers(ir: ApiSemanticIR) -> list[Ambiguity]:
    """Blocking ambiguities that name no single operation and so affect the whole surface."""
    known = {item.operation_id for item in ir.operations}
    return [
        item
        for item in ir.blocking_ambiguities
        if not (
            item.field.startswith("operations.")
            and len(item.field.split(".")) >= 2
            and item.field.split(".")[1] in known
        )
    ]


def _descriptor(
    artifact: ToolArtifact,
    operations: list[OperationIR],
    blocking: list[Ambiguity],
    surface_wide: list[Ambiguity],
    policy: ToolPolicy | None,
    policy_required: bool,
) -> ToolDescriptor:
    """Build one tool descriptor and decide whether it may be executable."""
    blockers: list[EmissionBlocker] = []
    bindings: list[ArgumentBinding] = []
    operation = operations[0]
    threaded = composite_threading(operations)
    try:
        input_schema, bindings = compose_input_schema(
            *operations,
            omit=frozenset(artifact.omitted_arguments),
            supplied=frozenset(threaded),
        )
    except ArgumentCollisionError:
        blockers.append(EmissionBlocker.ARGUMENT_NAME_COLLISION)
        input_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    confirmation: ConfirmationPolicy | None = policy.confirmation if policy else None
    spans_a_change = any(item.side_effect is not SideEffectClass.READ for item in operations)
    unconfirmed = confirmation is None or not confirmation.required
    if len(operations) > 1 and spans_a_change and unconfirmed:
        # Decision: a composite that spans an irreversible step may only become
        # executable once a confirmation requirement is what stands between an agent and that
        # step; without one, exposing it removes the guard it was created for.
        #
        # The condition used to be every composite, which was written when a composite could
        # only be a prepare-then-commit pair. A composite of pure reads guards nothing, so
        # demanding confirmation for it asked a reviewer to authorise a risk that is not there
        # and blocked the whole read-only surface.
        blockers.append(EmissionBlocker.COMPOSITE_PENDING_CONFIRMATION)
    if policy_required and (policy is None or policy.unresolved):
        blockers.append(EmissionBlocker.POLICY_UNRESOLVED)
    if blocking or surface_wide:
        blockers.append(EmissionBlocker.BLOCKING_AMBIGUITY)
    if artifact.review_status is ReviewStatus.REJECTED:
        blockers.append(EmissionBlocker.REJECTED)
    if artifact.risk is RiskClass.UNKNOWN:
        blockers.append(EmissionBlocker.UNCLASSIFIED_RISK)
    if (
        artifact.risk in APPROVAL_REQUIRED_RISKS
        and artifact.review_status is not ReviewStatus.APPROVED
    ):
        blockers.append(EmissionBlocker.AWAITING_APPROVAL)

    ordered = sorted(set(blockers), key=lambda item: item.value)
    detail = (
        "; ".join(_BLOCKER_DETAIL[item] for item in ordered) if ordered else None
    )
    records = [
        Provenance(
            field=name,
            source_pointer=operation.source_pointer,
            derivation=Derivation.NORMALIZED,
            rule=f"codegen.tool.{name}",
        )
        for name in (
            "tool_id",
            "name",
            "description",
            "kind",
            "risk",
            "emission",
            "input_schema",
            "source_operations",
        )
    ]
    if ordered:
        records.append(
            Provenance(
                field="blockers",
                source_pointer=operation.source_pointer,
                derivation=Derivation.NORMALIZED,
                rule="codegen.tool.emission_gate",
            )
        )
        records.append(
            Provenance(
                field="blocker_detail",
                source_pointer=operation.source_pointer,
                derivation=Derivation.NORMALIZED,
                rule="codegen.tool.emission_gate.detail",
            )
        )
    output_schema = compose_output_schema(operation)
    if output_schema is not None:
        records.append(
            Provenance(
                field="output_schema",
                source_pointer=operation.source_pointer,
                derivation=Derivation.NORMALIZED,
                rule="codegen.tool.output_schema",
            )
        )

    return ToolDescriptor(
        tool_id=artifact.artifact_id,
        name=artifact.name,
        description=artifact.description,
        kind=artifact.kind,
        risk=artifact.risk,
        emission=EmissionStatus.DISABLED if ordered else EmissionStatus.EXECUTABLE,
        blockers=ordered,
        blocker_detail=detail,
        input_schema=input_schema,
        output_schema=output_schema,
        argument_bindings=bindings,
        source_operations=list(artifact.source_operations),
        provenance=records,
    )


def generate_surface(
    ir: ApiSemanticIR, plan: ToolPlan, manifest: PolicyManifest | None = None
) -> ToolSurface:
    """Generate a tool surface from an IR, a plan that references it, and its policy.

    When a manifest is supplied, generation fails closed: a tool with no policy, or with
    unresolved policy metadata, is disabled rather than emitted with defaults. A defaulted
    policy is indistinguishable from a derived one once written.

    Raises `SurfaceGenerationError` when the plan or manifest was compiled from different
    specification bytes than the IR, so neither can be silently applied to a revision it was
    not reviewed against.
    """
    if plan.source_digest != ir.service.source_digest:
        raise SurfaceGenerationError(
            f"plan was compiled from {plan.source_digest} but the IR is "
            f"{ir.service.source_digest}; regenerate the plan before generating a surface"
        )
    if manifest is not None and manifest.source_digest != ir.service.source_digest:
        raise SurfaceGenerationError(
            f"policy manifest was derived from {manifest.source_digest} but the IR is "
            f"{ir.service.source_digest}; re-derive the policy before generating a surface"
        )

    operations = {item.operation_id: item for item in ir.operations}
    blocking = _blocking_operations(ir)
    surface_wide = _document_level_blockers(ir)
    # A reviewer who classified an operation has answered the question that blocked it. The
    # ambiguity stays in the IR, because the specification really does not say; what changes
    # is that somebody accountable has now said.
    classified = {
        item.target
        for item in plan.decisions
        if item.kind is DecisionKind.RECLASSIFY and item.origin is DecisionOrigin.HUMAN
    }
    for operation_id in classified:
        remaining = [
            item
            for item in blocking.get(operation_id, [])
            if item.code != "unclassified_side_effect"
        ]
        if remaining:
            blocking[operation_id] = remaining
        else:
            blocking.pop(operation_id, None)

    tools: list[ToolDescriptor] = []
    for artifact in plan.artifacts:
        if not artifact.source_operations:
            raise SurfaceGenerationError(
                f"artifact {artifact.artifact_id!r} references no source operation"
            )
        sources: list[OperationIR] = []
        for source in artifact.source_operations:
            operation = operations.get(source)
            if operation is None:
                raise SurfaceGenerationError(
                    f"artifact {artifact.artifact_id!r} references unknown operation {source!r}"
                )
            sources.append(operation)
        related = [
            item
            for source in artifact.source_operations
            for item in blocking.get(source, [])
        ]
        tools.append(
            _descriptor(
                artifact,
                sources,
                related,
                surface_wide,
                manifest.policy_for(artifact.artifact_id) if manifest else None,
                manifest is not None,
            )
        )

    return ToolSurface(
        service_id=plan.service_id,
        planner=plan.planner,
        source_digest=plan.source_digest,
        tools=tools,
    )
