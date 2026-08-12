"""Placing a credential where the specification said it goes.

A generated server used to send every credential as `Authorization: Bearer`, whatever the
document declared. For an API key in a header named by the service, or HTTP basic, that is
not a degraded call: it is a 401 on every request, from a compiler that had parsed the right
answer and discarded it at the last step.

The policy manifest records which scheme a tool needs, chosen by least-privilege selection
among the alternatives. This module turns that scheme into an instruction the generated
server can follow without re-reading the specification, and names the environment variable
each credential is read from. No credential value is ever written into a generated file.
"""

from __future__ import annotations

import re
from typing import Any

from api_mcp_compiler.models import (
    ApiSemanticIR,
    AuthSchemeIR,
    PolicyManifest,
    ToolDescriptor,
)

#: Schemes whose credential travels as `Authorization: Bearer <value>`.
_BEARER_TYPES = frozenset({"oauth2", "openIdConnect"})


def environment_variable(slug: str, scheme_id: str) -> str:
    """Name the variable one scheme's credential is read from.

    One variable per scheme rather than one per service: a surface that legitimately uses two
    credentials cannot express that in a single variable, and overloading one would make the
    least-privilege choice unenforceable at the point it matters.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", scheme_id).strip("_").upper()
    return f"{slug}_{cleaned}_CREDENTIAL"


def placement(scheme: AuthSchemeIR, slug: str) -> dict[str, Any] | None:
    """Describe how one scheme's credential is sent, or None when it cannot be.

    Returning None rather than guessing is the point. A scheme this compiler cannot place is
    reported to the reviewer as unresolved policy, which blocks emission, instead of being
    sent somewhere plausible.
    """
    variable = environment_variable(slug, scheme.scheme_id)
    if scheme.type in _BEARER_TYPES:
        return {"kind": "header", "name": "Authorization", "prefix": "Bearer ", "env": variable}
    if scheme.type == "apiKey":
        if scheme.api_key_in not in {"header", "query", "cookie"} or not scheme.api_key_name:
            return None
        return {
            "kind": str(scheme.api_key_in),
            "name": scheme.api_key_name,
            "prefix": "",
            "env": variable,
        }
    if scheme.type == "http":
        declared = (scheme.http_scheme or "").lower()
        if declared == "bearer":
            return {
                "kind": "header", "name": "Authorization", "prefix": "Bearer ", "env": variable
            }
        if declared == "basic":
            # The variable holds `user:password`; the server encodes it, because a base64
            # blob in an environment variable is a credential nobody can read or rotate.
            return {"kind": "basic", "name": "Authorization", "prefix": "", "env": variable}
        return None
    return None


def placements(ir: ApiSemanticIR, slug: str) -> dict[str, dict[str, Any]]:
    """Every scheme this surface can place, keyed by scheme identifier."""
    resolved = {}
    for scheme in ir.service.auth_schemes:
        described = placement(scheme, slug)
        if described is not None:
            resolved[scheme.scheme_id] = described
    return resolved


def tool_schemes(
    tools: list[ToolDescriptor], manifest: PolicyManifest | None
) -> dict[str, list[str]]:
    """Which schemes each tool presents, taken from the manifest and not re-derived.

    Re-deriving here would let the server authenticate as something the reviewed policy did
    not choose, which is the same defect as ignoring the choice entirely.
    """
    if manifest is None:
        return {}
    return {
        tool.name: list(policy.required_schemes)
        for tool in tools
        if (policy := manifest.policy_for(tool.tool_id)) is not None and policy.required_schemes
    }


def variables(ir: ApiSemanticIR, slug: str, used: dict[str, list[str]]) -> dict[str, str]:
    """The environment variables a generated server reads, and what each is for.

    Reported by `serve` so a deployment is not left to discover them by getting 401s.
    """
    described = placements(ir, slug)
    needed = sorted({scheme for schemes in used.values() for scheme in schemes})
    return {
        described[scheme]["env"]: scheme
        for scheme in needed
        if scheme in described
    }
