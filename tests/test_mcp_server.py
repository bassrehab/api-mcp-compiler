"""Generated MCP server tests.

The emitter is the first stage whose output is meant to be run rather than read, so these
check the properties that would let a deployment quietly undo an earlier decision.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from api_mcp_compiler.codegen.mcp_server import ServerEmissionError, emit_server
from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.ingest.wsdl import parse_wsdl
from api_mcp_compiler.models import EmissionStatus, ReviewStatus, ToolPlan
from api_mcp_compiler.planning.baseline import plan_baseline
from api_mcp_compiler.planning.overlay import load_overlay
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy
from tests.conftest import CUSTOMER_SERVICE, ORDER_SERVICE

ORDER_OVERLAY = "examples/overlays/order_service.overlay.json"


def _approved(plan: ToolPlan) -> ToolPlan:
    return plan.model_copy(
        update={
            "artifacts": [
                item.model_copy(update={"review_status": ReviewStatus.APPROVED})
                for item in plan.artifacts
            ]
        }
    )


def _emit(approve_all: bool = True):
    ir = parse_openapi(Path(ORDER_SERVICE))
    plan = plan_semantic(ir, load_overlay(Path(ORDER_OVERLAY)))
    if approve_all:
        plan = _approved(plan)
    surface = generate_surface(ir, plan)
    return ir, surface, emit_server(ir, surface, synthesize_policy(ir, plan))


def test_the_generated_server_is_valid_python() -> None:
    _, _, emitted = _emit()
    ast.parse(emitted.source)


def test_only_tools_that_cleared_the_gate_are_registered() -> None:
    """A server that registered a withheld tool would undo the gate at the last step."""
    _, surface, emitted = _emit(approve_all=False)
    executable = {
        item.name for item in surface.tools if item.emission is EmissionStatus.EXECUTABLE
    }
    assert set(emitted.registered) == executable
    for name in emitted.withheld:
        assert f"async def {name}(" not in emitted.source


def test_a_withheld_tool_is_named_with_its_reason() -> None:
    """The surface documents what it withheld rather than appearing to be everything."""
    _, _, emitted = _emit(approve_all=False)
    assert emitted.withheld
    for reason in emitted.withheld.values():
        assert reason
    assert 'surface://withheld' in emitted.source


def test_policy_is_written_into_the_server_not_left_beside_it() -> None:
    """A deployment must not be able to take the tools and leave the governance."""
    _, _, emitted = _emit()
    assert "requires_confirmation" in emitted.source
    assert "max_output_bytes" in emitted.source
    assert "redact_fields" in emitted.source


def test_a_confirmation_is_bound_to_its_arguments() -> None:
    """Confirming one action must not authorise another."""
    _, _, emitted = _emit()
    assert "def _digest(tool_name: str, arguments: dict[str, Any]) -> str:" in emitted.source
    assert "sort_keys=True" in emitted.source


def test_no_credential_is_written_into_the_generated_file() -> None:
    _, _, emitted = _emit()
    lowered = emitted.source.lower()
    for marker in ("sk-", "bearer ey", "password", "api_key ="):
        assert marker not in lowered
    assert 'os.environ.get(described["env"])' in emitted.source


def test_arguments_are_validated_before_the_call_is_made() -> None:
    """A surface that validates after calling has already had the effect it was checking."""
    _, _, emitted = _emit()
    validate_at = emitted.source.index("iter_errors(arguments)")
    call_at = emitted.source.index("await client.request(")
    assert validate_at < call_at


def test_a_surface_with_nothing_executable_refuses_to_emit() -> None:
    """Reads clear the gate on their own, so this needs a surface where none did."""
    ir = parse_openapi(Path(ORDER_SERVICE))
    surface = generate_surface(ir, plan_baseline(ir))
    withheld_only = surface.model_copy(
        update={
            "tools": [
                item
                for item in surface.tools
                if item.emission is not EmissionStatus.EXECUTABLE
            ]
        }
    )
    with pytest.raises(ServerEmissionError, match="no executable tools"):
        emit_server(ir, withheld_only)


def test_a_soap_surface_is_refused_rather_than_served_wrongly() -> None:
    """WSDL operations carry no route, and inventing one would be a guess about transport."""
    ir = parse_wsdl(Path(CUSTOMER_SERVICE))
    plan = _approved(plan_semantic(ir))
    surface = generate_surface(ir, plan)
    with pytest.raises(ServerEmissionError):
        emit_server(ir, surface)
