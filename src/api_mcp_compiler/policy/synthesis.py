"""Deterministic derivation of a governance manifest from an IR and a plan.

Every value here is derived from something the specification states or something the plan
decided, and anything that cannot be derived is named in `unresolved` rather than defaulted.
A defaulted policy is indistinguishable from a derived one once written, which is why the
emission gate refuses a tool whose policy is unresolved instead of letting a plausible
default through.
"""

from __future__ import annotations

import re

from api_mcp_compiler.models import (
    ApiSemanticIR,
    ApprovalClass,
    ArtifactKind,
    AuthRequirementIR,
    ConfirmationPolicy,
    DataSensitivity,
    Derivation,
    Environment,
    Idempotency,
    LogClass,
    OperationIR,
    OutputPolicy,
    PolicyManifest,
    Provenance,
    RateBudget,
    RetryPolicy,
    RiskClass,
    SecurityRequirementIR,
    ToolArtifact,
    ToolPlan,
    ToolPolicy,
)

#: Ceiling on serialised tool output. Enterprise payloads are routinely far larger than a
#: model should be handed, so the default is deliberately small and policy may not raise it
#: without a reviewer changing this constant.
DEFAULT_MAX_OUTPUT_BYTES = 32_768

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_WORD = re.compile(r"[^A-Za-z0-9]+")

_FINANCIAL_TOKENS = frozenset(
    {
        "payment", "refund", "invoice", "charge", "price", "amount", "currency",
        "iban", "card", "billing", "ledger", "payout", "settlement", "tax",
    }
)
_PERSONAL_TOKENS = frozenset(
    {
        "customer", "email", "phone", "address", "name", "birth", "dob", "ssn",
        "passport", "person", "contact", "user",
    }
)
#: Field names whose values must never reach an audit record or a model context.
_SECRET_TOKENS = frozenset(
    {"password", "secret", "token", "key", "credential", "authorization", "cookie", "pin"}
)

def _tokens(text: str) -> set[str]:
    spaced = _CAMEL_BOUNDARY.sub(" ", text)
    return {piece.lower() for piece in _NON_WORD.split(spaced) if piece}


def _operation_vocabulary(operation: OperationIR) -> set[str]:
    """Every word an operation exposes, from its name, wording, inputs and outputs."""
    words = _tokens(operation.operation_id) | _tokens(operation.intent)
    if operation.description:
        words |= _tokens(operation.description)
    if operation.route:
        words |= _tokens(operation.route)
    for field in operation.inputs:
        words |= _tokens(field.name)
        words |= _schema_tokens(field.type_schema)
    for response in operation.outputs:
        words |= _schema_tokens(response.type_schema)
    return words


def _schema_tokens(schema: dict[str, object] | None) -> set[str]:
    """Property names reachable in a schema fragment."""
    if not schema:
        return set()
    found: set[str] = set()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            found |= _tokens(str(name))
            if isinstance(child, dict):
                found |= _schema_tokens(child)
    items = schema.get("items")
    if isinstance(items, dict):
        found |= _schema_tokens(items)
    return found


def classify_sensitivity(operation: OperationIR) -> tuple[DataSensitivity, str]:
    """Classify the data an operation touches, raising the level but never lowering it."""
    words = _operation_vocabulary(operation)
    financial = sorted(_FINANCIAL_TOKENS & words)
    personal = sorted(_PERSONAL_TOKENS & words)
    if financial:
        return (
            DataSensitivity.FINANCIAL,
            f"Financial vocabulary present: {', '.join(financial)}.",
        )
    if personal:
        return (
            DataSensitivity.PERSONAL,
            f"Personal-data vocabulary present: {', '.join(personal)}.",
        )
    return (
        DataSensitivity.INTERNAL,
        "No financial or personal vocabulary was found, so the default internal "
        "classification stands. Confirm before treating the output as public.",
    )


