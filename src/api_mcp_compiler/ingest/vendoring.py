"""Bringing remote references onto disk, as a separate and deliberate act.

Ingestion never reaches the network, and that stays true. A specification is third-party
input, and following its pointers is an action taken on its author's behalf, at compile time,
against whatever the URL happens to serve at that moment. Doing it silently would make a
compile depend on the network and on time, which is the opposite of what the digests in this
project are for.

So fetching is its own step with its own command. It records a lock: for each remote
reference, the URL it came from, the digest of the bytes that were fetched, and the local file
they were written to. Ingestion then resolves those references from disk and verifies the
digest, and refuses anything the lock does not name.

That gives the three properties the alternative could not have. A compile is reproducible from
the specification, the lock and the cache, with no network at all. A change upstream is
visible as a digest that no longer matches, rather than as a surface that quietly became
something else. And trusting a source is a moment someone chose, not something that happened
while a build ran.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

REF_LOCK_SCHEMA_VERSION = "0.1.0"

#: The same ceiling the benchmark fetcher uses. A reference document larger than this is
#: almost certainly not a schema fragment.
MAX_REFERENCE_BYTES = 8 * 1024 * 1024


class VendoringError(RuntimeError):
    """Raised when a remote reference cannot be brought onto disk safely."""


class LockedReference(BaseModel):
    """One remote reference, pinned to the bytes it resolved to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    path: str = Field(description="Where the bytes were written, relative to the lock file.")


class RefLock(BaseModel):
    """Every remote reference one specification needs, and what each resolved to.

    Committed alongside the specification. Without it a document with remote references does
    not compile at all, which is the intended failure: a missing lock is a person deciding
    nothing, and this project does not treat that as consent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = REF_LOCK_SCHEMA_VERSION
    source_digest: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="Digest of the specification these references were collected from.",
    )
    references: list[LockedReference] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _pinned_version(cls, value: str) -> str:
        if value != REF_LOCK_SCHEMA_VERSION:
            raise ValueError(f"expected ref lock schema_version {REF_LOCK_SCHEMA_VERSION}")
        return value

    def resolve(self, url: str) -> LockedReference | None:
        """The entry for one URL, ignoring its fragment, or None if it is not locked."""
        wanted = strip_fragment(url)
        return next((item for item in self.references if item.url == wanted), None)


def strip_fragment(url: str) -> str:
    """The document a reference points at, without the pointer into it.

    Two references into different parts of the same remote document are one document, and
    fetching it twice would be two chances for the answers to disagree.
    """
    split = urlsplit(url)
    return urlunsplit((split.scheme, split.netloc, split.path, split.query, ""))


def digest_of(payload: bytes) -> str:
    """Prefixed sha256 of fetched bytes."""
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def remote_references(document: Any) -> list[str]:
    """Every distinct remote document a specification points at, in first-seen order."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            target = node.get("$ref")
            if isinstance(target, str) and urlsplit(target).scheme in {"http", "https"}:
                document_url = strip_fragment(target)
                if document_url not in found:
                    found.append(document_url)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(document)
    return found


def local_name(url: str) -> str:
    """A stable file name for one URL.

    Named by digest of the URL rather than by its path, because two hosts serving
    `components.yaml` are two documents and a cache that let one overwrite the other would
    resolve a reference to bytes from somewhere else entirely.
    """
    stem = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    suffix = Path(urlsplit(url).path).suffix
    return f"{stem}{suffix if suffix in {'.json', '.yaml', '.yml'} else '.json'}"


def vendor(
    document: Any,
    source_digest: str,
    lock_path: Path,
    cache: Path,
    *,
    record: bool,
    existing: RefLock | None = None,
) -> tuple[RefLock, list[str], list[str]]:
    """Bring every remote reference onto disk, and return the lock, fetched and unchanged.

    A URL the existing lock already names is fetched and verified against it. A URL it does
    not name is trust on first use, which requires `record`, so the moment a source becomes
    trusted is a decision someone made rather than something a build did on their behalf.

    Nothing is written until bytes verify, so a failed run cannot leave a half-vendored cache
    that the next compile would treat as authoritative.
    """
    from api_mcp_compiler.benchmarks import BenchmarkFetchError, download

    wanted = remote_references(document)
    known = {item.url: item for item in (existing.references if existing else [])}
    unknown = [url for url in wanted if url not in known]
    if unknown and not record:
        listed = "\n  ".join(unknown)
        raise VendoringError(
            f"{len(unknown)} remote reference(s) are not in the lock:\n  {listed}\n"
            "Re-run with --record to trust them on first use, once you are satisfied they "
            "are the documents you meant. Nothing was fetched."
        )

    entries: list[LockedReference] = []
    fetched: list[str] = []
    unchanged: list[str] = []
    for url in wanted:
        target = cache / local_name(url)
        prior = known.get(url)
        # Already on disk and still the bytes that were locked: nothing to ask the network
        # about, which is the common case on every run after the first.
        if prior is not None and _matches(target, prior.sha256):
            entries.append(prior)
            unchanged.append(url)
            continue
        try:
            payload = download(url)
        except BenchmarkFetchError as error:
            raise VendoringError(str(error)) from error
        actual = digest_of(payload)
        if prior is not None and actual != prior.sha256:
            raise VendoringError(
                f"{url} is locked to {prior.sha256} and now serves {actual}. Nothing was "
                "written. If the change is intended, delete the entry and re-record it, "
                "having read what changed."
            )
        cache.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        entries.append(
            LockedReference(
                url=url,
                sha256=actual,
                path=_relative(target, lock_path.parent),
            )
        )
        fetched.append(url)

    return (
        RefLock(source_digest=source_digest, references=entries),
        fetched,
        unchanged,
    )


def _matches(target: Path, expected: str) -> bool:
    """Whether the file on disk is the bytes that were pinned."""
    return target.is_file() and digest_of(target.read_bytes()) == expected


def _relative(target: Path, base: Path) -> str:
    """Where the cache sits relative to the lock, so the pair can move together."""
    try:
        return target.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return target.resolve().as_posix()


def load_lock(path: Path) -> RefLock:
    """Read a lock file, refusing one written against a different contract version."""
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise VendoringError(f"could not read reference lock {path}: {error}") from error
    return RefLock.model_validate(payload)


def cached_documents(lock: RefLock, lock_path: Path) -> dict[str, tuple[Path, str]]:
    """Map each locked URL to the file holding it and the digest it must have."""
    base = lock_path.parent
    return {
        item.url: ((base / item.path).resolve(), item.sha256) for item in lock.references
    }


def read_cached(url: str, path: Path, expected: str) -> bytes:
    """Read one vendored document, refusing bytes that are not the ones that were locked."""
    if not path.is_file():
        raise VendoringError(
            f"{url} is locked to {path}, which is missing. Re-run the vendor command to "
            "fetch it, or restore the cache alongside the lock."
        )
    payload = path.read_bytes()
    actual = digest_of(payload)
    if actual != expected:
        raise VendoringError(
            f"{url} was locked to {expected} and the file on disk digests to {actual}. "
            "Nothing was loaded. Re-run the vendor command deliberately if the change is "
            "intended, and read the diff before you do."
        )
    return payload
