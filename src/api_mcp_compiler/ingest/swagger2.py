"""Translating Swagger 2.0 into the OpenAPI 3 shape the rest of the compiler reads.

The project instructions put Swagger 2 behind an adapter rather than in the parser, and that
is the right split: the two formats describe the same things differently, so one translation
here keeps every stage downstream — planning, policy, generation, evaluation — unaware that
there are two input formats at all.

The translation is total for the constructs Swagger 2 has and OpenAPI 3 also has, and honest
about the rest. Where 2.0 can express something 3.0 cannot, or where a document is ambiguous
about what it meant, that is an `Ambiguity` rather than a guess, exactly as anywhere else in
this compiler.

Nothing here reaches the network. A `$ref` is rewritten, never followed.
"""

from __future__ import annotations

import copy
from typing import Any

from api_mcp_compiler.models import Ambiguity

#: Swagger 2 keeps schemas here; OpenAPI 3 keeps them under components.
_DEFINITIONS_PREFIX = "#/definitions/"
_COMPONENTS_PREFIX = "#/components/schemas/"

#: Parameter keywords that describe the value rather than the parameter, and so belong in the
#: schema OpenAPI 3 nests underneath it.
_SCHEMA_KEYS = frozenset(
    {
        "type", "format", "items", "enum", "default", "maximum", "exclusiveMaximum",
        "minimum", "exclusiveMinimum", "maxLength", "minLength", "pattern", "maxItems",
        "minItems", "uniqueItems", "multipleOf",
    }
)

#: How Swagger 2 said an array was serialised, and the OpenAPI 3 pair that means the same.
_COLLECTION_FORMATS = {
    "csv": ("form", False),
    "ssv": ("spaceDelimited", False),
    "pipes": ("pipeDelimited", False),
    "multi": ("form", True),
}


def is_swagger2(document: Any) -> bool:
    """Whether a loaded document declares itself Swagger 2."""
    return isinstance(document, dict) and str(document.get("swagger", "")).startswith("2")


