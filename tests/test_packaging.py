"""Tests that the contracts travel with the code rather than with the repository.

The schemas were kept at the repository root for most of this project's life. Everything
passed, because every test imports the package from the source tree where the schemas sit
two directories up whether or not the packaging mentions them — and the wheel shipped none
of them. An installed copy could not validate a single artifact it produced.

These tests fix the location. They cannot prove the built distribution is correct, since
they run against the source tree like everything else; `scripts/check_packaging.py` builds
the wheel and proves that, and the verification gate runs it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import api_mcp_compiler
from api_mcp_compiler import contracts
from api_mcp_compiler.contracts import ContractViolation, load_schema, schema_dir

PACKAGE_ROOT = Path(api_mcp_compiler.__file__).resolve().parent

#: Every schema the module names, read from the module so that adding a constant without a
#: file — or a file without a constant — fails here rather than at a user's first validation.
DECLARED = sorted(
    value
    for name, value in vars(contracts).items()
    if name.endswith("_SCHEMA") and isinstance(value, str)
)


def test_schemas_are_declared() -> None:
    """The constants exist at all; an empty set would make the rest of this module vacuous."""
    assert len(DECLARED) == 8


def test_schema_directory_lives_inside_the_package() -> None:
    """Anywhere else and the wheel cannot carry it."""
    assert schema_dir() == PACKAGE_ROOT / "schemas"


@pytest.mark.parametrize("name", DECLARED)
def test_every_declared_schema_is_present(name: str) -> None:
    """Each named schema is a real file in the package, and loads."""
    assert (schema_dir() / name).is_file()
    assert load_schema(name)["$id"]


def test_no_schema_file_is_undeclared() -> None:
    """A schema on disk that nothing names would ship without ever being validated."""
    on_disk = sorted(path.name for path in schema_dir().glob("*.schema.json"))
    assert on_disk == DECLARED


def test_no_stale_copy_remains_at_the_repository_root() -> None:
    """Two copies of a contract is one contract too many, and the stale one wins silently."""
    assert not (PACKAGE_ROOT.parents[1] / "schemas").exists()


def test_override_redirects_the_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The override is the only escape hatch left, so it has to work."""
    monkeypatch.setenv(contracts.SCHEMA_DIR_ENV_VAR, str(tmp_path))
    assert schema_dir() == tmp_path


def test_a_missing_schema_names_the_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure has to say where it looked, or it is unfixable from the message alone."""
    monkeypatch.setenv(contracts.SCHEMA_DIR_ENV_VAR, str(tmp_path))
    with pytest.raises(ContractViolation) as failure:
        load_schema(DECLARED[0])
    assert str(tmp_path) in str(failure.value)
    assert contracts.SCHEMA_DIR_ENV_VAR in str(failure.value)
