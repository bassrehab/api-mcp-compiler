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

#: Confirmation nonces already spent in this process. A sealed confirmation is valid until it
#: expires, on any replica holding the key, so without a record one answer could be replayed.
#: This set covers one process; `_RECORD` covers every replica that shares it.
_SPENT: set[str] = set()

#: The shared record of spent confirmations, when `_RECORD_ENV` names one: a Redis client, so a
#: nonce is spent once across every replica. None means this process's `_SPENT` is the record.
_RECORD: Any = None

#: The protocol revision from which confirmation travels as a multi round-trip request.
_MRTR_SINCE = "2026-07-28"


class _Confirmation(BaseModel):
    """The one field a person fills in to confirm, for clients on the earlier protocol."""

    confirm: str


def _tool(name: str, description: str, annotations: dict[str, bool]) -> Any:
    """Record a tool for `_Surface` to serve. Registration only; nothing is wrapped."""

    def register(function: Any) -> Any:
        _TOOLS[name] = (description, annotations, function)
        return function

    return register


def _state_security() -> Any:
    """The key that seals confirmation state, or a refusal to start without one.

    A confirmation is carried to the client and back, sealed, so any replica can check it. That
    only works if every replica holds the same key, and the SDK's default is a random key per
    process. So a surface with a confirmation-gated tool will not start until someone chooses:
    a shared key, or `ephemeral` for a single process.
    """
    if not _CONFIRM:
        return None
    key = os.environ.get(_STATE_KEY_ENV, "")
    if not key:
        raise SystemExit(
            f"{_STATE_KEY_ENV} is not set. This surface has tools a person must confirm, and the "
            "confirmation is sealed with this key so any replica can check it. Set it to the same "
            "secret on every replica, or to 'ephemeral' when exactly one process serves it."
        )
    record = os.environ.get(_RECORD_ENV, "")
    if record:
        _open_record(record)
    if key == "ephemeral":
        return None
    unsafe = sorted(name for name, gate in _CONFIRM.items() if not gate["idempotent"])
    if unsafe and not record:
        raise SystemExit(
            f"{', '.join(unsafe)} cannot be confirmed safely across replicas without a shared "
            "record. A sealed confirmation can be replayed on any replica until it expires, and "
            "for an operation that is not idempotent only a record of spent confirmations every "
            f"replica checks prevents that. Set {_RECORD_ENV} to a Redis URL shared by every "
            f"replica, or set {_STATE_KEY_ENV}=ephemeral and run one process."
        )
    return RequestStateSecurity(keys=[key], ttl=float(max(g["ttl"] for g in _CONFIRM.values())))


def _open_record(url: str) -> None:
    """Connect the shared record, or refuse to start without it.

    Checked at startup, because a confirmation that cannot be recorded as spent can be replayed,
    and finding that out on the first destructive call is finding it out too late.
    """
    global _RECORD
    try:
        import redis
        import redis.asyncio
    except ImportError as error:
        raise SystemExit(
            f"{_RECORD_ENV} is set, so this server needs the Redis client: pip install 'redis>=5'."
        ) from error
    try:
        redis.Redis.from_url(url, socket_timeout=5).ping()
    except Exception as error:
        raise SystemExit(
            f"{_RECORD_ENV} names a record this server cannot reach ({type(error).__name__}). "
            "A confirmation that cannot be recorded as spent could be replayed, so the server "
            "does not start."
        ) from error
    _RECORD = redis.asyncio.Redis.from_url(url, socket_timeout=5)


async def _spend(nonce: str, ttl_seconds: float) -> str:
    """Spend a confirmation once: `ok`, `spent` if it was already used, or `unrecorded`.

    With a shared record this is Redis's atomic set-if-absent, so two replicas racing on the same
    answer cannot both win. The key outlives the sealed state, so it is still there for as long as
    the state could be presented.
    """
    if _RECORD is not None:
        try:
            won = await _RECORD.set(
                f"{_RECORD_PREFIX}{nonce}", "1", nx=True, px=int(ttl_seconds * 1000) + 60_000
            )
        except Exception:
            return "unrecorded"
        return "ok" if won else "spent"
    if nonce in _SPENT:
        return "spent"
    _SPENT.add(nonce)
    return "ok"


def _cache_hints() -> Any:
    """The list freshness the policy derived, for the lists that describe this surface.

    `resources/read` is left to the SDK default: it returns data, not the surface, and how long
    a client may keep data is not what this policy decides.
    """
    if not _LIST_CACHE:
        return None
    hint = CacheHint(ttl_ms=_LIST_CACHE["ttl_ms"], scope=_LIST_CACHE["scope"])
    return {"tools/list": hint, "resources/list": hint, "resources/templates/list": hint}


def _refused(code: str, detail: str) -> Any:
    payload = {"error": code, "detail": detail}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload,
        is_error=True,
    )


def _expected(name: str, gate: dict[str, Any], arguments: dict[str, Any]) -> str:
    """What the person types: the identifying argument if there is one, else the tool name."""
    value = arguments.get(gate["phrase_arg"]) if gate.get("phrase_arg") else None
    return str(value) if value not in (None, "") else name


def _answer(response: Any) -> tuple[Any, Any]:
    """An elicitation answer's action and content, whether it arrived as a model or a dict."""
    if isinstance(response, dict):
        return response.get("action"), response.get("content")
    return getattr(response, "action", None), getattr(response, "content", None)


