"""List-cache and routing-header policy: what is derived, and what a served surface does with it.

Both are MCP 2026-07-28 features a generated server could simply default. They are derived
instead, and written into the governance manifest, because each is a governance decision: how
long a revoked tool may linger in a client's cache, and which arguments every intermediary on
the path gets to see.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from api_mcp_compiler.codegen.mcp_server import emit_server
from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.ingest.wsdl import parse_wsdl
from api_mcp_compiler.models import CacheScope, RiskClass
from api_mcp_compiler.planning.approval import approve
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import _header_name, synthesize_policy
from tests.conftest import CUSTOMER_SERVICE, INVENTORY_SERVICE, ORDER_SERVICE

SDK = int(importlib.metadata.version("mcp").split(".")[0])

#: RFC 9110 token syntax, which MCP 2026-07-28 requires of an `x-mcp-header` value.
TOKEN = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")


def _manifest(spec: str) -> Any:
    ir = parse_wsdl(Path(spec)) if spec.endswith(".wsdl") else parse_openapi(Path(spec))
    return synthesize_policy(ir, plan_semantic(ir))


def _headers(manifest: Any) -> dict[str, list[tuple[str, str]]]:
    return {
        policy.tool_name: [(item.argument, item.header) for item in policy.routing_headers]
        for policy in manifest.policies
    }


# What is derived


def test_a_path_identifier_is_mirrored_under_a_hyphenated_name() -> None:
    headers = _headers(_manifest(INVENTORY_SERVICE))
    assert headers["list_items_held_warehouse"] == [("warehouse_id", "Warehouse-Id")]


def test_a_tool_with_no_path_identifier_gets_no_header() -> None:
    headers = _headers(_manifest(ORDER_SERVICE))
    assert headers["create_refund_request"] == []
    assert headers["approve_refund_and_release_payment"] == [("refund_id", "Refund-Id")]


def test_a_soap_operation_has_no_path_to_mirror() -> None:
    assert _headers(_manifest(CUSTOMER_SERVICE)) == {"get_customer": []}


@pytest.mark.parametrize(
    ("argument", "header"),
    [
        ("warehouse_id", "Warehouse-Id"),
        ("warehouseId", "Warehouse-Id"),
        ("id", "Id"),
        ("tenant-name", "Tenant-Name"),
    ],
)
def test_header_names_are_tokens_without_underscores(argument: str, header: str) -> None:
    """Some proxies drop request headers whose names contain an underscore."""
    assert _header_name(argument) == header
    assert TOKEN.match(header) and "_" not in header


def test_every_derived_header_meets_the_spec_constraints() -> None:
    for spec in (INVENTORY_SERVICE, ORDER_SERVICE, CUSTOMER_SERVICE):
        for policy in _manifest(spec).policies:
            names = [item.header.lower() for item in policy.routing_headers]
            assert len(names) == len(set(names)), "header names must be case-insensitively unique"
            for item in policy.routing_headers:
                assert TOKEN.match(item.header)
                assert item.provenance, "a routing decision without provenance cannot be reviewed"


def test_a_surface_that_changes_state_gets_the_short_ttl() -> None:
    assert _manifest(INVENTORY_SERVICE).list_cache.ttl_ms == 60_000


def test_a_read_only_surface_gets_the_long_ttl() -> None:
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    plan = plan_semantic(ir)
    reads = plan.model_copy(
        update={"artifacts": [item for item in plan.artifacts if item.risk is RiskClass.READ]}
    )
    assert synthesize_policy(ir, reads).list_cache.ttl_ms == 300_000


def test_a_surface_behind_credentials_is_private_and_one_without_is_public() -> None:
    assert _manifest(INVENTORY_SERVICE).list_cache.scope is CacheScope.PRIVATE
    assert _manifest(ORDER_SERVICE).list_cache.scope is CacheScope.PUBLIC


# What a served surface does with it

ENV = {
    **os.environ,
    "SYNTHETIC_INVENTORY_SERVICE_INVENTORYOAUTH_CREDENTIAL": "unused",
    "SYNTHETIC_INVENTORY_SERVICE_REQUEST_STATE_KEY": "ephemeral",
}
MODERN = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _served_source(sdk: int) -> str:
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    overlay = approve(
        plan_semantic(ir), overlay=None, risk=RiskClass.DESTRUCTIVE, group=None, names=[]
    ).overlay
    approved = plan_semantic(ir, overlay)
    manifest = synthesize_policy(ir, approved)
    return emit_server(ir, generate_surface(ir, approved, manifest), manifest, sdk=sdk).source


def test_the_1x_target_does_not_advertise_2026_features() -> None:
    source = _served_source(1)
    assert "x-mcp-header" not in source
    assert "_LIST_CACHE: dict[str, Any] = json.loads('{}')" in source


@pytest.mark.skipif(SDK < 2, reason="list hints and routing headers are 2026-07-28 features")
def test_lists_carry_the_policy_hints_and_tools_carry_the_routing_header(tmp_path: Path) -> None:
    module = tmp_path / "server.py"
    module.write_text(_served_source(2))
    server = subprocess.Popen(
        [sys.executable, str(module)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=ENV,
    )
    assert server.stdin is not None and server.stdout is not None
    results = {}
    try:
        for index, method in enumerate(
            ["tools/list", "resources/list", "resources/templates/list"], start=1
        ):
            message = {"jsonrpc": "2.0", "id": index, "method": method,
                       "params": {"_meta": MODERN}}
            server.stdin.write(json.dumps(message) + "\n")
            server.stdin.flush()
            results[method] = json.loads(server.stdout.readline())["result"]
    finally:
        server.kill()
        server.wait()

    for method, result in results.items():
        assert result["ttlMs"] == 60_000, method
        assert result["cacheScope"] == "private", method
    for tool in results["tools/list"]["tools"]:
        assert tool["inputSchema"]["properties"]["warehouse_id"]["x-mcp-header"] == "Warehouse-Id"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.skipif(SDK < 2, reason="routing headers are a 2026-07-28 feature")
def test_a_client_sends_the_routing_header_a_gateway_can_see(tmp_path: Path) -> None:
    """Over Streamable HTTP, the SDK client mirrors the argument into `Mcp-Param-Warehouse-Id`."""
    module = tmp_path / "server.py"
    module.write_text(_served_source(2))
    log = tmp_path / "headers.jsonl"
    port = _free_port()
    runner = tmp_path / "run_http.py"
    runner.write_text(
        "import json, runpy, sys\n"
        "import uvicorn\n"
        f"ns = runpy.run_path({str(module)!r}, run_name='generated')\n"
        "app = ns['mcp'].streamable_http_app()\n"
        "class Log:\n"
        "    def __init__(self, app): self.app = app\n"
        "    async def __call__(self, scope, receive, send):\n"
        "        if scope['type'] == 'http':\n"
        "            seen = {k.decode().lower(): v.decode() for k, v in scope['headers']}\n"
        "            mcp = {k: v for k, v in seen.items() if k.startswith('mcp-')}\n"
        f"            with open({str(log)!r}, 'a') as out: out.write(json.dumps(mcp) + '\\n')\n"
        "        await self.app(scope, receive, send)\n"
        f"uvicorn.run(Log(app), host='127.0.0.1', port={port}, log_level='error')\n"
    )
    server = subprocess.Popen(
        [sys.executable, str(runner)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV
    )
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

        from mcp.client.client import Client

        async def call() -> None:
            async with Client(server=f"http://127.0.0.1:{port}/mcp", mode="2026-07-28") as client:
                await client.list_tools()
                # The upstream does not resolve; the header the call carried is the point.
                with contextlib.suppress(Exception):
                    await client.call_tool("list_items_held_warehouse", {"warehouse_id": "wh-7"})

        asyncio.run(call())
    finally:
        server.kill()
        server.wait()

    calls = [
        json.loads(line)
        for line in log.read_text().splitlines()
        if json.loads(line).get("mcp-method") == "tools/call"
    ]
    assert calls, "no tools/call request reached the server"
    assert calls[0]["mcp-name"] == "list_items_held_warehouse"
    assert calls[0]["mcp-param-warehouse-id"] == "wh-7"
