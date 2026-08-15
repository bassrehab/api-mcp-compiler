"""WSDL 1.1 ingestion adapter.

Depth: the binding surface that the WSDL document states directly, namely port
types, bindings, style, transport, SOAPAction, endpoint addresses and message parts, each
with a provenance record. XSD type resolution, typed faults, SOAP headers, WS-Security and
MTOM are not translated, and are recorded as ambiguities rather than omitted.

A SOAP operation never receives an inferred side-effect class. HTTP method semantics have
no SOAP equivalent, and guessing would let a write or destructive operation reach the
approval gate labelled as a read. Every operation therefore carries a blocking ambiguity
until a human classifies it.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from api_mcp_compiler.ingest.transport import Transport, declared_requirement, declared_scheme
from api_mcp_compiler.ingest.wspolicy import collect_policies, read_policy
from api_mcp_compiler.ingest.xsd import (
    XSD_NAMESPACE,
    XsdIndex,
    XsdResolution,
    build_index,
    resolve_type,
)
from api_mcp_compiler.models import (
    Ambiguity,
    ApiSemanticIR,
    AuthRequirementIR,
    AuthSchemeIR,
    Derivation,
    DocumentRole,
    FaultIR,
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
    SoapBindingIR,
    SourceDocumentIR,
    SourceFormat,
)
from api_mcp_compiler.provenance import (
    operation_identifier,
    slug,
    source_digest,
    wsdl_pointer,
    xpath_step,
)

WSDL_NS = "http://schemas.xmlsoap.org/wsdl/"
SOAP11_NS = "http://schemas.xmlsoap.org/wsdl/soap/"
SOAP12_NS = "http://schemas.xmlsoap.org/wsdl/soap12/"
WSDL20_NS = "http://www.w3.org/ns/wsdl"

_ROOT_STEP = xpath_step("definitions")

# Decision: WSDL documents are third-party input. External entity
# resolution, network retrieval and DTD loading are disabled because an ingested
# specification must never be able to read local files or reach the network.
_XML_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    dtd_validation=False,
    huge_tree=False,
)


class WsdlIngestionError(ValueError):
    """Raised when a document cannot be ingested as WSDL 1.1."""


def _local_name(tag: object) -> str:
    """Return the local part of an element tag."""
    text = str(tag)
    return text.rsplit("}", 1)[-1]


def _resolve_qname(value: str, element: etree._Element) -> tuple[str | None, str]:
    """Resolve a prefixed QName against an element's in-scope namespace declarations."""
    if ":" not in value:
        return element.nsmap.get(None), value
    prefix, local = value.split(":", 1)
    return element.nsmap.get(prefix), local


def _documentation(element: etree._Element) -> str | None:
    """Return the trimmed text of a WSDL `documentation` child, if present."""
    child = element.find(f"{{{WSDL_NS}}}documentation")
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def _message_parts(
    root: etree._Element, message_local: str
) -> tuple[list[etree._Element], str | None]:
    """Return the `part` elements of a named message, and the message name if it exists."""
    for message in root.findall(f"{{{WSDL_NS}}}message"):
        if message.get("name") == message_local:
            return message.findall(f"{{{WSDL_NS}}}part"), message_local
    return [], None


def _soap_child(element: etree._Element, name: str) -> etree._Element | None:
    """Find a SOAP 1.1 or SOAP 1.2 extension child by local name."""
    for namespace in (SOAP11_NS, SOAP12_NS):
        found = element.find(f"{{{namespace}}}{name}")
        if found is not None:
            return found
    return None


def _binding_for_port_type(
    root: etree._Element, port_type_name: str
) -> etree._Element | None:
    """Find the binding whose `type` attribute resolves to a given port type."""
    for binding in root.findall(f"{{{WSDL_NS}}}binding"):
        raw_type = binding.get("type")
        if raw_type is None:
            continue
        _, local = _resolve_qname(raw_type, binding)
        if local == port_type_name:
            return binding
    return None


def _port_for_binding(
    root: etree._Element, binding_name: str
) -> tuple[etree._Element | None, etree._Element | None]:
    """Find the service port bound to a binding, together with its owning service."""
    for service in root.findall(f"{{{WSDL_NS}}}service"):
        for port in service.findall(f"{{{WSDL_NS}}}port"):
            raw_binding = port.get("binding")
            if raw_binding is None:
                continue
            _, local = _resolve_qname(raw_binding, port)
            if local == binding_name:
                return service, port
    return None, None


