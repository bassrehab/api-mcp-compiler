"""Fetch and verify the third-party documents a benchmark depends on.

    python scripts/fetch_benchmark.py            # verify and fetch recorded sources
    python scripts/fetch_benchmark.py --record   # trust a new source on first use

Nothing fetched here is committed. The manifest records where each document came from and
what its bytes must hash to, which is what keeps a result reconstructible without this
repository redistributing anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from api_mcp_compiler.benchmarks import (
    BenchmarkFetchError,
    fetch_source,
    load_manifest,
    save_manifest,
)
from api_mcp_compiler.models import BenchmarkManifest

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = REPO_ROOT / "examples" / "benchmarks"
MANIFEST = BENCHMARK_ROOT / "manifest.json"


def main() -> int:
    """Fetch every source in the manifest, verifying each against its recorded digest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--record",
        action="store_true",
        help="Trust a source with no recorded digest on first use and write the digest back.",
    )
    arguments = parser.parse_args()

    manifest = load_manifest(MANIFEST)
    updated = []
    failures: list[str] = []
    for source in manifest.sources:
        try:
            outcome, resolved = fetch_source(source, BENCHMARK_ROOT, record=arguments.record)
        except BenchmarkFetchError as error:
            failures.append(f"{source.source_id}: {error}")
            updated.append(source)
            continue
        updated.append(resolved)
        state = "already verified" if outcome.skipped else "fetched"
        note = " and recorded" if outcome.recorded and not outcome.skipped else ""
        print(f"  {state}{note}: {outcome.source_id} -> {outcome.target.name} [{outcome.digest}]")

    if arguments.record:
        save_manifest(BenchmarkManifest(sources=updated), MANIFEST)

    if failures:
        print(f"\n{len(failures)} source(s) failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"{len(manifest.sources)} source(s) verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
