# api-mcp-compiler

A compiler that turns REST/OpenAPI and SOAP/WSDL service descriptions into agent-ready MCP
tool surfaces, with provenance on every field, a safety gate that refuses to emit what it
cannot justify, and governance derived separately from code.

Turning one API operation into one MCP tool is already commodity. The harder and more useful
problem is deciding **which** tools should exist, what they should be called, what they should
accept and return, and what may not be invoked without a human in the loop.

## Status

Working software, and two recorded comparisons that did not find a difference.

The compiler ingests OpenAPI 3.x and WSDL 1.1, produces an intermediate representation, plans
a tool surface, derives a policy manifest, and generates a transport-independent surface that
runs against a deterministic mock. An evaluation harness scores a surface against a task
corpus using deterministic oracles over real service state, driven either by a replay of a
recorded solution or by a model that sees only the goal and the tools. It emits a runnable MCP server for the approved part of a surface — over HTTP for
OpenAPI, and over SOAP envelopes for WSDL — while the compiler itself binds to no MCP SDK,
never calls a model, and never reaches the network.

The central question — whether a semantically planned surface beats one-tool-per-operation on
task success, unsafe-action rate and cost — **has been measured twice and is still unanswered**.

Both comparisons were pre-registered: the hypothesis, corpus, arms, model, success definition
and significance threshold were fixed in a digested document before the run, and each run
records that digest. Both returned inconclusive, on 24 tasks over a third-party benchmark, and
the nominal direction reversed between them. Nothing here should be read as evidence that the
semantic surface performs better. It is an argued position that has so far survived no test
capable of confirming it.

What the runs did establish is mostly about the instrument. Several defects were found only by
pointing the system at a specification and a task set written by other people: a parser that
marked every optional argument required, generated schemas no client would load, a store in
which a read mutated state and a bulk delete removed everything, and oracles that scored the
route an annotator took rather than the outcome a goal asked for. Each is fixed, and the rules
that would have prevented the last class now fail the build.

The most distinctive claim — that operations should be composed into workflow tools — remains
untested. The rule that proposes composites fires nowhere on the benchmark API, and designing
a better one after reading that benchmark's solution paths would fit the treatment to the test
set. It needs a benchmark whose tasks have not been read here.

## Install and verify

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python scripts/verify_repo.py
```

The gate runs pytest, ruff, mypy in strict mode, and validates every committed example against
its versioned JSON Schema. Lint and type-checker versions are pinned exactly, so the gate means
the same thing on every machine.

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

## How it works

```text
specification -> ingestion -> API Semantic IR -> planner -> tool plan
                                                    |
                              overlay (human decisions)
                                                    |
                                              policy manifest
                                                    |
                                          generated tool surface
                                                    |
                                            deterministic mock
```

Ingestion, planning, policy synthesis and code generation are separate stages with separate
contracts. Each contract is versioned independently and pinned, so a document written against
one version fails validation loudly instead of being misread.

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

## Scope

Uses synthetic and public specifications only. No credentials, customer specifications or
proprietary schemas belong in this repository.

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`.

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
