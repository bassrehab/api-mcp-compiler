"""OpenAPI ingestion tests."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from api_mcp_compiler.ingest.openapi import OpenApiIngestionError, parse_openapi
from api_mcp_compiler.models import (
    ApiSemanticIR,
    Derivation,
    Idempotency,
    ParameterLocation,
    Protocol,
    SideEffectClass,
    SourceFormat,
)
from api_mcp_compiler.planning.baseline import operation_per_tool
from tests.conftest import INVENTORY_SERVICE, ORDER_SERVICE


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "spec.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_order_service_parses() -> None:
    ir = parse_openapi(Path(ORDER_SERVICE))
    assert [item.operation_id for item in ir.operations] == [
        "getCustomer",
        "listCustomerOrders",
        "createRefund",
        "approveRefund",
    ]
    assert len(operation_per_tool(ir)) == len(ir.operations)
    assert ir.operations[0].route == "/customers/{customer_id}"


def test_service_identity_is_captured() -> None:
    ir = parse_openapi(Path(ORDER_SERVICE))
    assert ir.service.service_id == "synthetic-order-and-refund-service"
    assert ir.service.title == "Synthetic Order and Refund Service"
    assert ir.service.version == "1.0.0"
    assert ir.service.source_format is SourceFormat.OPENAPI
    assert ir.service.source_digest.startswith("sha256:")
    assert [server.url for server in ir.service.servers] == ["https://api.example.invalid"]


@pytest.mark.parametrize(
    ("operation_id", "side_effect", "idempotency"),
    [
        ("getCustomer", SideEffectClass.READ, Idempotency.IDEMPOTENT),
        ("createRefund", SideEffectClass.WRITE, Idempotency.NON_IDEMPOTENT),
    ],
)
def test_method_semantics_are_proposed(
    operation_id: str, side_effect: SideEffectClass, idempotency: Idempotency
) -> None:
    ir = parse_openapi(Path(ORDER_SERVICE))
    operation = next(item for item in ir.operations if item.operation_id == operation_id)
    assert operation.protocol is Protocol.HTTP
    assert operation.side_effect is side_effect
    assert operation.idempotency is idempotency


def test_delete_is_classified_destructive() -> None:
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    operation = next(
        item for item in ir.operations if item.operation_id == "purgeWarehouseItems"
    )
    assert operation.side_effect is SideEffectClass.DESTRUCTIVE


def test_request_body_is_captured() -> None:
    """The seeded parser dropped request bodies, leaving the only write tool argumentless."""
    ir = parse_openapi(Path(ORDER_SERVICE))
    operation = next(item for item in ir.operations if item.operation_id == "createRefund")
    body = next(item for item in operation.inputs if item.location is ParameterLocation.BODY)
    assert body.required is True
    assert body.media_type == "application/json"
    assert body.type_schema is not None
    assert body.type_schema["required"] == ["order_id", "reason"]


def test_path_item_parameters_are_inherited_by_every_method() -> None:
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    for operation_id in ("listWarehouseItems", "purgeWarehouseItems"):
        operation = next(item for item in ir.operations if item.operation_id == operation_id)
        names = {item.name: item.location for item in operation.inputs}
        assert names["warehouse_id"] is ParameterLocation.PATH


def test_operation_parameter_overrides_the_path_item_parameter(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Override, version: '1'}
        paths:
          /x:
            parameters:
              - {in: query, name: limit, required: false, schema: {type: string}}
            get:
              operationId: getX
              parameters:
                - {in: query, name: limit, required: true, schema: {type: integer}}
              responses: {'200': {description: ok}}
        """,
    )
    operation = parse_openapi(spec).operations[0]
    limits = [item for item in operation.inputs if item.name == "limit"]
    assert len(limits) == 1
    assert limits[0].required is True
    assert limits[0].type_schema == {"type": "integer"}


