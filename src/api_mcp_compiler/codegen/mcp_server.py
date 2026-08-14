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
from dataclasses import dataclass, field

from api_mcp_compiler.codegen.composite import composite_threading
from api_mcp_compiler.codegen.credentials import placements, tool_schemes, variables
from api_mcp_compiler.models import (
    ApiSemanticIR,
    ArgumentBinding,
    EmissionStatus,
    ParameterLocation,
    PolicyManifest,
    RetryPolicy,
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
    #: Environment variable to scheme identifier, for everything the server reads a
    #: credential from. Empty when the specification declares no authentication it can place.
    credentials: dict[str, str] = field(default_factory=dict)


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


#: A service description is written for a documentation page and can run to paragraphs. An
#: agent pays for every token of it on every request, so only the opening is carried.
_INSTRUCTION_LIMIT = 600


def _instructions(ir: ApiSemanticIR) -> str | None:
    """What to tell an agent about the service before it reads the tool list."""
    described = (ir.service.description or "").strip()
    if not described:
        return None
    collapsed = " ".join(described.split())
    if len(collapsed) <= _INSTRUCTION_LIMIT:
        return collapsed
    return collapsed[:_INSTRUCTION_LIMIT].rsplit(" ", 1)[0] + "..."


def _budgets(
    tools: list[ToolDescriptor], manifest: PolicyManifest | None
) -> dict[str, dict[str, int]]:
    """The call budget each tool was given, so the server can hold it to it."""
    if manifest is None:
        return {}
    budgets: dict[str, dict[str, int]] = {}
    for tool in tools:
        policy = manifest.policy_for(tool.tool_id)
        if policy is None:
            continue
        # A limit the policy leaves unset is omitted rather than written as zero. Zero is a
        # budget of none, and a server that refused every call would be enforcing a number
        # nobody chose.
        declared = {
            "calls_per_minute": policy.rate.calls_per_minute,
            "max_concurrent": policy.rate.max_concurrent,
            "daily_call_budget": policy.rate.daily_call_budget,
        }
        budgets[tool.name] = {
            name: value for name, value in declared.items() if value is not None
        }
    return budgets


def _path_parameters(tool: ToolDescriptor) -> list[str]:
    """The arguments a resource's address carries, named as the template names them.

    The template's placeholders are wire names taken from the route, while the tool's
    arguments carry the compiler's names for them. The generated function has to be callable
    by the template, so it takes the wire names and hands the argument names to `_invoke`.
    """
    return [
        binding.wire_name
        for binding in tool.argument_bindings
        if binding.location is ParameterLocation.PATH
    ]


def _annotations(tool: ToolDescriptor) -> dict[str, bool]:
    """The MCP annotations to register this tool with.

    Emitted because a client reads them to decide whether to auto-approve a call, and because
    the protocol's own position is that a client must treat them as untrusted since a server
    can assert whatever it likes. These are derived from the specification and carry provenance
    in the surface artifact, which is a different kind of claim than an assertion.

    `openWorldHint` is absent. It asks whether a tool reaches outside a closed domain, nothing
    in a specification answers that, and one invented value beside three derived ones is how a
    set of trustworthy hints stops being checked.

    The two extensions are namespaced. `sensitiveHint` and a reversibility hint were both
    proposed to the specification and neither was merged, so shipping them unprefixed would be
    claiming a standard that does not exist.
    """
    if tool.annotations is None:
        return {}
    described: dict[str, bool] = {
        "readOnlyHint": tool.annotations.read_only,
        "destructiveHint": tool.annotations.destructive,
        "idempotentHint": tool.annotations.idempotent,
    }
    if tool.annotations.sensitive is not None:
        described["x-rotaforge/sensitiveHint"] = tool.annotations.sensitive
    if tool.annotations.reversible is not None:
        described["x-rotaforge/reversibleHint"] = tool.annotations.reversible
    return described


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
    # Derived governance the server has to act on, not merely carry: a retry policy nothing
    # reads is the same defect as a confirmation nobody expires.
    retry = policy.retry.value if policy else RetryPolicy.NEVER.value
    needs_key = policy.idempotency_key_required if policy else False
    confirmation_ttl = (
        policy.confirmation.token_ttl_seconds
        if policy is not None and policy.confirmation is not None
        else 0
    )
    max_bytes = policy.output.max_bytes if policy else None
    redact = sorted(policy.output.redact_fields) if policy else []
    destructive = tool.risk is RiskClass.DESTRUCTIVE
    annotations = _annotations(tool)

    if tool.uri_template is not None:
        # A resource is read by address, so it is registered as one. Emitting it with
        # `@mcp.tool` would discard the planner's reclassification at the last step and spend
        # a tool slot on a lookup, which is the thing reclassifying it was for.
        parameters = ", ".join(f"{name}: str" for name in _path_parameters(tool))
        forwarded = ", ".join(f"{name!r}: {name}" for name in _path_parameters(tool))
        return f'''
@mcp.resource({tool.uri_template!r}, name={tool.name!r}, description={tool.description!r})
async def {tool.name}({parameters}) -> dict[str, Any]:
    """{tool.description}"""
    return await _invoke(
        tool_name={tool.name!r},
        steps={[(method, route, operation) for method, route, operation in steps]!r},
        threading={threading!r},
        arguments={{{forwarded}}},
        schema=_SCHEMAS[{tool.name!r}],
        bindings={_binding_map(tool)!r},
        requires_confirmation=False,
        confirmation_ttl_seconds=0,
        retry={retry!r},
        idempotency_key_required=False,
        destructive=False,
        max_output_bytes={max_bytes!r},
        redact_fields={redact!r},
    )
'''

    return f'''
@mcp.tool(name={tool.name!r}, description={tool.description!r}, annotations={annotations!r})
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
        retry={retry!r},
        idempotency_key_required={needs_key!r},
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

import asyncio
import json
import os
import time
import uuid
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get({env_var!r}, {base_url!r})
#: Credentials are read from the environment. Nothing generated here stores one.
#: How each security scheme's credential is sent, and the variable it is read from.
_AUTH: dict[str, dict[str, str]] = json.loads({auth!r})
#: Which schemes each tool needs, as least-privilege selection chose them.
_TOOL_SCHEMES: dict[str, list[str]] = json.loads({tool_schemes!r})

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


#: Attempts in total, not retries in addition. Three is a compromise: enough to ride out a
#: single restart or rate-limit window, few enough that a wedged upstream is reported rather
#: than hammered.
_MAX_ATTEMPTS = 3

#: Status codes worth sending again. Deliberately excludes 500: a server error may mean the
#: effect happened and the response was lost, and repeating that is exactly what the retry
#: policy exists to prevent. 429 and the gateway codes are unambiguous about not having acted.
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


async def _send(
    client: Any,
    method: str,
    path: str,
    *,
    params: Any,
    headers: dict[str, str],
    json_body: Any,
    retry: str,
    idempotency_key: str | None,
) -> Any:
    """Make one request, repeating it only as far as the derived policy allows.

    `never` means one attempt. It is the policy for an operation whose idempotency could not
    be determined, where a repeat could duplicate an effect nobody asked for twice.

    An idempotency key is generated per invocation and held across that invocation's retries,
    which is the whole point: a fresh key per attempt would make every retry a new operation,
    and a key derived from the arguments would make two deliberate identical calls collide.
    """
    attempts = 1 if retry == "never" else _MAX_ATTEMPTS
    for attempt in range(attempts):
        sending = dict(headers)
        if idempotency_key is not None:
            sending["Idempotency-Key"] = idempotency_key
        last_error: Exception | None = None
        try:
            response = await client.request(
                method, path, params=params, headers=sending or None, json=json_body
            )
        except Exception as error:  # transport failure: nothing was answered, so it is safe
            last_error = error
            if attempt + 1 >= attempts:
                raise
            await asyncio.sleep(_backoff(attempt, None))
            continue
        if response.status_code in _RETRYABLE_STATUS and attempt + 1 < attempts:
            await asyncio.sleep(_backoff(attempt, response))
            continue
        return response
    raise last_error if last_error is not None else RuntimeError("no attempt was made")


def _credentials(tool_name: str) -> tuple[dict[str, str], dict[str, str]]:
    """Read the credentials this tool needs and place each where the service declared it.

    Returns headers and query parameters to add. A scheme whose variable is unset is left
    out rather than sent empty, so an unauthenticated call fails at the service with a clear
    401 instead of here with something that looks like a bug in the tool.
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
    retry: str,
    idempotency_key_required: bool,
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

    refusal = _reserve(tool_name)
    if refusal is not None:
        return refusal

    carried = dict(arguments)
    responses: list[Any] = []
    status_code = 0
    idempotency_key = str(uuid.uuid4()) if idempotency_key_required else None

    try:
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

                authorization, authorized_query = _credentials(tool_name)
                headers.update(authorization)
                query.update(authorized_query)

                response = await _send(
                    client,
                    method,
                    path,
                    params=query or None,
                    headers=headers,
                    json_body=body,
                    retry=retry,
                    idempotency_key=idempotency_key,
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
    finally:
        _release(tool_name)
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
        instructions=_instructions(ir),
        base_url=base_url,
        env_var=f"{slug}_BASE_URL",
        auth=json.dumps(placements(ir, slug)),
        tool_schemes=json.dumps(tool_schemes(registered, manifest)),
        budgets=json.dumps(_budgets(registered, manifest)),
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
        credentials=variables(ir, slug, tool_schemes(registered, manifest)),
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
