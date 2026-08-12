"""Binding a result to a hypothesis fixed beforehand, and analysing it as registered.

A pre-registration only constrains anything if a result cannot be detached from it. The
document is digested and an evaluation run records that digest, so changing any registered
decision, including which model each arm uses, changes the digest and shows up as a mismatch.

The analysis is implemented here rather than described, so the test applied to a result is the
one that was registered and not one chosen once the numbers were visible.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import comb
from pathlib import Path

from api_mcp_compiler.contracts import canonical_json
from api_mcp_compiler.models import EvaluationRun, PreRegistration


class PreRegistrationMismatchError(ValueError):
    """Raised when a run was not produced under the registration it is analysed against."""


def digest_of(registration: PreRegistration) -> str:
    """Digest a registration exactly as it is stored."""
    payload = canonical_json(registration.model_dump(mode="json"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load(path: Path) -> PreRegistration:
    """Read a pre-registration."""
    return PreRegistration.model_validate(json.loads(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class Comparison:
    """The registered analysis applied to a pair of runs."""

    discordant_favouring_first: int
    discordant_favouring_second: int
    p_value: float
    significant: bool
    verdict: str


def _two_sided_exact(favouring: int, against: int) -> float:
    """Exact binomial probability over discordant pairs, two-sided."""
    total = favouring + against
    if total == 0:
        return 1.0
    extreme = min(favouring, against)
    tail = sum(comb(total, k) for k in range(extreme + 1))
    return float(min(1.0, 2 * tail / (2**total)))


def compare(
    first: EvaluationRun, second: EvaluationRun, registration: PreRegistration
) -> Comparison:
    """Apply the registered test to two runs.

    Raises `PreRegistrationMismatchError` when either run was produced under a different
    registration, which is the whole point of recording the digest on the run.
    """
    expected = digest_of(registration)
    for run in (first, second):
        if run.preregistration_digest != expected:
            raise PreRegistrationMismatchError(
                f"the {run.planner.value} run records {run.preregistration_digest}, but this "
                f"registration is {expected}; a result may not be analysed against a "
                "hypothesis it was not produced under"
            )

    by_task = {item.task_id: item.success for item in second.results}
    favouring_first = sum(
        1
        for item in first.results
        if item.task_id in by_task and item.success and not by_task[item.task_id]
    )
    favouring_second = sum(
        1
        for item in first.results
        if item.task_id in by_task and not item.success and by_task[item.task_id]
    )
    discordant = favouring_first + favouring_second
    p_value = _two_sided_exact(favouring_first, favouring_second)
    # The registration fixes 6 discordant pairs as the minimum that can reach significance
    # for this corpus size. Below it the result is inconclusive whatever the raw difference.
    significant = discordant >= 6 and p_value <= 0.05
    if not significant:
        verdict = (
            f"inconclusive: {discordant} discordant pair(s), fewer than the 6 this corpus "
            "size requires"
            if discordant < 6
            else f"inconclusive: p = {p_value:.4f}"
        )
    elif favouring_second > favouring_first:
        verdict = f"{second.planner.value} favoured, p = {p_value:.4f}"
    else:
        verdict = f"{first.planner.value} favoured, p = {p_value:.4f}"
    return Comparison(favouring_first, favouring_second, p_value, significant, verdict)