def test_parameter_omitting_required_is_accepted(tmp_path: Path) -> None:
    """`required` defaults to false and is routinely omitted on query parameters.

    Every committed fixture declared it explicitly, so the provenance invariant for the
    defaulted value went unexercised and the parser raised on ordinary input.
    """
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Minimal, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              parameters:
                - {in: query, name: cursor, schema: {type: string}}
              responses: {'200': {description: ok}}
        """,
    )
    field = parse_openapi(spec).operations[0].inputs[0]
    assert field.required is False
    record = next(item for item in field.provenance if item.field == "required")
    assert record.derivation is Derivation.DEFAULT


def test_operation_with_only_mandatory_keys_is_accepted(tmp_path: Path) -> None:
    """Every optional construct omitted at once, so no defaulted field loses provenance."""
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Bare, version: '1'}
        paths:
          /x:
            get:
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    assert len(ir.operations) == 1
    assert ir.operations[0].inputs == []


def test_responses_split_into_outputs_and_faults() -> None:
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    operation = next(
        item for item in ir.operations if item.operation_id == "listWarehouseItems"
    )
    assert [item.status for item in operation.outputs] == ["200"]
    assert [item.code for item in operation.faults] == ["default"]
    assert operation.faults[0].media_type == "application/json"


def test_retryability_is_never_guessed() -> None:
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    operation = next(
        item for item in ir.operations if item.operation_id == "listWarehouseItems"
    )
    assert operation.faults[0].retryable is None


def test_local_reference_is_resolved_in_place() -> None:
    """The response schema is a `$ref`; the IR must carry the resolved object."""
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    operation = next(
        item for item in ir.operations if item.operation_id == "listWarehouseItems"
    )
    schema = operation.outputs[0].type_schema
    assert schema is not None
    assert "$ref" not in schema
    assert schema["properties"]["items"]["type"] == "array"
    assert not [item for item in ir.ambiguities if item.code.startswith("ref_")]


def test_resolved_reference_keeps_a_pointer_to_the_reference_site() -> None:
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    operation = next(
        item for item in ir.operations if item.operation_id == "listWarehouseItems"
    )
    record = next(
        item for item in operation.outputs[0].provenance if item.field == "type_schema"
    )
    assert record.source_pointer.endswith("/content/application~1json/schema")


def test_reference_to_a_missing_target_is_reported(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Ref, version: '1'}
        paths:
          /x:
            $ref: '#/components/pathItems/Absent'
        """,
    )
    ir = parse_openapi(spec)
    assert ir.operations == []
    codes = [item.code for item in ir.ambiguities]
    assert "ref_target_missing" in codes
    assert ir.blocking_ambiguities


def test_security_schemes_and_scopes_are_captured() -> None:
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    schemes = {item.scheme_id: item for item in ir.service.auth_schemes}
    assert schemes["inventoryOAuth"].type == "oauth2"
    assert schemes["inventoryOAuth"].scopes == [
        "inventory.admin",
        "inventory.read",
        "inventory.write",
    ]
    assert schemes["inventoryAdminKey"].scopes == []


def test_document_level_security_applies_when_the_operation_declares_none() -> None:
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    operation = next(
        item for item in ir.operations if item.operation_id == "listWarehouseItems"
    )
    assert operation.authentication is not None
    assert operation.authentication.scopes == ["inventory.read"]
    assert operation.authentication.disabled is False
    record = next(
        item for item in operation.authentication.provenance if item.field == "scopes"
    )
    assert record.derivation is Derivation.SOURCE


def test_alternative_security_requirements_are_marked_as_an_over_approximation() -> None:
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    operation = next(
        item for item in ir.operations if item.operation_id == "purgeWarehouseItems"
    )
    assert operation.authentication is not None
    record = next(
        item for item in operation.authentication.provenance if item.field == "scopes"
    )
    assert record.derivation is Derivation.INFERRED
    assert record.confidence < 1.0
    # The alternatives are kept separately so least-privilege can compare them.
    assert len(operation.authentication.alternatives) == 3
    assert any(
        item.code == "security_requirement_alternatives" for item in ir.ambiguities
    )


