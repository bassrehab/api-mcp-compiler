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
from dataclasses import dataclass, field

from api_mcp_compiler.codegen.credentials import placements, tool_schemes, variables
from api_mcp_compiler.codegen.mcp_server import _budgets, _instructions
from api_mcp_compiler.models import (
    ApiSemanticIR,
    EmissionStatus,
    OperationIR,
    ParameterLocation,
    PolicyManifest,
    RetryPolicy,
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
    #: Environment variable to scheme identifier, for everything the server reads a
    #: credential from.
    credentials: dict[str, str] = field(default_factory=dict)


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
    retry = policy.retry.value if policy else RetryPolicy.NEVER.value
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
        retry={retry!r},
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

import asyncio
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
#: How each security scheme's credential is sent, and the variable it is read from.
_AUTH: dict[str, dict[str, str]] = json.loads({auth!r})
#: Which schemes each tool needs, as least-privilege selection chose them.
_TOOL_SCHEMES: dict[str, list[str]] = json.loads({tool_schemes!r})
ENVELOPE_NS = "http://schemas.xmlsoap.org/soap/envelope/"

mcp = FastMCP({service_id!r}, instructions={instructions!r})

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


#: Attempts in total, not retries in addition.
_MAX_ATTEMPTS = 3

#: Transport-level codes worth sending again. A SOAP fault arrives as 500 and is an answer,
#: not a failure to answer, so 500 is never retried here.
_RETRYABLE_STATUS = frozenset({{429, 502, 503, 504}})


#: Calls per minute, concurrent calls and calls per day, per tool, as policy derived them.
_BUDGETS: dict[str, dict[str, int]] = json.loads({budgets!r})

#: When each recent call happened, when the current day's window opened and how many it has
#: spent, and how many calls are in flight. In this process only: a deployment that runs
#: several workers needs a shared counter, and this one cannot pretend to be that.
_RECENT: dict[str, list[float]] = {{}}
_DAILY: dict[str, tuple[float, int]] = {{}}
_ACTIVE: dict[str, int] = {{}}

_MINUTE = 60.0
_DAY = 86_400.0


def _reserve(tool_name: str) -> dict[str, Any] | None:
    """Take one call out of the tool's budget, or explain why it cannot be taken.

    Refusing rather than queueing is deliberate. A queued call looks to an agent like a slow
    service, and it will wait, retry, or give up on a goal it could have achieved. A refusal
    that names the limit and when it lifts is something an agent can reason about.

    A call that never reaches the service does not spend budget, so this runs after argument
    validation and after the confirmation gate rather than before them.
    """
    budget = _BUDGETS.get(tool_name)
    if budget is None:
        return None
    now = time.monotonic()

    recent = [stamp for stamp in _RECENT.get(tool_name, []) if now - stamp < _MINUTE]
    per_minute = budget.get("calls_per_minute", 0)
    if per_minute and len(recent) >= per_minute:
        return {{
            "error": "rate_limited",
            "limit": "calls_per_minute",
            "allowed": per_minute,
            "retry_after_seconds": round(_MINUTE - (now - recent[0]), 3),
            "detail": (
                f"{{tool_name}} is limited to {{per_minute}} calls per minute by its derived "
                "policy, and that many have been made in the last minute."
            ),
        }}

    opened, spent = _DAILY.get(tool_name, (now, 0))
    if now - opened >= _DAY:
        opened, spent = now, 0
    daily = budget.get("daily_call_budget", 0)
    if daily and spent >= daily:
        return {{
            "error": "rate_limited",
            "limit": "daily_call_budget",
            "allowed": daily,
            "retry_after_seconds": round(_DAY - (now - opened), 3),
            "detail": (
                f"{{tool_name}} is limited to {{daily}} calls per day by its derived policy, "
                "and the budget for this window is spent."
            ),
        }}

    concurrent = budget.get("max_concurrent", 0)
    active = _ACTIVE.get(tool_name, 0)
    if concurrent and active >= concurrent:
        return {{
            "error": "rate_limited",
            "limit": "max_concurrent",
            "allowed": concurrent,
            "retry_after_seconds": None,
            "detail": (
                f"{{tool_name}} allows {{concurrent}} concurrent call(s) by its derived "
                f"policy, and {{active}} are in flight."
            ),
        }}

    recent.append(now)
    _RECENT[tool_name] = recent
    _DAILY[tool_name] = (opened, spent + 1)
    _ACTIVE[tool_name] = active + 1
    return None


def _release(tool_name: str) -> None:
    """Give back the concurrency slot. The minute and day counts are spent, not borrowed."""
    if tool_name in _ACTIVE:
        _ACTIVE[tool_name] = max(0, _ACTIVE[tool_name] - 1)


def _backoff(attempt: int, response: Any) -> float:
    """How long to wait, preferring what the service asked for over what we guessed."""
    if response is not None:
        requested = getattr(response, "headers", {{}}) or {{}}
        raw = requested.get("Retry-After") or requested.get("retry-after")
        if raw:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
    return float(2**attempt) / 2


async def _post(client: Any, envelope: str, headers: dict[str, str], query: Any, retry: str) -> Any:
    """Post the envelope, repeating it only as far as the derived policy allows.

    No idempotency key is sent. WSDL declares nothing equivalent, so a key here would be a
    header invented by this compiler that no service was built to honour, and a retry of a
    non-idempotent SOAP operation is exactly what `never` is for.
    """
    for attempt in range(1 if retry == "never" else _MAX_ATTEMPTS):
        try:
            response = await client.post(
                ENDPOINT, content=envelope.encode("utf-8"), headers=headers, params=query or None
            )
        except Exception:  # nothing was answered, so nothing was performed twice
            if attempt + 1 >= (1 if retry == "never" else _MAX_ATTEMPTS):
                raise
            await asyncio.sleep(_backoff(attempt, None))
            continue
        if response.status_code in _RETRYABLE_STATUS and attempt + 1 < _MAX_ATTEMPTS:
            await asyncio.sleep(_backoff(attempt, response))
            continue
        return response
    raise RuntimeError("no attempt was made")


def _credentials(tool_name: str) -> tuple[dict[str, str], dict[str, str]]:
    """Read the credentials this tool needs and place each where the service declared it.

    A scheme whose variable is unset is left out rather than sent empty, so an
    unauthenticated call fails at the service rather than looking like a fault in the tool.
    """
    import base64

    headers: dict[str, str] = {{}}
    query: dict[str, str] = {{}}
    for scheme_id in _TOOL_SCHEMES.get(tool_name, []):
        described = _AUTH.get(scheme_id)
        if described is None:
            continue
        value = os.environ.get(described["env"])
        if not value:
            continue
        kind = described["kind"]
        if kind == "basic":
            encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
            headers[described["name"]] = f"Basic {{encoded}}"
        elif kind == "header":
            headers[described["name"]] = described["prefix"] + value
        elif kind == "cookie":
            existing = headers.get("Cookie")
            pair = f"{{described['name']}}={{value}}"
            headers["Cookie"] = f"{{existing}}; {{pair}}" if existing else pair
        else:
            query[described["name"]] = value
    return headers, query


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
    retry: str,
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

    refusal = _reserve(tool_name)
    if refusal is not None:
        return refusal

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
    authorization, query = _credentials(tool_name)
    headers.update(authorization)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await _post(client, envelope, headers, query, retry)
    finally:
        _release(tool_name)

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
        instructions=_instructions(ir),
        endpoint=endpoint,
        env_var=f"{slug}_ENDPOINT",
        auth=json.dumps(placements(ir, slug)),
        tool_schemes=json.dumps(tool_schemes(registered, manifest)),
        budgets=json.dumps(_budgets(registered, manifest)),
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
        credentials=variables(ir, slug, tool_schemes(registered, manifest)),
    )
