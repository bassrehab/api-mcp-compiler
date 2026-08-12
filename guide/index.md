# api-mcp-compiler

Compile REST/OpenAPI and SOAP/WSDL services into agent-ready MCP tool surfaces that are
semantically designed, policy-governed and evaluation-backed.

Turning one API operation into one MCP tool is already commodity. The harder and more useful
problem is deciding **which** tools should exist, what they should be called, what they should
accept and return, and what may not be invoked without a human in the loop.

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

## What it does

| Stage | Input | Output |
|---|---|---|
| [Ingestion](concepts/ir.md) | OpenAPI 3.x, Swagger 2.0, WSDL 1.1 | API Semantic IR |
| [Planning](concepts/planning.md) | IR, overlay | Tool plan |
| [Policy](concepts/policy.md) | IR, plan | Policy manifest |
| Generation | IR, plan, manifest | Tool surface |
| Review | Plan | Overlay |
| [Evaluation](evaluation.md) | Surface, corpus | Evaluation run |

Each stage has its own versioned [contract](contracts.md), and each artifact carries the digest
of the specification it came from.

## Where to start

- **[Install](install.md)**, then run the verification gate to confirm the checkout is sound.
- **[Quickstart](quickstart.md)** walks a specification to a running server in nine commands.
- The **[notebook](https://github.com/bassrehab/api-mcp-compiler/blob/main/notebooks/from_specification_to_server.ipynb)**
  does the same walk with every intermediate value printed, and its outputs are re-executed by
  the verification gate so they cannot go stale.

## What is demonstrated

The conversion and the governance. OpenAPI 3.x, Swagger 2.0 and WSDL 1.1 all compile to a
governed surface, exercised against specifications this project did not write: 40 Spotify
operations, the 20-operation Swagger 2 Petstore, 40 WSDL documents from a public collection,
and two live SOAP services that answered real calls from a generated server.

Judgement is proposed rather than applied, a write or destructive tool is not emitted in
executable form until a human approves it by class, and what cannot be demonstrated is
recorded as unresolved instead of assumed.

## What is not demonstrated

That a semantically planned surface measurably beats one tool per operation. That question has
been [pre-registered and measured four times](evaluation.md), and every result was
inconclusive. The claim remains argued, not demonstrated, and this documentation says so
wherever it comes up.

## Design properties in one page

**Every informative field carries provenance.** The field name, a pointer into the source
document, how the value was derived (`source`, `normalized`, `inferred`, `default`) and a
confidence. An inference cannot claim certainty and a source fact cannot express doubt.

**Nothing is dropped silently.** What cannot be resolved becomes an `Ambiguity` beside the
construct that produced it, and a completeness sweep reports every key an adapter did not read.

**Ingestion never reaches the network.** Remote `$ref` resolution is refused outright, and
local resolution only inside directories you name.

**Inference can only make an operation look less safe.** It raises a write to destructive; it
never lowers a class.

**Judgement never becomes silent action.** Every planner decision is a proposal with a
rationale and a confidence, and applies only once recorded in an overlay bound by digest to the
specification revision it was reviewed against.

**A tool is executable only if it earned it.** See [the emission gate](concepts/gate.md).

**Governance is derived, not assumed.** See [policy](concepts/policy.md).

**Evaluation is decided by state, never by a model.** See [evaluation](evaluation.md).

## Author

**Subhadip Mitra**, [subhadipmitra.com](https://subhadipmitra.com),
contact@subhadipmitra.com. Licensed Apache-2.0.
