"""Evaluation harness tests.

An oracle that has only ever been shown to pass is not evidence of anything, so most of
these tests drive the harness into failure deliberately: state left wrong, a prohibited
operation invoked, a mutation performed without confirmation. A harness that cannot fail
cannot measure.

The reference driver scores every surface identically by construction. That is asserted
here rather than treated as an incidental fact, because it is the reason this phase produces
no comparison.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.contracts import (
    canonical_json,
    dump_canonical,
    validate_eval_corpus,
    validate_evaluation_run,
)
from api_mcp_compiler.evaluation.harness import (
    CorpusMismatchError,
    ReferenceDriver,
    bind_operations,
    run_corpus,
    run_task,
    selection,
)
from api_mcp_compiler.evaluation.state import EffectKind, ServiceStore, derive_effect
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.models import (
    ApiSemanticIR,
    EvalCorpus,
    EvalTask,
    OracleKind,
    PlannerKind,
    ReviewStatus,
    StepOutcome,
    TaskOracle,
    ToolPlan,
    ToolSurface,
    TraceStep,
)
from api_mcp_compiler.planning.baseline import plan_baseline
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy
from tests.conftest import ORDER_SERVICE

CORPUS_PATH = "examples/evals/order_tasks.json"


def _ir() -> ApiSemanticIR:
    return parse_openapi(Path(ORDER_SERVICE))


def _corpus() -> EvalCorpus:
    return EvalCorpus.model_validate(json.loads(Path(CORPUS_PATH).read_text(encoding="utf-8")))


def _approved(plan: ToolPlan) -> ToolPlan:
    """Approve every artifact, so the harness can exercise the success path."""
    return plan.model_copy(
        update={
            "artifacts": [
                item.model_copy(update={"review_status": ReviewStatus.APPROVED})
                for item in plan.artifacts
            ]
        }
    )


def _task(corpus: EvalCorpus, task_id: str) -> EvalTask:
    return next(item for item in corpus.tasks if item.task_id == task_id)


def _variant(task: EvalTask, **overrides: Any) -> EvalTask:
    """Build a task variant through validation.

    `model_copy(update=...)` bypasses validation, so nested dictionaries stay dictionaries
    and only fail much later at the point of use.
    """
    return EvalTask.model_validate({**task.model_dump(mode="json"), **overrides})


def test_committed_corpus_validates() -> None:
    validate_eval_corpus(_corpus().model_dump(mode="json"), label=CORPUS_PATH)


def test_corpus_records_how_it_was_authored() -> None:
    """A reader has to be able to judge whether the corpus is independent of the surface."""
    note = _corpus().authoring_note
    assert "without reference to any" in note
    assert "synthetic" in note


def test_corpus_from_another_revision_is_refused() -> None:
    ir = _ir()
    stale = _corpus().model_copy(update={"source_digest": "sha256:" + "0" * 64})
    plan = plan_semantic(ir)
    with pytest.raises(CorpusMismatchError, match="re-author or re-stamp"):
        run_corpus(stale, ir, generate_surface(ir, plan))


@pytest.mark.parametrize("planner", [plan_baseline, plan_semantic])
def test_one_corpus_scores_both_surfaces(planner: Any) -> None:
    """Tasks name operations, so a single corpus describes both planners' output."""
    ir = _ir()
    plan = _approved(planner(ir))
    run = run_corpus(_corpus(), ir, generate_surface(ir, plan))
    assert run.success_rate == 1.0
    assert not any(item.unmapped_operations for item in run.results)


def test_the_reference_driver_cannot_distinguish_surfaces() -> None:
    """This is why a replay driver produces no comparison, asserted rather than assumed."""
    ir = _ir()
    baseline = run_corpus(_corpus(), ir, generate_surface(ir, _approved(plan_baseline(ir))))
    semantic = run_corpus(_corpus(), ir, generate_surface(ir, _approved(plan_semantic(ir))))
    assert baseline.success_rate == semantic.success_rate
    assert [item.success for item in baseline.results] == [
        item.success for item in semantic.results
    ]


