"""Semantic planner and overlay tests.

Two things are being protected here. That every decision carries the rationale and
confidence a reviewer needs, and that judgement never becomes silent action: a proposal
changes nothing until an overlay records that a human accepted it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.contracts import dump_canonical, validate_overlay
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.models import (
    ApiSemanticIR,
    ArtifactKind,
    CompositeEntry,
    DecisionKind,
    DecisionOrigin,
    EmissionBlocker,
    EmissionStatus,
    OperationIR,
    ParameterLocation,
    PlannerKind,
    ReviewStatus,
    SideEffectClass,
    ToolOverlay,
)
from api_mcp_compiler.planning.overlay import load_overlay, restamp, save_overlay
from api_mcp_compiler.planning.report import review_report
from api_mcp_compiler.planning.semantic import (
    OverlayMismatchError,
    derive_argument_projection,
    plan_semantic,
    propose_lookup_then_use,
    rewrite_description,
)
from tests.conftest import (
    INVENTORY_SERVICE,
    OPENAPI_EXAMPLES,
    ORDER_SERVICE,
)


def _operation_with_description(description: str, tmp_path: Path) -> OperationIR:
    """Parse the smallest specification a description rewrite can be judged on."""
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        textwrap.dedent(
            f"""
            openapi: 3.0.3
            info: {{title: Descriptions, version: '1'}}
            paths:
              /albums/{{id}}:
                get:
                  operationId: getAlbum
                  summary: Get Album
                  description: {description!r}
                  parameters:
                    - {{in: path, name: id, required: true, schema: {{type: string}}}}
                  responses: {{'200': {{description: ok}}}}
            """
        ),
        encoding="utf-8",
    )
    return parse_openapi(spec).operations[0]


ORDER_OVERLAY = "examples/overlays/order_service.overlay.json"
INVENTORY_OVERLAY = "examples/overlays/inventory_service.overlay.json"


def _ir(example: str) -> ApiSemanticIR:
    return parse_openapi(Path(example))


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "spec.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_names_come_from_the_summary_not_the_operation_id() -> None:
    """Naming a tool after a generated identifier is the anti-pattern this planner avoids."""
    plan = plan_semantic(_ir(ORDER_SERVICE))
    names = {item.name for item in plan.artifacts}
    assert "get_customer" in names
    assert "create_refund_request" in names
    assert "getCustomer" not in names


@pytest.mark.parametrize("example", OPENAPI_EXAMPLES)
def test_no_artifact_is_named_after_a_source_identifier(example: str) -> None:
    ir = _ir(example)
    identifiers = {item.operation_id for item in ir.operations}
    assert not {item.name for item in plan_semantic(ir).artifacts} & identifiers


def test_addressable_read_is_proposed_as_a_resource() -> None:
    plan = plan_semantic(_ir(ORDER_SERVICE))
    artifact = next(item for item in plan.artifacts if item.name == "get_customer")
    assert artifact.kind is ArtifactKind.RESOURCE


def test_read_with_a_filter_stays_a_tool() -> None:
    """A read taking a page or a filter is a query, not something addressable."""
    plan = plan_semantic(_ir(INVENTORY_SERVICE))
    artifact = next(
        item for item in plan.artifacts if item.name.startswith("list_items_held")
    )
    assert artifact.kind is ArtifactKind.TOOL


def test_operations_are_grouped_by_path_prefix() -> None:
    plan = plan_semantic(_ir(ORDER_SERVICE))
    groups = {item.name: item.group for item in plan.artifacts}
    assert groups["get_customer"] == "customers"
    assert groups["create_refund_request"] == "refunds"


def test_projection_drops_fields_the_response_does_not_require() -> None:
    """Returning an entire enterprise payload into model context is an anti-pattern."""
    plan = plan_semantic(_ir(ORDER_SERVICE))
    artifact = next(item for item in plan.artifacts if item.name == "get_customer")
    assert artifact.output_fields == ["id", "name"]
    decision = next(
        item
        for item in plan.decisions
        if item.kind is DecisionKind.PROJECT and item.target == "getCustomer"
    )
    assert "internal_account_ref" in decision.rationale


def test_deprecated_operation_is_proposed_for_omission_but_not_omitted() -> None:
    """A planner that deletes silently repeats the failure the ingestion sweep prevents."""
    ir = _ir(INVENTORY_SERVICE)
    plan = plan_semantic(ir)
    decision = next(item for item in plan.decisions if item.kind is DecisionKind.OMIT)
    assert decision.target == "listWarehouseItemsLegacy"
    assert decision.applied is False
    assert len(plan.artifacts) == len(ir.operations)


def test_accepted_omission_removes_the_artifact() -> None:
    ir = _ir(INVENTORY_SERVICE)
    plan = plan_semantic(ir, load_overlay(Path(INVENTORY_OVERLAY)))
    assert len(plan.artifacts) == len(ir.operations) - 1
    assert all("retired" not in item.name for item in plan.artifacts)


def test_prepare_then_execute_pair_is_proposed_as_a_composite() -> None:
    """Exposed separately, an agent can take the irreversible step on its own."""
    plan = plan_semantic(_ir(ORDER_SERVICE))
    decision = next(item for item in plan.decisions if item.kind is DecisionKind.COMPOSE)
    assert decision.members == ["createRefund", "approveRefund"]
    assert decision.applied is False


def test_accepted_composite_becomes_an_artifact() -> None:
    plan = plan_semantic(_ir(ORDER_SERVICE), load_overlay(Path(ORDER_OVERLAY)))
    composite = next(
        item for item in plan.artifacts if item.kind is ArtifactKind.COMPOSITE
    )
    assert composite.name == "refund_order"
    assert composite.source_operations == ["createRefund", "approveRefund"]


def test_composite_is_never_executable_in_this_phase() -> None:
    ir = _ir(ORDER_SERVICE)
    surface = generate_surface(ir, plan_semantic(ir, load_overlay(Path(ORDER_OVERLAY))))
    composite = next(item for item in surface.tools if item.kind is ArtifactKind.COMPOSITE)
    assert composite.emission is EmissionStatus.DISABLED
    assert EmissionBlocker.COMPOSITE_PENDING_CONFIRMATION in composite.blockers


@pytest.mark.parametrize("example", OPENAPI_EXAMPLES)
def test_every_decision_carries_a_rationale_and_a_confidence(example: str) -> None:
    """The acceptance criteria require both for every merge, omission, rename and composite."""
    decisions = plan_semantic(_ir(example)).decisions
    assert decisions
    for item in decisions:
        assert item.rationale.strip()
        assert 0.0 <= item.confidence <= 1.0
        assert item.provenance


@pytest.mark.parametrize("example", OPENAPI_EXAMPLES)
def test_a_proposal_never_presents_itself_as_certain(example: str) -> None:
    for item in plan_semantic(_ir(example)).decisions:
        if item.origin is DecisionOrigin.PLANNER:
            assert item.confidence < 1.0
        else:
            assert item.confidence == 1.0


def test_suitability_is_scored_and_explained() -> None:
    plan = plan_semantic(_ir(ORDER_SERVICE))
    artifact = next(item for item in plan.artifacts if item.name == "get_customer")
    assert artifact.confidence is not None
    assert 0.0 < artifact.confidence <= 1.0
    assert "readiness signals" in artifact.rationale


def test_overlay_rename_overrides_the_proposal() -> None:
    plan = plan_semantic(_ir(ORDER_SERVICE), load_overlay(Path(ORDER_OVERLAY)))
    assert "look_up_customer" in {item.name for item in plan.artifacts}
    human = [
        item
        for item in plan.decisions
        if item.kind is DecisionKind.RENAME and item.origin is DecisionOrigin.HUMAN
    ]
    assert len(human) == 1
    assert human[0].proposed_value == "look_up_customer"


def test_overlay_approval_releases_a_write_tool() -> None:
    ir = _ir(ORDER_SERVICE)
    surface = generate_surface(ir, plan_semantic(ir, load_overlay(Path(ORDER_OVERLAY))))
    tool = next(item for item in surface.tools if item.name == "create_refund_request")
    assert tool.emission is EmissionStatus.EXECUTABLE


def test_overlay_rejection_keeps_a_tool_disabled() -> None:
    ir = _ir(INVENTORY_SERVICE)
    surface = generate_surface(ir, plan_semantic(ir, load_overlay(Path(INVENTORY_OVERLAY))))
    tool = next(item for item in surface.tools if item.risk.value == "destructive")
    assert EmissionBlocker.REJECTED in tool.blockers


def test_stale_overlay_is_refused() -> None:
    """Decisions recorded about other bytes must not be applied silently."""
    ir = _ir(ORDER_SERVICE)
    stale = load_overlay(Path(ORDER_OVERLAY)).model_copy(
        update={"source_digest": "sha256:" + "0" * 64}
    )
    with pytest.raises(OverlayMismatchError, match="re-review"):
        plan_semantic(ir, stale)


def test_restamp_rebinds_an_overlay(tmp_path: Path) -> None:
    overlay = load_overlay(Path(ORDER_OVERLAY))
    rebound = restamp(overlay, "sha256:" + "1" * 64)
    assert rebound.source_digest == "sha256:" + "1" * 64
    assert rebound.entries == overlay.entries
    path = tmp_path / "out.overlay.json"
    save_overlay(rebound, path)
    assert load_overlay(path) == rebound


def test_overlay_loads_from_yaml(tmp_path: Path) -> None:
    """An overlay is edited by a person, so the friendlier format has to work."""
    ir = _ir(ORDER_SERVICE)
    path = tmp_path / "o.yaml"
    path.write_text(
        "schema_version: '0.3.0'\n"
        f"service_id: {ir.service.service_id}\n"
        f"source_digest: {ir.service.source_digest}\n"
        "entries:\n"
        "  - operation_id: getCustomer\n"
        "    name: fetch_customer\n",
        encoding="utf-8",
    )
    plan = plan_semantic(ir, load_overlay(path))
    assert "fetch_customer" in {item.name for item in plan.artifacts}


@pytest.mark.parametrize("overlay", [ORDER_OVERLAY, INVENTORY_OVERLAY])
def test_committed_overlays_validate(overlay: str) -> None:
    validate_overlay(load_overlay(Path(overlay)).model_dump(mode="json"), label=overlay)


def test_overlay_rejects_a_foreign_schema_version() -> None:
    with pytest.raises(ValueError, match="expected overlay schema_version"):
        ToolOverlay(
            schema_version="9.9.9",
            service_id="s",
            source_digest="sha256:" + "0" * 64,
        )


def test_plan_is_marked_as_produced_by_the_semantic_planner() -> None:
    assert plan_semantic(_ir(ORDER_SERVICE)).planner is PlannerKind.SEMANTIC


@pytest.mark.parametrize("example", OPENAPI_EXAMPLES)
def test_planning_is_reproducible(example: str) -> None:
    ir = _ir(example)
    assert dump_canonical(plan_semantic(ir)) == dump_canonical(plan_semantic(ir))


@pytest.mark.parametrize("example", OPENAPI_EXAMPLES)
def test_review_report_is_deterministic_and_complete(example: str) -> None:
    ir = _ir(example)
    plan = plan_semantic(ir)
    first = review_report(ir, plan)
    assert first == review_report(ir, plan)
    for decision in plan.decisions:
        assert decision.target in first


def test_review_report_separates_proposals_from_accepted_decisions() -> None:
    ir = _ir(ORDER_SERVICE)
    report = review_report(ir, plan_semantic(ir, load_overlay(Path(ORDER_OVERLAY))))
    assert "What a reviewer must decide" in report
    assert "Decisions already accepted by a reviewer:" in report


def test_operation_without_a_summary_is_named_with_low_confidence(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: NoSummary, version: '1'}
        paths:
          /things:
            get:
              operationId: listThings
              responses: {'200': {description: ok}}
        """,
    )
    plan = plan_semantic(parse_openapi(spec))
    decision = next(item for item in plan.decisions if item.kind is DecisionKind.RENAME)
    assert decision.confidence < 0.5
    assert "No summary" in decision.rationale


