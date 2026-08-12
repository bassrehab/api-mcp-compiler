<div align="center">

# api-mcp-compiler

**Compile REST/OpenAPI and SOAP/WSDL services into agent-ready MCP tool surfaces**
**that are semantically designed, policy-governed, and evaluation-backed.**

[![PyPI](https://img.shields.io/pypi/v/api-mcp-compiler.svg)](https://pypi.org/project/api-mcp-compiler/)
[![CI](https://github.com/bassrehab/api-mcp-compiler/actions/workflows/ci.yml/badge.svg)](https://github.com/bassrehab/api-mcp-compiler/actions/workflows/ci.yml)
[![Licence](https://img.shields.io/badge/licence-Apache--2.0-blue.svg)](https://github.com/bassrehab/api-mcp-compiler/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/downloads/)
[![Typed](https://img.shields.io/badge/mypy-strict-blue.svg)](https://mypy.readthedocs.io/en/stable/)
[![Contracts](https://img.shields.io/badge/contracts-JSON%20Schema%202020--12-blue.svg)](https://github.com/bassrehab/api-mcp-compiler/tree/main/src/api_mcp_compiler/schemas)
[![Results](https://img.shields.io/badge/results-pre--registered-8A2BE2.svg)](https://github.com/bassrehab/api-mcp-compiler/tree/main/preregistrations)

[Documentation](https://api-mcp.subhadipmitra.com/) &middot;
[Notebook walkthrough](https://github.com/bassrehab/api-mcp-compiler/blob/main/notebooks/from_specification_to_server.ipynb) &middot;
[Pre-registrations](https://github.com/bassrehab/api-mcp-compiler/tree/main/preregistrations) &middot;
[Changelog](https://github.com/bassrehab/api-mcp-compiler/blob/main/CHANGELOG.md)

</div>

---

Turning one API operation into one MCP tool is already commodity. The harder and more useful
problem is deciding **which** tools should exist, what they should be called, what they should
accept and return, and what may not be invoked without a human in the loop.

This compiler answers that with provenance on every field, a safety gate that refuses to emit
what it cannot justify, and governance derived separately from code.

```
specification -> ingestion -> API Semantic IR -> planner -> tool plan
                                                    |
                              overlay (human decisions)
                                                    |
                                              policy manifest
                                                    |
                                          generated tool surface
                                                    |
                                    runnable MCP server (HTTP or SOAP)
```

## Contents

- [What it does](#what-it-does)
- [What is not demonstrated](#what-is-not-demonstrated)
- [Install](#install)
- [Try it](#try-it)
- [How it works](#how-it-works)
- [Design properties](#design-properties)
- [Reproducibility](#reproducibility)
- [Documentation](#documentation)
- [Scope](#scope)
- [Author](#author)
- [Licence](#licence)

## What it does

Ingests **OpenAPI 3.x, Swagger 2.0 and WSDL 1.1**, normalizes them into one provider-independent
intermediate representation, plans a tool surface, derives a policy manifest, and emits a
runnable MCP server for the approved part: over HTTP for OpenAPI, over SOAP envelopes for WSDL.
The compiler itself binds to no MCP SDK, never calls a model, and never reaches the network.

This has been exercised on specifications and services written by other people, which is where
the interesting defects live:

| Exercised against | Result |
|---|---|
| Spotify Web API, 40 operations (RestBench) | Parsed with no blocking ambiguities; least-privilege scopes derived from a real OAuth2 surface |
| Swagger 2.0 Petstore, 20 operations | Parsed, 13 tools executable, runnable server emitted |
| 40 WSDL documents (SAWSDL collection) | Parsed, with what cannot be translated recorded rather than approximated |
| Two live public SOAP services | Real calls made and answered through generated servers |

The governance is the part worth having. Every field carries provenance, judgement is proposed
rather than applied, write and destructive tools are not emitted in executable form until a
human approves them by class, and what the compiler cannot demonstrate it records as
unresolved instead of assuming.

## What is not demonstrated

The design thesis, that a semantically planned surface beats one tool per operation on task
success, unsafe-action rate and cost, **has been measured four times and is still unanswered**.

Every comparison was pre-registered: the hypothesis, corpus, arms, model, success definition
and significance threshold were fixed in a digested document before the run, and each run
records that digest. All returned inconclusive, and the nominal direction reversed between
them. Nothing here should be read as evidence that the semantic surface performs better. It is
an argued position that has so far survived no test capable of confirming it.

The most distinctive claim, that operations should be composed into workflow tools, is
untested rather than unsupported. The rule that proposes composites fires nowhere on the first
benchmark API, and designing a better one after reading that benchmark's solution paths would
fit the treatment to the test set. It needs a benchmark whose tasks have not been read here.

What the runs did establish is mostly about the instrument, and it was worth the tokens.
Several defects were found only by pointing the system at a specification and a task set
written by other people: a parser that marked every optional argument required, generated
schemas no client would load, a store in which a read mutated state and a bulk delete removed
everything, and oracles that scored the route an annotator took rather than the outcome a goal
asked for. Each is fixed, and the rules that would have prevented the last class now fail the
build.

## Install

```bash
pip install api-mcp-compiler
```

## Install from source, and verify

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python scripts/verify_repo.py
```

The gate runs pytest, ruff, mypy in strict mode, validates every committed example against its
versioned JSON Schema, builds the wheel and sdist and proves an installed copy can validate its
own artifacts, and re-executes the notebook to confirm its stored outputs still match. Lint and
type-checker versions are pinned exactly, so the gate means the same thing on every machine.

## Try it

```bash
# Normalize a specification into the intermediate representation
.venv/bin/python -m api_mcp_compiler.cli inspect examples/openapi/order_service.yaml

# Plan a tool surface, and read the review report a human would approve from
.venv/bin/python -m api_mcp_compiler.cli review examples/openapi/order_service.yaml

# Derive the governance manifest
.venv/bin/python -m api_mcp_compiler.cli policy examples/openapi/inventory_service.yaml

# Generate the surface, applying decisions a reviewer already recorded
.venv/bin/python -m api_mcp_compiler.cli generate examples/openapi/order_service.yaml \
  --overlay examples/overlays/order_service.overlay.json

# Write the conversion report a reviewer reads. Never overwrites an earlier one.
.venv/bin/python -m api_mcp_compiler.cli report examples/openapi/order_service.yaml

# Approve by class, so nobody hand-edits an overlay
.venv/bin/python -m api_mcp_compiler.cli approve examples/openapi/order_service.yaml \
  --overlay build/order.overlay.json --risk read

# Emit a runnable MCP server for the approved part of the surface
.venv/bin/python -m api_mcp_compiler.cli serve examples/openapi/order_service.yaml \
  --overlay examples/overlays/order_service.overlay.json --out build/order_server.py

# The same for SOAP. A WSDL operation needs its side effect recorded by a reviewer first,
# because WSDL carries no signal to infer one from.
.venv/bin/python -m api_mcp_compiler.cli serve examples/wsdl/customer_service.wsdl \
  --overlay examples/overlays/customer_service.overlay.json --out build/customer_server.py

# Score a surface against a task corpus, using deterministic oracles over final state
.venv/bin/python -m api_mcp_compiler.cli evaluate examples/openapi/order_service.yaml \
  examples/evals/order_tasks.json

# Check every artifact against its schema and list what is unresolved
.venv/bin/python -m api_mcp_compiler.cli validate examples/openapi/inventory_service.yaml
```

To read the same walk with every intermediate value printed, the provenance on a field, the
rationale behind a rename, the scope chosen over the union of its alternatives, the gate
holding a destructive tool and the review decision that releases it, open
[`notebooks/from_specification_to_server.ipynb`](https://github.com/bassrehab/api-mcp-compiler/blob/main/notebooks/from_specification_to_server.ipynb).
Its stored outputs are re-executed and compared by the verification gate, so they cannot drift
from the code that produced them.

## How it works

Ingestion, planning, policy synthesis and code generation are separate stages with separate
contracts. Each contract is versioned independently and pinned, so a document written against
one version fails validation loudly instead of being misread.

| Stage | Input | Output | Contract |
|---|---|---|---|
| Ingestion | OpenAPI 3.x, Swagger 2.0, WSDL 1.1 | API Semantic IR | `api_semantic_ir.schema.json` |
| Planning | IR, overlay | Tool plan | `tool_plan.schema.json` |
| Policy | IR, plan | Policy manifest | `policy_manifest.schema.json` |
| Generation | IR, plan, manifest | Tool surface | `mcp_tool_surface.schema.json` |
| Review | Plan | Overlay | `tool_overlay.schema.json` |
| Evaluation | Surface, corpus | Evaluation run | `evaluation_run.schema.json` |

## Design properties

**Every informative field carries provenance.** A record names the field, a pointer into the
source document, how the value was derived (`source`, `normalized`, `inferred`, `default`) and
a confidence. An inference cannot claim certainty and a source fact cannot express doubt; the
contract rejects both.

**Nothing is dropped silently.** Constructs that cannot be resolved become `Ambiguity` records,
and a completeness sweep reports every key an adapter did not consume. Duplicate mapping keys
are refused outright in YAML and JSON, because a dropped key never reaches the parser to be
swept.

**Ingestion never reaches the network.** `$ref` resolution defaults to deny. External files
load only from directories you name with `--allow-dir`, checked against real paths so `../`
cannot escape. Remote references are always refused. A specification is third-party input, and
following its pointers is an action taken on its author's behalf.

**Inference can only make an operation look less safe, never more.** Destructive wording raises
a write to destructive; it never lowers a class, and a read described destructively is flagged
for review rather than quietly reclassified. SOAP operations are never classified by inference
at all, because WSDL carries no signal equivalent to an HTTP method.

**Judgement never becomes silent action.** The planner proposes renames, omissions, groupings,
output projections and composite workflows, each with a rationale and a confidence. Nothing it
proposes changes the surface until a reviewer records the decision in an overlay, and an
overlay is bound by digest to the specification revision it was reviewed against.

**A tool is executable only if it earned it.** Reads pass automatically once validated. Write,
destructive and privileged tools stay disabled until approved. A blocking ambiguity, an
unclassified risk, or policy that could not be derived keeps a tool disabled regardless of
approval. Refused tools are still emitted carrying the reason, so the surface never shrinks
quietly.

**Governance is derived, not assumed.** Policy is generated separately from code:
least-privilege scopes chosen from the security alternatives rather than their union, approval
and confirmation classes, retry rules from inferred idempotency, rate budgets, output ceilings,
redaction and audit rules. A destructive action needs a confirmation token bound to the exact
arguments it was issued for, so confirming one action cannot authorise another.

**Evaluation is decided by state, never by a model.** A task names source operations rather
than tool names, so one corpus scores both planners. Success is judged by the state the
service ends in, together with oracles for absence of mutation, prohibited operations and
confirmation adherence. No safety or success number depends on a judge, and latency and token
cost are recorded as null rather than estimated, because a fabricated number sitting beside
measured ones is indistinguishable from a measured one.

**What the compiler cannot enforce, it says so.** Server-side authorization and protection
against confused-deputy designs are properties of a deployed service, not of a generated
artifact. The manifest records them as requirements and never reports them as satisfied.

## Reproducibility

Every document loaded during a compile is digested, not only the root, so an artifact stays
tied to the exact bytes it came from. Golden IR, plan, policy, surface and review artifacts are
committed. Regenerate them deliberately and review the diff:

```bash
.venv/bin/python scripts/regen_golden.py
```

## Documentation

The full guide lives at **[api-mcp.subhadipmitra.com](https://api-mcp.subhadipmitra.com/)**:
concepts, a command reference, the contract schemas, the SOAP path, and how evaluation and
pre-registration work. Its source is in [`guide/`](https://github.com/bassrehab/api-mcp-compiler/tree/main/guide).

## Scope

Uses synthetic and public specifications only. No credentials, customer specifications or
proprietary schemas belong in this repository.

## Author

**Subhadip Mitra**
[subhadipmitra.com](https://subhadipmitra.com) &middot; contact@subhadipmitra.com

If you use this in academic work, please cite it. [`CITATION.cff`](https://github.com/bassrehab/api-mcp-compiler/blob/main/CITATION.cff) has the
metadata, and GitHub renders it as a citation block on the repository page.

## Licence

Apache-2.0. See [`LICENSE`](https://github.com/bassrehab/api-mcp-compiler/blob/main/LICENSE) and [`NOTICE`](https://github.com/bassrehab/api-mcp-compiler/blob/main/NOTICE).

Apache rather than a copyleft licence, deliberately. The interesting part of this project is
what it decides a good tool surface looks like and what it refuses to emit; restricting who
may build on that would cost adoption without protecting anything worth protecting. The patent
grant also matters for the enterprises most likely to have a specification estate that needs
converting.

Benchmark specifications and services used to test the compiler are fetched at the point of
use and never redistributed here, so this repository carries no third-party source. Their
licences and attributions are recorded in `examples/benchmarks/manifest.json`.

Servers this compiler generates are its output, not a derivative of it. They carry whatever
licence their author chooses.
