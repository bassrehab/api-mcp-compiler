# Contracts

Every artifact this compiler produces is described twice: by a Pydantic model inside Python,
and by a JSON Schema that states the same contract in a language-independent way. Both are
enforced, and a test asserts they agree, because two statements of one contract will drift.

## The schemas

They ship inside the package, at `api_mcp_compiler/schemas/`, so an installed copy can validate
its own artifacts with no repository checkout present.

| Schema | Artifact |
|---|---|
| `api_semantic_ir.schema.json` | The normalized intermediate representation. |
| `tool_plan.schema.json` | A planner's proposals, with rationale and confidence. |
| `tool_overlay.schema.json` | Human decisions, bound by digest to a specification revision. |
| `policy_manifest.schema.json` | Governance derived per tool. |
| `mcp_tool_surface.schema.json` | The generated surface, including refused tools. |
| `eval_corpus.schema.json` | Tasks and oracles. |
| `evaluation_run.schema.json` | A scored run. |
| `preregistration.schema.json` | A hypothesis fixed before a run. |

## Versioning

Each contract carries a `schema_version` pinned with `const` in its schema and to a constant in
Python. Contracts are versioned **independently**: when nothing in the tool plan changed, its
version stays put while the IR moves. Moving both in lockstep would be less truthful, not more
consistent.

A document written against a different version fails validation loudly rather than being
misread.

## How to validate

```python
from api_mcp_compiler.contracts import validate_ir, validate_tool_plan

validate_ir(payload)
validate_tool_plan(payload)
```

Validation reports **every** error at once, sorted by position. Reporting only the first would
hide the rest of a contract drift behind whichever field happened to sort first.

To validate against a different revision of the contracts than the one installed, point
`API_MCP_COMPILER_SCHEMA_DIR` at another directory.

## Canonical JSON

Artifacts are serialised with sorted object keys, so dictionary insertion order cannot change
the bytes, and with sequences left in place, because operation order is source order and is
part of the contract. This is what makes digests and golden artifacts meaningful.

## Models

The Pydantic models are frozen and forbid unknown fields. A field carrying an inference must
carry provenance for it, enforced by a base-class validator rather than by convention, so a new
adapter cannot forget.
