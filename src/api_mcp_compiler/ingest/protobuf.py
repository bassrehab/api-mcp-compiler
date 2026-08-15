"""Ingesting gRPC service definitions.

protobuf suits this pipeline better than OpenAPI does, and it is worth saying why rather than
asserting it. A `.proto` file is a schema language with no optional parts: every field has a
type, every message is closed, and a service block declares exactly which methods exist with
exactly which request and response messages. There is nothing to infer, which means there is
nothing to guess wrong.

The differentiation was never the parser. An adapter here inherits provenance, policy
derivation, the emission gate, drift and evidence; a competitor adding a format inherits
nothing, because there is nothing behind theirs.

## Parsed with protoc rather than by hand

`grpc_tools.protoc` compiles the file to a `FileDescriptorSet` and this reads that. Writing a
`.proto` parser would mean quietly disagreeing with the specification on the day somebody used
a feature the hand-rolled version had not met, and a compiler that misreads a type produces a
tool that corrupts data rather than failing.

It is an optional dependency, installed with `pip install api-mcp-compiler[grpc]`. Twenty-two
megabytes of protoc is not something to impose on somebody who came here to read an OpenAPI
document.

## What has no shape in MCP

A streaming method is not request-and-response, and the same reasoning applies as for AsyncAPI
receives and GraphQL subscriptions:

- **Server streaming** returns many messages over time. A tool returning the first, or all of
  them buffered, is a shape the service did not offer.
- **Client streaming** and **bidirectional** need the caller to keep sending, which a tool call
  cannot express at all.

All three are ingested and held with a blocking ambiguity. Skipping them would let somebody
believe their streaming surface was covered; collapsing them into a unary call would be this
compiler inventing an integration.

## Side effects, and the one thing protobuf does not say

A `.proto` file states types perfectly and says nothing about what a method does. `DeleteUser`
and `GetUser` are the same shape, and only the name separates them, so the shared vocabulary
classifies them exactly as it does an operation name anywhere else. A method whose name carries
no signal at all is a write when it takes a non-empty request and returns `Empty`, and unknown
otherwise, which is recorded with low confidence rather than asserted.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from api_mcp_compiler.language import destructive_signals, word_tokens
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

#: protobuf scalar types by their descriptor number, mapped to JSON Schema.
#:
#: 64-bit integers become strings, which is the protobuf JSON mapping rather than a choice made
#: here: a value beyond 2^53 does not survive a JSON number, and an agent handed a silently
#: rounded account identifier has no way to know.
_SCALARS: dict[int, dict[str, Any]] = {
    1: {"type": "number"},  # double
    2: {"type": "number"},  # float
    3: {"type": "string", "description": "64-bit integer, carried as a string."},  # int64
    4: {"type": "string", "description": "64-bit integer, carried as a string."},  # uint64
    5: {"type": "integer"},  # int32
    6: {"type": "string", "description": "64-bit integer, carried as a string."},  # fixed64
    7: {"type": "integer"},  # fixed32
    8: {"type": "boolean"},
    9: {"type": "string"},
    12: {"type": "string", "description": "Bytes, base64 encoded."},
    13: {"type": "integer"},  # uint32
    15: {"type": "integer"},  # sfixed32
    16: {"type": "string", "description": "64-bit integer, carried as a string."},
    17: {"type": "integer"},  # sint32
    18: {"type": "string", "description": "64-bit integer, carried as a string."},
}

#: Verbs that say a method changes something, where the name carries no destructive signal.
_WRITING = frozenset(
    {"create", "update", "set", "add", "insert", "put", "patch", "send", "submit", "apply"}
)


class ProtobufIngestionError(ValueError):
    """Raised when a `.proto` file cannot be read at all."""


def parse_proto(path: Path, *, include: tuple[Path, ...] = ()) -> ApiSemanticIR:
    """Compile a `.proto` file into the IR.

    `include` names directories to resolve imports from. Local only: a remote import would
    make compiling depend on the network and on the moment it ran, which is the rule every
    other adapter here keeps.
    """
    descriptor = _descriptor_set(path, include)
    file = next(
        (item for item in descriptor.file if item.name == path.name),
        descriptor.file[-1] if descriptor.file else None,
    )
    if file is None:
        raise ProtobufIngestionError(f"{path.name} produced no descriptor.")
    if not file.service:
        raise ProtobufIngestionError(
            f"{path.name} declares no service. A file of messages says what data looks like "
            "and not what anybody may call."
        )

    from hashlib import sha256

    digest = "sha256:" + sha256(path.read_bytes()).hexdigest()
    messages = _message_index(descriptor)
    ambiguities: list[Ambiguity] = []

    operations = []
    for service in file.service:
        for method in service.method:
            operations.append(
                _operation(service.name, method, messages, ambiguities, file.package)
            )

    return ApiSemanticIR(
        service=_service(path, digest, file),
        operations=operations,
        ambiguities=ambiguities,
    )


def _descriptor_set(path: Path, include: tuple[Path, ...]) -> Any:
    """Compile with protoc and read the result.

    Shelled out rather than parsed, because writing a `.proto` parser means disagreeing with
    the specification the first time somebody uses a feature it had not met, and a compiler
    that misreads a type produces a tool that corrupts data rather than failing.
    """
    try:
        from google.protobuf import descriptor_pb2
    except ImportError as error:  # pragma: no cover - exercised by the extras test
        raise ProtobufIngestionError(
            "Reading .proto files needs the grpc extra: pip install api-mcp-compiler[grpc]"
        ) from error

    with tempfile.TemporaryDirectory() as workspace:
        output = Path(workspace) / "descriptor.pb"
        command = [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"--proto_path={path.parent}",
            *[f"--proto_path={item}" for item in include],
            f"--descriptor_set_out={output}",
            "--include_imports",
            path.name,
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=60
        )
        if completed.returncode != 0:
            raise ProtobufIngestionError(
                f"{path.name} could not be compiled: {completed.stderr.strip()}"
            )
        payload = descriptor_pb2.FileDescriptorSet()
        payload.ParseFromString(output.read_bytes())
        return payload


def _message_index(descriptor: Any) -> dict[str, Any]:
    """Every message by its fully qualified name, so a method's types resolve."""
    index: dict[str, Any] = {}

    def walk(prefix: str, messages: Any) -> None:
        for message in messages:
            name = f"{prefix}.{message.name}" if prefix else message.name
            index[f".{name}"] = message
            walk(name, message.nested_type)

    for file in descriptor.file:
        walk(file.package, file.message_type)
    return index


