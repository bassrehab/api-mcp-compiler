"""Tests over the third-party benchmark.

These skip when the benchmark has not been fetched, because the documents are deliberately
not stored in this repository. The skip is loud rather than silent: a run without the
benchmark reports it, so an absent benchmark is never mistaken for a passing one.

Fetch with `SSL_CERT_FILE=/etc/ssl/cert.pem python scripts/fetch_benchmark.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.evaluation.harness import run_corpus
from api_mcp_compiler.evaluation.restbench import (
    OracleSidecar,
    bind_solution,
    import_corpus,
    operation_index,
    query_digest,
)
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.models import ReviewStatus, ToolPlan
from api_mcp_compiler.planning.baseline import plan_baseline
from api_mcp_compiler.planning.semantic import plan_semantic

SPEC = Path("examples/benchmarks/restbench/spotify_oas.json")
TASKS = Path("examples/benchmarks/restbench/spotify_tasks.json")
SIDECAR = Path("examples/oracles/restbench_spotify.oracles.json")

needs_benchmark = pytest.mark.skipif(
    not (SPEC.is_file() and TASKS.is_file()),
    reason="benchmark not fetched; run scripts/fetch_benchmark.py",
)


def _sidecar() -> OracleSidecar:
    return OracleSidecar.model_validate(json.loads(SIDECAR.read_text(encoding="utf-8")))


def _approved(plan: ToolPlan) -> ToolPlan:
    return plan.model_copy(
        update={
            "artifacts": [
                item.model_copy(update={"review_status": ReviewStatus.APPROVED})
                for item in plan.artifacts
            ]
        }
    )


def test_the_sidecar_is_committed_and_records_its_selection_rule() -> None:
    """The oracles are ours, so how they were chosen has to be inspectable."""
    sidecar = _sidecar()
    assert len(sidecar.entries) == 24
    note = sidecar.authoring_note
    assert "every third write-bearing task" in note
    assert "without consulting the generated tool surface" in note


def test_every_authored_entry_carries_an_oracle() -> None:
    for entry in _sidecar().entries:
        assert entry.oracles, entry.task_id


@needs_benchmark
def test_every_solution_step_binds_to_an_operation() -> None:
    """One corpus has to describe both planners, which is why tasks name operations."""
    ir = parse_openapi(SPEC)
    index = operation_index(ir)
    upstream = json.loads(TASKS.read_text(encoding="utf-8"))
    unbound = [step for item in upstream for step in bind_solution(item["solution"], index)[1]]
    assert not unbound


@needs_benchmark
def test_authored_oracles_are_pinned_to_the_goal_they_were_written_against() -> None:
    """An upstream reordering must not silently attach an oracle to a different goal."""
    upstream = json.loads(TASKS.read_text(encoding="utf-8"))
    for entry in _sidecar().entries:
        assert entry.query_digest == query_digest(upstream[entry.index]["query"])


@needs_benchmark
def test_the_corpus_imports() -> None:
    ir = parse_openapi(SPEC)
    corpus, outstanding = import_corpus(TASKS, ir, _sidecar())
    assert len(corpus.tasks) == 24
    assert len(outstanding) == 31, "tasks without authored oracles must be counted, not dropped"
    assert corpus.source_digest == ir.service.source_digest


@needs_benchmark
@pytest.mark.parametrize("planner", [plan_baseline, plan_semantic])
def test_the_authored_corpus_is_satisfiable_on_both_surfaces(planner: object) -> None:
    """An oracle nobody can satisfy measures the harness, not the surface."""
    ir = parse_openapi(SPEC)
    corpus, _ = import_corpus(TASKS, ir, _sidecar())
    plan = _approved(planner(ir))  # type: ignore[operator]
    run = run_corpus(corpus, ir, generate_surface(ir, plan))
    failed = [item.task_id for item in run.results if not item.success]
    assert run.success_rate == 1.0, f"unsatisfiable: {failed}"
    assert sum(item.invalid_argument_calls for item in run.results) == 0


@needs_benchmark
def test_the_reference_driver_still_cannot_distinguish_the_surfaces() -> None:
    """At real scale, as at fixture scale. This is why no comparison exists yet."""
    ir = parse_openapi(SPEC)
    corpus, _ = import_corpus(TASKS, ir, _sidecar())
    baseline = run_corpus(corpus, ir, generate_surface(ir, _approved(plan_baseline(ir))))
    semantic = run_corpus(corpus, ir, generate_surface(ir, _approved(plan_semantic(ir))))
    assert [item.success for item in baseline.results] == [
        item.success for item in semantic.results
    ]


@needs_benchmark
def test_optional_parameters_are_not_required_on_the_real_api() -> None:
    """Coercing a string boolean once marked all 92 inputs required; the truth is 31."""
    ir = parse_openapi(SPEC)
    required = sum(1 for item in ir.operations for field in item.inputs if field.required)
    total = sum(len(item.inputs) for item in ir.operations)
    assert required < total // 2
    search = next(item for item in ir.operations if item.route == "/search")
    assert sorted(item.name for item in search.inputs if item.required) == ["q", "type"]


@needs_benchmark
@pytest.mark.parametrize("planner", [plan_baseline, plan_semantic])
def test_every_generated_schema_is_a_schema_a_client_could_load(planner: object) -> None:
    """The API rejected 22 of 40 before schema keywords were interpreted rather than copied."""
    from jsonschema import Draft202012Validator

    from api_mcp_compiler.evaluation.model_driver import tool_definitions

    ir = parse_openapi(SPEC)
    definitions, _ = tool_definitions(generate_surface(ir, _approved(planner(ir))))  # type: ignore[operator]
    for definition in definitions:
        Draft202012Validator.check_schema(definition["input_schema"])


@needs_benchmark
@pytest.mark.parametrize("planner", [plan_baseline, plan_semantic])
def test_only_executable_tools_are_offered_to_a_driver(planner: object) -> None:
    """A tool the approval gate holds is not one an agent could call in production."""
    from api_mcp_compiler.evaluation.model_driver import tool_definitions

    ir = parse_openapi(SPEC)
    surface = generate_surface(ir, _approved(planner(ir)))  # type: ignore[operator]
    definitions, mapping = tool_definitions(surface)
    executable = {item.name for item in surface.tools if item.emission.value == "executable"}
    assert {item["name"] for item in definitions} == executable
    assert set(mapping) == executable


@needs_benchmark
def test_the_tool_block_is_marked_for_caching() -> None:
    """It is identical for every task in an arm and is most of the prompt."""
    from api_mcp_compiler.evaluation.model_driver import tool_definitions

    ir = parse_openapi(SPEC)
    definitions, _ = tool_definitions(generate_surface(ir, _approved(plan_baseline(ir))))
    assert definitions[-1]["cache_control"] == {"type": "ephemeral"}
    assert not any("cache_control" in item for item in definitions[:-1])


def _entries() -> list[object]:
    return _sidecar().entries


def test_no_oracle_asserts_an_identifier_the_service_assigns() -> None:
    """The error that cost two registered runs, now checked instead of remembered.

    A record identifier may be asserted only when the agent could know it: because the fixture
    seeded it, or because it is a singleton keyed by its own path. An identifier that only the
    reference arguments supplied asserts the annotator's route, and marks an agent wrong for
    creating the very thing the goal asked for.
    """
    for entry in _entries():
        knowable = {
            record_id
            for collection in entry.fixture.values()
            for record_id in collection
        } | set(entry.fixture)
        for oracle in entry.oracles:
            for assertion in oracle.assertions:
                if assertion.record_id is None:
                    continue
                knowable_here = knowable | {assertion.collection}
                assert assertion.record_id in knowable_here, (
                    f"{entry.task_id}: asserts record {assertion.record_id!r}, which the agent "
                    "has no way to know"
                )


def test_no_oracle_pins_the_operation_a_goal_must_be_reached_by() -> None:
    """A goal asks for an outcome; the route is the agent's to choose."""
    for entry in _entries():
        for oracle in entry.oracles:
            for assertion in oracle.retrieval_assertions:
                assert assertion.operation_id is None, (
                    f"{entry.task_id}: requires the answer to come from "
                    f"{assertion.operation_id!r}, which asserts a route"
                )


