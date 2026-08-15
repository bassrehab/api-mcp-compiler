"""Ingesting AsyncAPI.

AsyncAPI declares channels and operations, which puts it with the specification-based adapters
and lets it inherit provenance, policy derivation, the emission gate, drift and evidence. A
competitor adding a format inherits nothing, because there is nothing behind their parser.

## The thing that makes this harder than it looks

MCP is request and response. AsyncAPI describes systems that are neither.

An operation with `action: send` maps cleanly: an agent publishes a message, which is a call
with arguments and no interesting result. An operation with `action: receive` does not. An
agent that *reacts* to an event is not calling a tool; it is being invoked by something, and
that is a different primitive rather than a tool with an awkward shape.

So a receive operation is ingested and recorded as **not exposable**, with a blocking ambiguity
saying why. Not skipped: a surface that quietly omitted half a document would let somebody
believe their event-driven estate was covered. Not invented into a polling tool either, because
that would be this compiler designing an integration rather than reading one.

## Version

AsyncAPI 3.x, where operations are top-level and reference channels. 2.x inverted that, putting
`publish`/`subscribe` under each channel with the confusing convention that `publish` describes
what a *client* may do. Rather than guess which reading a 2.x document intends, this refuses it
and says so: the reversal is exactly the kind of thing that produces a surface which looks
right and does the opposite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api_mcp_compiler.ingest.documents import load_document
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
    ServerIR,
    ServiceIR,
    SideEffectClass,
    SourceDocumentIR,
    SourceFormat,
)
from api_mcp_compiler.provenance import json_pointer, operation_identifier, slug


class AsyncApiIngestionError(ValueError):
    """Raised when an AsyncAPI document cannot be read at all."""


def is_asyncapi(payload: Any) -> bool:
    """Whether a loaded document is AsyncAPI."""
    return isinstance(payload, dict) and "asyncapi" in payload


def parse_asyncapi(path: Path) -> ApiSemanticIR:
    """Compile an AsyncAPI 3.x document into the IR."""
    payload, digest = load_document(path)

    if not is_asyncapi(payload):
        raise AsyncApiIngestionError(f"{path.name} declares no `asyncapi` version.")

    version = str(payload.get("asyncapi") or "")
    if not version.startswith("3."):
        raise AsyncApiIngestionError(
            f"{path.name} declares AsyncAPI {version!r}, and this adapter reads 3.x. In 2.x, "
            "`publish` describes what a client may do rather than what the server does, and "
            "guessing which reading a document intends produces a surface that looks right "
            "and does the opposite."
        )

    info = payload.get("info")
    if not isinstance(info, dict) or not info.get("title"):
        raise AsyncApiIngestionError(f"{path.name} declares no `info.title`.")

    ambiguities: list[Ambiguity] = []
    channels = payload.get("channels") if isinstance(payload.get("channels"), dict) else {}
    operations_block = (
        payload.get("operations") if isinstance(payload.get("operations"), dict) else {}
    )
    if not operations_block:
        raise AsyncApiIngestionError(
            f"{path.name} declares no operations. A document describing only channels says "
            "what exists and not what anybody may do with it."
        )

    operations = [
        _operation(name, entry, channels, ambiguities)
        for name, entry in operations_block.items()
        if isinstance(entry, dict)
    ]

    return ApiSemanticIR(
        service=_service(info, payload, path, digest),
        operations=operations,
        ambiguities=ambiguities,
    )


def _service(
    info: dict[str, Any], payload: dict[str, Any], path: Path, digest: str
) -> ServiceIR:
    title = str(info["title"])
    servers = []
    for name, entry in (payload.get("servers") or {}).items():
        if not isinstance(entry, dict) or not entry.get("host"):
            continue
        protocol = str(entry.get("protocol") or "")
        servers.append(
            ServerIR(
                url=f"{protocol}://{entry['host']}" if protocol else str(entry["host"]),
                description=entry.get("description"),
                provenance=[
                    Provenance(
                        field="url",
                        source_pointer=json_pointer("servers", str(name), "host"),
                        derivation=Derivation.NORMALIZED,
                        rule="asyncapi.server.host_and_protocol",
                    ),
                    Provenance(
                        field="description",
                        source_pointer=json_pointer("servers", str(name), "description"),
                        derivation=(
                            Derivation.SOURCE
                            if entry.get("description")
                            else Derivation.DEFAULT
                        ),
                        rule="asyncapi.server.description",
                    ),
                ],
            )
        )

    return ServiceIR(
        service_id=slug(title),
        title=title,
        version=str(info.get("version") or "unversioned"),
        source_format=SourceFormat.ASYNCAPI,
        source_uri=path.as_posix(),
        source_digest=digest,
        description=info.get("description"),
        source_documents=[
            SourceDocumentIR(uri=path.as_posix(), digest=digest, role=DocumentRole.ROOT)
        ],
        servers=servers,
        provenance=[
            Provenance(
                field=name,
                source_pointer=pointer,
                derivation=derivation,
                rule=rule,
            )
            for name, pointer, derivation, rule in (
                ("title", json_pointer("info", "title"), Derivation.SOURCE, "asyncapi.title"),
                (
                    "service_id",
                    json_pointer("info", "title"),
                    Derivation.NORMALIZED,
                    "asyncapi.service_id.slug",
                ),
                (
                    "version",
                    json_pointer("info", "version"),
                    Derivation.SOURCE if info.get("version") else Derivation.DEFAULT,
                    "asyncapi.info.version",
                ),
                (
                    "description",
                    json_pointer("info", "description"),
                    Derivation.SOURCE if info.get("description") else Derivation.DEFAULT,
                    "asyncapi.info.description",
                ),
                (
                    "source_format",
                    json_pointer("asyncapi"),
                    Derivation.NORMALIZED,
                    "asyncapi.format.marker",
                ),
                ("source_uri", json_pointer(), Derivation.NORMALIZED, "asyncapi.source.path"),
                (
                    "source_digest",
                    json_pointer(),
                    Derivation.NORMALIZED,
                    "asyncapi.source.digest",
                ),
                (
                    "source_documents",
                    json_pointer(),
                    Derivation.NORMALIZED,
                    "asyncapi.source.single_document",
                ),
                ("servers", json_pointer("servers"), Derivation.NORMALIZED, "asyncapi.servers"),
            )
        ],
    )


def _operation(
    name: str,
    entry: dict[str, Any],
    channels: dict[str, Any],
    ambiguities: list[Ambiguity],
) -> OperationIR:
    pointer = json_pointer("operations", name)
    identifier = operation_identifier(name)
    action = str(entry.get("action") or "").lower()

    if action not in {"send", "receive"}:
        raise AsyncApiIngestionError(
            f"operation {name!r} declares action {action!r}. AsyncAPI 3.x has `send` and "
            "`receive`."
        )

    if action == "receive":
        # Recorded rather than skipped. A surface that quietly omitted half a document would
        # let somebody believe their event-driven estate was covered, and inventing a polling
        # tool would be this compiler designing an integration rather than reading one.
        ambiguities.append(
            Ambiguity(
                code="event_driven_operation",
                field=f"operations.{identifier}.side_effect",
                source_pointer=pointer,
                detail=(
                    f"{name} is a receive operation: something delivers a message to this "
                    "system. MCP is request and response, so an agent reacting to an event "
                    "is not calling a tool, it is being invoked by one. No tool is emitted "
                    "for it, and nothing here invents a polling shape it was not given."
                ),
                blocking=True,
            )
        )

    channel = _channel_for(entry, channels)
    return OperationIR(
        operation_id=identifier,
        protocol=Protocol.ASYNC,
        source_pointer=pointer,
        route=channel.get("address"),
        intent=str(entry.get("summary") or name),
        # A send publishes a message, which changes state somewhere by definition. A receive
        # is unknown because nothing is being called at all.
        side_effect=SideEffectClass.WRITE if action == "send" else SideEffectClass.UNKNOWN,
        idempotency=Idempotency.UNKNOWN,
        description=_description(entry, channel, action),
        deprecated=bool(entry.get("deprecated", False)),
        tags=[
            str(item.get("name"))
            for item in entry.get("tags", [])
            if isinstance(item, dict) and item.get("name")
        ],
        inputs=_inputs(channel, pointer),
        outputs=_outputs(action, pointer),
        provenance=[
            Provenance(
                field=field,
                source_pointer=where,
                derivation=derivation,
                rule=rule,
                # Omitted rather than passed as None: the model has its own default and
                # `confidence=None` is not the same thing as leaving it unset.
                **({"confidence": confidence} if confidence is not None else {}),
            )
            for field, where, derivation, rule, confidence in (
                ("operation_id", pointer, Derivation.NORMALIZED, "asyncapi.operation.key", None),
                (
                    "intent",
                    json_pointer("operations", name, "summary"),
                    Derivation.SOURCE if entry.get("summary") else Derivation.DEFAULT,
                    "asyncapi.operation.summary",
                    None,
                ),
                (
                    "side_effect",
                    json_pointer("operations", name, "action"),
                    Derivation.INFERRED,
                    f"asyncapi.side_effect.action.{action}",
                    0.9 if action == "send" else 0.0,
                ),
                (
                    "idempotency",
                    pointer,
                    Derivation.DEFAULT,
                    "asyncapi.idempotency.undetermined",
                    None,
                ),
                ("protocol", pointer, Derivation.NORMALIZED, "asyncapi.protocol.async", None),
                (
                    "source_pointer",
                    pointer,
                    Derivation.NORMALIZED,
                    "asyncapi.operation.position",
                    None,
                ),
                (
                    "route",
                    pointer,
                    Derivation.SOURCE if channel.get("address") else Derivation.DEFAULT,
                    "asyncapi.channel.address",
                    None,
                ),
                (
                    "description",
                    pointer,
                    Derivation.NORMALIZED,
                    "asyncapi.description.includes_channel",
                    None,
                ),
                (
                    "deprecated",
                    pointer,
                    Derivation.SOURCE if "deprecated" in entry else Derivation.DEFAULT,
                    "asyncapi.operation.deprecated",
                    None,
                ),
            )
        ],
    )


def _channel_for(entry: dict[str, Any], channels: dict[str, Any]) -> dict[str, Any]:
    """The channel an operation names, resolved through its local reference.

    Only local references. A remote one would make compiling depend on the network and on the
    moment it ran, which is the rule the OpenAPI adapter already keeps.
    """
    reference = entry.get("channel")
    if not isinstance(reference, dict):
        return {}
    pointer = str(reference.get("$ref") or "")
    if not pointer.startswith("#/channels/"):
        return {}
    return channels.get(pointer.removeprefix("#/channels/"), {}) or {}


def _inputs(channel: dict[str, Any], pointer: str) -> list[FieldIR]:
    """Channel parameters, which is what a caller has to supply to address a message."""
    fields = []
    for name, definition in (channel.get("parameters") or {}).items():
        if not isinstance(definition, dict):
            continue
        at = json_pointer("channels", "parameters", str(name))
        fields.append(
            FieldIR(
                name=str(name),
                location=ParameterLocation.PATH,
                required=True,
                type_schema={"type": "string"},
                description=definition.get("description"),
                deprecated=False,
                provenance=[
                    Provenance(field=field, source_pointer=at, derivation=derivation, rule=rule)
                    for field, derivation, rule in (
                        ("name", Derivation.SOURCE, "asyncapi.channel.parameter"),
                        ("location", Derivation.NORMALIZED, "asyncapi.parameter.address"),
                        ("required", Derivation.NORMALIZED, "asyncapi.parameter.addressing"),
                        # AsyncAPI channel parameters are strings by definition; there is no
                        # type to read, so this is normalization rather than a guess.
                        ("type_schema", Derivation.NORMALIZED, "asyncapi.parameter.string"),
                        (
                            "description",
                            Derivation.SOURCE
                            if definition.get("description")
                            else Derivation.DEFAULT,
                            "asyncapi.parameter.description",
                        ),
                        ("deprecated", Derivation.DEFAULT, "asyncapi.parameter.deprecated"),
                    )
                ],
            )
        )
    return fields


def _outputs(action: str, pointer: str) -> list[ResponseIR]:
    """What comes back, which for a publish is an acknowledgement and nothing more."""
    described = (
        "The message was accepted for delivery. Publishing is not the same as being acted on, "
        "and nothing in this document says when or whether it will be."
        if action == "send"
        else "Nothing is returned: this operation is not something a caller invokes."
    )
    return [
        ResponseIR(
            status="202" if action == "send" else "000",
            description=described,
            provenance=[
                Provenance(
                    field=field,
                    source_pointer=pointer,
                    derivation=Derivation.NORMALIZED,
                    rule=f"asyncapi.response.{action}",
                )
                for field in ("status", "description")
            ],
        )
    ]


def _description(entry: dict[str, Any], channel: dict[str, Any], action: str) -> str:
    """The description, with the channel in it.

    A reviewer deciding whether an agent may publish needs to know where it publishes to.
    `orders.created` and `payments.settled` are the same shape and not the same decision.
    """
    parts = []
    stated = str(entry.get("description") or "").strip()
    if stated:
        parts.append(stated)

    address = channel.get("address")
    if address:
        parts.append(
            f"{'Publishes to' if action == 'send' else 'Receives from'} {address}."
        )
    if action == "send":
        parts.append(
            "Returns once the message is accepted, which is not the same as it having been "
            "acted on."
        )
    return "\n\n".join(parts) if parts else str(entry.get("summary") or "")
