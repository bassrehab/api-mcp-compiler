"""Emitting a runnable MCP server.

Everything upstream of here produces descriptions: an intermediate representation, a plan, a
policy manifest, a tool surface. None of it can be pointed at an agent. This module closes
that gap by writing a Python module that serves the surface over MCP.

Two properties matter more than the code being short.

A refused tool is not registered. The emission gate decided which tools a reviewer approved
and which carry a blocker, and a generated server that quietly registered everything would
undo that decision at the last step. Refused tools are listed in the module's docstring and
returned by a resource, so the surface documents what it withheld instead of shrinking
silently.

Policy travels with the tool rather than beside it. Confirmation, output ceilings and
redaction are emitted into the server, so a deployment cannot pick up the tools and leave the
governance behind.

The generated module imports the MCP SDK and an HTTP client. This package does not, and
nothing here is imported by the compiler at runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from api_mcp_compiler.codegen.composite import composite_threading
from api_mcp_compiler.models import (
    ApiSemanticIR,
    ArgumentBinding,
    EmissionStatus,
    ParameterLocation,
    PolicyManifest,
    RiskClass,
    ToolDescriptor,
    ToolSurface,
)

#: The generated module targets these, and this package depends on neither.
GENERATED_REQUIREMENTS = ("mcp>=1.2", "httpx>=0.27")


class ServerEmissionError(ValueError):
    """Raised when a surface cannot be served as written."""


@dataclass(frozen=True)
class EmittedServer:
    """A generated server and what it decided to leave out."""

    source: str
    registered: list[str]
    withheld: dict[str, str]
    base_url: str


def _base_url(ir: ApiSemanticIR) -> str:
    """Resolve the upstream base URL the generated server will call."""
    for server in ir.service.servers:
        if server.url.startswith(("http://", "https://")):
            return server.url.rstrip("/")
    raise ServerEmissionError(
        f"{ir.service.service_id!r} declares no absolute server URL, so a generated server "
        "would have nowhere to send a request. Add one to the specification or supply it at "
        "deployment."
    )


def _binding_map(tool: ToolDescriptor) -> dict[str, tuple[str, str, str | None]]:
    """Map each argument to where it goes on the wire, and which step it belongs to."""
    return {
        item.argument: (item.location.value, item.wire_name, item.source_operation)
        for item in tool.argument_bindings
    }


def _method_and_route(ir: ApiSemanticIR, operation_id: str) -> tuple[str, str]:
    """Recover the HTTP method and route for one operation."""
    operation = next(item for item in ir.operations if item.operation_id == operation_id)
    if not operation.route:
        raise ServerEmissionError(
            f"{operation_id!r} carries no route, so it cannot be served over HTTP. SOAP "
            "surfaces need a different transport than this emitter writes."
        )
    return operation.source_pointer.rsplit("/", 1)[-1].upper(), operation.route


def _tool_function(
    ir: ApiSemanticIR,
    tool: ToolDescriptor,
    manifest: PolicyManifest | None,
) -> str:
    """Write one registered tool."""
    steps = [
        (*_method_and_route(ir, item), item) for item in tool.source_operations
    ]
    operations = {item.operation_id: item for item in ir.operations}
    threading = {
        name: (item.step_index, item.from_step, item.response_field)
        for name, item in composite_threading(
            [operations[identifier] for identifier in tool.source_operations]
        ).items()
    }
    policy = manifest.policy_for(tool.tool_id) if manifest else None
    confirm = policy is not None and policy.confirmation is not None
    confirmation_ttl = (
        policy.confirmation.token_ttl_seconds
        if policy is not None and policy.confirmation is not None
        else 0
    )
    max_bytes = policy.output.max_bytes if policy else None
    redact = sorted(policy.output.redact_fields) if policy else []
    destructive = tool.risk is RiskClass.DESTRUCTIVE

    return f'''
@mcp.tool(name={tool.name!r}, description={tool.description!r})
async def {tool.name}(arguments: dict[str, Any]) -> dict[str, Any]:
    """{tool.description}"""
    return await _invoke(
        tool_name={tool.name!r},
        steps={[(method, route, operation) for method, route, operation in steps]!r},
        threading={threading!r},
        arguments=arguments,
        schema=_SCHEMAS[{tool.name!r}],
        bindings={_binding_map(tool)!r},
        requires_confirmation={confirm!r},
        confirmation_ttl_seconds={confirmation_ttl!r},
        destructive={destructive!r},
        max_output_bytes={max_bytes!r},
        redact_fields={redact!r},
    )
'''


_PREAMBLE = '''"""Generated MCP server. Do not edit.

