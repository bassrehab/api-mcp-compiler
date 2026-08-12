# Quickstart

This walk uses `examples/openapi/inventory_service.yaml`, a small synthetic specification
chosen because it is awkward in useful ways: one destructive operation, three alternative
security requirements, a deprecated endpoint and a `default` response that may or may not be a
fault.

## 1. Look at what the compiler understood

```bash
.venv/bin/python -m api_mcp_compiler.cli inspect examples/openapi/inventory_service.yaml
```

Canonical JSON: the service, its digest, every operation with its side effect and idempotency,
and the provenance of each field. Ambiguities appear beside the constructs that produced them.

## 2. Read the review report

```bash
.venv/bin/python -m api_mcp_compiler.cli review examples/openapi/inventory_service.yaml
```

Markdown written for a person: what the planner proposes to rename, group, project, reclassify
or omit, each with a rationale and a confidence, and what it is holding back.

## 3. Derive the governance manifest

```bash
.venv/bin/python -m api_mcp_compiler.cli policy examples/openapi/inventory_service.yaml
```

Scopes, approval classes, confirmation requirements, retry rules, rate budgets, output ceilings
and redaction, derived separately from any code. See [policy](concepts/policy.md).

## 4. Generate the surface

```bash
.venv/bin/python -m api_mcp_compiler.cli generate examples/openapi/inventory_service.yaml
```

The destructive tool appears with `emission: disabled` and a blocker of `awaiting_approval`. It
is emitted rather than dropped, because a surface that silently omitted it would be
indistinguishable from one where the operation never existed.

## 5. Write the report a reviewer approves from

```bash
.venv/bin/python -m api_mcp_compiler.cli report examples/openapi/inventory_service.yaml
```

A self-contained HTML file naming the source digest it was produced from. Reports are never
overwritten: a decision made against one set of proposals is not evidence about a different
set, so each run writes a new file.

## 6. Approve by class

```bash
.venv/bin/python -m api_mcp_compiler.cli approve examples/openapi/inventory_service.yaml \
  --overlay build/inventory.overlay.json --risk destructive
```

Approval is granted over a class a person can reason about: `--risk`, `--group`, or named
tools. There is deliberately no flag that approves a surface without saying what class of thing
it belongs to. The command reports what the selection covered, and writes the overlay so nobody
hand-edits JSON.

## 7. Emit a runnable server

```bash
.venv/bin/python -m api_mcp_compiler.cli serve examples/openapi/inventory_service.yaml \
  --overlay build/inventory.overlay.json --out build/inventory_server.py
```

The emitted server validates arguments against each tool's own schema before calling anything,
demands a confirmation token bound to a digest of the arguments for destructive tools, caps
output size, applies redaction, and exposes `surface://withheld` listing what it refused to
register and why.

## 8. Score a surface against tasks

```bash
.venv/bin/python -m api_mcp_compiler.cli evaluate examples/openapi/order_service.yaml \
  examples/evals/order_tasks.json
```

Deterministic oracles over final service state. See [evaluation](evaluation.md).

## 9. Check everything against its contract

```bash
.venv/bin/python -m api_mcp_compiler.cli validate examples/openapi/inventory_service.yaml
```

Validates the IR and plan against their schemas and lists what is unresolved.
