"""Hold the documentation to the repository's house conventions.

Two checks, both cheap and both catching things review reliably misses.

**Prose is plain ASCII.** Typographic dashes, smart quotes and ellipsis characters render
inconsistently across terminals, break naive search, and turn a one-word edit into a diff
nobody can read. Ordinary punctuation says the same thing. Anything else is reported with a
line and column so it can be fixed without hunting.

There is deliberately no allowlist. Nothing in the repository needs one today, and an
allowlist written for a need nobody has is configuration for behaviour that does not exist. A
character that earns its place can be admitted then, with the reason recorded beside it.

**The command reference matches the CLI.** A documented command that no longer exists, or a
command nobody documented, is the most common way a reference page rots. Both directions are
checked against the Typer application itself rather than against a list maintained here.

Pre-registrations are deliberately exempt from the prose rule. Each is digested and recorded
runs reference that digest, so editing one, even to change punctuation, would detach a result
from the hypothesis it was produced under.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from api_mcp_compiler.cli import app  # noqa: E402

#: Checked for plain ASCII. Directories are walked; files are taken as they are.
PROSE: tuple[str, ...] = (
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SOURCES.md",
    "CITATION.cff",
    "guide",
    "src",
    "scripts",
    "tests",
    "notebooks",
)

#: Not checked, and why.
EXEMPT = (
    "preregistrations",  # digested records; a punctuation edit would void a result
    "examples/benchmarks",  # third-party documents, fetched rather than authored
    # Test data, not prose. This module feeds non-ASCII service titles to the slug rule
    # precisely to prove the slug comes out ASCII, so the input has to stay as it is.
    "tests/test_provenance.py",
)

CLI_PAGE = REPO_ROOT / "guide" / "cli.md"


def _files() -> list[Path]:
    """Every file the prose rule applies to."""
    found: list[Path] = []
    for name in PROSE:
        target = REPO_ROOT / name
        if target.is_file():
            found.append(target)
        elif target.is_dir():
            found.extend(
                path
                for path in sorted(target.rglob("*"))
                if path.is_file()
                and path.suffix in {".md", ".py", ".cff", ".ipynb", ".txt"}
                and "__pycache__" not in path.parts
            )
    return [
        path
        for path in found
        if not any(str(path.relative_to(REPO_ROOT)).startswith(part) for part in EXEMPT)
    ]


def _check_prose(failures: list[str]) -> int:
    """Report every non-ASCII character outside the allowlist."""
    checked = 0
    for path in _files():
        checked += 1
        relative = path.relative_to(REPO_ROOT)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for column, character in enumerate(line, start=1):
                if character.isascii():
                    continue
                failures.append(
                    f"{relative}:{number}:{column}: {character!r} "
                    f"(U+{ord(character):04X}) is not plain ASCII"
                )
    return checked


def _check_cli_reference(failures: list[str]) -> int:
    """Check the command reference against the application it documents."""
    from typer.main import get_command

    commands = set(get_command(app).commands)  # type: ignore[attr-defined]
    if not CLI_PAGE.is_file():
        failures.append(f"{CLI_PAGE.relative_to(REPO_ROOT)} is missing")
        return 0

    documented = set(re.findall(r"^## ([a-z][a-z-]*)$", CLI_PAGE.read_text(encoding="utf-8"), re.M))
    for name in sorted(commands - documented):
        failures.append(f"guide/cli.md does not document the {name!r} command")
    for name in sorted(documented - commands):
        failures.append(f"guide/cli.md documents {name!r}, which the CLI does not provide")
    return len(commands)


def main() -> int:
    """Run both checks and report everything that failed."""
    failures: list[str] = []
    checked = _check_prose(failures)
    commands = _check_cli_reference(failures)

    if failures:
        print(f"{len(failures)} documentation issue(s):", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"Checked {checked} files for plain ASCII prose and {commands} documented commands.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
