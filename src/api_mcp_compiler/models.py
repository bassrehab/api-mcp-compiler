"""Versioned contracts for the API Semantic IR and the agent tool-surface plan.

These models are the stable boundary between source-specific ingestion adapters and every
downstream planner, policy generator and evaluator. They are deliberately strict: unknown
fields are rejected, enumerations are closed, and no value carrying information may exist
without a provenance record explaining where it came from.

The rationale behind the split risk enumerations, per-field provenance, ambiguity records
and version pinning is documented on the individual classes below.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IR_SCHEMA_VERSION = "0.11.0"
TOOL_PLAN_SCHEMA_VERSION = "0.4.0"
TOOL_SURFACE_SCHEMA_VERSION = "0.4.0"
TOOL_OVERLAY_SCHEMA_VERSION = "0.3.0"
POLICY_MANIFEST_SCHEMA_VERSION = "0.3.0"
EVAL_CORPUS_SCHEMA_VERSION = "0.3.0"
EVALUATION_RUN_SCHEMA_VERSION = "0.2.0"
BENCHMARK_MANIFEST_SCHEMA_VERSION = "0.1.0"
PREREGISTRATION_SCHEMA_VERSION = "0.1.0"


class SourceFormat(StrEnum):
    """Format of the source specification an IR document was compiled from."""

    OPENAPI = "openapi"
    SWAGGER = "swagger"
    WSDL = "wsdl"
    #: A query catalogue: named, parameterised statements an organisation has written down.
    #: See `docs/query-catalogue.md` for why a catalogue is compiled and a schema is not.
    CATALOGUE = "catalogue"
    ASYNCAPI = "asyncapi"
    GRAPHQL = "graphql"
    PROTOBUF = "protobuf"


class Protocol(StrEnum):
    """Wire protocol an operation is bound to."""

    HTTP = "http"
    SOAP = "soap"
    #: A statement executed against a database. Not a wire protocol in the way the others are,
    #: and it belongs here for the same reason they do: it is what a caller has to speak.
    SQL = "sql"
    #: A message published to or delivered from a channel. The transport underneath varies and
    #: the document names it; what matters here is that the exchange is not request-response.
    ASYNC = "async"
    #: A GraphQL root field. One endpoint serves them all, which is why an operation carries no
    #: route: the schema never names the URL.
    GRAPHQL = "graphql"
    #: A gRPC method. The route is the canonical path protoc derives, which every gRPC client
    #: builds the same way, so it is normalization rather than invention.
    GRPC = "grpc"


class SideEffectClass(StrEnum):
    """Effect an operation has on server-side state.

    This describes the source operation only. Authorization sensitivity is a property of an
    exposed tool and lives in `RiskClass`.
    """

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


class RiskClass(StrEnum):
    """Risk classification of an exposed tool-surface artifact."""

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    PRIVILEGED = "privileged"
    UNKNOWN = "unknown"


class AuthSchemeType(StrEnum):
    """Authentication scheme kinds declared by an OpenAPI security scheme.

    `other` exists so that a specification using a scheme this version does not model still
    compiles, with an ambiguity recorded, rather than failing the parse.
    """

    HTTP = "http"
    API_KEY = "apiKey"
    OAUTH2 = "oauth2"
    OPEN_ID_CONNECT = "openIdConnect"
    MUTUAL_TLS = "mutualTLS"
    OTHER = "other"


class ParameterStyle(StrEnum):
    """OpenAPI parameter serialization styles.

    The set is closed by the specification. A value outside it is a defect in the source
    document and is recorded as an ambiguity rather than carried into the IR.
    """

    MATRIX = "matrix"
    LABEL = "label"
    FORM = "form"
    SIMPLE = "simple"
    SPACE_DELIMITED = "spaceDelimited"
    PIPE_DELIMITED = "pipeDelimited"
    DEEP_OBJECT = "deepObject"


class PaginationStyle(StrEnum):
    """Shape of a proposed pagination mechanism."""

    CURSOR = "cursor"
    PAGE_NUMBER = "page_number"
    OFFSET_LIMIT = "offset_limit"
    LINK_HEADER = "link_header"


class DocumentRole(StrEnum):
    """Why a document was loaded during a compile."""

    ROOT = "root"
    REFERENCED = "referenced"


# Decision: this mapping is total and deliberately does not produce
# `privileged`. Ingestion must not make authorization judgements; only the semantic planner
# and policy synthesis may raise an artifact to `privileged`, and only with a rationale.
SIDE_EFFECT_TO_RISK: dict[SideEffectClass, RiskClass] = {
    SideEffectClass.READ: RiskClass.READ,
    SideEffectClass.WRITE: RiskClass.WRITE,
    SideEffectClass.DESTRUCTIVE: RiskClass.DESTRUCTIVE,
    SideEffectClass.UNKNOWN: RiskClass.UNKNOWN,
}


class Idempotency(StrEnum):
    """Whether repeating an operation with identical input is safe."""

    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


class Derivation(StrEnum):
    """How a value in the IR was obtained.

    `source` and `normalized` are facts and always carry confidence 1.0. `inferred` is a
    proposal and must carry confidence below 1.0. `default` records that the source was
    inspected and held no evidence, so the contract default applies.
    """

    SOURCE = "source"
    NORMALIZED = "normalized"
    INFERRED = "inferred"
    DEFAULT = "default"


class ParameterLocation(StrEnum):
    """Where an input value is carried on the wire."""

    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    COOKIE = "cookie"
    BODY = "body"
    SOAP_HEADER = "soap_header"
    SOAP_BODY = "soap_body"


class ArtifactKind(StrEnum):
    """Kind of MCP surface an artifact proposes."""

    TOOL = "tool"
    RESOURCE = "resource"
    PROMPT = "prompt"
    COMPOSITE = "composite"
    OMITTED = "omitted"


class ReviewStatus(StrEnum):
    """Human review state of a plan artifact.

    Nothing may be generated as executable while this is `proposed`.
    """

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class PlannerKind(StrEnum):
    """Which planner produced a tool plan.

    `baseline` exists only for controlled comparison and is never a production surface.
    """

    BASELINE = "baseline"
    SEMANTIC = "semantic"


class EmissionStatus(StrEnum):
    """Whether a generated tool may actually be invoked."""

    EXECUTABLE = "executable"
    DISABLED = "disabled"


class EmissionBlocker(StrEnum):
    """Why a generated tool was refused executable status.

    A refused tool is still emitted, carrying its blockers, so the surface stays auditable.
    Omitting it would make it indistinguishable from one that was never planned.
    """

    BLOCKING_AMBIGUITY = "blocking_ambiguity"
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"
    UNCLASSIFIED_RISK = "unclassified_risk"
    ARGUMENT_NAME_COLLISION = "argument_name_collision"
    COMPOSITE_PENDING_CONFIRMATION = "composite_pending_confirmation"
    POLICY_UNRESOLVED = "policy_unresolved"


class DataSensitivity(StrEnum):
    """How sensitive the data an operation touches is.

    Deliberately a separate axis from the side-effect class. Sensitivity is often listed
    beside read, write and destructive, but a *read* of financial data is still financial,
    so folding it into the side-effect enum would make it unrepresentable on exactly the
    operations that leak it. This is the reasoning that split `privileged` out of
    `SideEffectClass` in the first place.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    PERSONAL = "personal"
    FINANCIAL = "financial"


