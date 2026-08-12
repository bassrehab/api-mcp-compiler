"""Completeness sweep over an ingested document.

Reporting only the constructs an adapter recognizes but cannot resolve leaves a gap: a
construct the adapter does not know about at all disappears without trace, and a caller
cannot tell "absent from the source" from "not supported yet".

Each object the adapter visits declares which keys it consumed. Whatever is left over is
reported, so the guarantee becomes structural rather than a promise repeated per construct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from api_mcp_compiler.models import Ambiguity
from api_mcp_compiler.provenance import openapi_pointer

VENDOR_EXTENSION_PREFIX = "x-"


@dataclass(frozen=True)
class _Visit:
    tokens: tuple[str, ...]
    kind: str
    present: frozenset[str]
    consumed: frozenset[str]


@dataclass
class ConsumptionLedger:
    """Records which keys of each visited object an adapter actually used."""

    visits: list[_Visit] = field(default_factory=list)

    def consume(
        self, tokens: tuple[str, ...], node: Any, consumed: set[str] | frozenset[str], kind: str
    ) -> None:
        """Record a visit to one object and the keys taken from it."""
        if not isinstance(node, dict):
            return
        self.visits.append(
            _Visit(
                tokens=tokens,
                kind=kind,
                present=frozenset(str(key) for key in node),
                consumed=frozenset(consumed),
            )
        )

    def ambiguities(self) -> list[Ambiguity]:
        """Report every key that was present in the source and never consumed.

        Both codes are non-blocking. Blocking on an unknown vendor extension would make the
        gate unusable while telling a reviewer nothing they could act on.
        """
        found: list[Ambiguity] = []
        for visit in self.visits:
            for key in sorted(visit.present - visit.consumed):
                vendor = key.startswith(VENDOR_EXTENSION_PREFIX)
                found.append(
                    Ambiguity(
                        code="vendor_extension" if vendor else "unconsumed_key",
                        field=".".join(visit.tokens) or "document",
                        source_pointer=openapi_pointer(*visit.tokens, key),
                        detail=(
                            f"Key {key!r} on the {visit.kind} was present in the source and not "
                            + (
                                "interpreted. Vendor extensions carry agent-relevant "
                                "annotations and are surfaced for review."
                                if vendor
                                else "consumed by this adapter, so it is absent from the IR."
                            )
                        ),
                        blocking=False,
                    )
                )
        return found