def _message_schema(parts: list[etree._Element], index: XsdIndex) -> XsdResolution:
    """Resolve a message into one schema.

    A single part is the response itself; several parts are an object over them, which is what
    a SOAP body containing several elements amounts to.

    A message with no parts carries nothing, and that is a statement rather than a gap. WSDL
    permits an empty message and services use it for a void return, the way HTTP uses 204.
    Reporting it as unresolved blocked operations whose documents were perfectly clear: two of
    the forty in the third-party collection said their response was empty and were refused for
    saying so.
    """
    if not parts:
        return XsdResolution(schema=None, unresolved=[])
    resolved = []
    for part in parts:
        reference = part.get("element") or part.get("type")
        if reference is None:
            resolved.append((part.get("name") or "part", XsdResolution(schema={})))
            continue
        resolved.append((part.get("name") or "part", resolve_type(str(reference), index)))
    unresolved = [item for _, resolution in resolved for item in resolution.unresolved]
    if len(resolved) == 1:
        return XsdResolution(schema=resolved[0][1].schema, unresolved=unresolved)
    if any(resolution.schema is None for _, resolution in resolved):
        return XsdResolution(schema=None, unresolved=unresolved)
    return XsdResolution(
        schema={
            "type": "object",
            "properties": {name: resolution.schema for name, resolution in resolved},
        },
        unresolved=unresolved,
    )


def _part_fields(
    parts: list[etree._Element],
    message_local: str,
    port_type_name: str,
    operation_name: str,
    index: XsdIndex,
) -> tuple[list[FieldIR], list[Ambiguity]]:
    """Convert message parts into input fields, resolving the XSD types they point at."""
    fields: list[FieldIR] = []
    ambiguities: list[Ambiguity] = []
    for part in parts:
        name = part.get("name")
        if name is None:
            continue
        pointer = wsdl_pointer(
            _ROOT_STEP,
            xpath_step("message", name=message_local),
            xpath_step("part", name=name),
        )
        type_reference = part.get("element") or part.get("type")
        resolution = (
            resolve_type(str(type_reference), index)
            if type_reference is not None
            else XsdResolution(schema=None)
        )
        fields.append(
            FieldIR(
                name=name,
                location=ParameterLocation.SOAP_BODY,
                required=True,
                description=type_reference,
                type_schema=resolution.schema,
                provenance=[
                    Provenance(
                        field="name",
                        source_pointer=pointer,
                        derivation=Derivation.SOURCE,
                        rule="wsdl.message.part.name",
                    ),
                    Provenance(
                        field="location",
                        source_pointer=pointer,
                        derivation=Derivation.NORMALIZED,
                        rule="wsdl.part.location.soap_body",
                    ),
                    # A part is structurally always present in the envelope. Whether its
                    # content is optional is an XSD minOccurs question the part itself does not
                    # resolve, which the ambiguity below records.
                    Provenance(
                        field="required",
                        source_pointer=pointer,
                        derivation=Derivation.NORMALIZED,
                        rule="wsdl.part.presence_in_message",
                    ),
                    # WSDL 1.1 has no deprecation marker, so the contract default stands.
                    Provenance(
                        field="deprecated",
                        source_pointer=pointer,
                        derivation=Derivation.DEFAULT,
                        rule="wsdl.part.deprecated.not_expressible",
                    ),
                    *(
                        [
                            Provenance(
                                field="description",
                                source_pointer=pointer,
                                derivation=Derivation.SOURCE,
                                rule="wsdl.message.part.type_reference",
                            )
                        ]
                        if type_reference is not None
                        else []
                    ),
                    *(
                        [
                            Provenance(
                                field="type_schema",
                                source_pointer=pointer,
                                derivation=Derivation.NORMALIZED,
                                rule="wsdl.part.xsd_type_resolution",
                            )
                        ]
                        if resolution.schema is not None
                        else []
                    ),
                ],
            )
        )
        if type_reference is not None and not resolution.complete:
            # A type that resolved with a caveat is reported without blocking: the schema is
            # usable and the caveat says what it does not capture. Only a type that produced
            # no schema at all leaves an operation without a contract.
            ambiguities.append(
                Ambiguity(
                    code="unresolved_xsd_type",
                    field=f"operations.{operation_name}.inputs.{name}.type_schema",
                    source_pointer=pointer,
                    detail=(
                        f"Part {name!r} of port type {port_type_name!r} references "
                        f"{type_reference!r}. "
                        + ("; ".join(resolution.unresolved) or "It could not be resolved.")
                    ),
                    blocking=resolution.schema is None,
                )
            )
    return fields, ambiguities