def test_no_oracle_counts_records_in_a_collection_an_agent_writes_into() -> None:
    """A count there counts calls, not things.

    Adding three tracks in one call and in three calls are both correct, and only one of them
    satisfies a count. Existence is the honest assertion.
    """
    written = {"playlists.tracks", "users.playlists", "me.player.queue"}
    for entry in _entries():
        for oracle in entry.oracles:
            for assertion in oracle.assertions:
                if assertion.collection in written:
                    assert assertion.count is None, (
                        f"{entry.task_id}: counts records in {assertion.collection!r}, which "
                        "counts calls rather than things"
                    )


def _composite_surface(
    steps: tuple[str, ...] = ("get_a_list_of_current_users_playlists", "add_tracks_to_playlist"),
):
    """Approve a composite on the benchmark API.

    The benchmark is used rather than a fixture here because a composite needs a policy it can
    resolve, and the fixtures in this repository declare no authentication, so scopes cannot be
    derived and the gate holds every write regardless of composition.
    """
    from api_mcp_compiler.models import CompositeEntry, ToolOverlay
    from api_mcp_compiler.planning.semantic import propose_lookup_then_use
    from api_mcp_compiler.policy.synthesis import synthesize_policy

    ir = parse_openapi(SPEC)
    del propose_lookup_then_use  # a reviewer may approve any composite, not only a proposed one
    composite_id = "_then_".join(steps)
    overlay = ToolOverlay(
        service_id=ir.service.service_id,
        source_digest=ir.service.source_digest,
        composites=[
            CompositeEntry(
                composite_id=composite_id,
                name="composed_tool",
                description="Two steps a goal cannot bridge on its own, in one call.",
                steps=list(steps),
                review_status=ReviewStatus.APPROVED,
            )
        ],
    )
    plan = _approved(plan_semantic(ir, overlay))
    manifest = synthesize_policy(ir, plan)
    return ir, generate_surface(ir, plan, manifest), manifest


