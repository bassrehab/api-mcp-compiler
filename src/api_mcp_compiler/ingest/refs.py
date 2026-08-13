"""JSON Reference resolution for OpenAPI documents.

A specification is third-party input, and `$ref` is a pointer its author controls. An
adapter that follows references by default therefore turns "parse this file" into "read
whatever the document names", which is why resolution here defaults to deny and never
reaches the network.

Resolution inlines the target value so downstream planners see complete schemas, while the
caller records provenance naming the original reference site, so traceability survives.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from api_mcp_compiler.ingest.documents import load_document
from api_mcp_compiler.models import Ambiguity, DocumentRole, SourceDocumentIR
from api_mcp_compiler.provenance import openapi_pointer

DEFAULT_MAX_DEPTH = 16


class RefResolutionError(ValueError):
    """Raised when a reference chain cannot be resolved safely."""


@dataclass(frozen=True)
class RefPolicy:
    """What reference targets an adapter is permitted to load.

    The default denies everything outside the root document. A caller that wants
    multi-file resolution must name the directories it trusts, and containment is checked
    against real paths so a relative reference cannot traverse out of them.
    """

    allowed_directories: tuple[Path, ...] = ()
    max_depth: int = DEFAULT_MAX_DEPTH

    def permits(self, candidate: Path) -> bool:
        """Report whether a resolved file path lies inside an allowed directory."""
        try:
            real = candidate.resolve(strict=True)
        except OSError:
            return False
        for allowed in self.allowed_directories:
            try:
                if real.is_relative_to(allowed.resolve(strict=True)):
                    return True
            except OSError:
                continue
        return False


@dataclass
class _Document:
    """One loaded document and the identity of its bytes."""

    uri: str
    path: Path | None
    data: Any
    digest: str
    role: DocumentRole


@dataclass
class RefResolver:
    """Resolves `$ref` within and across documents under a policy.

    Instances are single-use per compile: they accumulate the set of documents loaded and
    the ambiguities raised, both of which the adapter folds into the IR.
    """

    root_path: Path
    root_data: dict[str, Any]
    root_digest: str
    policy: RefPolicy = field(default_factory=RefPolicy)
    #: Remote documents already fetched and pinned, as URL to file and expected digest. Empty
    #: by default, which is what keeps a plain compile unable to reach the network at all.
    vendored: dict[str, tuple[Path, str]] = field(default_factory=dict)
    ambiguities: list[Ambiguity] = field(default_factory=list)
    _documents: dict[str, _Document] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        uri = self.root_path.as_posix()
        self._documents[uri] = _Document(
            uri=uri,
            path=self.root_path,
            data=self.root_data,
            digest=self.root_digest,
            role=DocumentRole.ROOT,
        )
        self._root_uri = uri

    @property
    def documents(self) -> list[SourceDocumentIR]:
        """Every document loaded during this compile, root first."""
        ordered = sorted(
            self._documents.values(),
            key=lambda item: (item.role is not DocumentRole.ROOT, item.uri),
        )
        return [
            SourceDocumentIR(uri=item.uri, digest=item.digest, role=item.role)
            for item in ordered
        ]

    def resolve(self, node: Any, *, field_path: str, pointer: str) -> Any:
        """Return a copy of `node` with every reachable reference inlined.

        `field_path` and `pointer` describe the IR field and source location being
        resolved, and are used only to make any ambiguity legible.
        """
        return self._walk(node, self._root_uri, (), 0, field_path, pointer)

    def _walk(
        self,
        node: Any,
        document_uri: str,
        stack: tuple[tuple[str, str], ...],
        depth: int,
        field_path: str,
        pointer: str,
    ) -> Any:
        if depth > self.policy.max_depth:
            raise RefResolutionError(
                f"reference chain from {pointer} exceeded the maximum depth of "
                f"{self.policy.max_depth}"
            )
        if isinstance(node, list):
            return [
                self._walk(item, document_uri, stack, depth, field_path, pointer)
                for item in node
            ]
        if not isinstance(node, dict):
            return node
        target = node.get("$ref")
        if not isinstance(target, str):
            return {
                key: self._walk(value, document_uri, stack, depth, field_path, pointer)
                for key, value in node.items()
            }
        return self._follow(node, target, document_uri, stack, depth, field_path, pointer)

    def _follow(
        self,
        node: dict[str, Any],
        target: str,
        document_uri: str,
        stack: tuple[tuple[str, str], ...],
        depth: int,
        field_path: str,
        pointer: str,
    ) -> Any:
        resolved_uri, fragment, refusal = self._locate(target, document_uri, field_path, pointer)
        if refusal is not None:
            self.ambiguities.append(refusal)
            return node

        key = (resolved_uri, fragment)
        if key in stack:
            # Decision: a self-referencing schema is legitimate and cannot be
            # inlined. Leaving the innermost $ref in place keeps the result a valid JSON
            # Schema; erroring here would reject correct specifications.
            self.ambiguities.append(
                Ambiguity(
                    code="recursive_reference",
                    field=field_path,
                    source_pointer=pointer,
                    detail=(
                        f"Reference {target!r} is recursive. The innermost occurrence was left "
                        "unexpanded so the schema remains finite and valid."
                    ),
                    blocking=False,
                )
            )
            return node

        document = self._documents[resolved_uri]
        try:
            value = _dereference(document.data, fragment)
        except (KeyError, IndexError, TypeError):
            self.ambiguities.append(
                Ambiguity(
                    code="ref_target_missing",
                    field=field_path,
                    source_pointer=pointer,
                    detail=(
                        f"Reference {target!r} does not resolve to a value in "
                        f"{document.uri}."
                    ),
                    blocking=True,
                )
            )
            return node

        resolved = self._walk(
            value, resolved_uri, (*stack, key), depth + 1, field_path, pointer
        )
        siblings = {name: item for name, item in node.items() if name != "$ref"}
        if not siblings or not isinstance(resolved, dict):
            return resolved
        # OpenAPI 3.1 permits keywords alongside $ref and applies them over the target.
        merged = dict(resolved)
        merged.update(
            {
                name: self._walk(item, document_uri, stack, depth, field_path, pointer)
                for name, item in siblings.items()
            }
        )
        return merged

    def _locate(
        self, target: str, document_uri: str, field_path: str, pointer: str
    ) -> tuple[str, str, Ambiguity | None]:
        """Turn a reference into a loaded document URI and a fragment."""
        split = urlsplit(target)
        fragment = unquote(split.fragment)
        if split.scheme in {"http", "https"}:
            return self._locate_vendored(target, field_path, pointer, fragment)
        if not split.path:
            return document_uri, fragment, None

        current = self._documents[document_uri]
        base = current.path.parent if current.path is not None else self.root_path.parent
        candidate = (base / unquote(split.path)).resolve()
        if not self.policy.permits(candidate):
            return (
                "",
                "",
                Ambiguity(
                    code="ref_not_allowlisted",
                    field=field_path,
                    source_pointer=pointer,
                    detail=(
                        f"Reference {target!r} resolves outside the allowed directories, so the "
                        "target was not loaded. Pass the directory explicitly to permit it."
                    ),
                    blocking=True,
                ),
            )
        uri = candidate.as_posix()
        if uri not in self._documents:
            self._documents[uri] = _load_document(candidate)
        return uri, fragment, None


    def _locate_vendored(
        self, target: str, field_path: str, pointer: str, fragment: str
    ) -> tuple[str, str, Ambiguity | None]:
        """Resolve a remote reference from what was vendored, or refuse it.

        Still no network. The bytes were fetched by a separate, deliberate command and pinned
        by digest; this reads them from disk and checks they are the ones that were pinned.
        A reference the lock does not name is refused exactly as it was before.
        """
        from api_mcp_compiler.ingest.vendoring import VendoringError, read_cached, strip_fragment

        document_url = strip_fragment(target)
        entry = self.vendored.get(document_url)
        if entry is None:
            return (
                "",
                "",
                Ambiguity(
                    code="remote_ref_refused",
                    field=field_path,
                    source_pointer=pointer,
                    detail=(
                        f"Reference {target!r} is remote. Ingestion never reaches the network, "
                        "so the target was not loaded. Vendor it first with `vendor-refs`, "
                        "which fetches it once, pins it by digest and records a lock."
                    ),
                    blocking=True,
                ),
            )

        path, expected = entry
        uri = path.as_posix()
        if uri not in self._documents:
            try:
                read_cached(document_url, path, expected)
            except VendoringError as error:
                return (
                    "",
                    "",
                    Ambiguity(
                        code="vendored_ref_unusable",
                        field=field_path,
                        source_pointer=pointer,
                        detail=str(error),
                        blocking=True,
                    ),
                )
            loaded = _load_document(path)
            # Recorded under the URL it came from, so an artifact says where the bytes
            # originated rather than where they happen to sit in someone's cache.
            self._documents[uri] = replace(loaded, uri=document_url)
        return uri, fragment, None


def _load_document(path: Path) -> _Document:
    """Read and digest one referenced document, refusing duplicate keys as the root does."""
    data, digest = load_document(path)
    return _Document(
        uri=path.as_posix(),
        path=path,
        data=data,
        digest=digest,
        role=DocumentRole.REFERENCED,
    )


def _dereference(document: Any, fragment: str) -> Any:
    """Resolve an RFC 6901 pointer fragment against a document."""
    if fragment in {"", "/"}:
        return document
    node = document
    for raw in fragment.lstrip("#").lstrip("/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        node = node[int(token)] if isinstance(node, list) else node[token]
    return node


def unresolved_refs(node: Any, tokens: tuple[str, ...]) -> list[tuple[tuple[str, ...], str]]:
    """Collect every `$ref` still present after resolution, with its location."""
    found: list[tuple[tuple[str, ...], str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append((tokens, value))
            else:
                found.extend(unresolved_refs(value, (*tokens, str(key))))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(unresolved_refs(item, (*tokens, str(index))))
    return found


def pointer_for(tokens: tuple[str, ...]) -> str:
    """Build an OpenAPI source pointer for a token path."""
    return openapi_pointer(*tokens)