class ApprovalClass(StrEnum):
    """How much human involvement an invocation requires."""

    NONE = "none"
    USER_CONFIRMATION = "user_confirmation"
    HUMAN_APPROVAL = "human_approval"
    DISABLED = "disabled"


class LogClass(StrEnum):
    """How much of an invocation may be recorded."""

    MINIMAL = "minimal"
    STANDARD = "standard"
    SENSITIVE = "sensitive"


class RetryPolicy(StrEnum):
    """Whether a failed invocation may be retried."""

    NEVER = "never"
    SAFE = "safe"
    WITH_IDEMPOTENCY_KEY = "with_idempotency_key"


class Environment(StrEnum):
    """Where a tool is permitted to run."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


#: Ordered least to most sensitive. Classification may raise a level but never lower one.
SENSITIVITY_ORDER: tuple[DataSensitivity, ...] = (
    DataSensitivity.PUBLIC,
    DataSensitivity.INTERNAL,
    DataSensitivity.CONFIDENTIAL,
    DataSensitivity.PERSONAL,
    DataSensitivity.FINANCIAL,
)


class DecisionKind(StrEnum):
    """A semantic planning decision that changes the surface an agent sees."""

    RENAME = "rename"
    DESCRIBE = "describe"
    RECLASSIFY = "reclassify"
    OMIT = "omit"
    GROUP = "group"
    PROJECT = "project"
    COMPOSE = "compose"
    APPROVE = "approve"


class DecisionOrigin(StrEnum):
    """Who made a decision.

    A planner decision is a proposal and must carry confidence below 1.0. A human decision
    recorded in an overlay is a fact and carries 1.0. The distinction is what lets a review
    report separate what was suggested from what was accepted.
    """

    PLANNER = "planner"
    HUMAN = "human"


class Provenance(BaseModel):
    """One record explaining the origin of one IR field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str = Field(description="Dotted path of the field this record explains.")
    source_pointer: str = Field(description="Scheme-prefixed pointer into the source document.")
    derivation: Derivation
    rule: str = Field(description="Stable identifier of the rule that produced the value.")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _confidence_matches_derivation(self) -> Provenance:
        """Reject confidence values that contradict the derivation kind.

        A fact presented with uncertainty, or a guess presented as certain, would let an
        inference reach the human approval gate disguised as source data.
        """
        if self.derivation is Derivation.INFERRED:
            if self.confidence >= 1.0:
                raise ValueError(
                    f"inferred provenance for {self.field!r} must have confidence below 1.0"
                )
        elif self.confidence != 1.0:
            raise ValueError(
                f"{self.derivation.value} provenance for {self.field!r} must have confidence 1.0"
            )
        return self


def _is_provenance_bearing(value: object) -> bool:
    """Report whether a field value carries its own provenance records."""
    if isinstance(value, ProvenanceBearing):
        return True
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(isinstance(item, ProvenanceBearing) for item in value)
    )


def _is_empty(value: object) -> bool:
    """Report whether a field value carries no information."""
    return value is None or (isinstance(value, str | list | dict | tuple | set) and len(value) == 0)


class ProvenanceBearing(BaseModel):
    """Base class enforcing that no informative field exists without provenance.

    The invariant is checked by the contract rather than trusted to parser discipline,
    because a missing provenance record is invisible at review time. A field is exempt only
    when it is empty (no information to trace) or when its value is itself a
    provenance-bearing model that carries its own records.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provenance: list[Provenance] = Field(default_factory=list)

    #: Fields never required to carry provenance, beyond the generic exemptions.
    provenance_exempt: ClassVar[frozenset[str]] = frozenset({"provenance"})

    @model_validator(mode="after")
    def _every_informative_field_has_provenance(self) -> ProvenanceBearing:
        covered = {record.field for record in self.provenance}
        missing: list[str] = []
        for name in type(self).model_fields:
            if name in self.provenance_exempt:
                continue
            value = getattr(self, name)
            if _is_empty(value) or _is_provenance_bearing(value):
                continue
            if name in covered or any(item.startswith(f"{name}.") for item in covered):
                continue
            missing.append(name)
        if missing:
            raise ValueError(
                f"{type(self).__name__} is missing provenance for: {', '.join(sorted(missing))}"
            )
        declared = set(type(self).model_fields)
        unknown = sorted(
            item for item in covered if item.split(".", 1)[0] not in declared
        )
        if unknown:
            raise ValueError(
                f"{type(self).__name__} has provenance for unknown fields: {', '.join(unknown)}"
            )
        return self


class Ambiguity(BaseModel):
    """An unresolved construct recorded instead of being silently dropped.

    `blocking` means no executable artifact may be generated from the affected operation
    until the construct is resolved or explicitly waived by a human reviewer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(description="Stable machine-readable ambiguity code.")
    field: str = Field(description="Dotted path of the affected field.")
    source_pointer: str
    detail: str
    blocking: bool


