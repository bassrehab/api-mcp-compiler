"""Running a corpus against a surface.

Tasks name source operations, never tool names. That is what lets one corpus score two
different planners: a task naming `createRefund` describes something both the baseline and the
semantic surface expose, under different names and possibly different shapes. The harness
resolves the operation to whichever tool a given surface offers, and records an unmapped
operation as a fact about that surface rather than a failure of the task.

The only driver shipped here selects correctly by construction. It exists to prove the
machinery works end to end, and it produces no signal whatsoever about the quality of a
surface: every surface scores identically under it. A driver that could distinguish surfaces
requires a model, which is deliberately absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from api_mcp_compiler.codegen.composite import composite_threading
from api_mcp_compiler.contracts import canonical_json
from api_mcp_compiler.evaluation.oracles import evaluate_oracle
from api_mcp_compiler.evaluation.state import EffectKind, ServiceStore, derive_effect
from api_mcp_compiler.models import (
    ApiSemanticIR,
    EmissionStatus,
    EvalCorpus,
    EvalTask,
    EvaluationRun,
    OracleResult,
    PolicyManifest,
    SideEffectClass,
    StepOutcome,
    TaskResult,
    ToolDescriptor,
    ToolSurface,
    TraceStep,
)
from api_mcp_compiler.runtime.governance import (
    ConfirmationError,
    check_confirmation,
    issue_confirmation,
)

REFERENCE_DRIVER = "deterministic-reference"


class CorpusMismatchError(ValueError):
    """Raised when a corpus was written against different specification bytes."""


def bind_operations(surface: ToolSurface) -> dict[str, ToolDescriptor]:
    """Map each source operation to the tool a surface exposes for it.

    A direct tool is preferred over a composite that merely includes the operation, because a
    composite performs additional steps a task did not ask for.
    """
    binding: dict[str, ToolDescriptor] = {}
    for tool in sorted(surface.tools, key=lambda item: len(item.source_operations)):
        for operation_id in tool.source_operations:
            binding.setdefault(operation_id, tool)
    return binding


class Driver(Protocol):
    """Chooses which operations to invoke for a task.

    A driver sees the goal and the tools available, never the oracles. Implementations that
    read the reference solution are self-tests for the harness, not agents.
    """

    name: str

    def next_call(
        self, task: EvalTask, surface: ToolSurface, trace: list[TraceStep]
    ) -> tuple[str, dict[str, Any]] | None:
        """Choose the next operation, given everything observed so far, or stop.

        The driver is asked once per step and sees the trace, so it can read what the previous
        call returned before deciding. An earlier version asked for the whole plan up front,
        which no agent can supply: a goal like "add the first track of an artist's newest album
        to a playlist" cannot name the track identifier until a lookup has returned it.
        """
        ...


@dataclass
class ReferenceDriver:
    """Replays the solution a task author recorded.

    Correct by construction, and therefore useless for comparing surfaces: it scores every
    surface identically. Its purpose is to show that the store, the oracles and the metrics
    agree on a run that is known to be right, so that a later disagreement is attributable.
    """

    name: str = REFERENCE_DRIVER

    def next_call(
        self, task: EvalTask, surface: ToolSurface, trace: list[TraceStep]
    ) -> tuple[str, dict[str, Any]] | None:
        del surface
        index = len(trace)
        if index >= len(task.reference_solution):
            return None
        step = task.reference_solution[index]
        return step.operation_id, dict(step.arguments)


@dataclass
class _RunContext:
    """State carried through one task run."""

    store: ServiceStore
    trace: list[TraceStep] = field(default_factory=list)
    confirmations: dict[str, Any] = field(default_factory=dict)
    invalid_arguments: int = 0
    unsafe: int = 0
    confirmation_failures: int = 0
    context_bytes: int = 0
    unmapped: list[str] = field(default_factory=list)


def _mutating_operations(ir: ApiSemanticIR) -> set[str]:
    return {
        item.operation_id
        for item in ir.operations
        if item.side_effect is not SideEffectClass.READ
    }


def run_task(
    task: EvalTask,
    ir: ApiSemanticIR,
    surface: ToolSurface,
    manifest: PolicyManifest | None,
    driver: Driver,
) -> TaskResult:
    """Run one task against one surface and judge it by its oracles."""
    binding = bind_operations(surface)
    operations = {item.operation_id: item for item in ir.operations}
    store = ServiceStore.from_fixture(task.fixture)
    initial_state = store.snapshot()
    context = _RunContext(store=store)
    prohibited = set(task.prohibited_operations)

    by_name = {item.name: item for item in surface.tools}
    for index in range(task.max_calls):
        choice = driver.next_call(task, surface, list(context.trace))
        if choice is None:
            break
        operation_id, arguments = choice
        composite = by_name.get(operation_id)
        if composite is not None and len(composite.source_operations) > 1:
            # A composite is one call. Counting its steps separately would charge the surface
            # for the very coupling composing removed.
            _attempt_composite(
                index, composite, operations, manifest, context, prohibited, arguments
            )
            continue
        tool = binding.get(operation_id)
        if tool is None or operation_id not in operations:
            context.unmapped.append(operation_id)
            context.trace.append(
                TraceStep(
                    index=index,
                    operation_id=operation_id,
                    arguments=arguments,
                    outcome=StepOutcome.UNMAPPED,
                    detail="the surface exposes no tool for this operation",
                )
            )
            continue
        _attempt(
            index,
            operation_id,
            arguments,
            tool,
            operations[operation_id],
            manifest,
            context,
            prohibited,
        )

    final_state = context.store.snapshot()
    mutating = _mutating_operations(ir)
    results: list[OracleResult] = [
        evaluate_oracle(
            oracle, initial_state, final_state, context.trace, task.prohibited_operations, mutating
        )
        for oracle in task.oracles
    ]
    expected_calls = len(task.reference_solution)
    successful = sum(1 for item in context.trace if item.outcome is StepOutcome.OK)
    return TaskResult(
        task_id=task.task_id,
        success=all(item.passed for item in results),
        oracle_results=results,
        calls=len(context.trace),
        unnecessary_calls=max(0, successful - expected_calls) if expected_calls else 0,
        unmapped_operations=sorted(set(context.unmapped)),
        invalid_argument_calls=context.invalid_arguments,
        unsafe_actions=context.unsafe,
        confirmation_failures=context.confirmation_failures,
        context_bytes=context.context_bytes,
        latency_ms=None,
        token_cost=None,
        trace=context.trace,
    )


def _attempt_composite(
    index: int,
    tool: ToolDescriptor,
    operations: dict[str, Any],
    manifest: PolicyManifest | None,
    context: _RunContext,
    prohibited: set[str],
    arguments: dict[str, Any],
) -> None:
    """Run a composite's steps in order, threading what the goal could not supply.

    The trace records one step, because that is what the agent did. Recording each underlying
    request would make a composite look more expensive than the tools it replaced, which is
    backwards.
    """
    steps = [operations[item] for item in tool.source_operations if item in operations]
    if len(steps) != len(tool.source_operations):
        context.unmapped.append(tool.name)
        context.trace.append(
            TraceStep(
                index=index,
                operation_id=tool.name,
                tool=tool.name,
                arguments=arguments,
                outcome=StepOutcome.UNMAPPED,
                detail="a step of this composite has no operation in the IR",
            )
        )
        return

    # Confirmation belongs to the composite, once, against the arguments the agent supplied.
    # Confirming each step separately compares a digest of the composite's arguments with a
    # digest of one step's, which never matches and refuses a correctly confirmed call.
    policy = manifest.policy_for(tool.tool_id) if manifest else None
    if policy is not None and policy.confirmation is not None:
        token = context.confirmations.get(tool.name) or issue_confirmation(
            tool.name, arguments, policy.confirmation
        )
        context.confirmations[tool.name] = token
        try:
            check_confirmation(tool.name, arguments, policy.confirmation, token)
        except ConfirmationError as error:
            context.confirmation_failures += 1
            context.trace.append(
                TraceStep(
                    index=index,
                    operation_id=tool.source_operations[-1],
                    tool=tool.name,
                    arguments=arguments,
                    outcome=StepOutcome.REFUSED_CONFIRMATION,
                    detail=str(error),
                )
            )
            return

    confirmed = policy is not None and policy.confirmation is not None
    threading = composite_threading(steps)
    carried = dict(arguments)
    last_response: dict[str, Any] | None = None
    before = len(context.trace)

    for position, operation in enumerate(steps):
        for threaded in threading.values():
            if threaded.step_index != position:
                continue
            source = context.trace[before + threaded.from_step] if context.trace[before:] else None
            payload = source.response if source is not None else None
            value = _threaded_value(payload, threaded.response_field)
            if value is None:
                context.trace.append(
                    TraceStep(
                        index=index,
                        operation_id=operation.operation_id,
                        tool=tool.name,
                        arguments=carried,
                        outcome=StepOutcome.REFUSED_ARGUMENTS,
                        detail=(
                            f"step {position} needs {threaded.argument!r}, and step "
                            f"{threaded.from_step} returned no {threaded.response_field!r} to "
                            "take it from"
                        ),
                    )
                )
                _collapse(context, before, index, tool, arguments)
                return
            carried[threaded.argument] = value
        _attempt(
            index,
            operation.operation_id,
            _step_arguments(tool, operation.operation_id, carried),
            tool,
            operation,
            None,
            context,
            prohibited,
            validate=False,
        )
        produced = context.trace[-1]
        if produced.outcome is not StepOutcome.OK:
            _collapse(context, before, index, tool, arguments)
            return
        last_response = produced.response

    _collapse(context, before, index, tool, arguments, response=last_response, confirmed=confirmed)


def _step_arguments(
    tool: ToolDescriptor, operation_id: str, carried: dict[str, Any]
) -> dict[str, Any]:
    """Select the arguments belonging to one step, restoring any qualified name.

    Two steps can each take a body, so the composite's schema qualifies the later one. The
    request still expects the plain name, and the binding says which step it belongs to.
    """
    selected: dict[str, Any] = {}
    for binding in tool.argument_bindings:
        if binding.source_operation not in (None, operation_id):
            continue
        if binding.argument in carried:
            plain = binding.argument
            prefix = f"{operation_id}_"
            if plain.startswith(prefix):
                plain = plain[len(prefix) :]
            selected[plain] = carried[binding.argument]
    return selected


def _threaded_value(payload: Any, field: str) -> Any:
    """Read a threaded value out of a response, whether it listed or returned one record."""
    if isinstance(payload, dict):
        if field in payload:
            return payload[field]
        items = payload.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0].get(field)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0].get(field)
    return None


def _collapse(
    context: _RunContext,
    before: int,
    index: int,
    tool: ToolDescriptor,
    arguments: dict[str, Any],
    response: dict[str, Any] | None = None,
    confirmed: bool = False,
) -> None:
    """Replace a composite's internal steps with the single call the agent actually made."""
    internal = context.trace[before:]
    del context.trace[before:]
    failed = next((item for item in internal if item.outcome is not StepOutcome.OK), None)
    context.trace.append(
        TraceStep(
            index=index,
            operation_id=tool.source_operations[-1],
            tool=tool.name,
            arguments=arguments,
            outcome=failed.outcome if failed is not None else StepOutcome.OK,
            detail=failed.detail if failed is not None else None,
            confirmed=confirmed,
            response=response,
            response_bytes=len(canonical_json(response).encode("utf-8")) if response else 0,
        )
    )


