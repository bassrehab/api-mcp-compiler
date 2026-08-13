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

### Confirmation tokens live in memory

A confirmation is bound to a digest of the arguments, expires, and is single use. The store
holding them is a dictionary in the process.

Two consequences. A restart forgets outstanding confirmations, so an agent mid-flow is asked
again, which is safe and merely annoying. And with several replicas, a confirmation issued by
one is unknown to another, so the agent is asked again by whichever answers next.

Neither weakens the guarantee: a call still cannot proceed without a confirmation that
process issued for those exact arguments. Sticky routing removes the friction if it matters.

### Output caps and redaction are per response

The output ceiling and redaction rules apply to what each tool returns. They do not bound what
an agent accumulates across a conversation, and they cannot redact something the service
returns in a field the specification never declared.

What to do: treat them as a floor. If a service can return unbounded or unexpected data, cap
it at the service or in front of it as well.

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
