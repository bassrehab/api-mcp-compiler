"""Validate every committed example artifact against its versioned JSON Schema.

The schemas must validate the example artifacts. Pydantic
enforces the contracts inside Python, but the JSON Schemas are the language-independent
statement of the same contracts and the two can drift apart silently. This script is the
check that they have not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.contracts import (
    EVAL_CORPUS_SCHEMA,
    EVALUATION_RUN_SCHEMA,
    IR_SCHEMA,
    POLICY_MANIFEST_SCHEMA,
    PREREGISTRATION_SCHEMA,
    TOOL_OVERLAY_SCHEMA,
    TOOL_PLAN_SCHEMA,
    TOOL_SURFACE_SCHEMA,
    ContractViolation,
    load_schema,
    validate_eval_corpus,
    validate_ir,
    validate_overlay,
    validate_policy_manifest,
    validate_preregistration,
    validate_tool_plan,
    validate_tool_surface,
)
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.ingest.wsdl import parse_wsdl
from api_mcp_compiler.planning.baseline import plan_baseline
from api_mcp_compiler.planning.overlay import load_overlay
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy

REPO_ROOT = Path(__file__).resolve().parents[1]
ALL_SCHEMAS = (
    IR_SCHEMA,
    TOOL_PLAN_SCHEMA,
    TOOL_SURFACE_SCHEMA,
    TOOL_OVERLAY_SCHEMA,
    EVAL_CORPUS_SCHEMA,
    EVALUATION_RUN_SCHEMA,
    PREREGISTRATION_SCHEMA,
    POLICY_MANIFEST_SCHEMA,
)


def _check_schemas_are_wellformed(failures: list[str]) -> None:
    """Confirm each schema file is itself a legal Draft 2020-12 schema."""
    for name in ALL_SCHEMAS:
        try:
            Draft202012Validator.check_schema(load_schema(name))
        except Exception as error:  # reported to the caller, not handled
            failures.append(f"{name}: not a valid Draft 2020-12 schema: {error}")


def _check_specifications(failures: list[str]) -> int:
    """Parse every example specification and validate the artifacts it compiles to."""
    sources = sorted(
        [
            *(REPO_ROOT / "examples" / "openapi").glob("*.yaml"),
            *(REPO_ROOT / "examples" / "openapi").glob("*.yml"),
            *(REPO_ROOT / "examples" / "openapi").glob("*.json"),
        ]
    )
    sources += sorted((REPO_ROOT / "examples" / "wsdl").glob("*.wsdl"))
    for source in sources:
        relative = source.relative_to(REPO_ROOT)
        try:
            ir = (
                parse_wsdl(source)
                if source.suffix.lower() == ".wsdl"
                else parse_openapi(source)
            )
            validate_ir(ir.model_dump(mode="json"), label=f"IR for {relative}")
            plan = plan_baseline(ir)
            validate_tool_plan(
                plan.model_dump(mode="json"), label=f"baseline plan for {relative}"
            )
            surface = generate_surface(ir, plan)
            validate_tool_surface(
                surface.model_dump(mode="json"), label=f"tool surface for {relative}"
            )
            overlay_path = (
                REPO_ROOT / "examples" / "overlays" / f"{source.stem}.overlay.json"
            )
            overlay = None
            if overlay_path.is_file():
                overlay = load_overlay(overlay_path)
                validate_overlay(
                    overlay.model_dump(mode="json"),
                    label=f"overlay for {relative}",
                )
            semantic = plan_semantic(ir, overlay)
            validate_tool_plan(
                semantic.model_dump(mode="json"), label=f"semantic plan for {relative}"
            )
            manifest = synthesize_policy(ir, semantic)
            validate_policy_manifest(
                manifest.model_dump(mode="json"), label=f"policy for {relative}"
            )
            validate_tool_surface(
                generate_surface(ir, semantic, manifest).model_dump(mode="json"),
                label=f"semantic surface for {relative}",
            )
        except (ContractViolation, ValueError) as error:
            failures.append(f"{relative}: {error}")
            continue
        blocking = len(ir.blocking_ambiguities)
        print(
            f"  ok {relative}: {len(ir.operations)} operations, "
            f"{len(plan.artifacts)} artifacts, "
            f"{len(surface.executable_tools)}/{len(surface.tools)} executable, "
            f"{len(ir.ambiguities)} ambiguities ({blocking} blocking)"
        )
    return len(sources)


def _check_eval_tasks(failures: list[str]) -> int:
    """Validate every evaluation corpus."""
    count = 0
    for path in sorted((REPO_ROOT / "examples" / "evals").glob("*.json")):
        relative = path.relative_to(REPO_ROOT)
        payload = json.loads(path.read_text(encoding="utf-8"))
        try:
            validate_eval_corpus(payload, label=str(relative))
        except ContractViolation as error:
            failures.append(str(error))
            continue
        tasks = payload.get("tasks", [])
        count += len(tasks)
        print(f"  ok {relative}: {len(tasks)} tasks")
    return count


def _check_preregistrations(failures: list[str]) -> int:
    """Validate every committed pre-registration."""
    count = 0
    for path in sorted((REPO_ROOT / "preregistrations").glob("*.json")):
        relative = path.relative_to(REPO_ROOT)
        try:
            validate_preregistration(
                json.loads(path.read_text(encoding="utf-8")), label=str(relative)
            )
        except ContractViolation as error:
            failures.append(str(error))
            continue
        count += 1
        print(f"  ok {relative}")
    return count


def main() -> int:
    """Run every example check and report all failures rather than only the first."""
    failures: list[str] = []
    _check_schemas_are_wellformed(failures)
    specifications = _check_specifications(failures)
    tasks = _check_eval_tasks(failures)
    registrations = _check_preregistrations(failures)

    if failures:
        print(f"\n{len(failures)} example validation failure(s):", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(
        f"Validated {len(ALL_SCHEMAS)} schemas, {specifications} specifications, "
        f"{tasks} evaluation tasks and {registrations} pre-registration(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
