"""Swagger 2.0 adapter tests.

The project instructions put Swagger 2 behind an adapter rather than in the parser, so these
check the translation itself. Everything downstream is tested against OpenAPI 3 already and
must stay unaware that a second input format exists.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.ingest.swagger2 import is_swagger2, to_openapi3


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "spec.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_a_swagger2_document_is_recognised() -> None:
    assert is_swagger2({"swagger": "2.0"})
    assert not is_swagger2({"openapi": "3.0.3"})
    assert not is_swagger2("not a document")


def test_the_three_fields_that_became_servers_are_reassembled(tmp_path: Path) -> None:
    """Swagger 2 split a server URL across host, basePath and schemes."""
    spec = _write(
        tmp_path,
        """
        swagger: '2.0'
        info: {title: Split, version: '1'}
        host: api.example.invalid
        basePath: /v2
        schemes: [https, http]
        paths:
          /things:
            get:
              operationId: listThings
              responses: {'200': {description: ok}}
        """,
    )
    ir = parse_openapi(spec)
    assert [item.url for item in ir.service.servers] == [
        "https://api.example.invalid/v2",
        "http://api.example.invalid/v2",
    ]


def test_a_body_parameter_becomes_a_request_body(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        swagger: '2.0'
        info: {title: Body, version: '1'}
        paths:
          /things:
            post:
              operationId: createThing
              consumes: [application/json]
              parameters:
                - {in: body, name: body, required: true, schema: {$ref: '#/definitions/Thing'}}
              responses: {'201': {description: created}}
        definitions:
          Thing: {type: object, properties: {name: {type: string}}}
        """,
    )
    ir = parse_openapi(spec)
    body = [item for item in ir.operations[0].inputs if item.location.value == "body"]
    assert len(body) == 1
    assert body[0].media_type == "application/json"


def test_a_definition_reference_is_rewritten_not_followed(tmp_path: Path) -> None:
    """`#/definitions/X` is where 2.0 kept schemas; nothing is fetched to resolve it."""
    document = {
        "swagger": "2.0",
        "info": {"title": "Refs", "version": "1"},
        "paths": {},
        "definitions": {"Thing": {"type": "object"}},
    }
    converted, _ = to_openapi3(document)
    assert "Thing" in converted["components"]["schemas"]
    assert "definitions" not in converted


def test_form_data_becomes_a_request_body_of_the_right_media_type() -> None:
    document = {
        "swagger": "2.0",
        "info": {"title": "Forms", "version": "1"},
        "paths": {
            "/upload": {
                "post": {
                    "operationId": "upload",
                    "parameters": [
                        {"in": "formData", "name": "note", "type": "string"},
                        {"in": "formData", "name": "file", "type": "file"},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    converted, _ = to_openapi3(document)
    content = converted["paths"]["/upload"]["post"]["requestBody"]["content"]
    assert "multipart/form-data" in content, "a file field means the form must be multipart"
    properties = content["multipart/form-data"]["schema"]["properties"]
    assert properties["file"] == {"type": "string", "format": "binary"}


def test_an_oauth2_flow_is_renamed_to_its_openapi3_name() -> None:
    document = {
        "swagger": "2.0",
        "info": {"title": "Auth", "version": "1"},
        "paths": {},
        "securityDefinitions": {
            "oauth": {
                "type": "oauth2",
                "flow": "accessCode",
                "authorizationUrl": "https://example.invalid/authorize",
                "tokenUrl": "https://example.invalid/token",
                "scopes": {"read": "read things"},
            }
        },
    }
    converted, ambiguities = to_openapi3(document)
    flows = converted["components"]["securitySchemes"]["oauth"]["flows"]
    assert "authorizationCode" in flows
    assert not ambiguities


def test_a_flow_with_no_equivalent_is_reported_rather_than_guessed() -> None:
    document = {
        "swagger": "2.0",
        "info": {"title": "Auth", "version": "1"},
        "paths": {},
        "securityDefinitions": {"odd": {"type": "oauth2", "flow": "invented"}},
    }
    converted, ambiguities = to_openapi3(document)
    assert "odd" not in converted.get("components", {}).get("securitySchemes", {})
    assert [item.code for item in ambiguities] == ["unsupported_oauth_flow"]


def test_a_body_and_form_together_are_refused_rather_than_merged() -> None:
    """They cannot both be the request body, and choosing silently would hide the conflict."""
    document = {
        "swagger": "2.0",
        "info": {"title": "Conflict", "version": "1"},
        "paths": {
            "/x": {
                "post": {
                    "operationId": "x",
                    "parameters": [
                        {"in": "body", "name": "body", "schema": {"type": "object"}},
                        {"in": "formData", "name": "field", "type": "string"},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    _, ambiguities = to_openapi3(document)
    conflict = [item for item in ambiguities if item.code == "conflicting_request_body"]
    assert conflict and conflict[0].blocking


def test_collection_format_multi_becomes_explode(tmp_path: Path) -> None:
    document = {
        "swagger": "2.0",
        "info": {"title": "Arrays", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "operationId": "x",
                    "parameters": [
                        {
                            "in": "query",
                            "name": "status",
                            "type": "array",
                            "items": {"type": "string"},
                            "collectionFormat": "multi",
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    converted, _ = to_openapi3(document)
    parameter = converted["paths"]["/x"]["get"]["parameters"][0]
    assert parameter["style"] == "form" and parameter["explode"] is True
    assert parameter["schema"]["type"] == "array"
