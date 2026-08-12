"""Importing the RestBench task set into an evaluation corpus.

RestBench supplies a natural-language goal and a human-annotated solution path expressed as
an HTTP method and a templated path. It supplies neither arguments nor any statement of what
the service should look like afterwards, because it scores by comparing the path an agent took
against the annotated one. This project scores by final state, so the goals and the paths are
reusable and the scoring is not.

That difference is where the remaining judgement lives, and it is kept separate on purpose.
The goals are third-party and are fetched. The oracles are ours and are committed, in a
sidecar keyed to the upstream task. Merging the two produces a runnable corpus that is never
stored here, because it would contain the upstream text verbatim.

Authoring an oracle from the goal alone, without consulting the generated surface, is what
keeps the corpus from encoding the assumptions of the thing it is meant to judge. Nothing
enforces that; it is a discipline, and it is stated here so a reader can hold it to account.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api_mcp_compiler.models import (
    ApiSemanticIR,
    EvalCorpus,
    EvalTask,
    ReferenceStep,
    TaskOracle,
)

ORACLE_SIDECAR_SCHEMA_VERSION = "0.1.0"


class ImportError_(ValueError):
    """Raised when the upstream task set cannot be mapped onto the specification."""


class OracleEntry(BaseModel):
    """Oracles authored here for one upstream task.

    `query_digest` pins the entry to the exact goal text it was written against. The upstream
    file is digest-verified, so an index is stable, but a reordering upstream would silently
    attach an oracle to a different goal without this.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    task_id: str
    query_digest: str = Field(pattern=r"^sha256:[0-9a-f]{16}$")
    fixture: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    arguments: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Arguments per solution step, so a deterministic driver can replay it.",
    )
    prohibited_operations: list[str] = Field(default_factory=list)
    oracles: list[TaskOracle] = Field(min_length=1)


class OracleSidecar(BaseModel):
    """Everything this project authors for an upstream task set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = ORACLE_SIDECAR_SCHEMA_VERSION
    source_id: str
    authoring_note: str
    entries: list[OracleEntry] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _pinned_version(cls, value: str) -> str:
        if value != ORACLE_SIDECAR_SCHEMA_VERSION:
            raise ValueError(
                f"expected sidecar schema_version {ORACLE_SIDECAR_SCHEMA_VERSION}, got {value!r}"
            )
        return value

    def entry(self, index: int) -> OracleEntry | None:
        """Return the authored entry for one upstream index, if it exists."""
        return next((item for item in self.entries if item.index == index), None)


def query_digest(query: str) -> str:
    """Short digest of a goal, used to detect an upstream reordering."""
    return "sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


def operation_index(ir: ApiSemanticIR) -> dict[str, str]:
    """Map `METHOD /path`, as RestBench writes it, to an operation identifier."""
    index: dict[str, str] = {}
    for operation in ir.operations:
        if operation.route is None:
            continue
        method = operation.source_pointer.rsplit("/", 1)[-1].upper()
        index[f"{method} {operation.route}"] = operation.operation_id
    return index


def bind_solution(solution: list[str], index: dict[str, str]) -> tuple[list[str], list[str]]:
    """Resolve a solution path to operation identifiers, reporting anything unbound."""
    bound: list[str] = []
    unbound: list[str] = []
    for step in solution:
        operation_id = index.get(step)
        if operation_id is None:
            unbound.append(step)
        else:
            bound.append(operation_id)
    return bound, unbound


def import_corpus(
    tasks_path: Path,
    ir: ApiSemanticIR,
    sidecar: OracleSidecar,
    corpus_id: str = "restbench-spotify",
) -> tuple[EvalCorpus, list[str]]:
    """Merge upstream goals with authored oracles into a runnable corpus.

    Returns the corpus and a report of every task that could not be included, so a gap is
    counted rather than quietly dropped.
    """
    upstream = json.loads(tasks_path.read_text(encoding="utf-8"))
    index = operation_index(ir)
    tasks: list[EvalTask] = []
    outstanding: list[str] = []

    for position, item in enumerate(upstream):
        query = str(item["query"])
        bound, unbound = bind_solution(list(item["solution"]), index)
        if unbound:
            outstanding.append(f"{position}: unbound steps {unbound}")
            continue
        entry = sidecar.entry(position)
        if entry is None:
            outstanding.append(f"{position}: no oracles authored for {query[:60]!r}")
            continue
        if entry.query_digest != query_digest(query):
            raise ImportError_(
                f"entry {position} was authored against a different goal; the upstream task "
                "set has changed and every oracle keyed by position must be re-checked"
            )
        arguments = entry.arguments or [{} for _ in bound]
        if len(arguments) != len(bound):
            raise ImportError_(
                f"entry {position} supplies {len(arguments)} argument sets for {len(bound)} steps"
            )
        tasks.append(
            EvalTask(
                task_id=entry.task_id,
                goal=query,
                fixture=entry.fixture,
                prohibited_operations=entry.prohibited_operations,
                oracles=list(entry.oracles),
                reference_solution=[
                    ReferenceStep(operation_id=name, arguments=values)
                    for name, values in zip(bound, arguments, strict=True)
                ],
                # The reference path is the optimal one. An agent has to look things up,
                # recover from a call that returned nothing useful, and sometimes re-read, so
                # a budget of the optimum plus two measures whether it can be lucky rather
                # than whether it can do the task.
                max_calls=max(len(bound) * 2, 6),
            )
        )

    return (
        EvalCorpus(
            corpus_id=corpus_id,
            service_id=ir.service.service_id,
            source_digest=ir.service.source_digest,
            authoring_note=sidecar.authoring_note,
            tasks=tasks,
        ),
        outstanding,
    )


class WithheldSolutionError(RuntimeError):
    """Raised when something tries to read an annotated solution that must stay unread."""


def read_goals_only(tasks_path: Path) -> list[str]:
    """Read a task set's goals while leaving its annotated solutions unread.

    A composition rule designed after seeing how somebody solved these tasks is fitted to
    them, however carefully it was derived. The Spotify set was read in full before that was
    understood, which is why it can no longer judge composition.

    This is the mechanism that keeps the next benchmark usable: the solutions are dropped here,
    inside the loader, so no caller can print them by accident and no reviewer has to take a
    promise on trust.
    """
    payload = json.loads(tasks_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise WithheldSolutionError(f"{tasks_path} is not a task list")
    return [str(item["query"]) for item in payload]


def goal_count(tasks_path: Path) -> int:
    """How many goals a task set contains, without reading anything else about them."""
    return len(read_goals_only(tasks_path))
