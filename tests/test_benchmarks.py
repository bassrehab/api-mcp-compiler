"""Benchmark fetch and verification tests.

The digest check is the whole mechanism: it is what allows a third-party document to stay out
of this repository while a result built from it remains reconstructible. Most of these tests
drive it into refusal, because a verification that has only ever passed proves nothing.

Nothing here reaches the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api_mcp_compiler.benchmarks import (
    BenchmarkFetchError,
    DigestMismatchError,
    digest_of,
    fetch_source,
    load_manifest,
    resolve_target,
)
from api_mcp_compiler.models import BenchmarkManifest, BenchmarkSource

MANIFEST = Path("examples/benchmarks/manifest.json")


def _source(**overrides: object) -> BenchmarkSource:
    base = {
        "source_id": "example",
        "description": "test source",
        "url": "https://example.invalid/spec.json",
        "sha256": "sha256:" + "0" * 64,
        "target": "example/spec.json",
        "licence": "MIT",
        "attribution": "test",
    }
    return BenchmarkSource.model_validate({**base, **overrides})


def test_committed_manifest_loads_and_pins_its_source() -> None:
    manifest = load_manifest(MANIFEST)
    assert manifest.sources
    source = manifest.sources[0]
    assert source.url.startswith("https://")
    assert source.pinned_ref, "a source must be pinned, or the bytes can change underneath us"
    assert source.sha256 is not None, "a recorded source must carry a digest"
    assert source.attribution


def test_only_https_is_fetchable() -> None:
    with pytest.raises(ValueError, match="String should match pattern"):
        _source(url="http://example.invalid/spec.json")


def test_a_target_escaping_the_benchmark_directory_is_refused(tmp_path: Path) -> None:
    """A manifest is edited by a person, so a traversing target must be refused."""
    with pytest.raises(BenchmarkFetchError, match="resolves outside"):
        resolve_target(_source(target="../../etc/passwd"), tmp_path)


def test_a_recorded_source_is_not_refetched_when_it_already_verifies(tmp_path: Path) -> None:
    payload = b'{"openapi": "3.0.3"}'
    target = tmp_path / "example" / "spec.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    source = _source(sha256=digest_of(payload))
    outcome, resolved = fetch_source(source, tmp_path)
    assert outcome.skipped is True
    assert resolved.sha256 == digest_of(payload)


def test_an_unrecorded_source_is_refused_without_an_explicit_record(tmp_path: Path) -> None:
    """Trusting a source on first use has to be a deliberate act, not a side effect."""
    with pytest.raises(BenchmarkFetchError, match="no recorded digest"):
        fetch_source(_source(sha256=None, url="https://example.invalid/x.json"), tmp_path)


def test_digest_mismatch_refuses_and_writes_nothing(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A poisoned file must never reach the target path, even briefly."""
    monkeypatch.setattr(
        "api_mcp_compiler.benchmarks.download", lambda url: b"something else entirely"
    )
    source = _source()
    with pytest.raises(DigestMismatchError, match="Nothing was written"):
        fetch_source(source, tmp_path)
    assert not (tmp_path / "example" / "spec.json").exists()


def test_verified_bytes_are_written(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = b'{"openapi": "3.0.3"}'
    monkeypatch.setattr("api_mcp_compiler.benchmarks.download", lambda url: payload)
    outcome, _ = fetch_source(_source(sha256=digest_of(payload)), tmp_path)
    assert outcome.target.read_bytes() == payload
    assert outcome.skipped is False


def test_manifest_rejects_a_foreign_schema_version() -> None:
    with pytest.raises(ValueError, match="expected manifest schema_version"):
        BenchmarkManifest(schema_version="9.9.9")


def test_fetched_documents_are_not_tracked() -> None:
    """The point of fetching is that nothing third-party enters the tree."""
    ignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "examples/benchmarks/*" in ignore
    assert "!examples/benchmarks/manifest.json" in ignore