def _names_a_secret(field_name: str) -> bool:
    """Whether a field name says the value is a credential.

    The word has to be what the field is, not merely a word inside it. A live service returns
    `TitleCaseWordsWithTokenResult`, where the token is a delimiter, and redacting it removed
    the answer while looking like the call had worked. Silent over-redaction is worse than
    none: the caller receives nothing useful and no reason.

    So the decisive token is the last one, which is the noun a field name ends on in
    `accessToken`, `api_key` and `clientSecret`, or the whole name when it is a bare secret
    word.
    """
    words = [item for item in _CAMEL_BOUNDARY.sub(" ", field_name).lower().split() if item]
    words = [piece for item in words for piece in _NON_WORD.split(item) if piece]
    if not words:
        return False
    return words[-1] in _SECRET_TOKENS or (len(words) == 1 and words[0] in _SECRET_TOKENS)


def secret_fields(operation: OperationIR) -> list[str]:
    """Response field names whose values must be redacted."""
    found: set[str] = set()
    for response in operation.outputs:
        properties = (response.type_schema or {}).get("properties")
        if isinstance(properties, dict):
            for name in properties:
                if _names_a_secret(str(name)):
                    found.add(str(name))
    return sorted(found)


def least_privilege_scopes(
    authentication: AuthRequirementIR | None,
) -> tuple[list[str], str, list[str]]:
    """Choose the narrowest security requirement that still grants access.

    Fewest scopes is not the same as least privilege. A scopeless credential such as an
    admin key looks narrowest by count while granting the most, so scoped alternatives are
    preferred and a scopeless winner is reported as a concern rather than accepted.

    Returns the scopes, the reasoning, and any concerns. A concern is only fatal for a tool
    that changes state: a read with no declared authentication is an ordinary public
    endpoint, while a destructive one with none is a red flag that must fail closed.
    """
    if authentication is None:
        return (
            [],
            "The specification declares no security for this operation, so no scopes are "
            "required.",
            ["no authentication is declared anywhere in the specification"],
        )
    if authentication.disabled:
        return (
            [],
            "The operation explicitly disables authentication with an empty security list.",
            ["the operation explicitly disables authentication"],
        )
    alternatives: list[SecurityRequirementIR] = list(authentication.alternatives)
    if not alternatives:
        return (list(authentication.scopes), "Scopes taken from the sole requirement.", [])
    if len(alternatives) == 1:
        return (
            list(alternatives[0].scopes),
            "A single security requirement, so its scopes are exactly what is needed.",
            [],
        )

    chosen = min(alternatives, key=lambda item: (not item.scopes, len(item.scopes), item.scopes))
    rejected = [
        f"{'+'.join(item.scheme_ids) or 'unnamed'}({', '.join(item.scopes) or 'no scopes'})"
        for item in alternatives
        if item is not chosen
    ]
    if not chosen.scopes:
        return (
            [],
            "Every alternative is a scopeless credential, so no least-privilege scope set "
            "can be demonstrated.",
            ["every authentication alternative uses an unscoped credential"],
        )
    union = sorted({scope for item in alternatives for scope in item.scopes})
    return (
        list(chosen.scopes),
        f"Narrowest of {len(alternatives)} alternatives, using "
        f"{'+'.join(chosen.scheme_ids)}. The union across alternatives would have granted "
        f"{', '.join(union)}; rejected {'; '.join(rejected)}.",
        [],
    )


def _approval_and_confirmation(
    artifact: ToolArtifact, operation: OperationIR
) -> tuple[ApprovalClass, ConfirmationPolicy | None, str]:
    """Decide how much human involvement an invocation requires."""
    if artifact.kind is ArtifactKind.COMPOSITE:
        return (
            ApprovalClass.USER_CONFIRMATION,
            ConfirmationPolicy(
                effect_summary=(
                    f"Runs {' then '.join(artifact.source_operations)}, including an "
                    "irreversible step."
                ),
                provenance=_records(
                    operation, ("required", "effect_summary", "token_ttl_seconds"), "confirmation"
                ),
            ),
            "A composite exists to guard an irreversible step, so it confirms before acting.",
        )
    if artifact.risk is RiskClass.DESTRUCTIVE:
        return (
            ApprovalClass.USER_CONFIRMATION,
            ConfirmationPolicy(
                effect_summary=f"{artifact.name} performs a destructive action that may not "
                "be reversible.",
                provenance=_records(
                    operation, ("required", "effect_summary", "token_ttl_seconds"), "confirmation"
                ),
            ),
            "Destructive actions require explicit confirmation before they run.",
        )
    if artifact.risk in {RiskClass.WRITE, RiskClass.PRIVILEGED}:
        return (
            ApprovalClass.HUMAN_APPROVAL,
            None,
            "A write or privileged tool requires human approval before it is enabled at all.",
        )
    if artifact.risk is RiskClass.UNKNOWN:
        return (
            ApprovalClass.DISABLED,
            None,
            "An unclassified side effect cannot be shown to be safe, so the tool is disabled.",
        )
    return (ApprovalClass.NONE, None, "A read requires no approval once validated.")


