"""Tests that run the confirmation logic the emitter writes, rather than reading it.

Every other test of the generated server asserts against its source text. That is enough to
show a value was written into the file and useless for showing what the file does with it,
which is how a confirmation time to live sat in the policy manifest, was passed to every
destructive tool, and expired nothing at all.

So this module loads the emitted module and calls into it. The MCP SDK and the HTTP client
are stubbed, because the point is the governance decision taken before either is reached: a
confirmation that expires must not be honoured, and the upstream call must never happen.
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

SPEC = Path(__file__).resolve().parents[1] / "examples" / "openapi" / "inventory_service.yaml"

#: What the policy asks for on the destructive tool in this specification.
TTL_SECONDS = 300


class _Response:
    """Just enough of an HTTP response for the generated code to carry on."""

    status_code = 200

    def json(self) -> dict[str, str]:
        return {"ok": "called"}

    @property
    def text(self) -> str:
        return "ok"


class _Client:
    """Records that a call happened, which is the thing a lapsed token must prevent."""

    def __init__(self, calls: list[tuple[str, str]], **_: Any) -> None:
        self._calls = calls

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def request(self, method: str, path: str, **_: Any) -> _Response:
        self._calls.append((method, path))
        return _Response()


@pytest.fixture
def generated(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Emit a server for an approved destructive tool and load it with the SDK stubbed."""
    ir = parse_openapi(SPEC)
    plan = plan_semantic(ir)
    overlay = approve(plan, overlay=None, risk=RiskClass.DESTRUCTIVE, group=None, names=[]).overlay
    approved = plan_semantic(ir, overlay)
    manifest = synthesize_policy(ir, approved)
    source = emit_server(ir, generate_surface(ir, approved, manifest), manifest).source

    calls: list[tuple[str, str]] = []

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
    httpx.AsyncClient = lambda **kwargs: _Client(calls, **kwargs)  # type: ignore[attr-defined]

    for name, module in {
        "mcp": package, "mcp.server": server, "mcp.server.fastmcp": fastmcp, "httpx": httpx
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    namespace: dict[str, Any] = {"__name__": "generated_server"}
    exec(compile(source, "<generated server>", "exec"), namespace)
    return namespace, calls


def _destructive(namespace: dict[str, Any]) -> Any:
    """The one tool in this specification that needs a confirmation."""
    return namespace["permanently_remove_item_record_warehouse"]


async def _call(namespace: dict[str, Any]) -> Any:
    return await _destructive(namespace)({"warehouse_id": "wh-1"})


def test_the_first_call_is_refused_and_states_when_the_token_lapses(
    generated: tuple[dict[str, Any], list[tuple[str, str]]],
) -> None:
    """A destructive call must not happen on the strength of asking for it."""
    namespace, calls = generated
    first = asyncio.run(_call(namespace))

    assert first["status"] == "confirmation_required"
    assert first["expires_in_seconds"] == TTL_SECONDS
    assert calls == [], "the upstream service was called before anyone confirmed"


def test_confirming_within_the_window_proceeds(
    generated: tuple[dict[str, Any], list[tuple[str, str]]],
) -> None:
    namespace, calls = generated
    asyncio.run(_call(namespace))
    asyncio.run(_call(namespace))

    assert calls == [("DELETE", "/warehouses/wh-1/items")]


def test_a_token_is_spent_by_the_call_it_authorised(
    generated: tuple[dict[str, Any], list[tuple[str, str]]],
) -> None:
    """Otherwise one confirmation authorises an unbounded number of deletions."""
    namespace, calls = generated
    asyncio.run(_call(namespace))
    asyncio.run(_call(namespace))
    third = asyncio.run(_call(namespace))

    assert third["status"] == "confirmation_required"
    assert len(calls) == 1


def test_a_lapsed_token_is_refused_rather_than_honoured(
    generated: tuple[dict[str, Any], list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the time to live, and what nothing used to check."""
    namespace, calls = generated
    start = time.monotonic()
    asyncio.run(_call(namespace))

    monkeypatch.setattr(time, "monotonic", lambda: start + TTL_SECONDS + 1)
    second = asyncio.run(_call(namespace))

    assert second["status"] == "confirmation_required"
    assert "expired" in second["detail"]
    assert calls == [], "a confirmation that had expired was honoured"


def test_a_fresh_token_after_expiry_still_works(
    generated: tuple[dict[str, Any], list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expiry must not wedge the tool: the next confirmation has to be usable."""
    namespace, calls = generated
    start = time.monotonic()
    asyncio.run(_call(namespace))

    monkeypatch.setattr(time, "monotonic", lambda: start + TTL_SECONDS + 1)
    asyncio.run(_call(namespace))
    asyncio.run(_call(namespace))

    assert calls == [("DELETE", "/warehouses/wh-1/items")]


def test_expired_tokens_do_not_accumulate(
    generated: tuple[dict[str, Any], list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server nobody restarts would otherwise hold every token ever issued."""
    namespace, _ = generated
    start = time.monotonic()
    for index in range(5):
        asyncio.run(_destructive(namespace)({"warehouse_id": f"wh-{index}"}))
    assert len(namespace["_CONFIRMED"]) == 5

    monkeypatch.setattr(time, "monotonic", lambda: start + TTL_SECONDS + 1)
    asyncio.run(_destructive(namespace)({"warehouse_id": "wh-9"}))

    assert set(namespace["_CONFIRMED"]) == {
        namespace["_digest"]("permanently_remove_item_record_warehouse", {"warehouse_id": "wh-9"})
    }
