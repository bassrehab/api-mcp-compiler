"""Report how often the evaluation store's effect model is guessing, over a real document.

The store derives what a call does from the route and the side-effect class. That is a
convention approximation, and the honest question about an approximation is not whether it is
correct but how much of a given surface it is confident about. Nothing could answer that
before: every derived effect looked the same whether it came from a rule that always holds or
one that frequently does not.

Point this at a specification and it prints the distribution of rules and the operations each
one covers, so the low-confidence cases can be read rather than assumed away. A task that
disagrees with the model can state its expectations directly, which is the intended escape
hatch and only useful if someone knows which operations to look at.

    python scripts/effect_coverage.py examples/openapi/order_service.yaml
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from api_mcp_compiler.evaluation.state import derive_effect  # noqa: E402
from api_mcp_compiler.ingest.openapi import parse_openapi  # noqa: E402
from api_mcp_compiler.ingest.refs import RefPolicy  # noqa: E402

#: Below this, the model is guessing enough that a task asserting against it should say so.
UNCERTAIN = 0.8


def main() -> int:
    """Print the effect-model distribution for one specification."""
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    allowed = tuple(Path(item) for item in sys.argv[2:])
    ir = parse_openapi(path, policy=RefPolicy(allowed_directories=allowed))

    by_basis: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for operation in ir.operations:
        effect = derive_effect(operation)
        by_basis[effect.basis].append(
            (operation.operation_id, effect.kind.value, effect.confidence)
        )

    total = len(ir.operations)
    uncertain = sum(
        len(items) for basis, items in by_basis.items() if items and items[0][2] < UNCERTAIN
    )
    print(f"{ir.service.title}: {total} operations")
    for basis in sorted(by_basis, key=lambda name: (-len(by_basis[name]), name)):
        items = by_basis[basis]
        share = 100 * len(items) / total if total else 0
        print(f"\n  {basis}  ({len(items)}, {share:.0f}%, confidence {items[0][2]})")
        for operation_id, kind, _ in sorted(items):
            print(f"    {operation_id:34} -> {kind}")

    print(
        f"\n{uncertain} of {total} operations are modelled below {UNCERTAIN} confidence. "
        "A task whose success depends on one of those should state its expectation directly "
        "rather than rely on the model."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
