"""Generated SOAP server tests.

A SOAP service has no routes: every operation is a POST to one endpoint and what distinguishes
them is the envelope. These check that the envelope is built from what the specification says
and that the things this emitter cannot write are refused rather than approximated.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from api_mcp_compiler.codegen.soap_server import SoapEmissionError, emit_soap_server
from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.ingest.wsdl import parse_wsdl
from api_mcp_compiler.models import (
    OverlayEntry,
    ReviewStatus,
    SideEffectClass,
    ToolOverlay,
)
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy
from tests.conftest import CUSTOMER_SERVICE


def _reviewed(side_effect: SideEffectClass = SideEffectClass.READ):
    """Classify the operation the way a reviewer must before anything SOAP can be emitted."""
    ir = parse_wsdl(Path(CUSTOMER_SERVICE))
    overlay = ToolOverlay(
        service_id=ir.service.service_id,
        source_digest=ir.service.source_digest,
        entries=[
            OverlayEntry(
                operation_id="GetCustomer",
                review_status=ReviewStatus.APPROVED,
                side_effect=side_effect,
            )
        ],
    )
    plan = plan_semantic(ir, overlay)
    return ir, generate_surface(ir, plan), synthesize_policy(ir, plan)


def test_a_reviewer_classification_is_what_releases_a_soap_operation() -> None:
    """WSDL carries no signal to infer a side effect from, so nothing else can release it."""
    ir = parse_wsdl(Path(CUSTOMER_SERVICE))
    unreviewed = generate_surface(ir, plan_semantic(ir))
    assert unreviewed.tools[0].emission.value != "executable"

    _, surface, _ = _reviewed()
    assert surface.tools[0].emission.value == "executable"


def test_the_generated_soap_server_is_valid_python() -> None:
    ir, surface, manifest = _reviewed()
    ast.parse(emit_soap_server(ir, surface, manifest).source)


def test_the_envelope_carries_what_the_specification_declared() -> None:
    ir, surface, manifest = _reviewed()
    emitted = emit_soap_server(ir, surface, manifest)
    soap = ir.operations[0].soap
    assert soap is not None
    assert repr(soap.soap_action) in emitted.source
    assert repr(soap.target_namespace) in emitted.source
    assert emitted.endpoint == soap.endpoint


def test_a_caller_supplied_value_is_escaped() -> None:
    """An argument reaches an XML document, so anything else would be an injection."""
    ir, surface, manifest = _reviewed()
    assert "escape(str(value))" in emit_soap_server(ir, surface, manifest).source


def test_a_fault_is_reported_as_a_fault_rather_than_a_transport_error() -> None:
    """It arrives with HTTP 500 and a body that explains itself; an agent can react to that."""
    ir, surface, manifest = _reviewed()
    source = emit_soap_server(ir, surface, manifest).source
    assert "soap_fault" in source
    assert "Fault" in source


def test_policy_travels_with_a_soap_tool_too() -> None:
    ir, surface, manifest = _reviewed(SideEffectClass.DESTRUCTIVE)
    source = emit_soap_server(ir, surface, manifest).source
    assert "requires_confirmation" in source
    assert "max_output_bytes" in source
    assert "redact_fields" in source


def test_an_unclassified_surface_refuses_to_emit() -> None:
    ir = parse_wsdl(Path(CUSTOMER_SERVICE))
    with pytest.raises(SoapEmissionError, match="side effect classified"):
        emit_soap_server(ir, generate_surface(ir, plan_semantic(ir)))


def test_no_credential_is_written_into_the_generated_file() -> None:
    ir, surface, manifest = _reviewed()
    source = emit_soap_server(ir, surface, manifest).source
    assert 'os.environ.get(described["env"])' in source
    for marker in ("password", "sk-", "bearer ey"):
        assert marker not in source.lower()
