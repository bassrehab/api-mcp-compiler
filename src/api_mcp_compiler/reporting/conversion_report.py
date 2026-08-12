"""The conversion report a reviewer actually reads.

Everything else this project emits is for a machine. This is the one artifact meant for the
person who owns the API and has to decide what an agent may do with it, so it answers their
questions rather than restating the pipeline's: what came in, what is proposed, what is
blocked, and what needs a decision from them specifically.

It is a single self-contained file. A report that needs a server to read is a report that
does not get read, and one that pulls in a stylesheet from somewhere else stops rendering the
day that somewhere else changes.

A report is never overwritten. It is evidence of what was proposed at a moment, and a decision
made against one set of proposals is not evidence about a different set.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

from api_mcp_compiler.models import (
    ApiSemanticIR,
    EmissionStatus,
    PolicyManifest,
    ReviewStatus,
    ToolPlan,
    ToolSurface,
)

_SAFE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class WrittenReport:
    """Where a report went and what it said needs deciding."""

    path: Path
    awaiting_review: list[str]
    blocked: dict[str, str]
    executable: list[str]


def next_report_path(directory: Path, service_id: str, source_digest: str) -> Path:
    """Choose a filename that does not collide with an earlier report.

    The digest is in the name so a report can be tied to the exact specification bytes it
    describes, and the sequence number means a second run against the same bytes is kept
    beside the first rather than replacing it.
    """
    slug = _SAFE.sub("-", service_id.lower()).strip("-")
    short = source_digest.split(":")[-1][:12]
    existing = sorted(directory.glob(f"{slug}.{short}.*.html")) if directory.is_dir() else []
    return directory / f"{slug}.{short}.{len(existing) + 1:03d}.html"


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _rows(cells: list[list[str]]) -> str:
    return "".join(
        "<tr>" + "".join(f"<td>{item}</td>" for item in row) + "</tr>" for row in cells
    )


def _decision_table(plan: ToolPlan) -> str:
    by_kind: dict[str, list[str]] = {}
    for decision in plan.decisions:
        by_kind.setdefault(decision.kind.value, []).append(decision.target)
    rows = [
        [_escape(kind), str(len(targets)), _escape(", ".join(sorted(set(targets))[:4]))]
        for kind, targets in sorted(by_kind.items())
    ]
    return _rows(rows)


def render(
    ir: ApiSemanticIR,
    plan: ToolPlan,
    surface: ToolSurface,
    manifest: PolicyManifest | None = None,
) -> tuple[str, WrittenReport]:
    """Render the report and the summary of what it asks a reviewer to decide."""
    executable = [item for item in surface.tools if item.emission is EmissionStatus.EXECUTABLE]
    blocked = {
        item.name: ", ".join(blocker.value for blocker in item.blockers)
        for item in surface.tools
        if item.emission is not EmissionStatus.EXECUTABLE
    }
    awaiting = sorted(
        item.name for item in plan.artifacts if item.review_status is ReviewStatus.PROPOSED
    )
    blocking = [item for item in ir.ambiguities if item.blocking]

    # The group is a planning decision and lives on the artifact, not on the emitted tool.
    group_of = {
        artifact.name: artifact.group or "" for artifact in plan.artifacts if artifact.group
    }
    tool_rows = _rows(
        [
            [
                f"<code>{_escape(item.name)}</code>",
                _escape(item.risk.value),
                _escape(group_of.get(item.name, "")),
                (
                    '<span class="ok">executable</span>'
                    if item.emission is EmissionStatus.EXECUTABLE
                    else f'<span class="held">{_escape(blocked.get(item.name, "held"))}</span>'
                ),
                str(len(item.input_schema.get("properties", {}))),
                _escape(item.description[:110]),
            ]
            for item in sorted(surface.tools, key=lambda entry: entry.name)
        ]
    )

    policy_rows = ""
    if manifest is not None:
        policy_rows = _rows(
            [
                [
                    f"<code>{_escape(item.tool_name)}</code>",
                    _escape(item.approval.value),
                    _escape(item.sensitivity.value),
                    _escape(", ".join(item.required_scopes) or "none"),
                    "yes" if item.confirmation is not None else "no",
                ]
                for item in sorted(manifest.policies, key=lambda entry: entry.tool_name)
            ]
        )

    ambiguity_rows = _rows(
        [
            [
                _escape(item.code),
                _escape(item.field),
                "blocking" if item.blocking else "advisory",
                _escape(item.detail[:130]),
            ]
            for item in sorted(ir.ambiguities, key=lambda entry: (not entry.blocking, entry.code))[
                :40
            ]
        ]
    )

    decide_note = (
        "Nothing is waiting on you. Every proposed artifact has been reviewed."
        if not awaiting
        else (
            f"{len(awaiting)} artifact(s) cannot be emitted as executable until you approve "
            "them. Approve by class rather than one at a time: "
            "<code>api-mcp-compiler approve --risk read</code> or "
            "<code>--group &lt;name&gt;</code>."
        )
    )

    document = f"""<!doctype html>