def test_operations_bind_to_a_direct_tool_rather_than_a_composite() -> None:
    """A composite performs steps the task did not ask for."""
    ir = _ir()
    surface = generate_surface(ir, _approved(plan_semantic(ir)))
    binding = bind_operations(surface)
    assert binding["createRefund"].kind.value == "tool"


def test_success_is_decided_by_final_state_not_by_the_trace() -> None:
    """The reference steps run identically; only the resulting state differs."""
    ir = _ir()
    surface = generate_surface(ir, _approved(plan_semantic(ir)))
    corpus = _corpus()
    task = _task(corpus, "create-a-refund-request")

    passing = run_task(task, ir, surface, None, ReferenceDriver())
    assert passing.success is True

    oracles = task.model_dump(mode="json")["oracles"]
    for oracle in oracles:
        if oracle["kind"] == OracleKind.FINAL_STATE.value:
            for assertion in oracle["assertions"]:
                if assertion.get("field") == "order_id":
                    assertion["equals"] = "O-NOT-THIS"
    impossible = _variant(task, oracles=oracles)
    failing = run_task(impossible, ir, surface, None, ReferenceDriver())
    assert failing.success is False
    assert [item.outcome for item in passing.trace] == [item.outcome for item in failing.trace]


def test_a_read_task_cannot_be_passed_by_doing_nothing() -> None:
    """Every other read-side oracle is negative, so an idle agent satisfies them all.

    Verified as a real defect before the retrieval oracle existed: the committed lookup task
    passed with zero calls.
    """
    ir = _ir()
    surface = generate_surface(ir, _approved(plan_semantic(ir)))
    task = _task(_corpus(), "look-up-a-customer")

    working = run_task(task, ir, surface, None, ReferenceDriver())
    assert working.success is True

    idle = run_task(_variant(task, reference_solution=[]), ir, surface, None, ReferenceDriver())
    assert idle.calls == 0
    assert idle.success is False
    failed = next(
        item for item in idle.oracle_results if item.kind is OracleKind.RETRIEVAL
    )
    assert not failed.passed
    assert "nothing was returned" in failed.detail


def test_every_read_task_in_the_corpus_carries_a_retrieval_oracle() -> None:
    """A read task without one is vacuous, so this is checked rather than trusted."""
    for task in _corpus().tasks:
        mutating = any(
            step.operation_id in {"createRefund", "approveRefund"}
            for step in task.reference_solution
        )
        if not mutating:
            assert any(item.kind is OracleKind.RETRIEVAL for item in task.oracles), task.task_id


def test_retrieval_checks_the_response_not_the_state() -> None:
    """A value written to the store but never returned must not satisfy retrieval."""
    ir = _ir()
    surface = generate_surface(ir, _approved(plan_semantic(ir)))
    task = _variant(
        _task(_corpus(), "look-up-a-customer"),
        oracles=[
            {
                "kind": "retrieval",
                "description": "A value that exists in the fixture but is never returned.",
                "retrieval_assertions": [{"field": "name", "equals": "Nobody At All"}],
            }
        ],
    )
    assert run_task(task, ir, surface, None, ReferenceDriver()).success is False


def test_a_retrieval_oracle_must_carry_an_assertion() -> None:
    """An empty one would pass on an empty run, which is the bug it exists to prevent."""
    with pytest.raises(ValueError, match="at least one assertion"):
        TaskOracle(kind=OracleKind.RETRIEVAL, description="empty")


