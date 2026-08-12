"""Completeness sweep and inference tests.

The sweep is what turns "nothing is dropped silently" from a promise repeated per construct
into a structural guarantee, so these tests check that an unrecognized key really does
surface rather than vanishing.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from api_mcp_compiler.ingest.documents import DuplicateKeyError
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.models import (
    Derivation,
    PaginationStyle,
    ParameterStyle,
    SideEffectClass,
)
from tests.conftest import OPENAPI_EXAMPLES


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "spec.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_unconsumed_key_is_reported(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Sweep, version: '1'}
        tags: [{name: unread}]
        paths:
          /x:
            get:
              operationId: getX
              callbacks: {onEvent: {}}
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    unconsumed = {item.field for item in ir.ambiguities if item.code == "unconsumed_key"}
    assert "document" in unconsumed
    assert "paths./x.get" in unconsumed
    assert all(
        item.blocking is False for item in ir.ambiguities if item.code == "unconsumed_key"
    )


def test_vendor_extension_is_reported_separately(tmp_path: Path) -> None:
    """Vendor extensions carry agent-relevant annotations, so they get their own code."""
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Vendor, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              x-agent-hint: prefer-this-tool
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    vendor = [item for item in ir.ambiguities if item.code == "vendor_extension"]
    assert len(vendor) == 1
    assert vendor[0].source_pointer.endswith("x-agent-hint")
    assert vendor[0].blocking is False


@pytest.mark.parametrize("example", OPENAPI_EXAMPLES)
def test_committed_examples_have_no_unconsumed_keys(example: str) -> None:
    """The fixtures should exercise supported constructs only, so the sweep stays quiet."""
    ir = parse_openapi(Path(example))
    leftovers = [
        item.source_pointer for item in ir.ambiguities if item.code == "unconsumed_key"
    ]
    assert not leftovers


def test_destructive_language_escalates_a_write(tmp_path: Path) -> None:
    """A POST described as permanently deleting must not reach review labelled a write."""
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Escalate, version: '1'}
        paths:
          /items/{id}/purge:
            post:
              operationId: purgeItem
              summary: Permanently delete every revision of an item
              responses: {'202': {description: accepted}}
        """,
    )
    operation = parse_openapi(spec).operations[0]
    assert operation.side_effect is SideEffectClass.DESTRUCTIVE
    rules = [item.rule for item in operation.provenance if item.field == "side_effect"]
    assert "openapi.side_effect.method.post" in rules
    assert any(rule.startswith("openapi.side_effect.language_escalation") for rule in rules)


def test_escalation_never_lowers_a_class(tmp_path: Path) -> None:
    """A DELETE stays destructive whatever the wording says."""
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: NoLower, version: '1'}
        paths:
          /x:
            delete:
              operationId: fetchX
              summary: Retrieve a listing
              responses: {'204': {description: gone}}
        """,
    )
    assert parse_openapi(spec).operations[0].side_effect is SideEffectClass.DESTRUCTIVE


def test_read_described_destructively_is_flagged_not_reclassified(tmp_path: Path) -> None:
    """A safe method with destructive wording is a contradiction, so a human decides."""
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Conflict, version: '1'}
        paths:
          /x:
            get:
              operationId: deleteEverything
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    assert ir.operations[0].side_effect is SideEffectClass.READ
    conflicts = [
        item for item in ir.ambiguities if item.code == "side_effect_language_conflict"
    ]
    assert len(conflicts) == 1
    assert conflicts[0].blocking is True


def test_past_tense_does_not_trigger_escalation(tmp_path: Path) -> None:
    """Whole-token matching keeps `listDeletedItems` from reading as a delete."""
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Tense, version: '1'}
        paths:
          /x:
            get:
              operationId: listDeletedItems
              summary: List items deleted today
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    assert ir.operations[0].side_effect is SideEffectClass.READ
    assert not [
        item for item in ir.ambiguities if item.code == "side_effect_language_conflict"
    ]


@pytest.mark.parametrize(
    ("parameters", "style"),
    [
        ("- {in: query, name: cursor, schema: {type: string}}", PaginationStyle.CURSOR),
        ("- {in: query, name: page, schema: {type: integer}}", PaginationStyle.PAGE_NUMBER),
        (
            "- {in: query, name: offset, schema: {type: integer}}\n"
            "                - {in: query, name: limit, schema: {type: integer}}",
            PaginationStyle.OFFSET_LIMIT,
        ),
    ],
)
def test_pagination_style_is_proposed(
    tmp_path: Path, parameters: str, style: PaginationStyle
) -> None:
    spec = _write(
        tmp_path,
        f"""
        openapi: 3.0.3
        info: {{title: Paging, version: '1'}}
        paths:
          /x:
            get:
              operationId: listX
              parameters:
                {parameters}
              responses: {{'200': {{description: ok}}}}
        """,
    )
    pagination = parse_openapi(spec).operations[0].pagination
    assert pagination is not None
    assert pagination.style is style
    record = next(item for item in pagination.provenance if item.field == "style")
    assert record.derivation is Derivation.INFERRED
    assert record.confidence < 1.0


def test_pagination_is_not_proposed_without_evidence(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: NoPaging, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              responses: {'200': {description: ok}}
        """,
    )
    assert parse_openapi(spec).operations[0].pagination is None