<meta charset="utf-8">
<title>Conversion report: {_escape(ir.service.title)}</title>
<style>
 :root {{ --ink:#14181d; --muted:#5b6673; --line:#dde3ea; --held:#8a5a00;
          --heldbg:#fff6e0; --ok:#0a6b3d; --okbg:#e8f6ee; --bg:#fff; }}
 @media (prefers-color-scheme: dark) {{
   :root {{ --ink:#e6eaef; --muted:#9aa6b4; --line:#2b333d; --held:#f0c274;
            --heldbg:#3a2f14; --ok:#7ede9f; --okbg:#12331f; --bg:#14181d; }}
 }}
 body {{ background:var(--bg); color:var(--ink); margin:0 auto; padding:2.5rem 1.25rem;
         max-width:62rem; font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
 h1 {{ font-size:1.6rem; margin:0 0 .25rem; }}
 h2 {{ font-size:1.1rem; margin:2.5rem 0 .5rem; padding-bottom:.3rem;
       border-bottom:1px solid var(--line); }}
 .sub {{ color:var(--muted); margin:0 0 2rem; font-size:.9rem; }}
 .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(9rem,1fr)); gap:.75rem; }}
 .card {{ border:1px solid var(--line); border-radius:.5rem; padding:.75rem 1rem; }}
 .card b {{ display:block; font-size:1.6rem; font-weight:600; }}
 .card span {{ color:var(--muted); font-size:.8rem; }}
 .decide {{ border-left:3px solid var(--held); background:var(--heldbg); color:var(--held);
            padding:.9rem 1.1rem; border-radius:0 .4rem .4rem 0; margin:1rem 0; }}
 .wrap {{ overflow-x:auto; }}
 table {{ border-collapse:collapse; width:100%; font-size:.85rem; }}
 th,td {{ text-align:left; padding:.45rem .6rem; border-bottom:1px solid var(--line);
          vertical-align:top; }}
 th {{ color:var(--muted); font-weight:600; white-space:nowrap; }}
 code {{ font:.85em ui-monospace,SFMono-Regular,Menlo,monospace; }}
 .ok {{ color:var(--ok); background:var(--okbg); padding:.1rem .45rem; border-radius:.25rem; }}
 .held {{ color:var(--held); background:var(--heldbg); padding:.1rem .45rem;
          border-radius:.25rem; }}
 footer {{ margin-top:3rem; color:var(--muted); font-size:.8rem; }}
</style>
<h1>{_escape(ir.service.title)}</h1>
<p class="sub">
  Conversion report &middot; planner <code>{_escape(plan.planner.value)}</code> &middot;
  source digest <code>{_escape(ir.service.source_digest)}</code>
</p>

<div class="grid">
  <div class="card"><b>{len(ir.operations)}</b><span>operations read</span></div>
  <div class="card"><b>{len(surface.tools)}</b><span>tools planned</span></div>
  <div class="card"><b>{len(executable)}</b><span>executable now</span></div>
  <div class="card"><b>{len(blocked)}</b><span>held by the gate</span></div>
  <div class="card"><b>{len(awaiting)}</b><span>awaiting you</span></div>
  <div class="card"><b>{len(blocking)}</b><span>blocking ambiguities</span></div>
</div>

<h2>What needs a decision</h2>
<div class="decide">{decide_note}</div>

<h2>The surface</h2>
<div class="wrap"><table>
<tr><th>Tool</th><th>Risk</th><th>Group</th><th>Status</th><th>Args</th><th>Description</th></tr>
{tool_rows}
</table></div>

<h2>Governance</h2>
{'<div class="wrap"><table><tr><th>Tool</th><th>Approval</th><th>Sensitivity</th>'
 '<th>Scopes</th><th>Confirm</th></tr>' + policy_rows + '</table></div>'
 if policy_rows else '<p class="sub">No policy manifest was supplied.</p>'}

<h2>Planning decisions</h2>
<div class="wrap"><table>
<tr><th>Kind</th><th>Count</th><th>Examples</th></tr>
{_decision_table(plan)}
</table></div>

<h2>What could not be resolved</h2>
{'<div class="wrap"><table><tr><th>Code</th><th>Field</th><th>Severity</th><th>Detail</th></tr>'
 + ambiguity_rows + '</table></div>'
 if ambiguity_rows else '<p class="sub">Nothing was left unresolved.</p>'}

<footer>
  Generated from the specification bytes named above. Reports are never overwritten, so an
  earlier decision stays attached to the proposals it was made against.
</footer>
"""
    return document, WrittenReport(
        path=Path(),
        awaiting_review=awaiting,
        blocked=blocked,
        executable=[item.name for item in executable],
    )


def write_report(
    directory: Path,
    ir: ApiSemanticIR,
    plan: ToolPlan,
    surface: ToolSurface,
    manifest: PolicyManifest | None = None,
) -> WrittenReport:
    """Render a report and write it to a filename no earlier report used."""
    document, summary = render(ir, plan, surface, manifest)
    directory.mkdir(parents=True, exist_ok=True)
    path = next_report_path(directory, ir.service.service_id, ir.service.source_digest)
    path.write_text(document, encoding="utf-8")
    return WrittenReport(
        path=path,
        awaiting_review=summary.awaiting_review,
        blocked=summary.blocked,
        executable=summary.executable,
    )
