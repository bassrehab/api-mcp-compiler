"""Ingesting a query catalogue.

The adapter for the one source that has no specification, and the reason this compiler can
address databases without becoming a query builder. `docs/query-catalogue.md` explains the
argument; this file is the mechanism.

## The derivation that has no equivalent elsewhere

Everywhere else in this compiler, an operation's danger is inferred from a label somebody chose
to fit a protocol. `DELETE /claims/{id}` and `DELETE /claims` are the same method and the same
risk class, and they differ by a company.

A statement says which it is:

    DELETE FROM claims WHERE id = :id     one row
    DELETE FROM claims                    the company

So a mutating statement with no predicate is recorded as an **unbounded mutation** and raises a
blocking ambiguity. Not an error: truncating a staging table nightly is a real thing somebody
means to do. The point is that it must be somebody's decision rather than a default, and it is
the sharpest such distinction anywhere in this project.

## The override that only goes one way

A catalogue may declare `side_effect` on a query, and it may only make it **more** dangerous
than the verb implies. Marking a `DELETE` as a read would be a way to route a destructive
statement past the gate by editing a document, and a gate that can be edited past is not one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from api_mcp_compiler.ingest.documents import load_document
from api_mcp_compiler.models import (
    Ambiguity,
    ApiSemanticIR,
    Derivation,
    DocumentRole,
    FieldIR,
    Idempotency,
    OperationIR,
    ParameterLocation,
    Protocol,
    Provenance,
    ResponseIR,
    ServiceIR,
    SourceDocumentIR,
    SourceFormat,
)
from api_mcp_compiler.provenance import json_pointer, operation_identifier, slug

#: The verb that opens a statement, and what it does. Ordered longest first so `CREATE TABLE`
#: is not read as `CREATE`.
_VERBS: dict[str, str] = {
    "SELECT": "read",
    "WITH": "read",
    "SHOW": "read",
    "EXPLAIN": "read",
    "INSERT": "write",
    "UPDATE": "write",
    "UPSERT": "write",
    "MERGE": "write",
    "REPLACE": "write",
    "DELETE": "destructive",
    "DROP": "destructive",
    "TRUNCATE": "destructive",
    "ALTER": "destructive",
    # GRANT and REVOKE change permissions. `privileged` is a risk class rather than a side
    # effect, so they are written as `write` here and raised separately below: a statement that
    # hands out authority is not the same kind of write as one that sets a column, and folding
    # them together would lose the distinction entirely.
    "GRANT": "write",
    "REVOKE": "write",
    "CREATE": "write",
}

#: Verbs that change who may do what. Recorded as an ambiguity rather than a risk class,
#: because the IR has no side effect for it and inventing one would be a contract change made
#: sideways.
_PERMISSION_VERBS = {"GRANT", "REVOKE"}

#: Ranked, so an override can be checked for direction. A catalogue may raise a query's class
#: and never lower it. Only the classes the IR actually has: `privileged` is a risk class the
#: planner assigns and is not a side effect anybody can declare here.
_SEVERITY = {"read": 0, "write": 1, "destructive": 2}

#: Statements that change something. A read with no predicate returns a large result, which is
#: an output-size question the policy layer already answers; a write with none is a different
#: kind of problem.
_MUTATING = {"write", "destructive"}

_PLACEHOLDER = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")
_WHERE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)

_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


class CatalogueIngestionError(ValueError):
    """Raised when a catalogue cannot be read at all."""


def is_catalogue(payload: Any) -> bool:
    """Whether a loaded document is a query catalogue.

    Dispatched on the marker rather than the file extension, because a catalogue and an
    OpenAPI document are both YAML and a caller should not have to say which.
    """
    return isinstance(payload, dict) and "catalogue" in payload and "queries" in payload


def parse_catalogue(path: Path) -> ApiSemanticIR:
    """Compile a query catalogue into the IR."""
    payload, digest = load_document(path)

    if not is_catalogue(payload):
        raise CatalogueIngestionError(
            f"{path.name} is not a query catalogue: it declares no `catalogue` version and "
            "no `queries`."
        )
    if payload.get("catalogue") != 1:
        raise CatalogueIngestionError(
            f"{path.name} declares catalogue version {payload.get('catalogue')!r}, and this "
            "compiler understands 1."
        )

    # No consumption ledger. A catalogue is small enough that every key it may carry is read
    # explicitly, and anything unrecognised is reported below rather than swept.
    ambiguities: list[Ambiguity] = []

    service_block = payload.get("service")
    if not isinstance(service_block, dict) or not service_block.get("name"):
        raise CatalogueIngestionError(f"{path.name} declares no `service.name`.")

    operations = []
    seen: set[str] = set()
    queries = payload.get("queries")
    if not isinstance(queries, list) or not queries:
        raise CatalogueIngestionError(f"{path.name} declares no queries.")

    for index, entry in enumerate(queries):
        if not isinstance(entry, dict):
            raise CatalogueIngestionError(f"queries[{index}] is not a mapping.")
        operations.append(_operation(entry, index, ambiguities, seen))

    # A key this compiler does not read is reported rather than dropped, which is the same
    # discipline the other adapters apply through a consumption ledger. A catalogue is small
    # enough that naming the three keys it may carry is clearer than tracking every access.
    for unknown in sorted(set(payload) - {"catalogue", "service", "queries"}):
        ambiguities.append(
            Ambiguity(
                code="unmapped_catalogue_key",
                field=unknown,
                source_pointer=json_pointer(unknown),
                detail=(
                    f"{unknown!r} is not a key this compiler reads. It is recorded so it is "
                    "not silently ignored, and it affects nothing."
                ),
                blocking=False,
            )
        )

    return ApiSemanticIR(
        service=_service(service_block, path, digest),
        operations=operations,
        ambiguities=ambiguities,
    )


def _service(block: dict[str, Any], path: Path, digest: str) -> ServiceIR:
    name = str(block["name"])
    return ServiceIR(
        service_id=slug(name),
        title=name,
        version=str(block.get("version") or "unversioned"),
        source_format=SourceFormat.CATALOGUE,
        source_uri=path.as_posix(),
        source_digest=digest,
        description=block.get("description"),
        source_documents=[
            SourceDocumentIR(uri=path.as_posix(), digest=digest, role=DocumentRole.ROOT)
        ],
        # No servers. A catalogue names a connection the deployment resolves, and putting a
        # connection string in a specification puts a credential in version control.
        servers=[],
        provenance=[
            Provenance(
                field="title",
                source_pointer=json_pointer("service", "name"),
                derivation=Derivation.SOURCE,
                rule="catalogue.service.name",
            ),
            Provenance(
                field="service_id",
                source_pointer=json_pointer("service", "name"),
                derivation=Derivation.NORMALIZED,
                rule="catalogue.service_id.slug",
            ),
            Provenance(
                field="version",
                source_pointer=json_pointer("service", "version"),
                derivation=Derivation.SOURCE if block.get("version") else Derivation.DEFAULT,
                rule="catalogue.service.version",
            ),
            Provenance(
                field="description",
                source_pointer=json_pointer("service", "description"),
                derivation=(
                    Derivation.SOURCE if block.get("description") else Derivation.DEFAULT
                ),
                rule="catalogue.service.description",
            ),
            Provenance(
                field="source_format",
                source_pointer=json_pointer("catalogue"),
                derivation=Derivation.NORMALIZED,
                rule="catalogue.format.marker",
            ),
            Provenance(
                field="source_uri",
                source_pointer=json_pointer(),
                derivation=Derivation.NORMALIZED,
                rule="catalogue.source.path",
            ),
            Provenance(
                field="source_digest",
                source_pointer=json_pointer(),
                derivation=Derivation.NORMALIZED,
                rule="catalogue.source.digest",
            ),
            Provenance(
                field="source_documents",
                source_pointer=json_pointer(),
                derivation=Derivation.NORMALIZED,
                rule="catalogue.source.single_document",
            ),
        ],
    )


def _operation(
    entry: dict[str, Any], index: int, ambiguities: list[Ambiguity], seen: set[str]
) -> OperationIR:
    pointer = json_pointer("queries", str(index))
    identifier = str(entry.get("id") or "").strip()
    if not identifier:
        raise CatalogueIngestionError(f"queries[{index}] declares no `id`.")
    if identifier in seen:
        raise CatalogueIngestionError(
            f"queries[{index}] repeats the id {identifier!r}. An identifier is what a decision "
            "binds to, so two queries sharing one means an approval cannot say which it was "
            "about."
        )
    seen.add(identifier)

    statement = str(entry.get("statement") or "").strip()
    if not statement:
        raise CatalogueIngestionError(f"{identifier} declares no `statement`.")

    side_effect, effect_records = _side_effect(entry, statement, pointer, identifier, ambiguities)
    inputs = _parameters(entry, statement, pointer, identifier)
    _unbounded(statement, side_effect, pointer, identifier, ambiguities)

    return OperationIR(
        operation_id=operation_identifier(identifier),
        protocol=Protocol.SQL,
        source_pointer=pointer,
        # No route. The statement is the operation, and it is carried in the description
        # rather than invented into a path that no caller would use.
        route=None,
        intent=str(entry.get("summary") or identifier),
        side_effect=side_effect,
        idempotency=_idempotency(side_effect),
        description=_description(entry, statement),
        deprecated=bool(entry.get("deprecated", False)),
        tags=[str(item) for item in entry.get("tags", []) if isinstance(item, str)],
        inputs=inputs,
        outputs=_outputs(entry, pointer),
        provenance=[
            Provenance(
                field="operation_id",
                source_pointer=json_pointer("queries", str(index), "id"),
                derivation=Derivation.SOURCE,
                rule="catalogue.query.id",
            ),
            Provenance(
                field="intent",
                source_pointer=json_pointer("queries", str(index), "summary"),
                derivation=Derivation.SOURCE if entry.get("summary") else Derivation.DEFAULT,
                rule="catalogue.query.summary",
            ),
            Provenance(
                field="protocol",
                source_pointer=pointer,
                derivation=Derivation.NORMALIZED,
                rule="catalogue.protocol.sql",
            ),
            Provenance(
                field="source_pointer",
                source_pointer=pointer,
                derivation=Derivation.NORMALIZED,
                rule="catalogue.query.position",
            ),
            Provenance(
                field="description",
                source_pointer=json_pointer("queries", str(index), "statement"),
                # Normalized rather than source: the statement itself is carried into the
                # description, because a reviewer deciding whether to expose a query needs to
                # read the query rather than a summary of it.
                derivation=Derivation.NORMALIZED,
                rule="catalogue.description.includes_statement",
            ),
            Provenance(
                field="deprecated",
                source_pointer=pointer,
                derivation=Derivation.SOURCE if "deprecated" in entry else Derivation.DEFAULT,
                rule="catalogue.query.deprecated",
            ),
            Provenance(
                field="idempotency",
                source_pointer=pointer,
                derivation=Derivation.INFERRED,
                rule="catalogue.idempotency.from_side_effect",
                # A read run twice returns the same rows. Anything else is unknown, because
                # `SET n = n + 1` and `SET n = 1` are both updates.
                confidence=0.9,
            ),
            *effect_records,
        ],
    )


def _side_effect(
    entry: dict[str, Any],
    statement: str,
    pointer: str,
    identifier: str,
    ambiguities: list[Ambiguity],
) -> tuple[Any, list[Provenance]]:
    """Classify from the verb, allowing a declaration to raise it and never to lower it."""
    from api_mcp_compiler.models import SideEffectClass

    cleaned = _COMMENT.sub(" ", statement).strip()
    first = cleaned.split(None, 1)[0].upper() if cleaned else ""
    derived = _VERBS.get(first)

    if derived is None:
        ambiguities.append(
            Ambiguity(
                code="unclassified_statement_verb",
                field=f"operations.{operation_identifier(identifier)}.side_effect",
                source_pointer=pointer,
                detail=(
                    f"{identifier} opens with {first!r}, which this compiler does not "
                    "recognise as a statement verb. Nothing infers what it does, so no tool "
                    "is emitted until somebody records the answer."
                ),
                blocking=True,
            )
        )
        return SideEffectClass.UNKNOWN, [
            Provenance(
                field="side_effect",
                source_pointer=pointer,
                derivation=Derivation.INFERRED,
                rule="catalogue.side_effect.unrecognised_verb",
                confidence=0.0,
            )
        ]

    if first in _PERMISSION_VERBS:
        ambiguities.append(
            Ambiguity(
                code="permission_changing_statement",
                field=f"operations.{operation_identifier(identifier)}.side_effect",
                source_pointer=pointer,
                detail=(
                    f"{identifier} opens with {first}, which changes who may do what rather "
                    "than what the data says. An agent able to grant permissions can grant "
                    "itself more, so this needs a person however narrow the statement looks."
                ),
                blocking=True,
            )
        )

    declared = entry.get("side_effect")
    if declared is None:
        return SideEffectClass(derived), [
            Provenance(
                field="side_effect",
                source_pointer=pointer,
                derivation=Derivation.INFERRED,
                rule=f"catalogue.side_effect.verb.{first.lower()}",
                # A verb is a stronger signal than an HTTP method: somebody chose it to
                # describe the statement rather than to fit a protocol.
                confidence=0.95,
            )
        ]

    declared = str(declared)
    if declared not in _SEVERITY:
        raise CatalogueIngestionError(
            f"{identifier} declares side_effect {declared!r}. One of {sorted(_SEVERITY)}."
        )
    if _SEVERITY[declared] < _SEVERITY[derived]:
        raise CatalogueIngestionError(
            f"{identifier} is a {first} statement, which this compiler classifies as "
            f"{derived}, and the catalogue declares it {declared}. A declaration may only "
            "raise a query's class. Lowering one would route a destructive statement past "
            "the gate by editing a document."
        )

    return SideEffectClass(declared), [
        Provenance(
            field="side_effect",
            source_pointer=json_pointer(pointer.lstrip("#/"), "side_effect"),
            derivation=Derivation.SOURCE,
            rule="catalogue.side_effect.declared",
            confidence=1.0,
        )
    ]


def _unbounded(
    statement: str,
    side_effect: Any,
    pointer: str,
    identifier: str,
    ambiguities: list[Ambiguity],
) -> None:
    """Raise a blocking ambiguity for a mutation with no predicate.

    Not an error. Truncating a staging table nightly is a real thing somebody means to do, and
    the point is that it must be somebody's decision rather than a default.
    """
    if side_effect.value not in _MUTATING:
        return
    if _WHERE.search(_COMMENT.sub(" ", statement)):
        return

    ambiguities.append(
        Ambiguity(
            code="unbounded_mutation",
            field=f"operations.{operation_identifier(identifier)}.side_effect",
            source_pointer=pointer,
            detail=(
                f"{identifier} changes state and has no WHERE clause, so it acts on every row "
                "it can reach. `DELETE FROM claims WHERE id = :id` and `DELETE FROM claims` "
                "differ by a clause and by a company. This may be exactly what was meant, and "
                "it needs to be somebody's decision rather than a default."
            ),
            blocking=True,
        )
    )


def _parameters(
    entry: dict[str, Any], statement: str, pointer: str, identifier: str
) -> list[FieldIR]:
    """Every placeholder, typed from the declaration.

    A placeholder with no declaration is an error rather than a guess: inventing a type would
    produce a tool an agent can call wrongly with no way to know it did.
    """
    placeholders = list(dict.fromkeys(_PLACEHOLDER.findall(_COMMENT.sub(" ", statement))))
    declared = {
        str(item.get("name")): item
        for item in entry.get("parameters", [])
        if isinstance(item, dict) and item.get("name")
    }

    missing = [name for name in placeholders if name not in declared]
    if missing:
        raise CatalogueIngestionError(
            f"{identifier} uses {missing} in its statement and declares no parameter for "
            "them. A parameter with no declared type would become a tool argument an agent "
            "can fill wrongly with no way to know."
        )

    unused = sorted(set(declared) - set(placeholders))
    if unused:
        raise CatalogueIngestionError(
            f"{identifier} declares parameters {unused} that its statement never uses. An "
            "argument that reaches nothing is one an agent will spend a call filling in."
        )

    fields = []
    for position, name in enumerate(placeholders):
        item = declared[name]
        kind = str(item.get("type") or "string")
        if kind not in _TYPES:
            raise CatalogueIngestionError(
                f"{identifier} declares parameter {name!r} as type {kind!r}. One of "
                f"{sorted(_TYPES)}."
            )
        at = json_pointer(pointer.lstrip("#/"), "parameters", str(position))
        fields.append(
            FieldIR(
                name=name,
                # Every parameter is a bound value rather than a path or query segment: a
                # statement has no URL, and the binding is what the driver substitutes.
                location=ParameterLocation.BODY,
                required=bool(item.get("required", True)),
                type_schema={"type": kind},
                description=item.get("description"),
                deprecated=bool(item.get("deprecated", False)),
                provenance=[
                    Provenance(
                        field="name",
                        source_pointer=at,
                        derivation=Derivation.SOURCE,
                        rule="catalogue.parameter.name",
                    ),
                    Provenance(
                        field="location",
                        source_pointer=at,
                        # Normalized rather than inferred: every catalogue parameter is a
                        # bound value by construction, so nothing is being guessed at.
                        derivation=Derivation.NORMALIZED,
                        rule="catalogue.parameter.bound_value",
                    ),
                    Provenance(
                        field="required",
                        source_pointer=at,
                        derivation=(
                            Derivation.SOURCE if "required" in item else Derivation.DEFAULT
                        ),
                        rule="catalogue.parameter.required",
                    ),
                    Provenance(
                        field="type_schema",
                        source_pointer=at,
                        derivation=Derivation.SOURCE if item.get("type") else Derivation.DEFAULT,
                        rule="catalogue.parameter.type",
                    ),
                    Provenance(
                        field="description",
                        source_pointer=at,
                        derivation=(
                            Derivation.SOURCE if item.get("description") else Derivation.DEFAULT
                        ),
                        rule="catalogue.parameter.description",
                    ),
                    Provenance(
                        field="deprecated",
                        source_pointer=at,
                        derivation=(
                            Derivation.SOURCE if "deprecated" in item else Derivation.DEFAULT
                        ),
                        rule="catalogue.parameter.deprecated",
                    ),
                ],
            )
        )
    return fields


def _outputs(entry: dict[str, Any], pointer: str) -> list[ResponseIR]:
    """The result shape, where the catalogue states one."""
    returns = entry.get("returns")
    if not isinstance(returns, list) or not returns:
        return [
            ResponseIR(
                status="200",
                description="Rows returned by the statement. The catalogue declares no shape.",
                provenance=[
                    Provenance(
                        field="status",
                        source_pointer=pointer,
                        derivation=Derivation.DEFAULT,
                        rule="catalogue.response.undeclared",
                    ),
                    Provenance(
                        field="description",
                        source_pointer=pointer,
                        derivation=Derivation.DEFAULT,
                        rule="catalogue.response.undeclared",
                    ),
                ],
            )
        ]

    properties = {}
    for column in returns:
        if not isinstance(column, dict) or not column.get("name"):
            continue
        properties[str(column["name"])] = {"type": str(column.get("type") or "string")}

    return [
        ResponseIR(
            status="200",
            description="Rows returned by the statement.",
            type_schema={
                "type": "array",
                "items": {"type": "object", "properties": properties},
            },
            provenance=[
                Provenance(
                    field="type_schema",
                    source_pointer=json_pointer(pointer.lstrip("#/"), "returns"),
                    derivation=Derivation.SOURCE,
                    rule="catalogue.response.returns",
                ),
                Provenance(
                    field="status",
                    source_pointer=pointer,
                    derivation=Derivation.DEFAULT,
                    rule="catalogue.response.status",
                ),
                Provenance(
                    field="description",
                    source_pointer=pointer,
                    derivation=Derivation.DEFAULT,
                    rule="catalogue.response.description",
                ),
            ],
        )
    ]


def _description(entry: dict[str, Any], statement: str) -> str:
    """The description, with the statement included.

    A reviewer deciding whether to expose a query needs to read the query. Summarising it
    would hide the clause that decides its blast radius, which is the one thing they are
    there to look at.
    """
    stated = str(entry.get("description") or "").strip()
    owner = str(entry.get("owner") or "").strip()

    parts = [stated] if stated else []
    parts.append(f"Runs: {' '.join(statement.split())}")
    if owner:
        parts.append(f"Owned by {owner}.")
    return "\n\n".join(parts)


def _idempotency(side_effect: Any) -> Idempotency:
    """Whether running it twice is the same as running it once.

    Only claimed where a statement makes it true. A read is idempotent; everything else is
    unknown, because `UPDATE ... SET n = n + 1` and `UPDATE ... SET n = 1` are both updates.
    """
    from api_mcp_compiler.models import SideEffectClass

    if side_effect is SideEffectClass.READ:
        return Idempotency.IDEMPOTENT
    return Idempotency.UNKNOWN