def test_baseline_plan_records_no_decisions() -> None:
    """The baseline exists to be a comparison point, so it must stay decision-free."""
    from api_mcp_compiler.planning.baseline import plan_baseline

    assert plan_baseline(_ir(ORDER_SERVICE)).decisions == []


def test_review_status_defaults_to_proposed_without_an_overlay() -> None:
    plan = plan_semantic(_ir(ORDER_SERVICE))
    assert all(item.review_status is ReviewStatus.PROPOSED for item in plan.artifacts)


def test_a_required_argument_is_never_withheld() -> None:
    """A projection that could make a call invalid would be a bug, not a simplification."""
    for example in (ORDER_SERVICE, INVENTORY_SERVICE):
        ir = _ir(example)
        for operation in ir.operations:
            proposal = derive_argument_projection(operation)
            if proposal is None:
                continue
            required = {item.name for item in operation.inputs if item.required}
            assert not (set(proposal[0]) & required), operation.operation_id


def test_a_body_is_never_withheld() -> None:
    """A body carries the task's own content; withholding it would remove capability."""
    for example in (ORDER_SERVICE, INVENTORY_SERVICE):
        ir = _ir(example)
        for operation in ir.operations:
            proposal = derive_argument_projection(operation)
            if proposal is None:
                continue
            body = {
                item.name
                for item in operation.inputs
                if item.location is ParameterLocation.BODY
            }
            assert not (set(proposal[0]) & body), operation.operation_id