def _soap_binding_records(values: dict[str, tuple[str | None, str]]) -> list[Provenance]:
    """Build one source provenance record per populated SOAP binding field.

    Each field carries the pointer to the element that actually declares it, rather than a
    single pointer for the whole binding block: an endpoint is declared on a service port,
    not on the binding.
    """
    return [
        Provenance(
            field=name,
            source_pointer=pointer,
            derivation=Derivation.SOURCE,
            rule=f"wsdl.binding.{name}",
        )
        for name, (value, pointer) in values.items()
        if value is not None
    ]


def _build_operation(
    root: etree._Element,
    port_type: etree._Element,
    operation: etree._Element,
    target_namespace: str,
    index: XsdIndex,
) -> tuple[OperationIR, list[Ambiguity]]:
    """Normalize one WSDL port-type operation into an `OperationIR`."""
    port_type_name = port_type.get("name") or "unnamed_port_type"
    raw_name = operation.get("name") or "unnamed_operation"
    operation_id = operation_identifier(raw_name)
    pointer = wsdl_pointer(
        _ROOT_STEP,
        xpath_step("portType", name=port_type_name),
        xpath_step("operation", name=raw_name),
    )
    ambiguities: list[Ambiguity] = []

    binding = _binding_for_port_type(root, port_type_name)
    binding_name = binding.get("name") if binding is not None else None
    binding_pointer = (
        wsdl_pointer(_ROOT_STEP, xpath_step("binding", name=binding_name))
        if binding_name is not None
        else wsdl_pointer(_ROOT_STEP)
    )
    style: str | None = None
    use: str | None = None
    transport: str | None = None
    soap_action: str | None = None
    if binding is not None:
        soap_binding = _soap_child(binding, "binding")
        if soap_binding is not None:
            style = soap_binding.get("style")
            transport = soap_binding.get("transport")
        for bound in binding.findall(f"{{{WSDL_NS}}}operation"):
            if bound.get("name") != raw_name:
                continue
            soap_operation = _soap_child(bound, "operation")
            if soap_operation is not None:
                soap_action = soap_operation.get("soapAction")
                style = soap_operation.get("style") or style
            bound_input = bound.find(f"{{{WSDL_NS}}}input")
            if bound_input is not None:
                soap_body = _soap_child(bound_input, "body")
                if soap_body is not None:
                    use = soap_body.get("use") or use

    port_name: str | None = None
    endpoint: str | None = None
    port_pointer = wsdl_pointer(_ROOT_STEP)
    if binding_name is not None:
        service, port = _port_for_binding(root, binding_name)
        if port is not None:
            port_name = port.get("name")
            service_name = (service.get("name") or "") if service is not None else ""
            port_pointer = wsdl_pointer(
                _ROOT_STEP,
                xpath_step("service", name=service_name),
                xpath_step("port", name=port_name or ""),
            )
            address = _soap_child(port, "address")
            if address is not None:
                endpoint = address.get("location")

    inputs: list[FieldIR] = []
    input_message: str | None = None
    input_element = operation.find(f"{{{WSDL_NS}}}input")
    if input_element is not None:
        raw_message = input_element.get("message")
        if raw_message is not None:
            namespace, local = _resolve_qname(raw_message, input_element)
            if namespace not in (None, target_namespace):
                ambiguities.append(
                    Ambiguity(
                        code="cross_namespace_message_reference",
                        field=f"operations.{operation_id}.inputs",
                        source_pointer=pointer,
                        detail=(
                            f"Input message {raw_message!r} resolves outside the target "
                            "namespace. WSDL imports are recorded rather than resolved."
                        ),
                        blocking=True,
                    )
                )
            parts, input_message = _message_parts(root, local)
            part_fields, part_ambiguities = _part_fields(
                parts, local, port_type_name, operation_id, index
            )
            inputs.extend(part_fields)
            ambiguities.extend(part_ambiguities)

    outputs: list[ResponseIR] = []
    output_message: str | None = None
    output_element = operation.find(f"{{{WSDL_NS}}}output")
    if output_element is not None:
        raw_message = output_element.get("message")
        if raw_message is not None:
            _, output_message = _resolve_qname(raw_message, output_element)
        # The output message names parts like the input does, so the response schema is the
        # part's type when there is one part, and an object over them when there are several.
        output_parts, _ = _message_parts(root, output_message or "")
        output_resolution = _message_schema(output_parts, index)
        outputs.append(
            ResponseIR(
                status="output",
                description=output_message,
                type_schema=output_resolution.schema,
                provenance=[
                    Provenance(
                        field="status",
                        source_pointer=pointer,
                        derivation=Derivation.NORMALIZED,
                        rule="wsdl.output.status.literal_output",
                    ),
                    *(
                        [
                            Provenance(
                                field="description",
                                source_pointer=pointer,
                                derivation=Derivation.SOURCE,
                                rule="wsdl.output.message",
                            )
                        ]
                        if output_message is not None
                        else []
                    ),
                    *(
                        [
                            Provenance(
                                field="type_schema",
                                source_pointer=pointer,
                                derivation=Derivation.NORMALIZED,
                                rule="wsdl.output.xsd_type_resolution",
                            )
                        ]
                        if output_resolution.schema is not None
                        else []
                    ),
                ],
            )
        )
        # A message with no parts resolved to nothing because it carries nothing, which is
        # not the same as a type this compiler could not translate. Only the caller knows
        # which of those happened, because `complete` cannot tell an empty answer from a
        # missing one.
        if output_message is not None and output_parts and not output_resolution.complete:
            ambiguities.append(
                Ambiguity(
                    code="unresolved_xsd_type",
                    field=f"operations.{operation_id}.outputs.type_schema",
                    source_pointer=pointer,
                    detail=(
                        f"Output message {output_message!r} references a type that "
                        + ("; ".join(output_resolution.unresolved) or "could not be resolved.")
                    ),
                    blocking=output_resolution.schema is None,
                )
            )

    faults: list[FaultIR] = []
    for fault in operation.findall(f"{{{WSDL_NS}}}fault"):
        fault_name = fault.get("name")
        if fault_name is None:
            continue
        faults.append(
            FaultIR(
                code=fault_name,
                provenance=[
                    Provenance(
                        field="code",
                        source_pointer=pointer,
                        derivation=Derivation.SOURCE,
                        rule="wsdl.fault.name",
                    )
                ],
            )
        )
        ambiguities.append(
            Ambiguity(
                code="untyped_soap_fault",
                field=f"operations.{operation_id}.faults.{fault_name}",
                source_pointer=pointer,
                detail=(
                    f"Fault {fault_name!r} was recorded by name only. Typed fault translation "
                    "is not translated."
                ),
                blocking=False,
            )
        )

    soap = SoapBindingIR(
        target_namespace=target_namespace,
        port_type=port_type_name,
        binding=binding_name,
        port=port_name,
        style=style,
        transport=transport,
        soap_action=soap_action,
        endpoint=endpoint,
        input_message=input_message,
        output_message=output_message,
        provenance=[
            Provenance(
                field="target_namespace",
                source_pointer=wsdl_pointer(_ROOT_STEP),
                derivation=Derivation.SOURCE,
                rule="wsdl.definitions.targetNamespace",
            ),
            Provenance(
                field="port_type",
                source_pointer=pointer,
                derivation=Derivation.SOURCE,
                rule="wsdl.portType.name",
            ),
            *_soap_binding_records(
                {
                    "binding": (binding_name, binding_pointer),
                    "port": (port_name, port_pointer),
                    "style": (style, binding_pointer),
                    "transport": (transport, binding_pointer),
                    "soap_action": (soap_action, binding_pointer),
                    "endpoint": (endpoint, port_pointer),
                    "input_message": (input_message, pointer),
                    "output_message": (output_message, pointer),
                }
            ),
        ],
    )

    if binding is None:
        ambiguities.append(
            Ambiguity(
                code="missing_soap_binding",
                field=f"operations.{operation_id}.soap.binding",
                source_pointer=pointer,
                detail=(
                    f"No binding declares port type {port_type_name!r}, so no SOAPAction or "
                    "endpoint could be determined."
                ),
                blocking=True,
            )
        )
    if style is not None and style not in {"document", "rpc"}:
        ambiguities.append(
            Ambiguity(
                code="unsupported_soap_style",
                field=f"operations.{operation_id}.soap.style",
                source_pointer=pointer,
                detail=f"Binding style {style!r} is neither document nor rpc.",
                blocking=True,
            )
        )
    if use is not None and use != "literal":
        # RPC and document differ in how the body is shaped, not in what the parameters are,
        # so the tool schema is derivable either way and the style alone does not block.
        # Encoding is different: SOAP Section 5 serialises values as a graph with href and id
        # references, and guessing at that would produce a request the service rejects. The
        # schema stands; serving it does not.
        ambiguities.append(
            Ambiguity(
                code="unsupported_soap_encoding",
                field=f"operations.{operation_id}.soap.use",
                source_pointer=pointer,
                detail=(
                    f"The body uses {use!r} rather than literal. SOAP Section 5 encoding "
                    "serialises values as a reference graph, which this compiler does not "
                    "write. The operation is described in full; it cannot be served."
                ),
                blocking=True,
            )
        )
    # Decision: this ambiguity is blocking on every SOAP operation by
    # design. WSDL carries no side-effect signal, and the project safety rules require
    # side-effect and idempotency classification before a write tool may be emitted.
    ambiguities.append(
        Ambiguity(
            code="unclassified_side_effect",
            field=f"operations.{operation_id}.side_effect",
            source_pointer=pointer,
            detail=(
                f"Operation {raw_name!r} has no side-effect classification. WSDL provides no "
                "signal equivalent to an HTTP method, so a human must classify it before any "
                "executable tool is generated."
            ),
            blocking=True,
        )
    )

    documentation = _documentation(operation)
    intent = documentation if documentation else operation_id
    records = [
        Provenance(
            field="operation_id",
            source_pointer=pointer,
            derivation=Derivation.SOURCE,
            rule="wsdl.portType.operation.name",
        ),
        Provenance(
            field="protocol",
            source_pointer=wsdl_pointer(_ROOT_STEP),
            derivation=Derivation.NORMALIZED,
            rule="wsdl.protocol.soap",
        ),
        Provenance(
            field="source_pointer",
            source_pointer=pointer,
            derivation=Derivation.NORMALIZED,
            rule="wsdl.operation.pointer",
        ),
        Provenance(
            field="intent",
            source_pointer=pointer,
            derivation=Derivation.SOURCE if documentation else Derivation.NORMALIZED,
            rule="wsdl.documentation" if documentation else "wsdl.intent.from_operation_name",
        ),
        Provenance(
            field="side_effect",
            source_pointer=pointer,
            derivation=Derivation.DEFAULT,
            rule="wsdl.side_effect.unclassified",
        ),
        Provenance(
            field="idempotency",
            source_pointer=pointer,
            derivation=Derivation.DEFAULT,
            rule="wsdl.idempotency.undetermined",
        ),
        Provenance(
            field="deprecated",
            source_pointer=pointer,
            derivation=Derivation.DEFAULT,
            rule="wsdl.operation.deprecated.not_expressible",
        ),
    ]
    if documentation:
        records.append(
            Provenance(
                field="description",
                source_pointer=pointer,
                derivation=Derivation.SOURCE,
                rule="wsdl.documentation",
            )
        )

    return (
        OperationIR(
            operation_id=operation_id,
            protocol=Protocol.SOAP,
            source_pointer=pointer,
            intent=intent,
            side_effect=SideEffectClass.UNKNOWN,
            idempotency=Idempotency.UNKNOWN,
            description=documentation,
            inputs=inputs,
            outputs=outputs,
            faults=faults,
            soap=soap,
            provenance=records,
        ),
        ambiguities,
    )


