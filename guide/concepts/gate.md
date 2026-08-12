# The emission gate

A tool is emitted executable only if it earned it. Everything else is emitted **disabled**,
carrying the reason.

Emitting a refused tool rather than dropping it is deliberate. A surface that silently omitted
an operation would be indistinguishable from one where the operation never existed, and nobody
reviews an absence.

## What blocks emission

| Blocker | Meaning |
|---|---|
| `blocking_ambiguity` | The source document is unclear in a way that affects this tool. |
| `awaiting_approval` | A write, destructive or privileged tool with no recorded approval. |
| `rejected` | A reviewer refused it. |
| `unclassified_risk` | The side effect could not be determined, which is the normal state for SOAP. |
| `argument_name_collision` | Two inputs would compose to the same argument, so the tool cannot represent its inputs faithfully. |
| `composite_pending_confirmation` | A composite that changes state, with no confirmation recorded. |
| `policy_unresolved` | Policy could not be derived, such as a state-changing tool whose authorization cannot be demonstrated. |

Reads pass automatically once validated. That is the whole point of separating them: a reviewer
clicking through twenty-three read tools individually is doing data entry, not governance, and
a gate that is drudgery gets bypassed wholesale rather than carefully.

## The gate blocks emission, not invocation

This is a deliberate choice about where the guarantee lives. An artifact that *cannot* invoke
an unapproved destructive tool is a stronger guarantee than one that could if a deployment were
misconfigured. The refusal is compiled in, not enforced at runtime by configuration.

## Approval is granted by class

```bash
api-mcp-compiler approve SPEC --overlay OVERLAY --risk destructive
api-mcp-compiler approve SPEC --overlay OVERLAY --group warehouses
api-mcp-compiler approve SPEC --overlay OVERLAY --name permanently_remove_item_record_warehouse
```

A selection that names nothing is refused, and so is one that matches nothing. There is
deliberately no flag that approves a whole surface without saying what class of thing it
belongs to.

The command reports what the approval covered, what was already approved and what it left
untouched, because a reviewer who cannot see what they just approved has not really approved
it.

## The human path

`report`, then `approve`, then `serve`.

A reviewer reads a self-contained HTML report, records a decision by class, and serves the
approved part of the surface. At no point should anyone have to hand-edit an overlay.
