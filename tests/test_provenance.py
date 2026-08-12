"""Provenance tests.

Every source-derived and inferred field must retain
provenance. A record that merely exists is not enough: these tests check that the pointer
it carries actually resolves in the source document, and that an inference is never
labelled as a fact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from lxml import etree

from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.ingest.wsdl import parse_wsdl
from api_mcp_compiler.models import (
    ApiSemanticIR,
    Derivation,
    Provenance,
    ProvenanceBearing,
)
from api_mcp_compiler.planning.baseline import plan_baseline
from api_mcp_compiler.provenance import (
    WSDL_XPATH_NAMESPACES,
    escape_pointer_token,
    json_pointer,
    openapi_pointer,
    operation_identifier,
    slug,
    source_digest,
    wsdl_pointer,
    xpath_literal,
    xpath_step,
)
from tests.conftest import (
    CUSTOMER_SERVICE,
    OPENAPI_EXAMPLES,
    ORDER_SERVICE,
    WSDL_EXAMPLES,
)


def _walk(node: object) -> list[ProvenanceBearing]:
    """Collect every provenance-bearing model reachable from a value."""
    found: list[ProvenanceBearing] = []
    if isinstance(node, ProvenanceBearing):
        found.append(node)
        for name in type(node).model_fields:
            if name != "provenance":
                found.extend(_walk(getattr(node, name)))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item))
    return found


def _ir_nodes(ir: ApiSemanticIR) -> list[ProvenanceBearing]:
    return [*_walk(ir.service), *_walk(ir.operations)]


def _resolve_json_pointer(document: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 pointer in URI-fragment form, raising KeyError if absent."""
    if pointer in {"#", "#/"}:
        return document
    node = document
    for raw in pointer.removeprefix("#/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        node = node[int(token)] if isinstance(node, list) else node[token]
    return node


def test_escape_pointer_token_escapes_tilde_before_slash() -> None:
    """Escaping the slash first would re-escape the tilde it introduces into `~01`."""
    assert escape_pointer_token("a/b") == "a~1b"
    assert escape_pointer_token("a~b") == "a~0b"
    assert escape_pointer_token("~/") == "~0~1"


def test_json_pointer_of_no_tokens_is_the_document_root() -> None:
    assert json_pointer() == "#"


def test_openapi_pointer_escapes_path_templates() -> None:
    """The seeded parser produced an ambiguous pointer for exactly this shape."""
    assert (
        openapi_pointer("paths", "/customers/{customer_id}", "get")
        == "openapi:#/paths/~1customers~1{customer_id}/get"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "'plain'"),
        ('has"double', "'has\"double'"),
        ("has'single", '"has\'single"'),
        ("both'and\"", "concat('both', \"'\", 'and\"')"),
    ],
)
def test_xpath_literal_quotes_every_case(value: str, expected: str) -> None:
    assert xpath_literal(value) == expected


def test_xpath_literal_with_both_quotes_evaluates_correctly() -> None:
    value = "both'and\""
    root = etree.fromstring(f'<r><n a="{value.replace(chr(34), "&quot;")}"/></r>'.encode())
    assert root.xpath(f"n[@a={xpath_literal(value)}]")


def test_wsdl_pointer_binds_the_namespace_prefix() -> None:
    """A prefix-free step would match only unnamespaced elements and silently find nothing."""
    assert (
        wsdl_pointer(xpath_step("definitions"), xpath_step("portType", name="P"))
        == "wsdl:/w:definitions/w:portType[@name='P']"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Synthetic Order and Refund Service", "synthetic-order-and-refund-service"),
        ("Ärchive  Service!", "archive-service"),
        ("CustomerService", "customerservice"),
        ("---", "unnamed"),
    ],
)
def test_slug_is_deterministic_and_ascii(value: str, expected: str) -> None:
    assert slug(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("getCustomer", "getCustomer"),
        # Runs of unsafe characters are not collapsed: collapsing would let two distinct
        # source names normalize onto the same identifier.
        ("get_/customers/{id}", "get__customers_id"),
        ("9lives", "op_9lives"),
        ("///", "unnamed_operation"),
    ],
)
def test_operation_identifier_is_safe_and_case_preserving(value: str, expected: str) -> None:
    assert operation_identifier(value) == expected


def test_source_digest_is_prefixed_sha256() -> None:
    assert source_digest(b"") == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


