# Policy

Policy generation is deliberately separate from code generation. A scope, an approval
requirement and a confirmation rule are governance claims; deriving them inside a code template
would make them invisible to review.

The output is a policy manifest: one `ToolPolicy` per tool, each carrying provenance for how
every value was reached, including the reasoning in the rule string.

## Least privilege, chosen rather than unioned

Ingestion refuses to pick between alternative security requirements. Policy picks, and shows
its work.

The subtlety worth knowing: **fewest scopes is not the same as least privilege**. A scopeless
credential such as an admin key looks narrowest by count while granting the most. So scoped
alternatives are preferred, and a scopeless winner is reported as a concern rather than
accepted.

For the shipped example, three alternatives resolve to:

```
scopes   ['inventory.write']
         Narrowest of 3 alternatives, using inventoryOAuth. The union across alternatives
         would have granted inventory.admin, inventory.write; rejected
         inventoryOAuth(inventory.admin, inventory.write); inventoryAdminKey(no scopes).
```

An authorization concern is fatal only for a tool that changes state. A read with no declared
authentication is an ordinary public endpoint; a write or destructive one with none cannot be
shown to be governed, so it fails closed.

## The credential, not only the scope

A scope says how much access is needed. It does not say what to present at the door, and a
generated server that knows one without the other cannot authenticate at all. The manifest
therefore records `required_schemes` beside `required_scopes`: the identifiers of the
alternative that least-privilege selection chose.

The emitted server places each credential where the specification declared it:

| Scheme | Placement |
|---|---|
| `apiKey` | The header, query parameter or cookie the service named. |
| `http` with `scheme: basic` | `Authorization: Basic`, base64 encoded from a `user:password` variable. |
| `http` with `scheme: bearer` | `Authorization: Bearer`. |
| `oauth2`, `openIdConnect` | `Authorization: Bearer`. |
| Anything else | Nothing is sent. An unplaceable scheme is reported, never guessed. |

One environment variable per scheme, named `<SERVICE>_<SCHEME>_CREDENTIAL` and reported by
`serve`. A variable that is unset is omitted rather than sent empty, so the call fails at the
service as unauthenticated rather than locally as malformed. No credential value is ever
written into a generated file.

## Confirmation is bound to the arguments

A destructive tool requires a confirmation token derived from a digest of the exact arguments
it was issued for, with a time to live. Confirming one call cannot authorise a different one.

The manifest also records an effect summary written for the person being asked to confirm, and
rollback guidance that says plainly when no automated compensation exists.

## What else is derived

| Field | Derived from |
|---|---|
| `approval` | Risk class and side effect. |
| `retry`, `idempotency_key_required` | Idempotency inferred at ingestion, per RFC 9110 for HTTP methods. |
| `rate` | Risk class: calls per minute, concurrency, daily budget. |
| `log_class`, `sensitivity` | Vocabulary in field names and paths. |
| `output.max_bytes`, `redact_fields` | Output ceilings and redaction. |
| `allowed_environments` | Risk class. |
| `unresolved` | Everything the compiler cannot demonstrate. |

## Redaction decides on the noun

Sensitivity is decided on the last token of a field name, the noun the name ends on, rather
than on any overlap with a secret vocabulary. `accessToken` and `clientSecret` are secrets;
`TitleCaseWordsWithTokenResult` is a result, and a live public service returned exactly that.
Redacting on containment removed the service's answer and reported HTTP 200 with no indication
that anything had been withheld.

## Retrying, and when not to

`retry` is derived from the idempotency inferred at ingestion, and the generated server acts
on it rather than merely carrying it.

| Policy | Derived when | The server then |
|---|---|---|
| `safe` | The operation is a read, or is idempotent | Retries up to three attempts in total |
| `with_idempotency_key` | The operation is not idempotent | Retries, sending an idempotency key |
| `never` | Idempotency could not be determined | Makes one attempt |

**What is retried** is deliberately narrow: 429, the gateway codes 502, 503 and 504, and
transport failures where nothing was answered at all. A 4xx is the service rejecting the
request, and repeating it changes nothing but the load.

**500 is never retried**, on any policy. A server error may mean the effect happened and the
answer was lost, and repeating that is exactly what a retry policy exists to prevent.

**The idempotency key is generated per invocation and held across that invocation's retries.**
A fresh key per attempt would make every retry a new operation, which defeats the point. A key
derived from the arguments would make two deliberate identical calls collide, which is worse:
the second would silently return the first one's result.

`Retry-After` is honoured over the backoff curve, because a service that names its window
knows more about it than any curve written here. Attempts are bounded so that a wedged
upstream is reported rather than hammered.

The SOAP path retries on the same transport codes and sends no idempotency key. WSDL declares
nothing equivalent, so a key here would be a header this compiler invented that no service was
built to honour.

## Call budgets, and what enforcing them here can and cannot mean

Policy scales three limits by risk, and the generated server counts calls against all three.

| Limit | Read | Destructive |
|---|---|---|
| `calls_per_minute` | 60 | 2 |
| `max_concurrent` | 4 | 1 |
| `daily_call_budget` | 5000 | 20 |

Over budget, a call is **refused rather than queued**. A queued call looks to an agent like a
slow service, and it will wait, retry, or abandon a goal it could have reached. A refusal that
names the limit, the number allowed and when it lifts is something an agent can act on:

```json
{
  "error": "rate_limited",
  "limit": "calls_per_minute",
  "allowed": 2,
  "retry_after_seconds": 41.2,
  "detail": "..."
}
```

A call that never reaches the service costs nothing. Argument validation and the confirmation
gate both run first, so a malformed call or an unconfirmed destructive one does not spend a
budget it never used.

**The counting is per process.** A deployment running several workers needs a shared counter,
and a generated server cannot pretend to be one. This is a real limit on what the artifact
enforces, and it is stated here rather than left for someone to discover after splitting a
service across two replicas.

## What the compiler cannot enforce, it says so

Server-side authorization, protection against confused-deputy designs, and end-user identity
propagation are properties of a deployed service rather than of a generated artifact. The
manifest records them as requirements and never reports them as satisfied.

Recording them is honest and, on its own, useless. [Deploying](../deploying.md) says what to
do about each, including the limits on the controls the artifact does enforce.
