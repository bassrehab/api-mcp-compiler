"""Emitting a runnable MCP server for a SOAP service.

The REST emitter writes one HTTP request per route. A SOAP service has no routes: every
operation is a POST to the same endpoint, and what distinguishes them is the envelope. That is
a different transport, not a variation on the same one, which is why it lives here rather than
as a branch inside the other emitter.

What the generated server will and will not do is decided at emission, not at runtime. A
document/literal or rpc/literal body it can write. A Section 5 encoded body it cannot, because
those values are a reference graph and guessing at one produces a request the service rejects
while looking like it worked. Emission is refused rather than approximated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from api_mcp_compiler.models import (
    ApiSemanticIR,
    EmissionStatus,
    OperationIR,
    ParameterLocation,
    PolicyManifest,
    RiskClass,
    ToolDescriptor,
    ToolSurface,
)

GENERATED_REQUIREMENTS = ("mcp>=1.2", "httpx>=0.27")

#: SOAP 1.1, which is what WSDL 1.1 describes.
ENVELOPE_NAMESPACE = "http://schemas.xmlsoap.org/soap/envelope/"


class SoapEmissionError(ValueError):
    """Raised when a SOAP surface cannot be served as written."""


@dataclass(frozen=True)
class EmittedSoapServer:
    """A generated SOAP server and what it decided to leave out."""

    source: str
    registered: list[str]
    withheld: dict[str, str]
    endpoint: str


def _operation_for(ir: ApiSemanticIR, tool: ToolDescriptor) -> OperationIR:
    return next(
        item for item in ir.operations if item.operation_id == tool.source_operations[0]
    )


def _tool_function(ir: ApiSemanticIR, tool: ToolDescriptor, manifest: PolicyManifest | None) -> str:
    """Write one registered SOAP tool."""
    operation = _operation_for(ir, tool)
    soap = operation.soap
    if soap is None:
        raise SoapEmissionError(f"{tool.name!r} carries no SOAP binding record")
    policy = manifest.policy_for(tool.tool_id) if manifest else None
    confirm = policy is not None and policy.confirmation is not None
    confirmation_ttl = (
        policy.confirmation.token_ttl_seconds
        if policy is not None and policy.confirmation is not None
        else 0
    )
    # In a document body the element the part *references* is what appears, not the part's own
    # name: a part called `parameters` pointing at `tns:NumberToWords` puts `NumberToWords` in
    # the envelope. Using the part name produced a fault from every real service.
    parts = [
        (
            item.name,
            (item.description or item.name).rsplit(":", 1)[-1],
        )
        for item in operation.inputs
        if item.location is ParameterLocation.SOAP_BODY
    ]
    return f'''
@mcp.tool(name={tool.name!r}, description={tool.description!r})
async def {tool.name}(arguments: dict[str, Any]) -> dict[str, Any]:
    """{tool.description}"""
    return await _invoke(
        tool_name={tool.name!r},
        operation={operation.operation_id!r},
        soap_action={(soap.soap_action or "")!r},
        namespace={(soap.target_namespace or "")!r},
        style={(soap.style or "document")!r},
        parts={parts!r},
        arguments=arguments,
        schema=_SCHEMAS[{tool.name!r}],
        requires_confirmation={confirm!r},
        confirmation_ttl_seconds={confirmation_ttl!r},
        destructive={(tool.risk is RiskClass.DESTRUCTIVE)!r},
        max_output_bytes={(policy.output.max_bytes if policy else None)!r},
        redact_fields={sorted(policy.output.redact_fields) if policy else []!r},
    )
'''


_PREAMBLE = '''"""Generated MCP server for a SOAP service. Do not edit.

{banner}

Every tool below was approved by a reviewer and cleared the emission gate. Tools the gate
withheld are named in `withheld_tools` and are deliberately not registered.