def test_no_mutation_oracle_fails_when_state_changes() -> None:
    ir = _ir()
    surface = generate_surface(ir, _approved(plan_semantic(ir)))
    corpus = _corpus()
    lookup = _task(corpus, "look-up-a-customer")
    mutating = _variant(
        lookup,
        reference_solution=[
            {
                "operation_id": "createRefund",
                "arguments": {"body": {"order_id": "O-1", "reason": "test"}},
            }
        ],
        prohibited_operations=[],
    )
    result = run_task(mutating, ir, surface, None, ReferenceDriver())
    assert result.success is False
    assert any(
        item.kind is OracleKind.NO_MUTATION and not item.passed
        for item in result.oracle_results
    )


def test_prohibited_operation_is_counted_as_an_unsafe_action() -> None:
    ir = _ir()
    surface = generate_surface(ir, _approved(plan_semantic(ir)))
    corpus = _corpus()
    lookup = _task(corpus, "look-up-a-customer")
    violating = _variant(
        lookup,
        reference_solution=[
            {
                "operation_id": "createRefund",
                "arguments": {"body": {"order_id": "O-1", "reason": "test"}},
            }
        ],
    )
    result = run_task(violating, ir, surface, None, ReferenceDriver())
    assert result.unsafe_actions == 1
    assert any(
        item.kind is OracleKind.PROHIBITED_OPERATIONS and not item.passed
        for item in result.oracle_results
    )


def test_invalid_arguments_are_counted_and_do_not_mutate_state() -> None:
    """A call the surface would reject must not change state behind the metric's back."""
    ir = _ir()
    surface = generate_surface(ir, _approved(plan_semantic(ir)))
    corpus = _corpus()
    task = _variant(
        _task(corpus, "create-a-refund-request"),
        reference_solution=[{"operation_id": "createRefund", "arguments": {"nonsense": 1}}],
    )
    result = run_task(task, ir, surface, None, ReferenceDriver())
    assert result.invalid_argument_calls == 1
    assert result.trace[0].outcome is StepOutcome.REFUSED_ARGUMENTS
    assert result.success is False


def test_valid_arguments_are_not_counted_as_invalid() -> None:
    ir = _ir()
    surface = generate_surface(ir, _approved(plan_semantic(ir)))
    result = run_task(
        _task(_corpus(), "create-a-refund-request"), ir, surface, None, ReferenceDriver()
    )
    assert result.invalid_argument_calls == 0


def test_governance_is_visible_in_the_results() -> None:
    """An unapproved write cannot succeed, and the harness reports why rather than hiding it."""
    ir = _ir()
    plan = plan_semantic(ir)
    manifest = synthesize_policy(ir, plan)
    run = run_corpus(_corpus(), ir, generate_surface(ir, plan, manifest), manifest)
    refund = next(item for item in run.results if item.task_id == "create-a-refund-request")
    assert refund.success is False
    refusal = refund.trace[0]
    assert refusal.outcome is StepOutcome.REFUSED_DISABLED
    assert "awaiting_approval" in (refusal.detail or "")


def test_an_operation_the_surface_omits_is_reported_not_silently_skipped() -> None:
    ir = _ir()
    plan = _approved(plan_semantic(ir))
    trimmed = plan.model_copy(
        update={"artifacts": [item for item in plan.artifacts if "refund" not in item.name]}
    )
    corpus = _corpus()
    result = run_task(
        _task(corpus, "create-a-refund-request"),
        ir,
        generate_surface(ir, trimmed),
        None,
        ReferenceDriver(),
    )
    assert result.unmapped_operations == ["createRefund"]
    assert result.trace[0].outcome is StepOutcome.UNMAPPED


def test_latency_and_token_cost_are_null_not_invented() -> None:
    """A fabricated number placed beside measured ones is indistinguishable from a measured one."""
    ir = _ir()
    run = run_corpus(_corpus(), ir, generate_surface(ir, _approved(plan_semantic(ir))))
    assert all(item.latency_ms is None for item in run.results)
    assert all(item.token_cost is None for item in run.results)


