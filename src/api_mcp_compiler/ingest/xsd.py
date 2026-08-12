"""Resolving XSD types into JSON Schema.

A WSDL says what its messages are made of by pointing at XSD types, so until those are
resolved an operation has no input or output schema and nothing downstream can run: no tool
schema, no argument validation, no emission. Every WSDL the compiler saw was blocked here.

The translation is deliberately narrow and says so. XSD can express things JSON Schema cannot
— substitution groups, mixed content, xsi:type polymorphism — and inventing an approximation
for those would produce a tool whose schema quietly disagrees with the service. What cannot be
translated is reported as an ambiguity and left for a person, exactly as an unresolved
construct is anywhere else in this compiler.

Nothing here reaches the network. An import of a schema this document does not contain is an
ambiguity, not a fetch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

XSD_NAMESPACE = "http://www.w3.org/2001/XMLSchema"

#: XSD built-ins mapped onto the JSON Schema type they are honestly closest to. A format is
#: recorded where JSON Schema has one, because a caller that knows a string is a date can
#: check it, and one that does not will still accept the string.
_PRIMITIVES: dict[str, dict[str, Any]] = {
    "string": {"type": "string"},
    "normalizedString": {"type": "string"},
    "token": {"type": "string"},
    "NMTOKEN": {"type": "string"},
    "Name": {"type": "string"},
    "NCName": {"type": "string"},
    "ID": {"type": "string"},
    "IDREF": {"type": "string"},
    "language": {"type": "string"},
    "anyURI": {"type": "string", "format": "uri"},
    "QName": {"type": "string"},
    "boolean": {"type": "boolean"},
    "decimal": {"type": "number"},
    "float": {"type": "number"},
    "double": {"type": "number"},
    "integer": {"type": "integer"},
    "int": {"type": "integer"},
    "long": {"type": "integer"},
    "short": {"type": "integer"},
    "byte": {"type": "integer"},
    "unsignedInt": {"type": "integer", "minimum": 0},
    "unsignedLong": {"type": "integer", "minimum": 0},
    "unsignedShort": {"type": "integer", "minimum": 0},
    "unsignedByte": {"type": "integer", "minimum": 0},
    "nonNegativeInteger": {"type": "integer", "minimum": 0},
    "positiveInteger": {"type": "integer", "minimum": 1},
    "nonPositiveInteger": {"type": "integer", "maximum": 0},
    "negativeInteger": {"type": "integer", "maximum": -1},
    "date": {"type": "string", "format": "date"},
    "dateTime": {"type": "string", "format": "date-time"},
    "time": {"type": "string", "format": "time"},
    "duration": {"type": "string", "format": "duration"},
    "base64Binary": {"type": "string", "contentEncoding": "base64"},
    "hexBinary": {"type": "string"},
    "anyType": {},
    "anySimpleType": {},
}

#: XSD facets that carry over to JSON Schema unchanged in meaning.
_FACETS: dict[str, tuple[str, type]] = {
    "minLength": ("minLength", int),
    "maxLength": ("maxLength", int),
    "length": ("minLength", int),
    "pattern": ("pattern", str),
    "minInclusive": ("minimum", float),
    "maxInclusive": ("maximum", float),
    "minExclusive": ("exclusiveMinimum", float),
    "maxExclusive": ("exclusiveMaximum", float),
}

#: Constructs with no honest JSON Schema equivalent. Reported rather than approximated.
_UNTRANSLATABLE = {
    "choice": "a choice has no JSON Schema equivalent that preserves its exclusivity",
    "any": "an xsd:any accepts content this schema cannot describe",
    "anyAttribute": "an xsd:anyAttribute accepts attributes this schema cannot describe",
    "group": "a named model group is not expanded",
    "attributeGroup": "a named attribute group is not expanded",
}


@dataclass
class XsdResolution:
    """A resolved schema and everything that could not be translated on the way."""

    schema: dict[str, Any] | None
    unresolved: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Whether the translation left nothing behind."""
        return self.schema is not None and not self.unresolved


def _local(name: str) -> str:
    """Strip a namespace prefix, which a QName carries and a lookup does not need."""
    return name.rsplit(":", 1)[-1] if ":" in name else name


def _tag(element: Any) -> str:
    """The local tag of an element, ignoring its namespace."""
    return str(element.tag).rsplit("}", 1)[-1]


class XsdIndex:
    """Every named type and element a document declares, by local name.

    Names are indexed without their namespace. Two schemas in one document that declare the
    same local name in different namespaces would collide, which is recorded as an ambiguity
    rather than resolved arbitrarily.
    """

    def __init__(self) -> None:
        self.types: dict[str, Any] = {}
        self.elements: dict[str, Any] = {}
        self.collisions: list[str] = []
        self.imports: list[str] = []

    def add_schema(self, schema: Any) -> None:
        """Index one `xsd:schema` element."""
        for child in schema:
            tag = _tag(child)
            name = child.get("name")
            if tag in {"import", "include", "redefine"}:
                location = child.get("schemaLocation") or child.get("namespace") or "unknown"
                self.imports.append(str(location))
                continue
            if not name:
                continue
            target = self.types if tag in {"simpleType", "complexType"} else None
            if tag == "element":
                target = self.elements
            if target is None:
                continue
            if name in target:
                self.collisions.append(name)
            target[name] = child


