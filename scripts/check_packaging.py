"""Prove that the built distribution can validate its own artifacts.

A wheel that ships the code but not the contract schemas installs cleanly, imports cleanly,
and then fails on the first artifact anyone asks it to validate. That failure appears only
after publication, on a machine with no repository checkout, which is the worst place for it
to appear and the reason this check exists rather than a note in the release instructions.

Running the tests is not enough to catch it. The tests import the package from the source
tree, where the schemas are present whether or not the packaging declares them, so the
in-repository suite passes on a distribution that is broken for every user of it. This check
therefore builds the real wheel, reads the archive, unpacks it somewhere else and validates
an artifact from outside the source tree entirely.

Nothing here reaches the network: the build runs without isolation, against the tooling that
is already installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import api_mcp_compiler.contracts as contracts  # noqa: E402

#: Where the wheel has to carry the contracts.
PACKAGE_DATA_DIR = "api_mcp_compiler/schemas"

#: What the wheel has to carry, read from the constants the module declares rather than from
#: the files on disk. Globbing the directory looked equivalent and was not: deleting a schema
#: made the expectation shrink to match, and this check reported success on a distribution
#: missing a contract. The declared set cannot shrink by accident.
DECLARED = sorted(
    value
    for name, value in vars(contracts).items()
    if name.endswith("_SCHEMA") and isinstance(value, str)
)

#: Validated by the unpacked wheel. Any artifact would do; this one is a golden file, so a
#: change that made it invalid would already have failed a test elsewhere.
SAMPLE_IR = REPO_ROOT / "tests" / "golden" / "customer_service.ir.json"

# The program run against the unpacked wheel. It asserts which copy of the package it
# imported before asserting anything about behaviour, because a check that silently fell
# back to the source tree would pass forever while shipping a broken distribution.
DRIVER = """
import json, pathlib, sys

import api_mcp_compiler
from api_mcp_compiler.contracts import ContractViolation, load_schema, validate_ir

unpacked, artifact = pathlib.Path(sys.argv[1]).resolve(), pathlib.Path(sys.argv[2])
imported = pathlib.Path(api_mcp_compiler.__file__).resolve().parent
if imported != unpacked / "api_mcp_compiler":
    raise SystemExit(f"imported {imported}, which is not the unpacked wheel at {unpacked}")

for name in sys.argv[3:]:
    load_schema(name)

payload = json.loads(artifact.read_text(encoding="utf-8"))
validate_ir(payload)

# Loading and accepting proves nothing on its own: a validator that accepted anything would
# also get this far. Take away a required field and require the rejection.
del payload["schema_version"]
try:
    validate_ir(payload)
except ContractViolation:
    pass
else:
    raise SystemExit("the installed package accepted an IR with no schema_version")
