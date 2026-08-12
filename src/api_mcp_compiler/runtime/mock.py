"""Deterministic mock execution of a generated tool surface.

Responses are synthesised from the declared response schema, seeded by the tool and the
property path, so repeated runs are byte-identical. A random mock would make contract tests
flaky and would make the later evaluation harness unable to attribute a behaviour change to
anything in particular.

A declared example is always preferred over a synthesised value: an example is what the
specification author said the response looks like, and inventing one over the top would
discard real evidence.

This module performs no network or filesystem access.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from api_mcp_compiler.models import (
    ApiSemanticIR,
    DataSensitivity,
    EmissionStatus,
    LogClass,
    OperationIR,
    PolicyManifest,
    ResponseIR,
    RiskClass,
    ToolDescriptor,
    ToolPolicy,
    ToolSurface,
)
from api_mcp_compiler.runtime.governance import (
    AuditEvent,
    ConfirmationError,
    ConfirmationToken,
    apply_output_policy,
    check_confirmation,
    digest_arguments,
    issue_confirmation,
)

MAX_SYNTHESIS_DEPTH = 6


class ToolInvocationError(RuntimeError):
    """Raised when a tool cannot be invoked as requested."""


class ToolDisabledError(ToolInvocationError):
    """Raised when a tool that did not clear the emission gate is invoked.

    This is the runtime half of the safety gate: refusing at generation time is only a
    guarantee if invocation refuses too.
    """


class ArgumentValidationError(ToolInvocationError):
    """Raised when arguments do not satisfy the tool's declared input schema."""


@dataclass(frozen=True)
class MockResponse:
    """One synthesised or example-derived response, after policy has been applied."""

    tool: str
    status: str
    body: Any
    from_example: bool
    refused: bool = False
    redacted_fields: tuple[str, ...] = ()