def _service(path: Path, digest: str, file: Any) -> ServiceIR:
    title = file.package or path.stem
    return ServiceIR(
        service_id=slug(title),
        title=title,
        version="unversioned",
        source_format=SourceFormat.PROTOBUF,
        source_uri=path.as_posix(),
        source_digest=digest,
        description=None,
        source_documents=[
            SourceDocumentIR(uri=path.as_posix(), digest=digest, role=DocumentRole.ROOT)
        ],
        # A .proto names no host. gRPC endpoints are deployment facts, not schema facts.
        servers=[],
        provenance=[
            Provenance(
                field=field,
                source_pointer=json_pointer(),
                derivation=derivation,
                rule=rule,
            )
            for field, derivation, rule in (
                ("title", Derivation.SOURCE, "protobuf.package"),
                ("service_id", Derivation.NORMALIZED, "protobuf.service_id.slug"),
                ("version", Derivation.DEFAULT, "protobuf.version.undeclared"),
                ("description", Derivation.DEFAULT, "protobuf.description.undeclared"),
                ("source_format", Derivation.NORMALIZED, "protobuf.format"),
                ("source_uri", Derivation.NORMALIZED, "protobuf.source.path"),
                ("source_digest", Derivation.NORMALIZED, "protobuf.source.digest"),
                ("source_documents", Derivation.NORMALIZED, "protobuf.source.single_document"),
                ("servers", Derivation.DEFAULT, "protobuf.servers.undeclared"),
            )
        ],
    )


