# The API Semantic IR

Ingestion normalizes OpenAPI 3.x, Swagger 2.0 and WSDL 1.1 into one provider-independent
intermediate representation, so every later stage is written once against the IR rather than
once per input format.

## Provenance on every informative field

Each field carries a record naming the field, a JSON Pointer into the source document, how the
value was derived, and a confidence.

| Derivation | Meaning |
|---|---|
| `source` | Copied from the document. |
| `normalized` | The document's value, restated in the IR's vocabulary. |
| `inferred` | Not stated; derived from a rule, which is named. |
| `default` | Not stated and not inferable; a documented fallback. |

The contract enforces the pairing: an inference cannot claim a confidence of 1.0, and a source
fact cannot express doubt. A base-class validator rejects a model that omits provenance for a
field that must carry it, so this cannot be forgotten in a new adapter.

Why it matters: `side_effect` is `inferred` for HTTP, and whether an operation is destructive
decides whether a human has to approve it. A claim that consequential should not be anonymous.

## Ambiguities instead of guesses

Where a document is genuinely unclear, the IR records an `Ambiguity` beside the construct that
produced it: a code, the field, a source pointer, a detail written for a person, and a
`blocking` flag.

A non-blocking ambiguity is a note for review. A blocking one stops the affected tool from
being emitted executable at all, whatever else is true about it.

Examples that occur in the shipped specifications:

- `default_response_classified_as_fault`, where OpenAPI's `default` response may be a success
  case and the document does not say.
- `security_requirement_alternatives`, where several alternative security requirements exist.
  Ingestion records their union, keeps every alternative, and defers the choice, because
  choosing is a [policy](policy.md) decision rather than a parsing one.

## Completeness

A consumption ledger records every key an adapter read. Anything left over is reported, so a
construct the parser does not understand cannot pass through unnoticed. Duplicate mapping keys
are refused outright in both YAML and JSON, because a dropped key never reaches the parser to
be swept in the first place.

## Reference resolution never reaches the network

`$ref` resolution defaults to deny. Local files load only from directories named with
`--allow-dir`, checked against resolved real paths so `../` cannot escape. Remote references
are always refused; there is deliberately no allowlist flag, because shipping configuration for
unimplemented behaviour is worse than refusing plainly.

A self-referencing schema is legitimate and common, so a cycle leaves the innermost `$ref` in
place and records a non-blocking ambiguity rather than raising. Depth overrun still raises.

## Digests

Every document loaded during a compile is digested, not only the root. An artifact therefore
stays tied to the exact bytes it came from, and a change anywhere in the document set is
visible as a changed digest. See [reproducibility](reproducibility.md).
