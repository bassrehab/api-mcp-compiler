"""Semantic tool-surface planner.

The baseline mirrors the API. This planner designs a surface for an agent instead, and
exists to avoid three failures that mirroring produces: tools named after generated operation
identifiers, internal identifiers an agent has no way to discover, and entire enterprise
payloads returned into model context.

Two properties hold throughout.

Every decision is a proposal carrying a rationale and a confidence, because the acceptance
criteria require one for every merge, omission, rename and composite, and because a reviewer
cannot accept what it cannot read.

Nothing here is non-deterministic. Proposals are pure functions of the IR and the overlay, so
a reviewed surface rebuilds byte for byte. Model-generated proposals are deliberately absent:
their quality cannot be judged before the evaluation harness exists, and claiming a semantic
improvement that has not been measured is exactly what the project's non-goals forbid.
"""

from __future__ import annotations

import re

from api_mcp_compiler.models import (
    SIDE_EFFECT_TO_RISK,
    ApiSemanticIR,
    ArtifactKind,
    DecisionKind,
    DecisionOrigin,
    Derivation,
    OperationIR,
    ParameterLocation,
    PlanDecision,
    PlannerKind,
    Provenance,
    ReviewStatus,
    RiskClass,
    SideEffectClass,
    ToolArtifact,
    ToolOverlay,
    ToolPlan,
)
from api_mcp_compiler.routes import (
    TEMPLATED,
    callable_from_a_goal,
    required_resources,
    segments,
    yielded_collection,
)

PLANNER_RULE_PREFIX = "semantic"

#: Words carrying no task meaning, dropped when a name is derived from a summary.
_STOPWORDS = frozenset(
    {"a", "an", "the", "of", "for", "in", "on", "to", "by", "from", "every", "all", "its"}
)
_MAX_NAME_TOKENS = 5

