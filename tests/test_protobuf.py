"""gRPC and protobuf ingestion.

protobuf states types perfectly and says nothing about what a method does, which is the
opposite balance from OpenAPI. So the tests split accordingly: the type mapping has to be exact,
and the side-effect classification has to say unknown where a name carries no signal rather than
inventing one.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.ingest.protobuf import ProtobufIngestionError, parse_proto
from api_mcp_compiler.models import (
    EmissionStatus,
    Protocol,
    SideEffectClass,
    SourceFormat,
)
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy

EXAMPLE = Path("examples/proto/claims.proto")


def _proto(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "service.proto"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _operation(ir, name: str):
    return next(item for item in ir.operations if item.operation_id == name)


def test_a_method_name_classifies_it() -> None:
    """A .proto states types perfectly and says nothing about what a method does.

    `DeleteUser` and `GetUser` are the same shape, so the shared vocabulary does the work it
    does anywhere else.
    """
    ir = parse_proto(EXAMPLE)

    assert _operation(ir, "Claims_GetClaim").side_effect is SideEffectClass.READ
    assert _operation(ir, "Claims_DeleteClaim").side_effect is SideEffectClass.DESTRUCTIVE


def test_a_name_with_no_signal_is_unknown_rather_than_guessed(tmp_path: Path) -> None:
    """Guessing here would produce a tool whose danger nobody stated and nobody checked."""
    path = _proto(
        tmp_path,
        """
        syntax = "proto3";
        package t;
        message In { string a = 1; }
        message Out { string b = 1; }
        service S { rpc Frobnicate(In) returns (Out); }
        """,
    )
    ir = parse_proto(path)

    assert _operation(ir, "S_Frobnicate").side_effect is SideEffectClass.UNKNOWN
    record = next(item for item in ir.operations[0].provenance if item.field == "side_effect")
    assert record.confidence == 0.0


def test_an_empty_response_is_weak_evidence_of_a_command(tmp_path: Path) -> None:
    """Recorded as weak rather than acted on as if it were not."""
    path = _proto(
        tmp_path,
        """
        syntax = "proto3";
        package t;
        message In { string a = 1; }
        message Empty {}
        service S { rpc Frobnicate(In) returns (Empty); }
        """,
    )
    ir = parse_proto(path)

    assert _operation(ir, "S_Frobnicate").side_effect is SideEffectClass.WRITE
    record = next(item for item in ir.operations[0].provenance if item.field == "side_effect")
    assert record.confidence == 0.5, "a shape is weaker evidence than a name"


def test_a_streaming_method_is_held_rather_than_collapsed() -> None:
    """A tool call is one request and one response. Returning the first message, or buffering
    all of them, would be a shape this service did not offer."""
    ir = parse_proto(EXAMPLE)

    finding = next(item for item in ir.ambiguities if item.code == "streaming_method")
    assert finding.blocking
    assert "server streaming" in finding.detail
    assert _operation(ir, "Claims_WatchClaims").side_effect is SideEffectClass.UNKNOWN


def test_sixty_four_bit_integers_are_carried_as_strings() -> None:
    """The protobuf JSON mapping, not a choice made here.

    A value beyond 2^53 does not survive a JSON number, and an agent handed a silently rounded
    account identifier has no way to know it was.
    """
    ir = parse_proto(EXAMPLE)
    returned = _operation(ir, "Claims_GetClaim").outputs[0].type_schema["properties"]

    assert returned["amount_cents"]["type"] == "string"
    assert "64-bit" in returned["amount_cents"]["description"]
    assert returned["id"]["type"] == "string"


def test_a_repeated_field_becomes_an_array() -> None:
    ir = parse_proto(EXAMPLE)
    returned = _operation(ir, "Claims_GetClaim").outputs[0].type_schema["properties"]

    assert returned["tags"] == {"type": "array", "items": {"type": "string"}}


def test_nothing_is_marked_required() -> None:
    """proto3 has no required. Everything is optional on the wire, and saying otherwise would
    make an agent fill in fields the service never demanded."""
    ir = parse_proto(EXAMPLE)

    for operation in ir.operations:
        assert all(not item.required for item in operation.inputs)


def test_the_route_is_the_canonical_grpc_path() -> None:
    """Every gRPC client builds it the same way, so it is normalization rather than
    invention."""
    ir = parse_proto(EXAMPLE)

    assert _operation(ir, "Claims_GetClaim").route == "/claims.v1.Claims/GetClaim"
    assert ir.service.servers == [], "a .proto names no host"
    assert ir.service.source_format is SourceFormat.PROTOBUF
    assert all(item.protocol is Protocol.GRPC for item in ir.operations)


def test_a_file_with_no_service_is_refused(tmp_path: Path) -> None:
    """It says what data looks like and not what anybody may call."""
    path = _proto(
        tmp_path,
        """
        syntax = "proto3";
        package t;
        message Only { string a = 1; }
        """,
    )
    with pytest.raises(ProtobufIngestionError, match="declares no service"):
        parse_proto(path)


def test_a_malformed_file_reports_protoc(tmp_path: Path) -> None:
    path = _proto(tmp_path, "syntax = \"proto3\"; service { rpc }")

    with pytest.raises(ProtobufIngestionError, match="could not be compiled"):
        parse_proto(path)


def test_the_example_compiles_to_a_governed_surface() -> None:
    ir = parse_proto(EXAMPLE)
    plan = plan_semantic(ir)
    surface = generate_surface(ir, plan, synthesize_policy(ir, plan))

    destructive = [item for item in surface.tools if item.risk.value == "destructive"]
    assert destructive
    assert all(item.emission is EmissionStatus.DISABLED for item in destructive)

    servable = [item for item in surface.tools if item.emission is EmissionStatus.EXECUTABLE]
    assert servable and all(item.risk.value == "read" for item in servable)


def test_every_field_carries_provenance() -> None:
    ir = parse_proto(EXAMPLE)

    for operation in ir.operations:
        recorded = {item.field for item in operation.provenance}
        assert {"operation_id", "side_effect", "protocol", "route"} <= recorded
        for field in operation.inputs:
            assert {item.field for item in field.provenance} >= {"name", "type_schema"}