def _service(
    root: etree._Element, path: Path, digest: str, target_namespace: str
) -> ServiceIR:
    """Build the service identity block from the WSDL service and port elements."""
    services = root.findall(f"{{{WSDL_NS}}}service")
    service_element = services[0] if services else None
    title = (service_element.get("name") if service_element is not None else None) or path.stem
    root_pointer = wsdl_pointer(_ROOT_STEP)
    service_pointer = (
        wsdl_pointer(_ROOT_STEP, xpath_step("service", name=title))
        if service_element is not None
        else root_pointer
    )

    servers: list[ServerIR] = []
    for service in services:
        for port in service.findall(f"{{{WSDL_NS}}}port"):
            address = _soap_child(port, "address")
            location = address.get("location") if address is not None else None
            if location is None:
                continue
            port_pointer = wsdl_pointer(
                _ROOT_STEP,
                xpath_step("service", name=service.get("name") or ""),
                xpath_step("port", name=port.get("name") or ""),
            )
            servers.append(
                ServerIR(
                    url=location,
                    description=port.get("name"),
                    provenance=[
                        Provenance(
                            field="url",
                            source_pointer=port_pointer,
                            derivation=Derivation.SOURCE,
                            rule="wsdl.port.address.location",
                        ),
                        *(
                            [
                                Provenance(
                                    field="description",
                                    source_pointer=port_pointer,
                                    derivation=Derivation.SOURCE,
                                    rule="wsdl.port.name",
                                )
                            ]
                            if port.get("name") is not None
                            else []
                        ),
                    ],
                )
            )

    return ServiceIR(
        service_id=slug(title),
        title=title,
        source_format=SourceFormat.WSDL,
        source_uri=path.as_posix(),
        source_digest=digest,
        # WSDL imports are not resolved in this phase, so the root is the only document
        # loaded. An unresolved import is reported as a blocking ambiguity instead.
        source_documents=[
            SourceDocumentIR(uri=path.as_posix(), digest=digest, role=DocumentRole.ROOT)
        ],
        servers=servers,
        provenance=[
            Provenance(
                field="source_documents",
                source_pointer=root_pointer,
                derivation=Derivation.NORMALIZED,
                rule="ingest.source_documents.root_only",
            ),
            Provenance(
                field="service_id",
                source_pointer=service_pointer,
                derivation=Derivation.NORMALIZED,
                rule="wsdl.service_id.slug_of_service_name",
            ),
            Provenance(
                field="title",
                source_pointer=service_pointer,
                derivation=Derivation.SOURCE
                if service_element is not None
                else Derivation.NORMALIZED,
                rule="wsdl.service.name"
                if service_element is not None
                else "wsdl.title.from_filename",
            ),
            Provenance(
                field="source_format",
                source_pointer=root_pointer,
                derivation=Derivation.SOURCE,
                rule="wsdl.definitions.namespace",
            ),
            Provenance(
                field="source_uri",
                source_pointer=root_pointer,
                derivation=Derivation.NORMALIZED,
                rule="ingest.source_uri.input_path",
            ),
            Provenance(
                field="source_digest",
                source_pointer=root_pointer,
                derivation=Derivation.SOURCE,
                rule="ingest.source_digest.sha256_of_bytes",
            ),
        ],
    )


