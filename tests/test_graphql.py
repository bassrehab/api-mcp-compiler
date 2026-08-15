"""GraphQL ingestion.

Root fields map to operations without inference, so the tests are mostly about the one decision
this adapter makes on somebody's behalf: what comes back.

A GraphQL call is "invoke this field **and select these subfields**", and the selection is the
caller's to write. Letting an agent write it is the `execute_sql` problem with different syntax.
So the adapter selects the scalars one level deep, refuses depth, and says so where a reviewer
will read it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.ingest.graphql_sdl import (
    GraphqlIngestionError,
    is_graphql,
    parse_graphql,
)
from api_mcp_compiler.models import (
    EmissionStatus,
    Protocol,
    SideEffectClass,
    SourceFormat,
)
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy

EXAMPLE = Path("examples/graphql/claims.graphql")


def _sdl(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "schema.graphql"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _operation(ir, name: str):
    return next(item for item in ir.operations if item.operation_id == name)


def test_the_root_type_classifies_the_field() -> None:
    """Somebody put a field under Mutation because it changes something. No inference needed."""
    ir = parse_graphql(EXAMPLE)

    assert _operation(ir, "claim").side_effect is SideEffectClass.READ
    assert _operation(ir, "createClaim").side_effect is SideEffectClass.WRITE


def test_a_mutation_named_destructively_is_classified_destructively() -> None:
    """The root type separates reads from writes and does not separate an update from a
    deletion, so the shared vocabulary is consulted exactly as it is for an HTTP method."""
    ir = parse_graphql(EXAMPLE)

    assert _operation(ir, "purgeClaim").side_effect is SideEffectClass.DESTRUCTIVE


def test_a_query_described_destructively_is_raised_for_review(tmp_path: Path) -> None:
    """A read-labelled field described as deleting is either mislabelled or misimplemented,
    and an agent reading the description will act on the wording either way."""
    path = _sdl(
        tmp_path,
        '''
        type Query {
          "Deletes a claim permanently."
          claim(id: ID!): String
        }
        ''',
    )
    ir = parse_graphql(path)

    finding = next(
        item for item in ir.ambiguities if item.code == "side_effect_language_conflict"
    )
    assert finding.blocking
    assert _operation(ir, "claim").side_effect is SideEffectClass.READ


def test_the_selection_is_scalars_one_level_deep() -> None:
    """A selection set over a graph has no natural bottom, and any depth limit is a number
    this compiler made up. Refusing depth is the honest option."""
    ir = parse_graphql(EXAMPLE)

    returned = _operation(ir, "claim").outputs[0].type_schema["properties"]

    assert set(returned) == {"id", "status", "amount", "openedAt"}
    assert "customer" not in returned, "following an edge would be a request nobody reviewed"


def test_the_reviewer_is_told_what_the_selection_is() -> None:
    """It is derived rather than declared, so leaving it implicit would hide the one decision
    this adapter makes on somebody's behalf."""
    ir = parse_graphql(EXAMPLE)

    description = _operation(ir, "claim").description
    assert "nothing that requires following a reference" in description
    assert "a selection an agent composes is a request nobody reviewed" in description


def test_a_subscription_is_held_rather_than_skipped() -> None:
    """The same problem AsyncAPI has: an agent subscribed to a stream is not calling a tool."""
    ir = parse_graphql(EXAMPLE)

    finding = next(item for item in ir.ambiguities if item.code == "event_driven_operation")
    assert finding.blocking
    assert _operation(ir, "claimSettled").side_effect is SideEffectClass.UNKNOWN


def test_an_enum_argument_carries_its_values() -> None:
    """An agent given a free string where six values are legal will invent a seventh."""
    ir = parse_graphql(EXAMPLE)

    status = next(
        item for item in _operation(ir, "claimsForCustomer").inputs if item.name == "status"
    )
    assert status.type_schema == {"type": "string", "enum": ["CLOSED", "OPEN", "SETTLED"]}
    assert status.required is False


def test_non_null_marks_an_argument_required() -> None:
    ir = parse_graphql(EXAMPLE)

    identifier = next(item for item in _operation(ir, "claim").inputs if item.name == "id")
    assert identifier.required is True


def test_no_server_is_invented() -> None:
    """SDL names no endpoint, and asserting one would be a fact the document does not carry."""
    ir = parse_graphql(EXAMPLE)

    assert ir.service.servers == []
    assert all(item.route is None for item in ir.operations)
    assert ir.service.source_format is SourceFormat.GRAPHQL
    assert all(item.protocol is Protocol.GRAPHQL for item in ir.operations)


def test_a_schema_with_nothing_callable_is_refused(tmp_path: Path) -> None:
    path = _sdl(tmp_path, "type Claim { id: ID! }")

    with pytest.raises(GraphqlIngestionError, match="neither a Query nor a Mutation"):
        parse_graphql(path)


def test_malformed_sdl_is_refused_clearly(tmp_path: Path) -> None:
    path = _sdl(tmp_path, "type Query { claim(: ID!): Claim }")

    with pytest.raises(GraphqlIngestionError, match="not valid GraphQL SDL"):
        parse_graphql(path)


def test_it_is_recognised_without_a_version_marker() -> None:
    """SDL has no header, so detection is on a type definition."""
    assert is_graphql("type Query { a: String }")
    assert is_graphql("schema { query: Query }")
    assert not is_graphql("openapi: 3.0.3")


def test_the_example_compiles_to_a_governed_surface() -> None:
    ir = parse_graphql(EXAMPLE)
    plan = plan_semantic(ir)
    surface = generate_surface(ir, plan, synthesize_policy(ir, plan))

    destructive = [item for item in surface.tools if item.risk.value == "destructive"]
    assert destructive
    assert all(item.emission is EmissionStatus.DISABLED for item in destructive)

    servable = [item for item in surface.tools if item.emission is EmissionStatus.EXECUTABLE]
    assert servable and all(item.risk.value == "read" for item in servable)


def test_every_field_carries_provenance() -> None:
    ir = parse_graphql(EXAMPLE)

    for operation in ir.operations:
        recorded = {item.field for item in operation.provenance}
        assert {"operation_id", "side_effect", "protocol", "route"} <= recorded