def test_explicitly_disabled_security_is_distinguishable_from_absent(tmp_path: Path) -> None:
    """`security: []` disables authentication; policy synthesis must be able to see that."""
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Open, version: '1'}
        security:
          - apiKey: []
        paths:
          /open:
            get:
              operationId: getOpen
              security: []
              responses: {'200': {description: ok}}
          /closed:
            get:
              operationId: getClosed
              responses: {'200': {description: ok}}
        """,
    )
    operations = {item.operation_id: item for item in parse_openapi(spec).operations}
    open_auth = operations["getOpen"].authentication
    closed_auth = operations["getClosed"].authentication
    assert open_auth is not None and open_auth.disabled is True
    assert closed_auth is not None and closed_auth.disabled is False
    assert closed_auth.scheme_ids == ["apiKey"]


def test_no_security_anywhere_leaves_authentication_undetermined(tmp_path: Path) -> None:
    """Declaring nothing is not the same as declaring none, so the field stays null."""
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Silent, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              responses: {'200': {description: ok}}
        """,
    )
    assert parse_openapi(spec).operations[0].authentication is None


def test_operation_servers_override_the_service_endpoints(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Servers, version: '1'}
        servers: [{url: 'https://root.example.invalid'}]
        paths:
          /x:
            servers: [{url: 'https://path.example.invalid'}]
            get:
              operationId: getX
              servers: [{url: 'https://op.example.invalid'}]
              responses: {'200': {description: ok}}
          /y:
            servers: [{url: 'https://path.example.invalid'}]
            get:
              operationId: getY
              responses: {'200': {description: ok}}
        """,
    )
    operations = {item.operation_id: item for item in parse_openapi(spec).operations}
    assert [s.url for s in operations["getX"].servers] == ["https://op.example.invalid"]
    assert [s.url for s in operations["getY"].servers] == ["https://path.example.invalid"]


def test_typed_security_schemes_are_normalized() -> None:
    ir = parse_openapi(Path(INVENTORY_SERVICE))
    schemes = {item.scheme_id: item for item in ir.service.auth_schemes}
    assert schemes["inventoryAdminKey"].api_key_in == "header"
    assert schemes["inventoryAdminKey"].api_key_name == "X-Inventory-Admin-Key"
    assert schemes["inventoryOAuth"].scopes == [
        "inventory.admin",
        "inventory.read",
        "inventory.write",
    ]
    assert schemes["inventoryOAuth"].api_key_in is None


def test_duplicate_operation_ids_are_reported(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Dup, version: '1'}
        paths:
          /a:
            get:
              operationId: same
              responses: {'200': {description: ok}}
          /b:
            get:
              operationId: same
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    codes = [item.code for item in ir.ambiguities]
    assert "duplicate_operation_id" in codes
    assert ir.blocking_ambiguities


def test_operation_id_fallback_is_a_safe_identifier(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: NoId, version: '1'}
        paths:
          /customers/{id}:
            get:
              responses: {'200': {description: ok}}
        """,
    )
    operation = parse_openapi(spec).operations[0]
    assert operation.operation_id == "get__customers_id"


def test_swagger_2_is_translated_rather_than_rejected(tmp_path: Path) -> None:
    """The project instructions put Swagger 2 behind an adapter, not out of scope.

    Everything downstream reads the OpenAPI 3 shape, so the translation happens at load and no
    later stage learns that a second input format exists.
    """
    spec = _write(
        tmp_path,
        """
        swagger: '2.0'
        info: {title: Legacy, version: '1'}
        host: api.example.invalid
        basePath: /v1
        schemes: [https]
        paths:
          /things:
            get:
              operationId: listThings
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    assert [item.operation_id for item in ir.operations] == ["listThings"]
    assert ir.service.servers[0].url == "https://api.example.invalid/v1"


def test_a_version_that_is_neither_is_still_refused(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: '4.1'
        info: {title: Future, version: '1'}
        paths: {}
        """,
    )
    with pytest.raises(OpenApiIngestionError, match=r"only OpenAPI 3\.x"):
        parse_openapi(spec)