def parse_wsdl(path: Path, transport: Transport | None = None) -> ApiSemanticIR:
    """Parse a WSDL 1.1 document into the API Semantic IR.

    Raises `WsdlIngestionError` for WSDL 2.0 and for documents whose root is not a WSDL 1.1
    `definitions` element. The seeded parser returned an empty operation list in those
    cases, which is indistinguishable from a service that genuinely has no operations.

    `transport` declares how the service is authenticated when the document does not say. It
    fills a silence and never overrides a policy the document states, because the contract is
    the thing under review, and a declaration that contradicted it is recorded as an ambiguity
    rather than allowed to win. See ADR-037 for why it carries `declared` provenance rather
    than `source`.
    """
    raw = path.read_bytes()
    digest = source_digest(raw)
    try:
        root = etree.fromstring(raw, parser=_XML_PARSER)
    except etree.XMLSyntaxError as error:
        raise WsdlIngestionError(f"{path}: not well-formed XML: {error}") from error

    namespace = etree.QName(root).namespace
    if namespace == WSDL20_NS:
        raise WsdlIngestionError(
            f"{path}: WSDL 2.0 is not supported by this adapter; WSDL 1.1 is expected"
        )
    if namespace != WSDL_NS or _local_name(root.tag) != "definitions":
        raise WsdlIngestionError(
            f"{path}: expected a WSDL 1.1 definitions root element, found {root.tag!r}"
        )

    target_namespace = root.get("targetNamespace")
    if target_namespace is None:
        raise WsdlIngestionError(f"{path}: definitions element has no targetNamespace")

    operations: list[OperationIR] = []
    ambiguities: list[Ambiguity] = []
    # Every schema the document embeds, indexed once. Imports are recorded rather than
    # followed: ingestion never reaches the network, and a schema this document does not
    # carry is a fact about the document.
    index = build_index(root.findall(f".//{{{XSD_NAMESPACE}}}schema"))
    for name in sorted(set(index.collisions)):
        ambiguities.append(
            Ambiguity(
                code="ambiguous_xsd_name",
                field=f"types.{name}",
                source_pointer=_ROOT_STEP,
                detail=(
                    f"More than one schema in this document declares {name!r}. Resolving it "
                    "would mean choosing one arbitrarily, so it is left for a reviewer."
                ),
                blocking=False,
            )
        )
    for port_type in root.findall(f"{{{WSDL_NS}}}portType"):
        for operation in port_type.findall(f"{{{WSDL_NS}}}operation"):
            built, operation_ambiguities = _build_operation(
                root, port_type, operation, target_namespace, index
            )
            operations.append(built)
            ambiguities.extend(operation_ambiguities)

    if root.findall(f"{{{WSDL_NS}}}import"):
        ambiguities.append(
            Ambiguity(
                code="unresolved_wsdl_import",
                field="operations",
                source_pointer=wsdl_pointer(_ROOT_STEP, xpath_step("import")),
                detail=(
                    "The document declares WSDL imports, which are recorded rather than "
                    "resolved, so "
                    "imported operations and types are absent from this IR."
                ),
                blocking=True,
            )
        )

    schemes, requirement, auth_ambiguities = _authentication(root, transport)
    ambiguities.extend(auth_ambiguities)
    if requirement is not None:
        # Applied to every operation. Neither source says anything per-operation: a policy is
        # attached to a binding, and a declaration describes the way in to a service, so
        # varying it per operation would suggest either said more than it did.
        operations = [
            item.model_copy(update={"authentication": requirement}) for item in operations
        ]

    # The schemes belong to the service rather than to the IR root, which is where every
    # other adapter puts them.
    service = _service(root, path, digest, target_namespace)
    if schemes:
        service = service.model_copy(update={"auth_schemes": schemes})

    return ApiSemanticIR(
        service=service,
        operations=operations,
        ambiguities=ambiguities,
    )


