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

__all__ = ["SURFACE_CLASS"]