@dataclass
class MockExecutor:
    """Executes a generated surface against synthesised responses.

    Instances are cheap and stateless apart from the surface and IR they were built with.
    """

    ir: ApiSemanticIR
    surface: ToolSurface
    manifest: PolicyManifest | None = None
    audit: list[AuditEvent] = field(default_factory=list)
    _tools: dict[str, ToolDescriptor] = field(default_factory=dict, init=False)
    _operations: dict[str, OperationIR] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._tools = {item.name: item for item in self.surface.tools}
        self._operations = {item.operation_id: item for item in self.ir.operations}

    def policy_for(self, name: str) -> ToolPolicy | None:
        """Return the policy governing one tool, if a manifest was supplied."""
        tool = self._tools.get(name)
        if tool is None or self.manifest is None:
            return None
        return self.manifest.policy_for(tool.tool_id)

    def prepare(self, name: str, arguments: dict[str, Any] | None = None) -> ConfirmationToken:
        """Issue a confirmation token for one specific invocation.

        This is the first half of the two-call protocol. The token names the effect and is
        bound to the arguments, so confirming one action cannot authorise another.
        """
        policy = self.policy_for(name)
        if policy is None or policy.confirmation is None:
            raise ConfirmationError(f"tool {name!r} does not require confirmation")
        token = issue_confirmation(name, arguments or {}, policy.confirmation)
        self._record(name, "confirmed", None, policy, digest_arguments(arguments or {}))
        return token

    def _record(
        self,
        tool: str,
        outcome: str,
        reason: str | None,
        policy: ToolPolicy | None,
        arguments_digest: str,
        output_bytes: int = 0,
        redacted: list[str] | None = None,
    ) -> None:
        """Append one audit event. Values are never recorded, only digests."""
        descriptor = self._tools.get(tool)
        self.audit.append(
            AuditEvent(
                tool=tool,
                outcome=outcome,
                reason=reason,
                risk=descriptor.risk if descriptor else RiskClass.UNKNOWN,
                sensitivity=policy.sensitivity if policy else DataSensitivity.INTERNAL,
                log_class=policy.log_class if policy else LogClass.STANDARD,
                arguments_digest=arguments_digest,
                output_bytes=output_bytes,
                redacted_fields=redacted or [],
            )
        )

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        confirmation: ConfirmationToken | None = None,
    ) -> MockResponse:
        """Invoke one tool, enforcing the emission gate and the policy that governs it."""
        tool = self._tools.get(name)
        if tool is None:
            raise ToolInvocationError(f"no tool named {name!r} in this surface")
        payload = arguments or {}
        policy = self.policy_for(name)
        arguments_digest = digest_arguments(payload)
        if tool.emission is not EmissionStatus.EXECUTABLE:
            reasons = ", ".join(item.value for item in tool.blockers)
            self._record(name, "refused", reasons, policy, arguments_digest)
            raise ToolDisabledError(
                f"tool {name!r} is disabled and cannot be invoked: {reasons}"
            )
        errors = sorted(
            Draft202012Validator(tool.input_schema).iter_errors(payload),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            detail = "; ".join(
                f"/{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
                for error in errors
            )
            self._record(name, "refused", "invalid arguments", policy, arguments_digest)
            raise ArgumentValidationError(f"arguments for {name!r} are invalid: {detail}")

        if policy is not None and policy.confirmation is not None:
            try:
                check_confirmation(name, payload, policy.confirmation, confirmation)
            except ConfirmationError as error:
                self._record(name, "refused", str(error), policy, arguments_digest)
                raise

        operation = self._operations[tool.source_operations[0]]
        response = _preferred_response(operation)
        status = response.status if response else "200"
        from_example = False
        body: Any = None
        if response is not None:
            example = next(
                (item for item in response.examples if item.value is not None), None
            )
            if example is not None:
                body, from_example = example.value, True
            elif response.type_schema:
                body = _synthesize(response.type_schema, f"{name}:{status}", 0)

        redacted: list[str] = []
        refused = False
        if policy is not None and body is not None:
            decision = apply_output_policy(body, policy.output)
            body, refused, redacted = decision.body, decision.refused, decision.redacted_fields
            self._record(
                name,
                "refused" if refused else "invoked",
                "output exceeds the policy limit" if refused else None,
                policy,
                arguments_digest,
                decision.byte_count,
                redacted,
            )
        else:
            self._record(name, "invoked", None, policy, arguments_digest)

        return MockResponse(
            tool=name,
            status=status,
            body=body,
            from_example=from_example,
            refused=refused,
            redacted_fields=tuple(redacted),
        )


def _preferred_response(operation: OperationIR) -> ResponseIR | None:
    """Choose the response a successful mock invocation should return.

    The lowest 2xx status wins, so a mock returns the ordinary success case rather than
    whichever response happened to be declared first.
    """
    successes = [item for item in operation.outputs if item.status[:1] == "2"]
    if not successes:
        return operation.outputs[0] if operation.outputs else None
    return sorted(successes, key=lambda item: item.status)[0]


def _seeded_int(seed: str, modulus: int) -> int:
    """Derive a stable integer from a seed.

    `hash()` is salted per process, so it would make output differ between runs. A digest
    keeps synthesis reproducible.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % modulus


def _synthesize(schema: dict[str, Any] | None, seed: str, depth: int) -> Any:
    """Build a deterministic value satisfying a JSON Schema fragment.

    Declared constants, defaults, examples and enumerations are preferred in that order,
    because each is something the specification actually says rather than something invented.
    """
    if not schema or depth > MAX_SYNTHESIS_DEPTH:
        return None
    if "const" in schema:
        return schema["const"]
    if "default" in schema:
        return schema["default"]
    if isinstance(schema.get("examples"), list) and schema["examples"]:
        return schema["examples"][0]
    if "example" in schema:
        return schema["example"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]

    declared = schema.get("type")
    kind = declared[0] if isinstance(declared, list) and declared else declared
    if kind is None:
        kind = "object" if "properties" in schema else "string"

    if kind == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return {}
        required = schema.get("required")
        wanted = set(required) if isinstance(required, list) else set(properties)
        return {
            name: _synthesize(properties[name], f"{seed}.{name}", depth + 1)
            for name in properties
            if name in wanted
        }
    if kind == "array":
        item = schema.get("items")
        if not isinstance(item, dict):
            return []
        return [_synthesize(item, f"{seed}[0]", depth + 1)]
    if kind == "integer":
        return _seeded_int(seed, 1000)
    if kind == "number":
        return float(_seeded_int(seed, 1000))
    if kind == "boolean":
        return _seeded_int(seed, 2) == 1
    if kind == "null":
        return None
    return f"{seed}-{_seeded_int(seed, 10000):04d}"
