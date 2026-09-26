"""Which SDK a generated server is written against, decided in one table.

The REST and SOAP emitters both read `codegen.registration`, so the imports, the surface class
and the requirements for a target cannot disagree between them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from api_mcp_compiler.codegen.mcp_server import GENERATED_REQUIREMENTS, emit_server
from api_mcp_compiler.codegen.registration import DEFAULT_SDK, REQUIREMENTS, check_sdk
from api_mcp_compiler.codegen.soap_server import emit_soap_server
from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy
from tests.conftest import INVENTORY_SERVICE
from tests.test_soap_server import _reviewed


def _inventory(sdk: int) -> str:
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    plan = plan_semantic(ir)
    manifest = synthesize_policy(ir, plan)
    return emit_server(ir, generate_surface(ir, plan, manifest), manifest, sdk=sdk).source


def test_the_default_target_serves_the_2026_07_28_protocol() -> None:
    assert DEFAULT_SDK == 2
    assert REQUIREMENTS[2] == GENERATED_REQUIREMENTS


@pytest.mark.parametrize(("sdk", "imported"), [
    (1, "from mcp.server.fastmcp import FastMCP"),
    (2, "from mcp.server.mcpserver import MCPServer"),
])
def test_each_target_imports_its_own_sdk(sdk: int, imported: str) -> None:
    source = _inventory(sdk)
    ast.parse(source)
    assert imported in source


@pytest.mark.parametrize("sdk", [1, 2])
def test_both_emitters_agree_on_requirements(sdk: int) -> None:
    ir, surface, manifest = _reviewed()
    soap = emit_soap_server(ir, surface, manifest, sdk=sdk)
    rest_ir = parse_openapi(Path(INVENTORY_SERVICE))
    plan = plan_semantic(rest_ir)
    policy = synthesize_policy(rest_ir, plan)
    rest = emit_server(rest_ir, generate_surface(rest_ir, plan, policy), policy, sdk=sdk)
    assert soap.requirements == rest.requirements == REQUIREMENTS[sdk]
    ast.parse(soap.source)


def test_every_requirement_is_bounded_above() -> None:
    """An unbounded SDK requirement installed a major every generated server failed on."""
    for requirements in REQUIREMENTS.values():
        mcp = next(item for item in requirements if item.startswith("mcp"))
        assert "<" in mcp


def test_an_unknown_target_is_refused() -> None:
    with pytest.raises(ValueError, match="supported"):
        check_sdk(3)