def test_non_mapping_document_is_rejected(tmp_path: Path) -> None:
    spec = _write(tmp_path, "- not: a document\n")
    with pytest.raises(OpenApiIngestionError, match="mapping at the root"):
        parse_openapi(spec)


def test_a_string_boolean_is_interpreted_not_coerced(tmp_path: Path) -> None:
    """`bool("false")` is `True`, and a real specification writes `"required": "false"`.

    Coercing it turned every optional parameter of a real API into a required one, which
    would have forced a caller to invent values the service never wanted.
    """
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: StringBools, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              parameters:
                - {in: query, name: needed, required: "true", schema: {type: string}}
                - {in: query, name: optional, required: "false", schema: {type: string}}
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    fields = {item.name: item.required for item in ir.operations[0].inputs}
    assert fields == {"needed": True, "optional": False}


def test_a_malformed_boolean_is_reported_rather_than_accepted_silently(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: StringBools, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              parameters:
                - {in: query, name: optional, required: "false", schema: {type: string}}
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    malformed = [item for item in ir.ambiguities if item.code == "malformed_boolean"]
    assert len(malformed) == 1
    assert "'false'" in malformed[0].detail
    assert malformed[0].blocking is False


def test_a_genuine_boolean_is_not_reported(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: RealBools, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              parameters:
                - {in: query, name: optional, required: false, schema: {type: string}}
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    assert not [item for item in ir.ambiguities if item.code == "malformed_boolean"]


def test_a_body_offered_in_several_media_types_is_one_argument(tmp_path: Path) -> None:
    """Offering JSON or XML is ordinary, and it made an operation unbuildable.

    A field per media type gave the operation two inputs both called `body`, which the tool
    could not compose. JSON is preferred because it is what an agent produces.
    """
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: MultiContent, version: '1'}
        paths:
          /things:
            post:
              operationId: createThing
              requestBody:
                content:
                  application/xml: {schema: {type: object}}
                  application/json: {schema: {type: object}}
              responses: {'201': {description: created}}
        """,
    )
    ir = parse_openapi(spec)
    body = [item for item in ir.operations[0].inputs if item.location is ParameterLocation.BODY]
    assert len(body) == 1
    assert body[0].media_type == "application/json"


def test_the_media_types_not_chosen_are_reported(tmp_path: Path) -> None:
    """A caller needing XML has to be able to see that the tool will send JSON."""
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: MultiContent, version: '1'}
        paths:
          /things:
            post:
              operationId: createThing
              requestBody:
                content:
                  application/json: {schema: {type: object}}
                  application/xml: {schema: {type: object}}
              responses: {'201': {description: created}}
        """,
    )
    ir = parse_openapi(spec)
    reported = [item for item in ir.ambiguities if item.code == "multiple_request_media_types"]
    assert reported and not reported[0].blocking
    assert "application/xml" in reported[0].detail


ASYNC_SERVICE = """openapi: 3.0.3
info:
  title: Batch Service
  version: 1.0.0
  description: Runs long jobs over customer records.
  termsOfService: https://batch.example.invalid/terms
servers: [{url: https://batch.example.invalid}]
paths:
  /exports:
    post:
      operationId: startExport
      summary: Start an export
      responses:
        '202':
          description: Accepted
          headers:
            Location: {description: Where to poll, schema: {type: string}}
  /reports:
    post:
      operationId: startReport
      summary: Start a report
      responses:
        '202': {description: Accepted with nowhere named}
  /instant:
    post:
      operationId: doItNow
      summary: Do it now
      responses:
        '200': {description: done, content: {application/json: {schema: {type: object}}}}
"""


