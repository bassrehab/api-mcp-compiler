"""Tests for bringing remote references onto disk without ingestion ever reaching the network.

Nothing here makes a request. The downloader is replaced, because the behaviour worth testing
is what is refused, what is pinned and what happens when the bytes change, not whether urllib
works.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from api_mcp_compiler.contracts import dump_canonical
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.ingest.vendoring import (
    VendoringError,
    cached_documents,
    digest_of,
    load_lock,
    local_name,
    remote_references,
    strip_fragment,
    vendor,
)

REMOTE = "https://schemas.example.invalid/common.yaml"

SPEC = """openapi: 3.0.3
info: {title: Split Service, version: 1.0.0}
servers: [{url: https://split.example.invalid}]
paths:
  /widgets:
    get:
      operationId: listWidgets
      summary: List widgets
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                $ref: 'https://schemas.example.invalid/common.yaml#/Widget'
"""

COMMON = b"Widget:\n  type: object\n  properties:\n    id: {type: string}\n"


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """Replace the downloader with a dictionary, and record what it was asked for."""
    payloads = {REMOTE: COMMON}

    def _download(url: str) -> bytes:
        if url not in payloads:
            raise RuntimeError(f"unexpected fetch of {url}")
        return payloads[url]

    monkeypatch.setattr("api_mcp_compiler.benchmarks.download", _download)
    return payloads


def _spec(tmp_path: Path) -> Path:
    path = tmp_path / "service.yaml"
    path.write_text(SPEC, encoding="utf-8")
    return path


def _document(tmp_path: Path) -> tuple[Any, str]:
    from api_mcp_compiler.ingest.documents import load_document

    return load_document(_spec(tmp_path))


def test_a_reference_names_the_document_not_the_pointer_into_it() -> None:
    """Two references into one remote document are one document, fetched once."""
    assert strip_fragment("https://h.invalid/a.yaml#/Widget") == "https://h.invalid/a.yaml"
    assert remote_references(
        {"a": {"$ref": "https://h.invalid/a.yaml#/One"}, "b": {"$ref": "https://h.invalid/a.yaml#/Two"}}
    ) == ["https://h.invalid/a.yaml"]


def test_two_hosts_serving_the_same_path_are_two_files() -> None:
    """A cache that let one overwrite the other would resolve to bytes from elsewhere."""
    first = local_name("https://one.invalid/common.yaml")
    second = local_name("https://two.invalid/common.yaml")

    assert first != second
    assert first.endswith(".yaml")


def test_an_unrecorded_reference_is_refused_before_anything_is_fetched(
    tmp_path: Path, served: dict[str, bytes]
) -> None:
    """Trusting a source is a decision someone makes, not something a build does for them."""
    document, digest = _document(tmp_path)

    with pytest.raises(VendoringError, match="not in the lock"):
        vendor(document, digest, tmp_path / "refs.lock.json", tmp_path / "refs", record=False)

    assert not (tmp_path / "refs").exists(), "something was written despite the refusal"


def test_recording_pins_the_bytes_that_were_served(
    tmp_path: Path, served: dict[str, bytes]
) -> None:
    document, digest = _document(tmp_path)

    lock, fetched, unchanged = vendor(
        document, digest, tmp_path / "refs.lock.json", tmp_path / "refs", record=True
    )

    assert fetched == [REMOTE] and unchanged == []
    entry = lock.resolve(REMOTE)
    assert entry is not None
    assert entry.sha256 == digest_of(COMMON)
    assert (tmp_path / "refs" / Path(entry.path).name).read_bytes() == COMMON


def test_a_second_run_asks_the_network_for_nothing(
    tmp_path: Path, served: dict[str, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case on every run after the first."""
    document, digest = _document(tmp_path)
    lock, _, _ = vendor(
        document, digest, tmp_path / "refs.lock.json", tmp_path / "refs", record=True
    )

    def _refuse(url: str) -> bytes:
        raise AssertionError(f"fetched {url} when the cache already held it")

    monkeypatch.setattr("api_mcp_compiler.benchmarks.download", _refuse)
    again, fetched, unchanged = vendor(
        document, digest, tmp_path / "refs.lock.json", tmp_path / "refs",
        record=False, existing=lock,
    )

    assert fetched == [] and unchanged == [REMOTE]
    assert again.references == lock.references


def test_upstream_changing_underneath_a_lock_is_refused(
    tmp_path: Path, served: dict[str, bytes]
) -> None:
    """The whole point of pinning: a changed document is visible, not silently adopted."""
    document, digest = _document(tmp_path)
    lock, _, _ = vendor(
        document, digest, tmp_path / "refs.lock.json", tmp_path / "refs", record=True
    )
    served[REMOTE] = b"Widget:\n  type: string\n"
    for stale in (tmp_path / "refs").iterdir():
        stale.unlink()

    with pytest.raises(VendoringError, match="now serves"):
        vendor(
            document, digest, tmp_path / "refs.lock.json", tmp_path / "refs",
            record=False, existing=lock,
        )


def test_a_locked_reference_resolves_at_compile_time(
    tmp_path: Path, served: dict[str, bytes]
) -> None:
    """The payoff: a document with a remote reference compiles, with no network."""
    spec = _spec(tmp_path)
    document, digest = _document(tmp_path)
    lock_path = tmp_path / "refs.lock.json"
    lock, _, _ = vendor(document, digest, lock_path, tmp_path / "refs", record=True)
    lock_path.write_text(dump_canonical(lock), encoding="utf-8")

    ir = parse_openapi(spec, vendored=cached_documents(load_lock(lock_path), lock_path))

    assert not [item for item in ir.ambiguities if item.code == "remote_ref_refused"]
    listed = ir.operations[0]
    assert listed.outputs[0].type_schema == {
        "type": "object",
        "properties": {"id": {"type": "string"}},
    }


def test_without_a_lock_a_remote_reference_is_still_refused(tmp_path: Path) -> None:
    """The default has not moved: a plain compile cannot reach the network at all."""
    ir = parse_openapi(_spec(tmp_path))

    refusals = [item for item in ir.ambiguities if item.code == "remote_ref_refused"]
    assert refusals and refusals[0].blocking
    assert "vendor-refs" in refusals[0].detail


def test_bytes_edited_after_locking_are_refused_at_compile_time(
    tmp_path: Path, served: dict[str, bytes]
) -> None:
    """A cache is not a trusted store; it is a place bytes were put, checked on every read."""
    spec = _spec(tmp_path)
    document, digest = _document(tmp_path)
    lock_path = tmp_path / "refs.lock.json"
    lock, _, _ = vendor(document, digest, lock_path, tmp_path / "refs", record=True)
    lock_path.write_text(dump_canonical(lock), encoding="utf-8")
    for cached in (tmp_path / "refs").iterdir():
        cached.write_bytes(b"Widget: {type: string}\n")

    ir = parse_openapi(spec, vendored=cached_documents(load_lock(lock_path), lock_path))

    unusable = [item for item in ir.ambiguities if item.code == "vendored_ref_unusable"]
    assert unusable and unusable[0].blocking
    assert "Nothing was loaded" in unusable[0].detail


def test_the_lock_records_the_specification_it_was_collected_from(
    tmp_path: Path, served: dict[str, bytes]
) -> None:
    """A lock that did not name its specification could be paired with the wrong one."""
    document, digest = _document(tmp_path)

    lock, _, _ = vendor(
        document, digest, tmp_path / "refs.lock.json", tmp_path / "refs", record=True
    )

    assert lock.source_digest == digest
    assert json.loads(dump_canonical(lock))["schema_version"] == "0.1.0"
