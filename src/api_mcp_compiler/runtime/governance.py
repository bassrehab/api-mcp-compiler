"""Runtime enforcement of a policy manifest.

Policy that is only written down is not a control. These are the pieces that make a manifest
bite at invocation time: the confirmation protocol, the output ceiling, field redaction and
the audit record.

Two properties are deliberate. A confirmation token is bound to the arguments it was issued
for, so confirming one action cannot authorise a different one. And an audit event never
carries argument or response values, only a digest of them, so enabling auditing can never
become a way of logging the data the policy was written to protect.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from api_mcp_compiler.contracts import canonical_json
from api_mcp_compiler.models import (
    ConfirmationPolicy,
    DataSensitivity,
    LogClass,
    OutputPolicy,
    RiskClass,
)

REDACTED = "[redacted]"


class ConfirmationError(RuntimeError):
    """Raised when a confirmation requirement is not satisfied."""


@dataclass(frozen=True)
class ConfirmationToken:
    """Evidence that a specific action, with specific arguments, was confirmed."""

    token: str
    tool: str
    effect_summary: str
    arguments_digest: str


class AuditEvent(BaseModel):
    """One structured audit record.

    Argument and response values are never recorded, only digests. An audit trail that
    carried the payload would defeat the sensitivity classification that produced it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    outcome: str = Field(description="`invoked`, `refused` or `confirmed`.")
    reason: str | None = None
    risk: RiskClass
    sensitivity: DataSensitivity
    log_class: LogClass
    arguments_digest: str
    output_bytes: int = 0
    redacted_fields: list[str] = Field(default_factory=list)


def digest_arguments(arguments: dict[str, Any]) -> str:
    """Digest an argument object canonically, so equal arguments digest equally."""
    return "sha256:" + hashlib.sha256(canonical_json(arguments).encode("utf-8")).hexdigest()


def issue_confirmation(
    tool: str, arguments: dict[str, Any], policy: ConfirmationPolicy
) -> ConfirmationToken:
    """Issue a token for one specific invocation."""
    arguments_digest = digest_arguments(arguments)
    material = f"{tool}|{arguments_digest}|{policy.effect_summary}"
    return ConfirmationToken(
        token="confirm:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32],
        tool=tool,
        effect_summary=policy.effect_summary,
        arguments_digest=arguments_digest,
    )


def check_confirmation(
    tool: str,
    arguments: dict[str, Any],
    policy: ConfirmationPolicy,
    presented: ConfirmationToken | None,
) -> None:
    """Verify a presented token authorises this exact invocation.

    Raises `ConfirmationError` when no token is presented, when it was issued for another
    tool, or when the arguments have changed since it was issued. The last case is the one
    that matters: without it, confirming a small refund would authorise a large one.
    """
    if not policy.required:
        return
    if presented is None:
        raise ConfirmationError(
            f"{tool} requires confirmation before it runs: {policy.effect_summary} "
            "Call prepare first and present the token it returns."
        )
    expected = issue_confirmation(tool, arguments, policy)
    if presented.tool != tool:
        raise ConfirmationError(
            f"the presented token was issued for {presented.tool!r}, not {tool!r}"
        )
    if presented.arguments_digest != expected.arguments_digest:
        raise ConfirmationError(
            f"the arguments changed since {tool} was confirmed, so the confirmation no "
            "longer describes what would happen"
        )
    if presented.token != expected.token:
        raise ConfirmationError(f"the confirmation token for {tool} is not valid")


def redact(value: Any, fields: list[str]) -> tuple[Any, list[str]]:
    """Replace declared secret-bearing fields anywhere in a payload."""
    if not fields:
        return value, []
    wanted = set(fields)
    removed: list[str] = []

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            result = {}
            for key, item in node.items():
                if key in wanted:
                    removed.append(str(key))
                    result[key] = REDACTED
                else:
                    result[key] = _walk(item)
            return result
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    return _walk(value), sorted(set(removed))


def project(value: Any, fields: list[str]) -> Any:
    """Keep only the projected top-level fields of an object response."""
    if not fields or not isinstance(value, dict):
        return value
    wanted = set(fields)
    return {key: item for key, item in value.items() if key in wanted}


@dataclass
class OutputDecision:
    """The result of applying an output policy to a response body."""

    body: Any
    byte_count: int
    refused: bool = False
    redacted_fields: list[str] = field(default_factory=list)


def apply_output_policy(value: Any, policy: OutputPolicy) -> OutputDecision:
    """Project, redact and size-check a response body.

    Exceeding the ceiling returns a structured refusal rather than truncated data. Truncated
    JSON either fails to parse or, worse, parses as a smaller answer that looks complete.
    """
    projected = project(value, policy.projected_fields)
    cleaned, removed = redact(projected, policy.redact_fields)
    size = len(canonical_json(cleaned).encode("utf-8"))
    if size > policy.max_bytes:
        return OutputDecision(
            body={
                "error": "output_exceeds_policy_limit",
                "bytes": size,
                "limit": policy.max_bytes,
            },
            byte_count=size,
            refused=True,
            redacted_fields=removed,
        )
    return OutputDecision(body=cleaned, byte_count=size, redacted_fields=removed)