def _async_ir(tmp_path: Path) -> ApiSemanticIR:
    spec = tmp_path / "batch.yaml"
    spec.write_text(ASYNC_SERVICE, encoding="utf-8")
    return parse_openapi(spec)


def test_service_level_context_is_kept_rather_than_swept(tmp_path: Path) -> None:
    """Both were reported as unread keys on a real specification."""
    ir = _async_ir(tmp_path)

    assert ir.service.description == "Runs long jobs over customer records."
    assert ir.service.terms_of_service == "https://batch.example.invalid/terms"
    assert not [item for item in ir.ambiguities if "info" in (item.source_pointer or "")]


def test_an_accepted_response_marks_the_operation_asynchronous(tmp_path: Path) -> None:
    """202 says the request was taken, not that the work was done."""
    ir = _async_ir(tmp_path)
    started = next(item for item in ir.operations if item.operation_id == "startExport")

    assert started.async_job is not None
    assert started.async_job.status == "202"
    assert started.async_job.poll_header == "Location"


def test_acceptance_without_a_poll_target_is_reported_as_such(tmp_path: Path) -> None:
    """Inventing a polling convention would be a promise the service never made."""
    ir = _async_ir(tmp_path)
    started = next(item for item in ir.operations if item.operation_id == "startReport")

    assert started.async_job is not None
    assert started.async_job.poll_header is None


def test_a_synchronous_operation_is_not_marked_asynchronous(tmp_path: Path) -> None:
    ir = _async_ir(tmp_path)
    immediate = next(item for item in ir.operations if item.operation_id == "doItNow")

    assert immediate.async_job is None


def test_the_poll_header_is_inferred_not_asserted(tmp_path: Path) -> None:
    """The status is what the document said; reading it as a poll target is a guess."""
    ir = _async_ir(tmp_path)
    started = next(item for item in ir.operations if item.operation_id == "startExport")

    assert started.async_job is not None
    derivations = {item.field: item.derivation for item in started.async_job.provenance}
    assert derivations["status"] is Derivation.SOURCE
    assert derivations["poll_header"] is Derivation.INFERRED


def test_a_read_whose_summary_says_it_deletes_is_raised_for_review(tmp_path: Path) -> None:
    """The wording people actually use, which the adapter used to read straight past.

    Summaries are written in the third person, so a GET summarised "Deletes a record" was
    invisible while `deleteRecord` was caught. The asymmetry is what makes this worth
    blocking: a false positive costs one review, a false negative leaves an agent destroying
    data through a tool the surface presents as safe.
    """
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        textwrap.dedent(
            """
            openapi: 3.0.3
            info: {title: Records, version: '1'}
            paths:
              /records/{id}:
                get:
                  operationId: getRecord
                  summary: Deletes a record permanently
                  parameters: [{in: path, name: id, required: true, schema: {type: string}}]
                  responses: {'200': {description: ok}}
            """
        ),
        encoding="utf-8",
    )

    ir = parse_openapi(spec)

    conflict = next(
        item for item in ir.ambiguities if item.code == "side_effect_language_conflict"
    )
    assert conflict.blocking
    assert "delete" in conflict.detail


def test_a_read_listing_deleted_records_is_not_raised(tmp_path: Path) -> None:
    """`deleted` is an adjective on a row, and flagging it would be the false alarm that
    teaches everybody to ignore this check."""
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        textwrap.dedent(
            """
            openapi: 3.0.3
            info: {title: Records, version: '1'}
            paths:
              /records/deleted:
                get:
                  operationId: listDeletedRecords
                  summary: Lists records that were deleted
                  responses: {'200': {description: ok}}
            """
        ),
        encoding="utf-8",
    )

    ir = parse_openapi(spec)

    assert not [
        item for item in ir.ambiguities if item.code == "side_effect_language_conflict"
    ]