def _retry(operation: OperationIR, risk: RiskClass) -> tuple[RetryPolicy, bool, str]:
    """Derive a retry policy from the idempotency the IR inferred."""
    if risk is RiskClass.READ:
        return RetryPolicy.SAFE, False, "Reads are safe to retry."
    if operation.idempotency is Idempotency.IDEMPOTENT:
        return (
            RetryPolicy.SAFE,
            False,
            "The operation is idempotent, so repeating it has the same effect.",
        )
    if operation.idempotency is Idempotency.NON_IDEMPOTENT:
        return (
            RetryPolicy.WITH_IDEMPOTENCY_KEY,
            True,
            "The operation is not idempotent, so a retry may duplicate the effect unless it "
            "carries an idempotency key.",
        )
    return (
        RetryPolicy.NEVER,
        False,
        "Idempotency could not be determined, so retrying could duplicate an effect.",
    )


def _rate(risk: RiskClass) -> tuple[tuple[int, int, int], str]:
    """Budget calls by risk, because a mistake costs more the more the tool can do."""
    if risk is RiskClass.READ:
        return (60, 4, 5_000), "Reads are cheap to repeat, so the budget is generous."
    if risk is RiskClass.DESTRUCTIVE:
        return (
            (2, 1, 20),
            "A destructive action is budgeted tightly and serialised, so a loop cannot "
            "cascade.",
        )
    return (10, 2, 200), "A write is budgeted conservatively."


def _log_class(sensitivity: DataSensitivity) -> tuple[LogClass, str]:
    if sensitivity in {DataSensitivity.FINANCIAL, DataSensitivity.PERSONAL}:
        return (
            LogClass.SENSITIVE,
            f"{sensitivity.value} data must not be recorded in full.",
        )
    return LogClass.STANDARD, "No sensitive vocabulary was detected."


def _records(operation: OperationIR, fields: tuple[str, ...], rule: str) -> list[Provenance]:
    return [
        Provenance(
            field=name,
            source_pointer=operation.source_pointer,
            derivation=Derivation.NORMALIZED,
            rule=f"policy.{rule}.{name}",
        )
        for name in fields
    ]


