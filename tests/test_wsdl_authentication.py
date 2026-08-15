"""A SOAP write or destructive operation can be emitted once authentication is known.

Issue #47. `parse_wsdl` never populated `OperationIR.authentication`, and policy synthesis
treats an unresolved authorization concern as fatal for anything that is not a read. So the
human classification gate asked a reviewer for the side effect WSDL cannot express, the
reviewer supplied it, and a second refusal then held the tool forever on `policy_unresolved`
for precisely the operations that needed governing. Reads passed, which is why the example
suite never caught it.

The fail-closed rule is not relaxed anywhere here. What is tested is that a SOAP service now
has a way to state how it is authenticated, from the document or from a declaration beside it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.ingest.transport import (
    Transport,
    TransportDeclarationError,
    load_transport,
)
from api_mcp_compiler.ingest.wsdl import parse_wsdl
from api_mcp_compiler.models import (
    Derivation,
    OverlayEntry,
    ReviewStatus,
    SideEffectClass,
    ToolOverlay,
)
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy

MINIMAL = """<?xml version="1.0"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
             xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
             xmlns:xsd="http://www.w3.org/2001/XMLSchema"
             xmlns:tns="urn:policies"
             targetNamespace="urn:policies">
  <types>
    <xsd:schema targetNamespace="urn:policies">
      <xsd:element name="CancelPolicy" type="xsd:string"/>
      <xsd:element name="CancelPolicyResponse" type="xsd:string"/>
    </xsd:schema>
  </types>
  <message name="CancelPolicyIn"><part name="parameters" element="tns:CancelPolicy"/></message>
  <message name="CancelPolicyOut">
    <part name="parameters" element="tns:CancelPolicyResponse"/>
  </message>
  <portType name="PolicyPort">
    <operation name="CancelPolicy">
      <documentation>Cancel a policy.</documentation>
      <input message="tns:CancelPolicyIn"/>
      <output message="tns:CancelPolicyOut"/>
    </operation>
  </portType>
  <binding name="PolicyBinding" type="tns:PolicyPort">{policy_attachment}
    <soap:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
    <operation name="CancelPolicy">
      <soap:operation soapAction="urn:policies/CancelPolicy"/>
      <input><soap:body use="literal"/></input>
      <output><soap:body use="literal"/></output>
    </operation>
  </binding>
  <service name="PolicyService">
    <port name="PolicyPort" binding="tns:PolicyBinding">
      <soap:address location="https://policies.example.invalid/soap"/>
    </port>
  </service>
</definitions>
"""

USERNAME_TOKEN_POLICY = """
<wsp:Policy xmlns:wsp="http://schemas.xmlsoap.org/ws/2004/09/policy"
            xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
            xmlns:sp="http://docs.oasis-open.org/ws-sx/ws-securitypolicy/200702"
            wsu:Id="PolicyBindingPolicy">
  <sp:TransportBinding>
    <wsp:Policy><sp:UsernameToken/></wsp:Policy>
  </sp:TransportBinding>
</wsp:Policy>
"""

UNKNOWN_POLICY = """
<wsp:Policy xmlns:wsp="http://schemas.xmlsoap.org/ws/2004/09/policy"
            xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
            xmlns:sp="http://docs.oasis-open.org/ws-sx/ws-securitypolicy/200702"
            wsu:Id="PolicyBindingPolicy">
  <sp:SpnegoContextToken/>
</wsp:Policy>
"""


def _document(tmp_path: Path, attachment: str = "") -> Path:
    path = tmp_path / "policies.wsdl"
    path.write_text(MINIMAL.format(policy_attachment=attachment), encoding="utf-8")
    return path


def _blockers(source: Path, side_effect: SideEffectClass, transport: Transport | None):
    """Compile the document with a reviewer's classification recorded, and report blockers."""
    ir = parse_wsdl(source, transport=transport)
    overlay = ToolOverlay(
        service_id=ir.service.service_id,
        source_digest=ir.service.source_digest,
        entries=[
            OverlayEntry(
                operation_id=ir.operations[0].operation_id,
                side_effect=side_effect,
                review_status=ReviewStatus.APPROVED,
            )
        ],
    )
    plan = plan_semantic(ir, overlay)
    surface = generate_surface(ir, plan, synthesize_policy(ir, plan))
    return {item.value for item in surface.tools[0].blockers}


@pytest.mark.parametrize(
    "side_effect",
    [SideEffectClass.READ, SideEffectClass.WRITE, SideEffectClass.DESTRUCTIVE],
)
def test_without_authentication_only_a_read_survives(
    tmp_path: Path, side_effect: SideEffectClass
) -> None:
    """The bug, preserved. A document declaring nothing still fails closed for a change."""
    blockers = _blockers(_document(tmp_path), side_effect, transport=None)
    if side_effect is SideEffectClass.READ:
        assert "policy_unresolved" not in blockers
    else:
        assert "policy_unresolved" in blockers


