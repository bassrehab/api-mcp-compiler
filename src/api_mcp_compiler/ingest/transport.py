"""How a service is reached, when its contract does not say.

A WSDL describes messages. It routinely says nothing about the HTTP basic credential or the
client certificate standing in front of the endpoint, because that is a property of a
deployment rather than of a contract. Without some way to state it, policy synthesis fails
closed on every SOAP write and destructive operation, correctly, since a tool that changes
state and cannot be shown to be governed should not be emitted. The human classification gate
that makes SOAP tractable then produces an answer nobody can use.

So this reads a small declaration supplied alongside the specification.

**It is not an overlay.** An overlay records review decisions about a surface. This records a
fact about infrastructure, asserted by whoever ran the compiler. Putting it in the overlay
would mix a claim about the world with a record of somebody's judgement, and the two need
different scrutiny: one is checked against reality, the other against authority.

**It is not `source` provenance.** See ADR-037. Everything here carries `declared`, so an
auditor can always tell which operations were governed on the authority of a contract and
which on the authority of an operator.

The format, which is deliberately small::

    transport:
      scheme: basic          # basic | bearer | mutual_tls | api_key
      description: Behind the corporate gateway.
      api_key_name: X-Api-Key   # api_key only
      api_key_in: header        # api_key only
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from api_mcp_compiler.models import (
    AuthRequirementIR,
    AuthSchemeIR,
    AuthSchemeType,
    Derivation,
    Provenance,
    SecurityRequirementIR,
)
from api_mcp_compiler.provenance import declaration_pointer

#: The scheme identifier a declared credential is recorded under. Fixed rather than derived
#: from the file, so that two deployments of the same service produce the same IR and a diff
#: between them is about the service rather than about what somebody named a key.
DECLARED_SCHEME_ID = "declared_transport"

RULE = "transport.declared"


class TransportDeclarationError(ValueError):
    """Raised when a declaration cannot be read, rather than being partially applied."""


class Transport(BaseModel):
    """One declared way of authenticating to a service."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scheme: str = Field(description="One of basic, bearer, mutual_tls, api_key.")
    description: str | None = None
    api_key_name: str | None = None
    api_key_in: str | None = None

    @model_validator(mode="after")
    def _fields_match_scheme(self) -> Transport:
        known = {"basic", "bearer", "mutual_tls", "api_key"}
        if self.scheme not in known:
            raise ValueError(
                f"{self.scheme!r} is not a transport scheme. Known schemes are "
                f"{', '.join(sorted(known))}."
            )
        if self.scheme == "api_key":
            if not self.api_key_name or not self.api_key_in:
                raise ValueError(
                    "an api_key transport must name the key and say where it is carried, "
                    "because a generated client cannot guess either"
                )
            if self.api_key_in not in {"header", "query", "cookie"}:
                raise ValueError(
                    f"{self.api_key_in!r} is not a place a key is carried; use header, query "
                    "or cookie"
                )
        elif self.api_key_name or self.api_key_in:
            raise ValueError(f"a {self.scheme} transport must not carry api_key fields")
        return self


class TransportDeclaration(BaseModel):
    """A declaration file, as read from disk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: Transport


def load_transport(path: Path) -> Transport:
    """Read a declaration, refusing anything it cannot fully understand.

    Refusing beats partial application. A declaration that was half-read would produce a
    surface governed by something other than what the operator wrote down, and they would have
    no way of knowing which half took effect.
    """
    try:
        payload: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise TransportDeclarationError(f"{path} could not be read: {error}") from error
    if not isinstance(payload, dict):
        raise TransportDeclarationError(
            f"{path} must be a mapping with a `transport` key, not {type(payload).__name__}"
        )
    try:
        return TransportDeclaration.model_validate(payload).transport
    except ValueError as error:
        raise TransportDeclarationError(f"{path} is not a usable declaration: {error}") from error


def _declared(*fields: str) -> list[Provenance]:
    """A `declared` record per field, which the IR requires for every informative value.

    Every one points at the declaration rather than at the document, so an auditor following a
    pointer arrives somewhere that exists. See ADR-037.
    """
    return [
        Provenance(
            field=name,
            source_pointer=declaration_pointer("transport/scheme"),
            derivation=Derivation.DECLARED,
            rule=RULE,
            confidence=1.0,
        )
        for name in fields
    ]


def declared_scheme(transport: Transport) -> AuthSchemeIR:
    """The scheme a declaration describes, carrying `declared` provenance throughout."""
    description = transport.description or (
        "Declared transport authentication. Not stated by the specification."
    )

    if transport.scheme == "mutual_tls":
        return AuthSchemeIR(
            scheme_id=DECLARED_SCHEME_ID,
            type=AuthSchemeType.MUTUAL_TLS,
            description=description,
            provenance=_declared("scheme_id", "type", "description"),
        )
    if transport.scheme == "api_key":
        return AuthSchemeIR(
            scheme_id=DECLARED_SCHEME_ID,
            type=AuthSchemeType.API_KEY,
            description=description,
            api_key_in=transport.api_key_in,
            api_key_name=transport.api_key_name,
            provenance=_declared(
                "scheme_id", "type", "description", "api_key_in", "api_key_name"
            ),
        )
    return AuthSchemeIR(
        scheme_id=DECLARED_SCHEME_ID,
        type=AuthSchemeType.HTTP,
        description=description,
        http_scheme=transport.scheme,
        provenance=_declared("scheme_id", "type", "description", "http_scheme"),
    )


def declared_requirement() -> AuthRequirementIR:
    """What an operation requires, when a transport is declared for the whole service.

    One alternative naming one scheme. A declaration describes the way in to a service, so it
    applies to every operation equally; there is no per-operation variation to express, and
    inventing one would suggest the declaration said more than it did.
    """
    return AuthRequirementIR(
        scheme_ids=[DECLARED_SCHEME_ID],
        alternatives=[
            SecurityRequirementIR(
                scheme_ids=[DECLARED_SCHEME_ID], provenance=_declared("scheme_ids")
            )
        ],
        provenance=_declared("scheme_ids", "alternatives", "disabled"),
    )
