"""A model-backed driver, so that a surface is judged by an agent rather than by a replay.

The reference driver replays a recorded solution and therefore scores every surface
identically. This driver is the opposite: it sees the goal and the tools and nothing else, so
what it manages to do is a property of the surface it was given.

Fairness is the whole design. Both arms get the same model, the same decoding settings, the
same system prompt, the same budget and the same starting state; the only difference permitted
is the tool list itself. Anything that reads the task's reference solution, its oracles or its
fixture would make the comparison meaningless, so this driver is given none of them.

The API key is read from the environment and never written anywhere. Arguments and responses
reach the transcript, so a benchmark fixture is the only thing that should ever be behind it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from api_mcp_compiler.models import EvalTask, StepOutcome, ToolSurface, TraceStep

#: Identical in both arms. It describes the job, never the shape of the surface, because a
#: prompt that hinted at grouped or composite tools would be an advantage handed to one arm.
SYSTEM_PROMPT = """You are operating a service through the tools you have been given.

Work towards the user's goal by calling tools. Look at what each call returns before deciding \
the next one: an identifier you need will usually come from an earlier result rather than from \
the goal text.

Call one tool at a time. When the goal is achieved, or when you cannot make further progress, \
stop calling tools and say briefly what you did. Do not ask the user questions; they are not \
watching and cannot answer."""

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_EFFORT = "medium"


class ModelDriverError(RuntimeError):
    """Raised when the driver cannot be constructed or a turn cannot be completed."""


@dataclass
class _Conversation:
    """Everything carried across the turns of one task."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_tool_use_id: str | None = None
    unexecuted_tool_use_ids: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stopped: bool = False
    refusal: str | None = None


def tool_definitions(surface: ToolSurface) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Render an executable surface as tool definitions, with a map back to operations.

    Only executable tools are offered. A tool held by the approval gate is not a tool an agent
    could call in production, so offering it here would measure a surface nobody would deploy.
    """
    definitions: list[dict[str, Any]] = []
    to_operation: dict[str, str] = {}
    for tool in surface.tools:
        if tool.emission.value != "executable":
            continue
        definitions.append(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
        )
        # A composite is addressed by its tool name, because the harness resolves it to
        # several operations and runs them as one call. A single-operation tool is addressed
        # by the operation itself.
        to_operation[tool.name] = (
            tool.source_operations[0]
            if len(tool.source_operations) == 1
            else tool.name
        )
    if not definitions:
        raise ModelDriverError("the surface exposes no executable tools")
    # Cache the tool block: it is identical for every task in an arm, and it is by far the
    # largest part of the prompt.
    definitions[-1] = {**definitions[-1], "cache_control": {"type": "ephemeral"}}
    return definitions, to_operation


def _result_content(step: TraceStep) -> tuple[str, bool]:
    """Render what happened on a step as the tool result the model sees.

    A refusal is reported honestly rather than disguised as an error, because how an agent
    reacts to the emission gate is part of what is being measured.
    """
    if step.outcome is StepOutcome.OK:
        return json.dumps(step.response, ensure_ascii=False) if step.response is not None else (
            "The call succeeded and returned no content."
        ), False
    detail = step.detail or step.outcome.value
    return f"The call did not succeed ({step.outcome.value}): {detail}", True


@dataclass
class ModelDriver:
    """Drives a surface with a Claude model.

    One instance per arm per run. Conversation state is keyed by task, and a task whose trace
    is empty starts a fresh conversation, so a re-run never inherits an earlier one.
    """

    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    effort: str = DEFAULT_EFFORT
    name: str = "model"
    _client: Any = field(default=None, repr=False)
    _conversations: dict[str, _Conversation] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        try:
            import anthropic
        except ImportError as error:  # pragma: no cover - exercised only without the extra
            raise ModelDriverError(
                "the anthropic SDK is not installed; install the 'eval' extra to run a "
                "model-backed evaluation"
            ) from error
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            raise ModelDriverError(
                "no Anthropic credentials in the environment; the driver never reads a key "
                "from a file and none is stored in this repository"
            )
        self._client = anthropic.Anthropic()
        self.name = f"model:{self.model}:{self.effort}"

    def usage(self, task_id: str) -> dict[str, int]:
        """Return the token usage recorded for one task."""
        state = self._conversations.get(task_id)
        if state is None:
            return {}
        return {
            "input_tokens": state.input_tokens,
            "output_tokens": state.output_tokens,
            "cache_read_tokens": state.cache_read_tokens,
            "cache_write_tokens": state.cache_write_tokens,
        }

    def next_call(
        self, task: EvalTask, surface: ToolSurface, trace: list[TraceStep]
    ) -> tuple[str, dict[str, Any]] | None:
        """Ask the model what to do next, given everything it has seen."""
        definitions, to_operation = tool_definitions(surface)

        if not trace:
            state = _Conversation(
                messages=[{"role": "user", "content": task.goal}],
            )
            self._conversations[task.task_id] = state
        else:
            state = self._conversations[task.task_id]
            if state.stopped:
                return None
            text, is_error = _result_content(trace[-1])
            if state.pending_tool_use_id is None:  # pragma: no cover - defensive
                return None
            # Every tool_use in the previous turn needs a result, including any the harness
            # did not run. The API rejects a turn that leaves one unanswered, and silently
            # dropping the extras ended the first attempt at this run with a 400.
            results: list[dict[str, Any]] = [
                {
                    "type": "tool_result",
                    "tool_use_id": state.pending_tool_use_id,
                    "content": text,
                    "is_error": is_error,
                }
            ]
            results += [
                {
                    "type": "tool_result",
                    "tool_use_id": identifier,
                    "content": "Not executed. Call one tool at a time and wait for its result.",
                    "is_error": True,
                }
                for identifier in state.unexecuted_tool_use_ids
            ]
            state.unexecuted_tool_use_ids = []
            state.messages.append({"role": "user", "content": results})

        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_config={"effort": self.effort},
            tools=definitions,
            # The harness executes one operation per step, and the system prompt asks for one
            # call at a time. Saying so in the request as well keeps the two consistent.
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            messages=state.messages,
        )

        usage = response.usage
        state.input_tokens += usage.input_tokens or 0
        state.output_tokens += usage.output_tokens or 0
        state.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        state.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

        if response.stop_reason == "refusal":
            # Recorded rather than retried. A refusal is a fact about the run, and silently
            # re-rolling it would let a surface look better than it was.
            state.stopped = True
            state.refusal = getattr(response, "stop_details", None) and getattr(
                response.stop_details, "category", None
            )
            return None

        state.messages.append({"role": "assistant", "content": response.content})

        calls = [item for item in response.content if item.type == "tool_use"]
        if not calls:
            state.stopped = True
            return None

        call = calls[0]
        state.pending_tool_use_id = call.id
        state.unexecuted_tool_use_ids = [item.id for item in calls[1:]]
        operation_id = to_operation.get(call.name)
        if operation_id is None:
            # The model invented a tool name. Reported as an unmapped step rather than hidden,
            # since inventing tools is exactly the failure a surface can cause.
            return call.name, dict(call.input)
        return operation_id, dict(call.input)