class ServerIR(ProvenanceBearing):
    """A base endpoint the service is reachable at."""

    url: str
    description: str | None = None


class SourceDocumentIR(BaseModel):
    """One document loaded during a compile, with the digest of its exact bytes.

    Reference resolution can pull in documents beyond the root, so the root digest alone no
    longer identifies the input. An artifact is reproducible when this whole list matches.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    role: DocumentRole


class AuthSchemeIR(ProvenanceBearing):
    """An authentication scheme declared by the source specification.

    A single model with a closed `type` and conditional validation, rather than a
    discriminated union: the variants differ by two or three fields, and a flat contract
    stays readable across languages. `detail` keeps the source object verbatim so that
    nothing is lost to normalization, because scope minimization is the policy synthesiser's
    concern rather than the parser's.
    """

    scheme_id: str
    type: AuthSchemeType
    description: str | None = None
    http_scheme: str | None = Field(default=None, description="`basic`, `bearer`, and so on.")
    bearer_format: str | None = None
    api_key_in: str | None = Field(default=None, description="`query`, `header` or `cookie`.")
    api_key_name: str | None = None
    open_id_connect_url: str | None = None
    scopes: list[str] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _fields_match_type(self) -> AuthSchemeIR:
        """Reject a scheme carrying fields that do not belong to its type.

        Without this, an `apiKey` scheme could silently carry a `bearer_format` and a
        downstream policy generator would have no way to know which field to trust.
        """
        required: dict[AuthSchemeType, tuple[str, ...]] = {
            AuthSchemeType.HTTP: ("http_scheme",),
            AuthSchemeType.API_KEY: ("api_key_in", "api_key_name"),
            AuthSchemeType.OPEN_ID_CONNECT: ("open_id_connect_url",),
        }
        allowed: dict[AuthSchemeType, tuple[str, ...]] = {
            AuthSchemeType.HTTP: ("http_scheme", "bearer_format"),
            AuthSchemeType.API_KEY: ("api_key_in", "api_key_name"),
            AuthSchemeType.OAUTH2: ("scopes",),
            AuthSchemeType.OPEN_ID_CONNECT: ("open_id_connect_url", "scopes"),
            AuthSchemeType.MUTUAL_TLS: (),
            AuthSchemeType.OTHER: (),
        }
        variant_fields = {
            "http_scheme",
            "bearer_format",
            "api_key_in",
            "api_key_name",
            "open_id_connect_url",
            "scopes",
        }
        for name in required.get(self.type, ()):
            if getattr(self, name) is None:
                raise ValueError(f"{self.type.value} scheme {self.scheme_id!r} requires {name}")
        permitted = set(allowed[self.type])
        for name in sorted(variant_fields - permitted):
            value = getattr(self, name)
            if not _is_empty(value):
                raise ValueError(
                    f"{self.type.value} scheme {self.scheme_id!r} must not carry {name}"
                )
        return self


class SecurityRequirementIR(ProvenanceBearing):
    """One security requirement object: a set of schemes that together satisfy access."""

    scheme_ids: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)


class AuthRequirementIR(ProvenanceBearing):
    """What an operation requires in order to authenticate.

    `disabled` records an explicit empty `security` list, which means the operation needs no
    authentication. That is materially different from declaring nothing, and policy synthesis
    cannot fail closed on an unauthenticated operation it cannot see.

    `alternatives` keeps each requirement object separately rather than only their union.
    The union over-approximates, and least-privilege selection needs to compare the
    alternatives to pick the narrowest one that satisfies access.
    """

    disabled: bool = False
    scheme_ids: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(
        default_factory=list, description="Union across alternatives, for convenience only."
    )
    alternatives: list[SecurityRequirementIR] = Field(default_factory=list)


class ExampleIR(ProvenanceBearing):
    """One example value attached to a parameter, request body or response."""

    name: str | None = Field(default=None, description="Key in `examples`; null for `example`.")
    summary: str | None = None
    value: Any = None
    external_value: str | None = None


class HeaderIR(ProvenanceBearing):
    """One response header.

    Headers carry the strongest machine-readable pagination and async-job signals a
    specification offers, so dropping them discards evidence nothing else replaces.
    """

    name: str
    description: str | None = None
    required: bool = False
    deprecated: bool = False
    type_schema: dict[str, Any] | None = None


class AsyncJobIR(ProvenanceBearing):
    """An operation that accepts work rather than performing it.

    Inferred, never stated. HTTP 202 means the request was accepted and not that it was
    carried out, and a `Location` header on that response is where the document says progress
    can be read. An agent told nothing about this treats acceptance as completion, which is
    among the most misleading things a tool surface can do: the goal looks met, and nothing
    has happened yet.

    `poll_header` stays null when a document declares acceptance without saying where to
    look, which is worth reporting rather than filling in with a convention.
    """

    status: str
    poll_header: str | None = None


class PaginationIR(ProvenanceBearing):
    """A proposed pagination mechanism.

    Always inferred, never asserted. Each populated field names the specific parameter,
    response field or header the guess came from, so a reviewer can confirm it without
    re-reading the specification.
    """

    style: PaginationStyle
    cursor_parameter: str | None = None
    page_parameter: str | None = None
    size_parameter: str | None = None
    offset_parameter: str | None = None
    next_cursor_field: str | None = None
    next_link_header: str | None = None


class FieldIR(ProvenanceBearing):
    """One input value accepted by an operation.

    `type_schema` holds the source schema with references resolved in place; the original
    `$ref` site survives in the provenance records.
    """

    name: str
    location: ParameterLocation
    required: bool
    description: str | None = None
    media_type: str | None = None
    type_schema: dict[str, Any] | None = None
    style: ParameterStyle | None = None
    explode: bool | None = None
    allow_reserved: bool | None = None
    deprecated: bool = False
    examples: list[ExampleIR] = Field(default_factory=list)


class ResponseIR(ProvenanceBearing):
    """One declared response of an operation."""

    status: str
    description: str | None = None
    media_type: str | None = None
    type_schema: dict[str, Any] | None = None
    headers: list[HeaderIR] = Field(default_factory=list)
    examples: list[ExampleIR] = Field(default_factory=list)


class FaultIR(ProvenanceBearing):
    """One declared fault or error response of an operation.

    `retryable` stays `None` unless the source states it. Ingestion does not guess
    retryability, because a wrong guess drives unsafe agent retry behaviour.
    """

    code: str
    description: str | None = None
    media_type: str | None = None
    type_schema: dict[str, Any] | None = None
    retryable: bool | None = None
    headers: list[HeaderIR] = Field(default_factory=list)
    examples: list[ExampleIR] = Field(default_factory=list)


class SoapBindingIR(ProvenanceBearing):
    """SOAP-specific binding metadata for an operation.

    Captures the binding surface present in the WSDL document itself. XSD types are resolved
    into JSON Schema; typed faults, SOAP headers, WS-Security and MTOM are not.
    """

    target_namespace: str
    port_type: str
    binding: str | None = None
    port: str | None = None
    style: str | None = None
    transport: str | None = None
    soap_action: str | None = None
    endpoint: str | None = None
    input_message: str | None = None
    output_message: str | None = None


class OperationIR(ProvenanceBearing):
    """A single normalized operation, independent of source format.

    `confidence` is the aggregate agent-suitability score for the operation. Ingestion leaves
    it unset rather than fabricating a value; per-field confidence lives in the provenance
    records.
    """

    operation_id: str
    protocol: Protocol
    source_pointer: str
    route: str | None = Field(
        default=None,
        description="Request path for HTTP operations. Null for SOAP, where the port type "
        "plays the grouping role instead.",
    )
    intent: str
    side_effect: SideEffectClass
    idempotency: Idempotency = Idempotency.UNKNOWN
    description: str | None = None
    deprecated: bool = False
    tags: list[str] = Field(
        default_factory=list,
        description="The groupings the document itself declares for this operation. Kept "
        "because a specification's own taxonomy is a source fact, and the alternative is a "
        "grouping the planner invents from path shape.",
    )
    inputs: list[FieldIR] = Field(default_factory=list)
    outputs: list[ResponseIR] = Field(default_factory=list)
    faults: list[FaultIR] = Field(default_factory=list)
    authentication: AuthRequirementIR | None = None
    servers: list[ServerIR] = Field(
        default_factory=list,
        description="Overrides the service endpoints when the operation declares its own.",
    )
    pagination: PaginationIR | None = None
    async_job: AsyncJobIR | None = None
    soap: SoapBindingIR | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ServiceIR(ProvenanceBearing):
    """Identity and transport-level metadata for one compiled service."""

    service_id: str
    title: str
    version: str | None = None
    spec_version: str | None = Field(
        default=None, description="Version declared by the source document, such as `3.1.0`."
    )
    source_format: SourceFormat
    source_uri: str | None = None
    source_digest: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="Prefixed sha256 digest of the root document's raw bytes.",
    )
    description: str | None = Field(
        default=None,
        description="What the document says the service is. Carried through so an agent can "
        "be told the domain it is working in rather than inferring it from tool names.",
    )
    terms_of_service: str | None = None
    source_documents: list[SourceDocumentIR] = Field(default_factory=list)
    servers: list[ServerIR] = Field(default_factory=list)
    auth_schemes: list[AuthSchemeIR] = Field(default_factory=list)


class ApiSemanticIR(BaseModel):
    """The provider-independent intermediate representation of one service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = IR_SCHEMA_VERSION
    service: ServiceIR
    operations: list[OperationIR] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _pinned_version(cls, value: str) -> str:
        if value != IR_SCHEMA_VERSION:
            raise ValueError(f"expected IR schema_version {IR_SCHEMA_VERSION}, got {value!r}")
        return value

    @property
    def blocking_ambiguities(self) -> list[Ambiguity]:
        """Ambiguities that must be resolved before any executable artifact is generated."""
        return [item for item in self.ambiguities if item.blocking]


