"""OpenAPI 3.x ingestion adapter.

Covers servers at every level, security schemes and per-operation requirements, parameters
including `content` and serialization style, request bodies, responses with headers and
examples, and reference resolution under an explicit policy. Every informative field
carries provenance, and every key the adapter does not consume is reported by the
completeness sweep, so an unsupported construct is always distinguishable from an absent
one.

Not done here: remote reference fetching, which is refused by design rather than deferred;
async job and dependency inference; and overlays, which belong to the planner. Swagger 2
arrives through its own adapter and is translated before this parser sees it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api_mcp_compiler.ingest.coverage import ConsumptionLedger
from api_mcp_compiler.ingest.documents import load_document
from api_mcp_compiler.ingest.refs import RefPolicy, RefResolver
from api_mcp_compiler.ingest.swagger2 import is_swagger2, to_openapi3
from api_mcp_compiler.models import (
    Ambiguity,
    ApiSemanticIR,
    AsyncJobIR,
    AuthRequirementIR,
    AuthSchemeIR,
    AuthSchemeType,
    Derivation,
    ExampleIR,
    FaultIR,
    FieldIR,
    HeaderIR,
    Idempotency,
    OperationIR,
    PaginationIR,
    PaginationStyle,
    ParameterLocation,
    ParameterStyle,
    Protocol,
    Provenance,
    ResponseIR,
    SecurityRequirementIR,
    ServerIR,
    ServiceIR,
    SideEffectClass,
    SourceFormat,
)
from api_mcp_compiler.provenance import (
    openapi_pointer,
    operation_identifier,
    slug,
)

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
SUPPORTED_MAJOR = "3."
KNOWN_MINORS = ("3.0", "3.1", "3.2")

# Decision: side effect and idempotency are proposals derived from HTTP
# method semantics, never assertions. Confidence stays below 1.0 so that the contract
# forces `inferred` provenance and the human approval gate can see what was guessed.
# DELETE is classified destructive rather than write because the safe failure mode is to
# demand confirmation for an operation that turns out to be reversible.
_METHOD_SIDE_EFFECT: dict[str, tuple[SideEffectClass, float]] = {
    "get": (SideEffectClass.READ, 0.95),
    "head": (SideEffectClass.READ, 0.95),
    "options": (SideEffectClass.READ, 0.95),
    "trace": (SideEffectClass.READ, 0.9),
    "post": (SideEffectClass.WRITE, 0.6),
    "put": (SideEffectClass.WRITE, 0.6),
    "patch": (SideEffectClass.WRITE, 0.6),
    "delete": (SideEffectClass.DESTRUCTIVE, 0.8),
}

# RFC 9110 idempotency. PATCH is absent deliberately: it is idempotent only when the patch
# document says so, and guessing either way would be wrong for real specifications.
_METHOD_IDEMPOTENCY: dict[str, tuple[Idempotency, float]] = {
    "get": (Idempotency.IDEMPOTENT, 0.95),
    "head": (Idempotency.IDEMPOTENT, 0.95),
    "options": (Idempotency.IDEMPOTENT, 0.95),
    "trace": (Idempotency.IDEMPOTENT, 0.9),
    "put": (Idempotency.IDEMPOTENT, 0.85),
    "delete": (Idempotency.IDEMPOTENT, 0.85),
    "post": (Idempotency.NON_IDEMPOTENT, 0.8),
}

_LOCATIONS = {
    "path": ParameterLocation.PATH,
    "query": ParameterLocation.QUERY,
    "header": ParameterLocation.HEADER,
    "cookie": ParameterLocation.COOKIE,
}

# Whole-token destructive verbs. Matching whole tokens rather than substrings keeps
# `listDeletedItems` from reading as a delete, while `purgeItem` still does.
_DESTRUCTIVE_TOKENS = frozenset(
    {"delete", "purge", "destroy", "erase", "wipe", "drop", "remove", "revoke", "terminate"}
)

_CURSOR_PARAMS = frozenset(
    {"cursor", "nextcursor", "pagetoken", "continuation", "continuationtoken", "startcursor"}
)
_PAGE_PARAMS = frozenset({"page", "pagenumber", "pagenum"})
_SIZE_PARAMS = frozenset({"perpage", "pagesize", "limit", "size", "maxresults"})
_OFFSET_PARAMS = frozenset({"offset", "skip"})
_CURSOR_FIELDS = frozenset(
    {"nextcursor", "nextpagetoken", "nexttoken", "continuationtoken", "cursor"}
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_WORD = re.compile(r"[^A-Za-z0-9]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

_ROOT_KEYS = frozenset({"openapi", "info", "servers", "paths", "components", "security"})
_INFO_KEYS = frozenset({"title", "version", "description", "termsOfService"})
_PATH_ITEM_KEYS = frozenset({"parameters", "servers", *HTTP_METHODS})
_OPERATION_KEYS = frozenset(
    {
        "operationId",
        "summary",
        "description",
        "parameters",
        "requestBody",
        "responses",
        "security",
        "deprecated",
        "servers",
        "tags",
    }
)
_PARAMETER_KEYS = frozenset(
    {
        "name",
        "in",
        "required",
        "description",
        "schema",
        "content",
        "style",
        "explode",
        "allowReserved",
        "deprecated",
        "example",
        "examples",
    }
)
_REQUEST_BODY_KEYS = frozenset({"description", "content", "required"})
_RESPONSE_KEYS = frozenset({"description", "content", "headers"})
_HEADER_KEYS = frozenset({"description", "required", "deprecated", "schema", "example", "examples"})
_SECURITY_SCHEME_KEYS = frozenset(
    {"type", "description", "name", "in", "scheme", "bearerFormat", "flows", "openIdConnectUrl"}
)
_SERVER_KEYS = frozenset({"url", "description"})
_MEDIA_TYPE_KEYS = frozenset({"schema", "example", "examples"})

_AUTH_TYPES = {member.value: member for member in AuthSchemeType}


class OpenApiIngestionError(ValueError):
    """Raised when a document cannot be ingested as OpenAPI 3.x."""


@dataclass
class _Context:
    """Shared state for one compile: the document, the resolver and the sweeps."""

    doc: dict[str, Any]
    resolver: RefResolver
    ledger: ConsumptionLedger = field(default_factory=ConsumptionLedger)
    ambiguities: list[Ambiguity] = field(default_factory=list)


def _load(path: Path) -> tuple[dict[str, Any], str, list[Ambiguity]]:
    """Read a specification, translating Swagger 2 before anything downstream sees it.

    The digest is of the bytes on disk, not of the translation, so a result stays tied to the
    document a person actually has.
    """
    data, digest = load_document(path)
    if not isinstance(data, dict):
        raise OpenApiIngestionError(f"{path}: OpenAPI document must be a mapping at the root")
    if is_swagger2(data):
        translated, translation_ambiguities = to_openapi3(data)
        return translated, digest, translation_ambiguities
    return data, digest, []


def _as_mapping(value: Any) -> dict[str, Any]:
    """Coerce a value to a mapping, treating anything else as absent."""
    return value if isinstance(value, dict) else {}


def _as_sequence(value: Any) -> list[Any]:
    """Coerce a value to a list, treating anything else as absent."""
    return value if isinstance(value, list) else []


def _json_bool(value: Any) -> tuple[bool, bool]:
    """Interpret a JSON boolean strictly, reporting whether the source was malformed.

    `bool()` must never be used on a source value. OpenAPI declares these fields as booleans,
    but real specifications write `"false"` as a string, and `bool("false")` is `True`. That
    single coercion turned every optional parameter of a real API into a required one, which
    would have forced a caller to invent values the service never wanted.

    Returns the interpreted value and whether the source needed interpreting.
    """
    if isinstance(value, bool):
        return value, False
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true", True
    return False, True


def _optional_str(value: Any) -> str | None:
    """Return a string value, or None when the source did not provide one."""
    return value if isinstance(value, str) else None


def _word_tokens(text: str) -> set[str]:
    """Split an identifier or sentence into lowercase whole words."""
    spaced = _CAMEL_BOUNDARY.sub(" ", text)
    return {piece.lower() for piece in _NON_WORD.split(spaced) if piece}


def _normalize_name(text: str) -> str:
    """Reduce a parameter or field name to letters and digits for pattern matching."""
    return _NON_ALNUM.sub("", _CAMEL_BOUNDARY.sub(" ", text).lower())


def _source(field_name: str, tokens: tuple[str, ...], rule: str) -> Provenance:
    """Build a source-derived provenance record."""
    return Provenance(
        field=field_name,
        source_pointer=openapi_pointer(*tokens),
        derivation=Derivation.SOURCE,
        rule=rule,
    )


def _examples(container: dict[str, Any], base: tuple[str, ...]) -> list[ExampleIR]:
    """Extract the singular `example` and the `examples` map from one container."""
    found: list[ExampleIR] = []
    if "example" in container:
        found.append(
            ExampleIR(
                value=container["example"],
                provenance=[_source("value", (*base, "example"), "openapi.example")],
            )
        )
    for name, entry in _as_mapping(container.get("examples")).items():
        mapping = _as_mapping(entry)
        pointer = (*base, "examples", name)
        records = [_source("name", pointer, "openapi.examples.key")]
        summary = _optional_str(mapping.get("summary"))
        if summary is not None:
            records.append(_source("summary", (*pointer, "summary"), "openapi.examples.summary"))
        external = _optional_str(mapping.get("externalValue"))
        if external is not None:
            records.append(
                _source("external_value", (*pointer, "externalValue"), "openapi.examples.external")
            )
        if "value" in mapping:
            records.append(_source("value", (*pointer, "value"), "openapi.examples.value"))
        found.append(
            ExampleIR(
                name=name,
                summary=summary,
                value=mapping.get("value"),
                external_value=external,
                provenance=records,
            )
        )
    return found


def _headers(container: dict[str, Any], base: tuple[str, ...], ctx: _Context) -> list[HeaderIR]:
    """Extract response headers.

    Headers carry the clearest pagination and async-job signals a specification offers, so
    discarding them removes evidence nothing else replaces.
    """
    found: list[HeaderIR] = []
    for name, entry in _as_mapping(container.get("headers")).items():
        mapping = _as_mapping(entry)
        pointer = (*base, "headers", name)
        ctx.ledger.consume(pointer, mapping, _HEADER_KEYS, "response header")
        records = [_source("name", pointer, "openapi.responses.headers.key")]
        description = _optional_str(mapping.get("description"))
        if description is not None:
            records.append(
                _source("description", (*pointer, "description"), "openapi.header.description")
            )
        required, _ = (
            _json_bool(mapping.get("required", False))
            if "required" in mapping
            else (False, False)
        )
        records.append(
            Provenance(
                field="required",
                source_pointer=openapi_pointer(*pointer, "required")
                if "required" in mapping
                else openapi_pointer(*pointer),
                derivation=Derivation.SOURCE if "required" in mapping else Derivation.DEFAULT,
                rule="openapi.header.required",
            )
        )
        deprecated, _ = (
            _json_bool(mapping.get("deprecated", False))
            if "deprecated" in mapping
            else (False, False)
        )
        records.append(
            Provenance(
                field="deprecated",
                source_pointer=openapi_pointer(*pointer, "deprecated")
                if "deprecated" in mapping
                else openapi_pointer(*pointer),
                derivation=Derivation.SOURCE if "deprecated" in mapping else Derivation.DEFAULT,
                rule="openapi.header.deprecated",
            )
        )
        type_schema = mapping.get("schema")
        if isinstance(type_schema, dict):
            records.append(_source("type_schema", (*pointer, "schema"), "openapi.header.schema"))
        else:
            type_schema = None
        found.append(
            HeaderIR(
                name=name,
                description=description,
                required=required,
                deprecated=deprecated,
                type_schema=type_schema,
                provenance=records,
            )
        )
    return found


def _parameter_style(
    entry: dict[str, Any], pointer: tuple[str, ...], ctx: _Context
) -> tuple[ParameterStyle | None, Provenance | None]:
    """Read the serialization style, reporting a value outside the closed set."""
    raw = _optional_str(entry.get("style"))
    if raw is None:
        return None, None
    try:
        style = ParameterStyle(raw)
    except ValueError:
        ctx.ambiguities.append(
            Ambiguity(
                code="unknown_parameter_style",
                field="inputs.style",
                source_pointer=openapi_pointer(*pointer, "style"),
                detail=(
                    f"Style {raw!r} is not one of the styles the OpenAPI specification defines, "
                    "so it was not carried into the IR."
                ),
                blocking=False,
            )
        )
        return None, None
    return style, _source("style", (*pointer, "style"), "openapi.parameter.style")


def _parameter_field(
    entry: dict[str, Any], pointer: tuple[str, ...], ctx: _Context
) -> FieldIR | None:
    """Convert one OpenAPI parameter object into a `FieldIR`."""
    ctx.ledger.consume(pointer, entry, _PARAMETER_KEYS, "parameter")
    name = _optional_str(entry.get("name"))
    location = _LOCATIONS.get(str(entry.get("in", "")))
    if name is None or location is None:
        return None

    records = [
        _source("name", (*pointer, "name"), "openapi.parameter.name"),
        _source("location", (*pointer, "in"), "openapi.parameter.in"),
    ]
    # `required` is a bool, so `False` still carries information and always needs a record.
    declared_required = "required" in entry
    required, malformed = (
        _json_bool(entry.get("required", False)) if declared_required else (False, False)
    )
    if malformed:
        ctx.ambiguities.append(
            Ambiguity(
                code="malformed_boolean",
                field=f"inputs.{name}.required",
                source_pointer=openapi_pointer(*pointer, "required"),
                detail=(
                    f"`required` on parameter {name!r} is {entry.get('required')!r} rather than "
                    f"a JSON boolean; it was interpreted as {required}."
                ),
                blocking=False,
            )
        )
    records.append(
        Provenance(
            field="required",
            source_pointer=openapi_pointer(*pointer, "required")
            if declared_required
            else openapi_pointer(*pointer),
            derivation=Derivation.SOURCE if declared_required else Derivation.DEFAULT,
            rule="openapi.parameter.required"
            if declared_required
            else "openapi.parameter.required.absent_defaults_false",
        )
    )

    description = _optional_str(entry.get("description"))
    if description is not None:
        records.append(
            _source("description", (*pointer, "description"), "openapi.parameter.description")
        )

    media_type: str | None = None
    type_schema = entry.get("schema")
    if isinstance(type_schema, dict):
        records.append(_source("type_schema", (*pointer, "schema"), "openapi.parameter.schema"))
    else:
        type_schema = None
        content = _as_mapping(entry.get("content"))
        if content:
            # A parameter may carry `content` instead of `schema` for structured values.
            media_type = next(iter(content))
            records.append(
                _source("media_type", (*pointer, "content"), "openapi.parameter.content.key")
            )
            candidate = _as_mapping(content[media_type]).get("schema")
            ctx.ledger.consume(
                (*pointer, "content", media_type),
                _as_mapping(content[media_type]),
                _MEDIA_TYPE_KEYS,
                "parameter media type",
            )
            if isinstance(candidate, dict):
                type_schema = candidate
                records.append(
                    _source(
                        "type_schema",
                        (*pointer, "content", media_type, "schema"),
                        "openapi.parameter.content.schema",
                    )
                )
            if len(content) > 1:
                ctx.ambiguities.append(
                    Ambiguity(
                        code="parameter_content_multiple_media_types",
                        field=f"inputs.{name}.type_schema",
                        source_pointer=openapi_pointer(*pointer, "content"),
                        detail=(
                            f"Parameter {name!r} declares {len(content)} media types; only "
                            f"{media_type!r} was carried into the IR."
                        ),
                        blocking=False,
                    )
                )

    style, style_record = _parameter_style(entry, pointer, ctx)
    if style_record is not None:
        records.append(style_record)

    explode = entry.get("explode")
    if isinstance(explode, bool):
        records.append(_source("explode", (*pointer, "explode"), "openapi.parameter.explode"))
    else:
        explode = None
    allow_reserved = entry.get("allowReserved")
    if isinstance(allow_reserved, bool):
        records.append(
            _source(
                "allow_reserved", (*pointer, "allowReserved"), "openapi.parameter.allowReserved"
            )
        )
    else:
        allow_reserved = None

    deprecated, _ = (
        _json_bool(entry.get("deprecated", False)) if "deprecated" in entry else (False, False)
    )
    records.append(
        Provenance(
            field="deprecated",
            source_pointer=openapi_pointer(*pointer, "deprecated")
            if "deprecated" in entry
            else openapi_pointer(*pointer),
            derivation=Derivation.SOURCE if "deprecated" in entry else Derivation.DEFAULT,
            rule="openapi.parameter.deprecated",
        )
    )

    return FieldIR(
        name=name,
        location=location,
        required=required,
        description=description,
        media_type=media_type,
        type_schema=type_schema,
        style=style,
        explode=explode,
        allow_reserved=allow_reserved,
        deprecated=deprecated,
        examples=_examples(entry, pointer),
        provenance=records,
    )


def _merged_parameters(
    path_item: dict[str, Any],
    operation: dict[str, Any],
    route: str,
    method: str,
    ctx: _Context,
) -> list[FieldIR]:
    """Combine path-item and operation parameters using OpenAPI override semantics.

    An operation parameter replaces a path-item parameter with the same name and location.
    """
    merged: dict[tuple[str, str], FieldIR] = {}
    sources: list[tuple[dict[str, Any], tuple[str, ...]]] = [
        (path_item, ("paths", route, "parameters")),
        (operation, ("paths", route, method, "parameters")),
    ]
    for container, base in sources:
        for index, entry in enumerate(_as_sequence(container.get("parameters"))):
            built = _parameter_field(_as_mapping(entry), (*base, str(index)), ctx)
            if built is not None:
                merged[(built.name, built.location.value)] = built
    return list(merged.values())


def _body_fields(
    operation: dict[str, Any], route: str, method: str, ctx: _Context
) -> list[FieldIR]:
    """Convert a request body into one `FieldIR` per declared media type.

    The body schema is kept whole. Exploding it into individual arguments is schema
    simplification, which belongs to the semantic planner.
    """
    body = _as_mapping(operation.get("requestBody"))
    if not body:
        return []
    base = ("paths", route, method, "requestBody")
    ctx.ledger.consume(base, body, _REQUEST_BODY_KEYS, "request body")
    declared_required = "required" in body
    required, _ = (
        _json_bool(body.get("required", False)) if declared_required else (False, False)
    )
    # A request body offered in several media types is still one argument. Emitting a field
    # per media type gave an operation two inputs both called `body`, which the tool could
    # not compose, so an ordinary "JSON or XML" operation was unbuildable. JSON is preferred
    # because it is what an agent produces; the alternatives are recorded rather than lost.
    content = _as_mapping(body.get("content"))
    offered = list(content)
    chosen = next(
        (item for item in offered if item.split(";")[0].strip() == "application/json"),
        offered[0] if offered else None,
    )
    if len(offered) > 1:
        ctx.ambiguities.append(
            Ambiguity(
                code="multiple_request_media_types",
                field=f"operations.{route}.{method}.inputs.body.media_type",
                source_pointer=openapi_pointer(*base, "content"),
                detail=(
                    f"The request body is offered as {', '.join(offered)}. The tool sends "
                    f"{chosen!r}; a caller needing another must say so."
                ),
                blocking=False,
            )
        )
    fields: list[FieldIR] = []
    for media_type, entry in content.items():
        if media_type != chosen:
            # Consumed so the completeness sweep does not report it as ignored: it was read
            # and deliberately not used.
            ctx.ledger.consume(
                (*base, "content", media_type),
                _as_mapping(entry),
                _MEDIA_TYPE_KEYS,
                "request body media type",
            )
            continue
        media = _as_mapping(entry)
        content_base = (*base, "content", media_type)
        ctx.ledger.consume(content_base, media, _MEDIA_TYPE_KEYS, "request body media type")
        records = [
            Provenance(
                field="name",
                source_pointer=openapi_pointer(*base),
                derivation=Derivation.NORMALIZED,
                rule="openapi.requestBody.name.literal_body",
            ),
            Provenance(
                field="location",
                source_pointer=openapi_pointer(*base),
                derivation=Derivation.NORMALIZED,
                rule="openapi.requestBody.location.body",
            ),
            Provenance(
                field="required",
                source_pointer=openapi_pointer(*base, "required")
                if declared_required
                else openapi_pointer(*base),
                derivation=Derivation.SOURCE if declared_required else Derivation.DEFAULT,
                rule="openapi.requestBody.required",
            ),
            _source("media_type", (*base, "content"), "openapi.requestBody.content.key"),
            Provenance(
                field="deprecated",
                source_pointer=openapi_pointer(*base),
                derivation=Derivation.DEFAULT,
                rule="openapi.requestBody.deprecated.not_applicable",
            ),
        ]
        description = _optional_str(body.get("description"))
        if description is not None:
            records.append(
                _source("description", (*base, "description"), "openapi.requestBody.description")
            )
        type_schema = media.get("schema")
        if isinstance(type_schema, dict):
            records.append(
                _source("type_schema", (*content_base, "schema"), "openapi.requestBody.schema")
            )
        else:
            type_schema = None
        fields.append(
            FieldIR(
                name="body",
                location=ParameterLocation.BODY,
                required=required,
                description=description,
                media_type=media_type,
                type_schema=type_schema,
                examples=_examples(media, content_base),
                provenance=records,
            )
        )
    return fields


def _responses(
    operation: dict[str, Any], route: str, method: str, ctx: _Context
) -> tuple[list[ResponseIR], list[FaultIR]]:
    """Split declared responses into successful outputs and faults.

    2xx and 3xx become outputs. 4xx, 5xx and the OpenAPI `default` response become faults,
    because `default` is overwhelmingly the error case and an agent needs it in the error
    channel rather than the result channel.
    """
    outputs: list[ResponseIR] = []
    faults: list[FaultIR] = []
    for status, entry in _as_mapping(operation.get("responses")).items():
        base = ("paths", route, method, "responses", status)
        mapping = _as_mapping(entry)
        ctx.ledger.consume(base, mapping, _RESPONSE_KEYS, "response")
        records = [_source("status", base, "openapi.responses.key")]
        description = _optional_str(mapping.get("description"))
        if description is not None:
            records.append(
                _source("description", (*base, "description"), "openapi.responses.description")
            )
        media_type: str | None = None
        type_schema: dict[str, Any] | None = None
        examples: list[ExampleIR] = []
        content = _as_mapping(mapping.get("content"))
        if content:
            media_type = next(iter(content))
            media = _as_mapping(content[media_type])
            ctx.ledger.consume(
                (*base, "content", media_type), media, _MEDIA_TYPE_KEYS, "response media type"
            )
            records.append(
                _source("media_type", (*base, "content"), "openapi.responses.content.key")
            )
            candidate = media.get("schema")
            if isinstance(candidate, dict):
                type_schema = candidate
                records.append(
                    _source(
                        "type_schema",
                        (*base, "content", media_type, "schema"),
                        "openapi.responses.schema",
                    )
                )
            examples = _examples(media, (*base, "content", media_type))
        headers = _headers(mapping, base, ctx)

        if status[:1] in {"2", "3"}:
            outputs.append(
                ResponseIR(
                    status=status,
                    description=description,
                    media_type=media_type,
                    type_schema=type_schema,
                    headers=headers,
                    examples=examples,
                    provenance=records,
                )
            )
            continue
        faults.append(
            FaultIR(
                code=status,
                description=description,
                media_type=media_type,
                type_schema=type_schema,
                headers=headers,
                examples=examples,
                provenance=[
                    record.model_copy(update={"field": "code"})
                    if record.field == "status"
                    else record
                    for record in records
                ],
            )
        )
        if status == "default":
            ctx.ambiguities.append(
                Ambiguity(
                    code="default_response_classified_as_fault",
                    field=f"operations.{method}.faults",
                    source_pointer=openapi_pointer(*base),
                    detail=(
                        "The OpenAPI `default` response was classified as a fault. Confirm "
                        "during review that it is not a success case."
                    ),
                    blocking=False,
                )
            )
    return outputs, faults


def _servers(container: dict[str, Any], base: tuple[str, ...], ctx: _Context) -> list[ServerIR]:
    """Extract declared base endpoints from any object that may carry `servers`."""
    servers: list[ServerIR] = []
    for index, entry in enumerate(_as_sequence(container.get("servers"))):
        mapping = _as_mapping(entry)
        pointer = (*base, "servers", str(index))
        ctx.ledger.consume(pointer, mapping, _SERVER_KEYS, "server")
        url = _optional_str(mapping.get("url"))
        if url is None:
            continue
        records = [_source("url", (*pointer, "url"), "openapi.servers.url")]
        description = _optional_str(mapping.get("description"))
        if description is not None:
            records.append(
                _source("description", (*pointer, "description"), "openapi.servers.description")
            )
        servers.append(ServerIR(url=url, description=description, provenance=records))
    return servers


def _auth_schemes(ctx: _Context) -> list[AuthSchemeIR]:
    """Extract and type the declared security schemes."""
    schemes: list[AuthSchemeIR] = []
    components = _as_mapping(ctx.doc.get("components"))
    declared = _as_mapping(components.get("securitySchemes"))
    for scheme_id, entry in declared.items():
        mapping = _as_mapping(entry)
        base = ("components", "securitySchemes", scheme_id)
        ctx.ledger.consume(base, mapping, _SECURITY_SCHEME_KEYS, "security scheme")
        raw_type = _optional_str(mapping.get("type"))
        if raw_type is None:
            continue
        scheme_type = _AUTH_TYPES.get(raw_type, AuthSchemeType.OTHER)
        if scheme_type is AuthSchemeType.OTHER:
            ctx.ambiguities.append(
                Ambiguity(
                    code="unknown_security_scheme_type",
                    field=f"auth_schemes.{scheme_id}.type",
                    source_pointer=openapi_pointer(*base, "type"),
                    detail=(
                        f"Security scheme type {raw_type!r} is not modelled, so its fields were "
                        "kept only in `detail`."
                    ),
                    blocking=False,
                )
            )

        records = [
            _source("scheme_id", base, "openapi.securitySchemes.key"),
            _source("type", (*base, "type"), "openapi.securitySchemes.type"),
            _source("detail", base, "openapi.securitySchemes.verbatim"),
        ]
        description = _optional_str(mapping.get("description"))
        if description is not None:
            records.append(
                _source(
                    "description", (*base, "description"), "openapi.securitySchemes.description"
                )
            )

        http_scheme = bearer_format = api_key_in = api_key_name = open_id_url = None
        scopes: list[str] = []
        if scheme_type is AuthSchemeType.HTTP:
            http_scheme = _optional_str(mapping.get("scheme"))
            if http_scheme is None:
                ctx.ambiguities.append(
                    Ambiguity(
                        code="incomplete_security_scheme",
                        field=f"auth_schemes.{scheme_id}.http_scheme",
                        source_pointer=openapi_pointer(*base),
                        detail=(
                            f"http scheme {scheme_id!r} declares no `scheme`; treated as `other`."
                        ),
                        blocking=False,
                    )
                )
                scheme_type = AuthSchemeType.OTHER
            else:
                records.append(
                    _source("http_scheme", (*base, "scheme"), "openapi.securitySchemes.scheme")
                )
                bearer_format = _optional_str(mapping.get("bearerFormat"))
                if bearer_format is not None:
                    records.append(
                        _source(
                            "bearer_format",
                            (*base, "bearerFormat"),
                            "openapi.securitySchemes.bearerFormat",
                        )
                    )
        elif scheme_type is AuthSchemeType.API_KEY:
            api_key_in = _optional_str(mapping.get("in"))
            api_key_name = _optional_str(mapping.get("name"))
            if api_key_in is None or api_key_name is None:
                ctx.ambiguities.append(
                    Ambiguity(
                        code="incomplete_security_scheme",
                        field=f"auth_schemes.{scheme_id}",
                        source_pointer=openapi_pointer(*base),
                        detail=(
                            f"apiKey scheme {scheme_id!r} is missing `in` or `name`; treated as "
                            "`other`."
                        ),
                        blocking=False,
                    )
                )
                scheme_type, api_key_in, api_key_name = AuthSchemeType.OTHER, None, None
            else:
                records.append(_source("api_key_in", (*base, "in"), "openapi.securitySchemes.in"))
                records.append(
                    _source("api_key_name", (*base, "name"), "openapi.securitySchemes.name")
                )
        elif scheme_type is AuthSchemeType.OAUTH2:
            collected: set[str] = set()
            for flow in _as_mapping(mapping.get("flows")).values():
                collected.update(_as_mapping(_as_mapping(flow).get("scopes")))
            scopes = sorted(collected)
            if scopes:
                records.append(
                    _source("scopes", (*base, "flows"), "openapi.securitySchemes.flows.scopes")
                )
        elif scheme_type is AuthSchemeType.OPEN_ID_CONNECT:
            open_id_url = _optional_str(mapping.get("openIdConnectUrl"))
            if open_id_url is None:
                scheme_type = AuthSchemeType.OTHER
            else:
                records.append(
                    _source(
                        "open_id_connect_url",
                        (*base, "openIdConnectUrl"),
                        "openapi.securitySchemes.openIdConnectUrl",
                    )
                )

        schemes.append(
            AuthSchemeIR(
                scheme_id=scheme_id,
                type=scheme_type,
                description=description,
                http_scheme=http_scheme,
                bearer_format=bearer_format,
                api_key_in=api_key_in,
                api_key_name=api_key_name,
                open_id_connect_url=open_id_url,
                scopes=scopes,
                detail=mapping,
                provenance=records,
            )
        )
    return schemes


def _authentication(
    operation: dict[str, Any], route: str, method: str, ctx: _Context
) -> AuthRequirementIR | None:
    """Resolve what an operation needs in order to authenticate.

    An explicit empty `security` list disables authentication for the operation, which is a
    materially different statement from declaring nothing, and policy synthesis must be able
    to tell them apart in order to fail closed.
    """
    if "security" in operation:
        requirements = _as_sequence(operation.get("security"))
        base: tuple[str, ...] = ("paths", route, method, "security")
    elif "security" in ctx.doc:
        requirements = _as_sequence(ctx.doc.get("security"))
        base = ("security",)
    else:
        return None

    if not requirements:
        return AuthRequirementIR(
            disabled=True,
            provenance=[_source("disabled", base, "openapi.security.explicitly_empty")],
        )

    scheme_ids: set[str] = set()
    scopes: set[str] = set()
    alternative_records: list[SecurityRequirementIR] = []
    for index, requirement in enumerate(requirements):
        entry_schemes: set[str] = set()
        entry_scopes: set[str] = set()
        for scheme_id, values in _as_mapping(requirement).items():
            entry_schemes.add(scheme_id)
            entry_scopes.update(item for item in _as_sequence(values) if isinstance(item, str))
        scheme_ids.update(entry_schemes)
        scopes.update(entry_scopes)
        records_for_entry = [
            _source("scheme_ids", (*base, str(index)), "openapi.security.requirement.schemes")
        ]
        if entry_scopes:
            records_for_entry.append(
                _source("scopes", (*base, str(index)), "openapi.security.requirement.scopes")
            )
        alternative_records.append(
            SecurityRequirementIR(
                scheme_ids=sorted(entry_schemes),
                scopes=sorted(entry_scopes),
                provenance=records_for_entry,
            )
        )

    alternatives = len(requirements)
    records = [
        Provenance(
            field="disabled",
            source_pointer=openapi_pointer(*base),
            derivation=Derivation.SOURCE,
            rule="openapi.security.present",
        ),
    ]
    if scheme_ids:
        records.append(_source("scheme_ids", base, "openapi.security.scheme_ids"))
    if scopes:
        records.append(
            Provenance(
                field="scopes",
                source_pointer=openapi_pointer(*base),
                derivation=Derivation.INFERRED if alternatives > 1 else Derivation.SOURCE,
                rule="openapi.security.scope_union"
                if alternatives > 1
                else "openapi.security.scopes",
                confidence=0.6 if alternatives > 1 else 1.0,
            )
        )
    if alternatives > 1:
        ctx.ambiguities.append(
            Ambiguity(
                code="security_requirement_alternatives",
                field="authentication.scopes",
                source_pointer=openapi_pointer(*base),
                detail=(
                    f"{alternatives} alternative security requirements were unioned into one "
                    "over-approximated scope set. Least-privilege selection is deferred."
                ),
                blocking=False,
            )
        )
    return AuthRequirementIR(
        disabled=False,
        scheme_ids=sorted(scheme_ids),
        scopes=sorted(scopes),
        alternatives=alternative_records,
        provenance=records,
    )


def _async_job(outputs: list[ResponseIR], pointer: str) -> AsyncJobIR | None:
    """Infer that an operation accepts work rather than performing it.

    202 is the whole signal. It says the request was accepted and deliberately does not say
    it was carried out, so a tool built from it that reports success is telling an agent the
    goal is met when nothing has happened yet.

    A `Location` header on that response is where the document says progress can be read.
    Where the document declares acceptance and names nowhere to look, that is recorded as
    such: inventing a polling convention would be this compiler making a promise the service
    never made.
    """
    accepted = next((item for item in outputs if item.status == "202"), None)
    if accepted is None:
        return None
    pollable = {"location", "content-location"}
    poll = next(
        (item.name for item in accepted.headers if item.name.lower() in pollable), None
    )
    return AsyncJobIR(
        status=accepted.status,
        poll_header=poll,
        provenance=[
            Provenance(
                field="status",
                source_pointer=pointer,
                derivation=Derivation.SOURCE,
                rule="openapi.async.accepted_status",
                confidence=1.0,
            ),
            *(
                [
                    Provenance(
                        field="poll_header",
                        source_pointer=pointer,
                        derivation=Derivation.INFERRED,
                        rule="openapi.async.poll_header",
                        confidence=0.8,
                    )
                ]
                if poll
                else []
            ),
        ],
    )


def _pagination(
    operation_id: str, inputs: list[FieldIR], outputs: list[ResponseIR], pointer: str
) -> PaginationIR | None:
    """Propose a pagination mechanism from parameter, field and header evidence."""
    query = {
        _normalize_name(item.name): item.name
        for item in inputs
        if item.location is ParameterLocation.QUERY
    }
    response_fields: dict[str, str] = {}
    header_names: dict[str, str] = {}
    for response in outputs:
        for name in _as_mapping((response.type_schema or {}).get("properties")):
            response_fields[_normalize_name(name)] = name
        for header in response.headers:
            header_names[_normalize_name(header.name)] = header.name

    cursor_param = next((query[key] for key in _CURSOR_PARAMS if key in query), None)
    cursor_field = next(
        (response_fields[key] for key in _CURSOR_FIELDS if key in response_fields), None
    )
    cursor_header = next(
        (
            header_names[key]
            for key in header_names
            if key in _CURSOR_FIELDS or key.startswith("nextcursor")
        ),
        None,
    )
    page_param = next((query[key] for key in _PAGE_PARAMS if key in query), None)
    size_param = next((query[key] for key in _SIZE_PARAMS if key in query), None)
    offset_param = next((query[key] for key in _OFFSET_PARAMS if key in query), None)
    link_header = header_names.get("link")

    style: PaginationStyle
    if cursor_param or cursor_field or cursor_header:
        style = PaginationStyle.CURSOR
    elif page_param:
        style = PaginationStyle.PAGE_NUMBER
    elif offset_param and size_param:
        style = PaginationStyle.OFFSET_LIMIT
    elif link_header:
        style = PaginationStyle.LINK_HEADER
    else:
        return None

    # Two independent signals agreeing is stronger evidence than one.
    corroborated = sum(
        1
        for signal in (cursor_param, cursor_field or cursor_header, page_param, offset_param)
        if signal
    )
    confidence = 0.85 if corroborated > 1 else 0.7

    values = {
        "cursor_parameter": cursor_param,
        "page_parameter": page_param,
        "size_parameter": size_param,
        "offset_parameter": offset_param,
        "next_cursor_field": cursor_field,
        "next_link_header": link_header or cursor_header,
    }
    records = [
        Provenance(
            field="style",
            source_pointer=pointer,
            derivation=Derivation.INFERRED,
            rule=f"openapi.pagination.style.{style.value}",
            confidence=confidence,
        )
    ]
    records.extend(
        Provenance(
            field=name,
            source_pointer=pointer,
            derivation=Derivation.INFERRED,
            rule=f"openapi.pagination.evidence.{name}",
            confidence=confidence,
        )
        for name, value in values.items()
        if value is not None
    )
    return PaginationIR(
        style=style,
        cursor_parameter=cursor_param,
        page_parameter=page_param,
        size_parameter=size_param,
        offset_parameter=offset_param,
        next_cursor_field=cursor_field,
        next_link_header=link_header or cursor_header,
        provenance=records,
    )


def _side_effect(
    method: str,
    operation_id: str,
    summary: str | None,
    pointer_tokens: tuple[str, ...],
    ctx: _Context,
) -> tuple[SideEffectClass, list[Provenance]]:
    """Classify the side effect from the HTTP method, then from language.

    Language may only raise the class. A safe method described in destructive language is a
    contradiction rather than evidence, so it is flagged for review instead of reclassified.
    """
    base, confidence = _METHOD_SIDE_EFFECT.get(method, (SideEffectClass.UNKNOWN, 0.0))
    route_pointer = openapi_pointer(*pointer_tokens[:2])
    if base is SideEffectClass.UNKNOWN:
        records = [
            Provenance(
                field="side_effect",
                source_pointer=openapi_pointer(*pointer_tokens),
                derivation=Derivation.DEFAULT,
                rule="openapi.side_effect.unclassified_method",
            )
        ]
    else:
        records = [
            Provenance(
                field="side_effect",
                source_pointer=route_pointer,
                derivation=Derivation.INFERRED,
                rule=f"openapi.side_effect.method.{method}",
                confidence=confidence,
            )
        ]

    tokens = _word_tokens(operation_id) | (_word_tokens(summary) if summary else set())
    signals = sorted(_DESTRUCTIVE_TOKENS & tokens)
    if not signals or base is SideEffectClass.DESTRUCTIVE:
        return base, records

    if base is SideEffectClass.READ:
        # Decision: blocking. A read-labelled operation described in
        # destructive language is rare, and the asymmetry favours a human glance: a false
        # positive costs one review, a false negative lets an agent destroy data through a
        # tool the surface presents as safe.
        ctx.ambiguities.append(
            Ambiguity(
                code="side_effect_language_conflict",
                field="side_effect",
                source_pointer=openapi_pointer(*pointer_tokens),
                detail=(
                    f"The HTTP method implies a read, but the wording contains {signals}. The "
                    "classification was left as read; confirm the method is correct."
                ),
                blocking=True,
            )
        )
        return base, records

    records.append(
        Provenance(
            field="side_effect",
            source_pointer=openapi_pointer(*pointer_tokens),
            derivation=Derivation.INFERRED,
            rule=f"openapi.side_effect.language_escalation.{signals[0]}",
            confidence=0.7,
        )
    )
    return SideEffectClass.DESTRUCTIVE, records


def _build_operation(
    path_item: dict[str, Any], operation: dict[str, Any], route: str, method: str, ctx: _Context
) -> OperationIR:
    """Normalize one OpenAPI operation into an `OperationIR`."""
    op_base = ("paths", route, method)
    ctx.ledger.consume(op_base, operation, _OPERATION_KEYS, "operation")
    pointer = openapi_pointer(*op_base)
    raw_id = _optional_str(operation.get("operationId"))
    operation_id = operation_identifier(raw_id if raw_id else f"{method}_{route}")
    summary = _optional_str(operation.get("summary"))

    records = [
        Provenance(
            field="operation_id",
            source_pointer=openapi_pointer(*op_base, "operationId")
            if raw_id
            else openapi_pointer(*op_base),
            derivation=Derivation.SOURCE if raw_id else Derivation.NORMALIZED,
            rule="openapi.operationId" if raw_id else "openapi.operationId.route_method_fallback",
        ),
        Provenance(
            field="protocol",
            source_pointer=openapi_pointer("openapi"),
            derivation=Derivation.NORMALIZED,
            rule="openapi.protocol.http",
        ),
        Provenance(
            field="source_pointer",
            source_pointer=pointer,
            derivation=Derivation.NORMALIZED,
            rule="openapi.operation.pointer",
        ),
        _source("route", ("paths", route), "openapi.paths.key"),
    ]

    if summary:
        intent = summary
        records.append(_source("intent", (*op_base, "summary"), "openapi.summary"))
    else:
        intent = operation_id
        records.append(
            Provenance(
                field="intent",
                source_pointer=pointer,
                derivation=Derivation.NORMALIZED,
                rule="openapi.intent.from_operation_id",
            )
        )

    side_effect, side_effect_records = _side_effect(method, operation_id, summary, op_base, ctx)
    records.extend(side_effect_records)

    idempotency, idempotency_confidence = _METHOD_IDEMPOTENCY.get(
        method, (Idempotency.UNKNOWN, 0.0)
    )
    records.append(
        Provenance(
            field="idempotency",
            source_pointer=openapi_pointer("paths", route)
            if idempotency is not Idempotency.UNKNOWN
            else pointer,
            derivation=Derivation.INFERRED
            if idempotency is not Idempotency.UNKNOWN
            else Derivation.DEFAULT,
            rule=f"openapi.idempotency.rfc9110.{method}"
            if idempotency is not Idempotency.UNKNOWN
            else "openapi.idempotency.undetermined",
            confidence=idempotency_confidence if idempotency is not Idempotency.UNKNOWN else 1.0,
        )
    )

    description = _optional_str(operation.get("description"))
    if description is not None:
        records.append(_source("description", (*op_base, "description"), "openapi.description"))

    deprecated, _ = (
        _json_bool(operation.get("deprecated", False))
        if "deprecated" in operation
        else (False, False)
    )
    records.append(
        Provenance(
            field="deprecated",
            source_pointer=openapi_pointer(*op_base, "deprecated")
            if "deprecated" in operation
            else pointer,
            derivation=Derivation.SOURCE if "deprecated" in operation else Derivation.DEFAULT,
            rule="openapi.operation.deprecated",
        )
    )
    tags = [item for item in operation.get("tags", []) if isinstance(item, str) and item]
    if tags:
        records.append(
            Provenance(
                field="tags",
                source_pointer=openapi_pointer(*op_base, "tags"),
                derivation=Derivation.SOURCE,
                rule="openapi.operation.tags",
            )
        )

    inputs = [
        *_merged_parameters(path_item, operation, route, method, ctx),
        *_body_fields(operation, route, method, ctx),
    ]
    outputs, faults = _responses(operation, route, method, ctx)

    # Operation servers win over path servers; both override the service endpoints.
    servers = _servers(operation, op_base, ctx) or _servers(path_item, ("paths", route), ctx)

    pagination = (
        _pagination(operation_id, inputs, outputs, pointer)
        if side_effect is SideEffectClass.READ
        else None
    )

    body_fields = [item for item in inputs if item.location is ParameterLocation.BODY]
    if len(body_fields) > 1:
        ctx.ambiguities.append(
            Ambiguity(
                code="multiple_request_media_types",
                field="inputs",
                source_pointer=openapi_pointer(*op_base, "requestBody", "content"),
                detail=(
                    f"{len(body_fields)} request media types were retained as separate body "
                    "inputs."
                ),
                blocking=False,
            )
        )

    return OperationIR(
        tags=tags,
        operation_id=operation_id,
        protocol=Protocol.HTTP,
        source_pointer=pointer,
        route=route,
        intent=intent,
        side_effect=side_effect,
        idempotency=idempotency,
        description=description,
        deprecated=deprecated,
        inputs=inputs,
        outputs=outputs,
        faults=faults,
        authentication=_authentication(operation, route, method, ctx),
        servers=servers,
        pagination=pagination,
        async_job=_async_job(outputs, pointer),
        provenance=records,
    )


def _service(ctx: _Context, path: Path, digest: str, spec_version: str) -> ServiceIR:
    """Build the service identity block."""
    info = _as_mapping(ctx.doc.get("info"))
    ctx.ledger.consume(("info",), info, _INFO_KEYS, "info object")
    title = _optional_str(info.get("title")) or path.stem
    version = _optional_str(info.get("version"))
    declared_title = bool(info.get("title"))
    title_pointer = ("info", "title") if declared_title else ("info",)
    records = [
        Provenance(
            field="service_id",
            source_pointer=openapi_pointer(*title_pointer),
            derivation=Derivation.NORMALIZED,
            rule="openapi.service_id.slug_of_title",
        ),
        Provenance(
            field="title",
            source_pointer=openapi_pointer(*title_pointer),
            derivation=Derivation.SOURCE if declared_title else Derivation.NORMALIZED,
            rule="openapi.info.title" if declared_title else "openapi.title.from_filename",
        ),
        _source("spec_version", ("openapi",), "openapi.version_marker"),
        _source("source_format", ("openapi",), "openapi.version_marker"),
        Provenance(
            field="source_uri",
            source_pointer=openapi_pointer(),
            derivation=Derivation.NORMALIZED,
            rule="ingest.source_uri.input_path",
        ),
        _source("source_digest", (), "ingest.source_digest.sha256_of_bytes"),
        _source("source_documents", (), "ingest.source_documents.loaded_set"),
    ]
    if version is not None:
        records.append(_source("version", ("info", "version"), "openapi.info.version"))
    description = _optional_str(info.get("description"))
    if description:
        records.append(_source("description", ("info", "description"), "openapi.info.description"))
    terms = _optional_str(info.get("termsOfService"))
    if terms:
        records.append(
            _source("terms_of_service", ("info", "termsOfService"), "openapi.info.termsOfService")
        )

    return ServiceIR(
        service_id=slug(title),
        title=title,
        version=version,
        description=description,
        terms_of_service=terms,
        spec_version=spec_version,
        source_format=SourceFormat.OPENAPI,
        source_uri=path.as_posix(),
        source_digest=digest,
        source_documents=ctx.resolver.documents,
        servers=_servers(ctx.doc, (), ctx),
        auth_schemes=_auth_schemes(ctx),
        provenance=records,
    )


def parse_openapi(path: Path, *, policy: RefPolicy | None = None) -> ApiSemanticIR:
    """Parse an OpenAPI 3.x document into the API Semantic IR.

    `policy` controls which reference targets may be loaded. The default denies everything
    outside the root document, so parsing a third-party specification cannot read files it
    was not explicitly pointed at, and never reaches the network.

    Raises `OpenApiIngestionError` if the document is not OpenAPI 3.x. Swagger 2 is a
    separate adapter rather than a special case here, so that a downgrade cannot silently
    produce a lossy IR.
    """
    doc, digest, translation_ambiguities = _load(path)
    spec_version = str(doc.get("openapi", ""))
    if not spec_version.startswith(SUPPORTED_MAJOR):
        found = spec_version or "no version"
        raise OpenApiIngestionError(
            f"{path}: only OpenAPI 3.x is supported by this adapter; got {found!r}"
        )

    resolver = RefResolver(
        root_path=path,
        root_data=doc,
        root_digest=digest,
        policy=policy or RefPolicy(),
    )
    ctx = _Context(doc=doc, resolver=resolver)
    # Anything the Swagger 2 translation could not carry over is reported alongside everything
    # the OpenAPI adapter finds, so a reader sees one list rather than having to know which
    # input format the document arrived in.
    ctx.ambiguities.extend(translation_ambiguities)
    ctx.ledger.consume((), doc, _ROOT_KEYS, "document")

    if not any(spec_version.startswith(minor) for minor in KNOWN_MINORS):
        ctx.ambiguities.append(
            Ambiguity(
                code="unrecognized_openapi_minor_version",
                field="service.spec_version",
                source_pointer=openapi_pointer("openapi"),
                detail=(
                    f"Version {spec_version!r} is 3.x but not one this adapter has been verified "
                    f"against ({', '.join(KNOWN_MINORS)}). Constructs new to it may be unhandled."
                ),
                blocking=False,
            )
        )

    operations: list[OperationIR] = []
    for route, raw_path_item in _as_mapping(doc.get("paths")).items():
        route_pointer = openapi_pointer("paths", route)
        path_item = _as_mapping(
            resolver.resolve(raw_path_item, field_path="paths", pointer=route_pointer)
        )
        if not path_item:
            continue
        ctx.ledger.consume(("paths", route), path_item, _PATH_ITEM_KEYS, "path item")
        for method, raw_operation in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            operation = _as_mapping(raw_operation)
            if not operation:
                continue
            operations.append(_build_operation(path_item, operation, route, method.lower(), ctx))

    seen: dict[str, int] = {}
    for built in operations:
        seen[built.operation_id] = seen.get(built.operation_id, 0) + 1
    for operation_id, count in seen.items():
        if count > 1:
            ctx.ambiguities.append(
                Ambiguity(
                    code="duplicate_operation_id",
                    field="operations.operation_id",
                    source_pointer=openapi_pointer("paths"),
                    detail=(
                        f"Operation identifier {operation_id!r} occurs {count} times. Tool names "
                        "derived from it would collide."
                    ),
                    blocking=True,
                )
            )

    service = _service(ctx, path, digest, spec_version)
    ambiguities = [*resolver.ambiguities, *ctx.ambiguities, *ctx.ledger.ambiguities()]
    return ApiSemanticIR(service=service, operations=operations, ambiguities=ambiguities)