def test_a_rewritten_description_states_a_destructive_side_effect() -> None:
    """The risk belongs where the model is reading, not only in a policy file."""
    destructive = [
        item
        for example in (ORDER_SERVICE, INVENTORY_SERVICE)
        for item in _ir(example).operations
        if item.side_effect is SideEffectClass.DESTRUCTIVE
    ]
    assert destructive, "the fixture must contain a destructive operation for this to mean anything"
    for operation in destructive:
        rewritten = rewrite_description(operation)
        assert rewritten is not None
        assert "cannot be undone" in rewritten[0]


def test_a_rewrite_drops_links_and_markup_a_model_cannot_use(tmp_path: Path) -> None:
    operation = _operation_with_description(
        "See the [guide](https://example.invalid/docs) for details.<br/>Then call it.",
        tmp_path,
    )
    rewritten = rewrite_description(operation)
    assert rewritten is not None
    assert "https://" not in rewritten[0]
    assert "<br/>" not in rewritten[0]
    assert "guide" in rewritten[0]


def test_a_rewrite_does_not_restate_the_tool_name(tmp_path: Path) -> None:
    """The name already carries the action; repeating it pads every entry in the tool list."""
    operation = _operation_with_description(
        "Get Spotify catalog information for one album.", tmp_path
    )
    rewritten = rewrite_description(operation)
    assert rewritten is None or not rewritten[0].lower().startswith(
        (operation.intent or "").lower().rstrip(".") + "."
    )