def test_runs_are_reproducible() -> None:
    ir = _ir()
    surface = generate_surface(ir, _approved(plan_semantic(ir)))
    first = run_corpus(_corpus(), ir, surface)
    second = run_corpus(_corpus(), ir, surface)
    assert dump_canonical(first) == dump_canonical(second)


def test_run_validates_against_the_schema() -> None:
    ir = _ir()
    run = run_corpus(_corpus(), ir, generate_surface(ir, _approved(plan_semantic(ir))))
    validate_evaluation_run(run.model_dump(mode="json"), label="order run")
    assert run.planner is PlannerKind.SEMANTIC


def test_max_calls_bounds_a_run() -> None:
    ir = _ir()
    surface = generate_surface(ir, _approved(plan_semantic(ir)))
    corpus = _corpus()
    task = _variant(_task(corpus, "refund-requires-approval-step"), max_calls=1)
    assert run_task(task, ir, surface, None, ReferenceDriver()).calls == 1


# --- the store's effect model -------------------------------------------------------


def test_reads_never_mutate() -> None:
    ir = _ir()
    operation = next(item for item in ir.operations if item.operation_id == "getCustomer")
    effect = derive_effect(operation)
    assert effect.kind is EffectKind.READ
    store = ServiceStore.from_fixture({"customers": {"C-1": {"id": "C-1"}}})
    before = store.snapshot()
    store.apply(effect, {"customer_id": "C-1"}, seed="t")
    assert store.snapshot() == before


def test_a_collection_write_creates_a_record() -> None:
    ir = _ir()
    operation = next(item for item in ir.operations if item.operation_id == "createRefund")
    effect = derive_effect(operation)
    assert effect.kind is EffectKind.CREATE
    store = ServiceStore.from_fixture({})
    store.apply(effect, {"body": {"order_id": "O-9"}}, seed="t")
    assert len(store.records("refunds")) == 1


def test_an_action_segment_updates_and_records_the_action() -> None:
    ir = _ir()
    operation = next(item for item in ir.operations if item.operation_id == "approveRefund")
    effect = derive_effect(operation)
    assert effect.kind is EffectKind.UPDATE
    assert effect.action == "approve"
    store = ServiceStore.from_fixture({"refunds": {"R-1": {"id": "R-1"}}})
    store.apply(effect, {"refund_id": "R-1"}, seed="t")
    assert store.records("refunds")["R-1"]["last_action"] == "approve"


def test_a_snapshot_cannot_be_used_to_mutate_the_store() -> None:
    store = ServiceStore.from_fixture({"customers": {"C-1": {"id": "C-1"}}})
    snapshot = store.snapshot()
    snapshot["customers"]["C-1"]["name"] = "injected"
    assert "name" not in store.records("customers")["C-1"]


def test_fixture_seeding_is_independent_between_runs() -> None:
    fixture: dict[str, dict[str, dict[str, Any]]] = {"refunds": {}}
    first = ServiceStore.from_fixture(fixture)
    effect = derive_effect(
        next(item for item in _ir().operations if item.operation_id == "createRefund")
    )
    first.apply(effect, {"body": {"order_id": "O-1"}}, seed="t")
    second = ServiceStore.from_fixture(fixture)
    assert second.records("refunds") == {}
    assert canonical_json(fixture) == canonical_json({"refunds": {}})


def test_a_driver_is_asked_once_per_step_and_can_read_what_came_back() -> None:
    """An agent cannot name an identifier a lookup has not returned yet.

    The driver protocol used to ask for a whole plan up front, which no agent can supply.
    """
    ir = parse_openapi(Path(ORDER_SERVICE))
    surface = generate_surface(ir, _approved(plan_baseline(ir)))
    seen: list[int] = []

    class _Observer:
        name = "observer"

        def next_call(
            self, task: EvalTask, surface: ToolSurface, trace: list[TraceStep]
        ) -> tuple[str, dict[str, object]] | None:
            seen.append(len(trace))
            if len(trace) >= 2:
                return None
            return ir.operations[0].operation_id, {}

    task = EvalTask(
        task_id="observes-the-trace",
        goal="stop after two calls",
        oracles=[TaskOracle(kind=OracleKind.NO_MUTATION, description="nothing changes")],
        max_calls=6,
    )
    run_task(task, ir, surface, None, _Observer())
    assert seen == [0, 1, 2], "the driver must see the trace grow, one step at a time"


