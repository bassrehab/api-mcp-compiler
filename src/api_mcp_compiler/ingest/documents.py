"""Strict loading of YAML and JSON specification documents.

Both `yaml.safe_load` and `json.loads` accept duplicate mapping keys and silently keep the
last one. A specification declaring two `get` operations under one path therefore loses one
without any trace, which defeats the completeness sweep outright: the dropped key never
exists to be swept.

A duplicate key means the document states two different things for the same construct, and
nothing in the file says which the author meant. Choosing one silently is the failure this
compiler exists to prevent, and recording an ambiguity while still choosing would ship the
wrong operation anyway. Loading therefore refuses the document and names the key and line so
the author can fix it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from api_mcp_compiler.provenance import source_digest

JSON_SUFFIXES = {".json"}


class DocumentLoadError(ValueError):
    """Raised when a document cannot be loaded unambiguously."""


class DuplicateKeyError(DocumentLoadError):
    """Raised when a mapping declares the same key twice."""


class _StrictLoader(yaml.SafeLoader):
    """A safe loader that refuses duplicate mapping keys instead of keeping the last."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:  # unhashable keys cannot collide in a Python mapping
                continue
            if duplicate:
                raise DuplicateKeyError(
                    f"duplicate key {key!r} at line {key_node.start_mark.line + 1}, column "
                    f"{key_node.start_mark.column + 1}: the document states two values for it "
                    "and nothing says which was intended"
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Object hook rejecting duplicate keys in JSON."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(
                f"duplicate key {key!r}: the document states two values for it and nothing "
                "says which was intended"
            )
        result[key] = value
    return result


def parse_text(text: str, *, is_json: bool) -> Any:
    """Parse document text, refusing duplicate keys in either format."""
    if is_json:
        return json.loads(text, object_pairs_hook=_reject_duplicate_pairs)
    try:
        return yaml.load(text, Loader=_StrictLoader)
    except yaml.YAMLError as error:
        if isinstance(error, DuplicateKeyError):
            raise
        raise DocumentLoadError(f"not well-formed YAML: {error}") from error


def load_document(path: Path) -> tuple[Any, str]:
    """Read a document and return its parsed value and the digest of its raw bytes."""
    raw = path.read_bytes()
    try:
        data = parse_text(raw.decode("utf-8"), is_json=path.suffix.lower() in JSON_SUFFIXES)
    except DocumentLoadError as error:
        raise type(error)(f"{path}: {error}") from error
    except json.JSONDecodeError as error:
        raise DocumentLoadError(f"{path}: not well-formed JSON: {error}") from error
    return data, source_digest(raw)