#: Final path segments that mark an action applied to an already-identified resource. These
#: are what turn two operations into a prepare-then-execute pair rather than two unrelated
#: writes.
_ACTION_SEGMENTS = frozenset(
    {"approve", "confirm", "execute", "submit", "commit", "cancel", "void", "finalize"}
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_WORD = re.compile(r"[^A-Za-z0-9]+")
_TEMPLATED = re.compile(r"\{[^}]*\}")


class OverlayMismatchError(ValueError):
    """Raised when an overlay was reviewed against different specification bytes."""


def _tokens(text: str) -> list[str]:
    """Split a summary or identifier into lowercase words.

    Single letters are dropped. A real specification writes "Get an Artist's Albums", and
    splitting on the apostrophe leaves a stray `s` that turns a name into
    `get_artist_s_albums`. No single letter carries task meaning in a tool name.
    """
    spaced = _CAMEL_BOUNDARY.sub(" ", text)
    return [
        piece.lower()
        for piece in _NON_WORD.split(spaced)
        if piece and not (len(piece) == 1 and piece.isalpha())
    ]


def disambiguate(name: str, operation: OperationIR, taken: set[str]) -> str:
    """Qualify a name that another operation already claimed.

    A summary describes what an operation does but not always what it does it to, so on a
    large API many collapse to the same phrase: TMDB has ten operations whose summary is
    "Get Details". Ten tools called `get_details` is not a surface an agent can choose from,
    and it is not one the API will even accept.

    The route says what the operation acts on, so the resource it names is what tells them
    apart. Qualifying is preferred to numbering, which would distinguish the tools without
    telling a reader anything.
    """
    if name not in taken:
        return name
    resources = [
        item for item in segments(operation.route or "") if not TEMPLATED.match(item)
    ]
    for qualifier in resources:
        candidate = f"{qualifier}_{name}" if not name.startswith(f"{qualifier}_") else name
        if candidate not in taken:
            return candidate
    joined = "_".join(resources)
    candidate = f"{joined}_{name}" if joined else name
    suffix = 2
    while candidate in taken:
        candidate = f"{joined}_{name}_{suffix}" if joined else f"{name}_{suffix}"
        suffix += 1
    return candidate


def derive_name(operation: OperationIR) -> tuple[str, float, str]:
    """Propose a task-oriented name, with a confidence and the rationale for it.

    A summary describes what the operation does for a user, so it is the better source. An
    operation identifier is a fallback and is scored lower, because naming a tool after a
    generated identifier is the anti-pattern this planner exists to avoid.
    """
    summary_derived = operation.intent != operation.operation_id
    source = operation.intent if summary_derived else operation.operation_id
    words = [item for item in _tokens(source) if item not in _STOPWORDS]
    if not words:
        words = _tokens(operation.operation_id) or ["operation"]
    name = "_".join(words[:_MAX_NAME_TOKENS])
    if summary_derived:
        return (
            name,
            0.75,
            f"Derived from the operation summary {operation.intent!r} rather than the source "
            f"identifier {operation.operation_id!r}, which names a tool after the API instead "
            "of the task.",
        )
    return (
        name,
        0.4,
        f"No summary was available, so the name falls back to the source identifier "
        f"{operation.operation_id!r}. A reviewer should replace it with a task-oriented name.",
    )


def derive_kind(operation: OperationIR) -> tuple[ArtifactKind, float, str]:
    """Propose whether an operation is better exposed as a tool or a resource.

    A read whose only inputs identify what to fetch is addressable, which is what a resource
    is for. Anything taking a filter, a page or a body is an action and stays a tool.
    """
    if operation.side_effect is not SideEffectClass.READ:
        return (
            ArtifactKind.TOOL,
            0.9,
            "The operation changes state, so it is an action rather than something "
            "addressable.",
        )
    non_path = [
        item for item in operation.inputs if item.location is not ParameterLocation.PATH
    ]
    if non_path:
        names = ", ".join(sorted(item.name for item in non_path))
        return (
            ArtifactKind.TOOL,
            0.7,
            f"A read taking non-identifying inputs ({names}) is a query rather than an "
            "addressable resource.",
        )
    return (
        ArtifactKind.RESOURCE,
        0.6,
        "A read whose only inputs identify what to fetch is addressable, so a resource "
        "avoids spending a tool slot on a lookup.",
    )


def derive_group(operation: OperationIR) -> tuple[str, float, str]:
    """Propose a grouping key so a surface does not present as a flat list of endpoints."""
    if operation.route:
        segments = [
            segment
            for segment in operation.route.split("/")
            if segment and not _TEMPLATED.fullmatch(segment)
        ]
        if segments:
            return (
                segments[0],
                0.65,
                f"Grouped by the first path segment {segments[0]!r}, which is the coarsest "
                "grouping the specification states rather than one this planner invents.",
            )
    if operation.soap is not None:
        return (
            operation.soap.port_type,
            0.65,
            f"Grouped by the SOAP port type {operation.soap.port_type!r}, which plays the "
            "role a path prefix plays for HTTP.",
        )
    return (
        "ungrouped",
        0.3,
        "Neither a path nor a port type was available, so no meaningful grouping could be "
        "derived.",
    )


def derive_projection(operation: OperationIR) -> tuple[list[str], float, str] | None:
    """Propose which top-level response fields to keep.

    Returning an entire enterprise payload into model context is an explicit anti-pattern.
    Required fields are the author's own statement of what always matters, so they are the
    defensible projection to propose; anything finer is a judgement a reviewer should make.
    """
    success = [item for item in operation.outputs if item.status[:1] == "2"]
    schema = next((item.type_schema for item in success if item.type_schema), None)
    if not schema:
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return None
    required = schema.get("required")
    if not isinstance(required, list) or not required:
        return None
    kept = sorted(str(item) for item in required if str(item) in properties)
    if not kept or len(kept) == len(properties):
        return None
    dropped = sorted(set(properties) - set(kept))
    return (
        kept,
        0.55,
        f"Projects the {len(kept)} field(s) the response declares required and drops "
        f"{', '.join(dropped)}, so an agent is not handed the whole payload. Confirm nothing "
        "dropped is needed downstream.",
    )


#: Arguments that carry transport concerns rather than task concerns. Naming is a convention,
#: so this tier is proposed at lower confidence than a declared default and a reviewer decides.
TRANSPORT_ARGUMENTS = frozenset(
    {
        "limit", "offset", "page", "per_page", "cursor", "after", "before",
        "market", "locale", "country", "region", "language",
        "include_external", "additional_types", "fields",
    }
)

_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def derive_argument_projection(
    operation: OperationIR,
) -> tuple[list[str], float, str] | None:
    """Propose which input arguments to withhold from an agent.

    An agent asked to follow an artist has no business reasoning about a market code, and
    every argument it can set is an argument it can set wrongly. Two tiers are proposed, and
    both are confined to optional arguments so nothing the service requires can be withheld.

    A declared default is the author's own statement that a caller need not supply the value,
    which makes it the defensible tier. A transport-concern name is a convention, so it is
    proposed less confidently and left for a reviewer.
    """
    declared: list[str] = []
    conventional: list[str] = []
    for field in operation.inputs:
        if field.required or field.location is ParameterLocation.BODY:
            continue
        if isinstance(field.type_schema, dict) and "default" in field.type_schema:
            declared.append(field.name)
        elif field.name in TRANSPORT_ARGUMENTS:
            conventional.append(field.name)
    projected = sorted(set(declared) | set(conventional))
    if not projected:
        return None
    if len(projected) == len([item for item in operation.inputs if not item.required]):
        confidence = 0.7
    else:
        confidence = 0.8 if declared and not conventional else 0.6
    parts = []
    if declared:
        parts.append(f"{', '.join(sorted(declared))} declare a default in the specification")
    if conventional:
        parts.append(f"{', '.join(sorted(conventional))} carry transport rather than task concerns")
    return (
        projected,
        confidence,
        "Withholds " + " and ".join(parts) + ". Each is optional and is left off the wire, so "
        "the service applies its own value. Confirm no caller needs to set them explicitly.",
    )


def rewrite_description(operation: OperationIR) -> tuple[str, str] | None:
    """Rewrite a description for the audience that will actually read it.

    A specification's prose is written for a person integrating over HTTP: links into a
    documentation site, markup, and paragraphs about tokens and availability. A model reads it
    inside a tool list, cannot follow a link, and pays for every token. What it needs is the
    action, what comes back, and whether the call changes anything.

    The original is never lost; it stays on the operation in the IR, and the rewrite is a
    decision with a rationale like every other.
    """
    source = (operation.description or "").strip()
    cleaned = _MARKDOWN_LINK.sub(r"\1", source)
    cleaned = _HTML_TAG.sub(" ", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()

    # The summary is not prepended when a description exists. The tool's own name already
    # states the action, so leading with the summary as well pads every entry in the tool list
    # to repeat what the reader just read.
    body = cleaned or (operation.intent or "").strip().rstrip(".")

    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\\s+", body) if item.strip()]
    text = " ".join(sentences[:2]).strip()
    if text and not text.endswith((".", "!", "?")):
        text += "."
    if operation.side_effect is SideEffectClass.DESTRUCTIVE:
        text += " This removes data and cannot be undone."
    elif operation.side_effect is SideEffectClass.WRITE:
        text += " This changes stored state."
    text = _WHITESPACE.sub(" ", text).strip()
    if not text or text == source:
        return None

    removed = []
    if _MARKDOWN_LINK.search(source):
        removed.append("documentation links a model cannot follow")
    if _HTML_TAG.search(source):
        removed.append("markup meant for a rendered page")
    if len(sentences) > 2:
        removed.append("integration prose beyond what the call does")
    detail = ", ".join(removed) if removed else "formatting written for a rendered page"
    return (
        text,
        f"Rewrites the description for an agent reading it in a tool list, dropping {detail}, "
        "and states the side effect where the model is actually looking. The source text is "
        "unchanged in the IR.",
    )


def propose_composites(
    operations: list[OperationIR],
) -> list[tuple[str, list[str], float, str]]:
    """Propose prepare-then-execute workflows.

    A write whose path extends another write's path and ends in an action verb is the
    familiar shape of a creation step followed by an irreversible confirmation step.
    Presenting them as two independent tools invites an agent to perform the second without
    the first.
    """
    routed = [
        item
        for item in operations
        if item.route and item.side_effect is not SideEffectClass.READ
    ]
    proposals: list[tuple[str, list[str], float, str]] = []
    for candidate in routed:
        assert candidate.route is not None
        segments = [item for item in candidate.route.split("/") if item]
        if not segments or segments[-1] not in _ACTION_SEGMENTS:
            continue
        prefix = "/" + "/".join(segments[:-1])
        base = _TEMPLATED.sub("", prefix).rstrip("/")
        for other in routed:
            assert other.route is not None
            if other is candidate:
                continue
            if _TEMPLATED.sub("", other.route).rstrip("/") != base:
                continue
            proposals.append(
                (
                    f"{other.operation_id}_then_{candidate.operation_id}",
                    [other.operation_id, candidate.operation_id],
                    0.5,
                    f"{other.operation_id!r} prepares a resource that {candidate.operation_id!r} "
                    f"then {segments[-1]}s. Exposing them separately lets an agent take the "
                    "irreversible step without the step that makes it meaningful.",
                )
            )
    return sorted(proposals, key=lambda item: item[0])


def propose_lookup_then_use(
    operations: list[OperationIR],
) -> list[tuple[str, list[str], float, str]]:
    """Propose a lookup paired with the write that cannot run without it.

    A write whose route carries an identifier cannot be called from a goal alone: the value has
    to come from a read first. The specification states both halves, which resource the write
    needs an identifier for and which read yields identifiers for that resource, so the pair
    is derivable without consulting how anybody happened to solve a task.

    Exposing the two separately makes the agent responsible for discovering a dependency the
    specification already describes.
    """
    yields: dict[str, list[OperationIR]] = {}
    for item in operations:
        collection = yielded_collection(item)
        # Only an operation a goal can reach on its own is a usable first step.
        if collection and callable_from_a_goal(item):
            yields.setdefault(collection, []).append(item)

    seen: set[str] = set()
    proposals: list[tuple[str, list[str], float, str]] = []
    for write in operations:
        for resource in required_resources(write):
            for read in yields.get(resource, []):
                composite_id = f"{read.operation_id}_then_{write.operation_id}"
                if read.operation_id == write.operation_id or composite_id in seen:
                    continue
                seen.add(composite_id)
                proposals.append(
                    (
                        f"{read.operation_id}_then_{write.operation_id}",
                        [read.operation_id, write.operation_id],
                        0.45,
                        f"{write.operation_id!r} cannot be called without a {resource} "
                        f"identifier, and {read.operation_id!r} is what yields one. The "
                        "specification states the dependency; exposing the two separately "
                        "makes the agent rediscover it.",
                    )
                )
    return sorted(proposals, key=lambda item: item[0])


def suitability(operation: OperationIR, blocked: bool) -> tuple[float, str]:
    """Score how ready an operation is to be exposed to an agent.

    The score is an average of stated signals rather than an opaque number, so a low score
    can be explained and acted on.
    """
    signals = {
        "has a summary": operation.intent != operation.operation_id,
        "has a description": bool(operation.description),
        "declares a success schema": any(
            item.type_schema for item in operation.outputs if item.status[:1] == "2"
        ),
        "classifies its side effect": operation.side_effect is not SideEffectClass.UNKNOWN,
        "is not deprecated": not operation.deprecated,
        "has no blocking ambiguity": not blocked,
    }
    met = [name for name, value in signals.items() if value]
    missing = [name for name, value in signals.items() if not value]
    score = round(len(met) / len(signals), 4)
    detail = f"Meets {len(met)} of {len(signals)} readiness signals."
    if missing:
        detail += " Missing: " + ", ".join(missing) + "."
    return score, detail


def _decision(
    kind: DecisionKind,
    target: str,
    rationale: str,
    confidence: float,
    *,
    applied: bool,
    pointer: str,
    origin: DecisionOrigin = DecisionOrigin.PLANNER,
    previous_value: str | None = None,
    proposed_value: str | None = None,
    members: list[str] | None = None,
) -> PlanDecision:
    """Build one decision record with provenance back to its source operation."""
    fields = ["kind", "origin", "target", "rationale", "confidence", "applied"]
    if previous_value is not None:
        fields.append("previous_value")
    if proposed_value is not None:
        fields.append("proposed_value")
    if members:
        fields.append("members")
    return PlanDecision(
        kind=kind,
        origin=origin,
        target=target,
        rationale=rationale,
        confidence=confidence,
        applied=applied,
        previous_value=previous_value,
        proposed_value=proposed_value,
        members=members or [],
        provenance=[
            Provenance(
                field=name,
                source_pointer=pointer,
                derivation=Derivation.NORMALIZED,
                rule=f"{PLANNER_RULE_PREFIX}.decision.{kind.value}",
            )
            for name in fields
        ],
    )


def _blocked_operations(ir: ApiSemanticIR) -> set[str]:
    """Operation identifiers named by a blocking ambiguity."""
    known = {item.operation_id for item in ir.operations}
    found = set()
    for item in ir.blocking_ambiguities:
        parts = item.field.split(".")
        if len(parts) >= 2 and parts[0] == "operations" and parts[1] in known:
            found.add(parts[1])
    return found


def plan_semantic(ir: ApiSemanticIR, overlay: ToolOverlay | None = None) -> ToolPlan:
    """Design an agent-facing tool surface from the IR and any accepted human decisions.

    Raises `OverlayMismatchError` when the overlay was reviewed against different bytes,
    because its decisions were made about a different specification.
    """
    if overlay is not None and overlay.source_digest != ir.service.source_digest:
        raise OverlayMismatchError(
            f"overlay was reviewed against {overlay.source_digest} but the specification is "
            f"{ir.service.source_digest}; re-review before applying it"
        )

    blocked = _blocked_operations(ir)
    artifacts: list[ToolArtifact] = []
    decisions: list[PlanDecision] = []

    # A composite a reviewer accepted as replacing its steps removes them from the surface.
    # Computed before the operations are planned, because a superseded step is never planned
    # at all rather than planned and then discarded.
    claimed_names: set[str] = set()
    superseded = {
        step: item.name
        for item in (overlay.composites if overlay else [])
        if item.review_status is ReviewStatus.APPROVED and item.supersedes_steps
        for step in item.steps
    }

    for operation in ir.operations:
        entry = overlay.entry(operation.operation_id) if overlay else None
        pointer = operation.source_pointer

        classified = entry.side_effect if entry and entry.side_effect else None
        if classified is not None:
            decisions.append(
                _decision(
                    DecisionKind.RECLASSIFY,
                    operation.operation_id,
                    "Side effect recorded by a reviewer in the overlay. WSDL carries no signal "
                    "to infer one from, so this is a judgement rather than a derivation, and "
                    "it is the only thing that can release a SOAP operation through the gate.",
                    1.0,
                    applied=True,
                    pointer=pointer,
                    origin=DecisionOrigin.HUMAN,
                    previous_value=operation.side_effect.value,
                    proposed_value=classified.value,
                )
            )

        proposed_name, name_confidence, name_rationale = derive_name(operation)
        proposed_name = disambiguate(proposed_name, operation, claimed_names)
        name = entry.name if entry and entry.name else proposed_name
        claimed_names.add(name)
        if proposed_name != operation.operation_id:
            decisions.append(
                _decision(
                    DecisionKind.RENAME,
                    operation.operation_id,
                    name_rationale,
                    name_confidence,
                    applied=not (entry and entry.name),
                    pointer=pointer,
                    previous_value=operation.operation_id,
                    proposed_value=proposed_name,
                )
            )
        if entry and entry.name:
            decisions.append(
                _decision(
                    DecisionKind.RENAME,
                    operation.operation_id,
                    "Name set by a reviewer in the overlay.",
                    1.0,
                    applied=True,
                    pointer=pointer,
                    origin=DecisionOrigin.HUMAN,
                    previous_value=proposed_name,
                    proposed_value=entry.name,
                )
            )

        proposed_kind, kind_confidence, kind_rationale = derive_kind(operation)
        kind = entry.kind if entry and entry.kind else proposed_kind
        if proposed_kind is not ArtifactKind.TOOL:
            decisions.append(
                _decision(
                    DecisionKind.RECLASSIFY,
                    operation.operation_id,
                    kind_rationale,
                    kind_confidence,
                    applied=not (entry and entry.kind),
                    pointer=pointer,
                    previous_value=ArtifactKind.TOOL.value,
                    proposed_value=proposed_kind.value,
                )
            )

        group_value, group_confidence, group_rationale = derive_group(operation)
        group = entry.group if entry and entry.group else group_value
        decisions.append(
            _decision(
                DecisionKind.GROUP,
                operation.operation_id,
                group_rationale,
                group_confidence,
                applied=not (entry and entry.group),
                pointer=pointer,
                proposed_value=group_value,
            )
        )

        description = operation.description or operation.intent
        rewritten = rewrite_description(operation)
        if rewritten is not None:
            description, description_rationale = rewritten
            decisions.append(
                _decision(
                    DecisionKind.DESCRIBE,
                    operation.operation_id,
                    description_rationale,
                    0.7,
                    applied=True,
                    pointer=pointer,
                    previous_value=operation.description or operation.intent,
                    proposed_value=description,
                )
            )

        omitted_arguments: list[str] = []
        argument_projection = derive_argument_projection(operation)
        if argument_projection is not None:
            omitted_arguments, argument_confidence, argument_rationale = argument_projection
            decisions.append(
                _decision(
                    DecisionKind.PROJECT,
                    operation.operation_id,
                    argument_rationale,
                    argument_confidence,
                    applied=True,
                    pointer=pointer,
                    members=omitted_arguments,
                )
            )

        projection = derive_projection(operation)
        output_fields = list(entry.output_fields) if entry and entry.output_fields else []
        if projection is not None:
            fields, projection_confidence, projection_rationale = projection
            if not output_fields:
                output_fields = fields
            decisions.append(
                _decision(
                    DecisionKind.PROJECT,
                    operation.operation_id,
                    projection_rationale,
                    projection_confidence,
                    applied=not (entry and entry.output_fields),
                    pointer=pointer,
                    members=fields,
                )
            )

        # Decision: a deprecated operation is proposed for omission but never
        # omitted here. Only a reviewer accepting it in the overlay removes it, because a
        # planner that deletes silently repeats the failure the ingestion sweep prevents.
        if operation.deprecated:
            decisions.append(
                _decision(
                    DecisionKind.OMIT,
                    operation.operation_id,
                    "The specification marks this operation deprecated, so exposing it spends "
                    "agent attention on a surface the provider intends to withdraw.",
                    0.6,
                    applied=bool(entry and entry.omit),
                    pointer=pointer,
                )
            )
        if operation.operation_id in superseded:
            decisions.append(
                _decision(
                    DecisionKind.OMIT,
                    operation.operation_id,
                    f"Superseded by the composite {superseded[operation.operation_id]!r}, "
                    "which a reviewer accepted as replacing its steps. Composing is meant to "
                    "reduce what an agent chooses between, so leaving the step beside the "
                    "composite would make that choice harder rather than easier.",
                    1.0,
                    applied=True,
                    pointer=pointer,
                    origin=DecisionOrigin.HUMAN,
                )
            )
            continue
        if entry and entry.omit:
            decisions.append(
                _decision(
                    DecisionKind.OMIT,
                    operation.operation_id,
                    "Omission accepted by a reviewer in the overlay.",
                    1.0,
                    applied=True,
                    pointer=pointer,
                    origin=DecisionOrigin.HUMAN,
                )
            )
            continue

        review_status = (
            entry.review_status if entry and entry.review_status else ReviewStatus.PROPOSED
        )
        if entry and entry.review_status is ReviewStatus.APPROVED:
            decisions.append(
                _decision(
                    DecisionKind.APPROVE,
                    operation.operation_id,
                    "Approved by a reviewer in the overlay, releasing it through the emission "
                    "gate.",
                    1.0,
                    applied=True,
                    pointer=pointer,
                    origin=DecisionOrigin.HUMAN,
                )
            )

        score, score_detail = suitability(operation, operation.operation_id in blocked)
        risk = SIDE_EFFECT_TO_RISK[classified or operation.side_effect]
        provenance_fields = [
            "artifact_id",
            "kind",
            "name",
            "description",
            "source_operations",
            "risk",
            "review_status",
            "rationale",
            "confidence",
            "group",
        ]
        if output_fields:
            provenance_fields.append("output_fields")
        if omitted_arguments:
            provenance_fields.append("omitted_arguments")
        artifacts.append(
            ToolArtifact(
                artifact_id=f"{kind.value}:{name}",
                kind=kind,
                name=name,
                description=description,
                source_operations=[operation.operation_id],
                risk=risk,
                review_status=review_status,
                rationale=f"{name_rationale} {score_detail}",
                confidence=score,
                group=group,
                output_fields=output_fields,
                omitted_arguments=omitted_arguments,
                provenance=[
                    Provenance(
                        field=item,
                        source_pointer=pointer,
                        derivation=Derivation.NORMALIZED,
                        rule=f"{PLANNER_RULE_PREFIX}.artifact.{item}",
                    )
                    for item in provenance_fields
                ],
            )
        )

    accepted = {item.composite_id: item for item in (overlay.composites if overlay else [])}
    candidates = [
        *propose_composites(ir.operations),
        *propose_lookup_then_use(ir.operations),
    ]
    for composite_id, steps, confidence, rationale in candidates:
        decisions.append(
            _decision(
                DecisionKind.COMPOSE,
                composite_id,
                rationale,
                confidence,
                applied=composite_id in accepted,
                pointer=ir.operations[0].source_pointer,
                members=steps,
            )
        )

    for composite in accepted.values():
        pointer = next(
            (
                item.source_pointer
                for item in ir.operations
                if item.operation_id in composite.steps
            ),
            ir.operations[0].source_pointer if ir.operations else "openapi:#",
        )
        decisions.append(
            _decision(
                DecisionKind.COMPOSE,
                composite.composite_id,
                "Composite accepted by a reviewer in the overlay.",
                1.0,
                applied=True,
                pointer=pointer,
                origin=DecisionOrigin.HUMAN,
                members=list(composite.steps),
            )
        )
        risks = [
            SIDE_EFFECT_TO_RISK[item.side_effect]
            for item in ir.operations
            if item.operation_id in composite.steps
        ]
        # A composite is only as safe as its most dangerous step.
        risk = (
            RiskClass.DESTRUCTIVE
            if RiskClass.DESTRUCTIVE in risks
            else RiskClass.WRITE
            if RiskClass.WRITE in risks
            else RiskClass.UNKNOWN
            if RiskClass.UNKNOWN in risks
            else RiskClass.READ
        )
        artifacts.append(
            ToolArtifact(
                artifact_id=f"composite:{composite.name}",
                kind=ArtifactKind.COMPOSITE,
                name=composite.name,
                description=composite.description,
                source_operations=list(composite.steps),
                risk=risk,
                review_status=composite.review_status,
                rationale=(
                    "Composite workflow accepted in the overlay. Its risk is that of its most "
                    "dangerous step."
                ),
                confidence=None,
                group=None,
                provenance=[
                    Provenance(
                        field=item,
                        source_pointer=pointer,
                        derivation=Derivation.NORMALIZED,
                        rule=f"{PLANNER_RULE_PREFIX}.composite.{item}",
                    )
                    for item in (
                        "artifact_id",
                        "kind",
                        "name",
                        "description",
                        "source_operations",
                        "risk",
                        "review_status",
                        "rationale",
                    )
                ],
            )
        )

    return ToolPlan(
        service_id=ir.service.service_id,
        planner=PlannerKind.SEMANTIC,
        source_digest=ir.service.source_digest,
        artifacts=artifacts,
        decisions=decisions,
    )
