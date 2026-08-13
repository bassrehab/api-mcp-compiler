"""Tests that a generated server holds a tool to the call budget policy gave it.

The manifest has always scaled calls per minute, concurrency and a daily budget by risk, and
the generated server counted nothing, so a destructive tool with a budget of two calls a
minute would happily make two hundred. As the issue put it, they were declarations rather
than controls.

Fourth in a row of the same shape, after the confirmation time to live, the credential
placement and the retry policy: derived, written into the artifact, and read by nobody. So
these tests exhaust real budgets against the emitted module rather than reading the file.
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

from api_mcp_compiler.codegen.mcp_server import emit_server
from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.models import RiskClass
from api_mcp_compiler.planning.approval import approve
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy

#: A destructive operation, because that is where the budget is tightest: policy allows two
#: calls a minute and one at a time.
SERVICE = """openapi: 3.0.3
info: {title: Budget Service, version: 1.0.0}
servers: [{url: https://budget.example.invalid}]
security: [{guard: []}]
paths:
  /widgets/{id}:
    delete:
      operationId: purgeWidget
      summary: Permanently delete a widget
      parameters: [{in: path, name: id, required: true, schema: {type: string}}]
      responses: {'204': {description: gone}}
components:
  securitySchemes:
    guard: {type: apiKey, in: header, name: X-Key}
"""


class _Client:
    """Answers immediately, or waits on a gate when the test wants calls to overlap."""

    def __init__(self, seen: list[str], gate: asyncio.Event | None) -> None:
        self.seen = seen
        self.gate = gate

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def request(self, method: str, path: str, **_: Any) -> Any:
        self.seen.append(path)
        if self.gate is not None:
            await self.gate.wait()
        return types.SimpleNamespace(
            status_code=204, headers={}, json=lambda: {}, text=""
        )


def _load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: asyncio.Event | None = None
) -> tuple[dict[str, Any], list[str]]:
    spec = tmp_path / "service.yaml"
    spec.write_text(SERVICE, encoding="utf-8")
    ir = parse_openapi(spec)
    plan = plan_semantic(ir)
    overlay = approve(plan, overlay=None, risk=RiskClass.DESTRUCTIVE, group=None, names=[]).overlay
    approved = plan_semantic(ir, overlay)
    manifest = synthesize_policy(ir, approved)
    source = emit_server(ir, generate_surface(ir, approved, manifest), manifest).source

    seen: list[str] = []
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = lambda *_, **__: types.SimpleNamespace(  # type: ignore[attr-defined]
        tool=lambda **_kw: (lambda function: function),
        resource=lambda *_a, **_kw: (lambda function: function),
    )
    server = types.ModuleType("mcp.server")
    server.fastmcp = fastmcp  # type: ignore[attr-defined]
    package = types.ModuleType("mcp")
    package.server = server  # type: ignore[attr-defined]
    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = lambda **_: _Client(seen, gate)  # type: ignore[attr-defined]
    for name, module in {
        "mcp": package, "mcp.server": server, "mcp.server.fastmcp": fastmcp, "httpx": httpx
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    namespace: dict[str, Any] = {"__name__": "generated_server"}
    exec(compile(source, "<generated server>", "exec"), namespace)
    return namespace, seen


def _confirmed_call(namespace: dict[str, Any], identifier: str = "w-1") -> Any:
    """Confirm, then call. A destructive tool refuses the first attempt by design."""
    tool = namespace["permanently_delete_widget"]
    asyncio.run(tool({"id": identifier}))
    return asyncio.run(tool({"id": identifier}))


def test_the_budget_reaches_the_generated_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace, _ = _load(tmp_path, monkeypatch)

    assert namespace["_BUDGETS"]["permanently_delete_widget"] == {
        "calls_per_minute": 2,
        "max_concurrent": 1,
        "daily_call_budget": 20,
    }


def test_calls_beyond_the_minute_budget_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two a minute is what policy derived for a destructive tool, so the third is refused."""
    namespace, seen = _load(tmp_path, monkeypatch)

    _confirmed_call(namespace, "w-1")
    _confirmed_call(namespace, "w-2")
    third = _confirmed_call(namespace, "w-3")

    assert len(seen) == 2
    assert third["error"] == "rate_limited"
    assert third["limit"] == "calls_per_minute"
    assert third["allowed"] == 2


def test_a_refusal_says_when_the_budget_lifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent can reason about a wait. It cannot reason about a bare failure."""
    namespace, _ = _load(tmp_path, monkeypatch)

    _confirmed_call(namespace, "w-1")
    _confirmed_call(namespace, "w-2")
    third = _confirmed_call(namespace, "w-3")

    assert 0 < third["retry_after_seconds"] <= 60


def test_the_budget_recovers_once_the_window_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A limit that never lifts is an outage, not a budget."""
    namespace, seen = _load(tmp_path, monkeypatch)
    start = time.monotonic()

    _confirmed_call(namespace, "w-1")
    _confirmed_call(namespace, "w-2")

    monkeypatch.setattr(time, "monotonic", lambda: start + 61)
    _confirmed_call(namespace, "w-3")

    assert len(seen) == 3


def test_a_call_that_never_reaches_the_service_spends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid arguments and an unconfirmed destructive call cost no budget."""
    namespace, seen = _load(tmp_path, monkeypatch)
    tool = namespace["permanently_delete_widget"]

    asyncio.run(tool({"id": 17}))  # invalid: id is a string
    asyncio.run(tool({"id": "w-1"}))  # unconfirmed, so refused before any call

    _confirmed_call(namespace, "w-2")
    _confirmed_call(namespace, "w-3")

    assert len(seen) == 2, "budget was spent by calls that never reached the service"


def test_concurrency_is_held_to_the_derived_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One at a time for a destructive tool, and the second is told why."""
    gate = asyncio.Event()
    namespace, seen = _load(tmp_path, monkeypatch, gate=gate)
    tool = namespace["permanently_delete_widget"]

    async def exercise() -> Any:
        # Confirm both first, so the confirmation gate is not what refuses the second.
        await tool({"id": "w-1"})
        await tool({"id": "w-2"})
        first = asyncio.create_task(tool({"id": "w-1"}))
        await asyncio.sleep(0)
        second = await tool({"id": "w-2"})
        gate.set()
        await first
        return second

    second = asyncio.run(exercise())

    assert second["error"] == "rate_limited"
    assert second["limit"] == "max_concurrent"
    assert len(seen) == 1


def test_the_slot_is_given_back_when_a_call_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise one call exhausts the tool for the life of the process."""
    namespace, seen = _load(tmp_path, monkeypatch)

    _confirmed_call(namespace, "w-1")
    _confirmed_call(namespace, "w-2")

    assert namespace["_ACTIVE"]["permanently_delete_widget"] == 0
    assert len(seen) == 2
