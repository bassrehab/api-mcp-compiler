# Planning a surface

Two planners produce the same contract.

**The baseline planner** maps one operation to one tool. It exists so the semantic planner has
something to be compared against, and for no other reason.

**The semantic planner** proposes a designed surface. Every decision it makes is recorded with
a kind, a target, a rationale written for a person, and a confidence.

## What the semantic planner proposes

| Decision | What it does |
|---|---|
| `rename` | Names a tool after the task rather than the API. `listWarehouseItems` becomes `list_items_held_warehouse`, from the operation summary. |
| `group` | Groups by the first path segment, the coarsest grouping the specification states rather than one the planner invents. |
| `project` | Withholds arguments that are transport rather than task, such as pagination cursors that are optional and have server defaults. |
| `describe` | Rewrites the description for an agent reading a tool list, dropping formatting written for a rendered page and stating the side effect where a model is actually looking. The source text is unchanged in the IR. |
| `reclassify` | Turns an addressable read into a resource, so a lookup does not spend a tool slot. |
| `omit` | Drops a deprecated operation, so agent attention is not spent on a surface the provider intends to withdraw. |
| `compose` | Proposes a composite workflow tool over a lookup-then-act chain. |

## Names have to be unique, and summaries do not guarantee that

A summary says what an operation does, but not always what it does it to. One real benchmark
API gives ten operations the summary "Get Details", so a renamer reading only the summary
produces ten tools called `get_details` and the API rejects the surface outright. Names are
therefore disambiguated against those already taken, using what the operation acts on.

## Confidence is counted, not asserted

An artifact's confidence comes from readiness signals present in the specification, and the
rationale names which ones are missing. A clean read scores 1.0; an operation with no
description and no declared success schema scores lower and says why.

## Nothing applies until a human records it

A plan is a set of proposals. The overlay is where a reviewer's decisions live, and it is bound
by digest to the specification revision it was reviewed against, so approval cannot silently
carry over to a document that has since changed.

Overlays are written by the `approve` command rather than by hand. A reviewer should never have
to edit JSON to use this project: the overlay is a machine artifact that records a decision,
not a form.

## Composition is the untested claim

The composite rule proposes a workflow tool where a write needs an identifier that a read
yields, with a constraint that the composite must begin with something a goal can reach on its
own. On the first benchmark API it fires zero times, and designing a broader rule after reading
that benchmark's annotated solution paths would fit the treatment to the test set. It needs a
benchmark whose tasks have not been read here. See [evaluation](../evaluation.md).
