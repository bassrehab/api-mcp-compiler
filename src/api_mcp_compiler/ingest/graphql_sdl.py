"""Ingesting a GraphQL schema.

One endpoint, an enormous surface, and the tool-design question at its sharpest.

## Root fields are the operations, and that part is easy

`Query` fields read, `Mutation` fields change state, `Subscription` fields deliver events. Each
root field has arguments and a return type, which is an operation by any reading. No inference
is needed and none is done.

## The part that is not easy

A GraphQL call is not "invoke this field". It is "invoke this field **and select these
subfields**", and the selection is the caller's to write. That is precisely the shape this
project refuses elsewhere: a client composing its own request against a schema is authoring,
and an agent composing one is authoring without review.

Three options, and only one of them is honest.

**Expose the selection to the agent.** The agent writes GraphQL. That is the `execute_sql`
problem with different syntax: the blast radius is the whole graph and no reviewer can look at
it in advance, because it does not exist until the agent invents it.

**Select everything.** A selection set over a graph has no natural bottom. Types reference each
other, and `customer { orders { customer { ... } } }` is legal and unbounded. Any depth limit is
a number this compiler made up.

**Select the scalars, one level deep, and say so.** What is returned is every scalar field on
the immediate type, and nothing that requires traversing an edge. It is derived rather than
declared, recorded as such with a source pointer, and a reviewer can see exactly what an agent
will get back.

The third is what this does. It gives up depth, which is real, and it gives up the thing that
makes GraphQL GraphQL. The alternative is a tool whose result shape nobody can review, and this
project has already decided that question in the other direction three times: for response
projection, for the query catalogue, and for raw schemas.

**Where an agent genuinely needs a nested selection, the answer is the same as for a database:
somebody writes the query down.** A named operation in a `.graphql` document is a specification
the same way a query catalogue is, and compiling those is a smaller and more honest feature
than compiling a schema. It is not built here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from graphql import (
    GraphQLEnumType,
    GraphQLField,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    build_schema,
)
from graphql.error import GraphQLSyntaxError

from api_mcp_compiler.language import destructive_signals
from api_mcp_compiler.models import (
    Ambiguity,
    ApiSemanticIR,
    Derivation,
    DocumentRole,
    FieldIR,
    Idempotency,
    OperationIR,
    ParameterLocation,
    Protocol,
    Provenance,
    ResponseIR,
    ServiceIR,
    SideEffectClass,
    SourceDocumentIR,
    SourceFormat,
)
from api_mcp_compiler.provenance import json_pointer, operation_identifier, slug

#: Scalars that map straight onto JSON Schema. Anything else is a custom scalar, whose
#: serialised form the schema does not state, so it is treated as a string and recorded as
#: normalized rather than source.
_SCALARS = {
    "String": "string",
    "ID": "string",
    "Int": "integer",
    "Float": "number",
    "Boolean": "boolean",
}


class GraphqlIngestionError(ValueError):
    """Raised when a GraphQL schema cannot be read at all."""


def is_graphql(text: str) -> bool:
    """Whether a document looks like GraphQL SDL.

    Matched on a type definition rather than a marker, because SDL has no version header. A
    document with neither `type Query` nor `schema {` is not one this can compile, and saying
    so early is better than a parse error about a stray brace.
    """
    stripped = text.lstrip()
    return "type Query" in stripped or stripped.startswith("schema") or "\nschema" in stripped


def parse_graphql(path: Path) -> ApiSemanticIR:
    """Compile a GraphQL schema into the IR."""
    text = path.read_text(encoding="utf-8")
    from hashlib import sha256

    digest = "sha256:" + sha256(path.read_bytes()).hexdigest()

    try:
        schema = build_schema(text)
    except GraphQLSyntaxError as error:
        raise GraphqlIngestionError(f"{path.name} is not valid GraphQL SDL: {error}") from error

    if schema.query_type is None and schema.mutation_type is None:
        raise GraphqlIngestionError(
            f"{path.name} declares neither a Query nor a Mutation type, so it names nothing "
            "anybody can call."
        )

    ambiguities: list[Ambiguity] = []
    operations: list[OperationIR] = []

    for root, kind in (
        (schema.query_type, "query"),
        (schema.mutation_type, "mutation"),
        (schema.subscription_type, "subscription"),
    ):
        if root is None:
            continue
        for name, field in root.fields.items():
            operations.append(
                _operation(name, field, kind, schema, ambiguities)
            )

    return ApiSemanticIR(
        service=_service(path, digest, schema),
        operations=operations,
        ambiguities=ambiguities,
    )


def _service(path: Path, digest: str, schema: GraphQLSchema) -> ServiceIR:
    """The service, named for the file.

    SDL carries no title, no version and no server. Inventing any of them would be this
    compiler asserting a fact the document does not contain, so the name comes from the
    filename and is recorded as normalized to say exactly that.
    """
    title = path.stem.replace("_", " ").replace("-", " ").title()
    return ServiceIR(
        service_id=slug(title),
        title=title,
        version="unversioned",
        source_format=SourceFormat.GRAPHQL,
        source_uri=path.as_posix(),
        source_digest=digest,
        description=schema.description,
        source_documents=[
            SourceDocumentIR(uri=path.as_posix(), digest=digest, role=DocumentRole.ROOT)
        ],
        # SDL names no endpoint. A GraphQL service is one URL and the schema never says which.
        servers=[],
        provenance=[
            Provenance(
                field=field,
                source_pointer=json_pointer(),
                derivation=derivation,
                rule=rule,
            )
            for field, derivation, rule in (
                ("title", Derivation.NORMALIZED, "graphql.title.from_filename"),
                ("service_id", Derivation.NORMALIZED, "graphql.service_id.slug"),
                ("version", Derivation.DEFAULT, "graphql.version.undeclared"),
                (
                    "description",
                    Derivation.SOURCE if schema.description else Derivation.DEFAULT,
                    "graphql.schema.description",
                ),
                ("source_format", Derivation.NORMALIZED, "graphql.format.sdl"),
                ("source_uri", Derivation.NORMALIZED, "graphql.source.path"),
                ("source_digest", Derivation.NORMALIZED, "graphql.source.digest"),
                ("source_documents", Derivation.NORMALIZED, "graphql.source.single_document"),
                ("servers", Derivation.DEFAULT, "graphql.servers.undeclared"),
            )
        ],
    )


def _operation(
    name: str,
    field: GraphQLField,
    kind: str,
    schema: GraphQLSchema,
    ambiguities: list[Ambiguity],
) -> OperationIR:
    pointer = json_pointer(kind, name)
    identifier = operation_identifier(name)
    side_effect, confidence, rule = _side_effect(
        name, kind, field, pointer, ambiguities, identifier
    )

    if kind == "subscription":
        # The same problem AsyncAPI has, for the same reason. An agent subscribed to a stream
        # is not calling a tool, and inventing a polling shape would be designing an
        # integration rather than reading one.
        ambiguities.append(
            Ambiguity(
                code="event_driven_operation",
                field=f"operations.{identifier}.side_effect",
                source_pointer=pointer,
                detail=(
                    f"{name} is a subscription: it delivers messages over time rather than "
                    "answering a call. MCP is request and response, so no tool is emitted for "
                    "it and nothing here invents a polling shape the schema did not give."
                ),
                blocking=True,
            )
        )

    return OperationIR(
        operation_id=identifier,
        protocol=Protocol.GRAPHQL,
        source_pointer=pointer,
        route=None,
        intent=name,
        side_effect=side_effect,
        idempotency=(
            Idempotency.IDEMPOTENT if side_effect is SideEffectClass.READ else Idempotency.UNKNOWN
        ),
        description=_description(name, field, kind, schema),
        deprecated=bool(field.deprecation_reason),
        tags=[kind],
        inputs=_arguments(field, pointer),
        outputs=_outputs(field, pointer, schema),
        provenance=[
            Provenance(
                field=at,
                source_pointer=pointer,
                derivation=derivation,
                rule=why,
                **({"confidence": score} if score is not None else {}),
            )
            for at, derivation, why, score in (
                ("operation_id", Derivation.SOURCE, "graphql.root_field.name", None),
                ("intent", Derivation.SOURCE, "graphql.root_field.name", None),
                ("side_effect", Derivation.INFERRED, rule, confidence),
                ("idempotency", Derivation.INFERRED, "graphql.idempotency.from_kind", 0.9),
                ("protocol", Derivation.NORMALIZED, "graphql.protocol", None),
                ("source_pointer", Derivation.NORMALIZED, "graphql.root_field.position", None),
                ("route", Derivation.DEFAULT, "graphql.route.single_endpoint", None),
                (
                    "description",
                    Derivation.NORMALIZED,
                    "graphql.description.includes_selection",
                    None,
                ),
                (
                    "deprecated",
                    Derivation.SOURCE if field.deprecation_reason else Derivation.DEFAULT,
                    "graphql.field.deprecated",
                    None,
                ),
                ("tags", Derivation.NORMALIZED, "graphql.tag.root_type", None),
            )
        ],
    )


def _side_effect(
    name: str,
    kind: str,
    field: GraphQLField,
    pointer: str,
    ambiguities: list[Ambiguity],
    identifier: str,
) -> tuple[SideEffectClass, float, str]:
    """Classify from the root type, raised where the field's own name says worse.

    The root type is a strong signal: somebody put the field under `Mutation` because it
    changes something. It does not separate a mutation that updates a row from one that
    deletes an account, so the shared vocabulary is consulted for that, exactly as it is for
    an HTTP method.
    """
    if kind == "subscription":
        return SideEffectClass.UNKNOWN, 0.0, "graphql.side_effect.subscription"

    if kind == "query":
        signals = destructive_signals(name, field.description or "")
        if signals:
            ambiguities.append(
                Ambiguity(
                    code="side_effect_language_conflict",
                    field=f"operations.{identifier}.side_effect",
                    source_pointer=pointer,
                    detail=(
                        f"{name} is a Query field, which reads, and its wording contains "
                        f"{signals}. The classification was left as read; confirm the field "
                        "is under the right root type."
                    ),
                    blocking=True,
                )
            )
        return SideEffectClass.READ, 0.95, "graphql.side_effect.query"

    if destructive_signals(name, field.description or ""):
        return (
            SideEffectClass.DESTRUCTIVE,
            0.9,
            "graphql.side_effect.mutation.destructive_wording",
        )
    return SideEffectClass.WRITE, 0.95, "graphql.side_effect.mutation"


def _arguments(field: GraphQLField, pointer: str) -> list[FieldIR]:
    """Field arguments, which are what a caller supplies."""
    fields = []
    for name, argument in field.args.items():
        at = json_pointer(pointer.lstrip("#/"), "args", name)
        required = isinstance(argument.type, GraphQLNonNull)
        fields.append(
            FieldIR(
                name=name,
                location=ParameterLocation.BODY,
                required=required,
                type_schema=_schema_for(argument.type),
                description=argument.description,
                deprecated=bool(argument.deprecation_reason),
                provenance=[
                    Provenance(field=at_field, source_pointer=at, derivation=derivation, rule=rule)
                    for at_field, derivation, rule in (
                        ("name", Derivation.SOURCE, "graphql.argument.name"),
                        ("location", Derivation.NORMALIZED, "graphql.argument.variable"),
                        ("required", Derivation.SOURCE, "graphql.argument.non_null"),
                        ("type_schema", Derivation.NORMALIZED, "graphql.argument.type"),
                        (
                            "description",
                            Derivation.SOURCE if argument.description else Derivation.DEFAULT,
                            "graphql.argument.description",
                        ),
                        (
                            "deprecated",
                            Derivation.SOURCE
                            if argument.deprecation_reason
                            else Derivation.DEFAULT,
                            "graphql.argument.deprecated",
                        ),
                    )
                ],
            )
        )
    return fields


def _schema_for(node: Any) -> dict[str, Any]:
    """A GraphQL type as JSON Schema, unwrapping wrappers as it goes."""
    if isinstance(node, GraphQLNonNull):
        return _schema_for(node.of_type)
    if isinstance(node, GraphQLList):
        return {"type": "array", "items": _schema_for(node.of_type)}
    if isinstance(node, GraphQLEnumType):
        return {"type": "string", "enum": sorted(node.values)}
    if isinstance(node, GraphQLScalarType):
        return {"type": _SCALARS.get(node.name, "string")}
    # An object or input type reached through an argument. Its shape is the schema's, and
    # flattening it here would invent a JSON Schema the document did not state.
    return {"type": "object"}


def _selection(node: Any) -> tuple[list[str], dict[str, Any]]:
    """The scalar fields of a return type, one level deep.

    Everything requiring an edge to be traversed is left out, and the caller records that this
    was derived. See the module docstring for why depth is refused rather than chosen.
    """
    while isinstance(node, GraphQLNonNull | GraphQLList):
        node = node.of_type

    if not isinstance(node, GraphQLObjectType):
        return [], _schema_for(node)

    selected = []
    properties: dict[str, Any] = {}
    for name, field in node.fields.items():
        inner = field.type
        while isinstance(inner, GraphQLNonNull | GraphQLList):
            inner = inner.of_type
        if isinstance(inner, GraphQLScalarType | GraphQLEnumType):
            selected.append(name)
            properties[name] = _schema_for(field.type)
    return selected, {"type": "object", "properties": properties}


def _outputs(field: GraphQLField, pointer: str, schema: GraphQLSchema) -> list[ResponseIR]:
    selected, shape = _selection(field.type)
    described = (
        f"Returns {', '.join(selected)}."
        if selected
        else "Returns a scalar."
    )
    return [
        ResponseIR(
            status="200",
            description=described,
            type_schema=shape,
            provenance=[
                Provenance(
                    field=at,
                    source_pointer=pointer,
                    derivation=Derivation.NORMALIZED,
                    rule="graphql.selection.scalars_one_level",
                )
                for at in ("status", "description", "type_schema")
            ],
        )
    ]


def _description(name: str, field: GraphQLField, kind: str, schema: GraphQLSchema) -> str:
    """The description, saying what the agent will get back.

    The selection is derived rather than declared, so a reviewer has to be told what it is.
    Leaving it implicit would hide the one decision this adapter makes on their behalf.
    """
    parts = []
    if field.description:
        parts.append(field.description.strip())

    selected, _ = _selection(field.type)
    if kind == "subscription":
        parts.append("Delivers messages over time rather than answering a call.")
    elif selected:
        parts.append(
            "Returns the scalar fields of the result and nothing that requires following a "
            f"reference: {', '.join(selected)}. Deeper selection is not offered, because a "
            "selection an agent composes is a request nobody reviewed."
        )
    if field.deprecation_reason:
        parts.append(f"Deprecated: {field.deprecation_reason}")
    return "\n\n".join(parts) if parts else name
