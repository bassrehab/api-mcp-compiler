"""Loading, saving and re-stamping tool overlays.

An overlay is a human artifact: it is edited by a reviewer, not generated. It is therefore
read from JSON or YAML, whichever the reviewer prefers, and written back as canonical JSON so
that a committed overlay diffs cleanly.

The digest it carries is the point of the whole mechanism. Re-stamping is deliberately a
separate, explicit action rather than something that happens on load, because silently
accepting an overlay against changed bytes would mean approvals survive a specification
change that might have invalidated them.
"""

from __future__ import annotations

from pathlib import Path

from api_mcp_compiler.contracts import canonical_json
from api_mcp_compiler.ingest.documents import load_document
from api_mcp_compiler.models import ToolOverlay


def load_overlay(path: Path) -> ToolOverlay:
    """Read an overlay from JSON or YAML."""
    data, _ = load_document(path)
    return ToolOverlay.model_validate(data)


def save_overlay(overlay: ToolOverlay, path: Path) -> None:
    """Write an overlay as canonical JSON."""
    path.write_text(canonical_json(overlay.model_dump(mode="json")), encoding="utf-8")


def restamp(overlay: ToolOverlay, source_digest: str) -> ToolOverlay:
    """Return the overlay bound to a new specification revision.

    Call this only after re-reviewing the decisions against the new specification. The
    digest is what stops an approval granted for one revision from silently applying to
    another, so re-stamping without reading is the one way to defeat the mechanism.
    """
    return overlay.model_copy(update={"source_digest": source_digest})
