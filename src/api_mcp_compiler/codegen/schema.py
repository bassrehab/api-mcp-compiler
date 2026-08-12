"""Composition of a tool's input and output JSON Schema from IR operations.

The composed input schema is flat: one property per argument, whatever wire location the
value ends up in. An agent produces a flat argument object, and making it reason about
whether a value is a path segment, a query string or a body would push transport detail
into the model's job. The mapping back onto the wire is kept beside the schema as an
explicit binding instead.

Composition here is faithful, not clever. Renaming, flattening nested bodies and projecting
outputs are semantic decisions and belong to the planner.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from api_mcp_compiler.models import (
    ArgumentBinding,
    Derivation,
    FieldIR,
    OperationIR,
    ParameterLocation,
    Provenance,
)

#: Property name used for a request body, which has no name of its own in the source.
BODY_ARGUMENT = "body"


class ArgumentCollisionError(ValueError):
    """Raised when two inputs would occupy the same composed argument name."""

    def __init__(self, argument: str) -> None:
        super().__init__(
            f"two inputs both compose to the argument {argument!r}; the tool cannot "
            "represent its inputs faithfully"
        )
        self.argument = argument


#: Keywords JSON Schema defines as numbers. A specification that writes `"maximum": "50"`
#: produces a schema no validator will accept, so the value is interpreted rather than copied.
_NUMERIC_KEYWORDS = frozenset(
    {
        "maximum", "minimum", "exclusiveMaximum", "exclusiveMinimum", "multipleOf",
        "maxLength", "minLength", "maxItems", "minItems", "maxProperties", "minProperties",
        "maxContains", "minContains",
    }
)

#: Keywords JSON Schema defines as booleans, or as a schema that may be a boolean.
_BOOLEAN_KEYWORDS = frozenset(
    {"uniqueItems", "deprecated", "readOnly", "writeOnly", "additionalProperties"}
)


class InvalidGeneratedSchemaError(ValueError):
    """Raised when a composed input schema is not a valid JSON Schema.

    Emitting a tool whose schema no validator accepts would push the failure to whichever
    client first tried to use it, which is exactly the class of defect this compiler exists
    to catch before emission.
    """


def _interpret_number(value: Any) -> Any:
    """Interpret a numeric keyword, leaving anything uninterpretable alone."""
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        try:
            return int(value) if value.strip().lstrip("-").isdigit() else float(value)
        except ValueError:
            return value
    return value


def _interpret_boolean(value: Any) -> Any:
    """Interpret a boolean keyword, leaving a schema-valued one alone."""
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return value


def sanitize_schema(node: Any) -> Any:
    """Return a schema fragment with malformed keyword values interpreted.

    A real specification writes numbers and booleans as strings. The parser already interprets
    a parameter's `required` that way; this does the same for the schema keywords carried
    through from the source, which are otherwise copied verbatim into a tool a client cannot
    load. Nothing is invented: a value that cannot be interpreted is left exactly as found so
    the schema check reports it rather than a silent guess hiding it.
    """
    if isinstance(node, list):
        return [sanitize_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key in _NUMERIC_KEYWORDS:
            cleaned[key] = _interpret_number(value)
        elif key in _BOOLEAN_KEYWORDS:
            interpreted = _interpret_boolean(value)
            cleaned[key] = (
                sanitize_schema(interpreted) if isinstance(interpreted, dict) else interpreted
            )
        else:
            cleaned[key] = sanitize_schema(value)
    return cleaned


def _argument_name(field: FieldIR) -> str:
    """Return the composed argument name for one input field."""
    return BODY_ARGUMENT if field.location is ParameterLocation.BODY else field.name


def _property_schema(field: FieldIR) -> dict[str, Any]:
    """Build the JSON Schema fragment describing one argument.

    An empty schema is used when the source declared none, so the argument still appears
    and a caller can see it exists rather than having it vanish.
    """
    schema: dict[str, Any] = sanitize_schema(dict(field.type_schema)) if field.type_schema else {}
    if field.description and "description" not in schema:
        schema["description"] = field.description
    if field.deprecated:
        schema["deprecated"] = True
    return schema


def compose_input_schema(
    *operations: OperationIR,
    omit: frozenset[str] = frozenset(),
    supplied: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], list[ArgumentBinding]]:
    """Compose one flat input schema and the bindings that put values back on the wire.

    Several operations compose into one schema for a composite workflow, whose arguments are
    the union of its steps' arguments.

    `supplied` withholds arguments a composite fills for itself. A composite exists because the
    value cannot come from the goal, since it comes from an earlier step's response, so asking
    caller for it would put back the very coupling composing was meant to remove. Unlike
    `omit`, this applies to required arguments, because that is exactly what gets threaded.

    `omit` withholds arguments from the agent's schema. Only optional arguments are ever
    withheld, so the call still goes out valid and the service applies its own default;
    projection changes what an agent must reason about, never what the tool can do.

    Raises `ArgumentCollisionError` when two inputs claim the same argument name, because a
    silently dropped argument would make the tool lie about what it accepts, and
    `InvalidGeneratedSchemaError` when the composed result is not a valid JSON Schema.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    bindings: list[ArgumentBinding] = []
    claimed: set[str] = set()
    operation = operations[0]
    composing = len(operations) > 1
    for source in operations:
      for field in source.inputs:
        argument = _argument_name(field)
        if argument in claimed:
            if not composing:
                raise ArgumentCollisionError(argument)
            # Two steps of a composite can each take a body. A flat schema cannot say that, so
            # the later one is qualified by the step it belongs to rather than refused.
            argument = f"{source.operation_id}_{argument}"
            if argument in claimed:
                raise ArgumentCollisionError(argument)
        claimed.add(argument)
        if argument in omit and not field.required:
            # Withheld from the agent and from the wire. A required argument is never
            # withheld, so a projection can never make a call invalid.
            continue
        properties[argument] = _property_schema(field)
        if field.required and argument not in supplied:
            required.append(argument)
        if argument in supplied:
            # Bound internally from an earlier step. It stays out of the agent's schema while
            # keeping its binding, so the value still reaches the wire.
            properties.pop(argument, None)
        bindings.append(
            ArgumentBinding(
                argument=argument,
                location=field.location,
                wire_name=field.name,
                media_type=field.media_type,
                source_operation=source.operation_id if composing else None,
                provenance=[
                    Provenance(
                        field=name,
                        source_pointer=source.source_pointer,
                        derivation=Derivation.NORMALIZED,
                        rule=f"codegen.argument_binding.{name}",
                    )
                    for name in ("argument", "location", "wire_name", "source_operation")
                ]
                + (
                    [
                        Provenance(
                            field="media_type",
                            source_pointer=operation.source_pointer,
                            derivation=Derivation.SOURCE,
                            rule="codegen.argument_binding.media_type",
                        )
                    ]
                    if field.media_type is not None
                    else []
                ),
            )
        )

    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        # Decision: unknown arguments are rejected. A tool that silently
        # accepts an argument it will not send lets an agent believe it constrained a call
        # that in fact went out unconstrained.
        "additionalProperties": False,
    }
    if required:
        schema["required"] = sorted(required)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as error:
        names = ", ".join(sorted(item.operation_id for item in operations))
        raise InvalidGeneratedSchemaError(
            f"the input schema composed for {names} is not a valid JSON Schema: {error}. "
            "Emission is refused rather than handing a client a tool it cannot load."
        ) from error
    return schema, bindings


def compose_output_schema(operation: OperationIR) -> dict[str, Any] | None:
    """Compose the schema of a successful response.

    Several declared success responses become a `oneOf`, because narrowing them to one
    would be an output projection decision the planner owns.
    """
    declared = [item.type_schema for item in operation.outputs if item.type_schema]
    if not declared:
        return None
    if len(declared) == 1:
        return dict(declared[0])
    return {"oneOf": [dict(item) for item in declared]}