class ToolArtifact(ProvenanceBearing):
    """One proposed MCP surface artifact.

    `review_status` gates emission: nothing may be generated as executable while the value
    is `proposed`.
    """

    artifact_id: str
    kind: ArtifactKind = ArtifactKind.TOOL
    name: str
    description: str
    source_operations: list[str]
    risk: RiskClass
    review_status: ReviewStatus = ReviewStatus.PROPOSED
    rationale: str
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Agent-suitability score. Assigned by the semantic planner, because "
        "suitability is a planning judgement and writing it back into the IR would make "
        "ingestion depend on a planner.",
    )
    group: str | None = None
    output_fields: list[str] = Field(
        default_factory=list,
        description="Top-level response fields the tool projects. Empty means no projection.",
    )
    omitted_arguments: list[str] = Field(
        default_factory=list,
        description="Arguments withheld from the agent's input schema. They are optional in "
        "the source and are left off the wire, so the service applies its own default. "
        "Projection changes what an agent must reason about, never what the tool can do.",
    )


class ToolPlan(BaseModel):
    """A complete, versioned tool-surface plan for one service.

    `source_digest` ties the plan to the exact specification bytes it was compiled from, so
    a plan can be shown to be reproducible or shown to be stale.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = TOOL_PLAN_SCHEMA_VERSION
    service_id: str
    planner: PlannerKind
    source_digest: str
    artifacts: list[ToolArtifact] = Field(default_factory=list)
    decisions: list[PlanDecision] = Field(
        default_factory=list,
        description="Every rename, omission, grouping, projection and composite, with its "
        "rationale. The baseline planner makes no decisions and emits none.",
    )

    @field_validator("schema_version")
    @classmethod
    def _pinned_version(cls, value: str) -> str:
        if value != TOOL_PLAN_SCHEMA_VERSION:
            raise ValueError(
                f"expected tool plan schema_version {TOOL_PLAN_SCHEMA_VERSION}, got {value!r}"
            )
        return value


class ArgumentBinding(ProvenanceBearing):
    """How one composed tool argument maps back onto the wire.

    The input schema is flat, because an agent produces a flat argument object and should
    not have to reason about transport. The binding is what lets a runtime put each value
    back in the right place.
    """

    argument: str
    location: ParameterLocation
    wire_name: str
    media_type: str | None = None
    source_operation: str | None = Field(
        default=None,
        description="Which of a composite's steps this value belongs to. A flat schema cannot "
        "say that two steps each take a body, so the argument is disambiguated by name and "
        "this records which request it actually belongs on.",
    )


class ToolAnnotationsIR(ProvenanceBearing):
    """The hints MCP clients read when deciding whether to auto-approve a call.

    The protocol defines these as hints and says plainly that they are not guaranteed to
    describe behaviour faithfully, that a client must treat them as untrusted unless the
    server is trusted, and that an untrusted server can lie. There is no verification
    mechanism anywhere in the ecosystem, and most clients treat installation as the trust
    signal.

    That is a gap this compiler is unusually placed to close, because it does not assert these
    facts: it derives them from the specification and records where each came from. A hint
    emitted here carries the same provenance as the classification behind it, so the question
    "on what basis does this tool claim to be read-only" has an answer.

    `open_world` is deliberately absent. It asks whether a tool interacts with entities outside
    a closed domain, and nothing in an OpenAPI or WSDL document answers that. Guessing it would
    put an invented value beside three derived ones, which is how a set of trustworthy hints
    becomes a set nobody checks.

    `sensitive` and `reversible` are not in the specification. Both were proposed and neither
    was merged, and the policy manifest computes both already, so they are emitted as
    extensions rather than withheld until a committee agrees.
    """

    read_only: bool
    destructive: bool
    idempotent: bool
    sensitive: bool | None = Field(
        default=None,
        description="Null where no policy was synthesised, because sensitivity is derived "
        "there. False would assert that a tool touches nothing sensitive, which is a claim "
        "and not the absence of one.",
    )
    reversible: bool | None = Field(
        default=None,
        description="Null where the source says nothing about whether an effect can be "
        "undone, which is most of the time and is not the same as saying it cannot.",
    )


class ToolDescriptor(ProvenanceBearing):
    """One generated tool, independent of any transport or SDK.

    `emission` is the safety gate. A tool is executable only when its source operation
    carries no blocking ambiguity, its risk is classified, and any write, destructive or
    privileged artifact has been approved by a human.
    """

    tool_id: str
    name: str
    description: str
    kind: ArtifactKind = ArtifactKind.TOOL
    risk: RiskClass
    emission: EmissionStatus
    blockers: list[EmissionBlocker] = Field(default_factory=list)
    blocker_detail: str | None = None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    argument_bindings: list[ArgumentBinding] = Field(default_factory=list)
    annotations: ToolAnnotationsIR | None = Field(
        default=None,
        description="MCP tool annotations, derived rather than asserted. Null only where no "
        "policy was supplied, since the classification behind them always exists.",
    )
    uri_template: str | None = Field(
        default=None,
        description="For a resource, the addressable form a client reads it by. A resource "
        "without one is a tool wearing the word: the planner reclassified an addressable "
        "read and nothing downstream could express the address.",
    )
    source_operations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_matches_blockers(self) -> ToolDescriptor:
        """Keep the status and the blocker list from disagreeing.

        A tool marked executable while carrying a blocker would defeat the gate silently,
        which is the exact failure this contract exists to prevent.
        """
        if self.emission is EmissionStatus.EXECUTABLE and self.blockers:
            raise ValueError(f"tool {self.name!r} is executable but carries blockers")
        if self.emission is EmissionStatus.DISABLED and not self.blockers:
            raise ValueError(f"tool {self.name!r} is disabled with no blocker recorded")
        return self


class ToolSurface(BaseModel):
    """A complete generated tool surface for one service.

    Carries the same `source_digest` as the IR and the plan it was generated from, so a
    surface can never be silently applied to a different revision of the specification.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = TOOL_SURFACE_SCHEMA_VERSION
    service_id: str
    planner: PlannerKind
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tools: list[ToolDescriptor] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _pinned_version(cls, value: str) -> str:
        if value != TOOL_SURFACE_SCHEMA_VERSION:
            raise ValueError(
                f"expected tool surface schema_version {TOOL_SURFACE_SCHEMA_VERSION}, "
                f"got {value!r}"
            )
        return value

    @property
    def executable_tools(self) -> list[ToolDescriptor]:
        """Tools that passed the emission gate."""
        return [item for item in self.tools if item.emission is EmissionStatus.EXECUTABLE]