def _authentication(
    root: etree._Element, transport: Transport | None
) -> tuple[list[AuthSchemeIR], AuthRequirementIR | None, list[Ambiguity]]:
    """What authenticates this service, preferring what the document itself states.

    The order is not a preference for richer data. The document is the thing under review, so
    a policy it publishes outranks a claim made about it from outside, and a declaration that
    disagrees is reported rather than silently losing. An operator who declares basic
    authentication for a service whose WSDL requires an X.509 token has a misunderstanding
    worth surfacing, and quietly picking either one hides it.
    """
    policies = collect_policies(root)
    ambiguities: list[Ambiguity] = []
    schemes: list[AuthSchemeIR] = []
    requirement: AuthRequirementIR | None = None

    for binding in root.findall(f"{{{WSDL_NS}}}binding"):
        name = binding.get("name") or ""
        pointer = wsdl_pointer(_ROOT_STEP, xpath_step("binding", name=name))
        found, from_policy, policy_ambiguities = read_policy(binding, policies, pointer)
        ambiguities.extend(policy_ambiguities)
        if from_policy is not None and requirement is None:
            schemes, requirement = found, from_policy

    if requirement is not None:
        if transport is not None:
            ambiguities.append(
                Ambiguity(
                    code="declared_transport_ignored",
                    field="authentication",
                    source_pointer=wsdl_pointer(_ROOT_STEP),
                    detail=(
                        "A transport was declared alongside this specification and the document "
                        "states its own security policy, which is used instead. The declaration "
                        "is recorded here rather than applied, because a contract outranks a "
                        "claim made about it from outside."
                    ),
                    blocking=False,
                )
            )
        return schemes, requirement, ambiguities

    if transport is not None:
        return [declared_scheme(transport)], declared_requirement(), ambiguities
    return [], None, ambiguities
