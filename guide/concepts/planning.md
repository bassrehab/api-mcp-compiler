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
| `group` | Groups by a tag the document declares, falling back to the first path segment. |
| `project` | Withholds arguments that are transport rather than task, such as pagination cursors that are optional and have server defaults. |
| `describe` | Rewrites the description for an agent reading a tool list, dropping formatting written for a rendered page and stating the side effect where a model is actually looking. The source text is unchanged in the IR. |
| `reclassify` | Turns an addressable read into a resource, so a lookup does not spend a tool slot. The resource carries the address it is read by. |
| `omit` | Drops a deprecated operation, so agent attention is not spent on a surface the provider intends to withdraw. |
| `compose` | Proposes a composite workflow tool over a lookup-then-act chain. |

## Grouping follows the document, not the path shape

A specification that declares `tags` has already said how its authors group their own
operations, and that is a better signal than anything derived from path shape. The planner
uses the first declared tag, at a higher confidence than the fallback, because a declared
grouping is a source fact where a path prefix is an inference.

Where an operation declares several tags the first is used. OpenAPI puts the primary tag
first by convention, and choosing by any other rule would be this planner overruling the
document about its own structure.

The path prefix remains the fallback for documents that declare no tags, and a SOAP operation
groups by its port type, which plays the role a path prefix plays for HTTP.

This was found by running the compiler at a real third-party specification for the first time:
all 40 operations of the Spotify document carry tags, and every one of them was being reported
by the completeness sweep as a key nothing had read.

## A resource is addressable, or it is not a resource

Reclassification only applies to a read whose inputs all identify what to fetch. The generated
surface records the address such a read is available at, as a URI template whose placeholders
are the operation's path parameters, and the server registers it as a resource rather than a
tool:

```
synthetic-inventory-service://warehouses/{warehouse_id}/items-v1
```

The scheme is the service identifier, so two surfaces mounted alongside each other cannot
collide on a shared path like `warehouses/{id}`.

An operation whose inputs the address cannot express stays a tool. So does every SOAP
operation: they are all a POST to one endpoint, and what distinguishes them is the envelope,
which is not something a URI can carry.

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