class PlanDecision(ProvenanceBearing):
    """One semantic decision, with the reasoning a reviewer needs to accept or reject it.

    The acceptance criteria require a rationale and a confidence for every merge, omission,
    rename and composite, so a decision without one is a contract violation rather than an
    oversight.
    """

    kind: DecisionKind
    origin: DecisionOrigin
    target: str = Field(description="Artifact or operation the decision applies to.")
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)
    applied: bool = Field(
        description="True when the decision shaped the emitted surface. A planner proposal "
        "the overlay did not accept is recorded but not applied."
    )
    previous_value: str | None = None
    proposed_value: str | None = None
    members: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _confidence_matches_origin(self) -> PlanDecision:
        """A proposal may not present itself as certain, and a human decision is not a guess."""
        if self.origin is DecisionOrigin.PLANNER and self.confidence >= 1.0:
            raise ValueError(
                f"planner decision {self.kind.value} on {self.target!r} must have confidence "
                "below 1.0"
            )
        if self.origin is DecisionOrigin.HUMAN and self.confidence != 1.0:
            raise ValueError(
                f"human decision {self.kind.value} on {self.target!r} must have confidence 1.0"
            )
        return self


class OverlayEntry(BaseModel):
    """Human decisions recorded against one source operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    review_status: ReviewStatus | None = None
    name: str | None = None
    kind: ArtifactKind | None = None
    side_effect: SideEffectClass | None = Field(
        default=None,
        description="The side effect a reviewer determined for this operation. WSDL carries no "
        "signal equivalent to an HTTP method, so a SOAP operation cannot be classified by "
        "inference and stays blocked until a person records the decision here. Without this "
        "field the compiler demanded a judgement it gave nobody a way to express.",
    )
    omit: bool = False
    output_fields: list[str] = Field(
        default_factory=list,
        description="Top-level response fields to keep. Empty means no projection.",
    )
    group: str | None = None


class CompositeEntry(BaseModel):
    """A human-accepted composite workflow over several source operations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    composite_id: str
    name: str
    description: str
    steps: list[str] = Field(min_length=2, description="Source operation identifiers, in order.")
    review_status: ReviewStatus = ReviewStatus.PROPOSED
    supersedes_steps: bool = Field(
        default=False,
        description="Whether the composite replaces its steps on the surface rather than "
        "joining them. Composing is meant to reduce what an agent chooses between, and a "
        "composite that only adds to the surface makes that choice harder rather than easier. "
        "It defaults to false because approving a tool should never silently remove others; "
        "a reviewer who wants the reduction has to say so.",
    )