def _restriction_schema(node: Any, index: XsdIndex, seen: set[str]) -> XsdResolution:
    """Translate a restriction: a base type narrowed by facets."""
    base = node.get("base")
    resolved = (
        resolve_type(str(base), index, seen)
        if base
        else XsdResolution(schema={}, unresolved=[])
    )
    schema = dict(resolved.schema or {})
    unresolved = list(resolved.unresolved)
    enumeration: list[Any] = []
    for child in node:
        tag = _tag(child)
        value = child.get("value")
        if tag == "enumeration" and value is not None:
            enumeration.append(value)
        elif tag in _FACETS and value is not None:
            key, caster = _FACETS[tag]
            try:
                schema[key] = caster(value)
            except (TypeError, ValueError):
                unresolved.append(f"facet {tag} has the non-numeric value {value!r}")
    if enumeration:
        schema["enum"] = enumeration
    return XsdResolution(schema=schema, unresolved=unresolved)


def _complex_schema(node: Any, index: XsdIndex, seen: set[str]) -> XsdResolution:
    """Translate a complex type into an object, or report what stopped the translation."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    unresolved: list[str] = []

    def walk(container: Any) -> None:
        for child in container:
            tag = _tag(child)
            if tag in _UNTRANSLATABLE:
                unresolved.append(f"{tag}: {_UNTRANSLATABLE[tag]}")
                continue
            if tag in {"sequence", "all", "complexContent", "simpleContent", "restriction"}:
                walk(child)
                continue
            if tag == "extension":
                base = child.get("base")
                if base:
                    extended = resolve_type(str(base), index, seen)
                    for key, value in (extended.schema or {}).get("properties", {}).items():
                        properties.setdefault(key, value)
                    required.extend((extended.schema or {}).get("required", []))
                    unresolved.extend(extended.unresolved)
                walk(child)
                continue
            if tag != "element":
                continue
            name = child.get("name") or _local(str(child.get("ref") or ""))
            if not name:
                continue
            member = _element_schema(child, index, seen)
            unresolved.extend(member.unresolved)
            properties[name] = member.schema if member.schema is not None else {}
            if (child.get("minOccurs") or "1") != "0":
                required.append(name)

    walk(node)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = sorted(set(required))
    return XsdResolution(schema=schema, unresolved=unresolved)


def _element_schema(node: Any, index: XsdIndex, seen: set[str]) -> XsdResolution:
    """Translate one element declaration, wrapping it in an array when it repeats."""
    reference = node.get("ref")
    if reference is not None:
        target = index.elements.get(_local(str(reference)))
        if target is None:
            return XsdResolution(
                schema=None, unresolved=[f"element reference {reference!r} is not declared here"]
            )
        node = target

    type_name = node.get("type")
    if type_name is not None:
        resolved = resolve_type(str(type_name), index, seen)
    else:
        inline = next(
            (item for item in node if _tag(item) in {"simpleType", "complexType"}), None
        )
        resolved = (
            _named_schema(inline, index, seen)
            if inline is not None
            else XsdResolution(schema={}, unresolved=[])
        )

    max_occurs = node.get("maxOccurs") or "1"
    if max_occurs == "unbounded" or (max_occurs.isdigit() and int(max_occurs) > 1):
        return XsdResolution(
            schema={"type": "array", "items": resolved.schema or {}},
            unresolved=resolved.unresolved,
        )
    return resolved


def _named_schema(node: Any, index: XsdIndex, seen: set[str]) -> XsdResolution:
    """Translate a simpleType or complexType node."""
    tag = _tag(node)
    if tag == "simpleType":
        restriction = next((item for item in node if _tag(item) == "restriction"), None)
        if restriction is not None:
            return _restriction_schema(restriction, index, seen)
        union = next((item for item in node if _tag(item) in {"union", "list"}), None)
        if union is not None:
            return XsdResolution(
                schema={"type": "string"},
                unresolved=[
                    f"{_tag(union)} is translated as a string, losing the members it allows"
                ],
            )
        return XsdResolution(schema={}, unresolved=[])
    if tag == "complexType":
        return _complex_schema(node, index, seen)
    return XsdResolution(schema=None, unresolved=[f"{tag} is not a type declaration"])


def resolve_type(reference: str, index: XsdIndex, seen: set[str] | None = None) -> XsdResolution:
    """Resolve a type QName into a JSON Schema fragment.

    A type that refers to itself is reported rather than followed. XSD permits recursion and
    JSON Schema can express it with `$ref`, but a tool argument that nests without limit is
    not something an agent can fill, so a reviewer should see it.
    """
    seen = set() if seen is None else seen
    local = _local(reference)
    prefix = reference.rsplit(":", 1)[0] if ":" in reference else ""

    if local in _PRIMITIVES and (prefix in {"xsd", "xs"} or local not in index.types):
        return XsdResolution(schema=dict(_PRIMITIVES[local]), unresolved=[])

    if local in seen:
        return XsdResolution(
            schema={},
            unresolved=[f"type {local!r} refers to itself, so it was not expanded further"],
        )

    node = index.types.get(local)
    if node is None:
        element = index.elements.get(local)
        if element is not None:
            return _element_schema(element, index, seen | {local})
        detail = f"type {reference!r} is not declared in this document"
        if index.imports:
            detail += (
                f"; it may come from one of {len(index.imports)} imported schema(s), which are "
                "not fetched because ingestion never reaches the network"
            )
        return XsdResolution(schema=None, unresolved=[detail])

    return _named_schema(node, index, seen | {local})


def build_index(schemas: list[Any]) -> XsdIndex:
    """Index every schema a WSDL document embeds."""
    index = XsdIndex()
    for schema in schemas:
        index.add_schema(schema)
    return index
