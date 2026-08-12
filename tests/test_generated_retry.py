"""Tests that a generated server retries exactly as far as the derived policy allows.

The manifest has always computed a retry policy and an idempotency requirement per tool, and
the generated server read neither: a `never` policy retried nothing because nothing retried
at all, which looks like compliance and is absence. This is the third value in a row that was
derived, written into the artifact, and acted on by nobody, after the confirmation time to
live and the credential placement.

As with those, the tests here run the emitted module and watch what it does to the wire.
"""

from __future__ import annotations

import asyncio
import sys
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

#: `put` is idempotent by RFC 9110 and `post` is not, which is what makes the two tools below
#: land on different retry policies without anything in the test saying so.
SERVICE = """openapi: 3.0.3
info: {title: Retry Service, version: 1.0.0}
servers: [{url: https://retry.example.invalid}]
# A write with no declared authentication cannot be shown to be governed, so the emission
# gate refuses it and there is nothing to test. That refusal is the subject of its own tests.
security: [{guard: []}]
paths:
  /widgets/{id}:
    put:
      operationId: replaceWidget
      summary: Replace a widget
      parameters: [{in: path, name: id, required: true, schema: {type: string}}]
      responses: {'200': {description: ok, content: {application/json: {schema: {type: object}}}}}
  /widgets:
    post:
      operationId: createWidget
      summary: Create a widget
      responses: {'201': {description: made, content: {application/json: {schema: {type: object}}}}}
components:
  securitySchemes:
    guard: {type: apiKey, in: header, name: X-Retry-Key}
"""


class _Reply:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}

    def json(self) -> dict[str, str]:
        return {"id": "w-1"}

    @property
    def text(self) -> str:
        return "{}"


class _Client:
    """Answers with a scripted sequence, recording every attempt."""

    def __init__(self, script: list[Any], seen: list[dict[str, Any]]) -> None:
        self.script = script
        self.seen = seen

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def request(
        self, method: str, path: str, params: Any = None, headers: Any = None, json: Any = None
    ) -> Any:
        self.seen.append({"method": method, "path": path, "headers": dict(headers or {})})
        answer = self.script.pop(0) if self.script else _Reply(200)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: list[Any]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[float]]:
    """Emit the server, stub the SDK and the clock, and return what it did."""
    spec = tmp_path / "service.yaml"
    spec.write_text(SERVICE, encoding="utf-8")
    ir = parse_openapi(spec)
    plan = plan_semantic(ir)
    overlay = approve(plan, overlay=None, risk=RiskClass.WRITE, group=None, names=[]).overlay
    approved = plan_semantic(ir, overlay)
    manifest = synthesize_policy(ir, approved)
    source = emit_server(ir, generate_surface(ir, approved, manifest), manifest).source

    seen: list[dict[str, Any]] = []
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
    httpx.AsyncClient = lambda **_: _Client(script, seen)  # type: ignore[attr-defined]
    for name, module in {
        "mcp": package, "mcp.server": server, "mcp.server.fastmcp": fastmcp, "httpx": httpx
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    namespace: dict[str, Any] = {"__name__": "generated_server"}
    exec(compile(source, "<generated server>", "exec"), namespace)

    # Waiting is the one part that must not actually happen in a test, but how long it would
    # have waited is worth asserting, so it is recorded rather than discarded.
    waits: list[float] = []

    async def _instant(delay: float) -> None:
        waits.append(delay)

    namespace["asyncio"] = types.SimpleNamespace(sleep=_instant)
    return namespace, seen, waits


def _replace(namespace: dict[str, Any]) -> Any:
    """The idempotent tool, whose derived policy is `safe`."""
    return asyncio.run(namespace["replace_widget"]({"id": "w-1"}))


def _create(namespace: dict[str, Any]) -> Any:
    """The non-idempotent tool, whose derived policy needs an idempotency key."""
    return asyncio.run(namespace["create_widget"]({}))


def test_a_retryable_status_is_sent_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace, seen, waits = _load(tmp_path, monkeypatch, [_Reply(503), _Reply(200)])

    _replace(namespace)

    assert len(seen) == 2
    assert waits == [0.5]


def test_a_client_error_is_not_sent_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeating a request the service rejected changes nothing except the load."""
    namespace, seen, _ = _load(tmp_path, monkeypatch, [_Reply(404), _Reply(200)])

    _replace(namespace)

    assert len(seen) == 1


def test_a_server_error_is_not_sent_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """500 may mean the effect happened and the answer was lost, which a retry duplicates."""
    namespace, seen, _ = _load(tmp_path, monkeypatch, [_Reply(500), _Reply(200)])

    _replace(namespace)

    assert len(seen) == 1


def test_attempts_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged upstream is reported, not hammered."""
    namespace, seen, waits = _load(
        tmp_path, monkeypatch, [_Reply(503), _Reply(503), _Reply(503), _Reply(200)]
    )

    _replace(namespace)

    assert len(seen) == 3
    assert waits == [0.5, 1.0]


def test_the_service_decides_how_long_to_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rate limit that names its window is more useful than any backoff curve."""
    namespace, _, waits = _load(
        tmp_path, monkeypatch, [_Reply(429, {"Retry-After": "7"}), _Reply(200)]
    )

    _replace(namespace)

    assert waits == [7.0]


def test_a_transport_failure_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was answered, so nothing was performed twice."""
    namespace, seen, _ = _load(
        tmp_path, monkeypatch, [ConnectionError("reset"), _Reply(200)]
    )

    _replace(namespace)

    assert len(seen) == 2


def test_a_non_idempotent_tool_carries_a_key_that_survives_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh key per attempt would make every retry a new operation."""
    namespace, seen, _ = _load(tmp_path, monkeypatch, [_Reply(503), _Reply(201)])

    _create(namespace)

    keys = [attempt["headers"].get("Idempotency-Key") for attempt in seen]
    assert len(seen) == 2
    assert keys[0] and keys[0] == keys[1]


def test_two_invocations_do_not_share_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key derived from the arguments would make two deliberate identical calls collide."""
    namespace, seen, _ = _load(tmp_path, monkeypatch, [_Reply(201), _Reply(201)])

    _create(namespace)
    _create(namespace)

    first, second = (attempt["headers"]["Idempotency-Key"] for attempt in seen)
    assert first != second


def test_an_idempotent_tool_sends_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The header is for the case that needs it, not decoration on every request."""
    namespace, seen, _ = _load(tmp_path, monkeypatch, [_Reply(200)])

    _replace(namespace)

    assert "Idempotency-Key" not in seen[0]["headers"]


def test_invalid_arguments_reach_no_request_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validating after calling has already had the effect it was checking for."""
    namespace, seen, _ = _load(tmp_path, monkeypatch, [_Reply(200)])

    outcome = asyncio.run(namespace["replace_widget"]({"id": 17}))

    assert outcome["error"] == "invalid_arguments"
    assert seen == []