{banner}

Reproduce with:

    python -m api_mcp_compiler.cli serve {service_id}

Every tool below was approved by a reviewer and cleared the emission gate. Tools the gate
withheld are named in `withheld_tools` and are deliberately not registered: a server that
registered them would undo the decision at the last step.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get({env_var!r}, {base_url!r})
#: Credentials are read from the environment. Nothing generated here stores one.
AUTH_ENV_VAR = {auth_env!r}

mcp = FastMCP({service_id!r})

_SCHEMAS: dict[str, dict[str, Any]] = json.loads({schemas!r})
_WITHHELD: dict[str, str] = json.loads({withheld!r})
#: Confirmation tokens issued in this process, keyed by tool and argument digest, each
#: holding the monotonic deadline after which it is no longer accepted. A token is single
#: use: it is removed when it is spent, and an expired one is refused rather than renewed.
_CONFIRMED: dict[str, float] = {{}}


def _digest(tool_name: str, arguments: dict[str, Any]) -> str:
    """Bind a confirmation to the exact arguments it was issued for."""
    import hashlib

    payload = json.dumps([tool_name, arguments], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _threaded(payload: Any, field: str) -> Any:
    """Read a threaded value out of a response, whether it listed or returned one record."""
    if isinstance(payload, dict):
        if field in payload:
            return payload[field]
        items = payload.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0].get(field)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0].get(field)
    return None


def _redact(value: Any, fields: list[str]) -> Any:
    """Remove fields the policy marks sensitive, at any depth."""
    if isinstance(value, dict):
        return {{
            key: "[redacted]" if key in fields else _redact(item, fields)
            for key, item in value.items()
        }}
    if isinstance(value, list):
        return [_redact(item, fields) for item in value]
    return value


@mcp.resource("surface://withheld")
def withheld_tools() -> dict[str, str]:
    """Tools this surface refused to register, and why.

    Exposed so the surface documents what it withheld rather than appearing to be everything
    the service offers.
    """
    return _WITHHELD


