"""Evaluate the server a user would deploy, rather than the surface that was planned.

The in-process harness hands the model each tool's planned schema and executes a call by
simulating what a generated server would do. An audit against MCP 2026-07-28 found that the
generated server had advertised something else entirely, so every comparison run that way had
measured a surface no deployed server presented. This module closes that gap.

`ServedSurface` emits the server for a surface, starts it as a subprocess, and talks to it over
MCP. Its upstream is an in-process HTTP stand-in that applies each request to the same
`ServiceStore` the in-process harness uses, through the same `derive_effect` and `apply`, so the
oracles judge the final state exactly as before. The model is shown what `tools/list` returned
and receives what `tools/call` returned; nothing about the surface comes from the plan.

A planner may reclassify an addressable read as an MCP resource, which the server serves through
`resources/read`, not `tools/list`. An agent reaches a resource however its host exposes them; this
harness bridges each resource template to a tool the model can call, taking its name and
description from the server's own template listing, and executes the call through the server's
real `resources/read`. That is how a capable agent host commonly exposes resources, and it is the
same for both arms. The in-process harness had simply offered such a read as an ordinary tool.

Two traces are kept. The oracles read the response body, as the store produced it, because their
field paths are written against it. The driver reads the full payload the server returned,
status code and all, because that is what an agent sees.

The SDK, uvicorn and starlette are imported lazily: they are dependencies of the generated
server and of this evaluation, never of the compiler.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from api_mcp_compiler.codegen.mcp_server import emit_server
from api_mcp_compiler.codegen.registration import DEFAULT_SDK
from api_mcp_compiler.contracts import canonical_json
from api_mcp_compiler.evaluation.harness import (
    Driver,
    _mutating_operations,
    bind_operations,
    selection,
)
from api_mcp_compiler.evaluation.oracles import evaluate_oracle
from api_mcp_compiler.evaluation.state import EffectKind, ServiceStore, derive_effect
from api_mcp_compiler.models import (
    ApiSemanticIR,
    EmissionStatus,
    EvalTask,
    OperationIR,
    OracleResult,
    ParameterLocation,
    StepOutcome,
    TaskResult,
    ToolSurface,
    TraceStep,
)

#: How long the generated server and the stand-in may take to come up, and a call to answer.
STARTUP_SECONDS = 30.0
CALL_SECONDS = 60.0


def _field(item: Any, snake: str, camel: str) -> Any:
    """Read a field under either SDK's naming, by which attribute exists rather than its value.

    SDK 2.x names fields in snake_case and 1.x in camelCase. Falling through on a falsy value
    reads the wrong name when the right one holds an empty list.
    """
    return getattr(item, snake) if hasattr(item, snake) else getattr(item, camel)


def _typed(value: str, schema: dict[str, Any] | None) -> Any:
    """Parse a path, query or header value as its declared type, as a real service would.

    Everything on a URL arrives as text. The in-process harness passes arguments as the model
    wrote them, so without this a volume of 60 sent as `?volume_percent=60` would be stored as
    the string "60" and compared unequal to the number the oracle expects.
    """
    kind = (schema or {}).get("type")
    if kind == "array":
        # httpx sends a list as a repeated parameter, which arrives here one value at a time; a
        # model may also pass one comma-joined string, OpenAPI's `explode: false` form.
        item = (schema or {}).get("items")
        return [_typed(part, item) for part in value.split(",")] if value else []
    try:
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
    except ValueError:
        return value
    if kind == "boolean" and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _route_pattern(route: str) -> re.Pattern[str]:
    """`/albums/{id}/tracks` matches `/albums/abc/tracks`, capturing `id`."""
    pattern = re.sub(r"\{([^}/]+)\}", lambda match: f"(?P<{_group(match.group(1))}>[^/]+)", route)
    return re.compile(f"^{pattern}/?$")


def _group(name: str) -> str:
    """A regex group name for a path parameter, which may contain characters a group cannot."""
    return "p_" + re.sub(r"[^A-Za-z0-9_]", "_", name)


@dataclass
class _Route:
    operation: OperationIR
    method: str
    pattern: re.Pattern[str]
    path_names: dict[str, str]


class _Upstream:
    """The HTTP service a generated server calls, applying each request to a `ServiceStore`.

    The store is swapped per task by the harness. A request is mapped back to its operation by
    method and route, its arguments are rebuilt under the names the store expects (path and query
    parameters by name, the JSON body as `body`), and it is applied with the operation id as the
    seed, exactly as the in-process harness applies it.
    """

    def __init__(self, ir: ApiSemanticIR) -> None:
        self.store = ServiceStore.from_fixture({})
        self.routes: list[_Route] = []
        for operation in ir.operations:
            if not operation.route:
                continue
            method = operation.source_pointer.rsplit("/", 1)[-1].upper()
            names = {
                _group(item.name): item.name
                for item in operation.inputs
                if item.location is ParameterLocation.PATH
            }
            self.routes.append(_Route(operation, method, _route_pattern(operation.route), names))
        # More specific routes first, so `/me/tracks/contains` is not taken by `/me/tracks/{id}`.
        self.routes.sort(
            key=lambda item: (item.pattern.pattern.count("(?P<"), -len(item.pattern.pattern))
        )

    def match(self, method: str, path: str) -> tuple[_Route, dict[str, str]] | None:
        for route in self.routes:
            if route.method != method:
                continue
            found = route.pattern.match(path)
            if found:
                return route, {
                    route.path_names[key]: unquote(value)
                    for key, value in found.groupdict().items()
                    if key in route.path_names
                }
        return None

    def app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def handle(request: Request) -> JSONResponse:
            matched = self.match(request.method, request.url.path)
            if matched is None:
                return JSONResponse({"error": "no such route"}, status_code=404)
            route, arguments = matched
            for item in route.operation.inputs:
                if item.location is ParameterLocation.QUERY and item.name in request.query_params:
                    raw_values = request.query_params.getlist(item.name)
                    if (item.type_schema or {}).get("type") == "array":
                        arguments[item.name] = [
                            part
                            for value in raw_values
                            for part in _typed(value, item.type_schema)
                        ]
                    else:
                        arguments[item.name] = _typed(raw_values[-1], item.type_schema)
                if item.location is ParameterLocation.HEADER and item.name in request.headers:
                    arguments[item.name] = _typed(request.headers[item.name], item.type_schema)
                if item.location is ParameterLocation.PATH and item.name in arguments:
                    arguments[item.name] = _typed(arguments[item.name], item.type_schema)
            raw = await request.body()
            if raw:
                try:
                    arguments["body"] = json.loads(raw)
                except ValueError:
                    arguments["body"] = raw.decode("utf-8", "replace")
            record = self.store.apply(
                derive_effect(route.operation), arguments, seed=route.operation.operation_id
            )
            # `null` rather than an empty 204, so the server parses it as "no content" the way the
            # in-process harness records None.
            return JSONResponse(record)

        methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]
        return Starlette(routes=[Route("/{path:path}", handle, methods=methods)])


@dataclass
class ServedCall:
    """What one `tools/call` returned."""

    payload: Any
    is_error: bool
    error: str | None = None


@dataclass
class ServedSurface:
    """A generated server for one surface, running, and a client connected to it."""

    ir: ApiSemanticIR
    surface: ToolSurface
    sdk: int = DEFAULT_SDK
    python: str = sys.executable
    _upstream: _Upstream | None = field(default=None, repr=False)
    _http: Any = field(default=None, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _queue: Any = field(default=None, repr=False)
    _ready: Future[Any] | None = field(default=None, repr=False)
    _tools: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict, repr=False)
    _templates: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)
    _directory: Any = field(default=None, repr=False)

    def __enter__(self) -> ServedSurface:
        import uvicorn

        self._upstream = _Upstream(self.ir)
        port = _free_port()
        config = uvicorn.Config(
            self._upstream.app(), host="127.0.0.1", port=port, log_level="error"
        )
        self._http = uvicorn.Server(config)
        threading.Thread(target=self._http.run, daemon=True).start()
        deadline = time.monotonic() + STARTUP_SECONDS
        while not self._http.started:
            if time.monotonic() > deadline:
                raise RuntimeError("the upstream stand-in did not start")
            time.sleep(0.05)

        emitted = emit_server(self.ir, self.surface, None, sdk=self.sdk)
        self._directory = tempfile.TemporaryDirectory(prefix="served-surface-")
        module = Path(self._directory.name) / "served_server.py"
        module.write_text(emitted.source, encoding="utf-8")
        slug = self.ir.service.service_id.replace("-", "_").upper()
        environment = {
            **os.environ,
            f"{slug}_BASE_URL": f"http://127.0.0.1:{port}",
            # One process serves each run, which is what `ephemeral` is for. The comparison runs
            # without a policy manifest, so no tool is gated and the key is never used.
            f"{slug}_REQUEST_STATE_KEY": "ephemeral",
        }
        for variable in emitted.credentials:
            environment.setdefault(variable, "evaluation")

        self._loop = asyncio.new_event_loop()
        self._ready = Future()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._session(str(module), environment), self._loop)
        self._tools, self._templates = self._ready.result(timeout=STARTUP_SECONDS)
        return self

    async def _session(self, module: str, environment: dict[str, str]) -> None:
        """Open the client, list the tools, then serve calls until told to stop.

        One task owns the session from opening to closing, because the SDK's transports are
        structured: a context entered in one task cannot be exited from another.
        """
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        assert self._ready is not None
        self._queue = asyncio.Queue()
        parameters = StdioServerParameters(command=self.python, args=[module], env=environment)
        try:
            async with (
                stdio_client(parameters) as (read, write),
                ClientSession(read, write) as client,
            ):
                await client.initialize()
                listed = (await client.list_tools()).tools
                tools = {
                    tool.name: (
                        tool.description or "",
                        dict(_field(tool, "input_schema", "inputSchema")),
                    )
                    for tool in listed
                }
                listed_templates = await client.list_resource_templates()
                templates = {
                    item.name: (
                        _field(item, "uri_template", "uriTemplate"),
                        item.description or "",
                    )
                    for item in _field(listed_templates, "resource_templates", "resourceTemplates")
                }
                # A read with no parameters is a fixed resource, listed by `resources/list` rather
                # than as a template. Its URI has no variables, so it bridges the same way.
                for item in (await client.list_resources()).resources:
                    templates.setdefault(item.name, (str(item.uri), item.description or ""))
                self._ready.set_result((tools, templates))
                while True:
                    request = await self._queue.get()
                    if request is None:
                        return
                    name, arguments, answer = request
                    if name in templates and name not in tools:
                        answer.set_result(await self._read(client, templates[name][0], arguments))
                        continue
                    try:
                        result = await client.call_tool(name, arguments)
                    except Exception as error:  # a protocol error, such as an unknown tool
                        answer.set_result(ServedCall(None, True, str(error)))
                        continue
                    structured = _field(result, "structured_content", "structuredContent")
                    if structured is None and result.content:
                        try:
                            structured = json.loads(result.content[0].text)
                        except (ValueError, AttributeError):
                            structured = {"text": getattr(result.content[0], "text", "")}
                    is_error = bool(_field(result, "is_error", "isError"))
                    answer.set_result(ServedCall(structured, is_error))
        except Exception as error:
            if not self._ready.done():
                self._ready.set_exception(error)
            raise

    @staticmethod
    async def _read(client: Any, template: str, arguments: dict[str, Any]) -> ServedCall:
        """Read a resource by expanding its template with the model's arguments."""
        missing = [name for name in re.findall(r"\{([^}]+)\}", template) if name not in arguments]
        if missing:
            return ServedCall(None, True, f"missing {', '.join(missing)} for {template}")
        uri = re.sub(r"\{([^}]+)\}", lambda match: str(arguments[match.group(1)]), template)
        try:
            result = await client.read_resource(uri)
        except Exception as error:
            return ServedCall(None, True, str(error))
        text = getattr(result.contents[0], "text", "") if result.contents else ""
        try:
            return ServedCall(json.loads(text), False)
        except ValueError:
            return ServedCall({"text": text}, False)

    def templates(self) -> dict[str, tuple[str, str]]:
        """Each advertised resource template's URI template and description, by name."""
        return dict(self._templates)

    def tools(self) -> dict[str, tuple[str, dict[str, Any]]]:
        """Each advertised tool's description and input schema, as `tools/list` returned them."""
        return dict(self._tools)

    def use(self, store: ServiceStore) -> None:
        """Point the upstream at one task's store."""
        assert self._upstream is not None
        self._upstream.store = store

    def call(self, name: str, arguments: dict[str, Any]) -> ServedCall:
        assert self._loop is not None and self._queue is not None
        answer: Future[ServedCall] = Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (name, arguments, answer))
        return answer.result(timeout=CALL_SECONDS)

    def __exit__(self, *_: object) -> None:
        if self._loop is not None and self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            time.sleep(0.2)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._http is not None:
            self._http.should_exit = True
        if self._directory is not None:
            self._directory.cleanup()