@pytest.mark.parametrize("example", [*OPENAPI_EXAMPLES, *WSDL_EXAMPLES])
def test_every_node_carries_at_least_one_provenance_record(example: str) -> None:
    source = Path(example)
    ir = parse_wsdl(source) if source.suffix == ".wsdl" else parse_openapi(source)
    nodes = _ir_nodes(ir)
    assert nodes
    without = [type(node).__name__ for node in nodes if not node.provenance]
    assert not without, f"{example}: nodes without provenance: {without}"


@pytest.mark.parametrize("example", [*OPENAPI_EXAMPLES, *WSDL_EXAMPLES])
def test_inferred_records_are_never_presented_as_certain(example: str) -> None:
    source = Path(example)
    ir = parse_wsdl(source) if source.suffix == ".wsdl" else parse_openapi(source)
    records: list[Provenance] = []
    for node in [*_ir_nodes(ir), *_walk(plan_baseline(ir).artifacts)]:
        records.extend(node.provenance)
    assert records
    for record in records:
        if record.derivation is Derivation.INFERRED:
            assert record.confidence < 1.0, record
        else:
            assert record.confidence == 1.0, record


@pytest.mark.parametrize("example", OPENAPI_EXAMPLES)
def test_openapi_provenance_pointers_resolve_in_the_source_document(example: str) -> None:
    source = Path(example)
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    ir = parse_openapi(source)
    unresolved: list[str] = []
    for node in _ir_nodes(ir):
        for record in node.provenance:
            pointer = record.source_pointer.removeprefix("openapi:")
            try:
                _resolve_json_pointer(document, pointer)
            except (KeyError, IndexError, TypeError):
                unresolved.append(record.source_pointer)
    assert not unresolved, f"{example}: unresolvable pointers: {sorted(set(unresolved))}"


@pytest.mark.parametrize("example", WSDL_EXAMPLES)
def test_wsdl_provenance_pointers_resolve_in_the_source_document(example: str) -> None:
    source = Path(example)
    root = etree.fromstring(source.read_bytes())
    ir = parse_wsdl(source)
    unresolved: list[str] = []
    for node in _ir_nodes(ir):
        for record in node.provenance:
            expression = record.source_pointer.removeprefix("wsdl:")
            if not root.xpath(expression, namespaces=WSDL_XPATH_NAMESPACES):
                unresolved.append(record.source_pointer)
    assert not unresolved, f"{example}: unresolvable pointers: {sorted(set(unresolved))}"


def test_ambiguity_pointers_are_scheme_prefixed() -> None:
    for example in (*OPENAPI_EXAMPLES, *WSDL_EXAMPLES):
        source = Path(example)
        ir = parse_wsdl(source) if source.suffix == ".wsdl" else parse_openapi(source)
        for item in ir.ambiguities:
            assert item.source_pointer.startswith(("openapi:", "wsdl:")), item


def test_inferred_side_effect_is_distinguishable_from_source_data() -> None:
    """The seeded parser stored a method guess with `derivation="source"`."""
    ir = parse_openapi(Path(ORDER_SERVICE))
    operation = next(item for item in ir.operations if item.operation_id == "createRefund")
    side_effect = next(
        record for record in operation.provenance if record.field == "side_effect"
    )
    intent = next(record for record in operation.provenance if record.field == "intent")
    assert side_effect.derivation is Derivation.INFERRED
    assert side_effect.confidence < 1.0
    assert intent.derivation is Derivation.SOURCE


def test_soap_side_effect_is_never_inferred() -> None:
    """WSDL carries no method-like signal, so a guess would be a fabrication."""
    ir = parse_wsdl(Path(CUSTOMER_SERVICE))
    record = next(
        item for item in ir.operations[0].provenance if item.field == "side_effect"
    )
    assert record.derivation is Derivation.DEFAULT


def test_baseline_plan_artifacts_trace_back_to_source_pointers() -> None:
    ir = parse_openapi(Path(ORDER_SERVICE))
    plan = plan_baseline(ir)
    pointers = {operation.source_pointer for operation in ir.operations}
    for artifact in plan.artifacts:
        assert artifact.provenance
        assert {record.source_pointer for record in artifact.provenance} <= pointers


def test_provenance_survives_a_json_round_trip() -> None:
    ir = parse_openapi(Path(ORDER_SERVICE))
    restored = ApiSemanticIR.model_validate(json.loads(json.dumps(ir.model_dump(mode="json"))))
    assert restored == ir