@pytest.mark.parametrize(
    "side_effect",
    [SideEffectClass.READ, SideEffectClass.WRITE, SideEffectClass.DESTRUCTIVE],
)
def test_a_declared_transport_unblocks_every_classification(
    tmp_path: Path, side_effect: SideEffectClass
) -> None:
    """What the issue asks for: a reviewer's classification is worth something."""
    blockers = _blockers(
        _document(tmp_path), side_effect, transport=Transport(scheme="basic")
    )
    assert blockers == set(), f"{side_effect.value} still blocked by {sorted(blockers)}"


@pytest.mark.parametrize(
    "side_effect",
    [SideEffectClass.WRITE, SideEffectClass.DESTRUCTIVE],
)
def test_a_published_policy_unblocks_without_any_declaration(
    tmp_path: Path, side_effect: SideEffectClass
) -> None:
    """A service that publishes WS-SecurityPolicy needs nobody to declare anything."""
    blockers = _blockers(
        _document(tmp_path, USERNAME_TOKEN_POLICY), side_effect, transport=None
    )
    assert blockers == set(), f"{side_effect.value} still blocked by {sorted(blockers)}"


def test_a_declared_transport_is_never_recorded_as_source(tmp_path: Path) -> None:
    """The whole point of ADR-037.

    An auditor reading an emitted surface has to be able to tell which operations were
    governed on the authority of a contract and which on the authority of an operator. If a
    declaration were folded into `source`, the IR would assert the document says something it
    does not, and the distinction could never be recovered.
    """
    ir = parse_wsdl(_document(tmp_path), transport=Transport(scheme="basic"))

    authentication = ir.operations[0].authentication
    assert authentication is not None
    kinds = {record.derivation for record in authentication.provenance}
    assert kinds == {Derivation.DECLARED}
    assert all(
        record.source_pointer.startswith("declaration:")
        for record in authentication.provenance
    ), "a declared fact must not point into the document, where it cannot be found"

    scheme = ir.service.auth_schemes[0]
    assert {record.derivation for record in scheme.provenance} == {Derivation.DECLARED}


def test_a_published_policy_is_recorded_as_source(tmp_path: Path) -> None:
    ir = parse_wsdl(_document(tmp_path, USERNAME_TOKEN_POLICY))

    authentication = ir.operations[0].authentication
    assert authentication is not None
    assert {record.derivation for record in authentication.provenance} == {Derivation.SOURCE}
    assert ir.service.auth_schemes[0].scheme_id == "wspolicy_UsernameToken"


def test_the_document_outranks_a_declaration_and_the_conflict_is_recorded(
    tmp_path: Path,
) -> None:
    """A declaration fills a silence. It does not contradict a statement.

    An operator who declares basic authentication for a service whose WSDL requires a username
    token has a misunderstanding worth surfacing. Quietly picking either one hides it.
    """
    ir = parse_wsdl(
        _document(tmp_path, USERNAME_TOKEN_POLICY), transport=Transport(scheme="basic")
    )

    assert ir.service.auth_schemes[0].scheme_id == "wspolicy_UsernameToken"
    codes = {item.code for item in ir.ambiguities}
    assert "declared_transport_ignored" in codes


def test_an_unrecognised_assertion_is_reported_rather_than_approximated(
    tmp_path: Path,
) -> None:
    """Guessing at a security policy is the least defensible approximation available.

    An assertion this version does not model might be the one that makes an operation safe.
    It contributes no scheme, so anything that changes state still fails closed, which is the
    correct outcome for a policy nobody has read.
    """
    ir = parse_wsdl(_document(tmp_path, UNKNOWN_POLICY))

    assert ir.operations[0].authentication is None
    unrecognised = [
        item for item in ir.ambiguities if item.code == "unrecognised_security_assertion"
    ]
    assert len(unrecognised) == 1
    assert "SpnegoContextToken" in unrecognised[0].detail
    # Non-blocking: the operation may be perfectly emittable for reasons unrelated to this.
    assert unrecognised[0].blocking is False


def test_a_declaration_is_refused_rather_than_half_applied(tmp_path: Path) -> None:
    path = tmp_path / "transport.yaml"

    path.write_text("transport: {scheme: totally-made-up}\n", encoding="utf-8")
    with pytest.raises(TransportDeclarationError, match="not a transport scheme"):
        load_transport(path)

    # An api_key with nowhere to put the key would produce a client that cannot authenticate.
    path.write_text("transport: {scheme: api_key}\n", encoding="utf-8")
    with pytest.raises(TransportDeclarationError, match="name the key"):
        load_transport(path)

    path.write_text("transport: {scheme: basic, api_key_name: X}\n", encoding="utf-8")
    with pytest.raises(TransportDeclarationError, match="must not carry api_key"):
        load_transport(path)


def test_a_declaration_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "transport.yaml"
    path.write_text(
        "transport:\n  scheme: api_key\n  api_key_name: X-Api-Key\n  api_key_in: header\n",
        encoding="utf-8",
    )
    transport = load_transport(path)
    assert transport.scheme == "api_key"
    assert transport.api_key_name == "X-Api-Key"
