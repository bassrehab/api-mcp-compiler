"""Shared test fixtures.

Example specifications are addressed by repository-relative path, and the resulting
`source_uri` is recorded in the IR and in the golden artifacts. The working directory is
therefore pinned to the repository root for the whole session, so that a golden comparison
cannot pass or fail depending on where pytest happened to be invoked from.
"""

from __future__ import annotations

import os
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"

ORDER_SERVICE = "examples/openapi/order_service.yaml"
INVENTORY_SERVICE = "examples/openapi/inventory_service.yaml"
CUSTOMER_SERVICE = "examples/wsdl/customer_service.wsdl"

OPENAPI_EXAMPLES = (ORDER_SERVICE, INVENTORY_SERVICE)
WSDL_EXAMPLES = (CUSTOMER_SERVICE,)
ALL_EXAMPLES = (*OPENAPI_EXAMPLES, *WSDL_EXAMPLES)


@pytest.fixture(autouse=True, scope="session")
def _run_from_repo_root() -> Iterator[None]:
    """Pin the working directory to the repository root for the whole test session."""
    previous = Path.cwd()
    os.chdir(REPO_ROOT)
    try:
        yield
    finally:
        os.chdir(previous)


class _EphemeralStateKeys(dict[str, str]):
    """The environment, with any unset `*_REQUEST_STATE_KEY` reading as `ephemeral`."""

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        if key.endswith("_REQUEST_STATE_KEY") and key not in self:
            return "ephemeral"
        return super().get(key, default)


class _StubServer:
    """Stands in for the SDK's server class when a generated module is executed in a test.

    A class rather than a namespace, because the generated module subclasses it: `_Surface`
    overrides `list_tools` and `call_tool` so the planned schema is what a client sees.
    """

    def __init__(self, *_: Any, **__: Any) -> None:
        #: (uri, name) for every resource the generated module registers, in order.
        self.resources: list[tuple[str, str | None]] = []

    def resource(self, uri: str, **options: Any) -> Any:
        def register(function: Any) -> Any:
            self.resources.append((uri, options.get("name")))
            return function

        return register


def stub_mcp_sdk(monkeypatch: pytest.MonkeyPatch, httpx_client: Any) -> None:
    """Load a generated server with the MCP SDK and the HTTP client replaced.

    Tests that use this are about governance decisions taken before either is reached. What a
    real SDK advertises is tested separately, against the real SDK, in test_served_surface.py:
    a stub cannot show what `tools/list` returns, which is how an opaque schema shipped.

    Both SDK majors are stubbed, so a module emitted for either target loads.
    """
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = _StubServer  # type: ignore[attr-defined]
    mcpserver = types.ModuleType("mcp.server.mcpserver")
    mcpserver.MCPServer = _StubServer  # type: ignore[attr-defined]
    mcpserver.RequestStateSecurity = lambda **options: options  # type: ignore[attr-defined]
    caching = types.ModuleType("mcp.server.caching")
    caching.CacheHint = lambda **options: options  # type: ignore[attr-defined]
    exceptions = types.ModuleType("mcp.shared.exceptions")
    exceptions.MCPError = type("MCPError", (Exception,), {})  # type: ignore[attr-defined]
    shared = types.ModuleType("mcp.shared")
    shared.exceptions = exceptions  # type: ignore[attr-defined]
    mcp_types = types.ModuleType("mcp.types")
    for name in (
        "Tool",
        "CallToolResult",
        "TextContent",
        "ElicitRequest",
        "ElicitRequestFormParams",
        "InputRequiredResult",
    ):
        setattr(mcp_types, name, lambda **fields: fields)
    server = types.ModuleType("mcp.server")
    server.fastmcp = fastmcp  # type: ignore[attr-defined]
    server.mcpserver = mcpserver  # type: ignore[attr-defined]
    server.caching = caching  # type: ignore[attr-defined]
    package = types.ModuleType("mcp")
    package.server = server  # type: ignore[attr-defined]
    package.shared = shared  # type: ignore[attr-defined]
    package.types = mcp_types  # type: ignore[attr-defined]
    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = httpx_client  # type: ignore[attr-defined]
    # A surface with a confirmation-gated tool refuses to start without a request-state key.
    # These tests are about what happens after a server starts, so an unset key reads as the
    # single-process choice; the refusal itself is tested in test_confirmation_gate.py.
    monkeypatch.setattr(os, "environ", _EphemeralStateKeys(os.environ))
    for name, module in {
        "mcp": package,
        "mcp.server": server,
        "mcp.server.fastmcp": fastmcp,
        "mcp.server.mcpserver": mcpserver,
        "mcp.server.caching": caching,
        "mcp.shared": shared,
        "mcp.shared.exceptions": exceptions,
        "mcp.types": mcp_types,
        "httpx": httpx,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