@needs_benchmark
def test_an_approved_composite_clears_the_gate() -> None:
    _, surface, _ = _composite_surface()
    composite = next(item for item in surface.tools if len(item.source_operations) > 1)
    assert composite.emission.value == "executable", composite.blockers


@needs_benchmark
def test_a_threaded_argument_is_not_asked_of_the_caller() -> None:
    """Asking for it would put back the coupling composing was meant to remove."""
    _, surface, _ = _composite_surface()
    composite = next(item for item in surface.tools if len(item.source_operations) > 1)
    assert "playlist_id" not in composite.input_schema.get("properties", {})
    assert any(item.argument == "playlist_id" for item in composite.argument_bindings)


@needs_benchmark
def test_two_steps_may_each_carry_a_body() -> None:
    """A flat schema cannot say that, so the later one is qualified rather than refused."""
    _, surface, _ = _composite_surface(("create_playlist", "add_tracks_to_playlist"))
    composite = next(item for item in surface.tools if len(item.source_operations) > 1)
    bodies = [
        item for item in composite.argument_bindings if item.argument.endswith("body")
    ]
    assert len(bodies) == 2
    assert len({item.source_operation for item in bodies}) == 2


@needs_benchmark
def test_a_composite_executes_as_one_call() -> None:
    """Charging its steps separately would bill the surface for the coupling it removed."""
    import ast

    from api_mcp_compiler.codegen.mcp_server import emit_server
    from api_mcp_compiler.evaluation.harness import run_task
    from api_mcp_compiler.models import EvalTask, OracleKind, StateAssertion, TaskOracle

    ir, surface, manifest = _composite_surface()
    composite = next(item for item in surface.tools if len(item.source_operations) > 1)

    class _Once:
        name = "once"

        def next_call(self, task: object, surface: object, trace: list[object]):
            if trace:
                return None
            return composite.name, {"uris": "tr-1"}

    task = EvalTask(
        task_id="composite",
        goal="add a track to my playlist",
        fixture={"me.playlists": {"pl-rock": {"id": "pl-rock", "name": "My Rock"}}},
        oracles=[
            TaskOracle(
                kind=OracleKind.FINAL_STATE,
                description="the track reached the new playlist",
                assertions=[StateAssertion(collection="playlists.tracks", exists=True)],
            )
        ],
        max_calls=4,
    )
    result = run_task(task, ir, surface, manifest, _Once())
    assert result.success, [item.detail for item in result.oracle_results if not item.passed]
    assert result.calls == 1, "a composite is one call"

    emitted = emit_server(ir, surface, manifest)
    ast.parse(emitted.source)
    assert composite.name in emitted.registered
    assert "threading={'playlist_id': (1, 0, 'id')}" in emitted.source


