"""WSDL ingestion tests."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from api_mcp_compiler.ingest.wsdl import WsdlIngestionError, parse_wsdl
from api_mcp_compiler.models import (
    Idempotency,
    ParameterLocation,
    Protocol,
    SideEffectClass,
    SourceFormat,
)
from tests.conftest import CUSTOMER_SERVICE


def _write(tmp_path: Path, body: str, name: str = "service.wsdl") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def test_customer_service_parses() -> None:
    ir = parse_wsdl(Path(CUSTOMER_SERVICE))
    assert ir.service.title == "CustomerService"
    assert ir.service.service_id == "customerservice"
    assert ir.service.source_format is SourceFormat.WSDL
    assert [item.operation_id for item in ir.operations] == ["GetCustomer"]
    assert ir.operations[0].protocol is Protocol.SOAP


def test_binding_surface_is_captured() -> None:
    """The seeded parser read operation names only and discarded the entire binding."""
    soap = parse_wsdl(Path(CUSTOMER_SERVICE)).operations[0].soap
    assert soap is not None
    assert soap.target_namespace == "urn:example:customer"
    assert soap.port_type == "CustomerPortType"
    assert soap.binding == "CustomerBinding"
    assert soap.port == "CustomerPort"
    assert soap.style == "document"
    assert soap.transport == "http://schemas.xmlsoap.org/soap/http"
    assert soap.soap_action == "urn:GetCustomer"
    assert soap.endpoint == "https://api.example.invalid/soap/customer"
    assert soap.input_message == "GetCustomerInput"
    assert soap.output_message == "GetCustomerOutput"


def test_endpoint_is_exposed_as_a_service_base_endpoint() -> None:
    ir = parse_wsdl(Path(CUSTOMER_SERVICE))
    assert [server.url for server in ir.service.servers] == [
        "https://api.example.invalid/soap/customer"
    ]


def test_message_parts_become_inputs() -> None:
    operation = parse_wsdl(Path(CUSTOMER_SERVICE)).operations[0]
    assert [item.name for item in operation.inputs] == ["parameters"]
    assert operation.inputs[0].location is ParameterLocation.SOAP_BODY
    assert operation.inputs[0].type_schema == {"type": "string"}


def test_xsd_types_are_resolved_into_json_schema() -> None:
    """Until this worked, every WSDL was blocked and nothing SOAP could be emitted.

    A measurement over 40 third-party WSDL documents put 88 unresolved types across 39 that
    parsed; resolving them left 7, and 37 gained both an input and an output schema.
    """
    ir = parse_wsdl(Path(CUSTOMER_SERVICE))
    operation = ir.operations[0]
    assert operation.inputs[0].type_schema is not None
    assert any(item.type_schema for item in operation.outputs)
    codes = [item.code for item in ir.ambiguities]
    assert codes.count("unresolved_xsd_type") == 0


def test_a_type_the_document_does_not_declare_is_reported_not_invented(tmp_path: Path) -> None:
    """An imported schema is recorded rather than fetched: ingestion never reaches the network."""
    source = tmp_path / "external.wsdl"
    source.write_text(
        Path(CUSTOMER_SERVICE).read_text(encoding="utf-8").replace(
            'type="xsd:string"', 'type="ext:SomethingElse"'
        ),
        encoding="utf-8",
    )
    ir = parse_wsdl(source)
    unresolved = [item for item in ir.ambiguities if item.code == "unresolved_xsd_type"]
    assert unresolved
    assert any("not declared in this document" in item.detail for item in unresolved)
    assert all(item.blocking for item in unresolved)


def test_side_effect_is_unclassified_and_blocks_generation() -> None:
    """A SOAP operation must not reach the approval gate labelled as a read."""
    ir = parse_wsdl(Path(CUSTOMER_SERVICE))
    operation = ir.operations[0]
    assert operation.side_effect is SideEffectClass.UNKNOWN
    assert operation.idempotency is Idempotency.UNKNOWN
    blocking = [item for item in ir.ambiguities if item.code == "unclassified_side_effect"]
    assert len(blocking) == 1
    assert blocking[0].blocking is True


def test_wsdl_2_is_rejected_rather_than_returning_no_operations(tmp_path: Path) -> None:
    """The seeded parser returned an empty operation list, which reads as a valid result."""
    spec = _write(
        tmp_path,
        """
        <?xml version="1.0"?>
        <description xmlns="http://www.w3.org/ns/wsdl" targetNamespace="urn:x"/>
        """,
    )
    with pytest.raises(WsdlIngestionError, match=r"WSDL 2\.0 is not supported"):
        parse_wsdl(spec)


def test_non_wsdl_root_is_rejected(tmp_path: Path) -> None:
    spec = _write(tmp_path, '<?xml version="1.0"?>\n<html/>\n')
    with pytest.raises(WsdlIngestionError, match="definitions root element"):
        parse_wsdl(spec)


def test_missing_target_namespace_is_rejected(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        <?xml version="1.0"?>
        <definitions xmlns="http://schemas.xmlsoap.org/wsdl/"/>
        """,
    )
    with pytest.raises(WsdlIngestionError, match="no targetNamespace"):
        parse_wsdl(spec)


def test_malformed_xml_is_rejected(tmp_path: Path) -> None:
    spec = _write(tmp_path, "<definitions>\n")
    with pytest.raises(WsdlIngestionError, match="not well-formed XML"):
        parse_wsdl(spec)


