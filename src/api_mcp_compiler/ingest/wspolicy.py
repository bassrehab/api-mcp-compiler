"""Reading WS-SecurityPolicy out of a WSDL.

WSDL says nothing about authentication on its own. WS-SecurityPolicy is how a SOAP service
states it in the document, attached to a binding as a `wsp:Policy` and referenced by
`wsp:PolicyReference`. Where a service publishes it, it is a fact about the contract and is the
best available answer, better than anything an operator declares alongside, because it is what
the service itself says.

## What is recognised, and what is refused

A deliberately small set of token assertions: username, X.509, Kerberos, issued token, and
HTTPS with a required client certificate. Each maps to an `AuthSchemeIR` whose `detail` keeps
the assertion's own element name, so nothing is lost to the mapping.

Anything else is recorded as a non-blocking ambiguity and contributes no scheme.

That asymmetry is deliberate. Approximating a security policy is the least defensible place in
this compiler to guess: an assertion this version does not model might be the one that makes an
operation safe, and treating it as understood would produce a surface that claims governance it
cannot demonstrate. Recording it as unrecognised leaves the operation without declared
authentication, which for anything that changes state means policy synthesis fails closed.
That is the correct outcome for a policy nobody has read.

The ambiguity is non-blocking because the operation may still be perfectly emittable: a read
needs no authentication to be defensible, and a write may be covered by a declared transport.
Blocking here would refuse operations that are fine for a reason that has nothing to do with
them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from api_mcp_compiler.models import (
    Ambiguity,
    AuthRequirementIR,
    AuthSchemeIR,
    AuthSchemeType,
    Derivation,
    Provenance,
    SecurityRequirementIR,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lxml import etree

WSP_NS = "http://www.w3.org/ns/ws-policy"
#: The 2004 namespace is still what most published WSDLs in service use.
WSP_2004_NS = "http://schemas.xmlsoap.org/ws/2004/09/policy"
SP_NS = "http://docs.oasis-open.org/ws-sx/ws-securitypolicy/200702"
SP_2005_NS = "http://schemas.xmlsoap.org/ws/2005/07/securitypolicy"

RULE = "wspolicy.assertion"


def _from_source(pointer: str, *fields: str) -> list[Provenance]:
    """A `source` record per field. The IR requires one for every informative value."""
    return [
        Provenance(
            field=name,
            source_pointer=pointer,
            derivation=Derivation.SOURCE,
            rule=RULE,
            confidence=1.0,
        )
        for name in fields
    ]

#: Token assertion local names this version understands, and the scheme each produces.
#:
#: `UsernameToken` becomes an `other` scheme rather than HTTP basic on purpose. A WS-Security
#: username token travels in a SOAP header with its own nonce and digest rules; calling it
#: basic authentication would tell a generated client to put credentials somewhere the service
#: does not read them.
_TOKENS: dict[str, tuple[AuthSchemeType, str]] = {
    "UsernameToken": (AuthSchemeType.OTHER, "A WS-Security username token in the SOAP header."),
    "X509Token": (AuthSchemeType.MUTUAL_TLS, "An X.509 certificate."),
    "KerberosToken": (AuthSchemeType.OTHER, "A Kerberos ticket."),
    "IssuedToken": (AuthSchemeType.OTHER, "A token issued by a security token service."),
    "HttpsToken": (AuthSchemeType.MUTUAL_TLS, "HTTPS with a required client certificate."),
}

#: Assertions that describe how a message is protected rather than who is calling. They are
#: recognised so they are not reported as unknown, and produce no scheme, because transport
#: confidentiality is not authentication and treating it as such would be the exact
#: over-claim this module exists to avoid.
_NOT_AUTHENTICATION = {
    "TransportBinding",
    "AsymmetricBinding",
    "SymmetricBinding",
    "Wss10",
    "Wss11",
    "Trust10",
    "Trust13",
    "TransportToken",
    "AlgorithmSuite",
    "Layout",
    "Strict",
    "Lax",
    "SignedParts",
    "EncryptedParts",
    "SignedSupportingTokens",
    "IncludeTimestamp",
    "Basic256",
    "Basic128",
    "TripleDes",
    "Body",
    "Header",
}


def _is_policy_namespace(namespace: str | None) -> bool:
    return namespace in {WSP_NS, WSP_2004_NS}


def _is_security_policy_namespace(namespace: str | None) -> bool:
    return namespace in {SP_NS, SP_2005_NS}


def _split(tag: object) -> tuple[str | None, str]:
    """Namespace and local name of an element tag, tolerating comments and processing nodes."""
    if not isinstance(tag, str):
        return None, ""
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return None, tag


def collect_policies(root: etree._Element) -> dict[str, etree._Element]:
    """Every `wsp:Policy` in the document, by its identifier.

    Identifiers are matched on the bare name so that a `wsp:PolicyReference URI="#p1"` finds
    the policy whose `wsu:Id` is `p1`, which is how attachment is written in practice.
    """
    found: dict[str, etree._Element] = {}
    for element in root.iter():
        namespace, local = _split(element.tag)
        if local != "Policy" or not _is_policy_namespace(namespace):
            continue
        for key, value in element.attrib.items():
            _, attribute = _split(key)
            if attribute in {"Id", "id"} and isinstance(value, str):
                found[value.lstrip("#")] = element
    return found


def _referenced(
    element: etree._Element, policies: dict[str, etree._Element]
) -> list[etree._Element]:
    """Policies attached to an element, whether inline or by reference."""
    attached: list[etree._Element] = []
    for child in element:
        namespace, local = _split(child.tag)
        if local == "Policy" and _is_policy_namespace(namespace):
            attached.append(child)
        elif local == "PolicyReference" and _is_policy_namespace(namespace):
            uri = child.get("URI")
            if uri is not None:
                target = policies.get(uri.lstrip("#"))
                if target is not None:
                    attached.append(target)
    return attached


def read_policy(
    binding: etree._Element,
    policies: dict[str, etree._Element],
    pointer: str,
) -> tuple[list[AuthSchemeIR], AuthRequirementIR | None, list[Ambiguity]]:
    """Read the security policy attached to a binding.

    Returns the schemes it declares, the requirement naming them, and anything unrecognised.
    An assertion outside the recognised set contributes no scheme and is reported.
    """
    attached = _referenced(binding, policies)
    if not attached:
        return [], None, []

    schemes: dict[str, AuthSchemeIR] = {}
    ambiguities: list[Ambiguity] = []
    seen_unknown: set[str] = set()

    for policy in attached:
        for element in policy.iter():
            namespace, local = _split(element.tag)
            if not _is_security_policy_namespace(namespace):
                continue
            if local in _TOKENS:
                scheme_type, description = _TOKENS[local]
                scheme_id = f"wspolicy_{local}"
                schemes.setdefault(
                    scheme_id,
                    AuthSchemeIR(
                        scheme_id=scheme_id,
                        type=scheme_type,
                        description=description,
                        detail={"assertion": local, "namespace": namespace},
                        provenance=_from_source(
                            pointer, "scheme_id", "type", "description", "detail"
                        ),
                    ),
                )
            elif local not in _NOT_AUTHENTICATION and local not in seen_unknown:
                seen_unknown.add(local)
                ambiguities.append(
                    Ambiguity(
                        code="unrecognised_security_assertion",
                        field="authentication",
                        source_pointer=pointer,
                        detail=(
                            f"The security policy uses {local!r}, which this version does not "
                            "model. It contributes no scheme rather than being approximated, "
                            "so any operation that changes state and has no other declared "
                            "authentication will fail closed."
                        ),
                        blocking=False,
                    )
                )

    if not schemes:
        return [], None, ambiguities

    ordered = [schemes[key] for key in sorted(schemes)]
    requirement = AuthRequirementIR(
        scheme_ids=[item.scheme_id for item in ordered],
        # One alternative naming every token the policy requires: WS-SecurityPolicy composes
        # assertions conjunctively within a policy, so these are required together rather than
        # being a choice between them.
        alternatives=[
            SecurityRequirementIR(
                scheme_ids=[item.scheme_id for item in ordered],
                provenance=_from_source(pointer, "scheme_ids"),
            )
        ],
        provenance=_from_source(pointer, "scheme_ids", "alternatives", "disabled"),
    )
    return ordered, requirement, ambiguities