def test_a_lookup_then_use_pair_is_derived_from_route_structure(tmp_path: Path) -> None:
    """The specification states the dependency; nothing here consults how a task was solved.

    A write whose route carries an identifier cannot be called from a goal alone. The value has
    to come from a read, and which read is stated by the route.
    """
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        textwrap.dedent(
            """
            openapi: 3.0.3
            info: {title: Lookup, version: '1'}
            paths:
              /playlists:
                get:
                  operationId: listPlaylists
                  responses: {'200': {description: ok}}
              /playlists/{playlist_id}/tracks:
                post:
                  operationId: addTracks
                  parameters:
                    - {in: path, name: playlist_id, required: true, schema: {type: string}}
                  responses: {'201': {description: created}}
            """
        ),
        encoding="utf-8",
    )
    ir = parse_openapi(spec)
    proposals = propose_lookup_then_use(ir.operations)
    assert [item[1] for item in proposals] == [["listPlaylists", "addTracks"]]


def test_a_write_needing_no_identifier_proposes_no_pair(tmp_path: Path) -> None:
    """Otherwise every read would pair with every write and the proposal would mean nothing."""
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        textwrap.dedent(
            """
            openapi: 3.0.3
            info: {title: NoLookup, version: '1'}
            paths:
              /playlists:
                get:
                  operationId: listPlaylists
                  responses: {'200': {description: ok}}
                post:
                  operationId: createPlaylist
                  responses: {'201': {description: created}}
            """
        ),
        encoding="utf-8",
    )
    assert propose_lookup_then_use(parse_openapi(spec).operations) == []


