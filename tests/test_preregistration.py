"""Pre-registration tests.

A pre-registration constrains nothing unless a result cannot be detached from it, so most of
these check the binding and the pre-committed thresholds rather than the prose.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api_mcp_compiler.evaluation.preregistration import (
    PreRegistrationMismatchError,
    compare,
    digest_of,
    load,
)
from api_mcp_compiler.models import EvaluationRun, PlannerKind, PreRegistration, TaskResult

REGISTRATION = Path("preregistrations/spotify-baseline-vs-semantic-001.json")


def _registration() -> PreRegistration:
    return load(REGISTRATION)


def _run(planner: PlannerKind, successes: list[bool], digest: str | None) -> EvaluationRun:
    return EvaluationRun(
        corpus_id="restbench-spotify",
        service_id="spotify-web-api",
        source_digest=_registration().corpus_source_digest,
        planner=planner,
        driver="test",
        preregistration_digest=digest,
        results=[
            TaskResult(task_id=f"t{index}", success=value)
            for index, value in enumerate(successes)
        ],
    )


def test_the_registration_is_committed_and_complete() -> None:
    registration = _registration()
    assert registration.arms == ["baseline", "semantic"]
    assert registration.runs_per_task >= 1
    assert registration.model
    assert registration.falsification
    assert registration.prohibited_after_seeing_results


def test_it_pre_commits_to_an_inconclusive_outcome() -> None:
    """A registration that cannot come out inconclusive is not constraining anything."""
    assert "inconclusive" in _registration().inconclusive_condition.lower()


def test_the_digest_changes_when_any_decision_changes() -> None:
    """Swapping the model between arms has to show, which is why the run records the digest."""
    registration = _registration()
    swapped = registration.model_copy(update={"model": "some-other-model"})
    assert digest_of(swapped) != digest_of(registration)


def test_the_committed_document_matches_its_own_digest() -> None:
    stored = json.loads(REGISTRATION.read_text(encoding="utf-8"))
    assert PreRegistration.model_validate(stored) == _registration()


def test_a_run_from_another_registration_cannot_be_analysed() -> None:
    registration = _registration()
    good = digest_of(registration)
    with pytest.raises(PreRegistrationMismatchError, match="not produced under"):
        compare(
            _run(PlannerKind.BASELINE, [True], good),
            _run(PlannerKind.SEMANTIC, [True], "sha256:" + "0" * 64),
            registration,
        )


def test_fewer_than_six_discordant_pairs_is_inconclusive() -> None:
    """The threshold was computed before the run, not chosen to fit one."""
    registration = _registration()
    digest = digest_of(registration)
    baseline = _run(PlannerKind.BASELINE, [False] * 5 + [True] * 19, digest)
    semantic = _run(PlannerKind.SEMANTIC, [True] * 24, digest)
    result = compare(baseline, semantic, registration)
    assert result.discordant_favouring_second == 5
    assert result.significant is False
    assert "inconclusive" in result.verdict


def test_six_discordant_pairs_in_one_direction_is_significant() -> None:
    registration = _registration()
    digest = digest_of(registration)
    baseline = _run(PlannerKind.BASELINE, [False] * 6 + [True] * 18, digest)
    semantic = _run(PlannerKind.SEMANTIC, [True] * 24, digest)
    result = compare(baseline, semantic, registration)
    assert result.discordant_favouring_second == 6
    assert result.significant is True
    assert result.p_value == pytest.approx(0.03125, abs=1e-6)
    assert "semantic favoured" in result.verdict


def test_a_split_result_is_not_significant() -> None:
    """Discordance in both directions cancels, which a raw rate difference would hide."""
    registration = _registration()
    digest = digest_of(registration)
    baseline = _run(PlannerKind.BASELINE, [False] * 4 + [True] * 20, digest)
    semantic = _run(PlannerKind.SEMANTIC, [True] * 4 + [False] * 4 + [True] * 16, digest)
    result = compare(baseline, semantic, registration)
    assert result.discordant_favouring_first > 0
    assert result.discordant_favouring_second > 0
    assert result.significant is False


def test_identical_runs_are_inconclusive_not_a_tie_in_favour_of_either() -> None:
    registration = _registration()
    digest = digest_of(registration)
    same = [True] * 20 + [False] * 4
    result = compare(
        _run(PlannerKind.BASELINE, same, digest),
        _run(PlannerKind.SEMANTIC, same, digest),
        registration,
    )
    assert result.p_value == 1.0
    assert result.significant is False
