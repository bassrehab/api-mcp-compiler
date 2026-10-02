"""The 2.x confirmation gate, driven over MCP against the real SDK.

On the 1.x target a destructive call is confirmed by calling it again with the same arguments,
which an agent can do on its own. The 2.x target asks a person instead: a client on the
2026-07-28 protocol gets an `input_required` result carrying an elicitation, and answers it on a
retry that echoes sealed request state; a client on an earlier protocol is asked mid-call.

The generated server's upstream is pointed at a local HTTP server that counts requests, so every
test can assert how many deletes actually reached the service, not only what the gate returned.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from api_mcp_compiler.codegen.mcp_server import emit_server
from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.models import RiskClass
from api_mcp_compiler.planning.approval import approve
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy
from tests.conftest import INVENTORY_SERVICE

SDK = int(importlib.metadata.version("mcp").split(".")[0])
pytestmark = pytest.mark.skipif(SDK < 2, reason="the confirmation gate is on the 2.x target")

TOOL = "permanently_remove_item_record_warehouse"
KEY = "SYNTHETIC_INVENTORY_SERVICE_REQUEST_STATE_KEY"
RECORD = "SYNTHETIC_INVENTORY_SERVICE_CONFIRMATION_RECORD"
ARGS = {"warehouse_id": "wh-7"}
#: The SDK refuses a key shorter than 32 bytes, so the shared key in these tests is a real one.
SHARED = "5f1c0a9e7b3d4c2a8e6f1b0d9c7a5e3f2b4d6a8c0e1f3a5b7d9c2e4f6a8b0c1d"
ACCEPT = {"confirm": {"action": "accept", "content": {"confirm": "wh-7"}}}
MODERN = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}},
}


class _Upstream(BaseHTTPRequestHandler):
    """Answers every request with an empty object, and records it."""

    seen: ClassVar[list[tuple[str, str]]] = []

    def _answer(self) -> None:
        self.seen.append((self.command, self.path))
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_DELETE = do_POST = do_PUT = do_PATCH = _answer

    def log_message(self, *_: Any) -> None:
        return None


@pytest.fixture
def upstream() -> Iterator[tuple[str, list[tuple[str, str]]]]:
    seen: list[tuple[str, str]] = []
    handler = type("Handler", (_Upstream,), {"seen": seen})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()


def _source(*, idempotent: bool = True) -> str:
    """The inventory service with its destructive tool approved, emitted for SDK 2.x."""
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    overlay = approve(
        plan_semantic(ir), overlay=None, risk=RiskClass.DESTRUCTIVE, group=None, names=[]
    ).overlay
    approved = plan_semantic(ir, overlay)
    manifest = synthesize_policy(ir, approved)
    source = emit_server(ir, generate_surface(ir, approved, manifest), manifest, sdk=2).source
    if not idempotent:
        # The example's delete is idempotent. The refusal to start is about operations that
        # are not, so the table is edited rather than inventing a second specification.
        source = source.replace('\\"idempotent\\": true', '\\"idempotent\\": false')
        source = source.replace('"idempotent": true', '"idempotent": false')
    return source


def _env(base_url: str, key: str | None, record: str | None = None) -> dict[str, str]:
    env = {
        **os.environ,
        "SYNTHETIC_INVENTORY_SERVICE_INVENTORYOAUTH_CREDENTIAL": "unused",
        "SYNTHETIC_INVENTORY_SERVICE_BASE_URL": base_url,
    }
    env.pop(KEY, None)
    env.pop(RECORD, None)
    if key is not None:
        env[KEY] = key
    if record is not None:
        env[RECORD] = record
    return env


class _Server:
    """One generated server process spoken to in raw 2026-07-28 JSON-RPC."""

    def __init__(self, module: Path, env: dict[str, str]) -> None:
        self.process = subprocess.Popen(
            [sys.executable, str(module)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )
        self.next_id = 0

    def call(self, arguments: dict[str, Any], **extra: Any) -> dict[str, Any]:
        self.next_id += 1
        params = {"name": TOOL, "arguments": arguments, "_meta": MODERN, **extra}
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": "tools/call", "params": params}
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        return dict(json.loads(self.process.stdout.readline()))

    def close(self) -> None:
        self.process.kill()
        self.process.wait()


def _server(
    tmp_path: Path,
    base_url: str,
    key: str | None = "ephemeral",
    *,
    idempotent: bool = True,
    record: str | None = None,
) -> _Server:
    module = tmp_path / "gated_server.py"
    module.write_text(_source(idempotent=idempotent))
    return _Server(module, _env(base_url, key, record))


def _error(response: dict[str, Any]) -> str | None:
    return (response["result"].get("structuredContent") or {}).get("error")


def _deletes(seen: list[tuple[str, str]]) -> int:
    return sum(1 for method, _ in seen if method == "DELETE")


# Starting


def _start(
    tmp_path: Path, key: str | None, *, idempotent: bool = True, record: str | None = None
) -> tuple[int, str]:
    module = tmp_path / "gated_server.py"
    module.write_text(_source(idempotent=idempotent))
    result = subprocess.run(
        [sys.executable, str(module)],
        input="",
        capture_output=True,
        text=True,
        timeout=30,
        env=_env("http://127.0.0.1:9", key, record),
    )
    return result.returncode, result.stderr


def test_a_gated_surface_will_not_start_without_a_key(tmp_path: Path) -> None:
    code, stderr = _start(tmp_path, None)
    assert code != 0
    assert KEY in stderr


def test_a_shared_key_is_refused_for_an_operation_that_is_not_idempotent(tmp_path: Path) -> None:
    code, stderr = _start(tmp_path, SHARED, idempotent=False)
    assert code != 0
    assert TOOL in stderr
    assert RECORD in stderr and "ephemeral" in stderr


@pytest.mark.parametrize("key", ["ephemeral", SHARED])
def test_it_starts_once_the_key_is_chosen(tmp_path: Path, key: str) -> None:
    code, stderr = _start(tmp_path, key)
    assert code == 0, stderr


# The 2026-07-28 protocol


def test_the_first_call_asks_a_person_and_touches_nothing(
    tmp_path: Path, upstream: tuple[str, list[tuple[str, str]]]
) -> None:
    base_url, seen = upstream
    server = _server(tmp_path, base_url)
    try:
        first = server.call(ARGS)
        retried = server.call(ARGS, requestState=first["result"]["requestState"])
    finally:
        server.close()

    request = first["result"]["inputRequests"]["confirm"]
    assert first["result"]["resultType"] == "input_required"
    assert request["method"] == "elicitation/create"
    assert "Type wh-7 to confirm" in request["params"]["message"]
    # Retrying without an answer is asked again. Persistence is not confirmation.
    assert retried["result"]["resultType"] == "input_required"
    assert _deletes(seen) == 0


def test_a_person_typing_the_identifier_lets_it_run_once(
    tmp_path: Path, upstream: tuple[str, list[tuple[str, str]]]
) -> None:
    base_url, seen = upstream
    server = _server(tmp_path, base_url)
    try:
        state = server.call(ARGS)["result"]["requestState"]
        confirmed = server.call(ARGS, inputResponses=ACCEPT, requestState=state)
        replayed = server.call(ARGS, inputResponses=ACCEPT, requestState=state)
    finally:
        server.close()

    assert confirmed["result"]["resultType"] == "complete"
    assert _error(confirmed) is None
    assert _error(replayed) == "confirmation_spent"
    assert _deletes(seen) == 1


@pytest.mark.parametrize(
    "answer",
    [
        {"confirm": {"action": "accept", "content": {"confirm": "wh-8"}}},
        {"confirm": {"action": "decline"}},
        {"confirm": {"action": "cancel"}},
    ],
    ids=["wrong value", "decline", "cancel"],
)
def test_anything_but_the_typed_identifier_is_refused(
    tmp_path: Path, upstream: tuple[str, list[tuple[str, str]]], answer: dict[str, Any]
) -> None:
    base_url, seen = upstream
    server = _server(tmp_path, base_url)
    try:
        state = server.call(ARGS)["result"]["requestState"]
        response = server.call(ARGS, inputResponses=answer, requestState=state)
    finally:
        server.close()

    assert _error(response) == "not_confirmed"
    assert _deletes(seen) == 0


def test_a_confirmation_cannot_be_moved_to_other_arguments(
    tmp_path: Path, upstream: tuple[str, list[tuple[str, str]]]
) -> None:
    base_url, seen = upstream
    server = _server(tmp_path, base_url)
    other = {"confirm": {"action": "accept", "content": {"confirm": "wh-8"}}}
    try:
        state = server.call(ARGS)["result"]["requestState"]
        moved = server.call({"warehouse_id": "wh-8"}, inputResponses=other, requestState=state)
        tampered = server.call(ARGS, inputResponses=ACCEPT, requestState=state[:-4] + "AAAA")
    finally:
        server.close()

    assert moved["error"]["code"] == -32602
    assert tampered["error"]["code"] == -32602
    assert _deletes(seen) == 0


def test_invalid_arguments_are_refused_before_anyone_is_asked(
    tmp_path: Path, upstream: tuple[str, list[tuple[str, str]]]
) -> None:
    base_url, seen = upstream
    server = _server(tmp_path, base_url)
    try:
        response = server.call({"arguments": ARGS})
    finally:
        server.close()

    assert response["result"]["resultType"] == "complete"
    assert _error(response) == "invalid_arguments"
    assert _deletes(seen) == 0


def test_a_shared_key_lets_another_replica_accept_the_answer(
    tmp_path: Path, upstream: tuple[str, list[tuple[str, str]]]
) -> None:
    base_url, seen = upstream
    first = _server(tmp_path, base_url, key=SHARED)
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _server(second_dir, base_url, key=SHARED)
    try:
        state = first.call(ARGS)["result"]["requestState"]
        confirmed = second.call(ARGS, inputResponses=ACCEPT, requestState=state)
    finally:
        first.close()
        second.close()

    assert _error(confirmed) is None
    assert _deletes(seen) == 1


# The earlier protocol


def _legacy(tmp_path: Path, base_url: str, callback: Any) -> Any:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    module = tmp_path / "gated_server.py"
    module.write_text(_source())
    parameters = StdioServerParameters(
        command=sys.executable, args=[str(module)], env=_env(base_url, "ephemeral")
    )

    async def run() -> Any:
        async with stdio_client(parameters) as (read, write), ClientSession(
            read, write, elicitation_callback=callback
        ) as session:
            await session.initialize()
            return await session.call_tool(TOOL, ARGS)

    return asyncio.run(run())


def _structured(result: Any) -> dict[str, Any]:
    value = getattr(result, "structured_content", None)
    if value is None:
        value = getattr(result, "structuredContent", None)
    return dict(value or {})


def test_an_earlier_client_is_asked_mid_call(
    tmp_path: Path, upstream: tuple[str, list[tuple[str, str]]]
) -> None:
    from mcp import types

    base_url, seen = upstream
    asked: list[str] = []

    async def person(_context: Any, params: Any) -> Any:
        asked.append(params.message)
        return types.ElicitResult(action="accept", content={"confirm": "wh-7"})

    result = _legacy(tmp_path, base_url, person)

    assert asked and "Type wh-7 to confirm" in asked[0]
    assert "error" not in _structured(result)
    assert _deletes(seen) == 1


def test_a_client_that_cannot_ask_a_person_is_refused(
    tmp_path: Path, upstream: tuple[str, list[tuple[str, str]]]
) -> None:
    base_url, seen = upstream
    result = _legacy(tmp_path, base_url, None)

    assert _structured(result)["error"] == "confirmation_unavailable"
    assert _deletes(seen) == 0


# The shared record of spent confirmations


@pytest.fixture
def record() -> Iterator[str]:
    """A Redis URL every replica in a test shares.

    CI provides a real Redis and names it in REDIS_URL. Elsewhere, fakeredis serves the Redis
    protocol over TCP, so separate server processes still share one record.
    """
    if os.environ.get("REDIS_URL"):
        import redis

        redis.Redis.from_url(os.environ["REDIS_URL"]).flushdb()
        yield os.environ["REDIS_URL"]
        return
    from fakeredis import TcpFakeServer

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    server = TcpFakeServer(("127.0.0.1", port), server_type="redis")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        server.shutdown()


def test_a_shared_record_lets_a_non_idempotent_tool_start_on_a_shared_key(
    tmp_path: Path, record: str
) -> None:
    code, stderr = _start(tmp_path, SHARED, idempotent=False, record=record)
    assert code == 0, stderr


def test_a_record_that_cannot_be_reached_stops_the_server_starting(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        unused = int(probe.getsockname()[1])
    code, stderr = _start(tmp_path, SHARED, record=f"redis://127.0.0.1:{unused}/0")
    assert code != 0
    assert RECORD in stderr


@pytest.mark.parametrize("idempotent", [False, True], ids=["not idempotent", "idempotent"])
def test_a_confirmation_is_spent_once_across_replicas(
    tmp_path: Path,
    upstream: tuple[str, list[tuple[str, str]]],
    record: str,
    idempotent: bool,
) -> None:
    """The case the record exists for: one answer, replayed on another replica, runs nothing."""
    base_url, seen = upstream
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    first = _server(tmp_path, base_url, key=SHARED, idempotent=idempotent, record=record)
    second = _server(second_dir, base_url, key=SHARED, idempotent=idempotent, record=record)
    try:
        state = first.call(ARGS)["result"]["requestState"]
        confirmed = second.call(ARGS, inputResponses=ACCEPT, requestState=state)
        replayed = first.call(ARGS, inputResponses=ACCEPT, requestState=state)
        replayed_again = second.call(ARGS, inputResponses=ACCEPT, requestState=state)
    finally:
        first.close()
        second.close()

    assert _error(confirmed) is None
    assert _error(replayed) == "confirmation_spent"
    assert _error(replayed_again) == "confirmation_spent"
    assert _deletes(seen) == 1
