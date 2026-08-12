"""Alignment tests between the Pydantic contracts and the JSON Schemas.

The Python models and the JSON Schemas are two statements of the same contract, and the
seeded scaffold had drifted between them: the schema allowed a `risk` value the models
could not represent, and no model existed for the tool-plan envelope at all. These tests
make that class of drift fail the build instead of surfacing at integration time.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from api_mcp_compiler import models
from api_mcp_compiler.contracts import (
    EVAL_CORPUS_SCHEMA,
    EVALUATION_RUN_SCHEMA,
    IR_SCHEMA,
    POLICY_MANIFEST_SCHEMA,
    PREREGISTRATION_SCHEMA,
    TOOL_PLAN_SCHEMA,
    ContractViolation,
    canonical_json,
    load_schema,
    validate_ir,
)

ALL_SCHEMAS = (
    IR_SCHEMA,
    TOOL_PLAN_SCHEMA,
    EVAL_CORPUS_SCHEMA,
    EVALUATION_RUN_SCHEMA,
    PREREGISTRATION_SCHEMA,
    POLICY_MANIFEST_SCHEMA,
)

# Every object definition in the two generated contracts, paired with the model that must
# produce it. A new definition or a new model field fails this table until it is paired.
IR_DEFINITIONS: dict[str, type[BaseModel]] = {
    "provenance": models.Provenance,
    "ambiguity": models.Ambiguity,
    "sourceDocument": models.SourceDocumentIR,
    "server": models.ServerIR,
    "authScheme": models.AuthSchemeIR,
    "authRequirement": models.AuthRequirementIR,
    "example": models.ExampleIR,
    "header": models.HeaderIR,
    "pagination": models.PaginationIR,
    "field": models.FieldIR,
    "response": models.ResponseIR,
    "fault": models.FaultIR,
    "soapBinding": models.SoapBindingIR,
    "operation": models.OperationIR,
    "service": models.ServiceIR,
}
TOOL_PLAN_DEFINITIONS: dict[str, type[BaseModel]] = {
    "provenance": models.Provenance,
    "artifact": models.ToolArtifact,
}


def _object_schema(schema: dict[str, Any], definition: str) -> dict[str, Any]:
    """Return the object schema for a definition, unwrapping array definitions."""
    node: dict[str, Any] = schema["$defs"][definition]
    if node.get("type") == "array":
        node = node["items"]
    return node


def _cases(
    schema_name: str, table: dict[str, type[BaseModel]]
) -> list[tuple[str, str, type[BaseModel]]]:
    return [(schema_name, definition, model) for definition, model in table.items()]


DEFINITION_CASES = [
    *_cases(IR_SCHEMA, IR_DEFINITIONS),
    *_cases(TOOL_PLAN_SCHEMA, TOOL_PLAN_DEFINITIONS),
]


@pytest.mark.parametrize("schema_name", ALL_SCHEMAS)
def test_schema_is_a_valid_draft_2020_12_schema(schema_name: str) -> None:
    Draft202012Validator.check_schema(load_schema(schema_name))


def test_every_schema_declares_a_unique_id() -> None:
    identifiers = [load_schema(name).get("$id") for name in ALL_SCHEMAS]
    missing = [name for name, value in zip(ALL_SCHEMAS, identifiers, strict=True) if not value]
    assert not missing, f"schema without $id: {missing}"
    assert len(set(identifiers)) == len(identifiers)


def test_ir_schema_version_is_pinned_to_the_python_constant() -> None:
    schema = load_schema(IR_SCHEMA)
    assert schema["properties"]["schema_version"]["const"] == models.IR_SCHEMA_VERSION


def test_tool_plan_schema_version_is_pinned_to_the_python_constant() -> None:
    schema = load_schema(TOOL_PLAN_SCHEMA)
    assert schema["properties"]["schema_version"]["const"] == models.TOOL_PLAN_SCHEMA_VERSION


@pytest.mark.parametrize(("schema_name", "definition", "model"), DEFINITION_CASES)
def test_schema_definition_declares_exactly_the_model_fields(
    schema_name: str, definition: str, model: type[BaseModel]
) -> None:
    node = _object_schema(load_schema(schema_name), definition)
    assert set(node["properties"]) == set(model.model_fields), (
        f"{schema_name}#/$defs/{definition} and {model.__name__} declare different fields"
    )


@pytest.mark.parametrize(("schema_name", "definition", "model"), DEFINITION_CASES)
def test_schema_requires_every_field_the_model_requires(
    schema_name: str, definition: str, model: type[BaseModel]
) -> None:
    node = _object_schema(load_schema(schema_name), definition)
    schema_required = set(node.get("required", []))
    model_required = {
        name for name, info in model.model_fields.items() if info.is_required()
    }
    assert model_required <= schema_required, (
        f"{schema_name}#/$defs/{definition} does not require {model_required - schema_required}"
    )
    assert schema_required <= set(model.model_fields)


@pytest.mark.parametrize(("schema_name", "definition", "_model"), DEFINITION_CASES)
def test_schema_definitions_forbid_unknown_fields(
    schema_name: str, definition: str, _model: type[BaseModel]
) -> None:
    """Strictness must match the models, which all set `extra="forbid"`."""
    node = _object_schema(load_schema(schema_name), definition)
    assert node.get("additionalProperties") is False


ENUM_CASES = [
    (IR_SCHEMA, ("$defs", "operation", "properties", "protocol"), models.Protocol),
    (IR_SCHEMA, ("$defs", "operation", "properties", "side_effect"), models.SideEffectClass),
    (IR_SCHEMA, ("$defs", "operation", "properties", "idempotency"), models.Idempotency),
    (IR_SCHEMA, ("$defs", "field", "properties", "location"), models.ParameterLocation),
    (IR_SCHEMA, ("$defs", "service", "properties", "source_format"), models.SourceFormat),
    (
        IR_SCHEMA,
        ("$defs", "provenance", "items", "properties", "derivation"),
        models.Derivation,
    ),
    (IR_SCHEMA, ("$defs", "authScheme", "properties", "type"), models.AuthSchemeType),
    (IR_SCHEMA, ("$defs", "sourceDocument", "properties", "role"), models.DocumentRole),
    (IR_SCHEMA, ("$defs", "pagination", "properties", "style"), models.PaginationStyle),
    (TOOL_PLAN_SCHEMA, ("properties", "planner"), models.PlannerKind),
    (TOOL_PLAN_SCHEMA, ("$defs", "artifact", "properties", "kind"), models.ArtifactKind),
    (TOOL_PLAN_SCHEMA, ("$defs", "artifact", "properties", "risk"), models.RiskClass),
    (
        TOOL_PLAN_SCHEMA,
        ("$defs", "artifact", "properties", "review_status"),
        models.ReviewStatus,
    ),
    (
        TOOL_PLAN_SCHEMA,
        ("$defs", "provenance", "items", "properties", "derivation"),
        models.Derivation,
    ),
]


@pytest.mark.parametrize(("schema_name", "path", "enum"), ENUM_CASES)
def test_schema_enum_matches_python_enum(
    schema_name: str, path: tuple[str, ...], enum: type[Enum]
) -> None:
    node: Any = load_schema(schema_name)
    for key in path:
        node = node[key]
    assert set(node["enum"]) == {member.value for member in enum}


def test_nullable_enum_matches_python_enum() -> None:
    """`style` is optional, so the schema wraps its enum in a oneOf with null."""
    node = load_schema(IR_SCHEMA)["$defs"]["field"]["properties"]["style"]
    branch = next(item for item in node["oneOf"] if "enum" in item)
    assert set(branch["enum"]) == {member.value for member in models.ParameterStyle}


def test_auth_scheme_rejects_fields_belonging_to_another_type() -> None:
    """An apiKey scheme carrying a bearer format would leave policy synthesis guessing."""
    with pytest.raises(ValidationError, match="must not carry bearer_format"):
        models.AuthSchemeIR(
            scheme_id="k",
            type=models.AuthSchemeType.API_KEY,
            api_key_in="header",
            api_key_name="X-Key",
            bearer_format="JWT",
            provenance=[
                models.Provenance(
                    field=name,
                    source_pointer="openapi:#/components/securitySchemes/k",
                    derivation=models.Derivation.SOURCE,
                    rule="test",
                )
                for name in ("scheme_id", "type", "api_key_in", "api_key_name", "bearer_format")
            ],
        )


def test_auth_scheme_requires_the_fields_its_type_implies() -> None:
    with pytest.raises(ValidationError, match="requires http_scheme"):
        models.AuthSchemeIR(
            scheme_id="h",
            type=models.AuthSchemeType.HTTP,
            provenance=[
                models.Provenance(
                    field=name,
                    source_pointer="openapi:#/components/securitySchemes/h",
                    derivation=models.Derivation.SOURCE,
                    rule="test",
                )
                for name in ("scheme_id", "type")
            ],
        )


def test_risk_class_is_a_superset_of_side_effect_class() -> None:
    """The tool plan may express `privileged`; the IR deliberately may not."""
    side_effects = {member.value for member in models.SideEffectClass}
    risks = {member.value for member in models.RiskClass}
    assert side_effects < risks
    assert risks - side_effects == {"privileged"}


def test_side_effect_to_risk_mapping_is_total_and_never_privileged() -> None:
    assert set(models.SIDE_EFFECT_TO_RISK) == set(models.SideEffectClass)
    assert models.RiskClass.PRIVILEGED not in models.SIDE_EFFECT_TO_RISK.values()


def test_inferred_provenance_must_carry_uncertainty() -> None:
    with pytest.raises(ValidationError, match=r"confidence below 1\.0"):
        models.Provenance(
            field="side_effect",
            source_pointer="openapi:#/paths",
            derivation=models.Derivation.INFERRED,
            rule="test",
            confidence=1.0,
        )


def test_source_provenance_must_not_carry_uncertainty() -> None:
    with pytest.raises(ValidationError, match=r"must have confidence 1\.0"):
        models.Provenance(
            field="intent",
            source_pointer="openapi:#/paths",
            derivation=models.Derivation.SOURCE,
            rule="test",
            confidence=0.5,
        )


def test_informative_field_without_provenance_is_rejected() -> None:
    with pytest.raises(ValidationError, match="missing provenance for: url"):
        models.ServerIR(url="https://example.invalid")


def test_empty_field_does_not_require_provenance() -> None:
    """An absent optional value carries no information, so it needs no trace."""
    server = models.ServerIR(
        url="https://example.invalid",
        description=None,
        provenance=[
            models.Provenance(
                field="url",
                source_pointer="openapi:#/servers/0/url",
                derivation=models.Derivation.SOURCE,
                rule="test",
            )
        ],
    )
    assert server.description is None


def test_provenance_for_an_unknown_field_is_rejected() -> None:
    """A typo in a provenance field name must not silently satisfy the invariant."""
    with pytest.raises(ValidationError, match="unknown fields: urls"):
        models.ServerIR(
            url="https://example.invalid",
            provenance=[
                models.Provenance(
                    field=name,
                    source_pointer="openapi:#/servers/0/url",
                    derivation=models.Derivation.SOURCE,
                    rule="test",
                )
                for name in ("url", "urls")
            ],
        )


def test_nested_provenance_bearing_values_are_exempt() -> None:
    """A service does not restate provenance its servers already carry."""
    service = models.ServiceIR(
        service_id="svc",
        title="Svc",
        source_format=models.SourceFormat.OPENAPI,
        source_digest="sha256:" + "0" * 64,
        servers=[
            models.ServerIR(
                url="https://example.invalid",
                provenance=[
                    models.Provenance(
                        field="url",
                        source_pointer="openapi:#/servers/0/url",
                        derivation=models.Derivation.SOURCE,
                        rule="test",
                    )
                ],
            )
        ],
        provenance=[
            models.Provenance(
                field=name,
                source_pointer="openapi:#",
                derivation=models.Derivation.SOURCE,
                rule="test",
            )
            for name in ("service_id", "title", "source_format", "source_digest")
        ],
    )
    assert len(service.servers) == 1


def test_ir_rejects_a_foreign_schema_version() -> None:
    with pytest.raises(ValidationError, match="expected IR schema_version"):
        models.ApiSemanticIR.model_validate(
            {
                "schema_version": "0.1.0",
                "service": {
                    "service_id": "svc",
                    "title": "Svc",
                    "source_format": "openapi",
                    "source_digest": "sha256:" + "0" * 64,
                    "provenance": [],
                },
                "operations": [],
            }
        )


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        models.Ambiguity(
            code="c",
            field="f",
            source_pointer="openapi:#",
            detail="d",
            blocking=False,
            severity="high",
        )


def test_validate_document_reports_every_error_at_once() -> None:
    payload = {"schema_version": "9.9.9", "service": {}, "operations": []}
    with pytest.raises(ContractViolation) as caught:
        validate_ir(payload)
    message = str(caught.value)
    assert "schema_version" in message
    assert "service" in message


def test_canonical_json_sorts_keys_and_ends_with_a_newline() -> None:
    text = canonical_json({"b": 1, "a": {"d": 2, "c": 3}})
    assert text.endswith("\n")
    assert text.index('"a"') < text.index('"b"')
    assert text.index('"c"') < text.index('"d"')


def test_canonical_json_preserves_sequence_order() -> None:
    """Operation order is source order and must never be sorted away."""
    assert canonical_json(["b", "a"]).splitlines()[1:3] == ['  "b",', '  "a"']
