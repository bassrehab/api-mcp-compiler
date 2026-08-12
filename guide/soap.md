# SOAP and WSDL

WSDL 1.1 is a first-class input, not a translation layer over the REST path.

## What is ingested

Namespaces, XSD types, SOAP headers, faults and bindings are preserved. XSD is translated to
JSON Schema for tool inputs: primitives, facets such as `minLength`, `pattern` and `enumeration`,
`minOccurs` and `maxOccurs`, and named type references resolved through an index built over
every schema in the document.

Document/literal and rpc/literal bindings are both ingested. They differ in how the body is
shaped, not in what the parameters are, so the tool schema is derivable either way.

## What is refused, and why

**Section 5 encoding** serialises values as a reference graph, which this compiler does not
write. An encoded operation is blocked rather than approximated.

Constructs with no faithful JSON Schema translation, such as substitution groups, mixed content
and `xsi:type` polymorphism, are recorded as ambiguities rather than approximated. Inventing an
approximation of a type system is how a generated client corrupts data.

WSDL imports are recorded rather than resolved. WS-Security and MTOM are recorded as
ambiguities.

## Side effects must be recorded by a human

WSDL carries no signal equivalent to an HTTP method. An operation named `DeleteCustomer` might
delete, and might return a receipt.

So SOAP operations are **never classified by inference**. Every one arrives `unclassified`, and
the emission gate blocks it until a reviewer records the side effect in the overlay. This was
once a dead end, where the compiler required a classification and offered no way to express
one; the overlay now carries it as a human decision naming the operation it applies to.

## What the generated server does

`serve` emits a server that posts a SOAP 1.1 envelope to the service endpoint, in document or
rpc shape as the binding declares, carrying the SOAPAction and target namespace the
specification named. Arguments are XML-escaped. Faults are reported as faults rather than as
transport errors. Policy travels with the tool exactly as it does over HTTP.

## Verified against real services

Two public document/literal services were fetched, classified by a reviewer, compiled and
served, and the generated server made real calls. Two defects appeared that no fixture would
have caught:

- A document body carries the element the message part **references**, not the part's own name.
  Using the part name produced a fault from every real service while every local fixture passed.
- Redaction removed a service's answer because a field name contained `token`, where it was a
  delimiter rather than a credential. See [policy](concepts/policy.md).

Coverage was also measured against forty third-party WSDL documents from a public test
collection, fetched and never redistributed.