def test_link_header_alone_proposes_pagination(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: LinkPaging, version: '1'}
        paths:
          /x:
            get:
              operationId: listX
              responses:
                '200':
                  description: ok
                  headers:
                    Link: {schema: {type: string}, description: RFC 8288 links}
        """,
    )
    pagination = parse_openapi(spec).operations[0].pagination
    assert pagination is not None
    assert pagination.style is PaginationStyle.LINK_HEADER
    assert pagination.next_link_header == "Link"


def test_response_headers_are_captured(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Headers, version: '1'}
        paths:
          /x:
            post:
              operationId: startJob
              responses:
                '202':
                  description: accepted
                  headers:
                    Location: {schema: {type: string}, description: poll here, required: true}
        """,
    )
    headers = parse_openapi(spec).operations[0].outputs[0].headers
    assert [item.name for item in headers] == ["Location"]
    assert headers[0].required is True
    assert headers[0].type_schema == {"type": "string"}


def test_unknown_parameter_style_is_reported(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: BadStyle, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              parameters:
                - {in: query, name: q, schema: {type: string}, style: nonsense}
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    assert ir.operations[0].inputs[0].style is None
    assert any(item.code == "unknown_parameter_style" for item in ir.ambiguities)


def test_known_parameter_style_is_carried(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: GoodStyle, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              parameters:
                - {in: query, name: q, schema: {type: array}, style: pipeDelimited, explode: false}
              responses: {'200': {description: ok}}
        """,
    )
    field = parse_openapi(spec).operations[0].inputs[0]
    assert field.style is ParameterStyle.PIPE_DELIMITED
    assert field.explode is False


def test_duplicate_yaml_key_is_refused(tmp_path: Path) -> None:
    """`yaml.safe_load` keeps only the last, so an operation would vanish untraceably.

    This is the exact mistake that produced the defect: two `get` entries under one path,
    and the sweep cannot report a key that never reached the parser.
    """
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Dup, version: '1'}
        paths:
          /x:
            get:
              operationId: first
              responses: {'200': {description: ok}}
            get:
              operationId: second
              responses: {'200': {description: ok}}
        """,
    )
    with pytest.raises(DuplicateKeyError, match="duplicate key 'get'"):
        parse_openapi(spec)


def test_duplicate_json_key_is_refused(tmp_path: Path) -> None:
    """`json.loads` has the same behaviour, so it needs the same refusal."""
    spec = tmp_path / "spec.json"
    spec.write_text(
        '{"openapi": "3.0.3", "info": {"title": "D", "version": "1"},'
        ' "paths": {}, "paths": {}}',
        encoding="utf-8",
    )
    with pytest.raises(DuplicateKeyError, match="duplicate key 'paths'"):
        parse_openapi(spec)


def test_duplicate_key_error_names_the_location(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Dup, version: '1'}
        info: {title: Other, version: '2'}
        paths: {}
        """,
    )
    # Line 4 of the dedented fixture: the triple-quoted string opens with a newline.
    with pytest.raises(DuplicateKeyError, match="line 4, column 1"):
        parse_openapi(spec)
