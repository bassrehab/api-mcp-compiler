"""Policy synthesis and runtime governance tests.

The negative tests at the end are the ones that matter most: scope escalation, confirmation
bypass, unsafe retry, oversized output and secret leakage. A control that has never been
shown to refuse is not a control.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.contracts import (
    canonical_json,
    dump_canonical,
    validate_policy_manifest,
)
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.models import (
    ApiSemanticIR,
    ApprovalClass,
    DataSensitivity,
    EmissionBlocker,
    EmissionStatus,
    Environment,
    LogClass,
    OutputPolicy,
    Provenance,
    RetryPolicy,
    ReviewStatus,
    RiskClass,
    ToolPlan,
)
from api_mcp_compiler.planning.overlay import load_overlay
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import (
    classify_sensitivity,
    least_privilege_scopes,
    secret_fields,
    synthesize_policy,
)
from api_mcp_compiler.runtime.governance import (
    ConfirmationError,
    ConfirmationToken,
    apply_output_policy,
    redact,
)
from api_mcp_compiler.runtime.mock import MockExecutor, ToolDisabledError
from tests.conftest import ALL_EXAMPLES, INVENTORY_SERVICE, ORDER_SERVICE

INVENTORY_OVERLAY = "examples/overlays/inventory_service.overlay.json"
ORDER_OVERLAY = "examples/overlays/order_service.overlay.json"


def _ir(example: str) -> ApiSemanticIR:
    source = Path(example)
    if source.suffix == ".wsdl":
        from api_mcp_compiler.ingest.wsdl import parse_wsdl

        return parse_wsdl(source)
    return parse_openapi(source)


def _planned(example: str, overlay: str | None = None) -> tuple[ApiSemanticIR, ToolPlan]:
    ir = _ir(example)
    accepted = load_overlay(Path(overlay)) if overlay else None
    return ir, plan_semantic(ir, accepted)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "spec.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _prov(*fields: str) -> list[Provenance]:
    return [
        Provenance(
            field=name,
            source_pointer="openapi:#/paths",
            derivation="normalized",
            rule="test",
        )
        for name in fields
    ]


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_manifest_validates_and_is_reproducible(example: str) -> None:
    ir, plan = _planned(example)
    manifest = synthesize_policy(ir, plan)
    validate_policy_manifest(manifest.model_dump(mode="json"), label=example)
    assert dump_canonical(manifest) == dump_canonical(synthesize_policy(ir, plan))


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_every_artifact_has_a_policy(example: str) -> None:
    ir, plan = _planned(example)
    manifest = synthesize_policy(ir, plan)
    assert {item.artifact_id for item in manifest.policies} == {
        item.artifact_id for item in plan.artifacts
    }


def test_financial_data_is_classified_on_a_read_as_well_as_a_write() -> None:
    """Folding `financial` into the side-effect class would hide it on exactly the reads."""
    ir = _ir(ORDER_SERVICE)
    refund = next(item for item in ir.operations if item.operation_id == "createRefund")
    customer = next(item for item in ir.operations if item.operation_id == "getCustomer")
    assert classify_sensitivity(refund)[0] is DataSensitivity.FINANCIAL
    assert classify_sensitivity(customer)[0] is DataSensitivity.PERSONAL


def test_sensitive_data_raises_the_log_class() -> None:
    ir, plan = _planned(ORDER_SERVICE)
    manifest = synthesize_policy(ir, plan)
    policy = next(item for item in manifest.policies if "refund" in item.tool_name)
    assert policy.sensitivity is DataSensitivity.FINANCIAL
    assert policy.log_class is LogClass.SENSITIVE


def test_least_privilege_picks_a_narrower_set_than_the_union() -> None:
    ir = _ir(INVENTORY_SERVICE)
    operation = next(
        item for item in ir.operations if item.operation_id == "purgeWarehouseItems"
    )
    assert operation.authentication is not None
    union = set(operation.authentication.scopes)
    chosen, schemes, rationale, concerns = least_privilege_scopes(operation.authentication)
    assert set(chosen) < union
    assert chosen == ["inventory.write"]
    assert "union across alternatives" in rationale
    assert not concerns
    # The scheme is returned as well as the scopes: knowing how much access is needed and
    # not which credential grants it is what left the generated server sending bearer for
    # everything.
    assert schemes == ["inventoryOAuth"]


def test_a_scopeless_credential_is_not_treated_as_least_privilege() -> None:
    """An admin key looks narrowest by count while granting the most."""
    ir = _ir(INVENTORY_SERVICE)
    operation = next(
        item for item in ir.operations if item.operation_id == "purgeWarehouseItems"
    )
    assert operation.authentication is not None
    scopeless = [
        item for item in operation.authentication.alternatives if not item.scopes
    ]
    assert scopeless, "fixture should offer a scopeless alternative"
    assert least_privilege_scopes(operation.authentication)[0] != []


def test_destructive_tools_require_confirmation() -> None:
    ir, plan = _planned(INVENTORY_SERVICE, INVENTORY_OVERLAY)
    manifest = synthesize_policy(ir, plan)
    policy = next(item for item in manifest.policies if item.tool_name.startswith("permanently"))
    assert policy.approval is ApprovalClass.USER_CONFIRMATION
    assert policy.confirmation is not None


def test_non_idempotent_writes_require_an_idempotency_key() -> None:
    ir, plan = _planned(ORDER_SERVICE)
    manifest = synthesize_policy(ir, plan)
    policy = next(item for item in manifest.policies if item.tool_name == "create_refund_request")
    assert policy.retry is RetryPolicy.WITH_IDEMPOTENCY_KEY
    assert policy.idempotency_key_required is True


def test_writes_are_not_allowed_in_production_by_default() -> None:
    ir, plan = _planned(ORDER_SERVICE)
    manifest = synthesize_policy(ir, plan)
    write = next(item for item in manifest.policies if item.tool_name == "create_refund_request")
    read = next(item for item in manifest.policies if item.tool_name == "get_customer")
    assert Environment.PRODUCTION not in write.allowed_environments
    assert Environment.PRODUCTION in read.allowed_environments


def test_destructive_tools_get_a_tighter_rate_budget() -> None:
    ir, plan = _planned(INVENTORY_SERVICE, INVENTORY_OVERLAY)
    manifest = synthesize_policy(ir, plan)
    destructive = next(
        item for item in manifest.policies if item.tool_name.startswith("permanently")
    )
    read = next(item for item in manifest.policies if item.tool_name.startswith("list_items"))
    assert destructive.rate.calls_per_minute is not None
    assert read.rate.calls_per_minute is not None
    assert destructive.rate.calls_per_minute < read.rate.calls_per_minute


def test_unauthenticated_read_is_allowed_but_unauthenticated_write_is_not() -> None:
    """A read with no declared auth is an ordinary public endpoint; a write is a red flag."""
    ir, plan = _planned(ORDER_SERVICE)
    manifest = synthesize_policy(ir, plan)
    read = next(item for item in manifest.policies if item.tool_name == "get_customer")
    write = next(item for item in manifest.policies if item.tool_name == "create_refund_request")
    assert read.unresolved == []
    assert write.unresolved


def test_generation_fails_closed_on_unresolved_policy() -> None:
    ir, plan = _planned(ORDER_SERVICE, ORDER_OVERLAY)
    manifest = synthesize_policy(ir, plan)
    surface = generate_surface(ir, plan, manifest)
    tool = next(item for item in surface.tools if item.name == "create_refund_request")
    assert tool.emission is EmissionStatus.DISABLED
    assert EmissionBlocker.POLICY_UNRESOLVED in tool.blockers


def test_a_composite_stops_being_blocked_once_confirmation_exists() -> None:
    """The gate blocks a composite spanning a change. Confirmation is what lifts that."""
    ir, plan = _planned(ORDER_SERVICE, ORDER_OVERLAY)
    without = generate_surface(ir, plan)
    composite_before = next(item for item in without.tools if item.name == "refund_order")
    assert EmissionBlocker.COMPOSITE_PENDING_CONFIRMATION in composite_before.blockers

    with_policy = generate_surface(ir, plan, synthesize_policy(ir, plan))
    composite_after = next(item for item in with_policy.tools if item.name == "refund_order")
    assert EmissionBlocker.COMPOSITE_PENDING_CONFIRMATION not in composite_after.blockers


def test_manifest_from_another_revision_is_refused() -> None:
    ir, plan = _planned(ORDER_SERVICE)
    stale = synthesize_policy(ir, plan).model_copy(
        update={"source_digest": "sha256:" + "0" * 64}
    )
    with pytest.raises(ValueError, match="re-derive the policy"):
        generate_surface(ir, plan, stale)


def test_secret_bearing_response_fields_are_detected(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Secrets, version: '1'}
        paths:
          /session:
            get:
              operationId: getSession
              summary: Fetch a session
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema:
                        type: object
                        properties:
                          id: {type: string}
                          access_token: {type: string}
                          api_key: {type: string}
        """,
    )
    assert secret_fields(parse_openapi(spec).operations[0]) == ["access_token", "api_key"]