class ToolOverlay(BaseModel):
    """Human decisions about a service's tool surface, kept beside the specification.

    The overlay is what reconciles semantic judgement with reproducibility: the planner
    proposes, the overlay records what a human accepted, and regeneration is a pure function
    of specification plus overlay. `source_digest` ties it to the revision it was reviewed
    against, so a stale overlay is refused rather than silently applied.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = TOOL_OVERLAY_SCHEMA_VERSION
    service_id: str
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    entries: list[OverlayEntry] = Field(default_factory=list)
    composites: list[CompositeEntry] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _pinned_version(cls, value: str) -> str:
        if value != TOOL_OVERLAY_SCHEMA_VERSION:
            raise ValueError(
                f"expected overlay schema_version {TOOL_OVERLAY_SCHEMA_VERSION}, got {value!r}"
            )
        return value

    def entry(self, operation_id: str) -> OverlayEntry | None:
        """Return the recorded decisions for one operation, if any."""
        return next(
            (item for item in self.entries if item.operation_id == operation_id), None
        )


class RateBudget(ProvenanceBearing):
    """Call rate, concurrency and daily budget for one tool."""

    calls_per_minute: int | None = Field(default=None, ge=1)
    max_concurrent: int | None = Field(default=None, ge=1)
    daily_call_budget: int | None = Field(default=None, ge=1)


class OutputPolicy(ProvenanceBearing):
    """What a tool is allowed to return.

    `max_bytes` is a hard ceiling on serialised output. Exceeding it returns a structured
    refusal rather than truncated data, because truncating a payload produces something that
    parses as an answer while being wrong.
    """

    max_bytes: int = Field(ge=1)
    projected_fields: list[str] = Field(default_factory=list)
    redact_fields: list[str] = Field(default_factory=list)


class ConfirmationPolicy(ProvenanceBearing):
    """A two-call confirmation requirement.

    A boolean flag on a single call would be satisfied by the same model turn that decided
    to act, which is not confirmation. A prepare call therefore issues a token naming the
    effect, and the execute call refuses without it. The token is bound to the arguments it
    was issued for, so confirming one action cannot authorise a different one.
    """

    required: bool = True
    effect_summary: str
    token_ttl_seconds: int = Field(default=300, ge=1)


class ToolPolicy(ProvenanceBearing):
    """The governance envelope for one generated tool.

    `unresolved` names anything that could not be derived. A tool with unresolved policy is
    refused emission rather than defaulted, because a defaulted policy is indistinguishable
    from a derived one once written.
    """

    artifact_id: str
    tool_name: str
    required_scopes: list[str] = Field(default_factory=list)
    # Which credential to present, not merely how much of it is needed. The scopes alone say
    # what access is required and leave a server unable to authenticate at all: a generated
    # client sent every credential as a bearer token because this was the only thing recorded.
    required_schemes: list[str] = Field(default_factory=list)
    approval: ApprovalClass
    confirmation: ConfirmationPolicy | None = None
    retry: RetryPolicy
    idempotency_key_required: bool = False
    rate: RateBudget
    allowed_environments: list[Environment] = Field(default_factory=list)
    log_class: LogClass
    sensitivity: DataSensitivity
    output: OutputPolicy
    rollback_guidance: str | None = None
    unresolved: list[str] = Field(default_factory=list)


class PolicyManifest(BaseModel):
    """A reviewable governance artifact for one service's tool surface.

    Generated separately from code, because a generator that also decided policy could be
    reviewed as neither.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = POLICY_MANIFEST_SCHEMA_VERSION
    service_id: str
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    policies: list[ToolPolicy] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _pinned_version(cls, value: str) -> str:
        if value != POLICY_MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                f"expected policy schema_version {POLICY_MANIFEST_SCHEMA_VERSION}, got {value!r}"
            )
        return value

    def policy_for(self, artifact_id: str) -> ToolPolicy | None:
        """Return the policy governing one artifact, if one was derived."""
        return next((item for item in self.policies if item.artifact_id == artifact_id), None)