def _operation(
    service: str,
    method: Any,
    messages: dict[str, Any],
    ambiguities: list[Ambiguity],
    package: str,
) -> OperationIR:
    pointer = json_pointer("service", service, "method", method.name)
    identifier = operation_identifier(f"{service}_{method.name}")
    streaming = method.client_streaming or method.server_streaming

    if streaming:
        which = (
            "bidirectional"
            if method.client_streaming and method.server_streaming
            else "client streaming"
            if method.client_streaming
            else "server streaming"
        )
        ambiguities.append(
            Ambiguity(
                code="streaming_method",
                field=f"operations.{identifier}.side_effect",
                source_pointer=pointer,
                detail=(
                    f"{service}.{method.name} is a {which} method. A tool call is one request "
                    "and one response, so returning the first message, or buffering all of "
                    "them, would be a shape this service did not offer. No tool is emitted "
                    "and nothing here invents one."
                ),
                blocking=True,
            )
        )

    side_effect, confidence, rule = _side_effect(
        method, messages, streaming
    )

    return OperationIR(
        operation_id=identifier,
        protocol=Protocol.GRPC,
        source_pointer=pointer,
        route=f"/{package}.{service}/{method.name}" if package else f"/{service}/{method.name}",
        intent=method.name,
        side_effect=side_effect,
        idempotency=(
            Idempotency.IDEMPOTENT if side_effect is SideEffectClass.READ else Idempotency.UNKNOWN
        ),
        description=_description(service, method, streaming),
        deprecated=bool(method.options.deprecated),
        tags=[service],
        inputs=_fields(messages.get(method.input_type), pointer),
        outputs=_outputs(method, messages, pointer, streaming),
        provenance=[
            Provenance(
                field=field,
                source_pointer=pointer,
                derivation=derivation,
                rule=why,
                **({"confidence": score} if score is not None else {}),
            )
            for field, derivation, why, score in (
                ("operation_id", Derivation.NORMALIZED, "protobuf.method.qualified_name", None),
                ("intent", Derivation.SOURCE, "protobuf.method.name", None),
                ("side_effect", Derivation.INFERRED, rule, confidence),
                ("idempotency", Derivation.INFERRED, "protobuf.idempotency.from_effect", 0.85),
                ("protocol", Derivation.NORMALIZED, "protobuf.protocol.grpc", None),
                ("source_pointer", Derivation.NORMALIZED, "protobuf.method.position", None),
                ("route", Derivation.NORMALIZED, "protobuf.route.grpc_path", None),
                ("description", Derivation.NORMALIZED, "protobuf.description.derived", None),
                (
                    "deprecated",
                    Derivation.SOURCE if method.options.deprecated else Derivation.DEFAULT,
                    "protobuf.method.deprecated",
                    None,
                ),
                ("tags", Derivation.NORMALIZED, "protobuf.tag.service", None),
            )
        ],
    )


def _side_effect(
    method: Any, messages: dict[str, Any], streaming: bool
) -> tuple[SideEffectClass, float, str]:
    """Classify from the method name, which is the only signal a `.proto` carries.

    `DeleteUser` and `GetUser` are the same shape. protobuf states types perfectly and says
    nothing about what a method does, so the shared vocabulary does the work it does anywhere
    else, and where a name carries no signal this says unknown rather than guessing.
    """
    if streaming:
        return SideEffectClass.UNKNOWN, 0.0, "protobuf.side_effect.streaming"

    name = method.name
    if destructive_signals(name):
        return SideEffectClass.DESTRUCTIVE, 0.9, "protobuf.side_effect.name.destructive"

    tokens = word_tokens(name)
    if tokens & _WRITING:
        return SideEffectClass.WRITE, 0.85, "protobuf.side_effect.name.writing"
    if tokens & {"get", "list", "read", "fetch", "find", "search", "query", "describe"}:
        return SideEffectClass.READ, 0.85, "protobuf.side_effect.name.reading"

    # No signal in the name. An empty response to a non-empty request is weak evidence of a
    # command, and it is recorded as weak rather than acted on as if it were not.
    output = messages.get(method.output_type)
    if output is not None and not output.field:
        return SideEffectClass.WRITE, 0.5, "protobuf.side_effect.empty_response"
    return SideEffectClass.UNKNOWN, 0.0, "protobuf.side_effect.unrecognised_name"


