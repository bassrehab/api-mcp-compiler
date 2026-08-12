"""Tests that a reclassified read is emitted as something a client can address.

The planner has always been able to propose that an addressable read become a resource, and
the surface recorded the decision while codegen emitted a tool anyway. The reclassification
survived as far as the plan and was discarded at the last step, which made it a label rather
than a decision.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from api_mcp_compiler.codegen.mcp_server import emit_server
from api_mcp_compiler.codegen.tools import generate_surface, uri_template
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.ingest.wsdl import parse_wsdl
from api_mcp_compiler.models import ArtifactKind
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "examples" / "openapi" / "inventory_service.yaml"
CUSTOMER = ROOT / "examples" / "wsdl" / "customer_service.wsdl"


def _surface(path: Path) -> Any:
    ir = parse_openapi(path)
    plan = plan_semantic(ir)
    return ir, generate_surface(ir, plan, synthesize_policy(ir, plan))


def test_a_resource_carries_the_address_it_is_read_by() -> None:
    _, surface = _surface(INVENTORY)
    resource = next(item for item in surface.tools if item.kind is ArtifactKind.RESOURCE)

    assert resource.uri_template == (
        "synthetic-inventory-service://warehouses/{warehouse_id}/items-v1"
    )


def test_the_scheme_is_the_service_so_two_surfaces_cannot_collide() -> None:
    """Mounted alongside each other, `warehouses/{id}` from two services must differ."""
    _, surface = _surface(INVENTORY)
    resource = next(item for item in surface.tools if item.kind is ArtifactKind.RESOURCE)

    assert resource.uri_template is not None
    assert resource.uri_template.startswith(f"{surface.service_id}://")


def test_a_tool_carries_no_address() -> None:
    """Only a resource is addressable; a template on a tool would claim otherwise."""
    _, surface = _surface(INVENTORY)

    for item in surface.tools:
        if item.kind is not ArtifactKind.RESOURCE:
            assert item.uri_template is None


def test_the_template_is_recorded_with_provenance() -> None:
    """Every informative field says where it came from, and this one is no exception."""
    _, surface = _surface(INVENTORY)
    resource = next(item for item in surface.tools if item.kind is ArtifactKind.RESOURCE)

    fields = {record.field for record in resource.provenance}
    assert "uri_template" in fields


def test_a_soap_operation_has_no_addressable_form() -> None:
    """Every SOAP operation is a POST to one endpoint, and a URI cannot carry the envelope."""
    ir = parse_wsdl(CUSTOMER)
    plan = plan_semantic(ir)
    surface = generate_surface(ir, plan, synthesize_policy(ir, plan))

    assert all(item.uri_template is None for item in surface.tools)


def test_a_resource_whose_inputs_exceed_its_address_stays_a_tool() -> None:
    """An address that cannot express an input would be unreadable, so it is not offered."""
    ir = parse_openapi(INVENTORY)
    plan = plan_semantic(ir)
    artifact = next(item for item in plan.artifacts if item.kind is ArtifactKind.RESOURCE)
    # The read that was reclassified, but paired with an operation that takes a query filter.
    querying = next(
        item for item in ir.operations if item.operation_id == "listWarehouseItems"
    )

    assert uri_template(ir.service.service_id, artifact, querying) is None


def test_the_generated_server_registers_it_as_a_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registered with `@mcp.tool`, the reclassification would be lost at the last step."""
    ir = parse_openapi(INVENTORY)
    plan = plan_semantic(ir)
    manifest = synthesize_policy(ir, plan)
    source = emit_server(ir, generate_surface(ir, plan, manifest), manifest).source

    registered: list[tuple[str, str | None]] = []
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = lambda *_, **__: types.SimpleNamespace(  # type: ignore[attr-defined]
        tool=lambda **kw: (lambda function: registered.append(("tool", kw.get("name")))
                           or function),
        resource=lambda uri, **kw: (lambda function: registered.append((uri, kw.get("name")))
                                    or function),
    )
    server = types.ModuleType("mcp.server")
    server.fastmcp = fastmcp  # type: ignore[attr-defined]
    package = types.ModuleType("mcp")
    package.server = server  # type: ignore[attr-defined]
    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = lambda **_: None  # type: ignore[attr-defined]
    for name, module in {
        "mcp": package, "mcp.server": server, "mcp.server.fastmcp": fastmcp, "httpx": httpx
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    exec(compile(source, "<generated server>", "exec"), {"__name__": "generated_server"})

    addressed = dict(registered)
    assert (
        addressed["synthetic-inventory-service://warehouses/{warehouse_id}/items-v1"]
        == "list_items_using_retired_v1"
    )
    assert ("tool", "list_items_using_retired_v1") not in registered
