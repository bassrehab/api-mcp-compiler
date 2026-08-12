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

## What the compiler cannot enforce, it says so

Server-side authorization, protection against confused-deputy designs, and end-user identity
propagation are properties of a deployed service rather than of a generated artifact. The
manifest records them as requirements and never reports them as satisfied.
