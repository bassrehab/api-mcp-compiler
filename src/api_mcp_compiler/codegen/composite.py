"""Threading a value from one step of a composite into the next.

A composite exists because one of its steps needs a value the goal cannot supply: an
identifier that only an earlier call returns. Working out which value, and where it goes, is
what turns a proposal into something executable.

The derivation is the same one the planner used to propose the pair, shared rather than
reimplemented, so a composite is never executed on a different rule from the one that
justified it.
"""

from __future__ import annotations

from dataclasses import dataclass

from api_mcp_compiler.models import OperationIR
from api_mcp_compiler.routes import thread_binding


@dataclass(frozen=True)
class ThreadedArgument:
    """One value carried from an earlier step into a later one."""

    step_index: int
    argument: str
    from_step: int
    response_field: str


def composite_threading(operations: list[OperationIR]) -> dict[str, ThreadedArgument]:
    """Resolve every argument a composite fills for itself, keyed by argument name.

    A single operation threads nothing: there is no earlier step to take a value from.
    """
    if len(operations) < 2:
        return {}
    threaded: dict[str, ThreadedArgument] = {}
    for index, writer in enumerate(operations):
        if index == 0:
            continue
        for earlier, reader in enumerate(operations[:index]):
            binding = thread_binding(reader, writer)
            if binding is None:
                continue
            argument, field = binding
            threaded.setdefault(
                argument,
                ThreadedArgument(
                    step_index=index,
                    argument=argument,
                    from_step=earlier,
                    response_field=field,
                ),
            )
    return threaded
