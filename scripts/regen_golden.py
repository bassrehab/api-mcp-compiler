"""Regenerate the committed golden IR and baseline plan artifacts.

Golden artifacts are the evidence that compilation is reproducible from a versioned source
specification. Run this deliberately after an intended contract or parser change,
then review the diff: an unexplained diff means the change was not the one intended.

    python scripts/regen_golden.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from api_mcp_compiler.codegen.tools import generate_surface
from api_mcp_compiler.contracts import dump_canonical
from api_mcp_compiler.evaluation.harness import run_corpus
from api_mcp_compiler.ingest.openapi import parse_openapi
from api_mcp_compiler.ingest.wsdl import parse_wsdl
from api_mcp_compiler.models import EvalCorpus
from api_mcp_compiler.planning.baseline import plan_baseline
from api_mcp_compiler.planning.overlay import load_overlay
from api_mcp_compiler.planning.report import review_report
from api_mcp_compiler.planning.semantic import plan_semantic
from api_mcp_compiler.policy.synthesis import synthesize_policy

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"

# Repository-relative, because the path is recorded in the artifact as `source_uri`.
EXAMPLES = (
    "examples/openapi/order_service.yaml",
    "examples/openapi/inventory_service.yaml",
    "examples/wsdl/customer_service.wsdl",
)


def golden_stem(example: str) -> str:
    """Return the golden file stem for an example path."""
    return Path(example).stem


def main() -> int:
    """Write one IR and one baseline plan golden artifact per example."""
    os.chdir(REPO_ROOT)
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for example in EXAMPLES:
        source = Path(example)
        ir = parse_wsdl(source) if source.suffix.lower() == ".wsdl" else parse_openapi(source)
        stem = golden_stem(example)
        (GOLDEN_DIR / f"{stem}.ir.json").write_text(dump_canonical(ir), encoding="utf-8")
        plan = plan_baseline(ir)
        (GOLDEN_DIR / f"{stem}.plan.json").write_text(dump_canonical(plan), encoding="utf-8")
        (GOLDEN_DIR / f"{stem}.surface.json").write_text(
            dump_canonical(generate_surface(ir, plan)), encoding="utf-8"
        )
        overlay_path = REPO_ROOT / "examples" / "overlays" / f"{stem}.overlay.json"
        overlay = load_overlay(overlay_path) if overlay_path.is_file() else None
        semantic = plan_semantic(ir, overlay)
        manifest = synthesize_policy(ir, semantic)
        (GOLDEN_DIR / f"{stem}.semantic-plan.json").write_text(
            dump_canonical(semantic), encoding="utf-8"
        )
        (GOLDEN_DIR / f"{stem}.policy.json").write_text(
            dump_canonical(manifest), encoding="utf-8"
        )
        (GOLDEN_DIR / f"{stem}.semantic-surface.json").write_text(
            dump_canonical(generate_surface(ir, semantic, manifest)), encoding="utf-8"
        )
        (GOLDEN_DIR / f"{stem}.review.md").write_text(
            review_report(ir, semantic), encoding="utf-8"
        )
        corpus_path = REPO_ROOT / "examples" / "evals" / f"{stem.split('_')[0]}_tasks.json"
        if corpus_path.is_file():
            corpus = EvalCorpus.model_validate(
                json.loads(corpus_path.read_text(encoding="utf-8"))
            )
            run = run_corpus(
                corpus, ir, generate_surface(ir, semantic, manifest), manifest
            )
            (GOLDEN_DIR / f"{stem}.evaluation.json").write_text(
                dump_canonical(run), encoding="utf-8"
            )
        print(f"  wrote artifacts for {stem}")
    relative = GOLDEN_DIR.relative_to(REPO_ROOT)
    count = len(list(GOLDEN_DIR.glob("*")))
    print(f"Regenerated {count} golden artifacts in {relative}.")

    # The notebook prints the same artifacts, so a change that moves a golden file moves the
    # notebook too. Refreshing both from one command means the diff to review is complete.
    return int(
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check_notebook.py"), "--write"],
            check=False,
            cwd=REPO_ROOT,
        ).returncode
    )


if __name__ == "__main__":
    raise SystemExit(main())