def test_external_entities_are_not_resolved(tmp_path: Path) -> None:
    """An ingested specification must never be able to read local files."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-VALUE", encoding="utf-8")
    spec = _write(
        tmp_path,
        f"""
        <?xml version="1.0"?>
        <!DOCTYPE definitions [
          <!ENTITY leak SYSTEM "file://{secret}">
        ]>
        <definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
                     xmlns:tns="urn:x" targetNamespace="urn:x">
          <portType name="P">
            <operation name="&leak;"/>
          </portType>
        </definitions>
        """,
    )
    try:
        ir = parse_wsdl(spec)
    except WsdlIngestionError:
        return
    assert "TOP-SECRET-VALUE" not in ir.model_dump_json()


def test_missing_binding_is_reported(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        <?xml version="1.0"?>
        <definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
                     xmlns:tns="urn:x" targetNamespace="urn:x">
          <portType name="P">
            <operation name="Op"/>
          </portType>
        </definitions>
        """,
    )
    ir = parse_wsdl(spec)
    codes = [item.code for item in ir.ambiguities]
    assert "missing_soap_binding" in codes


def test_rpc_style_is_ingested_and_its_encoding_is_what_blocks(tmp_path: Path) -> None:
    """RPC and document differ in how the body is shaped, not in what the parameters are.

    Refusing the style discarded a document whose tool schema was perfectly derivable. What
    cannot be written is Section 5 encoding, which serialises values as a reference graph, and
    that is a transport limit rather than a description one.
    """
    source = tmp_path / "rpc.wsdl"
    source.write_text(
        Path(CUSTOMER_SERVICE)
        .read_text(encoding="utf-8")
        .replace('style="document"', 'style="rpc"'),
        encoding="utf-8",
    )
    ir = parse_wsdl(source)
    codes = [item.code for item in ir.ambiguities]
    assert "unsupported_soap_style" not in codes
    assert ir.operations[0].inputs[0].type_schema is not None


def test_section_five_encoding_blocks_but_still_describes(tmp_path: Path) -> None:
    """The operation is described in full; only serving it is refused."""
    source = tmp_path / "encoded.wsdl"
    source.write_text(
        Path(CUSTOMER_SERVICE)
        .read_text(encoding="utf-8")
        .replace('use="literal"', 'use="encoded"'),
        encoding="utf-8",
    )
    ir = parse_wsdl(source)
    encoded = [item for item in ir.ambiguities if item.code == "unsupported_soap_encoding"]
    assert encoded and all(item.blocking for item in encoded)
    assert ir.operations[0].inputs[0].type_schema is not None


def test_wsdl_imports_are_reported(tmp_path: Path) -> None:
    spec = _write(
        tmp_path,
        """
        <?xml version="1.0"?>
        <definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
                     xmlns:tns="urn:x" targetNamespace="urn:x">
          <import namespace="urn:y" location="other.wsdl"/>
        </definitions>
        """,
    )
    ir = parse_wsdl(spec)
    assert [item.code for item in ir.ambiguities] == ["unresolved_wsdl_import"]


VOID_RESPONSE = """<?xml version="1.0"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"
             xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"
             xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
             xmlns:xsd="http://www.w3.org/2001/XMLSchema"
             xmlns:tns="urn:void" targetNamespace="urn:void">
  <message name="pingRequest">
    <part name="who" type="xsd:string"/>
  </message>
  <message name="pingResponse"></message>
  <portType name="VoidPort">
    <operation name="ping">
      <input message="tns:pingRequest"/>
      <output message="tns:pingResponse"/>
    </operation>
  </portType>
  <binding name="VoidBinding" type="tns:VoidPort">
    <soap:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
    <operation name="ping">
      <soap:operation soapAction="urn:void/ping"/>
      <input><soap:body use="literal"/></input>
      <output><soap:body use="literal"/></output>
    </operation>
  </binding>
  <service name="VoidService">
    <port name="VoidPort" binding="tns:VoidBinding">
      <soap:address location="https://void.example.invalid/soap"/>
    </port>
  </service>
</definitions>
"""


def test_a_message_with_no_parts_is_a_void_response_not_an_unresolved_type(
    tmp_path: Path,
) -> None:
    """WSDL permits an empty message, and services use it the way HTTP uses 204.

    Reporting it as unresolved blocked operations whose documents were perfectly clear. Two of
    the forty in the third-party collection declared an empty response and were refused for
    saying so.
    """
    path = tmp_path / "void.wsdl"
    path.write_text(VOID_RESPONSE, encoding="utf-8")

    ir = parse_wsdl(path)

    assert not [item for item in ir.ambiguities if item.code == "unresolved_xsd_type"]
    operation = ir.operations[0]
    assert operation.outputs[0].type_schema is None
    assert [item.name for item in operation.inputs] == ["who"]


def test_a_type_that_cannot_be_translated_is_still_reported(tmp_path: Path) -> None:
    """The change must not have quietened the case it was distinguishing itself from."""
    path = tmp_path / "unknown.wsdl"
    path.write_text(
        VOID_RESPONSE.replace(
            '<message name="pingResponse"></message>',
            '<message name="pingResponse"><part name="out" type="tns:Missing"/></message>',
        ),
        encoding="utf-8",
    )

    ir = parse_wsdl(path)

    assert [item for item in ir.ambiguities if item.code == "unresolved_xsd_type"]
