"""Tool surface generation and emission gate tests.

The emission gate is where the compiler's safety posture stops being metadata and starts
being behaviour, so most of these tests are about what generation refuses.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from api_mcp_compiler.codegen.schema import compose_input_schema, compose_output_schema
from api_mcp_compiler.codegen.tools import SurfaceGenerationError, generate_surface
from api_mcp_compiler.contracts import dump_canonical, validate_tool_surface
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.ingest.wsdl import parse_wsdl
from api_mcp_compiler.models import (
    ApiSemanticIR,
    CompositeEntry,
    Derivation,
    EmissionBlocker,
    EmissionStatus,
    ParameterLocation,
    Provenance,
    ReviewStatus,
    RiskClass,
    SideEffectClass,
    ToolDescriptor,
    ToolOverlay,
    ToolPlan,
)
from api_mcp_compiler.planning.baseline import plan_baseline
from api_mcp_compiler.planning.semantic import plan_semantic
from tests.conftest import ALL_EXAMPLES, CUSTOMER_SERVICE, INVENTORY_SERVICE, ORDER_SERVICE


def _ir(example: str) -> ApiSemanticIR:
    source = Path(example)
    return parse_wsdl(source) if source.suffix == ".wsdl" else parse_openapi(source)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "spec.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _descriptor_provenance(*fields: str) -> list[Provenance]:
    """Minimal provenance so the emission validator, not the provenance one, is exercised."""
    return [
        Provenance(
            field=name,
            source_pointer="openapi:#/paths",
            derivation=Derivation.NORMALIZED,
            rule="test",
        )
        for name in fields
    ]


def _approve_all(plan: ToolPlan) -> ToolPlan:
    return plan.model_copy(
        update={
            "artifacts": [
                item.model_copy(update={"review_status": ReviewStatus.APPROVED})
                for item in plan.artifacts
            ]
        }
    )


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_surface_validates_against_the_schema(example: str) -> None:
    ir = _ir(example)
    surface = generate_surface(ir, plan_baseline(ir))
    validate_tool_surface(surface.model_dump(mode="json"), label=example)


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_generation_is_reproducible(example: str) -> None:
    ir = _ir(example)
    first = generate_surface(ir, plan_baseline(ir))
    second = generate_surface(ir, plan_baseline(ir))
    assert dump_canonical(first) == dump_canonical(second)


def test_input_schema_is_flat_and_closed() -> None:
    ir = _ir(ORDER_SERVICE)
    operation = next(item for item in ir.operations if item.operation_id == "createRefund")
    schema, bindings = compose_input_schema(operation)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"body"}
    assert schema["required"] == ["body"]
    binding = bindings[0]
    assert binding.argument == "body"
    assert binding.location is ParameterLocation.BODY
    assert binding.media_type == "application/json"


def test_bindings_preserve_the_wire_name() -> None:
    ir = _ir(INVENTORY_SERVICE)
    operation = next(
        item for item in ir.operations if item.operation_id == "listWarehouseItems"
    )
    _, bindings = compose_input_schema(operation)
    by_argument = {item.argument: item for item in bindings}
    assert by_argument["warehouse_id"].location is ParameterLocation.PATH
    assert by_argument["warehouse_id"].wire_name == "warehouse_id"
    assert by_argument["page"].location is ParameterLocation.QUERY


def test_optional_arguments_are_not_required() -> None:
    ir = _ir(INVENTORY_SERVICE)
    operation = next(
        item for item in ir.operations if item.operation_id == "listWarehouseItems"
    )
    schema, _ = compose_input_schema(operation)
    assert schema["required"] == ["warehouse_id"]
    assert "page" in schema["properties"]


def test_deprecated_input_is_marked_in_the_schema(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Deprecated, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              parameters:
                - {in: query, name: legacy, deprecated: true, schema: {type: string}}
              responses: {'200': {description: ok}}
        """,
    )
    schema, _ = compose_input_schema(parse_openapi(spec).operations[0])
    assert schema["properties"]["legacy"]["deprecated"] is True


def test_output_schema_is_absent_when_nothing_is_declared() -> None:
    ir = _ir(ORDER_SERVICE)
    operation = next(
        item for item in ir.operations if item.operation_id == "listCustomerOrders"
    )
    assert compose_output_schema(operation) is None


