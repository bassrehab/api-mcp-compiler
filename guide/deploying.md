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

### Runtime confirmation: a person on 2.x, friction on 1.x

The first human approval happens at compile time: a reviewer enables a destructive tool by name,
group or risk class, and until then it is not emitted at all. What happens when an enabled
destructive tool is called depends on the target.

**On the 2.x target, a person confirms.** The server asks through an MCP elicitation, showing
the policy's effect summary and the arguments, and the person retypes the identifying argument,
such as the warehouse id, to proceed. A client on the 2026-07-28 protocol receives this as an
`input_required` result and answers on a retry; a client on an earlier protocol is asked
mid-call. Arguments are validated first, so nobody is asked to confirm a call the schema would
refuse. An agent that retries without an answer is asked again. A client that cannot show a
person an elicitation is refused with `confirmation_unavailable`, rather than falling back to
anything the agent could satisfy alone.

The pending call travels to the client and back as sealed request state, bound by the SDK to
the tool, a digest of the arguments, the caller and an expiry, so a confirmation cannot be moved
to other arguments or altered. The server also records each confirmation it honours, so one
answer cannot be replayed within a process.

**The key.** Sealed state is only checkable by a replica that holds the key that sealed it, and
the SDK's default is a new random key per process. So a server with a confirmation-gated tool
will not start until `<SERVICE>_REQUEST_STATE_KEY` is set: to the same secret of at least 32
bytes on every replica, or to `ephemeral` when exactly one process serves the surface. `serve`
prints the variable's name.

**Replay across replicas.** A sealed confirmation is valid on any replica holding the key until
it expires. Within one process the server records each confirmation it honours. Across replicas
that takes a shared record: set `<SERVICE>_CONFIRMATION_RECORD` to a Redis URL every replica
shares, and install `redis>=5`. Each confirmation is then spent with Redis's atomic set-if-absent,
once across all replicas, with a key that outlives the sealed state. The server checks the record
is reachable when it starts, and refuses a call whose confirmation it cannot record rather than
honour it.

With a shared key and a gated tool that is not idempotent, the record is required: the server
refuses to start without it and names the tools. For idempotent tools it is optional, and without
it a confirmation could be replayed on another replica until it expires. `serve` prints the
variable's name.

**On the 1.x target, confirmation is friction.** A first call returns `confirmation_required`
with a token bound to a digest of the arguments, and an identical call to the same process inside
the time to live runs the operation. An agent that repeats the call has confirmed it. That stops
an accidental first call; it does not put a person in the loop. The tokens live in the process,
so a restart forgets them, and behind round-robin routing a confirmation issued by one replica is
unknown to the next, which fails safe.

What to do: use the 2.x target for any surface with an enabled destructive tool, and use a client
that can show elicitations to a person. The MCP specification also asks clients to show tool
calls to a person before sending them; the 2.x gate does not depend on that.

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

## List caching and routing headers

Both come from the policy manifest, and a 2.x server renders them; the 1.x target has neither,
since both are 2026-07-28 features.

**List caching.** `tools/list`, `resources/list` and `resources/templates/list` carry the
manifest's `list_cache`: a `ttlMs` of one minute when any tool can change state and five minutes
when every tool is a read, and a `cacheScope` of `private` when any tool needs a credential.
A client may keep the list for that long, which is how long a revoked tool can linger in front
of an agent after you redeploy without it. The value is derived by a rule, not configured per
surface; if the rule is wrong for a surface you run, that is a finding against the rule, and
editing the generated code would only hide it until the next compile.

**Routing headers.** A tool's path identifiers, such as `warehouse_id`, carry `x-mcp-header` in
its schema, so a client on Streamable HTTP sends them as `Mcp-Param-Warehouse-Id`. A gateway can
route or enforce on that without parsing the body, for instance allowing a caller only its own
warehouses. Only identifiers of type string or integer are mirrored, never free text and never a
name the redaction rules flag, because every intermediary on the path can read a header. Names
are hyphenated because some proxies drop headers with underscores.

What to do: if a gateway enforces on a routing header, treat it as a hint the client supplied,
and check it against the body or the upstream authorization as well. A header is only as honest
as the client that set it.

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