def test_a_composite_is_proposed_and_never_applied_without_a_reviewer() -> None:
    """Composition changes what an agent can do in one call, so it is a decision, not a default."""
    ir = _ir(ORDER_SERVICE)
    plan = plan_semantic(ir)
    composed = [item for item in plan.decisions if item.kind is DecisionKind.COMPOSE]
    assert composed
    assert not any(item.applied for item in composed)


def _composite_overlay(ir: ApiSemanticIR, steps: list[str], supersede: bool) -> ToolOverlay:
    return ToolOverlay(
        service_id=ir.service.service_id,
        source_digest=ir.service.source_digest,
        composites=[
            CompositeEntry(
                composite_id="c",
                name="composed_tool",
                description="Two steps a goal cannot bridge on its own, in one call.",
                steps=steps,
                review_status=ReviewStatus.APPROVED,
                supersedes_steps=supersede,
            )
        ],
    )


def test_a_composite_joins_the_surface_unless_a_reviewer_says_otherwise() -> None:
    """Approving a tool must never silently remove others."""
    ir = _ir(ORDER_SERVICE)
    plan = plan_semantic(ir, _composite_overlay(ir, ["createRefund", "approveRefund"], False))
    planned = {item.name for item in plan.artifacts if item.kind is not ArtifactKind.OMITTED}
    assert "create_refund_request" in planned
    assert "approve_refund_and_release_payment" in planned


def test_a_composite_can_replace_its_steps_when_a_reviewer_says_so() -> None:
    """Composing is meant to reduce what an agent chooses between."""
    ir = _ir(ORDER_SERVICE)
    plan = plan_semantic(ir, _composite_overlay(ir, ["createRefund", "approveRefund"], True))
    planned = {item.name for item in plan.artifacts if item.kind is not ArtifactKind.OMITTED}
    assert "create_refund_request" not in planned
    assert "approve_refund_and_release_payment" not in planned
    assert "composed_tool" in planned


def test_superseding_is_recorded_as_a_decision_with_its_reason() -> None:
    """A tool that disappears from a surface must say who removed it and why."""
    ir = _ir(ORDER_SERVICE)
    plan = plan_semantic(ir, _composite_overlay(ir, ["createRefund", "approveRefund"], True))
    omitted = [item for item in plan.decisions if item.kind is DecisionKind.OMIT and item.applied]
    assert omitted
    assert any("Superseded by the composite" in item.rationale for item in omitted)


def test_an_unapproved_composite_supersedes_nothing() -> None:
    """A proposal must not remove a tool before anyone has accepted it."""
    ir = _ir(ORDER_SERVICE)
    overlay = _composite_overlay(ir, ["createRefund", "approveRefund"], True)
    proposed = overlay.model_copy(
        update={
            "composites": [
                overlay.composites[0].model_copy(
                    update={"review_status": ReviewStatus.PROPOSED}
                )
            ]
        }
    )
    plan = plan_semantic(ir, proposed)
    planned = {item.name for item in plan.artifacts if item.kind is not ArtifactKind.OMITTED}
    assert "create_refund_request" in planned


def test_a_surface_never_carries_two_tools_of_the_same_name() -> None:
    """Ten tools called `get_details` is not a surface an agent can choose from.

    A summary says what an operation does but not always what it does it to, so on a large API
    many collapse to the same phrase. The first TMDB run was rejected outright by the API for
    exactly this.
    """
    for example in OPENAPI_EXAMPLES:
        ir = _ir(example)
        names = [item.name for item in plan_semantic(ir).artifacts]
        assert len(names) == len(set(names)), example