TMDB_SPEC = Path("examples/benchmarks/restbench/tmdb_oas.json")
TMDB_TASKS = Path("examples/benchmarks/restbench/tmdb_tasks.json")
TMDB_SIDECAR = Path("examples/oracles/restbench_tmdb.oracles.json")

needs_tmdb = pytest.mark.skipif(
    not (TMDB_SPEC.is_file() and TMDB_TASKS.is_file()),
    reason="TMDB benchmark not fetched; run scripts/fetch_benchmark.py",
)


def _tmdb_sidecar() -> OracleSidecar:
    return OracleSidecar.model_validate(json.loads(TMDB_SIDECAR.read_text(encoding="utf-8")))


def test_the_tmdb_sidecar_records_that_solutions_were_never_read() -> None:
    """The claim that matters most about this corpus, kept where a reader will find it."""
    note = _tmdb_sidecar().authoring_note
    assert "never read" in note
    assert "every third goal by upstream index" in note
    assert len(_tmdb_sidecar().entries) == 34


def test_no_tmdb_oracle_asserts_a_route() -> None:
    """Every task is a retrieval, so pinning an operation would be the Spotify error again."""
    for entry in _tmdb_sidecar().entries:
        for oracle in entry.oracles:
            for assertion in oracle.retrieval_assertions:
                assert assertion.operation_id is None, entry.task_id
                assert assertion.field is None, entry.task_id


def test_every_tmdb_answer_is_present_in_its_own_fixture() -> None:
    """An answer nothing seeded is an oracle no agent could satisfy."""
    for entry in _tmdb_sidecar().entries:
        blob = json.dumps(entry.fixture)
        for oracle in entry.oracles:
            for assertion in oracle.retrieval_assertions:
                if assertion.equals is None:
                    continue
                needle = json.dumps(assertion.equals).strip('"')
                assert needle in blob, f"{entry.task_id}: {assertion.equals!r} is unreachable"


@needs_tmdb
def test_tmdb_goals_are_read_without_their_solutions() -> None:
    """The discipline is the loader's, not a reviewer's memory."""
    from api_mcp_compiler.evaluation.restbench import read_goals_only

    goals = read_goals_only(TMDB_TASKS)
    assert len(goals) == 100
    assert all(isinstance(item, str) for item in goals)


@needs_tmdb
def test_every_tmdb_entry_is_pinned_to_the_goal_it_was_written_against() -> None:
    from api_mcp_compiler.evaluation.restbench import query_digest, read_goals_only

    goals = read_goals_only(TMDB_TASKS)
    for entry in _tmdb_sidecar().entries:
        assert entry.query_digest == query_digest(goals[entry.index]), entry.task_id


@needs_tmdb
def test_the_composite_rule_fires_on_the_held_out_api() -> None:
    """If it proposed nothing here, this benchmark could not judge composition either."""
    from api_mcp_compiler.planning.semantic import propose_lookup_then_use

    ir = parse_openapi(TMDB_SPEC)
    assert len(propose_lookup_then_use(ir.operations)) > 10


@needs_tmdb
def test_tmdb_fixture_identifiers_satisfy_the_declared_types() -> None:
    """A fixture that disagrees with the specification makes its chains unsatisfiable.

    The first TMDB run scored one task in thirty-four. The agent was calling the right
    operations in the right order; it simply could not pass on the identifier the search
    returned, because the fixture used readable string keys and the specification declares its
    identifiers as integers. Every chain was impossible by construction.
    """
    from api_mcp_compiler.evaluation.state import derive_effect

    ir = parse_openapi(TMDB_SPEC)
    integer_keyed: dict[str, str] = {}
    for operation in ir.operations:
        effect = derive_effect(operation)
        if not effect.identifier_argument:
            continue
        field = next(
            (
                item
                for item in operation.inputs
                if item.name == effect.identifier_argument and item.type_schema
            ),
            None,
        )
        if field and field.type_schema.get("type") == "integer":
            integer_keyed[effect.collection] = effect.identifier_argument

    assert integer_keyed, "the benchmark must declare integer identifiers for this to bite"
    for entry in _tmdb_sidecar().entries:
        for collection, records in entry.fixture.items():
            if collection not in integer_keyed:
                continue
            for key in records:
                assert key.lstrip("-").isdigit(), (
                    f"{entry.task_id}: {collection} is keyed by {key!r}, but the specification "
                    f"declares {integer_keyed[collection]!r} an integer, so no agent could "
                    "pass it on"
                )
