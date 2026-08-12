"""Run a pre-registered comparison and record it.

Everything this script does is fixed by the registration it is given: which corpus, which
arms, which model, how many runs per task, and what counts as a success. It has no options
that could change any of them, because a runner with a knob is a runner that can be turned
until the answer is agreeable.

Runs are written out with the registration's digest on them, so a result cannot later be
attached to a hypothesis it was not produced under.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from api_mcp_compiler.codegen.tools import generate_surface  # noqa: E402
from api_mcp_compiler.contracts import canonical_json  # noqa: E402
from api_mcp_compiler.evaluation.harness import run_task  # noqa: E402
from api_mcp_compiler.evaluation.model_driver import ModelDriver  # noqa: E402
from api_mcp_compiler.evaluation.preregistration import compare, digest_of, load  # noqa: E402
from api_mcp_compiler.evaluation.restbench import OracleSidecar, import_corpus  # noqa: E402
from api_mcp_compiler.ingest.openapi import parse_openapi  # noqa: E402
from api_mcp_compiler.models import (  # noqa: E402
    EvaluationRun,
    PlannerKind,
    ReviewStatus,
    TaskResult,
    ToolPlan,
)
from api_mcp_compiler.planning.baseline import plan_baseline  # noqa: E402
from api_mcp_compiler.planning.overlay import load_overlay  # noqa: E402
from api_mcp_compiler.planning.semantic import plan_semantic  # noqa: E402

#: Which files a registration's corpus refers to. The registration names the corpus; the
#: mapping lives here so a runner cannot be pointed at different data than the one registered.
#: `overlay` carries the human decisions the semantic arm is entitled to — composites a
#: reviewer approved. The baseline never receives one, because one tool per operation is what
#: it is.
CORPORA: dict[str, dict[str, Path | None]] = {
    "restbench-spotify": {
        "spec": REPO_ROOT / "examples/benchmarks/restbench/spotify_oas.json",
        "tasks": REPO_ROOT / "examples/benchmarks/restbench/spotify_tasks.json",
        "sidecar": REPO_ROOT / "examples/oracles/restbench_spotify.oracles.json",
        "overlay": None,
    },
    "restbench-tmdb": {
        "spec": REPO_ROOT / "examples/benchmarks/restbench/tmdb_oas.json",
        "tasks": REPO_ROOT / "examples/benchmarks/restbench/tmdb_tasks.json",
        "sidecar": REPO_ROOT / "examples/oracles/restbench_tmdb.oracles.json",
        "overlay": REPO_ROOT / "examples/overlays/tmdb.overlay.json",
    },
}
PLANNER_KINDS = {"baseline": PlannerKind.BASELINE, "semantic": PlannerKind.SEMANTIC}


def _approved(plan: ToolPlan) -> ToolPlan:
    """Approve every artifact in both arms, so the gate cannot advantage either."""
    return plan.model_copy(
        update={
            "artifacts": [
                item.model_copy(update={"review_status": ReviewStatus.APPROVED})
                for item in plan.artifacts
            ]
        }
    )


def _combine(results: list[TaskResult]) -> TaskResult:
    """Collapse the runs of one task under the registered success definition.

    Success requires every run to pass. The other metrics are summed, because an unsafe action
    in one run out of three is still an unsafe action the surface permitted.
    """
    # Report against a run that failed where there is one, so the merged record explains
    # itself. Copying the first run's oracles could show every oracle passing on a task
    # recorded as failed.
    representative = next((item for item in results if not item.success), results[0])
    return representative.model_copy(
        update={
            "success": all(item.success for item in results),
            "run_successes": [item.success for item in results],
            "calls": sum(item.calls for item in results),
            "unnecessary_calls": sum(item.unnecessary_calls for item in results),
            "invalid_argument_calls": sum(item.invalid_argument_calls for item in results),
            "unsafe_actions": sum(item.unsafe_actions for item in results),
            "confirmation_failures": sum(item.confirmation_failures for item in results),
            "context_bytes": sum(item.context_bytes for item in results),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registration", type=Path)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "artifacts/private/runs")
    arguments = parser.parse_args()

    registration = load(arguments.registration)
    digest = digest_of(registration)
    arguments.out.mkdir(parents=True, exist_ok=True)

    corpus_files = CORPORA.get(registration.corpus_id)
    if corpus_files is None:
        print(f"REFUSED: no corpus registered under {registration.corpus_id!r}.", file=sys.stderr)
        return 1
    spec_path, tasks_path = corpus_files["spec"], corpus_files["tasks"]
    sidecar_path, overlay_path = corpus_files["sidecar"], corpus_files["overlay"]
    assert spec_path is not None and tasks_path is not None and sidecar_path is not None
    ir = parse_openapi(spec_path)
    if ir.service.source_digest != registration.corpus_source_digest:
        print("REFUSED: the specification does not match the one registered.", file=sys.stderr)
        return 1
    sidecar = OracleSidecar.model_validate(
        json.loads(sidecar_path.read_text(encoding="utf-8"))
    )
    corpus, _ = import_corpus(tasks_path, ir, sidecar, corpus_id=registration.corpus_id)
    overlay = load_overlay(overlay_path) if overlay_path is not None else None

    print(f"registration {registration.registration_id}  [{digest}]")
    print(f"{len(corpus.tasks)} tasks x {len(registration.arms)} arms x "
          f"{registration.runs_per_task} runs = "
          f"{len(corpus.tasks) * len(registration.arms) * registration.runs_per_task} episodes")
    print(f"model {registration.model}\n", flush=True)

    runs: dict[str, EvaluationRun] = {}
    usage_total: dict[str, int] = {}
    started = time.monotonic()

    for arm in registration.arms:
        kind = PLANNER_KINDS[arm]
        # The overlay carries composites a reviewer approved, which are a semantic-planning
        # product. One tool per operation is what the baseline is, so it never receives one.
        planned = plan_semantic(ir, overlay) if arm == "semantic" else plan_baseline(ir)
        surface = generate_surface(ir, _approved(planned))
        combined: list[TaskResult] = []
        for position, task in enumerate(corpus.tasks, start=1):
            attempts: list[TaskResult] = []
            for attempt in range(registration.runs_per_task):
                driver = ModelDriver(model=registration.model)
                try:
                    attempts.append(run_task(task, ir, surface, None, driver))
                except Exception as error:
                    print(f"  ! {arm}/{task.task_id} run {attempt}: {error}", flush=True)
                    attempts.append(
                        TaskResult(task_id=task.task_id, success=False, calls=0)
                    )
                for key, value in driver.usage(task.task_id).items():
                    usage_total[key] = usage_total.get(key, 0) + value
            merged = _combine(attempts)
            combined.append(merged)
            mark = "pass" if merged.success else "FAIL"
            print(f"  [{arm:8}] {position:2}/{len(corpus.tasks)} {task.task_id:38} {mark} "
                  f"({sum(1 for item in attempts if item.success)}/{len(attempts)} runs)",
                  flush=True)
        runs[arm] = EvaluationRun(
            corpus_id=corpus.corpus_id,
            service_id=ir.service.service_id,
            source_digest=ir.service.source_digest,
            planner=kind,
            driver=f"model:{registration.model}",
            preregistration_digest=digest,
            results=combined,
        )
        path = arguments.out / f"{registration.registration_id}.{arm}.json"
        path.write_text(canonical_json(runs[arm].model_dump(mode="json")), encoding="utf-8")
        print(f"  -> {path}\n", flush=True)

    result = compare(runs["baseline"], runs["semantic"], registration)
    elapsed = time.monotonic() - started
    cost = (
        usage_total.get("input_tokens", 0) * 2
        + usage_total.get("cache_read_tokens", 0) * 0.2
        + usage_total.get("cache_write_tokens", 0) * 2.5
        + usage_total.get("output_tokens", 0) * 10
    ) / 1e6

    summary: dict[str, Any] = {
        "registration_id": registration.registration_id,
        "preregistration_digest": digest,
        "baseline_success_rate": runs["baseline"].success_rate,
        "semantic_success_rate": runs["semantic"].success_rate,
        "discordant_favouring_baseline": result.discordant_favouring_first,
        "discordant_favouring_semantic": result.discordant_favouring_second,
        "p_value": result.p_value,
        "significant": result.significant,
        "verdict": result.verdict,
        "usage": usage_total,
        "approximate_cost_usd": round(cost, 2),
        "elapsed_seconds": round(elapsed),
    }
    (arguments.out / f"{registration.registration_id}.summary.json").write_text(
        canonical_json(summary), encoding="utf-8"
    )
    print("=" * 72)
    for key, value in summary.items():
        if key != "usage":
            print(f"{key:32} {value}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
