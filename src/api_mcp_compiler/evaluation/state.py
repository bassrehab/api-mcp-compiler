"""A stateful mock service, so that final state exists to assert on.

An earlier mock synthesised each response independently and kept nothing, which meant task
success could only ever be judged by whether the trace looked right. The acceptance criteria
require the opposite: success is judged by the state the service ends in.

The effect model is a REST-convention approximation, and is honest about being one. It reads
the operation's route and side-effect class and decides what a call would do to a collection.
It is a mock, not a semantics: a service that does not follow the conventions will be modelled
wrongly, which is why a task may state its expectations directly rather than relying on the
approximation being right.

Two corrections came from running it against a real third-party API rather than fixtures
written here.

A collection is keyed by the whole path with templates removed, not by its last named segment.
On a real API `/me/tracks` and `/playlists/{id}/tracks` are different collections, and keying
by the last segment silently merged them, so a final-state assertion could not tell saving a
track to a library from adding one to a playlist.

A `PUT` with no record identifier sets a singleton rather than creating a record. HTTP says
`PUT` replaces the resource at a URI, so `PUT /me/player/volume` updates one thing rather than
creating a `volume` record.

A bodyless `POST` on a collection root is a command rather than a creation. A creation needs
something to create from, so `POST /me/player/next` commands a skip where `POST /playlists`
creates a playlist, and modelling the first as a creation invented a record for every track
skipped.

Every effect now names the rule that produced it and how much that rule is worth, because the
model is an approximation and could not previously say which calls it was confident about. A
result judged against a guess and one judged against a convention that always holds looked
identical from the outside. `scripts/effect_coverage.py` reports the distribution over a real
specification, which is how "how often does the approximation hold" stops being a question
nobody can answer.

Known remaining limitation: a `POST` carrying a body to a collection root is read as a
creation even when the service treats it as a command, and nothing in the path or the body
distinguishes those. That case is modelled at 0.75 rather than silently.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from api_mcp_compiler.models import OperationIR, ParameterLocation, SideEffectClass

_TEMPLATED = re.compile(r"^\{[^}]*\}$")

#: Final path segments that act on an already-identified record rather than a collection.
ACTION_SEGMENTS = frozenset(
    {"approve", "confirm", "execute", "submit", "commit", "cancel", "void", "finalize", "purge"}
)


class EffectKind(StrEnum):
    """What a call does to the store."""

    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    NONE = "none"


@dataclass(frozen=True)
class Effect:
    """The store effect derived for one operation.

    `basis` names the rule that decided this, and `confidence` says how much the rule is
    worth. Both exist because the model is an approximation and the previous version of it
    could not say which calls it was confident about. A result judged against a guess and a
    result judged against a convention that always holds looked identical.
    """

    kind: EffectKind
    collection: str
    action: str | None = None
    identifier_argument: str | None = None
    basis: str = "unclassified"
    confidence: float = 0.0


#: What a service does when a caller says nothing about page size. Real APIs page by default
#: rather than returning everything, and a caller who omits the argument gets that default.
DEFAULT_PAGE_SIZE = 20


def _as_int(value: Any) -> int | None:
    """Read an integer argument, ignoring anything that is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _paginate(items: list[dict[str, Any]], arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply the paging and filtering arguments a real service would apply.

    Ignoring them made the store blind to the thing argument projection is for: an agent that
    sets `limit` badly paid nothing, so withholding `limit` saved it from nothing, and no
    corpus size could have revealed the difference.

    This is a convention approximation like the rest of the effect model, and it cuts both
    ways: an agent that pages too narrowly misses what it needed, and one that cannot page at
    all receives the service's default.
    """
    market = arguments.get("market")
    if isinstance(market, str):
        items = [
            item
            for item in items
            if "market" not in item or item.get("market") == market
        ]
    offset = _as_int(arguments.get("offset")) or 0
    if offset:
        items = items[offset:]
    limit = _as_int(arguments.get("limit"))
    return items[: limit if limit is not None else DEFAULT_PAGE_SIZE]


def _identifier_list(arguments: dict[str, Any]) -> list[str]:
    """Read the identifiers a bulk operation names, however the caller spelled them."""
    value = arguments.get("ids")
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _is_put(operation: OperationIR) -> bool:
    """Report whether an operation is bound to PUT."""
    return operation.source_pointer.rsplit("/", 1)[-1].lower() == "put"


def _has_body(operation: OperationIR) -> bool:
    """Whether the operation sends anything to create a record from."""
    return any(item.location is ParameterLocation.BODY for item in operation.inputs)


def _segments(route: str) -> list[str]:
    return [item for item in route.split("/") if item]


def derive_effect(operation: OperationIR) -> Effect:
    """Derive what an operation would do to the store.

    Reads never mutate. A write on a collection root creates; a write on an identified record
    updates; a write whose final segment is an action verb updates and records the action. A
    destructive operation deletes.
    """
    if not operation.route:
        return Effect(kind=EffectKind.NONE, collection="")
    parts = _segments(operation.route)
    if not parts:
        return Effect(kind=EffectKind.NONE, collection="")

    action: str | None = None
    if parts[-1] in ACTION_SEGMENTS and len(parts) > 1:
        action = parts[-1]
        parts = parts[:-1]

    named = [item for item in parts if not _TEMPLATED.match(item)]
    collection = ".".join(named) if named else parts[0]
    identifier = None
    for item in reversed(parts):
        if _TEMPLATED.match(item):
            identifier = item.strip("{}")
            break
    targets_record = bool(identifier) and _TEMPLATED.match(parts[-1]) is not None

    if operation.side_effect is SideEffectClass.READ:
        return Effect(EffectKind.READ, collection, action, identifier, "read", 1.0)
    if operation.side_effect is SideEffectClass.DESTRUCTIVE:
        return Effect(EffectKind.DELETE, collection, action, identifier, "destructive", 0.9)
    if operation.side_effect is SideEffectClass.WRITE:
        if action:
            return Effect(EffectKind.UPDATE, collection, action, identifier, "action_segment", 0.8)
        if targets_record:
            return Effect(EffectKind.UPDATE, collection, action, identifier, "identified", 0.9)
        if _is_put(operation):
            # PUT replaces the resource at a URI, so with no record identifier it sets one
            # thing rather than adding to a collection.
            return Effect(
                EffectKind.UPDATE,
                collection,
                action,
                identifier or collection,
                "put_singleton",
                0.85,
            )
        if not _has_body(operation):
            # A creation needs something to create from. A bodyless POST on a collection root
            # is a command, and modelling it as a creation invented a record for every skipped
            # track. The final segment is what was commanded, whether or not it is a verb this
            # module happens to know.
            return Effect(
                EffectKind.UPDATE,
                collection,
                action or (parts[-1] if parts else None),
                identifier or collection,
                "bodyless_command",
                0.6,
            )
        return Effect(EffectKind.CREATE, collection, action, identifier, "collection_post", 0.75)
    return Effect(EffectKind.NONE, collection, action, identifier, "unclassified", 0.0)


@dataclass
class ServiceStore:
    """In-memory service state for one task run.

    State lives for the duration of a single run and is seeded from the task fixture, so two
    runs over the same corpus start identically and determinism holds.
    """

    collections: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    _created: int = field(default=0, init=False)

    @classmethod
    def from_fixture(cls, fixture: dict[str, dict[str, dict[str, Any]]]) -> ServiceStore:
        """Seed a store from a task fixture."""
        return cls(collections=deepcopy(fixture))

    def snapshot(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Return a deep copy, so a caller cannot mutate the store by holding its result."""
        return deepcopy(self.collections)

    def records(self, collection: str) -> dict[str, dict[str, Any]]:
        """Return one collection, empty if it does not exist."""
        return self.collections.get(collection, {})

    def apply(self, effect: Effect, arguments: dict[str, Any], seed: str) -> Any:
        """Apply one effect and return the record involved, if any.

        Identifiers for created records are derived from a seed rather than a counter, so a
        run is reproducible regardless of how many other tasks ran first.
        """
        if effect.kind is EffectKind.NONE or not effect.collection:
            return None
        if effect.kind is EffectKind.READ:
            # A read must never create the collection it looks in. Opening the bucket before
            # branching meant any read of an unseeded collection inserted an empty one, so a
            # no_mutation oracle failed on a task whose trace contained nothing but reads.
            return self._read(effect, self.collections.get(effect.collection, {}), arguments)
        bucket = self.collections.setdefault(effect.collection, {})
        # Data arrives as a body or as ordinary arguments. `PUT /me/player/volume` carries the
        # value in a query parameter, so taking only the body would leave the record empty and
        # nothing about the call assertable.
        payload = {
            key: value
            for key, value in arguments.items()
            if key != "body" and key != effect.identifier_argument
        }
        body = arguments.get("body")
        if isinstance(body, dict):
            payload.update(body)
        identifier = self._identifier(effect, arguments)

        if effect.kind is EffectKind.CREATE:
            self._created += 1
            new_id = str(payload.get("id") or f"{effect.collection}-{seed}-{self._created}")
            record = {"id": new_id, **payload}
            bucket[new_id] = record
            return record
        if effect.kind is EffectKind.UPDATE:
            if identifier is None:
                return None
            record = bucket.setdefault(identifier, {"id": identifier})
            record.update(payload)
            if effect.action:
                record["last_action"] = effect.action
            return record
        if effect.kind is EffectKind.DELETE:
            targeted = _identifier_list(arguments)
            if targeted:
                # A bulk delete names what it removes. Without this, "remove the third track"
                # emptied the whole library, and an oracle could not tell a correct removal
                # from an agent deleting everything.
                removed = {item: bucket.pop(item) for item in targeted if item in bucket}
                return {"deleted": len(removed)} if removed else None
            if identifier is not None:
                return bucket.pop(identifier, None)
            # A destructive call on a collection removes everything scoped to it, which is
            # what "purge every item for this warehouse" means.
            removed = dict(bucket)
            bucket.clear()
            return {"deleted": len(removed)} if removed else None
        return None

    def _read(
        self, effect: Effect, bucket: dict[str, dict[str, Any]], arguments: dict[str, Any]
    ) -> Any:
        """Resolve a read without touching the store.

        A read with no identifier is either a singleton, mirroring how a PUT with no identifier
        writes one, or a listing.
        """
        identifier = self._identifier(effect, arguments)
        if identifier is not None:
            return bucket.get(identifier)
        singleton = bucket.get(effect.collection)
        if singleton is not None:
            return singleton
        if not bucket:
            return None
        items = _paginate(list(bucket.values()), arguments)
        return {"items": items} if items else {"items": []}

    def _identifier(self, effect: Effect, arguments: dict[str, Any]) -> str | None:
        """Resolve which record a call targets."""
        if effect.identifier_argument and effect.identifier_argument in arguments:
            return str(arguments[effect.identifier_argument])
        if effect.identifier_argument == effect.collection:
            # A singleton is keyed by its own path, so there is no argument to look up.
            return effect.collection
        for key, value in arguments.items():
            if key.endswith("_id") and isinstance(value, str | int):
                return str(value)
        return None