This server speaks document/literal and rpc/literal. A Section 5 encoded body is refused at
emission rather than approximated, because its values are a reference graph and a guess at one
produces a request the service rejects while looking like it worked.
"""

from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ElementTree
from typing import Any
from xml.sax.saxutils import escape

import httpx
from mcp.server.fastmcp import FastMCP

ENDPOINT = os.environ.get({env_var!r}, {endpoint!r})
#: Credentials are read from the environment. Nothing generated here stores one.
AUTH_ENV_VAR = {auth_env!r}
ENVELOPE_NS = "http://schemas.xmlsoap.org/soap/envelope/"

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


def _element(name: str, value: Any, namespace: str = "") -> str:
    """Serialise one value as XML, escaping anything a caller supplied.

    A namespace is declared only on the element that carries it, which for a document body is
    the one the message part references; its children inherit it.
    """
    declaration = f' xmlns="{{namespace}}"' if namespace else ""
    if isinstance(value, dict):
        inner = "".join(_element(key, item) for key, item in value.items())
    elif isinstance(value, list):
        return "".join(_element(name, item, namespace) for item in value)
    elif isinstance(value, bool):
        inner = "true" if value else "false"
    elif value is None:
        return f"<{{name}}{{declaration}} xsi:nil=\\"true\\"/>"
    else:
        inner = escape(str(value))
    return f"<{{name}}{{declaration}}>{{inner}}</{{name}}>"


def _to_dict(node: ElementTree.Element) -> Any:
    """Turn a response element into plain data, dropping namespaces from names."""
    children = list(node)
    if not children:
        return (node.text or "").strip()
    result: dict[str, Any] = {{}}
    for child in children:
        key = child.tag.rsplit("}}", 1)[-1]
        value = _to_dict(child)
        if key in result:
            existing = result[key]
            result[key] = existing + [value] if isinstance(existing, list) else [existing, value]
        else:
            result[key] = value
    return result


@mcp.resource("surface://withheld")
def withheld_tools() -> dict[str, str]:
    """Tools this surface refused to register, and why."""
    return _WITHHELD


async def _invoke(
    *,
    tool_name: str,
    operation: str,
    soap_action: str,
    namespace: str,
    style: str,
    parts: list[str],
    arguments: dict[str, Any],
    schema: dict[str, Any],
    requires_confirmation: bool,
    confirmation_ttl_seconds: int,
    destructive: bool,
    max_output_bytes: int | None,
    redact_fields: list[str],
) -> dict[str, Any]:
    """Validate, confirm, post an envelope, and sanitise. In that order."""
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

    if style == "rpc":
        # An rpc body wraps the parts, under their own names, in an element named for the
        # operation. A document body carries the element each part references, namespaced.
        inner = "".join(
            _element(name, arguments[name]) for name, _ in parts if name in arguments
        )
        body = f'<tns:{{operation}} xmlns:tns="{{namespace}}">{{inner}}</tns:{{operation}}>'
    else:
        body = "".join(
            _element(element, arguments[name], namespace)
            for name, element in parts
            if name in arguments
        )
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{{ENVELOPE_NS}}" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<soap:Body>{{body}}</soap:Body></soap:Envelope>"
    )

    headers = {{
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": f'"{{soap_action}}"',
    }}
    credential = os.environ.get(AUTH_ENV_VAR)
    if credential:
        headers["Authorization"] = f"Bearer {{credential}}"

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(ENDPOINT, content=envelope.encode("utf-8"), headers=headers)

    try:
        root = ElementTree.fromstring(response.text)
    except ElementTree.ParseError as error:
        return {{"error": "malformed_response", "detail": str(error)}}

    fault = root.find(f".//{{{{{{ENVELOPE_NS}}}}}}Fault")
    if fault is None:
        fault = root.find(".//Fault")
    if fault is not None:
        # A SOAP fault arrives with HTTP 500 and a body that explains itself. Reporting it as
        # a fault rather than a transport error is what lets an agent react to it.
        return {{"error": "soap_fault", "detail": _to_dict(fault)}}

    body_element = root.find(f"{{{{{{ENVELOPE_NS}}}}}}Body")
    payload = _redact(_to_dict(body_element) if body_element is not None else None, redact_fields)

    encoded = json.dumps(payload)
    if max_output_bytes is not None and len(encoded.encode("utf-8")) > max_output_bytes:
        return {{
            "status": "truncated",
            "detail": (
                f"The response exceeded the {{max_output_bytes}} byte ceiling this tool's "
                "policy sets, so it was withheld rather than flooding the context."
            ),
            "status_code": response.status_code,
        }}
    return {{"status_code": response.status_code, "body": payload}}
'''


def emit_soap_server(
    ir: ApiSemanticIR,
    surface: ToolSurface,
    manifest: PolicyManifest | None = None,
) -> EmittedSoapServer:
    """Write a runnable MCP server for the approved part of a SOAP surface."""
    registered = [item for item in surface.tools if item.emission is EmissionStatus.EXECUTABLE]
    withheld = {
        item.name: ", ".join(blocker.value for blocker in item.blockers)
        for item in surface.tools
        if item.emission is not EmissionStatus.EXECUTABLE
    }
    if not registered:
        raise SoapEmissionError(
            f"{ir.service.service_id!r} has no executable tools, so a generated server would "
            "expose nothing. A SOAP operation needs its side effect classified in an overlay "
            "before it can be approved, because WSDL carries no signal to infer one from."
        )

    endpoints = set()
    for item in registered:
        soap = _operation_for(ir, item).soap
        if soap is not None and soap.endpoint:
            endpoints.add(soap.endpoint)
    if len(endpoints) != 1:
        raise SoapEmissionError(
            f"the approved operations name {len(endpoints)} endpoints; this emitter writes one "
            "server per endpoint, so split the surface or approve one service at a time."
        )
    endpoint = str(next(iter(endpoints)))

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
        endpoint=endpoint,
        env_var=f"{slug}_ENDPOINT",
        auth_env=f"{slug}_TOKEN",
        schemas=json.dumps({item.name: item.input_schema for item in registered}),
        withheld=json.dumps(withheld),
    )
    body = "".join(_tool_function(ir, item, manifest) for item in registered)
    footer = '\n\nif __name__ == "__main__":\n    mcp.run()\n'
    return EmittedSoapServer(
        source=header + body + footer,
        registered=[item.name for item in registered],
        withheld=withheld,
        endpoint=endpoint,
    )
