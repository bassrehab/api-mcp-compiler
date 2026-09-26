"""How a generated server tells a client what its tools accept.

The SDK's high-level server derives each tool's input schema from the Python signature of the
function behind it. Generated tools took one parameter, `arguments: dict`, and validated it
against the planned schema inside the function, so `tools/list` advertised an object with a
single free-form property and the planned schema never left the process. An agent talking to
the served artifact had to guess parameter names from a description, while the evaluation
harness handed the model the planned schema directly. The two were measuring different
surfaces.

The planned schema cannot be expressed as a Python signature without loss. It is JSON Schema
2020-12 with composition, `$ref` and facets that no annotation carries. So the generated
server overrides the two public methods the SDK binds its tool handlers to, `list_tools` and
`call_tool`, and advertises the planned schema as written. Resources, instructions and
transport stay with the SDK.

There are two versions of the class, one per SDK major. They differ where the SDK does: 2.x
names the server `MCPServer`, types results rather than accepting a dict, and drops annotation
keys it does not know, so the namespaced hints move to `_meta`, the protocol's extension slot.

This is text written into the generated module. Nothing here is imported at runtime.
"""

from __future__ import annotations

#: Inserted into a generated module after `_SCHEMAS` is defined and before the server is
#: constructed. Written as a plain string rather than a format template, so the braces below
#: are Python, not placeholders.
SURFACE_CLASS = '''
#: Every registered tool, in emission order: description, annotations, and the function that
#: runs it. Order is kept because `tools/list` should not reshuffle between calls; a client
#: caching the list, and a model's prompt cache, both depend on that.
_TOOLS: dict[str, tuple[str, dict[str, bool], Any]] = {}


def _tool(name: str, description: str, annotations: dict[str, bool]) -> Any:
    """Record a tool for `_Surface` to serve. Registration only; nothing is wrapped."""

    def register(function: Any) -> Any:
        _TOOLS[name] = (description, annotations, function)
        return function

    return register


class _Surface(FastMCP):
    """The SDK's server, with tools advertised from the plan rather than from signatures."""

    async def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name=name,
                description=description,
                inputSchema=_SCHEMAS[name],
                annotations=annotations or None,
            )
            for name, (description, annotations, _) in _TOOLS.items()
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        registered = _TOOLS.get(name)
        if registered is None:
            raise ValueError(f"Unknown tool: {name}")
        # The planned schema is enforced inside `_invoke`, where a failure comes back as a
        # structured `invalid_arguments` result an agent can correct from. Validating here as
        # well would turn the same mistake into a protocol error that says less.
        return await registered[2](arguments or {})
'''

#: The same surface for SDK 2.x, which serves the 2026-07-28 protocol and the earlier ones from
#: one process. Inserted at the same point as `SURFACE_CLASS`.
SURFACE_CLASS_V2 = '''
#: Every registered tool, in emission order: description, annotations, and the function that
#: runs it. Order is kept because `tools/list` should not reshuffle between calls; a client
#: caching the list, and a model's prompt cache, both depend on that.
_TOOLS: dict[str, tuple[str, dict[str, bool], Any]] = {}


def _tool(name: str, description: str, annotations: dict[str, bool]) -> Any:
    """Record a tool for `_Surface` to serve. Registration only; nothing is wrapped."""

    def register(function: Any) -> Any:
        _TOOLS[name] = (description, annotations, function)
        return function

    return register


class _Surface(MCPServer):
    """The SDK's server, with tools advertised from the plan rather than from signatures."""

    async def list_tools(self) -> list[Tool]:
        listed = []
        for name, (description, annotations, _) in _TOOLS.items():
            # The protocol's hints go in `annotations`. The namespaced ones, which no
            # specification defines, go in `_meta`: SDK 2.x drops annotation keys it does not
            # know, and `_meta` is where the protocol puts extensions.
            standard = {key: value for key, value in annotations.items() if "/" not in key}
            extended = {key: value for key, value in annotations.items() if "/" in key}
            listed.append(
                Tool(
                    name=name,
                    description=description,
                    input_schema=_SCHEMAS[name],
                    annotations=standard or None,
                    _meta=extended or None,
                )
            )
        return listed

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        registered = _TOOLS.get(name)
        if registered is None:
            # An unknown tool is a protocol error in 2026-07-28, not a tool result.
            raise MCPError(-32602, f"Unknown tool: {name}")
        # The planned schema is enforced inside `_invoke`, where a failure comes back as a
        # structured `invalid_arguments` result an agent can correct from.
        result = await registered[2](arguments or {})
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result))],
            structured_content=result,
        )
'''

#: Which SDK major a generated server is written against. 2 is the default: it serves the
#: 2026-07-28 protocol and the earlier ones from one process. 1 remains for deployments that
#: cannot move yet, for the length of the protocol's deprecation window.
DEFAULT_SDK = 2
SUPPORTED_SDKS = (1, 2)

#: What a generated server imports from the SDK, per major.
SDK_IMPORTS = {
    1: "from mcp.server.fastmcp import FastMCP\nfrom mcp.types import Tool",
    2: (
        "from mcp.server.mcpserver import MCPServer\n"
        "from mcp.shared.exceptions import MCPError\n"
        "from mcp.types import CallToolResult, TextContent, Tool"
    ),
}

SURFACE_CLASSES = {1: SURFACE_CLASS, 2: SURFACE_CLASS_V2}

#: What a generated server needs installed, per major. Each is bounded above because a new SDK
#: major renamed the module the previous one imported, and an unbounded requirement installed
#: a version every generated server failed to start on. Moving major is a recompile.
REQUIREMENTS = {
    1: ("mcp>=1.2,<2", "httpx>=0.27"),
    2: ("mcp>=2,<3", "httpx>=0.27"),
}


def check_sdk(sdk: int) -> int:
    """Refuse an SDK major this compiler cannot write for."""
    if sdk not in SUPPORTED_SDKS:
        raise ValueError(f"no emitter for MCP SDK {sdk}; supported: {SUPPORTED_SDKS}")
    return sdk


__all__ = [
    "DEFAULT_SDK",
    "REQUIREMENTS",
    "SDK_IMPORTS",
    "SUPPORTED_SDKS",
    "SURFACE_CLASS",
    "SURFACE_CLASSES",
    "SURFACE_CLASS_V2",
    "check_sdk",
]