def _fields(message: Any, pointer: str) -> list[FieldIR]:
    """A request message's fields, flattened one level.

    Nested messages become objects without their inner shape spelled out. Expanding them here
    would inline a type the caller can already resolve, and a recursive message would not
    terminate.
    """
    if message is None:
        return []

    fields = []
    for field in message.field:
        at = json_pointer(pointer.lstrip("#/"), "field", field.name)
        # Label 3 is repeated, and 1 is optional in proto3's descriptor encoding.
        repeated = field.label == 3
        schema = _SCALARS.get(field.type, {"type": "object"})
        if repeated:
            schema = {"type": "array", "items": schema}

        fields.append(
            FieldIR(
                name=field.name,
                location=ParameterLocation.BODY,
                # proto3 has no required. Everything is optional on the wire, and saying
                # otherwise would make an agent fill in fields the service never demanded.
                required=False,
                type_schema=schema,
                description=None,
                deprecated=bool(field.options.deprecated),
                provenance=[
                    Provenance(field=name, source_pointer=at, derivation=derivation, rule=rule)
                    for name, derivation, rule in (
                        ("name", Derivation.SOURCE, "protobuf.field.name"),
                        ("location", Derivation.NORMALIZED, "protobuf.field.message_body"),
                        ("required", Derivation.NORMALIZED, "protobuf.proto3.all_optional"),
                        ("type_schema", Derivation.NORMALIZED, "protobuf.field.type"),
                        ("description", Derivation.DEFAULT, "protobuf.field.no_comment"),
                        (
                            "deprecated",
                            Derivation.SOURCE
                            if field.options.deprecated
                            else Derivation.DEFAULT,
                            "protobuf.field.deprecated",
                        ),
                    )
                ],
            )
        )
    return fields


def _outputs(
    method: Any, messages: dict[str, Any], pointer: str, streaming: bool
) -> list[ResponseIR]:
    output = messages.get(method.output_type)
    properties = {}
    if output is not None:
        for field in output.field:
            schema = _SCALARS.get(field.type, {"type": "object"})
            properties[field.name] = (
                {"type": "array", "items": schema} if field.label == 3 else schema
            )

    described = (
        "Delivers a stream rather than a single response, which a tool call cannot express."
        if streaming
        else f"Returns {method.output_type.lstrip('.')}."
    )
    return [
        ResponseIR(
            status="200",
            description=described,
            type_schema={"type": "object", "properties": properties} if properties else None,
            provenance=[
                Provenance(
                    field=field,
                    source_pointer=pointer,
                    derivation=Derivation.NORMALIZED,
                    rule="protobuf.response.message",
                )
                for field in ("status", "description", "type_schema")
            ],
        )
    ]


def _description(service: str, method: Any, streaming: bool) -> str:
    """What the method is, said plainly.

    A `.proto` carries comments only if protoc was asked for source info, and this does not
    ask: a comment is not a contract, and a description built from one would vary with how the
    file was formatted.
    """
    parts = [f"{service}.{method.name}, a gRPC method."]
    if streaming:
        parts.append(
            "Streams rather than answering once, so no tool is offered for it."
        )
    parts.append(
        f"Takes {method.input_type.lstrip('.')} and returns {method.output_type.lstrip('.')}."
    )
    if method.options.deprecated:
        parts.append("Deprecated in the schema.")
    return " ".join(parts)
