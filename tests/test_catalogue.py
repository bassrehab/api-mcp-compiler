"""Query catalogue ingestion.

The format exists so this compiler can address databases without becoming a query builder, and
`docs/query-catalogue.md` makes that argument. What is protected here is the derivation that
has no equivalent anywhere else in the project: a statement says whether it touches one row or
every row, and nothing in HTTP does.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.ingest.catalogue import (
    CatalogueIngestionError,
    is_catalogue,
    parse_catalogue,
)
from api_mcp_compiler.models import EmissionStatus, Protocol, SideEffectClass, SourceFormat
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy

EXAMPLE = Path("examples/catalogue/claims_warehouse.yaml")


def _catalogue(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "catalogue.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _one(tmp_path: Path, statement: str, *, parameters: str = "", **extra: str) -> Path:
    """One query, written as YAML rather than assembled, so indentation is not a variable.

    `parameters` is supplied by the caller because the adapter refuses a placeholder with no
    declaration, which is the behaviour under test elsewhere and would otherwise make every
    fixture here fail for the wrong reason.
    """
    body = [
        "catalogue: 1",
        "service: {name: Warehouse}",
        "queries:",
        "  - id: q",
        "    summary: A query",
        f"    statement: {statement!r}",
    ]
    body += [f"    {key}: {value}" for key, value in extra.items()]
    if parameters:
        body.append("    parameters:")
        body += [f"      - {item}" for item in parameters.split(";")]
    path = tmp_path / "catalogue.yaml"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def test_the_verb_classifies_the_statement(tmp_path: Path) -> None:
    """Stronger than an HTTP method: somebody chose the verb to describe the statement rather
    than to fit a protocol."""
    cases = {
        "SELECT id FROM claims WHERE id = 1": SideEffectClass.READ,
        "INSERT INTO claims (id) VALUES (1)": SideEffectClass.WRITE,
        "UPDATE claims SET status = 'x' WHERE id = 1": SideEffectClass.WRITE,
        "DELETE FROM claims WHERE id = 1": SideEffectClass.DESTRUCTIVE,
        "TRUNCATE TABLE claims": SideEffectClass.DESTRUCTIVE,
        "GRANT SELECT ON claims TO agent": SideEffectClass.WRITE,
    }
    for statement, expected in cases.items():
        ir = parse_catalogue(_one(tmp_path, statement))
        assert ir.operations[0].side_effect is expected, statement


def test_a_permission_changing_statement_needs_a_person(tmp_path: Path) -> None:
    """An agent able to grant permissions can grant itself more.

    `privileged` is a risk class the planner assigns rather than a side effect a catalogue can
    declare, so this is raised as an ambiguity instead of invented into the enum.
    """
    ir = parse_catalogue(_one(tmp_path, "GRANT SELECT ON claims TO agent"))

    finding = next(
        item for item in ir.ambiguities if item.code == "permission_changing_statement"
    )
    assert finding.blocking
    assert "grant itself more" in finding.detail


def test_a_mutation_with_no_predicate_is_raised_as_blocking(tmp_path: Path) -> None:
    """The sharpest distinction anywhere in this project.

    `DELETE FROM claims WHERE id = :id` and `DELETE FROM claims` differ by a clause and by a
    company, and nothing in HTTP separates them.
    """
    ir = parse_catalogue(_one(tmp_path, "DELETE FROM claims"))

    finding = next(item for item in ir.ambiguities if item.code == "unbounded_mutation")
    assert finding.blocking
    assert "differ by a clause and by a company" in finding.detail


def test_a_bounded_mutation_is_not_raised(tmp_path: Path) -> None:
    """Or every catalogue would be blocked and the distinction would carry no information."""
    ir = parse_catalogue(
        _one(
            tmp_path,
            "DELETE FROM claims WHERE id = :id",
            parameters="{name: id, type: string}",
        )
    )

    assert not [item for item in ir.ambiguities if item.code == "unbounded_mutation"]


def test_an_unbounded_read_is_not_raised(tmp_path: Path) -> None:
    """A SELECT with no predicate returns a large result, which is an output-size question the
    policy layer already answers. It is not the same problem."""
    ir = parse_catalogue(_one(tmp_path, "SELECT id FROM claims"))

    assert not [item for item in ir.ambiguities if item.code == "unbounded_mutation"]


def test_a_where_clause_in_a_comment_does_not_count(tmp_path: Path) -> None:
    """Otherwise a comment could disarm the one check that separates a row from a company."""
    ir = parse_catalogue(_one(tmp_path, "DELETE FROM claims -- WHERE id = :id"))

    assert [item for item in ir.ambiguities if item.code == "unbounded_mutation"]


def test_a_declaration_may_raise_a_class_and_never_lower_it(tmp_path: Path) -> None:
    """Lowering one would route a destructive statement past the gate by editing a document,
    and a gate that can be edited past is not one."""
    raised = parse_catalogue(
        _one(tmp_path, "SELECT id FROM claims WHERE id = 1", side_effect="destructive")
    )
    assert raised.operations[0].side_effect is SideEffectClass.DESTRUCTIVE

    with pytest.raises(CatalogueIngestionError, match="may only raise"):
        parse_catalogue(
            _one(tmp_path, "DELETE FROM claims WHERE id = 1", side_effect="read")
        )


def test_a_placeholder_with_no_parameter_is_an_error(tmp_path: Path) -> None:
    """Inventing a type would produce a tool argument an agent can fill wrongly with no way
    to know it did."""
    with pytest.raises(CatalogueIngestionError, match="declares no parameter"):
        parse_catalogue(_one(tmp_path, "SELECT id FROM claims WHERE id = :missing"))


def test_a_parameter_the_statement_never_uses_is_an_error(tmp_path: Path) -> None:
    """An argument that reaches nothing is one an agent spends a call filling in."""
    path = _catalogue(
        tmp_path,
        """
        catalogue: 1
        service: {name: Warehouse}
        queries:
          - id: q
            summary: A query
            statement: SELECT id FROM claims
            parameters:
              - {name: unused, type: string}
        """,
    )
    with pytest.raises(CatalogueIngestionError, match="never uses"):
        parse_catalogue(path)


def test_a_repeated_id_is_refused(tmp_path: Path) -> None:
    """An identifier is what a decision binds to, so two queries sharing one means an approval
    cannot say which it was about."""
    path = _catalogue(
        tmp_path,
        """
        catalogue: 1
        service: {name: Warehouse}
        queries:
          - {id: q, summary: One, statement: SELECT 1}
          - {id: q, summary: Two, statement: SELECT 2}
        """,
    )
    with pytest.raises(CatalogueIngestionError, match="repeats the id"):
        parse_catalogue(path)


def test_an_unrecognised_verb_blocks_rather_than_guessing(tmp_path: Path) -> None:
    ir = parse_catalogue(_one(tmp_path, "VACUUM claims"))

    assert ir.operations[0].side_effect is SideEffectClass.UNKNOWN
    finding = next(
        item for item in ir.ambiguities if item.code == "unclassified_statement_verb"
    )
    assert finding.blocking


def test_the_statement_reaches_the_description(tmp_path: Path) -> None:
    """A reviewer deciding whether to expose a query needs to read the query.

    Summarising it would hide the clause that decides its blast radius, which is the one thing
    they are there to look at.
    """
    ir = parse_catalogue(
        _one(
            tmp_path,
            "DELETE FROM claims WHERE id = :id",
            parameters="{name: id, type: string}",
        )
    )

    assert "DELETE FROM claims WHERE id = :id" in ir.operations[0].description


def test_no_server_is_recorded(tmp_path: Path) -> None:
    """A catalogue names a connection the deployment resolves. A connection string in a
    specification is a credential in version control."""
    ir = parse_catalogue(EXAMPLE)

    assert ir.service.servers == []
    assert ir.service.source_format is SourceFormat.CATALOGUE
    assert all(item.protocol is Protocol.SQL for item in ir.operations)


def test_every_field_carries_provenance(tmp_path: Path) -> None:
    """The same completeness discipline every other adapter is held to."""
    ir = parse_catalogue(EXAMPLE)

    for operation in ir.operations:
        recorded = {item.field for item in operation.provenance}
        assert {"operation_id", "side_effect", "protocol", "idempotency"} <= recorded
        for field in operation.inputs:
            assert {item.field for item in field.provenance} >= {"name", "type_schema"}


def test_the_example_compiles_to_a_governed_surface() -> None:
    """End to end, because an adapter that produces an IR nothing downstream accepts is not
    an adapter."""
    ir = parse_catalogue(EXAMPLE)
    plan = plan_semantic(ir)
    surface = generate_surface(ir, plan, synthesize_policy(ir, plan))

    by_risk = {item.risk.value for item in surface.tools}
    assert {"read", "write", "destructive"} <= by_risk

    unbounded = next(item for item in surface.tools if item.risk.value == "destructive")
    assert unbounded.emission is EmissionStatus.DISABLED
    assert "blocking_ambiguity" in {item.value for item in unbounded.blockers}

    servable = [item for item in surface.tools if item.emission is EmissionStatus.EXECUTABLE]
    assert servable and all(item.risk.value == "read" for item in servable)


def test_a_catalogue_is_recognised_by_its_marker() -> None:
    """A catalogue and an OpenAPI document are both YAML, and a caller should not have to say
    which."""
    assert is_catalogue({"catalogue": 1, "queries": []})
    assert not is_catalogue({"openapi": "3.0.3", "paths": {}})
    assert not is_catalogue("not a mapping")


def test_a_future_catalogue_version_is_refused(tmp_path: Path) -> None:
    path = _catalogue(
        tmp_path,
        """
        catalogue: 2
        service: {name: Warehouse}
        queries:
          - {id: q, summary: One, statement: SELECT 1}
        """,
    )
    with pytest.raises(CatalogueIngestionError, match="understands 1"):
        parse_catalogue(path)


def test_an_unknown_key_is_recorded_rather_than_dropped(tmp_path: Path) -> None:
    path = _catalogue(
        tmp_path,
        """
        catalogue: 1
        service: {name: Warehouse}
        experiments: {something: true}
        queries:
          - {id: q, summary: One, statement: SELECT 1}
        """,
    )
    ir = parse_catalogue(path)

    finding = next(item for item in ir.ambiguities if item.code == "unmapped_catalogue_key")
    assert finding.field == "experiments"
    assert not finding.blocking