async def _confirmed(
    name: str, gate: dict[str, Any], arguments: dict[str, Any], context: Any
) -> Any:
    """None when a person confirmed this exact call; otherwise what to return instead."""
    expected = _expected(name, gate, arguments)
    message = (
        f"{gate['effect']} Arguments: {json.dumps(arguments, sort_keys=True)}. "
        f"Type {expected} to confirm."
    )
    capabilities = getattr(context, "client_capabilities", None)
    if capabilities is None or capabilities.elicitation is None:
        # No fallback to asking the agent twice: that is what this gate replaced.
        return _refused(
            "confirmation_unavailable",
            "This client cannot show a person a confirmation, and this tool does not run "
            "without one.",
        )

    if (context.protocol_version or "") < _MRTR_SINCE:
        # The earlier protocol asks mid-call, with a request from the server to the client.
        answer = await context.elicit(message, _Confirmation)
        action = answer.action
        typed = answer.data.confirm if action == "accept" else None
    else:
        response = (context.input_responses or {}).get("confirm")
        state = context.request_state
        if response is None or state is None:
            # The SDK seals this state and binds it to the tool, a digest of these arguments,
            # the caller and an expiry, so a retry with other arguments fails before this runs.
            return InputRequiredResult(
                input_requests={
                    "confirm": ElicitRequest(
                        params=ElicitRequestFormParams(
                            message=message,
                            requested_schema={
                                "type": "object",
                                "properties": {
                                    "confirm": {
                                        "type": "string",
                                        "title": f"Type {expected} to confirm",
                                    }
                                },
                                "required": ["confirm"],
                            },
                        )
                    )
                },
                request_state=json.dumps({"nonce": secrets.token_hex(16), "issued": time.time()}),
            )
        claims = json.loads(state)
        if time.time() - float(claims["issued"]) > gate["ttl"]:
            return _refused(
                "confirmation_expired", "The confirmation expired; call again to be asked afresh."
            )
        action, content = _answer(response)
        typed = (content or {}).get("confirm") if action == "accept" else None
        if action == "accept":
            # Spent on any acceptance, typed right or wrong, so one sealed state cannot be used to
            # guess the identifier more than once.
            spent = await _spend(claims["nonce"], gate["ttl"])
            if spent == "spent":
                return _refused("confirmation_spent", "This confirmation was already used.")
            if spent == "unrecorded":
                return _refused(
                    "confirmation_unrecorded",
                    "The record of spent confirmations could not be reached, and a confirmation "
                    "that cannot be recorded is not honoured.",
                )

    if action != "accept":
        return _refused("not_confirmed", f"The person chose to {action}.")
    if typed != expected:
        return _refused("not_confirmed", f"What was typed did not match {expected!r}.")
    return None


class _Surface(MCPServer):
    """The SDK's server, with tools advertised from the plan rather than from signatures."""

    def __init__(self, name: str, **options: Any) -> None:
        super().__init__(
            name,
            request_state_security=_state_security(),
            cache_hints=_cache_hints(),
            **options,
        )

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
        arguments = arguments or {}
        gate = _CONFIRM.get(name)
        if gate is not None:
            # Validated first, so a person is never asked to confirm a call the schema would
            # refuse anyway. `_invoke` validates again; that is cheap and keeps it self-contained.
            errors = sorted(
                Draft202012Validator(_SCHEMAS[name]).iter_errors(arguments),
                key=lambda error: list(error.absolute_path),
            )
            if errors:
                return _refused(
                    "invalid_arguments",
                    "; ".join(
                        f"/{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
                        for error in errors
                    ),
                )
            # A person confirms here, before the tool runs, rather than the agent confirming by
            # calling twice.
            outcome = await _confirmed(name, gate, arguments, context)
            if outcome is not None:
                return outcome
        result = await registered[2](arguments)
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
        "import secrets\n"
        "\n"
        "from mcp.server.caching import CacheHint\n"
        "from mcp.server.mcpserver import MCPServer, RequestStateSecurity\n"
        "from mcp.shared.exceptions import MCPError\n"
        "from mcp.types import (\n"
        "    CallToolResult,\n"
        "    ElicitRequest,\n"
        "    ElicitRequestFormParams,\n"
        "    InputRequiredResult,\n"
        "    TextContent,\n"
        "    Tool,\n"
        ")\n"
        "from jsonschema import Draft202012Validator\n"
        "from pydantic import BaseModel"
    ),
}

SURFACE_CLASSES = {1: SURFACE_CLASS, 2: SURFACE_CLASS_V2}

#: What a generated server needs installed, per major. Each is bounded above because a new SDK
#: major renamed the module the previous one imported, and an unbounded requirement installed
#: a version every generated server failed to start on. Moving major is a recompile.
#: Everything a generated module imports is named, not only the SDK: jsonschema validates
#: arguments on both targets, and the 2.x confirmation gate declares its form with pydantic. Both
#: arrive with the SDK today, and relying on that is how httpx went missing when SDK 2.x stopped
#: installing it.
REQUIREMENTS = {
    1: ("mcp>=1.2,<2", "httpx>=0.27", "jsonschema>=4.20"),
    2: ("mcp>=2,<3", "httpx>=0.27", "jsonschema>=4.20", "pydantic>=2.11"),
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