class OracleKind(StrEnum):
    """How a task decides whether it succeeded.

    `final_state` is the one the acceptance criteria single out: success is judged by the
    state the service ends in, not by whether the trace resembled an expected one.

    `retrieval` exists because a read changes no state, so every other oracle here is
    negative and an agent that does nothing at all satisfies them. Without a positive
    requirement that the asked-for information was actually returned, a read task is
    vacuous.
    """

    FINAL_STATE = "final_state"
    RETRIEVAL = "retrieval"
    NO_MUTATION = "no_mutation"
    CONFIRMATION_ADHERENCE = "confirmation_adherence"
    PROHIBITED_OPERATIONS = "prohibited_operations"


class StepOutcome(StrEnum):
    """What happened on one attempted tool call."""

    OK = "ok"
    REFUSED_DISABLED = "refused_disabled"
    REFUSED_ARGUMENTS = "refused_arguments"
    REFUSED_CONFIRMATION = "refused_confirmation"
    REFUSED_OUTPUT = "refused_output"
    UNMAPPED = "unmapped"


class StateAssertion(BaseModel):
    """One deterministic claim about the service state a task should leave behind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    collection: str
    count: int | None = Field(default=None, ge=0)
    record_id: str | None = None
    exists: bool | None = None
    field: str | None = None
    equals: Any = None
    contains: str | None = Field(
        default=None,
        description="Assert the field's value references this, rather than equals it. A "
        "service names things in more than one form, and an agent that sends the fuller one "
        "is right: `spotify:playlist:pl-rock` and `pl-rock` identify the same playlist. "
        "Equality then fails a correct call, which is a defect in the oracle, not the agent.",
    )


class RetrievalAssertion(BaseModel):
    """One claim that a value was actually returned to the caller.

    Checked against the recorded response, never by a judge. A read task needs at least one
    of these, or it can be passed by making no calls at all.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str | None = Field(
        default=None,
        description="Restrict the check to responses from this operation. Usually a mistake: "
        "a goal asks for an outcome, and pinning the operation asserts a route instead, so an "
        "agent that reaches the same outcome by a better route is marked wrong. Set it only "
        "when the goal genuinely names the operation.",
    )
    field: str | None = Field(
        default=None, description="Dotted path into the response; null searches anywhere."
    )
    equals: Any = None
    contains: str | None = Field(
        default=None,
        description="Assert the returned value references this, rather than equals it.",
    )


class TaskOracle(BaseModel):
    """One check applied to a completed task run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: OracleKind
    assertions: list[StateAssertion] = Field(default_factory=list)
    retrieval_assertions: list[RetrievalAssertion] = Field(default_factory=list)
    description: str

    @model_validator(mode="after")
    def _retrieval_needs_an_assertion(self) -> TaskOracle:
        """A retrieval oracle with nothing to assert would pass on an empty run."""
        if self.kind is OracleKind.RETRIEVAL and not self.retrieval_assertions:
            raise ValueError("a retrieval oracle must carry at least one assertion")
        return self


class ReferenceStep(BaseModel):
    """One step of a solution the task author asserts is correct.

    This exists so a deterministic driver can exercise the harness end to end. It is not a
    scoring key: a run is judged by its oracles, and a driver that reproduces these steps
    exactly can still fail if the resulting state is wrong.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class EvalTask(BaseModel):
    """One benchmark task, expressed against source operations rather than tool names.

    Naming operations is what lets a single corpus score two different surfaces. A task that
    named tools could describe one planner's output and would be unable to describe the
    other, which defeats the comparison the corpus exists to make.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    goal: str = Field(description="Natural-language user goal.")
    fixture: dict[str, dict[str, dict[str, Any]]] = Field(
        default_factory=dict,
        description="Starting service state, as collection to record identifier to record.",
    )
    identity: dict[str, Any] = Field(default_factory=dict)
    allowed_operations: list[str] = Field(default_factory=list)
    prohibited_operations: list[str] = Field(default_factory=list)
    oracles: list[TaskOracle] = Field(min_length=1)
    reference_solution: list[ReferenceStep] = Field(default_factory=list)
    max_calls: int = Field(default=8, ge=1)


class EvalCorpus(BaseModel):
    """A versioned set of benchmark tasks for one service.

    An earlier corpus was a bare JSON array with no envelope, so nothing recorded which
    service the tasks addressed or which revision of it they were written against.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = EVAL_CORPUS_SCHEMA_VERSION
    corpus_id: str
    service_id: str
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    authoring_note: str = Field(
        description="How the tasks were authored, so a reader can judge their independence."
    )
    tasks: list[EvalTask] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _pinned_version(cls, value: str) -> str:
        if value != EVAL_CORPUS_SCHEMA_VERSION:
            raise ValueError(
                f"expected corpus schema_version {EVAL_CORPUS_SCHEMA_VERSION}, got {value!r}"
            )
        return value


