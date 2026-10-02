"""The served harness: the generated server, reached over MCP, judged by the same oracles.

The comparison runs used to hand a model each tool's planned schema. These tests pin that the
served route measures the same thing when the agent is known to be right, so that a difference
under a model is attributable to what the server presents rather than to the harness.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from api_mcp_compiler.codegen.mcp_server import _docstring
from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.evaluation.harness import ReferenceDriver, run_task
from api_mcp_compiler.evaluation.served import ServedSurface, _typed, run_task_served
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.models import EvalCorpus, ReviewStatus, ToolPlan
from api_mcp_compiler.planning.baseline import plan_baseline
from api_mcp_compiler.planning.semantic import plan_semantic
from tests.conftest import ORDER_SERVICE

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "examples/evals/order_tasks.json"
#: Emit for the SDK installed, so the 1.x CI job measures the 1.x target rather than failing to
#: import the 2.x one.
SDK = int(importlib.metadata.version("mcp").split(".")[0])


def _approved(plan: ToolPlan) -> ToolPlan:
    return plan.model_copy(
        update={
            "artifacts": [
                item.model_copy(update={"review_status": ReviewStatus.APPROVED})
                for item in plan.artifacts
            ]
        }
    )


@pytest.fixture(autouse=True)
def _quiet() -> Any:
    # subhadipmitra@: httpx logs every upstream request at INFO, which buries a failure.
    logging.disable(logging.INFO)
    yield
    logging.disable(logging.NOTSET)


@pytest.mark.parametrize("arm", ["baseline", "semantic"])
def test_served_and_in_process_agree_under_the_reference_driver(arm: str) -> None:
    ir = parse_openapi(Path(ORDER_SERVICE))
    plan = plan_semantic(ir) if arm == "semantic" else plan_baseline(ir)
    surface = generate_surface(ir, _approved(plan))
    corpus = EvalCorpus.model_validate(json.loads(CORPUS.read_text(encoding="utf-8")))
    with ServedSurface(ir, surface, sdk=SDK) as served:
        for task in corpus.tasks:
            local = run_task(task, ir, surface, None, ReferenceDriver())
            remote = run_task_served(task, ir, surface, served, ReferenceDriver())
            assert remote.success == local.success, task.task_id
            assert [step.outcome for step in remote.trace] == [
                step.outcome for step in local.trace
            ], task.task_id
            assert remote.oracle_results == local.oracle_results, task.task_id


def test_the_semantic_surface_offers_its_resources_through_the_server() -> None:
    """A read the planner made a resource is reached by `resources/read`, not dropped."""
    ir = parse_openapi(Path(ORDER_SERVICE))
    surface = generate_surface(ir, _approved(plan_semantic(ir)))
    with ServedSurface(ir, surface, sdk=SDK) as served:
        assert "get_customer" not in served.tools()
        assert "get_customer" in served.templates()


@pytest.mark.parametrize(
    ("value", "schema", "expected"),
    [
        ("60", {"type": "integer"}, 60),
        ("0.5", {"type": "number"}, 0.5),
        ("true", {"type": "boolean"}, True),
        ("sixty", {"type": "integer"}, "sixty"),
        ("C-100", {"type": "string"}, "C-100"),
        ("C-100", None, "C-100"),
    ],
)
def test_url_values_arrive_as_their_declared_type(value: str, schema: Any, expected: Any) -> None:
    assert _typed(value, schema) == expected


@pytest.mark.parametrize(
    "text", ["plain", r"a \d+ pattern", 'ends in """', "\\", 'a "quoted" word', 'say "hi"']
)
def test_a_description_survives_being_written_into_a_docstring(text: str) -> None:
    namespace: dict[str, Any] = {}
    exec(compile(f'def f():\n    """{_docstring(text)}"""\n', "generated", "exec"), namespace)
    assert namespace["f"].__doc__ == text


WIDGETS = """
openapi: 3.0.3
info: {title: Widgets, version: "1"}
servers: [{url: "https://widgets.example.com"}]
paths:
  /widgets/{widget_id}:
    get:
      operationId: getWidget
      summary: Get one widget by its numeric id.
      parameters:
        - {name: widget_id, in: path, required: true, schema: {type: integer}}
      responses:
        "200":
          description: The widget.
          content:
            application/json:
              schema: {type: object, properties: {id: {type: integer}}}
"""


def test_a_resource_with_a_numeric_identifier_can_be_read(tmp_path: Path) -> None:
    """Every URI value is text; the server reads it as the type its schema declares.

    Until this was fixed, `api://widget/7` was validated as the string "7" against an integer
    schema, so no client could read any resource with a numeric identifier. A served comparison
    on TMDB found it; no bundled example had such a resource.
    """
    from api_mcp_compiler.evaluation.state import ServiceStore

    spec = tmp_path / "widgets.yaml"
    spec.write_text(WIDGETS, encoding="utf-8")
    ir = parse_openapi(spec)
    surface = generate_surface(ir, _approved(plan_semantic(ir)))
    with ServedSurface(ir, surface, sdk=SDK) as served:
        (name,) = [key for key, (uri, _) in served.templates().items() if "{" in uri]
        served.use(ServiceStore.from_fixture({"widgets": {"7": {"id": 7}}}))
        result = served.call(name, {"widget_id": 7})
    assert result.payload == {"status_code": 200, "body": {"id": 7}}