def test_audit_events_record_a_digest_not_the_arguments() -> None:
    """An audit trail carrying the payload would defeat the classification that produced it."""
    ir, plan = _planned(ORDER_SERVICE, ORDER_OVERLAY)
    manifest = synthesize_policy(ir, plan)
    executor = MockExecutor(
        ir=ir, surface=generate_surface(ir, plan, manifest), manifest=manifest
    )
    executor.invoke("look_up_customer", {"customer_id": "C-SECRET-1234"})
    assert executor.audit
    serialised = executor.audit[-1].model_dump_json()
    assert "C-SECRET-1234" not in serialised
    assert executor.audit[-1].arguments_digest.startswith("sha256:")


# --- Negative tests: the five failure modes the governance controls exist to stop ----


def test_negative_scope_escalation_is_not_granted() -> None:
    """The union across alternatives must never become the granted scope set."""
    ir = _ir(INVENTORY_SERVICE)
    operation = next(
        item for item in ir.operations if item.operation_id == "purgeWarehouseItems"
    )
    assert operation.authentication is not None
    granted = set(least_privilege_scopes(operation.authentication)[0])
    assert "inventory.admin" in set(operation.authentication.scopes)
    assert "inventory.admin" not in granted


def test_negative_confirmation_bypass_is_refused() -> None:
    ir, plan = _planned(INVENTORY_SERVICE, INVENTORY_OVERLAY)
    approved = plan.model_copy(
        update={
            "artifacts": [
                item.model_copy(update={"review_status": ReviewStatus.APPROVED})
                for item in plan.artifacts
            ]
        }
    )
    manifest = synthesize_policy(ir, approved)
    surface = generate_surface(ir, approved, manifest)
    executor = MockExecutor(ir=ir, surface=surface, manifest=manifest)
    name = next(item.name for item in surface.tools if item.risk is RiskClass.DESTRUCTIVE)

    with pytest.raises(ConfirmationError, match="requires confirmation"):
        executor.invoke(name, {"warehouse_id": "W-1"})

    token = executor.prepare(name, {"warehouse_id": "W-1"})
    assert executor.invoke(name, {"warehouse_id": "W-1"}, confirmation=token).tool == name

    # A token issued for one warehouse must not authorise another.
    with pytest.raises(ConfirmationError, match="arguments changed"):
        executor.invoke(name, {"warehouse_id": "W-2"}, confirmation=token)

    forged = ConfirmationToken(
        token="confirm:" + "0" * 32,
        tool=name,
        effect_summary="forged",
        arguments_digest=token.arguments_digest,
    )
    with pytest.raises(ConfirmationError, match="not valid"):
        executor.invoke(name, {"warehouse_id": "W-1"}, confirmation=forged)


