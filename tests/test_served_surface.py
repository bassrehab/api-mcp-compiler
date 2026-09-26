"""What a generated server advertises over MCP, asked of a running server with the real SDK.

Every other test of a generated server reads its source or executes it with the SDK stubbed.
Neither can show what `tools/list` returns, and that is where the defect was: each tool took a
single `arguments: dict`, so the advertised input schema was an object with one free-form
property and the planned schema never left the process. The evaluation harness gave the model
the planned schema directly, so it measured a surface no deployed server presented.

These tests start the emitted module as a subprocess and talk to it over stdio, as an agent's
client would. They emit for whichever SDK major is installed, because the server subprocess runs
in this environment; CI runs them once on each major. On 2.x they also speak the 2026-07-28
protocol directly, since `ClientSession` negotiates the earlier handshake and would otherwise
leave the new era untested. Nothing here reaches an upstream service: the destructive call
stops at the confirmation gate and the SOAP call has an endpoint that does not resolve.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
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

#: The SDK major installed here, which is the target every server in this module is emitted for.
SDK = int(importlib.metadata.version("mcp").split(".")[0])

#: The credential the inventory example reads; a placeholder is enough to start the server.
ENV = {**os.environ, "SYNTHETIC_INVENTORY_SERVICE_INVENTORYOAUTH_CREDENTIAL": "unused"}


def _field(item: Any, snake: str, camel: str) -> Any:
    """Read a field under either SDK's naming: 2.x is snake_case, 1.x is camelCase."""
    value = getattr(item, snake, None)
    return value if value is not None else getattr(item, camel, None)


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
    return emit_server(ir, surface, manifest, sdk=SDK).source, planned


async def _session(source: str, tmp_path: Path, script: Any) -> Any:
    """Run a generated server and hand an initialised client session to `script`."""
    module = tmp_path / "generated_server.py"
    module.write_text(source)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(module)],
        env=ENV,
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return await script(session)


def _payload(result: Any) -> dict[str, Any]:
    structured = _field(result, "structured_content", "structuredContent")
    if structured is not None:
        return dict(structured)
    return dict(json.loads(result.content[0].text))


def test_tools_list_advertises_the_planned_schema(tmp_path: Path) -> None:
    source, planned = _inventory()

    async def script(session: ClientSession) -> dict[str, Any]:
        tools = (await session.list_tools()).tools
        return {tool.name: _field(tool, "input_schema", "inputSchema") for tool in tools}

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
        listed = await session.list_resource_templates()
        templates = [
            _field(item, "uri_template", "uriTemplate")
            for item in _field(listed, "resource_templates", "resourceTemplates")
        ]
        return tools[DESTRUCTIVE].annotations, resources, templates

    annotations, resources, templates = asyncio.run(_session(source, tmp_path, script))

    assert _field(annotations, "destructive_hint", "destructiveHint") is True
    assert _field(annotations, "read_only_hint", "readOnlyHint") is False
    assert "surface://withheld" in resources
    assert any(template.endswith("/items-v1") for template in templates)


def test_an_unknown_tool_is_an_error_not_a_crash(tmp_path: Path) -> None:
    """A tool result with isError on 1.x; the protocol error 2026-07-28 prescribes on 2.x."""
    source, _ = _inventory()

    async def script(session: ClientSession) -> Any:
        try:
            return await session.call_tool("no_such_tool", {})
        except Exception as error:  # the SDK raises the protocol error it received
            return error

    outcome = asyncio.run(_session(source, tmp_path, script))

    if SDK >= 2:
        assert isinstance(outcome, Exception)
        assert "Unknown tool" in str(outcome)
    else:
        assert _field(outcome, "is_error", "isError") is True


def test_a_soap_server_advertises_its_planned_schema_too(tmp_path: Path) -> None:
    ir, surface, manifest = _reviewed()
    planned = {
        item.name: item.input_schema
        for item in surface.tools
        if item.emission is EmissionStatus.EXECUTABLE
    }
    source = emit_soap_server(ir, surface, manifest, sdk=SDK).source

    async def script(session: ClientSession) -> dict[str, Any]:
        tools = (await session.list_tools()).tools
        return {tool.name: _field(tool, "input_schema", "inputSchema") for tool in tools}

    assert asyncio.run(_session(source, tmp_path, script)) == planned


#: Per-request metadata a 2026-07-28 client sends in place of the initialize handshake.
_MODERN = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.mark.skipif(SDK < 2, reason="the 1.x target serves protocol versions up to 2025-11-25")
def test_the_2026_07_28_protocol_is_served_without_a_handshake(tmp_path: Path) -> None:
    """Raw JSON-RPC, so nothing negotiates down to the earlier era on the test's behalf."""
    source, planned = _inventory()
    module = tmp_path / "generated_server.py"
    module.write_text(source)
    server = subprocess.Popen(
        [sys.executable, str(module)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=ENV,
    )
    assert server.stdin is not None and server.stdout is not None

    def rpc(identifier: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        message = {"jsonrpc": "2.0", "id": identifier, "method": method,
                   "params": {**params, "_meta": _MODERN}}
        server.stdin.write(json.dumps(message) + "\n")  # type: ignore[union-attr]
        server.stdin.flush()  # type: ignore[union-attr]
        return dict(json.loads(server.stdout.readline()))  # type: ignore[union-attr]

    try:
        discovered = rpc(1, "server/discover", {})["result"]
        listed = rpc(2, "tools/list", {})["result"]
        called = rpc(3, "tools/call", {"name": DESTRUCTIVE, "arguments": {"warehouse_id": "wh-7"}})
        unknown = rpc(4, "tools/call", {"name": "no_such_tool", "arguments": {}})
    finally:
        server.kill()
        server.wait()

    assert "2026-07-28" in discovered["supportedVersions"]
    assert listed["resultType"] == "complete"
    assert {tool["name"]: tool["inputSchema"] for tool in listed["tools"]} == planned
    assert called["result"]["resultType"] == "complete"
    assert called["result"]["structuredContent"]["status"] == "confirmation_required"
    assert unknown["error"]["code"] == -32602


@pytest.mark.skipif(SDK < 2, reason="the extension hints move to _meta only on the 2.x target")
def test_namespaced_hints_travel_in_meta_on_2x(tmp_path: Path) -> None:
    """SDK 2.x drops annotation keys it does not know, so the extensions ride in `_meta`."""
    source, _ = _inventory()

    async def script(session: ClientSession) -> Any:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        return _field(tools[DESTRUCTIVE], "meta", "_meta")

    meta = asyncio.run(_session(source, tmp_path, script))

    assert meta["x-rotaforge/reversibleHint"] is False
    assert "x-rotaforge/sensitiveHint" in meta
