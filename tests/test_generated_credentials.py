"""Tests that a generated server sends the credential the specification declared.

Before this, every scheme was sent as `Authorization: Bearer`. An API key in a header the
service named, or HTTP basic, produced a 401 on every call from a compiler that had parsed
the correct placement and thrown it away at the last step.

These tests run the emitted module and inspect the request it actually made, for the same
reason the confirmation tests do: reading the source proves a value was written into the
file, not that anything reads it.
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
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy

SERVICE = """openapi: 3.0.3
info: {{title: Guarded Service, version: 1.0.0}}
servers: [{{url: https://guarded.example.invalid}}]
security: [{{guard: {scopes}}}]
paths:
  /widgets:
    get:
      operationId: listWidgets
      summary: List every widget
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema: {{type: array, items: {{type: object}}}}
components:
  securitySchemes:
    guard: {scheme}
"""


class _Recorder:
    """Captures the one request the generated tool makes."""

    def __init__(self, seen: list[dict[str, Any]]) -> None:
        self.seen = seen

    async def __aenter__(self) -> _Recorder:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def request(
        self, method: str, path: str, params: Any = None, headers: Any = None, json: Any = None
    ) -> Any:
        self.seen.append({"headers": headers or {}, "params": params or {}})
        return types.SimpleNamespace(
            status_code=200, json=lambda: [{"id": "w-1"}], text="[]"
        )


def _load(scheme: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scopes: str = "[]") -> Any:
    """Emit a server for one security scheme and load it with the SDK stubbed."""
    spec = tmp_path / "service.yaml"
    spec.write_text(SERVICE.format(scheme=scheme, scopes=scopes), encoding="utf-8")
    ir = parse_openapi(spec)
    plan = plan_semantic(ir)
    manifest = synthesize_policy(ir, plan)
    emitted = emit_server(ir, generate_surface(ir, plan, manifest), manifest)

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
    httpx.AsyncClient = lambda **_: _Recorder(seen)  # type: ignore[attr-defined]
    for name, module in {
        "mcp": package, "mcp.server": server, "mcp.server.fastmcp": fastmcp, "httpx": httpx
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    namespace: dict[str, Any] = {"__name__": "generated_server"}
    exec(compile(emitted.source, "<generated server>", "exec"), namespace)
    return namespace, seen, emitted


def _call(namespace: dict[str, Any]) -> Any:
    tool = namespace["list_widget"]
    return asyncio.run(tool({}))


def test_an_api_key_goes_in_the_header_the_service_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case that used to produce a bearer token and a 401."""
    namespace, seen, emitted = _load(
        "{type: apiKey, in: header, name: X-Widget-Key}", tmp_path, monkeypatch
    )
    variable = next(iter(emitted.credentials))
    monkeypatch.setenv(variable, "secret-value")

    _call(namespace)

    assert seen[0]["headers"].get("X-Widget-Key") == "secret-value"
    assert "Authorization" not in seen[0]["headers"]


def test_an_api_key_in_the_query_is_sent_as_a_parameter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace, seen, emitted = _load(
        "{type: apiKey, in: query, name: api_key}", tmp_path, monkeypatch
    )
    monkeypatch.setenv(next(iter(emitted.credentials)), "secret-value")

    _call(namespace)

    assert seen[0]["params"].get("api_key") == "secret-value"
    assert seen[0]["headers"] == {}


def test_basic_auth_is_encoded_rather_than_sent_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The variable holds `user:password`, because base64 in the environment cannot be read."""
    import base64

    namespace, seen, emitted = _load("{type: http, scheme: basic}", tmp_path, monkeypatch)
    monkeypatch.setenv(next(iter(emitted.credentials)), "alice:opensesame")

    _call(namespace)

    expected = base64.b64encode(b"alice:opensesame").decode("ascii")
    assert seen[0]["headers"]["Authorization"] == f"Basic {expected}"


def test_a_bearer_scheme_still_sends_a_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one case the old behaviour got right must keep working."""
    namespace, seen, emitted = _load("{type: http, scheme: bearer}", tmp_path, monkeypatch)
    monkeypatch.setenv(next(iter(emitted.credentials)), "token-value")

    _call(namespace)

    assert seen[0]["headers"]["Authorization"] == "Bearer token-value"


def test_an_unset_credential_is_omitted_rather_than_sent_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty header reads as a malformed request rather than an unauthenticated one."""
    namespace, seen, emitted = _load(
        "{type: apiKey, in: header, name: X-Widget-Key}", tmp_path, monkeypatch
    )
    monkeypatch.delenv(next(iter(emitted.credentials)), raising=False)

    _call(namespace)

    assert seen[0]["headers"] == {}


def test_no_credential_value_is_written_into_the_generated_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file names variables. It must never carry what they hold."""
    monkeypatch.setenv("GUARDED_SERVICE_GUARD_CREDENTIAL", "secret-value")
    _, _, emitted = _load("{type: apiKey, in: header, name: X-Widget-Key}", tmp_path, monkeypatch)

    assert "secret-value" not in emitted.source
    assert "GUARDED_SERVICE_GUARD_CREDENTIAL" in emitted.source


def test_the_variables_a_deployment_must_set_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise they are discovered by getting 401s in production."""
    _, _, emitted = _load("{type: apiKey, in: header, name: X-Widget-Key}", tmp_path, monkeypatch)

    assert emitted.credentials == {"GUARDED_SERVICE_GUARD_CREDENTIAL": "guard"}


def test_a_scheme_that_cannot_be_placed_is_not_invented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unplaceable scheme must send nothing rather than something plausible."""
    namespace, seen, emitted = _load(
        "{type: http, scheme: negotiate}", tmp_path, monkeypatch
    )
    assert emitted.credentials == {}

    _call(namespace)

    assert seen[0]["headers"] == {}
