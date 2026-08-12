"""Shared test fixtures.

Example specifications are addressed by repository-relative path, and the resulting
`source_uri` is recorded in the IR and in the golden artifacts. The working directory is
therefore pinned to the repository root for the whole session, so that a golden comparison
cannot pass or fail depending on where pytest happened to be invoked from.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"

ORDER_SERVICE = "examples/openapi/order_service.yaml"
INVENTORY_SERVICE = "examples/openapi/inventory_service.yaml"
CUSTOMER_SERVICE = "examples/wsdl/customer_service.wsdl"

OPENAPI_EXAMPLES = (ORDER_SERVICE, INVENTORY_SERVICE)
WSDL_EXAMPLES = (CUSTOMER_SERVICE,)
ALL_EXAMPLES = (*OPENAPI_EXAMPLES, *WSDL_EXAMPLES)


@pytest.fixture(autouse=True, scope="session")
def _run_from_repo_root() -> Iterator[None]:
    """Pin the working directory to the repository root for the whole test session."""
    previous = Path.cwd()
    os.chdir(REPO_ROOT)
    try:
        yield
    finally:
        os.chdir(previous)