def served_view(surface: ToolSurface, served: ServedSurface) -> ToolSurface:
    """The surface as the server advertised it: its descriptions and schemas, not the plan's.

    A tool the server does not list is dropped, so the model is offered exactly what an agent
    connected to the deployed server would be offered.
    """
    advertised = served.tools()
    templates = served.templates()
    tools = []
    for tool in surface.tools:
        if tool.emission is not EmissionStatus.EXECUTABLE:
            continue
        if tool.name in advertised:
            description, schema = advertised[tool.name]
        elif tool.name in templates:
            # A resource, bridged to a tool: its parameters are the template's variables.
            uri, description = templates[tool.name]
            variables = re.findall(r"\{([^}]+)\}", uri)
            schema = {
                "type": "object",
                "properties": {name: {"type": "string"} for name in variables},
                "required": variables,
                "additionalProperties": False,
            }
            description = f"{description} (reads the resource {uri})".strip()
        else:
            continue
        tools.append(tool.model_copy(update={"description": description, "input_schema": schema}))
    return surface.model_copy(update={"tools": tools})


def run_task_served(
    task: EvalTask,
    ir: ApiSemanticIR,
    surface: ToolSurface,
    served: ServedSurface,
    driver: Driver,
) -> TaskResult:
    """Run one task through the deployed server and judge it by its oracles."""
    store = ServiceStore.from_fixture(task.fixture)
    served.use(store)
    initial_state = store.snapshot()
    view = served_view(surface, served)
    binding = bind_operations(view)
    by_name = {item.name: item for item in view.tools}
    operations = {item.operation_id: item for item in ir.operations}
    prohibited = set(task.prohibited_operations)

    trace: list[TraceStep] = []
    seen: list[TraceStep] = []
    unmapped: list[str] = []
    invalid = unsafe = context_bytes = 0

    for index in range(task.max_calls):
        choice = driver.next_call(task, view, list(seen))
        if choice is None:
            break
        operation_id, arguments = choice
        tool = by_name.get(operation_id) or binding.get(operation_id)
        if tool is None:
            unmapped.append(operation_id)
            step = TraceStep(
                index=index,
                operation_id=operation_id,
                arguments=arguments,
                outcome=StepOutcome.UNMAPPED,
                detail="the server lists no tool for this",
            )
            trace.append(step)
            seen.append(step)
            continue

        outcome = served.call(tool.name, arguments)
        payload = outcome.payload
        error = payload.get("error") if isinstance(payload, dict) else None
        if outcome.error is not None or error == "invalid_arguments":
            invalid += 1 if error == "invalid_arguments" else 0
            detail = outcome.error or str(payload.get("detail", error))
            step = TraceStep(
                index=index,
                operation_id=operation_id,
                tool=tool.name,
                arguments=arguments,
                outcome=StepOutcome.REFUSED_ARGUMENTS,
                detail=detail,
            )
            trace.append(step)
            seen.append(step)
            continue

        body = (
            payload.get("body")
            if isinstance(payload, dict) and "status_code" in payload
            else payload
        )
        size = len(canonical_json(payload).encode("utf-8")) if payload is not None else 0
        context_bytes += size
        for source in tool.source_operations:
            operation = operations.get(source)
            if (
                operation is not None
                and source in prohibited
                and derive_effect(operation).kind is not EffectKind.READ
            ):
                unsafe += 1
        common = {
            "index": index,
            "operation_id": operation_id,
            "tool": tool.name,
            "arguments": arguments,
            "outcome": StepOutcome.OK,
            "response_bytes": size,
        }
        trace.append(TraceStep(**common, response=body))
        seen.append(TraceStep(**common, response=payload))

    final_state = store.snapshot()
    mutating = _mutating_operations(ir)
    results: list[OracleResult] = [
        evaluate_oracle(
            oracle, initial_state, final_state, trace, task.prohibited_operations, mutating
        )
        for oracle in task.oracles
    ]
    selected, rate = selection(task, trace)
    expected = len(task.reference_solution)
    successful = sum(1 for item in trace if item.outcome is StepOutcome.OK)
    return TaskResult(
        task_id=task.task_id,
        success=all(item.passed for item in results),
        oracle_results=results,
        calls=len(trace),
        unnecessary_calls=max(0, successful - expected) if expected else 0,
        unmapped_operations=sorted(set(unmapped)),
        selected_operations=selected,
        selection_rate=rate,
        invalid_argument_calls=invalid,
        unsafe_actions=unsafe,
        confirmation_failures=0,
        context_bytes=context_bytes,
        latency_ms=None,
        token_cost=None,
        trace=trace,
    )


__all__ = ["ServedCall", "ServedSurface", "run_task_served", "served_view"]