class TraceStep(BaseModel):
    """One attempted call, recorded for evaluation.

    Kept separate from `AuditEvent`, which records digests and never values. An evaluator
    needs the values to judge argument validity; an audit trail that carried them would
    defeat the sensitivity classification that produced it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    operation_id: str
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    outcome: StepOutcome
    detail: str | None = None
    confirmed: bool = False
    response: Any = Field(
        default=None, description="What the call returned, so retrieval can be checked."
    )
    response_bytes: int = Field(default=0, ge=0)


class OracleResult(BaseModel):
    """Whether one oracle passed, and why not if it did not."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: OracleKind
    passed: bool
    detail: str


class TaskResult(BaseModel):
    """Everything measured about one task on one surface.

    `latency_ms` and `token_cost` stay null under a deterministic driver. They have no source
    without a model in the loop, and inventing them would put fabricated numbers beside
    measured ones.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    success: bool
    oracle_results: list[OracleResult] = Field(default_factory=list)
    calls: int = Field(default=0, ge=0)
    unnecessary_calls: int = Field(default=0, ge=0)
    unmapped_operations: list[str] = Field(default_factory=list)
    selected_operations: list[str] = Field(
        default_factory=list,
        description="Distinct source operations the agent actually reached for, in the order "
        "it first reached for each. Recorded against operations rather than tool names so a "
        "baseline and a semantic surface can be compared without favouring either.",
    )
    selection_rate: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Proportion of calls that selected an operation the task permits. Null "
        "when the task declares no permitted set, because a rate over an unstated constraint "
        "would be an invented number rather than a measured one.",
    )
    invalid_argument_calls: int = Field(default=0, ge=0)
    unsafe_actions: int = Field(
        default=0, ge=0, description="Prohibited operations invoked, or disabled tools called."
    )
    confirmation_failures: int = Field(default=0, ge=0)
    context_bytes: int = Field(default=0, ge=0)
    latency_ms: float | None = None
    token_cost: int | None = None
    trace: list[TraceStep] = Field(default_factory=list)
    run_successes: list[bool] = Field(
        default_factory=list,
        description="Per-run outcomes when several runs are collapsed under a success "
        "definition. Without it a merged record can report failure with every oracle marked "
        "passed, because the oracles shown came from a run that succeeded.",
    )


class EvaluationRun(BaseModel):
    """The result of running one corpus against one surface with one driver."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = EVALUATION_RUN_SCHEMA_VERSION
    corpus_id: str
    service_id: str
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    planner: PlannerKind
    driver: str
    preregistration_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="The hypothesis this run was produced under. Required for any run that "
        "consumes a model, so a result cannot be attached to a hypothesis written afterwards.",
    )
    results: list[TaskResult] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _pinned_version(cls, value: str) -> str:
        if value != EVALUATION_RUN_SCHEMA_VERSION:
            raise ValueError(
                f"expected run schema_version {EVALUATION_RUN_SCHEMA_VERSION}, got {value!r}"
            )
        return value

    @property
    def success_rate(self) -> float:
        """Fraction of tasks whose oracles all passed."""
        if not self.results:
            return 0.0
        return round(sum(1 for item in self.results if item.success) / len(self.results), 4)


class BenchmarkSource(BaseModel):
    """One third-party document a benchmark depends on.

    The document is fetched rather than stored here. Using material and redistributing it are
    different acts, and only the second carries a licensing obligation this project would have
    to resolve, so fetching sidesteps a question rather than answering it.

    `sha256` is what makes that safe. A null digest means the source has never been recorded
    and the first fetch is trust-on-first-use; every fetch afterwards is verified, and a
    mismatch is refused rather than reported.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    description: str
    url: str = Field(pattern=r"^https://")
    pinned_ref: str | None = Field(
        default=None, description="Upstream commit or tag the URL is pinned to, when it is."
    )
    sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    target: str = Field(description="Path relative to the benchmark directory.")
    licence: str
    attribution: str = Field(
        description="Notice that must travel with the file if it is ever redistributed."
    )


class BenchmarkManifest(BaseModel):
    """Everything a benchmark needs that this repository deliberately does not contain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = BENCHMARK_MANIFEST_SCHEMA_VERSION
    sources: list[BenchmarkSource] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _pinned_version(cls, value: str) -> str:
        if value != BENCHMARK_MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                f"expected manifest schema_version {BENCHMARK_MANIFEST_SCHEMA_VERSION}, "
                f"got {value!r}"
            )
        return value


class PreRegistration(BaseModel):
    """A hypothesis and analysis fixed before any model-backed run.

    Without this, whichever numbers arrive can be narrated into a result afterwards, and the
    separation between a claim and a measurement becomes unenforceable in practice.

    The document is digested, and an evaluation run records that digest. Changing any decision
    here changes the digest, so a run cannot silently be matched to a hypothesis written after
    the fact, and a model cannot be swapped between arms without it showing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = PREREGISTRATION_SCHEMA_VERSION
    registration_id: str
    hypothesis: str
    null_hypothesis: str
    corpus_id: str
    corpus_source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    arms: list[str] = Field(min_length=2, description="The surfaces compared, by planner.")
    model: str = Field(description="Pinned before the run; both arms must use this exactly.")
    runs_per_task: int = Field(ge=1)
    success_definition: str
    equal_budget_conditions: list[str] = Field(min_length=1)
    primary_metric: str
    primary_test: str
    significance_threshold: str
    secondary_metrics: list[str] = Field(default_factory=list)
    falsification: str = Field(
        description="What result would count against the hypothesis, stated in advance."
    )
    inconclusive_condition: str
    prohibited_after_seeing_results: list[str] = Field(min_length=1)

    @field_validator("schema_version")
    @classmethod
    def _pinned_version(cls, value: str) -> str:
        if value != PREREGISTRATION_SCHEMA_VERSION:
            raise ValueError(
                f"expected preregistration schema_version {PREREGISTRATION_SCHEMA_VERSION}, "
                f"got {value!r}"
            )
        return value