"""


def _stage(destination: Path) -> Path:
    """Copy the build inputs somewhere clean, and return the staged root.

    Building in place is not safe for this check. setuptools keeps a `build/` tree and
    reuses what is already in it, so a wheel can be assembled from a previous run's copy of
    files the current configuration no longer declares. That is not hypothetical: the first
    version of this script built in place, and it reported a correctly packaged wheel from a
    pyproject with the package-data declaration deleted.

    Only the inputs the build reads are copied. If a new one is added to the project and not
    to this list, the build fails and names the missing file, which is a loud failure rather
    than a quiet one.
    """
    staged = destination / "source"
    staged.mkdir()
    for name in ("pyproject.toml", "LICENSE", "NOTICE", "README.md"):
        source = REPO_ROOT / name
        if source.is_file():
            shutil.copy2(source, staged / name)
    shutil.copytree(
        REPO_ROOT / "src",
        staged / "src",
        ignore=shutil.ignore_patterns("*.egg-info", "__pycache__", "*.pyc"),
    )
    return staged


def _build_wheel(staged: Path, destination: Path) -> Path:
    """Build the wheel from a staged copy of the source and return its path."""
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel",
            "--no-deps", "--no-build-isolation",
            "--wheel-dir", str(destination), str(staged),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=staged,
    )
    if result.returncode:
        raise SystemExit(f"building the wheel failed:\n{result.stdout}\n{result.stderr}")
    wheels = sorted(destination.glob("api_mcp_compiler-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one wheel in {destination}, found {len(wheels)}")
    return wheels[0]


def _build_sdist(staged: Path, destination: Path) -> Path:
    """Build the source distribution from the same staged copy, and return its path.

    The wheel is what most installations use, but `pip install --no-binary` and every
    downstream packager build from the sdist, and it draws its data files from the same
    declaration. Checking one and publishing both would leave half the release unverified.
    """
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; from setuptools import build_meta;"
            " print(build_meta.build_sdist(sys.argv[1]))",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=staged,
    )
    if result.returncode:
        raise SystemExit(f"building the sdist failed:\n{result.stdout}\n{result.stderr}")
    archives = sorted(destination.glob("api_mcp_compiler-*.tar.gz"))
    if len(archives) != 1:
        raise SystemExit(f"expected exactly one sdist in {destination}, found {len(archives)}")
    return archives[0]


def _check_sdist(sdist: Path, expected: list[str], failures: list[str]) -> None:
    """Check that the source distribution carries the contracts as well."""
    with tarfile.open(sdist) as archive:
        # The leading directory carries the version, which is not what is being checked here.
        present = {name.split("/src/", 1)[-1] for name in archive.getnames() if "/src/" in name}
    for name in expected:
        if f"{PACKAGE_DATA_DIR}/{name}" not in present:
            failures.append(f"{sdist.name} does not contain src/{PACKAGE_DATA_DIR}/{name}")


def _check_archive(wheel: Path, expected: list[str], failures: list[str]) -> None:
    """Check the archive itself, before anything unpacks or imports it."""
    with zipfile.ZipFile(wheel) as archive:
        present = set(archive.namelist())
    for name in expected:
        if f"{PACKAGE_DATA_DIR}/{name}" not in present:
            failures.append(f"{wheel.name} does not contain {PACKAGE_DATA_DIR}/{name}")


def _check_unpacked(wheel: Path, expected: list[str], failures: list[str]) -> None:
    """Unpack the wheel elsewhere and make it validate an artifact from outside the tree."""
    with tempfile.TemporaryDirectory() as raw:
        unpacked = Path(raw) / "site"
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(unpacked)

        environment = dict(os.environ)
        # An override would let this check pass by pointing at the repository, which is
        # precisely the situation it exists to detect.
        environment.pop(contracts.SCHEMA_DIR_ENV_VAR, None)
        environment["PYTHONPATH"] = str(unpacked)

        result = subprocess.run(
            [sys.executable, "-c", DRIVER, str(unpacked), str(SAMPLE_IR), *expected],
            check=False,
            capture_output=True,
            text=True,
            # Anywhere but the repository: a relative path that resolved against the source
            # tree would make the whole check meaningless.
            cwd=raw,
            env=environment,
        )
        if result.returncode:
            failures.append(
                "the unpacked wheel could not validate an artifact:\n"
                f"{result.stdout}{result.stderr}".rstrip()
            )


def main() -> int:
    """Build the distribution and verify that it carries its own contracts."""
    expected = DECLARED
    if not expected:
        print("api_mcp_compiler.contracts declares no schemas", file=sys.stderr)
        return 1

    failures: list[str] = []
    # A file nobody names ships unvalidated and unloadable; catch it before the build so the
    # report names the file rather than the wheel.
    on_disk = sorted(path.name for path in contracts.schema_dir().glob("*.schema.json"))
    failures.extend(
        f"{name} is in the package but no constant names it" for name in on_disk
        if name not in expected
    )

    with tempfile.TemporaryDirectory() as raw:
        destination = Path(raw)
        staged = _stage(destination)
        _check_archive(_build_wheel(staged, destination), expected, failures)
        _check_sdist(_build_sdist(staged, destination), expected, failures)
        # Only worth unpacking if the files are in there; otherwise the second failure is
        # just the first one restated.
        if not failures:
            _check_unpacked(next(destination.glob("*.whl")), expected, failures)

    if failures:
        print(f"{len(failures)} packaging check(s) failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(
        f"Wheel and sdist ship {len(expected)} contract schemas; "
        "the unpacked wheel validates an artifact with no source tree present."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
