"""Loading, validation and canonical serialization of versioned contract artifacts.

Pydantic enforces the contracts inside Python. The JSON Schemas shipped alongside this
module are the language-independent statement of the same contracts, and the two can drift.
Everything
this compiler emits is therefore validated against the schema as well as the model, and a
test asserts that the two agree.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel

SCHEMA_DIR_ENV_VAR = "API_MCP_COMPILER_SCHEMA_DIR"

IR_SCHEMA = "api_semantic_ir.schema.json"
TOOL_PLAN_SCHEMA = "tool_plan.schema.json"
TOOL_SURFACE_SCHEMA = "mcp_tool_surface.schema.json"
TOOL_OVERLAY_SCHEMA = "tool_overlay.schema.json"
EVAL_CORPUS_SCHEMA = "eval_corpus.schema.json"
EVALUATION_RUN_SCHEMA = "evaluation_run.schema.json"
PREREGISTRATION_SCHEMA = "preregistration.schema.json"
POLICY_MANIFEST_SCHEMA = "policy_manifest.schema.json"


class ContractViolation(Exception):
    """Raised when an artifact does not satisfy its declared JSON Schema."""


def schema_dir() -> Path:
    """Return the directory holding the versioned JSON Schemas.

    The schemas ship inside the package rather than beside it, so an installed distribution
    can validate its own artifacts with no repository checkout present. They were briefly
    kept at the repository root, which worked in development and shipped a wheel that could
    not validate anything; `scripts/check_packaging.py` now proves the built distribution
    carries them.

    The override exists for callers who need to validate against a different revision of the
    contracts than the one they have installed.
    """
    override = os.environ.get(SCHEMA_DIR_ENV_VAR)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "schemas"


def load_schema(name: str) -> dict[str, Any]:
    """Load one JSON Schema by file name."""
    path = schema_dir() / name
    if not path.is_file():
        raise ContractViolation(
            f"schema {name!r} not found at {path}; set {SCHEMA_DIR_ENV_VAR} to override"
        )
    loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ContractViolation(f"schema {name!r} must be a JSON object")
    return loaded


def validate_document(payload: Any, schema_name: str, *, label: str = "document") -> None:
    """Validate one payload against a named schema, reporting every error at once.

    Reporting only the first error would hide the rest of a contract drift behind whichever
    field happened to sort first.
    """
    validator = Draft202012Validator(load_schema(schema_name))
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.absolute_path))
    if not errors:
        return
    lines = [
        f"  at /{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
        for error in errors
    ]
    raise ContractViolation(
        f"{label} failed validation against {schema_name}:\n" + "\n".join(lines)
    )


def validate_ir(payload: Any, *, label: str = "IR document") -> None:
    """Validate an API Semantic IR payload."""
    validate_document(payload, IR_SCHEMA, label=label)


def validate_tool_plan(payload: Any, *, label: str = "tool plan") -> None:
    """Validate a tool-surface plan payload."""
    validate_document(payload, TOOL_PLAN_SCHEMA, label=label)


def validate_tool_surface(payload: Any, *, label: str = "tool surface") -> None:
    """Validate a generated tool-surface payload."""
    validate_document(payload, TOOL_SURFACE_SCHEMA, label=label)


def validate_overlay(payload: Any, *, label: str = "overlay") -> None:
    """Validate a tool-overlay payload."""
    validate_document(payload, TOOL_OVERLAY_SCHEMA, label=label)


def validate_policy_manifest(payload: Any, *, label: str = "policy manifest") -> None:
    """Validate a policy-manifest payload."""
    validate_document(payload, POLICY_MANIFEST_SCHEMA, label=label)


def validate_preregistration(payload: Any, *, label: str = "pre-registration") -> None:
    """Validate a pre-registration payload."""
    validate_document(payload, PREREGISTRATION_SCHEMA, label=label)


def validate_eval_corpus(payload: Any, *, label: str = "eval corpus") -> None:
    """Validate an evaluation corpus payload."""
    validate_document(payload, EVAL_CORPUS_SCHEMA, label=label)


def validate_evaluation_run(payload: Any, *, label: str = "evaluation run") -> None:
    """Validate an evaluation run payload."""
    validate_document(payload, EVALUATION_RUN_SCHEMA, label=label)


def canonical_json(payload: Any) -> str:
    """Serialize a payload to the canonical form used for golden artifacts.

    Object keys are sorted so that dictionary insertion order cannot change the bytes.
    Sequences are never reordered, because operation order is source order and is itself
    part of the contract.
    """
    return (
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            indent=2,
            separators=(",", ": "),
        )
        + "\n"
    )


def dump_canonical(model: BaseModel) -> str:
    """Serialize a contract model to canonical JSON."""
    return canonical_json(model.model_dump(mode="json"))