async def _invoke(
    *,
    tool_name: str,
    steps: list[tuple[str, str, str]],
    threading: dict[str, tuple[int, int, str]],
    arguments: dict[str, Any],
    schema: dict[str, Any],
    bindings: dict[str, tuple[str, str, str | None]],
    requires_confirmation: bool,
    confirmation_ttl_seconds: int,
    destructive: bool,
    max_output_bytes: int | None,
    redact_fields: list[str],
) -> dict[str, Any]:
    """Validate, confirm, run every step in order, and sanitise. In that order."""
    from jsonschema import Draft202012Validator

    errors = sorted(
        Draft202012Validator(schema).iter_errors(arguments),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        return {{
            "error": "invalid_arguments",
            "detail": "; ".join(
                f"/{{'/'.join(str(part) for part in error.absolute_path)}}: {{error.message}}"
                for error in errors
            ),
        }}

    if requires_confirmation:
        token = _digest(tool_name, arguments)
        now = time.monotonic()
        deadline = _CONFIRMED.pop(token, None)
        lapsed = deadline is not None and deadline <= now
        if deadline is None or lapsed:
            # Anything already past its deadline is dropped here, so a long-running server
            # does not accumulate tokens nobody will ever spend.
            for stale in [key for key, limit in _CONFIRMED.items() if limit <= now]:
                del _CONFIRMED[stale]
            _CONFIRMED[token] = now + confirmation_ttl_seconds
            return {{
                "status": "confirmation_required",
                "tool": tool_name,
                "destructive": destructive,
                "confirmation_token": token,
                "expires_in_seconds": confirmation_ttl_seconds,
                "detail": (
                    (
                        "The previous confirmation for these arguments expired before it was "
                        "used, so a new one is required. "
                        if lapsed
                        else ""
                    )
                    + "Call again with identical arguments within "
                    f"{{confirmation_ttl_seconds}} seconds to proceed. The token is bound to "
                    "these arguments, so confirming this call cannot authorise another."
                ),
            }}

    carried = dict(arguments)
    responses: list[Any] = []
    status_code = 0

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        for position, (method, route, operation_id) in enumerate(steps):
            for argument, (step_index, from_step, field) in threading.items():
                if step_index != position:
                    continue
                source = responses[from_step] if from_step < len(responses) else None
                value = _threaded(source, field)
                if value is None:
                    return {{
                        "error": "composite_step_unresolved",
                        "detail": (
                            f"step {{position}} needs {{argument!r}}, and step {{from_step}} "
                            f"returned no {{field!r}} to take it from"
                        ),
                    }}
                carried[argument] = value

            path, query, headers, body = route, {{}}, {{}}, None
            for argument, value in carried.items():
                location, wire_name, owner = bindings.get(argument, ("query", argument, None))
                if owner not in (None, operation_id):
                    continue
                if location == "path":
                    path = path.replace("{{" + wire_name + "}}", str(value))
                elif location == "query":
                    query[wire_name] = value
                elif location == "header":
                    headers[wire_name] = str(value)
                else:
                    body = value

            credential = os.environ.get(AUTH_ENV_VAR)
            if credential:
                headers.setdefault("Authorization", f"Bearer {{credential}}")

            response = await client.request(
                method, path, params=query or None, headers=headers or None, json=body
            )
            status_code = response.status_code
            try:
                parsed = response.json()
            except ValueError:
                parsed = {{"text": response.text}}
            responses.append(parsed)
            if response.status_code >= 400:
                # A composite stops at its first failure. Continuing would perform later steps
                # against a resource the earlier one did not create.
                return {{
                    "error": "composite_step_failed",
                    "step": position,
                    "status_code": response.status_code,
                    "body": _redact(parsed, redact_fields),
                }}

    payload = _redact(responses[-1] if responses else None, redact_fields)
    encoded = json.dumps(payload)
    if max_output_bytes is not None and len(encoded.encode("utf-8")) > max_output_bytes:
        return {{
            "status": "truncated",
            "detail": (
                f"The response exceeded the {{max_output_bytes}} byte ceiling this tool's "
                "policy sets, so it was withheld rather than flooding the context."
            ),
            "status_code": status_code,
        }}
    return {{"status_code": status_code, "body": payload}}
'''


def emit_server(
    ir: ApiSemanticIR,
    surface: ToolSurface,
    manifest: PolicyManifest | None = None,
) -> EmittedServer:
    """Write a runnable MCP server for one approved surface."""
    base_url = _base_url(ir)
    registered = [item for item in surface.tools if item.emission is EmissionStatus.EXECUTABLE]
    withheld = {
        item.name: ", ".join(blocker.value for blocker in item.blockers)
        for item in surface.tools
        if item.emission is not EmissionStatus.EXECUTABLE
    }
    if not registered:
        raise ServerEmissionError(
            f"{ir.service.service_id!r} has no executable tools, so a generated server would "
            "expose nothing. Approve a tool first."
        )

    slug = ir.service.service_id.replace("-", "_").upper()
    banner = (
        f"Service: {ir.service.title}\n"
        f"Source digest: {ir.service.source_digest}\n"
        f"Planner: {surface.planner.value}\n"
        f"Registered: {len(registered)} tool(s); withheld: {len(withheld)}"
    )
    header = _PREAMBLE.format(
        banner=banner,
        service_id=ir.service.service_id,
        base_url=base_url,
        env_var=f"{slug}_BASE_URL",
        auth_env=f"{slug}_TOKEN",
        schemas=json.dumps({item.name: item.input_schema for item in registered}),
        withheld=json.dumps(withheld),
    )
    body = "".join(_tool_function(ir, item, manifest) for item in registered)
    footer = '\n\nif __name__ == "__main__":\n    mcp.run()\n'
    return EmittedServer(
        source=header + body + footer,
        registered=[item.name for item in registered],
        withheld=withheld,
        base_url=base_url,
    )


__all__ = [
    "GENERATED_REQUIREMENTS",
    "EmittedServer",
    "ServerEmissionError",
    "emit_server",
]


def _unused(binding: ArgumentBinding, location: ParameterLocation) -> None:  # pragma: no cover
    """Keep the imported contract types referenced for the type checker."""
    del binding, location
