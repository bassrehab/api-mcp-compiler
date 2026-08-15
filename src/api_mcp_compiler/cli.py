"""Command line interface for the API-to-MCP agent-readiness compiler.

Exposes inspection, baseline planning, tool-surface generation and contract validation.
No command runs an MCP server, binds to an SDK, or performs network access.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

import typer

from api_mcp_compiler.codegen.mcp_server import GENERATED_REQUIREMENTS, emit_server
from api_mcp_compiler.codegen.soap_server import emit_soap_server
from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.contracts import (
    ContractViolation,
    canonical_json,
    dump_canonical,
    validate_ir,
    validate_tool_plan,
    validate_tool_surface,
)
from api_mcp_compiler.evaluation.harness import run_corpus
from api_mcp_compiler.ingest.asyncapi import is_asyncapi, parse_asyncapi
from api_mcp_compiler.ingest.catalogue import is_catalogue, parse_catalogue
from api_mcp_compiler.ingest.documents import load_document
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.ingest.refs import RefPolicy
from api_mcp_compiler.ingest.vendoring import (
    VendoringError,
    cached_documents,
    load_lock,
    remote_references,
    vendor,
)
from api_mcp_compiler.ingest.wsdl import parse_wsdl
from api_mcp_compiler.models import (
    ApiSemanticIR,
    EvalCorpus,
    PlannerKind,
    RiskClass,
    SourceFormat,
    ToolPlan,
)
from api_mcp_compiler.planning.approval import ApprovalSelectionError, approve
from api_mcp_compiler.planning.baseline import plan_baseline
from api_mcp_compiler.planning.overlay import load_overlay, restamp, save_overlay
from api_mcp_compiler.planning.report import review_report
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy
from api_mcp_compiler.reporting.conversion_report import write_report

app = typer.Typer(no_args_is_help=True, help=__doc__)

_WSDL_SUFFIXES = {".wsdl", ".xml"}


class SourceKind(StrEnum):
    """How to interpret a source document."""

    AUTO = "auto"
    OPENAPI = "openapi"
    WSDL = "wsdl"
    CATALOGUE = "catalogue"
    ASYNCAPI = "asyncapi"


REFS_LOCK_HELP = (
    "Path to a reference lock written by `vendor-refs`. Remote references resolve from the "
    "files it pins, and any whose bytes have changed are refused. Ingestion still performs "
    "no network access."
)

OVERLAY_HELP = (
    "Path to a reviewed overlay. Its digest must match the specification, so decisions made "
    "about other bytes are refused rather than silently applied."
)
PLANNER_HELP = "Which planner to use. The baseline exists only for controlled comparison."
ENFORCE_POLICY_HELP = (
    "Derive a policy manifest and fail closed on any tool whose policy is unresolved. "
    "Disabling this shows what would be emitted without governance; it is not a safe mode."
)

ALLOW_DIR_HELP = (
    "Directory whose files may be loaded by $ref. Repeatable. Omitted by default, so a "
    "specification cannot pull in files it was not explicitly pointed at."
)


def _detect(source: Path) -> SourceKind:
    """Read a document's marker to choose an adapter.

    Reads it rather than trusting the extension, because a catalogue, an AsyncAPI document and
    an OpenAPI document are all YAML and a caller should not have to say which they have.

    A parse failure falls through to OpenAPI rather than being reported here: choosing an
    adapter is not the place to explain that a document is malformed, and the adapter that
    receives it will say so better.
    """
    try:
        payload, _ = load_document(source)
    except Exception:
        return SourceKind.OPENAPI
    if is_catalogue(payload):
        return SourceKind.CATALOGUE
    if is_asyncapi(payload):
        return SourceKind.ASYNCAPI
    return SourceKind.OPENAPI


def _parse(
    source: Path,
    kind: SourceKind,
    allow_dir: list[Path] | None = None,
    refs_lock: Path | None = None,
) -> ApiSemanticIR:
    """Dispatch a source document to the matching ingestion adapter.

    A catalogue is detected by its marker rather than its extension, because it is YAML like
    an OpenAPI document and a caller should not have to say which they have.
    """
    if kind is SourceKind.AUTO:
        # WSDL is decided by extension because it is XML and the others are not; everything
        # else is decided by reading the document's own marker.
        kind = (
            SourceKind.WSDL
            if source.suffix.lower() in _WSDL_SUFFIXES
            else _detect(source)
        )
    if kind is SourceKind.WSDL:
        return parse_wsdl(source)
    if kind is SourceKind.CATALOGUE:
        return parse_catalogue(source)
    if kind is SourceKind.ASYNCAPI:
        return parse_asyncapi(source)
    vendored = cached_documents(load_lock(refs_lock), refs_lock) if refs_lock else None
    return parse_openapi(
        source,
        policy=RefPolicy(allowed_directories=tuple(allow_dir or ())),
        vendored=vendored,
    )


def _plan(ir: ApiSemanticIR, planner: PlannerKind, overlay: Path | None) -> ToolPlan:
    """Build a plan with the requested planner, applying an overlay when one is given."""
    if planner is PlannerKind.BASELINE:
        return plan_baseline(ir)
    return plan_semantic(ir, load_overlay(overlay) if overlay else None)


@app.command()
def inspect(
    source: Path = typer.Argument(..., help="Path to an OpenAPI 3.x or WSDL 1.1 document."),
    kind: SourceKind = typer.Option(SourceKind.AUTO, "--kind", help="Override format detection."),
    allow_dir: list[Path] = typer.Option([], "--allow-dir", help=ALLOW_DIR_HELP),
    refs_lock: Path | None = typer.Option(None, "--refs-lock", help=REFS_LOCK_HELP),
    baseline: bool = typer.Option(True, help="Include the baseline tool plan in the output."),
) -> None:
    """Parse a source document and print the normalized IR as canonical JSON."""
    ir = _parse(source, kind, allow_dir, refs_lock)
    payload: dict[str, object] = {"ir": ir.model_dump(mode="json")}
    if baseline:
        payload["baseline_plan"] = plan_baseline(ir).model_dump(mode="json")
    typer.echo(canonical_json(payload), nl=False)


@app.command()
def plan(
    source: Path = typer.Argument(..., help="Path to an OpenAPI 3.x or WSDL 1.1 document."),
    kind: SourceKind = typer.Option(SourceKind.AUTO, "--kind", help="Override format detection."),
    allow_dir: list[Path] = typer.Option([], "--allow-dir", help=ALLOW_DIR_HELP),
    refs_lock: Path | None = typer.Option(None, "--refs-lock", help=REFS_LOCK_HELP),
    planner: PlannerKind = typer.Option(PlannerKind.SEMANTIC, "--planner", help=PLANNER_HELP),
    overlay: Path | None = typer.Option(None, "--overlay", help=OVERLAY_HELP),
) -> None:
    """Print a tool plan as canonical JSON.

    Every artifact is `proposed` until a reviewer records approval in an overlay, and the
    emission gate refuses to make a write or destructive tool executable before then.
    """
    ir = _parse(source, kind, allow_dir, refs_lock)
    typer.echo(dump_canonical(_plan(ir, planner, overlay)), nl=False)


@app.command()
def generate(
    source: Path = typer.Argument(..., help="Path to an OpenAPI 3.x or WSDL 1.1 document."),
    kind: SourceKind = typer.Option(SourceKind.AUTO, "--kind", help="Override format detection."),
    allow_dir: list[Path] = typer.Option([], "--allow-dir", help=ALLOW_DIR_HELP),
    refs_lock: Path | None = typer.Option(None, "--refs-lock", help=REFS_LOCK_HELP),
    planner: PlannerKind = typer.Option(PlannerKind.SEMANTIC, "--planner", help=PLANNER_HELP),
    overlay: Path | None = typer.Option(None, "--overlay", help=OVERLAY_HELP),
    enforce_policy: bool = typer.Option(
        True, "--enforce-policy/--no-enforce-policy", help=ENFORCE_POLICY_HELP
    ),
) -> None:
    """Generate a tool surface and print it as canonical JSON.

    The surface binds to no MCP SDK and performs no I/O. A tool is emitted executable only
    when its source operation carries no blocking ambiguity, its risk is classified, and any
    write, destructive or privileged tool has been approved. Refused tools are still emitted,
    carrying the reason, so the surface stays auditable.
    """
    ir = _parse(source, kind, allow_dir, refs_lock)
    plan = _plan(ir, planner, overlay)
    manifest = synthesize_policy(ir, plan) if enforce_policy else None
    typer.echo(dump_canonical(generate_surface(ir, plan, manifest)), nl=False)


@app.command()
def report(
    source: Path = typer.Argument(..., help="Path to an OpenAPI 3.x or WSDL 1.1 document."),
    out_dir: Path = typer.Option(Path("reports"), "--out-dir", help="Directory for reports."),
    kind: SourceKind = typer.Option(SourceKind.AUTO, "--kind", help="Override format detection."),
    allow_dir: list[Path] = typer.Option([], "--allow-dir", help=ALLOW_DIR_HELP),
    refs_lock: Path | None = typer.Option(None, "--refs-lock", help=REFS_LOCK_HELP),
    planner: PlannerKind = typer.Option(PlannerKind.SEMANTIC, "--planner", help=PLANNER_HELP),
    overlay: Path | None = typer.Option(None, "--overlay", help=OVERLAY_HELP),
) -> None:
    """Write the conversion report a reviewer reads before approving anything.

    One self-contained HTML file: what was read, what is proposed, what the gate is holding,
    and what needs a decision. Reports are never overwritten, because a decision made against
    one set of proposals is not evidence about a different set, so each run writes a file named
    for the source digest it describes.
    """
    ir = _parse(source, kind, allow_dir, refs_lock)
    plan = _plan(ir, planner, overlay)
    manifest = synthesize_policy(ir, plan)
    written = write_report(out_dir, ir, plan, generate_surface(ir, plan, manifest), manifest)
    typer.echo(f"wrote {written.path}")
    typer.echo(f"  executable now: {len(written.executable)}")
    typer.echo(f"  held by the gate: {len(written.blocked)}")
    if written.awaiting_review:
        typer.echo(f"  awaiting your approval: {len(written.awaiting_review)}")
        typer.echo("  approve by class, for example:")
        typer.echo(f"    api-mcp-compiler approve {source} --risk read --overlay <path>")


@app.command("approve")
def approve_surface(
    source: Path = typer.Argument(..., help="Path to an OpenAPI 3.x or WSDL 1.1 document."),
    overlay: Path = typer.Option(..., "--overlay", help="Overlay to create or extend."),
    risk: RiskClass | None = typer.Option(
        None, "--risk", help="Approve every tool of a risk class."
    ),
    group: str | None = typer.Option(None, "--group", help="Approve every tool in a group."),
    name: list[str] = typer.Option([], "--name", help="Approve one named tool. Repeatable."),
    kind: SourceKind = typer.Option(SourceKind.AUTO, "--kind", help="Override format detection."),
    allow_dir: list[Path] = typer.Option([], "--allow-dir", help=ALLOW_DIR_HELP),
    refs_lock: Path | None = typer.Option(None, "--refs-lock", help=REFS_LOCK_HELP),
) -> None:
    """Record approval for a class of tools, writing the overlay so nobody hand-edits JSON.

    A selection must name what it covers. There is deliberately no flag that approves a whole
    surface without saying what class of thing it is.
    """
    ir = _parse(source, kind, allow_dir, refs_lock)
    existing = load_overlay(overlay) if overlay.is_file() else None
    plan = plan_semantic(ir, existing)
    try:
        outcome = approve(plan, overlay=existing, risk=risk, group=group, names=name)
    except ApprovalSelectionError as error:
        typer.echo(f"refused: {error}", err=True)
        raise typer.Exit(code=2) from error
    save_overlay(outcome.overlay, overlay)
    typer.echo(f"approved {len(outcome.approved)} artifact(s) in {overlay}")
    for item in outcome.approved:
        typer.echo(f"    + {item}")
    if outcome.already_approved:
        typer.echo(f"  already approved: {', '.join(outcome.already_approved)}")
    if outcome.untouched:
        typer.echo(f"  still awaiting a decision: {', '.join(outcome.untouched)}")


@app.command()
def serve(
    source: Path = typer.Argument(..., help="Path to an OpenAPI 3.x or WSDL 1.1 document."),
    out: Path = typer.Option(..., "--out", help="Where to write the generated server module."),
    kind: SourceKind = typer.Option(SourceKind.AUTO, "--kind", help="Override format detection."),
    allow_dir: list[Path] = typer.Option([], "--allow-dir", help=ALLOW_DIR_HELP),
    refs_lock: Path | None = typer.Option(None, "--refs-lock", help=REFS_LOCK_HELP),
    planner: PlannerKind = typer.Option(PlannerKind.SEMANTIC, "--planner", help=PLANNER_HELP),
    overlay: Path | None = typer.Option(None, "--overlay", help=OVERLAY_HELP),
) -> None:
    """Emit a runnable MCP server for the approved part of a surface.

    Only tools that cleared the emission gate are registered. Tools the gate withheld are
    named by a `surface://withheld` resource and are deliberately absent, so a deployment
    cannot pick up the tools and leave the decision behind. Policy travels with them:
    confirmation, output ceilings and redaction are written into the server rather than
    documented beside it.

    Each credential is read at call time from an environment variable named after its
    security scheme, and placed where the specification said it goes: an API key in the
    header or query parameter the service named, HTTP basic encoded, OAuth2 as a bearer
    token. The variables are reported below rather than left to be discovered.

    The generated module needs `mcp` and `httpx`, which this compiler does not depend on.
    """
    ir = _parse(source, kind, allow_dir, refs_lock)
    plan = _plan(ir, planner, overlay)
    manifest = synthesize_policy(ir, plan)
    surface = generate_surface(ir, plan, manifest)
    # A SOAP service has no routes: every operation is a POST to one endpoint and what
    # distinguishes them is the envelope, so it needs a different emitter rather than a
    # branch inside the same one.
    soap = ir.service.source_format is SourceFormat.WSDL
    if soap:
        soap_emitted = emit_soap_server(ir, surface, manifest)
        generated, registered, withheld = (
            soap_emitted.source,
            soap_emitted.registered,
            soap_emitted.withheld,
        )
        upstream = soap_emitted.endpoint
        credentials = soap_emitted.credentials
    else:
        http_emitted = emit_server(ir, surface, manifest)
        generated, registered, withheld = (
            http_emitted.source,
            http_emitted.registered,
            http_emitted.withheld,
        )
        upstream = http_emitted.base_url
        credentials = http_emitted.credentials
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(generated, encoding="utf-8")
    typer.echo(f"wrote {out} for {ir.service.service_id} ({'SOAP' if soap else 'HTTP'})")
    typer.echo(f"  registered {len(registered)}: {', '.join(registered)}")
    if withheld:
        typer.echo(f"  withheld {len(withheld)}:")
        for name, reason in sorted(withheld.items()):
            typer.echo(f"    {name}: {reason}")
    typer.echo(f"  upstream {upstream}")
    # Named here because the alternative is discovering them from 401s in production. The
    # server reads each at call time; no credential is ever written into the file.
    if credentials:
        typer.echo("  set before running:")
        for variable, scheme_id in sorted(credentials.items()):
            typer.echo(f"    {variable}  (for the {scheme_id!r} security scheme)")
    typer.echo(f"  run it with: pip install {' '.join(GENERATED_REQUIREMENTS)} && python {out}")


@app.command()
def policy(
    source: Path = typer.Argument(..., help="Path to an OpenAPI 3.x or WSDL 1.1 document."),
    kind: SourceKind = typer.Option(SourceKind.AUTO, "--kind", help="Override format detection."),
    allow_dir: list[Path] = typer.Option([], "--allow-dir", help=ALLOW_DIR_HELP),
    refs_lock: Path | None = typer.Option(None, "--refs-lock", help=REFS_LOCK_HELP),
    planner: PlannerKind = typer.Option(PlannerKind.SEMANTIC, "--planner", help=PLANNER_HELP),
    overlay: Path | None = typer.Option(None, "--overlay", help=OVERLAY_HELP),
) -> None:
    """Print the governance manifest for a planned surface as canonical JSON.

    Policy is derived separately from code generation. Anything that cannot be derived is
    named in `unresolved`, and generation then refuses the tool rather than defaulting it.
    """
    ir = _parse(source, kind, allow_dir, refs_lock)
    typer.echo(dump_canonical(synthesize_policy(ir, _plan(ir, planner, overlay))), nl=False)


@app.command()
def review(
    source: Path = typer.Argument(..., help="Path to an OpenAPI 3.x or WSDL 1.1 document."),
    kind: SourceKind = typer.Option(SourceKind.AUTO, "--kind", help="Override format detection."),
    allow_dir: list[Path] = typer.Option([], "--allow-dir", help=ALLOW_DIR_HELP),
    refs_lock: Path | None = typer.Option(None, "--refs-lock", help=REFS_LOCK_HELP),
    overlay: Path | None = typer.Option(None, "--overlay", help=OVERLAY_HELP),
) -> None:
    """Print the human review report for the semantic plan.

    This is the artifact the approval gate depends on: every proposed rename, omission,
    grouping, projection and composite, with its rationale and confidence.
    """
    ir = _parse(source, kind, allow_dir, refs_lock)
    typer.echo(review_report(ir, plan_semantic(ir, load_overlay(overlay) if overlay else None)))


@app.command("overlay-restamp")
def overlay_restamp(
    source: Path = typer.Argument(..., help="Path to the specification the overlay describes."),
    overlay: Path = typer.Argument(..., help="Overlay to re-stamp in place."),
    kind: SourceKind = typer.Option(SourceKind.AUTO, "--kind", help="Override format detection."),
    allow_dir: list[Path] = typer.Option([], "--allow-dir", help=ALLOW_DIR_HELP),
    refs_lock: Path | None = typer.Option(None, "--refs-lock", help=REFS_LOCK_HELP),
) -> None:
    """Bind an overlay to the current specification revision.

    Run this only after re-reading the decisions against the changed specification. The
    digest is what stops an approval granted for one revision from applying to another, so
    re-stamping without reviewing defeats the mechanism it exists to provide.
    """
    ir = _parse(source, kind, allow_dir, refs_lock)
    current = load_overlay(overlay)
    if current.source_digest == ir.service.source_digest:
        typer.echo(f"{overlay}: already bound to {ir.service.source_digest}.")
        return
    save_overlay(restamp(current, ir.service.source_digest), overlay)
    typer.echo(
        f"{overlay}: re-stamped from {current.source_digest} to {ir.service.source_digest}. "
        "Confirm the recorded decisions still hold."
    )


@app.command()
def evaluate(
    source: Path = typer.Argument(..., help="Path to an OpenAPI 3.x or WSDL 1.1 document."),
    corpus: Path = typer.Argument(..., help="Path to an evaluation corpus."),
    kind: SourceKind = typer.Option(SourceKind.AUTO, "--kind", help="Override format detection."),
    allow_dir: list[Path] = typer.Option([], "--allow-dir", help=ALLOW_DIR_HELP),
    refs_lock: Path | None = typer.Option(None, "--refs-lock", help=REFS_LOCK_HELP),
    planner: PlannerKind = typer.Option(PlannerKind.SEMANTIC, "--planner", help=PLANNER_HELP),
    overlay: Path | None = typer.Option(None, "--overlay", help=OVERLAY_HELP),
    enforce_policy: bool = typer.Option(
        True, "--enforce-policy/--no-enforce-policy", help=ENFORCE_POLICY_HELP
    ),
) -> None:
    """Run an evaluation corpus against a generated surface and print the result.

    The only driver available replays the reference solution each task records. It is correct
    by construction and therefore scores every surface identically, which makes it useful for
    checking that the harness agrees with itself and useless for comparing surfaces. No
    output of this command is evidence that one planner outperforms another.
    """
    ir = _parse(source, kind, allow_dir, refs_lock)
    plan = _plan(ir, planner, overlay)
    manifest = synthesize_policy(ir, plan) if enforce_policy else None
    loaded = EvalCorpus.model_validate(json.loads(corpus.read_text(encoding="utf-8")))
    run = run_corpus(loaded, ir, generate_surface(ir, plan, manifest), manifest)
    typer.echo(dump_canonical(run), nl=False)


@app.command()
def vendor_refs(
    source: Path = typer.Argument(..., help="Path to an OpenAPI 3.x document."),
    lock: Path = typer.Option(..., "--lock", help="Where to write the reference lock."),
    cache: Path | None = typer.Option(
        None, "--cache", help="Where to write fetched documents. Defaults to beside the lock."
    ),
    record: bool = typer.Option(
        False,
        "--record",
        help="Trust references not already in the lock, on first use. Without this, an "
        "unrecorded reference is refused before anything is fetched.",
    ),
) -> None:
    """Fetch the remote references a specification names, and pin them by digest.

    This is the only command that reaches the network, and it exists so that ingestion never
    has to. It fetches over HTTPS with certificate verification, writes nothing until bytes
    verify, and records a lock naming each URL, the digest of what it served and the file the
    bytes went into.

    Commit the lock and the cache. A compile then needs neither the network nor the clock: it
    reads the pinned files and refuses any whose bytes have changed, so an upstream edit
    surfaces as a mismatch rather than as a surface that quietly became something else.
    """
    document, digest = load_document(source)
    if not isinstance(document, dict):
        typer.echo(f"{source}: OpenAPI document must be a mapping at the root", err=True)
        raise typer.Exit(code=1)

    wanted = remote_references(document)
    if not wanted:
        typer.echo(f"{source} names no remote references; nothing to vendor.")
        return

    existing = load_lock(lock) if lock.is_file() else None
    destination = cache or lock.parent / "refs"
    try:
        written, fetched, unchanged = vendor(
            document, digest, lock, destination, record=record, existing=existing
        )
    except VendoringError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error

    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(dump_canonical(written), encoding="utf-8")
    typer.echo(f"wrote {lock} for {source}")
    for url in fetched:
        typer.echo(f"  fetched   {url}")
    for url in unchanged:
        typer.echo(f"  unchanged {url}")
    typer.echo(f"  cache {destination}")


@app.command()
def validate(
    source: Path = typer.Argument(..., help="Path to an OpenAPI 3.x or WSDL 1.1 document."),
    kind: SourceKind = typer.Option(SourceKind.AUTO, "--kind", help="Override format detection."),
    allow_dir: list[Path] = typer.Option([], "--allow-dir", help=ALLOW_DIR_HELP),
    refs_lock: Path | None = typer.Option(None, "--refs-lock", help=REFS_LOCK_HELP),
) -> None:
    """Validate the IR and baseline plan against their schemas and report ambiguities.

    Exits non-zero when a contract is violated. Blocking ambiguities are reported but do not
    fail the command: they are the work queue for later phases, not defects in this one.
    """
    ir = _parse(source, kind, allow_dir, refs_lock)
    baseline = plan_baseline(ir)
    try:
        validate_ir(ir.model_dump(mode="json"), label=f"IR for {source}")
        validate_tool_plan(baseline.model_dump(mode="json"), label=f"baseline plan for {source}")
        manifest = synthesize_policy(ir, baseline)
        surface = generate_surface(ir, baseline, manifest)
        validate_tool_surface(
            surface.model_dump(mode="json"), label=f"tool surface for {source}"
        )
    except ContractViolation as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error

    blocking = ir.blocking_ambiguities
    typer.echo(
        f"{source}: {len(ir.operations)} operations, {len(baseline.artifacts)} baseline artifacts, "
        f"{len(surface.executable_tools)}/{len(surface.tools)} executable, "
        f"{len(ir.ambiguities)} ambiguities ({len(blocking)} blocking). Contracts valid."
    )
    for tool in surface.tools:
        if tool.blockers:
            reasons = ", ".join(item.value for item in tool.blockers)
            typer.echo(f"  DISABLED {tool.name}: {reasons}")
    for item in ir.ambiguities:
        marker = "BLOCKING" if item.blocking else "note    "
        typer.echo(f"  {marker} {item.code} at {item.field}: {item.detail}")


if __name__ == "__main__":
    app()