def synthesize_policy(ir: ApiSemanticIR, plan: ToolPlan) -> PolicyManifest:
    """Derive one governance manifest for a planned tool surface."""
    if plan.source_digest != ir.service.source_digest:
        raise ValueError(
            f"plan was compiled from {plan.source_digest} but the IR is "
            f"{ir.service.source_digest}; policy cannot be derived across revisions"
        )
    operations = {item.operation_id: item for item in ir.operations}
    policies: list[ToolPolicy] = []

    for artifact in plan.artifacts:
        sources = [operations[name] for name in artifact.source_operations if name in operations]
        if not sources:
            continue
        primary = sources[0]
        unresolved: list[str] = []

        # A composite is only as constrained as its most demanding step.
        scope_sets = [least_privilege_scopes(item.authentication) for item in sources]
        scopes = sorted({scope for entry in scope_sets for scope in entry[0]})
        scope_rationale = " ".join(entry[1] for entry in scope_sets)
        # Decision: an authorization concern is fatal only for a tool that
        # changes state. A read with no declared authentication is an ordinary public
        # endpoint; a write or destructive one with none cannot be shown to be governed, so
        # it fails closed rather than being emitted with empty scopes.
        concerns = sorted({item for entry in scope_sets for item in entry[2]})
        if concerns and artifact.risk is not RiskClass.READ:
            unresolved.extend(f"required_scopes: {item}" for item in concerns)

        sensitivity, sensitivity_rationale = max(
            (classify_sensitivity(item) for item in sources),
            key=lambda entry: (
                entry[0] is DataSensitivity.FINANCIAL,
                entry[0] is DataSensitivity.PERSONAL,
            ),
        )
        approval, confirmation, approval_rationale = _approval_and_confirmation(artifact, primary)
        retry, needs_key, retry_rationale = _retry(primary, artifact.risk)
        (per_minute, concurrent, daily), rate_rationale = _rate(artifact.risk)
        log_class, log_rationale = _log_class(sensitivity)

        redact = sorted({name for item in sources for name in secret_fields(item)})
        environments = (
            [Environment.DEVELOPMENT, Environment.STAGING, Environment.PRODUCTION]
            if artifact.risk is RiskClass.READ
            else [Environment.DEVELOPMENT, Environment.STAGING]
        )
        rollback = (
            "No automated compensation exists. Confirm the effect can be reversed manually "
            "before enabling in production."
            if artifact.risk in {RiskClass.WRITE, RiskClass.DESTRUCTIVE}
            else None
        )

        fields = [
            "artifact_id", "tool_name", "approval", "retry", "idempotency_key_required",
            "allowed_environments", "log_class", "sensitivity",
        ]
        if scopes:
            fields.append("required_scopes")
        if rollback:
            fields.append("rollback_guidance")
        if unresolved:
            fields.append("unresolved")
        policies.append(
            ToolPolicy(
                artifact_id=artifact.artifact_id,
                tool_name=artifact.name,
                required_scopes=scopes,
                approval=approval,
                confirmation=confirmation,
                retry=retry,
                idempotency_key_required=needs_key,
                rate=RateBudget(
                    calls_per_minute=per_minute,
                    max_concurrent=concurrent,
                    daily_call_budget=daily,
                    provenance=_records(
                        primary,
                        ("calls_per_minute", "max_concurrent", "daily_call_budget"),
                        "rate",
                    ),
                ),
                allowed_environments=environments,
                log_class=log_class,
                sensitivity=sensitivity,
                output=OutputPolicy(
                    max_bytes=DEFAULT_MAX_OUTPUT_BYTES,
                    projected_fields=list(artifact.output_fields),
                    redact_fields=redact,
                    provenance=_records(
                        primary,
                        ("max_bytes", *(["projected_fields"] if artifact.output_fields else []),
                         *(["redact_fields"] if redact else [])),
                        "output",
                    ),
                ),
                rollback_guidance=rollback,
                unresolved=unresolved,
                provenance=[
                    *_records(primary, tuple(fields), "tool"),
                    Provenance(
                        field="approval",
                        source_pointer=primary.source_pointer,
                        derivation=Derivation.NORMALIZED,
                        rule=f"policy.rationale.approval: {approval_rationale}",
                    ),
                    Provenance(
                        field="required_scopes" if scopes else "artifact_id",
                        source_pointer=primary.source_pointer,
                        derivation=Derivation.NORMALIZED,
                        rule=f"policy.rationale.scopes: {scope_rationale}",
                    ),
                    Provenance(
                        field="sensitivity",
                        source_pointer=primary.source_pointer,
                        derivation=Derivation.NORMALIZED,
                        rule=f"policy.rationale.sensitivity: {sensitivity_rationale}",
                    ),
                    Provenance(
                        field="retry",
                        source_pointer=primary.source_pointer,
                        derivation=Derivation.NORMALIZED,
                        rule=f"policy.rationale.retry: {retry_rationale}",
                    ),
                    Provenance(
                        field="log_class",
                        source_pointer=primary.source_pointer,
                        derivation=Derivation.NORMALIZED,
                        rule=f"policy.rationale.log: {log_rationale}",
                    ),
                    Provenance(
                        field="artifact_id",
                        source_pointer=primary.source_pointer,
                        derivation=Derivation.NORMALIZED,
                        rule=f"policy.rationale.rate: {rate_rationale}",
                    ),
                ],
            )
        )

    return PolicyManifest(
        service_id=plan.service_id,
        source_digest=plan.source_digest,
        policies=policies,
    )
