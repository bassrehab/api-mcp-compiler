# CLI reference

```bash
api-mcp-compiler COMMAND [OPTIONS]
# or, from a checkout
.venv/bin/python -m api_mcp_compiler.cli COMMAND [OPTIONS]
```

No command runs an MCP server, binds to an SDK, or reaches the network. `serve` writes a server
module; running it is a separate act.

## Options shared by most commands

| Option | Meaning |
|---|---|
| `--kind auto\|openapi\|wsdl` | Override format detection. Detection is by file suffix and content, and `auto` is almost always right. |
| `--allow-dir PATH` | A directory whose files may be loaded by `$ref`. Repeatable, omitted by default, checked against resolved real paths so `../` cannot escape. |
| `--planner baseline\|semantic` | Which planner to use. The baseline exists only for controlled comparison. |
| `--overlay PATH` | A reviewed overlay. Its digest must match the specification, so decisions made about other bytes are refused rather than silently applied. |
| `--refs-lock PATH` | A reference lock from `vendor-refs`. Remote references resolve from the files it pins; bytes that have changed are refused. Ingestion still performs no network access. |

## inspect

Parse a source document and print the normalized IR as canonical JSON.

```bash
api-mcp-compiler inspect SPEC [--baseline/--no-baseline]
```

`--baseline` includes the baseline tool plan alongside the IR, and is on by default.

## plan

Print a tool plan as canonical JSON.

```bash
api-mcp-compiler plan SPEC [--planner semantic] [--overlay OVERLAY]
```

Every artifact is `proposed` until a reviewer records approval in an overlay.

## policy

Print the governance manifest for a planned surface as canonical JSON.

```bash
api-mcp-compiler policy SPEC [--planner semantic] [--overlay OVERLAY]
```

## generate

Generate a tool surface and print it as canonical JSON.

```bash
api-mcp-compiler generate SPEC [--overlay OVERLAY] [--enforce-policy/--no-enforce-policy]
```

The surface binds to no MCP SDK and performs no I/O. A tool is emitted executable only when its
source operation carries no blocking ambiguity, its risk is classified, and any write,
destructive or privileged tool has been approved. Refused tools are still emitted carrying the
reason, so the surface stays auditable.

## review

Print the human review report for the semantic plan, as Markdown.

```bash
api-mcp-compiler review SPEC [--overlay OVERLAY]
```

## report

Write the conversion report a reviewer reads before approving anything.

```bash
api-mcp-compiler report SPEC [--out-dir reports] [--overlay OVERLAY]
```

A self-contained HTML file naming the source digest it was produced from. Reports are never
overwritten: each run writes a new file, because a decision made against one set of proposals
is not evidence about a different set.

## approve

Record approval for a class of tools, writing the overlay so nobody hand-edits JSON.

```bash
api-mcp-compiler approve SPEC --overlay OVERLAY [--risk CLASS] [--group NAME] [--name TOOL]
```

`--risk` covers `read`, `write`, `destructive`, `privileged` and `unknown`. `--name` is
repeatable. A selection that names nothing is refused, and so is one that matches nothing.

The command reports what it approved, what was already approved, and what it left untouched.

## serve

Emit a runnable MCP server for the approved part of a surface.

```bash
api-mcp-compiler serve SPEC --out build/server.py [--overlay OVERLAY]
```

Emits an HTTP server for OpenAPI and a SOAP server for WSDL, and prints the requirements the
generated module needs. Tools the gate refused are not registered, and are listed on the
`surface://withheld` resource instead.

## vendor-refs

Fetch the remote references a specification names, and pin them by digest.

```bash
api-mcp-compiler vendor-refs SPEC --lock refs.lock.json [--cache DIR] [--record]
```

**The only command that reaches the network**, and it exists so that ingestion never has to.
It fetches over HTTPS with certificate verification, refuses anything larger than a schema
fragment plausibly is, and writes nothing until the bytes verify.

`--record` trusts references the lock does not already name, on first use. Without it, an
unrecorded reference is refused **before anything is fetched**, because trusting a source
should be a decision someone made rather than something that happened while a build ran.

Commit the lock and the cache. A compile then needs neither the network nor the clock, and an
upstream edit surfaces as a digest that no longer matches rather than as a surface that
quietly became something else.

## overlay-restamp

Bind an overlay to the current specification revision.

```bash
api-mcp-compiler overlay-restamp SPEC OVERLAY
```

Both arguments are positional, and the overlay is rewritten in place. Use this after an
intended change to a specification, having re-read what was approved.

## evaluate

Run an evaluation corpus against a generated surface and print the result.

```bash
api-mcp-compiler evaluate SPEC CORPUS [--planner semantic] [--overlay OVERLAY]
```

Uses the deterministic replay driver and deterministic oracles over final service state. See
[evaluation](evaluation.md).

## validate

Validate the IR and baseline plan against their schemas and report ambiguities.

```bash
api-mcp-compiler validate SPEC
```

Exits non-zero if an artifact does not satisfy its contract.
