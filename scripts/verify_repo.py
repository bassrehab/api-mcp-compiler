"""Complete verification gate for the repository.

Every check runs even when an earlier one fails, so a single run reports the full picture
rather than whichever check happened to be listed first. The command exits non-zero if any
check failed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Decision: `scripts` is included in the lint and type targets. The
# verification script itself was previously unchecked, which meant the gate could not
# detect a defect in the gate.
COMMANDS: list[list[str]] = [
    [sys.executable, "-m", "pytest", "-q"],
    [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"],
    [sys.executable, "-m", "mypy", "src", "scripts"],
    [sys.executable, str(REPO_ROOT / "scripts" / "validate_examples.py")],
    # Decision: the packaging check runs in the gate rather than at release time. It is the
    # only check here that exercises the built distribution instead of the source tree, and
    # the defect it catches — a wheel with no contracts in it — is invisible to every other
    # check and visible to every user.
    [sys.executable, str(REPO_ROOT / "scripts" / "check_packaging.py")],
]


def main() -> int:
    """Run every check and return a non-zero code if any of them failed."""
    failed: list[str] = []
    for command in COMMANDS:
        printable = " ".join(command)
        print(f"+ {printable}", flush=True)
        result = subprocess.run(command, check=False, cwd=REPO_ROOT)
        if result.returncode:
            failed.append(printable)
        print(flush=True)

    if failed:
        print(f"{len(failed)} check(s) failed:", file=sys.stderr)
        for name in failed:
            print(f"  - {name}", file=sys.stderr)
        return 1
    print("All repository checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
