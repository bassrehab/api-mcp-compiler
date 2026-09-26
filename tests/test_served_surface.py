"""What a generated server advertises over MCP, asked of a running server with the real SDK.

Every other test of a generated server reads its source or executes it with the SDK stubbed.
Neither can show what `tools/list` returns, and that is where the defect was: each tool took a
single `arguments: dict`, so the advertised input schema was an object with one free-form
property and the planned schema never left the process. The evaluation harness gave the model
the planned schema directly, so it measured a surface no deployed server presented.

These tests start the emitted module as a subprocess and talk to it over stdio, as an agent's
client would. Nothing here reaches an upstream service: the destructive call stops at the
confirmation gate and the SOAP call has an endpoint that does not resolve.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from api_mcp_compiler.codegen.mcp_server import emit_server
from api_mcp_compiler.codegen.soap_server import emit_soap_server
from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.models import EmissionStatus, RiskClass
from api_mcp_compiler.planning.approval import approve
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy
from tests.conftest import INVENTORY_SERVICE
from tests.test_soap_server import _reviewed

DESTRUCTIVE = "permanently_remove_item_record_warehouse"


def _inventory() -> tuple[str, dict[str, dict[str, Any]]]:
    """The inventory service with its destructive tool approved, and each tool's plan."""
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    overlay = approve(
        plan_semantic(ir), overlay=None, risk=RiskClass.DESTRUCTIVE, group=None, names=[]
    ).overlay
    approved = plan_semantic(ir, overlay)
    manifest = synthesize_policy(ir, approved)
    surface = generate_surface(ir, approved, manifest)
    planned = {
        item.name: item.input_schema
        for item in surface.tools
        if item.emission is EmissionStatus.EXECUTABLE and item.uri_template is None
    }
    return emit_server(ir, surface, manifest).source, planned


async def _session(source: str, tmp_path: Path, script: Any) -> Any:
    """Run a generated server and hand an initialised client session to `script`."""
    module = tmp_path / "generated_server.py"
    module.write_text(source)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(module)],
        # Credentials are read from the environment; a placeholder is enough to start.
        env={**os.environ, "SYNTHETIC_INVENTORY_SERVICE_INVENTORYOAUTH_CREDENTIAL": "unused"},
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return await script(session)


def _payload(result: Any) -> dict[str, Any]:
    if result.structuredContent is not None:
        return dict(result.structuredContent)
    return dict(json.loads(result.content[0].text))


def test_tools_list_advertises_the_planned_schema(tmp_path: Path) -> None:
    source, planned = _inventory()

    async def script(session: ClientSession) -> dict[str, Any]:
        return {tool.name: tool.inputSchema for tool in (await session.list_tools()).tools}

    advertised = asyncio.run(_session(source, tmp_path, script))

    assert advertised == planned


def test_the_advertised_arguments_are_the_ones_the_tool_accepts(tmp_path: Path) -> None:
    """Calling with the advertised shape reaches the gate; the old wrapper shape does not."""
    source, _ = _inventory()

    async def script(session: ClientSession) -> tuple[dict[str, Any], dict[str, Any]]:
        planned = await session.call_tool(DESTRUCTIVE, {"warehouse_id": "wh-7"})
        wrapped = await session.call_tool(DESTRUCTIVE, {"arguments": {"warehouse_id": "wh-7"}})
        return _payload(planned), _payload(wrapped)

    planned, wrapped = asyncio.run(_session(source, tmp_path, script))

    assert planned["status"] == "confirmation_required"
    assert wrapped["error"] == "invalid_arguments"


def test_annotations_and_resources_survive_the_change(tmp_path: Path) -> None:
    source, _ = _inventory()

    async def script(session: ClientSession) -> tuple[Any, list[str], list[str]]:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        resources = [str(item.uri) for item in (await session.list_resources()).resources]
        templates = [
            item.uriTemplate for item in (await session.list_resource_templates()).resourceTemplates
        ]
        return tools[DESTRUCTIVE].annotations, resources, templates

    annotations, resources, templates = asyncio.run(_session(source, tmp_path, script))

    assert annotations.destructiveHint is True
    assert annotations.readOnlyHint is False
    assert "surface://withheld" in resources
    assert any(template.endswith("/items-v1") for template in templates)


def test_an_unknown_tool_is_an_error_not_a_crash(tmp_path: Path) -> None:
    source, _ = _inventory()

    async def script(session: ClientSession) -> Any:
        return await session.call_tool("no_such_tool", {})

    result = asyncio.run(_session(source, tmp_path, script))

    assert result.isError is True


def test_a_soap_server_advertises_its_planned_schema_too(tmp_path: Path) -> None:
    ir, surface, manifest = _reviewed()
    planned = {
        item.name: item.input_schema
        for item in surface.tools
        if item.emission is EmissionStatus.EXECUTABLE
    }
    source = emit_soap_server(ir, surface, manifest).source

    async def script(session: ClientSession) -> dict[str, Any]:
        return {tool.name: tool.inputSchema for tool in (await session.list_tools()).tools}

    assert asyncio.run(_session(source, tmp_path, script)) == planned
