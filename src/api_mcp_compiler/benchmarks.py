"""Fetching and verifying third-party benchmark documents.

Benchmark specifications are deliberately not stored in this repository. Using material and
redistributing it are different acts, and only the second carries an obligation this project
would have to resolve, so fetching sidesteps the question rather than answering it.

What makes that safe is the digest. A recorded source is verified on every fetch and a
mismatch is refused, so a result stays reconstructible from a manifest and a trace even though
the bytes live elsewhere. That is the same discipline the compiler already applies to every
document it loads.

Nothing here is called during ingestion. Ingestion never reaches the network; fetching is a
separate step a person runs deliberately.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from api_mcp_compiler.models import BenchmarkManifest, BenchmarkSource

#: Refuse anything larger. A benchmark specification is a document, not an archive.
MAX_FETCH_BYTES = 16 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 30


class BenchmarkFetchError(RuntimeError):
    """Raised when a source cannot be fetched or does not verify."""


class DigestMismatchError(BenchmarkFetchError):
    """Raised when fetched bytes do not match the digest the manifest recorded.

    This is the failure the whole mechanism exists to produce. A silent acceptance here would
    make every downstream result unreproducible without anyone noticing.
    """


class _SameHostRedirectHandler(HTTPRedirectHandler):
    """Follows redirects only within the original host.

    A redirect to another host turns a pinned, reviewed source into an arbitrary one, which is
    precisely what pinning was meant to prevent.
    """

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        if urlsplit(newurl).hostname != urlsplit(req.full_url).hostname:
            raise BenchmarkFetchError(
                f"refusing a redirect from {req.full_url} to another host at {newurl}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class FetchOutcome:
    """What happened for one source."""

    source_id: str
    target: Path
    digest: str
    recorded: bool
    skipped: bool = False


def digest_of(payload: bytes) -> str:
    """Return the prefixed sha256 of fetched bytes."""
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def resolve_target(source: BenchmarkSource, root: Path) -> Path:
    """Resolve where a source is written, refusing anything outside the benchmark directory.

    A manifest is a document a person edits, so a target that escapes the directory has to be
    refused rather than trusted.
    """
    candidate = (root / source.target).resolve()
    base = root.resolve()
    if not candidate.is_relative_to(base):
        raise BenchmarkFetchError(
            f"source {source.source_id!r} targets {source.target!r}, which resolves outside "
            f"{base}"
        )
    return candidate


def download(url: str) -> bytes:
    """Fetch one document over HTTPS, refusing anything unexpected.

    Bytes are returned rather than written, so a caller can verify before anything reaches the
    filesystem and a failed verification cannot leave a poisoned file behind.
    """
    if urlsplit(url).scheme != "https":
        raise BenchmarkFetchError(f"refusing to fetch {url!r}: only https is permitted")
    opener = build_opener(_SameHostRedirectHandler)
    request = Request(url, headers={"Accept": "application/json, text/plain, */*"})
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            payload = response.read(MAX_FETCH_BYTES + 1)
    except URLError as error:
        reason = str(getattr(error, "reason", error))
        hint = ""
        if "CERTIFICATE_VERIFY_FAILED" in reason:
            hint = (
                " This interpreter has no certificate authority bundle. On a python.org "
                "build, run its 'Install Certificates.command', or set SSL_CERT_FILE to a "
                "bundle. Certificate verification is never disabled to work around this."
            )
        raise BenchmarkFetchError(f"could not fetch {url}: {reason}.{hint}") from error
    except OSError as error:
        raise BenchmarkFetchError(f"could not fetch {url}: {error}") from error
    if not isinstance(payload, bytes):  # pragma: no cover - urllib always yields bytes
        raise BenchmarkFetchError(f"{url} did not return bytes")
    if len(payload) > MAX_FETCH_BYTES:
        raise BenchmarkFetchError(
            f"{url} exceeded the {MAX_FETCH_BYTES} byte ceiling for a benchmark document"
        )
    return payload


def fetch_source(
    source: BenchmarkSource, root: Path, *, record: bool = False
) -> tuple[FetchOutcome, BenchmarkSource]:
    """Fetch one source, verify it, and write it only if it verified.

    With no recorded digest the first fetch is trust-on-first-use and `record` must be set,
    which keeps the moment a source becomes trusted explicit rather than incidental. Every
    fetch afterwards is verified.
    """
    target = resolve_target(source, root)
    if source.sha256 and target.is_file() and digest_of(target.read_bytes()) == source.sha256:
        return (
            FetchOutcome(source.source_id, target, source.sha256, recorded=False, skipped=True),
            source,
        )

    if source.sha256 is None and not record:
        # Refused before any request. Fetching bytes only to reject them would be pointless
        # traffic, and it would make trusting a source look like something that happens on
        # its own rather than a deliberate act.
        raise BenchmarkFetchError(
            f"source {source.source_id!r} has no recorded digest. Re-run with --record to "
            "trust it on first use, once you are satisfied the source is the right one."
        )

    payload = download(source.url)
    actual = digest_of(payload)

    if source.sha256 is None:
        source = source.model_copy(update={"sha256": actual})
    elif actual != source.sha256:
        raise DigestMismatchError(
            f"source {source.source_id!r} fetched from {source.url} has digest {actual}, but "
            f"the manifest records {source.sha256}. Nothing was written."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return (
        FetchOutcome(source.source_id, target, actual, recorded=source.sha256 == actual),
        source,
    )


def load_manifest(path: Path) -> BenchmarkManifest:
    """Read a benchmark manifest."""
    return BenchmarkManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def save_manifest(manifest: BenchmarkManifest, path: Path) -> None:
    """Write a benchmark manifest as canonical JSON."""
    from api_mcp_compiler.contracts import canonical_json

    path.write_text(canonical_json(manifest.model_dump(mode="json")), encoding="utf-8")