def test_a_collision_is_resolved_by_naming_the_resource(tmp_path: Path) -> None:
    """Qualifying tells a reader what the tools differ by; numbering would not."""
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        textwrap.dedent(
            """
            openapi: 3.0.3
            info: {title: Collides, version: '1'}
            paths:
              /movie/{id}:
                get:
                  operationId: getMovie
                  summary: Get Details
                  parameters:
                    - {in: path, name: id, required: true, schema: {type: integer}}
                  responses: {'200': {description: ok}}
              /tv/{id}:
                get:
                  operationId: getTv
                  summary: Get Details
                  parameters:
                    - {in: path, name: id, required: true, schema: {type: integer}}
                  responses: {'200': {description: ok}}
            """
        ),
        encoding="utf-8",
    )
    names = {
        item.source_operations[0]: item.name
        for item in plan_semantic(parse_openapi(spec)).artifacts
    }
    assert names["getMovie"] != names["getTv"]
    assert "movie" in names["getMovie"] or "tv" in names["getTv"]


TAGGED = """openapi: 3.0.3
info: {title: Tagged Service, version: 1.0.0}
servers: [{url: https://tagged.example.invalid}]
paths:
  /v2/resources/{id}:
    get:
      operationId: getThing
      summary: Get a thing
      tags: [Catalogue, Legacy]
      parameters: [{in: path, name: id, required: true, schema: {type: string}}]
      responses:
        '200': {description: ok, content: {application/json: {schema: {type: object}}}}
  /v2/other/{id}:
    get:
      operationId: getOther
      summary: Get another thing
      parameters: [{in: path, name: id, required: true, schema: {type: string}}]
      responses:
        '200': {description: ok, content: {application/json: {schema: {type: object}}}}
"""


def _tagged(tmp_path: Path) -> ApiSemanticIR:
    spec = tmp_path / "tagged.yaml"
    spec.write_text(TAGGED, encoding="utf-8")
    return parse_openapi(spec)


def test_a_declared_tag_is_kept_rather_than_swept(tmp_path: Path) -> None:
    """The sweep reported every tag on a real 40-operation document as unread."""
    ir = _tagged(tmp_path)
    tagged = next(item for item in ir.operations if item.operation_id == "getThing")

    assert tagged.tags == ["Catalogue", "Legacy"]
    assert not [item for item in ir.ambiguities if "tags" in (item.field or "")]


def test_grouping_prefers_what_the_document_declares(tmp_path: Path) -> None:
    """A tag is a source fact where a path prefix is an inference."""
    ir = _tagged(tmp_path)
    plan = plan_semantic(ir)

    grouped = {item.source_operations[0]: item.group for item in plan.artifacts}
    assert grouped["getThing"] == "Catalogue"


def test_the_first_tag_is_used_when_several_are_declared(tmp_path: Path) -> None:
    """Choosing by any other rule would overrule the document about its own structure."""
    ir = _tagged(tmp_path)
    decision = next(
        item
        for item in plan_semantic(ir).decisions
        if item.kind is DecisionKind.GROUP and item.target == "getThing"
    )

    assert "'Catalogue'" in decision.rationale
    assert decision.confidence > 0.65


def test_the_path_prefix_remains_the_fallback(tmp_path: Path) -> None:
    """A specification that declares no tags still gets a grouping."""
    ir = _tagged(tmp_path)
    plan = plan_semantic(ir)

    grouped = {item.source_operations[0]: item.group for item in plan.artifacts}
    assert grouped["getOther"] == "v2"


def test_an_asynchronous_operation_says_so_where_a_model_reads(tmp_path: Path) -> None:
    """An agent that reads acceptance as completion reports a goal met before work starts."""
    spec = tmp_path / "batch.yaml"
    spec.write_text(
        "openapi: 3.0.3\n"
        "info: {title: Batch Service, version: 1.0.0}\n"
        "servers: [{url: https://batch.example.invalid}]\n"
        "paths:\n"
        "  /exports:\n"
        "    post:\n"
        "      operationId: startExport\n"
        "      summary: Start an export\n"
        "      responses:\n"
        "        '202':\n"
        "          description: Accepted\n"
        "          headers:\n"
        "            Location: {description: poll here, schema: {type: string}}\n",
        encoding="utf-8",
    )
    plan = plan_semantic(parse_openapi(spec))
    artifact = plan.artifacts[0]

    assert "returns before the work is done" in artifact.description
    assert "poll the Location header" in artifact.description
