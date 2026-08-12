"""Reproducibility tests.

Required architecture item 6 states that generated artifacts are reproducible from
versioned source specifications. These tests are the evidence: parsing the same bytes twice
produces identical output, and that output matches a committed golden artifact.

Regenerate the goldens deliberately with `python scripts/regen_golden.py`, then review the
diff. An unexplained golden diff means a contract or parser change was wider than intended.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.contracts import dump_canonical
from api_mcp_compiler.evaluation.harness import run_corpus
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.ingest.wsdl import parse_wsdl
from api_mcp_compiler.models import ApiSemanticIR, EvalCorpus
from api_mcp_compiler.planning.baseline import plan_baseline
from api_mcp_compiler.planning.overlay import load_overlay
from api_mcp_compiler.planning.report import review_report
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy
from tests.conftest import ALL_EXAMPLES, GOLDEN_DIR, ORDER_SERVICE


def _ir(example: str) -> ApiSemanticIR:
    source = Path(example)
    return parse_wsdl(source) if source.suffix == ".wsdl" else parse_openapi(source)


def _semantic(example: str):  # type: ignore[no-untyped-def]
    """Plan with the committed overlay when one exists, matching golden regeneration."""
    ir = _ir(example)
    overlay_path = Path("examples/overlays") / f"{Path(example).stem}.overlay.json"
    overlay = load_overlay(overlay_path) if overlay_path.is_file() else None
    return ir, plan_semantic(ir, overlay)


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_parsing_twice_produces_identical_bytes(example: str) -> None:
    assert dump_canonical(_ir(example)) == dump_canonical(_ir(example))


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_planning_twice_produces_identical_bytes(example: str) -> None:
    assert dump_canonical(plan_baseline(_ir(example))) == dump_canonical(
        plan_baseline(_ir(example))
    )


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_ir_matches_the_golden_artifact(example: str) -> None:
    golden = GOLDEN_DIR / f"{Path(example).stem}.ir.json"
    assert golden.is_file(), "missing golden: run python scripts/regen_golden.py"
    assert dump_canonical(_ir(example)) == golden.read_text(encoding="utf-8"), (
        f"{example} IR drifted from {golden.name}; regenerate and review the diff"
    )


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_plan_matches_the_golden_artifact(example: str) -> None:
    golden = GOLDEN_DIR / f"{Path(example).stem}.plan.json"
    assert golden.is_file(), "missing golden: run python scripts/regen_golden.py"
    assert dump_canonical(plan_baseline(_ir(example))) == golden.read_text(encoding="utf-8"), (
        f"{example} baseline plan drifted from {golden.name}; regenerate and review the diff"
    )


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_surface_matches_the_golden_artifact(example: str) -> None:
    golden = GOLDEN_DIR / f"{Path(example).stem}.surface.json"
    assert golden.is_file(), "missing golden: run python scripts/regen_golden.py"
    ir = _ir(example)
    generated = dump_canonical(generate_surface(ir, plan_baseline(ir)))
    assert generated == golden.read_text(encoding="utf-8"), (
        f"{example} tool surface drifted from {golden.name}; regenerate and review the diff"
    )


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_semantic_plan_matches_the_golden_artifact(example: str) -> None:
    golden = GOLDEN_DIR / f"{Path(example).stem}.semantic-plan.json"
    assert golden.is_file(), "missing golden: run python scripts/regen_golden.py"
    _, plan = _semantic(example)
    assert dump_canonical(plan) == golden.read_text(encoding="utf-8"), (
        f"{example} semantic plan drifted from {golden.name}; regenerate and review the diff"
    )


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_semantic_surface_matches_the_golden_artifact(example: str) -> None:
    golden = GOLDEN_DIR / f"{Path(example).stem}.semantic-surface.json"
    assert golden.is_file(), "missing golden: run python scripts/regen_golden.py"
    ir, plan = _semantic(example)
    surface = generate_surface(ir, plan, synthesize_policy(ir, plan))
    assert dump_canonical(surface) == golden.read_text(encoding="utf-8")


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_policy_matches_the_golden_artifact(example: str) -> None:
    golden = GOLDEN_DIR / f"{Path(example).stem}.policy.json"
    assert golden.is_file(), "missing golden: run python scripts/regen_golden.py"
    ir, plan = _semantic(example)
    assert dump_canonical(synthesize_policy(ir, plan)) == golden.read_text(encoding="utf-8"), (
        f"{example} policy drifted from {golden.name}; regenerate and review the diff"
    )


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_review_report_matches_the_golden_artifact(example: str) -> None:
    golden = GOLDEN_DIR / f"{Path(example).stem}.review.md"
    assert golden.is_file(), "missing golden: run python scripts/regen_golden.py"
    ir, plan = _semantic(example)
    assert review_report(ir, plan) == golden.read_text(encoding="utf-8"), (
        f"{example} review report drifted from {golden.name}; regenerate and review the diff"
    )


def test_evaluation_run_matches_the_golden_artifact() -> None:
    golden = GOLDEN_DIR / "order_service.evaluation.json"
    assert golden.is_file(), "missing golden: run python scripts/regen_golden.py"
    ir, plan = _semantic(ORDER_SERVICE)
    manifest = synthesize_policy(ir, plan)
    corpus = EvalCorpus.model_validate(
        json.loads(Path("examples/evals/order_tasks.json").read_text(encoding="utf-8"))
    )
    run = run_corpus(corpus, ir, generate_surface(ir, plan, manifest), manifest)
    assert dump_canonical(run) == golden.read_text(encoding="utf-8"), (
        "evaluation run drifted from its golden; regenerate and review the diff"
    )


@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_source_digest_matches_the_bytes_on_disk(example: str) -> None:
    expected = hashlib.sha256(Path(example).read_bytes()).hexdigest()
    assert _ir(example).service.source_digest == f"sha256:{expected}"


def test_operations_keep_source_order_rather_than_alphabetical_order() -> None:
    """Sorting operations would silently reorder the tool surface between runs of a spec."""
    order = [item.operation_id for item in _ir(ORDER_SERVICE).operations]
    assert order == ["getCustomer", "listCustomerOrders", "createRefund", "approveRefund"]
    assert order != sorted(order)
