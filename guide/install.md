# Install

## From source

```bash
git clone https://github.com/bassrehab/api-mcp-compiler.git
cd api-mcp-compiler
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Python 3.11 or newer. The runtime dependencies are `pydantic`, `typer`, `PyYAML`, `lxml` and
`jsonschema`. Nothing in the compiler binds to an MCP SDK, calls a model, or reaches the
network; the generated server is the only artifact that talks to anything.

## Verify the checkout

```bash
.venv/bin/python scripts/verify_repo.py
```

The gate runs, in order:

| Check | What it proves |
|---|---|
| `pytest -q` | The suite passes. |
| `ruff check` | Lint, with the rule set pinned explicitly rather than left to defaults. |
| `mypy --strict` | Types hold across sources and scripts. |
| `validate_examples.py` | Every committed example validates against its versioned schema. |
| `check_packaging.py` | The built wheel and sdist carry the contract schemas, and an unpacked wheel validates an artifact with no source tree present. |
| `check_notebook.py` | The notebook still produces the outputs stored in it. |

Lint and type-checker versions are pinned exactly. An unpinned `ruff>=0.5` silently changes
which rules run when a new release lands, which would make the gate mean something different
on every machine.

## Generated servers

A server this compiler emits imports the MCP Python SDK and an HTTP client, which are
dependencies of the emitted artifact rather than of the compiler:

```bash
python -m pip install mcp httpx
```

`serve` prints the exact requirement list for what it generated.

## Building the documentation

```bash
.venv/bin/python -m pip install '.[docs]'
.venv/bin/mkdocs serve
```

The published site is built from `guide/` by the `Docs` workflow on every push to `main`.