def test_negative_unsafe_retry_is_not_permitted() -> None:
    """A non-idempotent write must never be marked safe to retry."""
    ir, plan = _planned(ORDER_SERVICE)
    manifest = synthesize_policy(ir, plan)
    for policy in manifest.policies:
        if policy.retry is RetryPolicy.SAFE:
            operation = next(
                item
                for item in ir.operations
                if item.operation_id
                in next(
                    art.source_operations
                    for art in plan.artifacts
                    if art.artifact_id == policy.artifact_id
                )
            )
            assert operation.side_effect.value == "read" or operation.idempotency.value == (
                "idempotent"
            )


def test_negative_oversized_output_is_refused_not_truncated() -> None:
    """Truncated JSON parses as a smaller answer that looks complete."""
    policy = OutputPolicy(max_bytes=32, provenance=_prov("max_bytes"))
    decision = apply_output_policy({"items": ["x" * 200]}, policy)
    assert decision.refused is True
    assert decision.body["error"] == "output_exceeds_policy_limit"
    assert decision.body["limit"] == 32


def test_negative_secret_leakage_is_redacted() -> None:
    policy = OutputPolicy(
        max_bytes=4096,
        redact_fields=["access_token"],
        provenance=_prov("max_bytes", "redact_fields"),
    )
    decision = apply_output_policy(
        {"id": "s-1", "access_token": "super-secret", "nested": {"access_token": "also"}},
        policy,
    )
    assert "super-secret" not in canonical_json(decision.body)
    assert "also" not in canonical_json(decision.body)
    assert decision.redacted_fields == ["access_token"]


def test_redaction_reaches_nested_structures() -> None:
    cleaned, removed = redact({"a": [{"api_key": "k"}]}, ["api_key"])
    assert cleaned == {"a": [{"api_key": "[redacted]"}]}
    assert removed == ["api_key"]


def test_disabled_tool_invocation_is_audited() -> None:
    ir, plan = _planned(ORDER_SERVICE, ORDER_OVERLAY)
    manifest = synthesize_policy(ir, plan)
    executor = MockExecutor(
        ir=ir, surface=generate_surface(ir, plan, manifest), manifest=manifest
    )
    with pytest.raises(ToolDisabledError):
        executor.invoke("create_refund_request", {"body": {}})
    assert executor.audit[-1].outcome == "refused"
    assert "policy_unresolved" in (executor.audit[-1].reason or "")


def test_a_credential_field_is_redacted() -> None:
    from api_mcp_compiler.policy.synthesis import _names_a_secret

    for name in ("accessToken", "api_key", "clientSecret", "password", "refresh_token"):
        assert _names_a_secret(name), name


def test_a_word_that_merely_appears_in_a_name_does_not_redact_it() -> None:
    """A live service returns `TitleCaseWordsWithTokenResult`, where the token is a delimiter.

    Redacting it removed the answer while the call still looked successful, which is worse
    than not redacting: the caller gets nothing useful and no reason.
    """
    from api_mcp_compiler.policy.synthesis import _names_a_secret

    for name in ("TitleCaseWordsWithTokenResult", "tokenDelimiter", "sText"):
        assert not _names_a_secret(name), name
