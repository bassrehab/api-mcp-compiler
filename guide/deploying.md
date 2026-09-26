# Deploying a generated server

This page is about the gap between what the compiler decides and what a running system
actually enforces. Everything here is a control the generated artifact cannot provide, or
provides only within limits worth knowing before you rely on it.

The compiler records these as requirements and never reports them as satisfied. That is the
honest position, and it is also useless on its own, so this page says what to do about each.

## What the artifact cannot enforce at all

### Server-side authorization

A tool description is not an access control. The generated server presents the credential the
specification declared and asks the service to do the work; whether the caller was entitled to
that work is decided by the service, and by nothing here.

**A surface that omits a destructive tool is not a service that refuses destructive calls.**
The emission gate governs what an agent is offered, which is a real reduction in what it will
attempt. It is not a boundary, and an agent with the credential can be pointed at the endpoint
by other means.

What to do: enforce authorization at the service, scoped to the credential the surface uses.
Treat the least-privilege scopes in the policy manifest as the maximum the credential should
be granted, not as evidence of what it can do.

### Confused deputy and token passthrough

A generated server holds a credential and acts on behalf of whoever asks it to. If that
credential is more privileged than the end user, the server is a deputy that can be confused
into doing work the user could not do alone.

The compiler cannot detect this. It sees one credential and one service.

What to do:

- Give each surface its own credential, scoped to what that surface's tools need and nothing
  more. The manifest's `required_scopes` is the list to scope it to.
- Where the upstream service supports it, propagate end-user identity rather than acting
  wholly as the application. A service that can distinguish "the application asked" from "this
  user asked" is the only thing that closes this properly.
- Do not accept a caller-supplied token and forward it to a different audience. The generated
  server never does this, and adding it by hand reintroduces the problem the design avoided.

### End-user identity propagation

Related and distinct: even correctly scoped, a server acting purely as an application loses
who asked. Audit trails downstream then record the surface rather than the person.

What to do: propagate identity where the protocol allows it, and where it does not, keep the
correlation on your side. The evaluation harness records an `identity` per task precisely
because identity is a property of the request, not of the tool.

## What the artifact enforces, within limits

### Call budgets are counted per process

The generated server holds each tool to the calls per minute, concurrency and daily budget its
policy derived. That counting lives in the process.

**Run four replicas and you have four budgets.** A destructive tool limited to two calls a
minute is limited to eight across four workers, which is not what the policy says and not what
a reviewer approved.

What to do: run a single instance per surface where the budget matters, or put a shared
counter in front of the tools. If you do the latter, the numbers to enforce are in the policy
manifest and do not need re-deriving.

### Runtime confirmation is friction, not a person

The human approval in this project happens at compile time: a reviewer enables a destructive
tool by name, group or risk class, and until then it is not emitted at all. The runtime
confirmation on a destructive tool is a second, weaker control, and it is worth being precise
about what it is.

A first call returns `confirmation_required` with a token bound to a digest of the arguments. An
identical call to the same process inside the time to live runs the operation. Nothing in that
exchange involves a person: an agent that repeats the call has confirmed it, and the refusal
message tells it how. So the confirmation stops an agent from performing a destructive operation
by accident on its first attempt. It does not put a human in the loop at runtime.

The store holding the tokens is a dictionary in the process, which has two further consequences.
A restart forgets outstanding confirmations, so an agent mid-flow is asked again. And with several
replicas behind round-robin routing, a confirmation issued by one is unknown to the next, so the
agent is asked again by whichever answers, indefinitely. That fails safe. Sticky routing removes
it.

What to do: if a destructive tool needs a person at runtime, the client host has to show the call
to one before sending it; the MCP specification places that duty on the client. Replacing this
mechanism with an elicitation a person answers, carried in sealed request state so it survives
replicas, is the next planned change, and it needs the 2.x target, which now exists.

### Output caps and redaction are per response

The output ceiling and redaction rules apply to what each tool returns. They do not bound what
an agent accumulates across a conversation, and they cannot redact something the service
returns in a field the specification never declared.

What to do: treat them as a floor. If a service can return unbounded or unexpected data, cap
it at the service or in front of it as well.

## Which MCP protocol a generated server speaks

By default a generated server is written against the 2.x Python SDK and requires `mcp>=2,<3`.
It serves both protocol eras from one process: a client on the 2026-07-28 revision sends each
request with its version in `_meta` and no handshake, and a client on an earlier version
completes the initialization handshake as before. The server answers `server/discover`, and an
unknown tool is the protocol error the new revision prescribes rather than a tool result.

`serve --sdk 1` writes against the 1.x SDK instead and requires `mcp>=1.2,<2`. That SDK speaks
protocol versions up to 2025-11-25 only, so a client that speaks only 2026-07-28 cannot connect.

What to do: use the default unless something pins you to 1.x, and if it does, check that your
clients support a pre-2026-07-28 protocol version. The 1.x target exists for deployments that
cannot move yet.

On the 2.x target, the two namespaced hints, `x-rotaforge/sensitiveHint` and
`x-rotaforge/reversibleHint`, travel in each tool's `_meta` rather than its `annotations`: SDK 2.x
drops annotation keys it does not know, and `_meta` is where the protocol puts extensions.

## Credentials

The generated server reads each credential from an environment variable named after its
security scheme, at call time. No credential is ever written into a generated file, and
`serve` prints the variables a deployment must set.

What to do:

- Supply them from a secret manager rather than a shell profile or an image layer.
- Rotate them. Nothing in the artifact caches a credential beyond the call it is used for.
- Scope them to the manifest's `required_scopes` for the tools you actually enabled. A surface
  where every destructive tool was withheld does not need a credential that can perform them.

## Before you enable a write or destructive tool

The gate already required a human to approve it by class. These are the questions worth
answering before that approval, and they are the ones the compiler cannot answer for you:

- Can the effect be reversed, and by whom? The manifest carries `rollback_guidance`, which
  says plainly when no automated compensation exists.
- Is the upstream authorization scoped to what this surface should be able to do?
- Does the budget in the manifest match what you would accept an agent doing in a bad minute?
- If the operation only accepts work, does anything downstream check that the work completed?
  An operation with an `async_job` returns before the result is real.

## What to read next

- [Policy](concepts/policy.md) for how each of these values is derived.
- [The emission gate](concepts/gate.md) for what approval does and does not mean.
- The `unresolved` list on each tool policy, which names everything the compiler could not
  demonstrate for that tool specifically.
