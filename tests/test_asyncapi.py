"""AsyncAPI ingestion.

The adapter's interesting case is the one MCP has no shape for. An operation an agent can call
maps cleanly; an operation that calls the agent does not, and what this adapter does about that
is the whole of its design.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.ingest.asyncapi import (
    AsyncApiIngestionError,
    is_asyncapi,
    parse_asyncapi,
)
from api_mcp_compiler.models import (
    EmissionStatus,
    Protocol,
    SideEffectClass,
    SourceFormat,
)
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy

EXAMPLE = Path("examples/asyncapi/orders_events.yaml")


def _document(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "async.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_a_send_becomes_a_tool_and_a_receive_does_not() -> None:
    """MCP is request and response. An agent reacting to an event is not calling a tool, it is
    being invoked by one, and that is a different primitive rather than an awkward shape."""
    ir = parse_asyncapi(EXAMPLE)
    plan = plan_semantic(ir)
    surface = generate_surface(ir, plan, synthesize_policy(ir, plan))

    send = next(item for item in ir.operations if item.operation_id == "requestOrderCancellation")
    receive = next(item for item in ir.operations if item.operation_id == "onOrderSettled")

    assert send.side_effect is SideEffectClass.WRITE
    assert receive.side_effect is SideEffectClass.UNKNOWN

    emitted = {item.name: item for item in surface.tools}
    for tool in emitted.values():
        if "settled" in tool.name or "on_order" in tool.name:
            assert tool.emission is EmissionStatus.DISABLED


def test_a_receive_is_recorded_rather_than_skipped() -> None:
    """A surface that quietly omitted half a document would let somebody believe their
    event-driven estate was covered."""
    ir = parse_asyncapi(EXAMPLE)

    assert any(item.operation_id == "onOrderSettled" for item in ir.operations)
    finding = next(item for item in ir.ambiguities if item.code == "event_driven_operation")
    assert finding.blocking
    assert "not calling a tool" in finding.detail


def test_nothing_invents_a_polling_shape() -> None:
    """That would be this compiler designing an integration rather than reading one."""
    ir = parse_asyncapi(EXAMPLE)
    receive = next(item for item in ir.operations if item.operation_id == "onOrderSettled")

    assert receive.inputs == []
    assert receive.outputs[0].status == "000"
    assert "not something a caller invokes" in receive.outputs[0].description


def test_a_two_point_x_document_is_refused(tmp_path: Path) -> None:
    """In 2.x, `publish` describes what a client may do rather than what the server does.

    Guessing which reading a document intends produces a surface that looks right and does the
    opposite, which is the worst failure available to an adapter.
    """
    path = _document(
        tmp_path,
        """
        asyncapi: 2.6.0
        info: {title: Old, version: '1'}
        channels:
          orders:
            publish:
              operationId: onOrder
        """,
    )
    with pytest.raises(AsyncApiIngestionError, match=r"reads 3\.x"):
        parse_asyncapi(path)


def test_a_document_with_only_channels_is_refused(tmp_path: Path) -> None:
    """It says what exists and not what anybody may do with it."""
    path = _document(
        tmp_path,
        """
        asyncapi: 3.0.0
        info: {title: Bare, version: '1'}
        channels:
          orders: {address: orders.created}
        """,
    )
    with pytest.raises(AsyncApiIngestionError, match="declares no operations"):
        parse_asyncapi(path)


def test_the_channel_reaches_the_description() -> None:
    """`orders.created` and `payments.settled` are the same shape and not the same decision."""
    ir = parse_asyncapi(EXAMPLE)
    send = next(item for item in ir.operations if item.operation_id == "requestOrderCancellation")

    assert "orders.cancellations" in send.description
    assert "not the same as it having been acted on" in send.description


def test_publishing_is_not_reported_as_completion() -> None:
    """A surface silent about that lets an agent read acceptance as the work being done."""
    ir = parse_asyncapi(EXAMPLE)
    send = next(item for item in ir.operations if item.operation_id == "requestOrderCancellation")

    assert send.outputs[0].status == "202"
    assert "not the same as being acted on" in send.outputs[0].description


def test_channel_parameters_become_inputs() -> None:
    ir = parse_asyncapi(EXAMPLE)
    send = next(item for item in ir.operations if item.operation_id == "requestOrderCancellation")

    assert [item.name for item in send.inputs] == ["region"]
    assert send.route == "orders.cancellations"


def test_the_server_carries_its_protocol() -> None:
    ir = parse_asyncapi(EXAMPLE)

    assert [item.url for item in ir.service.servers] == ["kafka://broker.example.invalid:9092"]
    assert ir.service.source_format is SourceFormat.ASYNCAPI
    assert all(item.protocol is Protocol.ASYNC for item in ir.operations)


def test_only_local_channel_references_resolve(tmp_path: Path) -> None:
    """A remote one would make compiling depend on the network and on the moment it ran."""
    path = _document(
        tmp_path,
        """
        asyncapi: 3.0.0
        info: {title: Remote, version: '1'}
        channels:
          orders: {address: orders.created}
        operations:
          publishOrder:
            action: send
            summary: Publish
            channel: {$ref: 'https://elsewhere.invalid/channels.yaml#/orders'}
        """,
    )
    ir = parse_asyncapi(path)

    assert ir.operations[0].route is None


def test_it_is_recognised_by_its_marker() -> None:
    assert is_asyncapi({"asyncapi": "3.0.0"})
    assert not is_asyncapi({"openapi": "3.0.3"})


def test_every_field_carries_provenance() -> None:
    ir = parse_asyncapi(EXAMPLE)

    for operation in ir.operations:
        recorded = {item.field for item in operation.provenance}
        assert {"operation_id", "side_effect", "protocol", "route"} <= recorded
