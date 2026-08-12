"""Reference resolution tests.

Resolution follows a pointer the document author controls, so the safety-relevant cases
matter as much as the happy path: what the resolver refuses, and what it does instead of
recursing forever.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.ingest.refs import RefPolicy, RefResolutionError
from api_mcp_compiler.models import DocumentRole


def _write(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _root(directory: Path, body: str) -> Path:
    return _write(directory, "root.yaml", body)


def test_local_reference_resolves_without_any_allowlist(tmp_path: Path) -> None:
    spec = _root(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Local, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema: {$ref: '#/components/schemas/Thing'}
        components:
          schemas:
            Thing: {type: object, properties: {id: {type: string}}}
        """,
    )
    schema = parse_openapi(spec).operations[0].outputs[0].type_schema
    assert schema == {"type": "object", "properties": {"id": {"type": "string"}}}


def test_external_file_reference_resolves_when_the_directory_is_allowed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "common.yaml",
        """
        schemas:
          Error: {type: object, properties: {message: {type: string}}}
        """,
    )
    spec = _root(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: External, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema: {$ref: 'common.yaml#/schemas/Error'}
        """,
    )
    ir = parse_openapi(spec, policy=RefPolicy(allowed_directories=(tmp_path,)))
    assert ir.operations[0].outputs[0].type_schema == {
        "type": "object",
        "properties": {"message": {"type": "string"}},
    }
    roles = {item.role for item in ir.service.source_documents}
    assert roles == {DocumentRole.ROOT, DocumentRole.REFERENCED}


def test_external_reference_is_refused_by_default(tmp_path: Path) -> None:
    """The default policy denies everything outside the root document."""
    _write(tmp_path, "common.yaml", "schemas: {Error: {type: object}}\n")
    spec = _root(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Denied, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema: {$ref: 'common.yaml#/schemas/Error'}
        """,
    )
    ir = parse_openapi(spec)
    refusals = [item for item in ir.ambiguities if item.code == "ref_not_allowlisted"]
    assert len(refusals) == 1
    assert refusals[0].blocking is True
    assert [item.role for item in ir.service.source_documents] == [DocumentRole.ROOT]


def test_traversal_out_of_an_allowed_directory_is_refused(tmp_path: Path) -> None:
    """`../` must not escape the allowlist, or the allowlist would be decorative."""
    _write(tmp_path, "secret.yaml", "schemas: {Leak: {type: object}}\n")
    inner = tmp_path / "inner"
    spec = _root(
        inner,
        """
        openapi: 3.0.3
        info: {title: Traversal, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema: {$ref: '../secret.yaml#/schemas/Leak'}
        """,
    )
    ir = parse_openapi(spec, policy=RefPolicy(allowed_directories=(inner,)))
    assert [item.code for item in ir.ambiguities if item.code.startswith("ref_")] == [
        "ref_not_allowlisted"
    ]


def test_remote_reference_is_refused_and_never_fetched(tmp_path: Path) -> None:
    """Ingestion must never reach the network on behalf of a document author."""
    spec = _root(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Remote, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema: {$ref: 'https://example.invalid/schemas.yaml#/Thing'}
        """,
    )
    ir = parse_openapi(spec, policy=RefPolicy(allowed_directories=(tmp_path,)))
    refusals = [item for item in ir.ambiguities if item.code == "remote_ref_refused"]
    assert len(refusals) == 1
    assert refusals[0].blocking is True


def test_missing_target_is_reported_rather_than_raised(tmp_path: Path) -> None:
    spec = _root(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Missing, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema: {$ref: '#/components/schemas/Absent'}
        """,
    )
    ir = parse_openapi(spec)
    assert [item.code for item in ir.ambiguities if item.code.startswith("ref_")] == [
        "ref_target_missing"
    ]


def test_recursive_schema_is_left_finite_rather_than_rejected(tmp_path: Path) -> None:
    """A self-referencing schema is legitimate; erroring would reject valid specifications."""
    spec = _root(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Recursive, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema: {$ref: '#/components/schemas/Node'}
        components:
          schemas:
            Node:
              type: object
              properties:
                child: {$ref: '#/components/schemas/Node'}
        """,
    )
    ir = parse_openapi(spec)
    schema = ir.operations[0].outputs[0].type_schema
    assert schema is not None
    assert schema["type"] == "object"
    assert schema["properties"]["child"] == {"$ref": "#/components/schemas/Node"}
    recursive = [item for item in ir.ambiguities if item.code == "recursive_reference"]
    assert len(recursive) == 1
    assert recursive[0].blocking is False


def test_chain_deeper_than_the_limit_raises(tmp_path: Path) -> None:
    """A non-cyclic chain past the limit is pathological, not legitimate."""
    spec = _root(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Deep, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema: {$ref: '#/components/schemas/A'}
        components:
          schemas:
            A: {$ref: '#/components/schemas/B'}
            B: {$ref: '#/components/schemas/C'}
            C: {$ref: '#/components/schemas/D'}
            D: {type: string}
        """,
    )
    with pytest.raises(RefResolutionError, match="exceeded the maximum depth"):
        parse_openapi(spec, policy=RefPolicy(max_depth=2))


def test_sibling_keywords_override_the_referenced_value(tmp_path: Path) -> None:
    """OpenAPI 3.1 permits keywords alongside `$ref` and applies them over the target."""
    spec = _root(
        tmp_path,
        """
        openapi: 3.1.0
        info: {title: Siblings, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema:
                        $ref: '#/components/schemas/Thing'
                        description: overridden
        components:
          schemas:
            Thing: {type: object, description: original}
        """,
    )
    schema = parse_openapi(spec).operations[0].outputs[0].type_schema
    assert schema == {"type": "object", "description": "overridden"}


def test_every_loaded_document_is_digested(tmp_path: Path) -> None:
    """Reproducibility must not weaken the moment references span files."""
    _write(tmp_path, "common.yaml", "schemas: {Error: {type: object}}\n")
    spec = _root(
        tmp_path,
        """
        openapi: 3.0.3
        info: {title: Digests, version: '1'}
        paths:
          /x:
            get:
              operationId: getX
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema: {$ref: 'common.yaml#/schemas/Error'}
        """,
    )
    ir = parse_openapi(spec, policy=RefPolicy(allowed_directories=(tmp_path,)))
    assert len(ir.service.source_documents) == 2
    assert all(item.digest.startswith("sha256:") for item in ir.service.source_documents)
    assert ir.service.source_documents[0].role is DocumentRole.ROOT
