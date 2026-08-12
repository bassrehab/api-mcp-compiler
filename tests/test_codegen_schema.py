"""Schema composition tests.

The compiler copies type information from the source specification into the tool schemas it
emits. A real specification writes numbers and booleans as strings, so copying verbatim
produced schemas that no JSON Schema validator would accept: 22 of 40 tools on the benchmark
API. These tests cover the interpretation and the refusal that now stand between a malformed
source and a tool a client cannot load.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from api_mcp_compiler.codegen.schema import (
    InvalidGeneratedSchemaError,
    compose_input_schema,
    sanitize_schema,
)
from api_mcp_compiler.ingest.openapi import parse_openapi


def _spec(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "spec.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_numeric_keywords_written_as_strings_are_interpreted() -> None:
    """A real specification writes `"maximum": "50"`, which no validator accepts."""
    assert sanitize_schema({"type": "integer", "maximum": "50", "minimum": "0"}) == {
        "type": "integer",
        "maximum": 50,
        "minimum": 0,
    }


def test_boolean_schema_keywords_written_as_strings_are_interpreted() -> None:
    assert sanitize_schema({"type": "object", "additionalProperties": "true"}) == {
        "type": "object",
        "additionalProperties": True,
    }


def test_a_schema_valued_keyword_is_not_flattened_to_a_boolean() -> None:
    """`additionalProperties` may be a schema; treating it as a boolean would discard it."""
    assert sanitize_schema({"type": "object", "additionalProperties": {"maxLength": "8"}}) == {
        "type": "object",
        "additionalProperties": {"maxLength": 8},
    }


def test_nested_fragments_are_interpreted() -> None:
    assert sanitize_schema({"type": "array", "items": {"maxLength": "3"}, "maxItems": "9"}) == {
        "type": "array",
        "items": {"maxLength": 3},
        "maxItems": 9,
    }


def test_an_uninterpretable_value_is_left_alone_rather_than_guessed() -> None:
    """Left as found, so the validity check reports it instead of a guess concealing it."""
    assert sanitize_schema({"maximum": "quite large"}) == {"maximum": "quite large"}


def test_a_real_looking_specification_composes_a_valid_schema(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: StringKeywords, version: '1'}
        paths:
          /things:
            get:
              operationId: listThings
              parameters:
                - in: query
                  name: limit
                  required: "false"
                  schema: {type: integer, maximum: "50", minimum: "1"}
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    schema, _ = compose_input_schema(ir.operations[0])
    Draft202012Validator.check_schema(schema)
    assert schema["properties"]["limit"]["maximum"] == 50


def test_a_schema_that_cannot_be_repaired_is_refused_rather_than_emitted(tmp_path: Path) -> None:
    """Emission is refused, because the alternative is a client failing on a tool we shipped."""
    spec = _spec(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Unrepairable, version: '1'}
        paths:
          /things:
            get:
              operationId: listThings
              parameters:
                - in: query
                  name: limit
                  schema: {type: integer, maximum: "quite large"}
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    with pytest.raises(InvalidGeneratedSchemaError, match="not a valid JSON Schema"):
        compose_input_schema(ir.operations[0])
