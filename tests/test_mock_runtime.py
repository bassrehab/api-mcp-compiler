"""Mock runtime contract tests.

Refusing a tool at generation time is only a guarantee if invocation refuses too, so the
gate is tested from both ends. The rest of these tests are the contract check the roadmap
asks for: a generated surface must behave the way its own schemas claim.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.ingest.wsdl import parse_wsdl
from api_mcp_compiler.models import ApiSemanticIR, ToolSurface
from api_mcp_compiler.planning.baseline import plan_baseline
from api_mcp_compiler.runtime.mock import (
    ArgumentValidationError,
    MockExecutor,
    ToolDisabledError,
    ToolInvocationError,
)
from tests.conftest import ALL_EXAMPLES, CUSTOMER_SERVICE, INVENTORY_SERVICE, ORDER_SERVICE


def _build(example: str) -> tuple[ApiSemanticIR, ToolSurface]:
    source = Path(example)
    ir = parse_wsdl(source) if source.suffix == ".wsdl" else parse_openapi(source)
    return ir, generate_surface(ir, plan_baseline(ir))


def _executor(example: str) -> MockExecutor:
    ir, surface = _build(example)
    return MockExecutor(ir=ir, surface=surface)


def _minimal_arguments(schema: dict[str, object]) -> dict[str, object]:
    """Build the smallest argument object the schema accepts."""
    required = schema.get("required")
    names = required if isinstance(required, list) else []
    properties = schema.get("properties")
    resolved = properties if isinstance(properties, dict) else {}
    values: dict[str, object] = {}
    for name in names:
        declared = resolved.get(name, {})
        kind = declared.get("type") if isinstance(declared, dict) else None
        defaults: dict[str, object] = {
            "integer": 1,
            "number": 1.0,
            "boolean": True,
            "array": [],
            "object": {},
        }
        values[name] = defaults.get(str(kind), "value")
    return values


def test_executable_read_tool_can_be_invoked() -> None:
    executor = _executor(ORDER_SERVICE)
    response = executor.invoke("getCustomer", {"customer_id": "C-100"})
    assert response.status == "200"
    assert isinstance(response.body, dict)


def test_disabled_tool_refuses_invocation() -> None:
    """Refusing at generation time is only a guarantee if invocation refuses too."""
    executor = _executor(ORDER_SERVICE)
    with pytest.raises(ToolDisabledError, match="awaiting_approval"):
        executor.invoke("createRefund", {"body": {"order_id": "O-1", "reason": "dup"}})


def test_blocked_soap_tool_refuses_invocation() -> None:
    executor = _executor(CUSTOMER_SERVICE)
    with pytest.raises(ToolDisabledError):
        executor.invoke("GetCustomer", {"parameters": "x"})


def test_unknown_tool_is_rejected() -> None:
    with pytest.raises(ToolInvocationError, match="no tool named"):
        _executor(ORDER_SERVICE).invoke("nope")


def test_missing_required_argument_is_rejected() -> None:
    with pytest.raises(ArgumentValidationError, match="customer_id"):
        _executor(ORDER_SERVICE).invoke("getCustomer", {})


def test_unknown_argument_is_rejected() -> None:
    """A tool that accepted an argument it will not send would mislead the caller."""
    with pytest.raises(ArgumentValidationError):
        _executor(ORDER_SERVICE).invoke("getCustomer", {"customer_id": "C-1", "extra": 1})


def test_wrongly_typed_argument_is_rejected() -> None:
    with pytest.raises(ArgumentValidationError):
        _executor(INVENTORY_SERVICE).invoke(
            "listWarehouseItems", {"warehouse_id": "W-1", "page": "not-an-integer"}
        )


def test_declared_example_is_preferred_over_synthesis() -> None:
    """An example is what the author said the response looks like."""
    response = _executor(INVENTORY_SERVICE).invoke(
        "listWarehouseItems", {"warehouse_id": "W-1"}
    )
    assert response.from_example is True
    assert response.body == {"items": [{"sku": "SKU-1", "quantity": 4}]}


def test_synthesis_is_used_when_no_example_exists() -> None:
    response = _executor(ORDER_SERVICE).invoke("getCustomer", {"customer_id": "C-1"})
    assert response.from_example is False
    assert set(response.body) == {"id", "name"}


def test_invocation_is_deterministic_across_executors() -> None:
    """A random mock would make contract tests flaky and evaluation unattributable."""
    first = _executor(ORDER_SERVICE).invoke("getCustomer", {"customer_id": "C-1"})
    second = _executor(ORDER_SERVICE).invoke("getCustomer", {"customer_id": "C-1"})
    assert first.body == second.body


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_every_mock_response_satisfies_its_declared_schema(example: str) -> None:
    """The contract test the roadmap asks for at this stage."""
    ir, surface = _build(example)
    executor = MockExecutor(ir=ir, surface=surface)
    checked = 0
    for tool in surface.executable_tools:
        response = executor.invoke(tool.name, _minimal_arguments(tool.input_schema))
        if tool.output_schema is None or response.body is None:
            continue
        errors = list(Draft202012Validator(tool.output_schema).iter_errors(response.body))
        assert not errors, f"{tool.name}: {[item.message for item in errors]}"
        checked += 1
    if example != CUSTOMER_SERVICE:
        assert checked > 0, "no executable tool declared an output schema to check"


def test_lowest_success_status_is_returned(tmp_path: Path) -> None:
    """The ordinary success case, not whichever response was declared first."""
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        "openapi: 3.0.3\n"
        "info: {title: Statuses, version: '1'}\n"
        "paths:\n"
        "  /x:\n"
        "    get:\n"
        "      operationId: getX\n"
        "      responses:\n"
        "        '206': {description: partial}\n"
        "        '200': {description: ok}\n",
        encoding="utf-8",
    )
    ir = parse_openapi(spec)
    executor = MockExecutor(ir=ir, surface=generate_surface(ir, plan_baseline(ir)))
    assert executor.invoke("getX").status == "200"


def test_executor_reaches_no_network_or_filesystem() -> None:
    """The mock is the reason a surface can be exercised with no service and no credential."""
    source = Path("src/api_mcp_compiler/runtime/mock.py").read_text(encoding="utf-8")
    for forbidden in ("import requests", "import httpx", "urllib", "socket", "open("):
        assert forbidden not in source