def _rewrite_refs(node: Any) -> Any:
    """Point every schema reference at where OpenAPI 3 keeps schemas."""
    if isinstance(node, dict):
        return {
            key: (
                value.replace(_DEFINITIONS_PREFIX, _COMPONENTS_PREFIX)
                if key == "$ref" and isinstance(value, str)
                else _rewrite_refs(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_rewrite_refs(item) for item in node]
    return node


def _file_to_binary(node: Any) -> Any:
    """Translate `type: file`, which OpenAPI 3 expresses as a binary string."""
    if isinstance(node, dict):
        if node.get("type") == "file":
            rest = {key: value for key, value in node.items() if key != "type"}
            return {"type": "string", "format": "binary", **_file_to_binary(rest)}
        return {key: _file_to_binary(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_file_to_binary(item) for item in node]
    return node


def _servers(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Assemble the server list from the three fields Swagger 2 split it across."""
    host = document.get("host")
    base_path = document.get("basePath") or ""
    schemes = document.get("schemes") or (["https"] if host else [])
    if not host:
        # A relative basePath is legal and means "the host that served this document", which
        # is a fact about deployment rather than something to invent a hostname for.
        return [{"url": base_path or "/"}] if base_path else []
    return [{"url": f"{scheme}://{host}{base_path}"} for scheme in schemes]


def _parameter(
    parameter: dict[str, Any], ambiguities: list[Ambiguity], where: str
) -> dict[str, Any]:
    """Translate one non-body parameter, moving its type information into a schema."""
    converted = {
        key: value
        for key, value in parameter.items()
        if key not in _SCHEMA_KEYS and key != "collectionFormat"
    }
    schema = {key: value for key, value in parameter.items() if key in _SCHEMA_KEYS}
    if schema:
        converted["schema"] = _file_to_binary(schema)

    collection_format = parameter.get("collectionFormat")
    if collection_format:
        pair = _COLLECTION_FORMATS.get(str(collection_format))
        if pair is None:
            ambiguities.append(
                Ambiguity(
                    code="unsupported_collection_format",
                    field=f"{where}.{parameter.get('name', 'parameter')}.collectionFormat",
                    source_pointer=where,
                    detail=(
                        f"collectionFormat {collection_format!r} has no OpenAPI 3 equivalent, "
                        "so how this array is serialised is left unstated rather than guessed."
                    ),
                    blocking=False,
                )
            )
        else:
            converted["style"], converted["explode"] = pair
    return converted


def _request_body(
    body: list[dict[str, Any]],
    form: list[dict[str, Any]],
    consumes: list[str],
    ambiguities: list[Ambiguity],
    where: str,
) -> dict[str, Any] | None:
    """Turn body and formData parameters into the request body OpenAPI 3 expects."""
    if body and form:
        ambiguities.append(
            Ambiguity(
                code="conflicting_request_body",
                field=f"{where}.requestBody",
                source_pointer=where,
                detail=(
                    "The operation declares both a body parameter and formData parameters, "
                    "which cannot both be the request body. The body parameter is used and "
                    "the form fields are dropped; a reviewer should confirm which was meant."
                ),
                blocking=True,
            )
        )
    if body:
        first = body[0]
        media_types = consumes or ["application/json"]
        schema = _file_to_binary(first.get("schema") or {})
        content = {media: {"schema": schema} for media in media_types}
        request: dict[str, Any] = {"content": content}
        if first.get("required"):
            request["required"] = True
        if first.get("description"):
            request["description"] = first["description"]
        return request
    if form:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for item in form:
            name = str(item.get("name", ""))
            if not name:
                continue
            properties[name] = _file_to_binary(
                {key: value for key, value in item.items() if key in _SCHEMA_KEYS}
            )
            if item.get("required"):
                required.append(name)
        # A file field means the form has to be multipart; otherwise it is url-encoded.
        binary = any(item.get("type") == "file" for item in form)
        media = "multipart/form-data" if binary else "application/x-www-form-urlencoded"
        form_schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            form_schema["required"] = sorted(required)
        return {"content": {media: {"schema": form_schema}}}
    return None


def _responses(
    responses: dict[str, Any], produces: list[str], ambiguities: list[Ambiguity], where: str
) -> dict[str, Any]:
    """Move each response schema under the media types the operation produces."""
    converted: dict[str, Any] = {}
    for status, response in responses.items():
        if not isinstance(response, dict):
            continue
        entry = {key: value for key, value in response.items() if key != "schema"}
        schema = response.get("schema")
        if schema is not None:
            media_types = produces or ["application/json"]
            entry["content"] = {
                media: {"schema": _file_to_binary(schema)} for media in media_types
            }
        entry.setdefault("description", "")
        converted[str(status)] = entry
    return converted


def _security_schemes(
    definitions: dict[str, Any], ambiguities: list[Ambiguity]
) -> dict[str, Any]:
    """Translate securityDefinitions, whose oauth2 shape differs most between the versions."""
    schemes: dict[str, Any] = {}
    for name, definition in definitions.items():
        if not isinstance(definition, dict):
            continue
        kind = definition.get("type")
        if kind == "basic":
            schemes[name] = {"type": "http", "scheme": "basic"}
        elif kind == "apiKey":
            schemes[name] = {
                key: value
                for key, value in definition.items()
                if key in {"type", "name", "in", "description"}
            }
        elif kind == "oauth2":
            flow_name = {
                "implicit": "implicit",
                "password": "password",
                "application": "clientCredentials",
                "accessCode": "authorizationCode",
            }.get(str(definition.get("flow")))
            if flow_name is None:
                ambiguities.append(
                    Ambiguity(
                        code="unsupported_oauth_flow",
                        field=f"securityDefinitions.{name}.flow",
                        source_pointer="#/securityDefinitions",
                        detail=(
                            f"OAuth2 flow {definition.get('flow')!r} has no OpenAPI 3 "
                            "equivalent, so the scheme is left undeclared rather than guessed."
                        ),
                        blocking=False,
                    )
                )
                continue
            flow: dict[str, Any] = {"scopes": definition.get("scopes") or {}}
            if definition.get("authorizationUrl"):
                flow["authorizationUrl"] = definition["authorizationUrl"]
            if definition.get("tokenUrl"):
                flow["tokenUrl"] = definition["tokenUrl"]
            schemes[name] = {"type": "oauth2", "flows": {flow_name: flow}}
        else:
            ambiguities.append(
                Ambiguity(
                    code="unsupported_security_scheme",
                    field=f"securityDefinitions.{name}.type",
                    source_pointer="#/securityDefinitions",
                    detail=(
                        f"Security scheme type {kind!r} is not one Swagger 2 defines, so it "
                        "was not translated."
                    ),
                    blocking=False,
                )
            )
    return schemes


def to_openapi3(document: dict[str, Any]) -> tuple[dict[str, Any], list[Ambiguity]]:
    """Translate a Swagger 2 document into the OpenAPI 3 shape, reporting what did not fit."""
    ambiguities: list[Ambiguity] = []
    source = copy.deepcopy(document)
    global_consumes = list(source.get("consumes") or [])
    global_produces = list(source.get("produces") or [])

    converted: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": source.get("info") or {},
        "paths": {},
    }
    servers = _servers(source)
    if servers:
        converted["servers"] = servers
    for carried in ("tags", "externalDocs", "security"):
        if carried in source:
            converted[carried] = source[carried]

    components: dict[str, Any] = {}
    if source.get("definitions"):
        components["schemas"] = _file_to_binary(_rewrite_refs(source["definitions"]))
    schemes = _security_schemes(source.get("securityDefinitions") or {}, ambiguities)
    if schemes:
        components["securitySchemes"] = schemes
    if components:
        converted["components"] = components

    for route, path_item in (source.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        converted_item: dict[str, Any] = {}
        for method, operation in path_item.items():
            where = f"#/paths/{route}/{method}"
            if method == "parameters" and isinstance(operation, list):
                converted_item["parameters"] = [
                    _parameter(_rewrite_refs(item), ambiguities, where)
                    for item in operation
                    if isinstance(item, dict) and item.get("in") not in {"body", "formData"}
                ]
                continue
            if not isinstance(operation, dict):
                continue
            parameters = [
                item for item in (operation.get("parameters") or []) if isinstance(item, dict)
            ]
            body = [item for item in parameters if item.get("in") == "body"]
            form = [item for item in parameters if item.get("in") == "formData"]
            other = [item for item in parameters if item.get("in") not in {"body", "formData"}]

            entry = {
                key: value
                for key, value in operation.items()
                if key not in {"parameters", "responses", "consumes", "produces", "schemes"}
            }
            entry["parameters"] = [
                _parameter(_rewrite_refs(item), ambiguities, where) for item in other
            ]
            request = _request_body(
                _rewrite_refs(body),
                _rewrite_refs(form),
                list(operation.get("consumes") or global_consumes),
                ambiguities,
                where,
            )
            if request is not None:
                entry["requestBody"] = request
            entry["responses"] = _responses(
                _rewrite_refs(operation.get("responses") or {}),
                list(operation.get("produces") or global_produces),
                ambiguities,
                where,
            )
            converted_item[method] = entry
        converted["paths"][route] = converted_item

    return converted, ambiguities
