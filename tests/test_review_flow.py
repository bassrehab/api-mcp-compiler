"""The human path: report, approve, serve.

A gate a person cannot pass without hand-editing JSON is a gate that gets bypassed wholesale,
so these check the ergonomics as carefully as the safety.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.models import ReviewStatus, RiskClass
from api_mcp_compiler.planning.approval import ApprovalSelectionError, approve
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy
from api_mcp_compiler.reporting.conversion_report import next_report_path, render, write_report
from tests.conftest import ORDER_SERVICE


def _plan():
    ir = parse_openapi(Path(ORDER_SERVICE))
    return ir, plan_semantic(ir)


def test_a_report_is_never_overwritten(tmp_path: Path) -> None:
    """A decision made against one set of proposals is not evidence about a different set."""
    ir, plan = _plan()
    surface = generate_surface(ir, plan)
    first = write_report(tmp_path, ir, plan, surface)
    second = write_report(tmp_path, ir, plan, surface)
    assert first.path != second.path
    assert first.path.is_file() and second.path.is_file()
    assert len(sorted(tmp_path.glob("*.html"))) == 2


def test_a_report_is_named_for_the_bytes_it_describes(tmp_path: Path) -> None:
    ir, _ = _plan()
    path = next_report_path(tmp_path, ir.service.service_id, ir.service.source_digest)
    assert ir.service.source_digest.split(":")[-1][:12] in path.name


def test_a_report_is_self_contained(tmp_path: Path) -> None:
    """A report that fetches a stylesheet stops rendering the day that host changes."""
    ir, plan = _plan()
    document, _ = render(ir, plan, generate_surface(ir, plan), synthesize_policy(ir, plan))
    for marker in ("<script", "src=\"http", "href=\"http", "@import"):
        assert marker not in document


def test_a_report_says_what_needs_a_decision(tmp_path: Path) -> None:
    ir, plan = _plan()
    written = write_report(tmp_path, ir, plan, generate_surface(ir, plan))
    assert written.awaiting_review
    assert "awaiting" in written.path.read_text(encoding="utf-8").lower()


def test_approval_by_risk_covers_a_class_not_one_tool() -> None:
    _, plan = _plan()
    outcome = approve(plan, risk=RiskClass.READ)
    assert len(outcome.approved) > 1
    assert all(
        entry.review_status is ReviewStatus.APPROVED for entry in outcome.overlay.entries
    )


def test_approval_reports_what_it_did_not_cover() -> None:
    """A reviewer who cannot see what is still outstanding has not finished reviewing."""
    _, plan = _plan()
    outcome = approve(plan, risk=RiskClass.READ)
    assert outcome.untouched, "writes should remain outstanding after approving reads"


def test_approval_extends_an_overlay_rather_than_replacing_it() -> None:
    _, plan = _plan()
    first = approve(plan, risk=RiskClass.READ)
    second = approve(plan, overlay=first.overlay, risk=RiskClass.WRITE)
    approved = {
        entry.operation_id
        for entry in second.overlay.entries
        if entry.review_status is ReviewStatus.APPROVED
    }
    earlier = {
        entry.operation_id
        for entry in first.overlay.entries
        if entry.review_status is ReviewStatus.APPROVED
    }
    assert earlier <= approved, "approving writes must not discard the earlier read approvals"


def test_a_selection_that_names_nothing_is_refused() -> None:
    """There is deliberately no flag that approves a surface without naming a class."""
    _, plan = _plan()
    with pytest.raises(ApprovalSelectionError, match="name what is being approved"):
        approve(plan)


def test_a_selection_matching_no_tool_is_refused() -> None:
    _, plan = _plan()
    with pytest.raises(ApprovalSelectionError, match="matches nothing"):
        approve(plan, group="no-such-group")


def test_an_unknown_tool_name_is_refused_rather_than_silently_ignored() -> None:
    _, plan = _plan()
    with pytest.raises(ApprovalSelectionError, match="no artifact named"):
        approve(plan, names=["not_a_tool"])