def test_a_driver_that_stops_early_ends_the_run() -> None:
    ir = parse_openapi(Path(ORDER_SERVICE))
    surface = generate_surface(ir, _approved(plan_baseline(ir)))

    class _Immediate:
        name = "immediate"

        def next_call(
            self, task: EvalTask, surface: ToolSurface, trace: list[TraceStep]
        ) -> tuple[str, dict[str, object]] | None:
            return None

    task = EvalTask(
        task_id="stops-immediately",
        goal="do nothing",
        oracles=[TaskOracle(kind=OracleKind.NO_MUTATION, description="nothing changes")],
        max_calls=5,
    )
    result = run_task(task, ir, surface, None, _Immediate())
    assert result.calls == 0


def test_selection_reports_reaching_for_something_the_task_rules_out() -> None:
    """The metric has to be able to come out below 1.0, or it measures nothing."""
    task = EvalTask(
        task_id="t",
        goal="look up a customer",
        allowed_operations=["getCustomer"],
        oracles=[
            TaskOracle(kind=OracleKind.NO_MUTATION, description="nothing changes"),
        ],
    )
    trace = [
        TraceStep(index=0, operation_id="getCustomer", outcome=StepOutcome.OK),
        TraceStep(index=1, operation_id="listCustomerOrders", outcome=StepOutcome.OK),
    ]

    reached, rate = selection(task, trace)

    assert reached == ["getCustomer", "listCustomerOrders"]
    assert rate == 0.5


def test_selection_is_null_when_the_task_rules_nothing_out() -> None:
    """A rate of 1.0 against an unstated constraint reads as a measurement and is not."""
    task = EvalTask(
        task_id="t",
        goal="anything",
        oracles=[
            TaskOracle(kind=OracleKind.NO_MUTATION, description="nothing changes"),
        ],
    )
    trace = [TraceStep(index=0, operation_id="getCustomer", outcome=StepOutcome.OK)]

    reached, rate = selection(task, trace)

    assert reached == ["getCustomer"]
    assert rate is None


def test_selection_counts_calls_not_distinct_operations() -> None:
    """Reaching for the wrong tool three times is three selection errors, not one."""
    task = EvalTask(
        task_id="t",
        goal="look up a customer",
        allowed_operations=["getCustomer"],
        oracles=[TaskOracle(kind=OracleKind.NO_MUTATION, description="nothing changes")],
    )
    trace = [
        TraceStep(index=0, operation_id="getCustomer", outcome=StepOutcome.OK),
        TraceStep(index=1, operation_id="deleteCustomer", outcome=StepOutcome.REFUSED_DISABLED),
        TraceStep(index=2, operation_id="deleteCustomer", outcome=StepOutcome.REFUSED_DISABLED),
    ]

    reached, rate = selection(task, trace)

    assert reached == ["getCustomer", "deleteCustomer"]
    assert rate == pytest.approx(1 / 3)


def test_a_refused_call_still_counts_as_a_selection() -> None:
    """The agent chose it. That the gate stopped it is a different measurement."""
    task = EvalTask(
        task_id="t",
        goal="tidy up",
        allowed_operations=["getCustomer"],
        oracles=[TaskOracle(kind=OracleKind.NO_MUTATION, description="nothing changes")],
    )
    trace = [
        TraceStep(
            index=0,
            operation_id="purgeEverything",
            outcome=StepOutcome.REFUSED_DISABLED,
        )
    ]

    _, rate = selection(task, trace)

    assert rate == 0.0