def _attempt(
    index: int,
    operation_id: str,
    arguments: dict[str, Any],
    tool: ToolDescriptor,
    operation: Any,
    manifest: PolicyManifest | None,
    context: _RunContext,
    prohibited: set[str],
    validate: bool = True,
) -> None:
    """Attempt one call, recording what happened and mutating state only if it succeeded.

    A composite's steps skip schema validation: the schema describes the composite's arguments,
    not each step's, and a threaded value is absent from it by design.
    """
    policy = manifest.policy_for(tool.tool_id) if manifest else None
    confirmed = False

    if tool.emission is not EmissionStatus.EXECUTABLE:
        # A refusal is the emission gate working, whether or not the task prohibited the
        # operation, so it is recorded as a refusal and never counted as an unsafe action.
        context.trace.append(
            TraceStep(
                index=index,
                operation_id=operation_id,
                tool=tool.name,
                arguments=arguments,
                outcome=StepOutcome.REFUSED_DISABLED,
                detail="refused: " + ", ".join(item.value for item in tool.blockers),
            )
        )
        return

    errors = (
        sorted(
            Draft202012Validator(tool.input_schema).iter_errors(arguments),
            key=lambda error: list(error.absolute_path),
        )
        if validate
        else []
    )
    if errors:
        # Argument validity is one of the required metrics, and a call the surface would
        # have rejected must not be allowed to mutate the store behind the metric's back.
        context.invalid_arguments += 1
        context.trace.append(
            TraceStep(
                index=index,
                operation_id=operation_id,
                tool=tool.name,
                arguments=arguments,
                outcome=StepOutcome.REFUSED_ARGUMENTS,
                detail="; ".join(
                    f"/{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
                    for error in errors
                ),
            )
        )
        return

    if policy is not None and policy.confirmation is not None:
        token = context.confirmations.get(tool.name) or issue_confirmation(
            tool.name, arguments, policy.confirmation
        )
        context.confirmations[tool.name] = token
        try:
            check_confirmation(tool.name, arguments, policy.confirmation, token)
            confirmed = True
        except ConfirmationError as error:
            context.confirmation_failures += 1
            context.trace.append(
                TraceStep(
                    index=index,
                    operation_id=operation_id,
                    tool=tool.name,
                    arguments=arguments,
                    outcome=StepOutcome.REFUSED_CONFIRMATION,
                    detail=str(error),
                )
            )
            return

    effect = derive_effect(operation)
    record = context.store.apply(effect, arguments, seed=operation_id)
    body_bytes = len(canonical_json(record).encode("utf-8")) if record is not None else 0
    context.context_bytes += body_bytes
    if operation_id in prohibited and effect.kind is not EffectKind.READ:
        context.unsafe += 1
    context.trace.append(
        TraceStep(
            index=index,
            operation_id=operation_id,
            tool=tool.name,
            arguments=arguments,
            outcome=StepOutcome.OK,
            confirmed=confirmed,
            response=record,
            response_bytes=body_bytes,
        )
    )


def run_corpus(
    corpus: EvalCorpus,
    ir: ApiSemanticIR,
    surface: ToolSurface,
    manifest: PolicyManifest | None = None,
    driver: Driver | None = None,
) -> EvaluationRun:
    """Run a whole corpus against one surface.

    Raises `CorpusMismatchError` when the corpus was written against different bytes, because
    tasks describing another revision of a service cannot judge this one.
    """
    if corpus.source_digest != ir.service.source_digest:
        raise CorpusMismatchError(
            f"corpus was written against {corpus.source_digest} but the specification is "
            f"{ir.service.source_digest}; re-author or re-stamp before running it"
        )
    chosen = driver or ReferenceDriver()
    return EvaluationRun(
        corpus_id=corpus.corpus_id,
        service_id=corpus.service_id,
        source_digest=corpus.source_digest,
        planner=surface.planner,
        driver=chosen.name,
        results=[run_task(task, ir, surface, manifest, chosen) for task in corpus.tasks],
    )
