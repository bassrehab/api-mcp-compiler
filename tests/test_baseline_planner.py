"""Baseline planner tests.

The baseline exists to be a controlled comparison point, so its defining property is that
it adds nothing: no renaming, no grouping, no omission, no risk escalation. These tests
pin that property, because a baseline that quietly improves would invalidate every measured
comparison against the semantic planner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api_mcp_compiler.contracts import validate_tool_plan
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.ingest.wsdl import parse_wsdl
from api_mcp_compiler.models import (
    SIDE_EFFECT_TO_RISK,
    ApiSemanticIR,
    ArtifactKind,
    PlannerKind,
    ReviewStatus,
    RiskClass,
)
from api_mcp_compiler.planning.baseline import operation_per_tool, plan_baseline
from tests.conftest import ALL_EXAMPLES, CUSTOMER_SERVICE, ORDER_SERVICE


def _ir(example: str) -> ApiSemanticIR:
    source = Path(example)
    return parse_wsdl(source) if source.suffix == ".wsdl" else parse_openapi(source)


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_one_artifact_per_operation_in_source_order(example: str) -> None:
    ir = _ir(example)
    plan = plan_baseline(ir)
    assert [item.name for item in plan.artifacts] == [
        item.operation_id for item in ir.operations
    ]


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_plan_validates_against_the_schema(example: str) -> None:
    validate_tool_plan(plan_baseline(_ir(example)).model_dump(mode="json"), label=example)


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_risk_is_the_identity_mapping_of_the_side_effect(example: str) -> None:
    ir = _ir(example)
    plan = plan_baseline(ir)
    for operation, artifact in zip(ir.operations, plan.artifacts, strict=True):
        assert artifact.risk is SIDE_EFFECT_TO_RISK[operation.side_effect]


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_baseline_never_escalates_an_artifact_to_privileged(example: str) -> None:
    """Only the semantic planner and policy synthesis may assign `privileged`."""
    plan = plan_baseline(_ir(example))
    assert all(item.risk is not RiskClass.PRIVILEGED for item in plan.artifacts)


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_every_artifact_awaits_human_review(example: str) -> None:
    plan = plan_baseline(_ir(example))
    assert all(item.review_status is ReviewStatus.PROPOSED for item in plan.artifacts)
    assert all(item.kind is ArtifactKind.TOOL for item in plan.artifacts)


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_plan_is_tied_to_the_source_revision(example: str) -> None:
    ir = _ir(example)
    plan = plan_baseline(ir)
    assert plan.planner is PlannerKind.BASELINE
    assert plan.service_id == ir.service.service_id
    assert plan.source_digest == ir.service.source_digest


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_no_operation_is_merged_renamed_or_omitted(example: str) -> None:
    ir = _ir(example)
    plan = plan_baseline(ir)
    assert len(plan.artifacts) == len(ir.operations)
    for operation, artifact in zip(ir.operations, plan.artifacts, strict=True):
        assert artifact.source_operations == [operation.operation_id]
        assert artifact.name == operation.operation_id
        assert artifact.artifact_id == f"tool:{operation.operation_id}"


def test_description_falls_back_to_intent_when_absent() -> None:
    ir = parse_openapi(Path(ORDER_SERVICE))
    operation = next(item for item in ir.operations if item.operation_id == "getCustomer")
    artifact = next(
        item for item in plan_baseline(ir).artifacts if item.name == "getCustomer"
    )
    assert operation.description is None
    assert artifact.description == operation.intent


def test_unclassified_soap_operations_carry_unknown_risk() -> None:
    ir = parse_wsdl(Path(CUSTOMER_SERVICE))
    plan = plan_baseline(ir)
    assert [item.risk for item in plan.artifacts] == [RiskClass.UNKNOWN]


def test_operation_per_tool_and_plan_baseline_agree() -> None:
    ir = parse_openapi(Path(ORDER_SERVICE))
    assert operation_per_tool(ir) == plan_baseline(ir).artifacts


def test_empty_ir_produces_an_empty_plan(tmp_path: Path) -> None:
    spec = tmp_path / "empty.yaml"
    spec.write_text("openapi: 3.0.3\ninfo: {title: Empty, version: '1'}\npaths: {}\n", "utf-8")
    plan = plan_baseline(parse_openapi(spec))
    assert plan.artifacts == []
    validate_tool_plan(plan.model_dump(mode="json"))
