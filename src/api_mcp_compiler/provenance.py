"""Source-pointer construction, identifier normalization and content digests.

Every value in the API Semantic IR must be traceable to the exact location in the source
document that produced it. This module owns the two pointer syntaxes, an RFC 6901 JSON
Pointer for OpenAPI and an XPath 1.0 expression for WSDL, the deterministic identifier
rules, and the source digest used to tie a generated artifact to the specification revision
it came from.

Parsers must call these helpers rather than building pointer or identifier strings by
interpolation, because an unescaped pointer is silently ambiguous rather than obviously
wrong.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

OPENAPI_SCHEME = "openapi"
WSDL_SCHEME = "wsdl"

WSDL_1_1_NAMESPACE = "http://schemas.xmlsoap.org/wsdl/"

# Decision: WSDL pointers carry an explicit XPath prefix. WSDL documents
# usually declare the WSDL namespace as the default namespace, and an XPath step without a
# prefix matches only unnamespaced elements, so a prefix-free pointer would look correct
# and silently resolve to nothing. Consumers evaluate pointers with WSDL_XPATH_NAMESPACES.
WSDL_XPATH_PREFIX = "w"
WSDL_XPATH_NAMESPACES = {WSDL_XPATH_PREFIX: WSDL_1_1_NAMESPACE}

_UNSAFE_IDENTIFIER = re.compile(r"[^0-9A-Za-z_]+")
_UNSAFE_SLUG = re.compile(r"[^0-9a-z]+")


def escape_pointer_token(token: str) -> str:
    """Escape one RFC 6901 JSON Pointer reference token.

    ``~`` becomes ``~0`` and ``/`` becomes ``~1``. The order matters: escaping ``/`` first
    would let the introduced ``~1`` be re-escaped into ``~01``.
    """
    return token.replace("~", "~0").replace("/", "~1")


def json_pointer(*tokens: str) -> str:
    """Build an RFC 6901 JSON Pointer in URI-fragment form from unescaped tokens."""
    return "#/" + "/".join(escape_pointer_token(token) for token in tokens) if tokens else "#"


def openapi_pointer(*tokens: str) -> str:
    """Build a scheme-prefixed OpenAPI source pointer.

    Example: ``openapi_pointer("paths", "/customers/{id}", "get")`` yields
    ``openapi:#/paths/~1customers~1{id}/get``.
    """
    return f"{OPENAPI_SCHEME}:{json_pointer(*tokens)}"


def xpath_literal(value: str) -> str:
    """Quote a string for use inside an XPath 1.0 predicate.

    XPath 1.0 has no escape sequence inside a string literal, so a value containing both
    quote characters can only be expressed with ``concat()``.
    """
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    # Split on the single quote and rejoin with a double-quoted single quote, so the
    # concatenation reproduces the original value exactly.
    parts = [f"'{piece}'" for piece in value.split("'")]
    return "concat(" + ", \"'\", ".join(parts) + ")"


def xpath_step(element: str, **predicates: str) -> str:
    """Build one XPath location step with optional attribute predicates.

    Predicate keys are attribute names; a trailing underscore is stripped so that Python
    keywords can be passed (``for_`` becomes ``for``).
    """
    step = element
    for name, value in predicates.items():
        step += f"[@{name.rstrip('_')}={xpath_literal(value)}]"
    return step


def wsdl_pointer(*steps: str) -> str:
    """Build a scheme-prefixed WSDL source pointer from XPath steps.

    Each step is bound to the WSDL 1.1 namespace prefix, so the resulting expression
    evaluates directly against the source document when
    `namespaces=WSDL_XPATH_NAMESPACES` is supplied.
    """
    return f"{WSDL_SCHEME}:/" + "/".join(f"{WSDL_XPATH_PREFIX}:{step}" for step in steps)


def slug(value: str, *, fallback: str = "unnamed") -> str:
    """Normalize a human title into a stable lowercase kebab-case identifier.

    Both parsers share this rule so that a service identifier does not depend on which
    ingestion adapter produced it.
    """
    ascii_form = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    cleaned = _UNSAFE_SLUG.sub("-", ascii_form.lower()).strip("-")
    return cleaned or fallback


def operation_identifier(value: str, *, fallback: str = "unnamed_operation") -> str:
    """Normalize a value into a safe operation identifier, preserving case.

    A source ``operationId`` such as ``getCustomer`` is already safe and passes through
    unchanged, which keeps generated artifacts readable and diffable against the source
    specification. Only synthesized fallbacks are rewritten.
    """
    cleaned = _UNSAFE_IDENTIFIER.sub("_", value).strip("_")
    if not cleaned:
        return fallback
    # Decision: a leading digit is prefixed rather than stripped so that
    # two distinct source names cannot collapse onto the same identifier.
    return f"op_{cleaned}" if cleaned[0].isdigit() else cleaned


def source_digest(data: bytes) -> str:
    """Return the prefixed sha256 digest of raw source bytes.

    The digest is taken over the bytes on disk, before any decoding or parsing, so that it
    identifies the specification revision exactly.
    """
    return f"sha256:{hashlib.sha256(data).hexdigest()}"