def test_multiple_success_schemas_become_a_union(tmp_path: Path) -> None:
    """Narrowing to one would be an output projection decision the planner owns."""
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Multi, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              responses:
                '200':
                  description: ok
                  content: {application/json: {schema: {type: object}}}
                '206':
                  description: partial
                  content: {application/json: {schema: {type: array}}}
        """,
    )
    schema = compose_output_schema(parse_openapi(spec).operations[0])
    assert schema is not None
    assert len(schema["oneOf"]) == 2


def test_read_tools_are_executable_without_approval() -> None:
    """A read-only tool may be generated automatically once validated; nothing else may."""
    ir = _ir(ORDER_SERVICE)
    surface = generate_surface(ir, plan_baseline(ir))
    read = {item.name for item in surface.tools if item.risk is RiskClass.READ}
    assert read == {"getCustomer", "listCustomerOrders"}
    assert all(
        item.emission is EmissionStatus.EXECUTABLE
        for item in surface.tools
        if item.name in read
    )


def test_write_tool_is_refused_until_approved() -> None:
    ir = _ir(ORDER_SERVICE)
    surface = generate_surface(ir, plan_baseline(ir))
    tool = next(item for item in surface.tools if item.name == "createRefund")
    assert tool.emission is EmissionStatus.DISABLED
    assert tool.blockers == [EmissionBlocker.AWAITING_APPROVAL]
    assert tool.blocker_detail is not None


def test_destructive_tool_is_refused_until_approved() -> None:
    ir = _ir(INVENTORY_SERVICE)
    surface = generate_surface(ir, plan_baseline(ir))
    tool = next(item for item in surface.tools if item.name == "purgeWarehouseItems")
    assert tool.risk is RiskClass.DESTRUCTIVE
    assert tool.emission is EmissionStatus.DISABLED


def test_approval_releases_a_write_tool() -> None:
    ir = _ir(ORDER_SERVICE)
    surface = generate_surface(ir, _approve_all(plan_baseline(ir)))
    tool = next(item for item in surface.tools if item.name == "createRefund")
    assert tool.emission is EmissionStatus.EXECUTABLE
    assert tool.blockers == []


def test_rejected_artifact_stays_disabled_even_when_read_only() -> None:
    ir = _ir(ORDER_SERVICE)
    plan = plan_baseline(ir)
    rejected = plan.model_copy(
        update={
            "artifacts": [
                item.model_copy(update={"review_status": ReviewStatus.REJECTED})
                if item.name == "getCustomer"
                else item
                for item in plan.artifacts
            ]
        }
    )
    tool = next(
        item for item in generate_surface(ir, rejected).tools if item.name == "getCustomer"
    )
    assert tool.emission is EmissionStatus.DISABLED
    assert EmissionBlocker.REJECTED in tool.blockers


def test_blocking_ambiguity_keeps_a_tool_disabled_even_when_approved() -> None:
    """Approval must not override an unresolved construct in the source operation."""
    ir = _ir(CUSTOMER_SERVICE)
    surface = generate_surface(ir, _approve_all(plan_baseline(ir)))
    tool = surface.tools[0]
    assert tool.emission is EmissionStatus.DISABLED
    assert EmissionBlocker.BLOCKING_AMBIGUITY in tool.blockers
    assert EmissionBlocker.UNCLASSIFIED_RISK in tool.blockers


def test_unclassified_risk_is_never_executable() -> None:
    ir = _ir(CUSTOMER_SERVICE)
    surface = generate_surface(ir, plan_baseline(ir))
    assert surface.executable_tools == []


def test_argument_collision_disables_the_tool(tmp_path: Path) -> None:
    """Two inputs sharing a name would make the input schema lie about what it accepts."""
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Collide, version: '1'}
        paths:
          /x/{id}:
            get:
              operationId: getX
              parameters:
                - {in: path, name: id, required: true, schema: {type: string}}
                - {in: query, name: id, required: false, schema: {type: integer}}
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    tool = generate_surface(ir, plan_baseline(ir)).tools[0]
    assert tool.emission is EmissionStatus.DISABLED
    assert EmissionBlocker.ARGUMENT_NAME_COLLISION in tool.blockers


def test_plan_from_a_different_revision_is_refused() -> None:
    """A plan reviewed against other bytes must never be applied silently."""
    ir = _ir(ORDER_SERVICE)
    stale = plan_baseline(ir).model_copy(update={"source_digest": "sha256:" + "0" * 64})
    with pytest.raises(SurfaceGenerationError, match="regenerate the plan"):
        generate_surface(ir, stale)


def test_plan_referencing_an_unknown_operation_is_refused() -> None:
    ir = _ir(ORDER_SERVICE)
    plan = plan_baseline(ir)
    broken = plan.model_copy(
        update={
            "artifacts": [
                plan.artifacts[0].model_copy(update={"source_operations": ["nope"]}),
                *plan.artifacts[1:],
            ]
        }
    )
    with pytest.raises(SurfaceGenerationError, match="unknown operation"):
        generate_surface(ir, broken)


def test_descriptor_cannot_be_executable_while_carrying_a_blocker() -> None:
    """The contract refuses the one shape that would defeat the gate silently."""
    with pytest.raises(ValueError, match="executable but carries blockers"):
        ToolDescriptor(
            tool_id="t",
            name="t",
            description="d",
            risk=RiskClass.READ,
            emission=EmissionStatus.EXECUTABLE,
            blockers=[EmissionBlocker.AWAITING_APPROVAL],
            input_schema={"type": "object"},
            provenance=_descriptor_provenance(
                "tool_id", "name", "description", "kind", "risk", "emission", "blockers",
                "input_schema",
            ),
        )


def test_descriptor_cannot_be_disabled_without_a_reason() -> None:
    with pytest.raises(ValueError, match="disabled with no blocker"):
        ToolDescriptor(
            tool_id="t",
            name="t",
            description="d",
            risk=RiskClass.READ,
            emission=EmissionStatus.DISABLED,
            input_schema={"type": "object"},
            provenance=_descriptor_provenance(
                "tool_id", "name", "description", "kind", "risk", "emission", "input_schema"
            ),
        )


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_refused_tools_are_emitted_rather_than_omitted(example: str) -> None:
    """An omitted tool is indistinguishable from one that was never planned."""
    ir = _ir(example)
    plan = plan_baseline(ir)
    surface = generate_surface(ir, plan)
    assert len(surface.tools) == len(plan.artifacts)
    assert all(
        item.blocker_detail is not None
        for item in surface.tools
        if item.emission is EmissionStatus.DISABLED
    )


def test_a_composite_over_pure_reads_needs_no_confirmation() -> None:
    """It guards nothing, so demanding one asks a reviewer to authorise a risk that is absent.

    The gate applied to every composite while a composite could only be a prepare-then-commit
    pair. Once a lookup-then-use pair over reads became possible, that condition blocked an
    entire read-only surface for a reason that did not apply to it.
    """
    ir = _ir(ORDER_SERVICE)
    reads = [item for item in ir.operations if item.side_effect is SideEffectClass.READ]
    assert len(reads) >= 2, "the fixture needs two reads for this to mean anything"
    overlay = ToolOverlay(
        service_id=ir.service.service_id,
        source_digest=ir.service.source_digest,
        composites=[
            CompositeEntry(
                composite_id="c",
                name="read_then_read",
                description="Two reads in one call.",
                steps=[reads[0].operation_id, reads[1].operation_id],
                review_status=ReviewStatus.APPROVED,
            )
        ],
    )
    plan = plan_semantic(ir, overlay)
    plan = plan.model_copy(
        update={
            "artifacts": [
                item.model_copy(update={"review_status": ReviewStatus.APPROVED})
                for item in plan.artifacts
            ]
        }
    )
    composite = next(
        item for item in generate_surface(ir, plan).tools if len(item.source_operations) > 1
    )
    assert EmissionBlocker.COMPOSITE_PENDING_CONFIRMATION not in composite.blockers


def test_a_composite_spanning_a_write_still_needs_confirmation() -> None:
    """Relaxing the read-only case must not relax the case the gate exists for."""
    ir = _ir(ORDER_SERVICE)
    overlay = ToolOverlay(
        service_id=ir.service.service_id,
        source_digest=ir.service.source_digest,
        composites=[
            CompositeEntry(
                composite_id="c",
                name="create_then_approve",
                description="Create a refund and approve it.",
                steps=["createRefund", "approveRefund"],
                review_status=ReviewStatus.APPROVED,
            )
        ],
    )
    plan = plan_semantic(ir, overlay)
    plan = plan.model_copy(
        update={
            "artifacts": [
                item.model_copy(update={"review_status": ReviewStatus.APPROVED})
                for item in plan.artifacts
            ]
        }
    )
    composite = next(
        item for item in generate_surface(ir, plan).tools if len(item.source_operations) > 1
    )
    assert EmissionBlocker.COMPOSITE_PENDING_CONFIRMATION in composite.blockers
