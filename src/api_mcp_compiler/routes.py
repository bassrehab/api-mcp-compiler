"""What a route says about the values a call needs and the values it hands back.

Three places have to agree about this: the planner that proposes a lookup-then-use pair, the
harness that executes one, and the emitter that serves one. If each derived it separately they
would drift, and a composite would be proposed on one rule and executed on another.

Nothing here reads a task, a corpus or a solution path. A route is a statement the
specification makes about its own shape.
"""

from __future__ import annotations

import re

from api_mcp_compiler.models import OperationIR, SideEffectClass

TEMPLATED = re.compile(r"^\{[^}]*\}$")


def segments(route: str) -> list[str]:
    """Split a route into its non-empty segments."""
    return [item for item in route.split("/") if item]


def yields_identifiers(operation: OperationIR) -> bool:
    """Whether an operation hands back an identifier a later call could use.

    A read does. So does a create: posting to a collection root returns the record it made,
    and the whole point of a prepare-then-commit pair is that the second step acts on what the
    first one created. Restricting this to reads left that pair unthreadable, which is the
    shape the action-verb rule exists to find.

    A destructive operation does not: whatever it returns identifies something now gone.
    """
    if operation.side_effect is SideEffectClass.READ:
        return True
    if operation.side_effect is SideEffectClass.DESTRUCTIVE:
        return False
    parts = segments(operation.route or "")
    return bool(parts) and not TEMPLATED.match(parts[-1])


def yielded_collection(operation: OperationIR) -> str | None:
    """The resource an operation hands back identifiers for.

    `GET /playlists/{id}`, `GET /me/playlists` and `POST /playlists` all yield playlists. The
    last named segment names the resource in every shape.
    """
    if not operation.route or not yields_identifiers(operation):
        return None
    named = [item for item in segments(operation.route) if not TEMPLATED.match(item)]
    return named[-1] if named else None


def required_resources(operation: OperationIR) -> list[str]:
    """The resources an operation needs an identifier for, read from its own route.

    In `/playlists/{playlist_id}/tracks` the templated segment follows `playlists`, so the call
    cannot be made without a playlist identifier that came from somewhere else. The same is
    true of `/movie/{movie_id}`: needing a value the goal cannot supply is a property of the
    route, not of whether the call changes anything. An earlier version asked only about
    writes, which was a restriction inherited from the safety framing of the action-verb rule
    and never justified by the structure.
    """
    if not operation.route:
        return []
    parts = segments(operation.route)
    return [
        parts[index - 1]
        for index, item in enumerate(parts)
        if index > 0 and TEMPLATED.match(item) and not TEMPLATED.match(parts[index - 1])
    ]


def callable_from_a_goal(operation: OperationIR) -> bool:
    """Whether an operation can be invoked from the goal alone.

    A composite has to begin with one of these. Pairing a lookup that itself needs an
    identifier with the call that needs its result moves the problem rather than solving it,
    and it is why a read-to-read rule would otherwise propose every detail endpoint against
    every other.
    """
    return not required_resources(operation)


def identifier_argument(operation: OperationIR, resource: str) -> str | None:
    """The argument name carrying a resource's identifier for one operation.

    The route names the parameter, so this reads it rather than guessing at a convention.
    """
    if not operation.route:
        return None
    parts = segments(operation.route)
    for index, item in enumerate(parts):
        if index > 0 and TEMPLATED.match(item) and parts[index - 1] == resource:
            return item.strip("{}")
    return None


def thread_binding(reader: OperationIR, writer: OperationIR) -> tuple[str, str] | None:
    """Resolve which argument of `writer` is filled from `reader`, and from which field.

    Returns the writer's argument name and the field to read out of the reader's response.
    A composite exists precisely because this value cannot come from the goal, so failing to
    resolve it means the pair should not have been proposed.
    """
    produced = yielded_collection(reader)
    if produced is None:
        return None
    for resource in required_resources(writer):
        if resource != produced:
            continue
        argument = identifier_argument(writer, resource)
        if argument:
            # A record identifies itself by `id`. The threading reads that rather than the
            # writer's parameter name, which belongs to the writer's route, not the reader's
            # payload.
            return argument, "id"
    return None
