# Changelog

## Unreleased

### Fixed

- **A WSDL message with no parts is a void response, not an unresolved type.** WSDL permits an
  empty message and services use it the way HTTP uses 204. Reading it as unresolved blocked
  the operation, so two of the forty documents in the third-party collection were refused for
  declaring their response empty clearly. Found by asking the completeness sweep what it was
  still reporting, rather than by guessing where to look.

## 0.5.0

Released 2026-08-13. Remote references resolve without ingestion ever reaching the network, an
operation that only accepts work says so, grouping follows the tags a document declares, and
the evaluation store reports which of its effects are guesses.

Minor rather than patch because the IR contract gained fields: an IR written by 0.4.0 no
longer validates. Surfaces, plans, policy manifests and evaluation runs are unchanged.

### Evaluation

- **A bodyless `POST` commands rather than creates.** A creation needs something to create
  from, so `POST /me/player/next` skips a track where `POST /playlists` makes a playlist. The
  old model invented a record for every skip, which the module's own docstring had listed as a
  known limitation. On the Spotify document it affects three operations.
- **Every derived effect names the rule that produced it and how much that rule is worth.** A
  result judged against a guess and one judged against a convention that always holds looked
  identical from the outside.
- `scripts/effect_coverage.py` reports the distribution over a real specification. On the 40
  operations of the Spotify document, 35 are modelled at 0.8 confidence or above and 5 below,
  which is the measurement the issue asked for rather than an assurance.

### Remote references

- **A specification with remote references now compiles, and ingestion still never reaches
  the network.** Those two facts are only compatible because fetching is a separate command:
  `vendor-refs` fetches each remote document once over HTTPS, pins it by digest and records a
  lock, and ingestion reads the pinned files, verifies them, and refuses anything the lock
  does not name.
- A reference the lock does not already name requires `--record`, and is refused **before
  anything is fetched**. Trusting a source should be a decision someone made rather than
  something that happened while a build ran.
- An upstream edit is visible rather than adopted: a document that now serves different bytes
  fails the vendor step, and bytes edited in the cache after locking fail at compile time with
  nothing loaded.
- Reproducibility is unchanged in kind. A compile needs the specification, the lock and the
  cache, and neither the network nor the clock. There is no time-based revalidation, because
  a cache that refreshed itself would reintroduce exactly the dependency this avoids.
- `--refs-lock` is accepted by every command that parses a document.

### Documentation

- **A deployment page for the controls a generated artifact cannot provide.** The manifest has
  always recorded server-side authorization, confused-deputy exposure and identity propagation
  as requirements it never reports as satisfied, which is honest and, alone, useless. The
  guide now says what to do about each.
- It also states the limits on the controls the artifact does enforce: call budgets are counted
  per process, so four replicas are four budgets, and confirmation tokens live in memory, so a
  restart forgets outstanding ones.

### Ingestion

- **An operation that only accepts work now says so.** HTTP 202 means the request was taken,
  not carried out, and a surface silent about that lets an agent read acceptance as completion
  and report a goal met before anything has happened. The IR records an `async_job`, and the
  planner states it in the tool description, which is where a model looks. One of the shipped
  examples turns out to be exactly this case.
- A `Location` header on that response is recorded as the poll target, marked inferred rather
  than source. Where a document declares acceptance and names nowhere to look, that is
  reported as such rather than filled in with a convention nobody promised.
- **`info.description` and `info.termsOfService` are kept rather than swept.** The description
  becomes the generated server's instructions, capped in length, so an agent is told the
  domain it works in instead of inferring it from tool names.
- `api_semantic_ir` schema version raised to `0.7.0`.

## 0.4.0

Released 2026-08-13. Call budgets are enforced rather than declared, grouping follows the
tags a document declares, a reclassified read is addressable, and tool selection is measured.

Minor rather than patch because three contracts moved: an IR, a surface or an evaluation run
written by 0.3.0 no longer validates. The policy manifest is unchanged.

### Safety

- **Call budgets are enforced rather than declared.** Policy has always scaled calls per
  minute, concurrency and a daily budget by risk, and the generated server counted nothing, so
  a destructive tool with a budget of two calls a minute would make two hundred. Fourth in a
  row of the same shape after the confirmation time to live, the credential placement and the
  retry policy.
- Over budget, a call is refused rather than queued, and the refusal names the limit, the
  number allowed and when it lifts. A queued call looks to an agent like a slow service; a
  refusal it can reason about is more useful than a wait it cannot see.
- A call that never reaches the service spends nothing: validation and the confirmation gate
  both run before the budget is taken. The concurrency slot is returned on every path out,
  including a failed composite step and a truncated response.
- The counting is per process, and the guide says so. A deployment running several workers
  needs a shared counter, which a generated artifact cannot be.

### Planning

- **Grouping follows the tags a document declares**, falling back to the first path segment
  only where none exist. A declared grouping is a source fact where a path prefix is an
  inference, and the confidence recorded for the decision says so.
- Operation `tags` are now kept in the IR rather than swept. Every one of the 40 operations in
  a real third-party specification carried a tag that nothing read, which is what the
  completeness sweep exists to surface.
- `api_semantic_ir` schema version raised to `0.6.0`.

### Evaluation

- **Tool selection is measured.** A run records the operations the agent reached for and the
  proportion of its calls that selected an operation the task permits. Measured against the
  permitted set the task declares, never against the reference solution: scoring an agent on
  how closely it retraced an annotator's route is the defect that made an earlier corpus
  unusable.
- The rate is `null` when a task rules nothing out, because a rate over an unstated constraint
  would be 1.0 for every agent on every task, which reads like a measurement and is not. It is
  also 1.0 by construction under the replay driver, and only carries information under the
  model-backed one.
- `evaluation_run` schema version raised to `0.2.0`.

### Resources

- **A reclassified read is now addressable.** The planner has always been able to propose that
  an addressable read become a resource, and code generation emitted a tool anyway, so the
  reclassification survived as far as the plan and was discarded at the last step.
- The surface records a `uri_template` whose placeholders are the operation's path parameters,
  and the generated server registers it with `@mcp.resource` rather than `@mcp.tool`. The
  scheme is the service identifier, so two surfaces mounted alongside each other cannot
  collide on a shared path.
- An operation whose inputs the address cannot express stays a tool, and so does every SOAP
  operation, since they are all a POST to one endpoint and what distinguishes them is the
  envelope.
- `mcp_tool_surface` schema version raised to `0.3.0`.

## 0.3.0

Released 2026-08-13. The generated server acts on the retry policy the manifest derives.

Minor rather than patch because a server emitted by 0.2.0 behaves differently: it made one
attempt regardless of policy, and now a `safe` tool retries a rate limit or a gateway failure.
No contract changed, so an artifact written by 0.2.0 still validates.

### Safety

- **The derived retry policy is now acted on.** The manifest computed `retry` and
  `idempotency_key_required` per tool and the generated server read neither, so `never`
  retried nothing only because nothing retried at all. That is the third value in a row that
  was derived, written into the artifact and acted on by nobody, after the confirmation time
  to live and the credential placement.
- A `safe` policy retries 429 and the gateway codes, and transport failures where nothing was
  answered. **500 is deliberately not retried**: it may mean the effect happened and the
  answer was lost, which is precisely what the policy exists to prevent. Client errors are
  never retried.
- A tool whose policy requires an idempotency key generates one per invocation and holds it
  across that invocation's retries. A fresh key per attempt would make every retry a new
  operation; a key derived from the arguments would make two deliberate identical calls
  collide.
- `Retry-After` is honoured over the backoff curve, because a service that names its window
  knows better than any curve here. Attempts are bounded at three, so a wedged upstream is
  reported rather than hammered.
- The SOAP path retries on the same transport codes and sends no idempotency key, because
  WSDL declares nothing equivalent and a header invented here would be honoured by nobody.

### Evaluation

- **A bodyless `POST` commands rather than creates.** A creation needs something to create
  from, so `POST /me/player/next` skips a track where `POST /playlists` makes a playlist. The
  old model invented a record for every skip, which the module's own docstring had listed as a
  known limitation. On the Spotify document it affects three operations.
- **Every derived effect names the rule that produced it and how much that rule is worth.** A
  result judged against a guess and one judged against a convention that always holds looked
  identical from the outside.
- `scripts/effect_coverage.py` reports the distribution over a real specification. On the 40
  operations of the Spotify document, 35 are modelled at 0.8 confidence or above and 5 below,
  which is the measurement the issue asked for rather than an assurance.

### Remote references

- **A specification with remote references now compiles, and ingestion still never reaches
  the network.** Those two facts are only compatible because fetching is a separate command:
  `vendor-refs` fetches each remote document once over HTTPS, pins it by digest and records a
  lock, and ingestion reads the pinned files, verifies them, and refuses anything the lock
  does not name.
- A reference the lock does not already name requires `--record`, and is refused **before
  anything is fetched**. Trusting a source should be a decision someone made rather than
  something that happened while a build ran.
- An upstream edit is visible rather than adopted: a document that now serves different bytes
  fails the vendor step, and bytes edited in the cache after locking fail at compile time with
  nothing loaded.
- Reproducibility is unchanged in kind. A compile needs the specification, the lock and the
  cache, and neither the network nor the clock. There is no time-based revalidation, because
  a cache that refreshed itself would reintroduce exactly the dependency this avoids.
- `--refs-lock` is accepted by every command that parses a document.

### Documentation

- The README and the guide now lead with what is demonstrated, and state what is not
  immediately after. Nothing about the four inconclusive comparisons was removed or softened;
  it was placed after the reader knows what the software does, because a null result about
  one component was reading as a verdict on all of it.

## 0.2.0

Released 2026-08-13. Two correctness fixes for generated servers, both of which affect
anyone running a server emitted by 0.1.0.

The minor rather than the patch position because the policy manifest contract changed: a
manifest written by 0.1.0 no longer validates, which is a break even though the reason for it
is a fix.

### Safety

- **A generated server could not authenticate to most services.** Every scheme was sent as
  `Authorization: Bearer`, whatever the document declared, so an API key in a service-named
  header or HTTP basic produced a 401 on every call. The compiler had parsed the correct
  placement and discarded it at the last step.
- The policy manifest now records `required_schemes` beside `required_scopes`, taken from the
  alternative least-privilege selection chose, and the emitted server places each credential
  where the specification said it goes: the named header, query parameter or cookie for an
  API key, base64 for HTTP basic, a bearer token for OAuth2. A scheme that cannot be placed
  sends nothing rather than something plausible.
- One environment variable per scheme rather than one per service, so a surface that
  legitimately needs two credentials can express that, and `serve` reports the variables a
  deployment must set instead of leaving them to be found through 401s.
- `policy_manifest` schema version raised to `0.3.0`.

## 0.1.0

First published release, on PyPI as `api-mcp-compiler`.

### Research validity

- **Three registered comparisons run and reported, all inconclusive.** Baseline 20 of 24 and
  semantic 21 of 24 on the repaired instrument, with one discordant pair in every run and the
  direction alternating. A registered prediction that repairing the store would favour the
  semantic arm was not confirmed.
- Earlier runs, before the instrument was repaired: both inconclusive, with the nominal
  direction reversing between them, the clearest available evidence that the one-task gap in
  either was noise. After correcting the corpus, baseline 17 of 24 and semantic 16 of 24.
- First registered comparison, on the uncorrected corpus: Baseline 13 of 24, semantic
  14 of 24, one discordant pair against a pre-registered threshold of six, p = 1.0. Recorded
  with the registration's digest on both runs so the result cannot be detached from the
  hypothesis it was produced under.

- A **pre-registration** contract, document and analysis. The hypothesis, corpus, arms, model,
  success definition, equal-budget conditions, primary test, threshold, falsification
  condition and the list of things that may not change afterwards are all fixed in a committed
  document before any model-backed run.
- The document is digested and an evaluation run records that digest, so a result cannot be
  attached to a hypothesis written after the fact, and a model cannot be swapped between arms
  without it showing as a mismatch.
- The registered test is implemented rather than described. With 24 paired tasks, McNemar's
  exact test at two-sided alpha 0.05 requires at least 6 discordant pairs all favouring the
  same surface. Fewer is pre-committed as inconclusive whatever the raw difference, which is
  the expected outcome at this corpus size.

### Benchmarks

- A 24-task corpus over the Spotify Web API, selected from RestBench by a rule fixed before
  any behaviour was inspected. Goals and specification are third-party and fetched; fixtures
  and oracles are authored here and committed in a sidecar pinned to each goal by digest, so
  an upstream reordering cannot silently attach an oracle to a different goal.

- Third-party benchmark documents are **fetched and verified, never stored here**. A manifest
  records the upstream URL pinned to a commit, an expected sha256, the licence and the
  attribution that would have to travel with the file if it were ever redistributed. Fetching
  downloads, verifies, and only then writes, so a mismatch refuses and leaves nothing behind.
  An unrecorded source is refused before any request is made. Certificate verification is
  never disabled.
- First run against a real third-party specification, the Spotify Web API from RestBench:
  40 operations parsed with no blocking ambiguities, 23 of 40 tools executable with the rest
  held for approval, and least-privilege scopes derived correctly from a real OAuth2 surface.

### Swagger 2.0

- **Swagger 2 documents are ingested**, translated at load into the OpenAPI 3 shape so no
  later stage learns there are two input formats. Servers are reassembled from `host`,
  `basePath` and `schemes`; body and formData parameters become a request body, with a file
  field forcing multipart; `definitions` move under components and their references are
  rewritten, never followed; `type: file` becomes a binary string; `collectionFormat` becomes
  style and explode; and OAuth2 flows are renamed to their OpenAPI 3 names.
- What has no equivalent is reported rather than guessed: an unknown flow or collection
  format, and an operation declaring both a body and form fields, which cannot both be the
  request body.
- Verified against the live Swagger 2 Petstore: 20 operations, no blocking ambiguities, 13
  tools executable, and a runnable MCP server emitted.

### Safety

- **A confirmation token expired nothing.** The policy manifest set `token_ttl_seconds` on
  every destructive tool, the emitter wrote it into the file, and the generated server kept
  confirmations in a set with no timestamps: a token issued once stayed valid for the life of
  the process. Both emitters now hold a deadline per token, refuse a lapsed one with a reason
  rather than silently asking again, and drop expired tokens so a long-running server does not
  accumulate them.
- **The generated governance logic is now executed by tests, not read.** Every existing test
  of the emitted server asserts against its source text, which is exactly why a value that was
  passed and ignored survived. The new tests load the emitted module with the SDK and HTTP
  client stubbed and check the decision that matters: an expired confirmation must not reach
  the upstream service.
- **A stale installed copy can no longer fake a green run.** `pip install .` beside an
  editable install wins the import and freezes the code under test, so the suite passes while
  describing a snapshot. It happened here. A test now asserts the package under test is this
  working tree.

### Evaluation

- **A bodyless `POST` commands rather than creates.** A creation needs something to create
  from, so `POST /me/player/next` skips a track where `POST /playlists` makes a playlist. The
  old model invented a record for every skip, which the module's own docstring had listed as a
  known limitation. On the Spotify document it affects three operations.
- **Every derived effect names the rule that produced it and how much that rule is worth.** A
  result judged against a guess and one judged against a convention that always holds looked
  identical from the outside.
- `scripts/effect_coverage.py` reports the distribution over a real specification. On the 40
  operations of the Spotify document, 35 are modelled at 0.8 confidence or above and 5 below,
  which is the measurement the issue asked for rather than an assurance.

### Remote references

- **A specification with remote references now compiles, and ingestion still never reaches
  the network.** Those two facts are only compatible because fetching is a separate command:
  `vendor-refs` fetches each remote document once over HTTPS, pins it by digest and records a
  lock, and ingestion reads the pinned files, verifies them, and refuses anything the lock
  does not name.
- A reference the lock does not already name requires `--record`, and is refused **before
  anything is fetched**. Trusting a source should be a decision someone made rather than
  something that happened while a build ran.
- An upstream edit is visible rather than adopted: a document that now serves different bytes
  fails the vendor step, and bytes edited in the cache after locking fail at compile time with
  nothing loaded.
- Reproducibility is unchanged in kind. A compile needs the specification, the lock and the
  cache, and neither the network nor the clock. There is no time-based revalidation, because
  a cache that refreshed itself would reintroduce exactly the dependency this avoids.
- `--refs-lock` is accepted by every command that parses a document.

### Documentation

- **A documentation site**, built from `guide/` with MkDocs Material and published to GitHub
  Pages on every push to `main`. Concepts, a command reference, the contract schemas, the SOAP
  path, and how evaluation and pre-registration work. The published guide lives in `guide/`
  rather than `docs/`, which the repository already uses for working notes.
- **`scripts/check_docs.py` runs in the gate.** It holds prose to plain ASCII, and checks the
  command reference against the Typer application in both directions, so a command nobody
  documented and a documented command that no longer exists both fail the build.
- Packaging metadata for publication: author, keywords, classifiers, project URLs and a
  `CITATION.cff` that GitHub renders as a citation block.

### Teaching artifact

- **A notebook that walks a specification to a governed server with the values visible.**
  Provenance on a field pointed at the pointer it came from, a rename and a projection with
  their rationale and confidence, a scope chosen as the narrowest of three declared
  alternatives rather than their union, the emission gate holding a destructive tool, the
  review decision that releases it, and the policy compiled into the emitted call site.
- **Its outputs are verified, not trusted.** A notebook is documentation that looks like
  evidence: a reader cannot tell a digest that is current from one that was current a year
  ago. `scripts/check_notebook.py` re-executes every cell in the gate and fails on any
  difference, and `regen_golden.py` refreshes the notebook alongside the golden artifacts so
  one command produces the whole diff to review.

### Packaging

- **The distribution shipped none of the contract schemas.** They were kept at the repository
  root, so every check passed against the source tree while an installed copy could not
  validate a single artifact it produced. They are now package data inside
  `api_mcp_compiler/schemas/`, and `schema_dir()` resolves against the package rather than
  against a directory two levels up.
- **The verification gate now builds the wheel and the sdist and exercises them.** It reads
  both archives for every declared schema, unpacks the wheel elsewhere and makes it validate
  an artifact with no source tree on the path, then makes it reject an invalid one, since a
  validator that accepted everything would pass the first half. Building happens from a clean
  copy of the source: setuptools reuses `build/`, and the first version of this check
  certified a wheel built from a stale tree after the declaration that produced it was
  deleted.

### Fixed

- **A request body offered in several media types was unbuildable.** It produced one input per
  media type, all named `body`, and the tool refused to compose. This affected OpenAPI 3
  documents equally; translating a real Swagger 2 document is only what made it visible.

### SOAP

- **Verified end to end against live third-party services.** Two public document/literal SOAP
  services were fetched, classified by a reviewer, compiled and served, and the generated
  server made real calls: `4207` came back as "four thousand two hundred and seven", and
  `"a<b & c>d"` round-tripped through the envelope intact.
- Two defects only a real service could show. A document body carries the element the message
  part *references*, not the part's own name. Using the part name produced a fault from
  every real service, while every fixture here would have passed. And redaction removed a
  service's answer because the field name contained "token", where it meant a delimiter.

- **A SOAP service can now be served.** `serve` emits an MCP server that posts a SOAP 1.1
  envelope to the service endpoint, in document or rpc shape as the binding declares, carrying
  the SOAPAction and target namespace the specification named. Faults are reported as faults
  rather than transport errors, arguments are XML-escaped, and policy travels with the tool as
  it does over HTTP.
- **A reviewer can now record a side effect.** WSDL carries no signal equivalent to an HTTP
  method, so the compiler required a classification and gave nobody a way to express it: every
  SOAP operation was blocked with no route to unblocking it. The overlay carries the decision,
  and it is recorded as a human decision with the operation it applies to.

- **RPC bindings are ingested.** RPC and document differ in how the body is shaped, not in
  what the parameters are, so the tool schema is derivable either way. Refusing the style
  discarded documents whose surface was perfectly describable.
- **Section 5 encoding is what actually blocks**, and it now says so. Encoded bodies serialise
  values as a reference graph, which this compiler does not write, so the operation is
  described in full and cannot be served. That is a transport limit, reported as one, rather
  than a description limit.

- **XSD types resolve into JSON Schema.** Until this existed every WSDL was blocked and
  nothing SOAP could reach a tool schema. Measured over 40 third-party WSDL documents:
  unresolved types fell from 88 to 7, and 37 documents gained both an input and an output
  schema where none had either.
- The translation is deliberately narrow. A choice, an `xsd:any`, a named group, a
  self-referential type and a union are each reported as an ambiguity rather than
  approximated, because a tool whose schema quietly disagrees with the service is worse than
  one that says what it could not express. An imported schema is recorded, never fetched.
- A type that resolves with a caveat no longer blocks: the schema is usable and the caveat
  says what it does not capture. Only a type that produced no schema at all still does.

### Composition measured

- **The distinctive claim has been tested and did not show.** On TMDB, whose every goal is a
  lookup-then-use chain, baseline and semantic both scored 28 of 34 with **zero** discordant
  pairs. The agent reached for a composite on 29 of 34 tasks and used 18 of the 29 offered.
- Calls fell 7.4 percent, so the registration's second falsification condition, equal success
  with no reduction in calls, is not met. Composition changed what the agent did without
  changing what it achieved.
- Context bytes rose 20 percent: a composite returns the last step's payload where the
  baseline agent often stopped at the smaller one it needed.

### Held-out benchmark

- TMDB fetched and verified. **54 operations, every one a read**, which forced the composite
  rule to stop asking about side effects: needing a value the goal cannot supply is a property
  of the route. A companion constraint, that a composite must begin with something a goal can
  reach on its own, keeps the read-to-read rule from proposing every detail endpoint against
  every other.
- `read_goals_only` drops annotated solution paths inside the loader, so a composition rule
  cannot be fitted to them and no reviewer has to take a promise on trust.
- A 34-task corpus authored from those goals alone. Every oracle asserts the answer rather
  than the route, and the build checks that no oracle pins an operation or names a field, and
  that every asserted answer is present in its own fixture.
- A composite may now **supersede its steps** rather than joining them, when a reviewer says
  so. It defaults to joining, because approving a tool should never silently remove others.

### Composition, executable

- **A composite runs as one call.** The harness performs its steps in order and records a
  single trace step; charging them separately would bill the surface for the coupling
  composing removed. The emitter writes one tool that makes several requests and stops at the
  first failure rather than acting on a resource an earlier step did not create.
- **A threaded argument leaves the caller's schema and keeps its binding.** A composite exists
  because the value cannot come from the goal, so asking for it would restore the coupling.
  `create_playlist_with_tracks` takes no `playlist_id` and still sends one.
- **Two steps may each carry a body.** A flat schema cannot say that, so the gate was refusing
  composites of two writes, which are the interesting case, as an argument collision. The later
  argument is now qualified by its step, and `ArgumentBinding` records `source_operation` so
  each value reaches the right request.
- Confirmation is taken once, against the composite's own arguments.

### Planning

- **Argument projection.** Optional arguments that declare a default, or that carry transport
  rather than task concerns, are withheld from the agent's input schema and left off the wire
  so the service applies its own value. On the benchmark API this halves what an agent must
  reason about: **92 arguments become 46**. A required argument is never withheld, so a
  projection cannot make a call invalid.
- **Description rewriting.** Descriptions are rewritten for the audience that reads them:
  a model inside a tool list, which cannot follow a link and pays for every token. Source-site
  links and markup are dropped, prose is cut to what the call does, and the side effect is
  stated in the text: a destructive tool now says so where the model is actually looking.
- Both are recorded as decisions with a rationale, and the source text stays unchanged in
  the IR.

### Planning

- **Composite proposals from route structure.** A write whose route carries a templated
  identifier cannot be called from a goal alone: the value has to come from a read, and the
  route names which resource. Pairing the write with the read that yields that resource uses
  only what the specification states, and needs no reference to how anybody solved a task.
- On the benchmark API it proposes six pairs; on this repository's own fixtures it proposes
  none, which is the check that matters. It is no more shaped to the examples written here
  than to the benchmark's annotated paths.
- It complements rather than replaces the existing action-verb rule: that one fires on the
  enterprise approve-then-commit shape and not on a consumer API, this one the other way round.
- Composites remain proposals. Nothing composes without a reviewer recording it.

### The human path

- **`report`** writes the conversion report a reviewer actually reads: one self-contained HTML
  file saying what was read, what is proposed, what the gate is holding, and what needs a
  decision. **Reports are never overwritten.** A decision made against one set of proposals is
  not evidence about a different set, so each run writes a new file named for the source
  digest it describes.
- **`approve`** records approval for a class of tools and writes the overlay, so nobody
  hand-edits JSON. A selection must name what it covers, by risk, by group or by name; there is
  deliberately no flag that approves a surface without saying what class of thing it is. An
  existing overlay is extended rather than replaced, and what the selection did not cover is
  reported back.
- The intended path is `report`, `approve`, `serve`. The project instructions now say so, and
  say why the granularity of a gate is a safety property rather than a convenience: a reviewer
  clicking through twenty-three read tools individually is doing data entry, not governance.

### Code generation

- **A runnable MCP server.** `serve` writes a Python module that registers the approved
  surface over MCP and calls the upstream service. Only tools that cleared the emission gate
  are registered; the rest are named by a `surface://withheld` resource with the reason, so a
  deployment cannot pick up the tools and leave the decision behind.
- Policy is written into the server rather than documented beside it: arguments validated
  before the request is made, confirmation bound by digest to the exact arguments, output
  ceilings, and redaction. Credentials are read from the environment and never written into
  the generated file.
- The generated module needs `mcp` and `httpx`. The compiler depends on neither.

### Safety

- **Call budgets are enforced rather than declared.** Policy has always scaled calls per
  minute, concurrency and a daily budget by risk, and the generated server counted nothing, so
  a destructive tool with a budget of two calls a minute would make two hundred. Fourth in a
  row of the same shape after the confirmation time to live, the credential placement and the
  retry policy.
- Over budget, a call is refused rather than queued, and the refusal names the limit, the
  number allowed and when it lifts. A queued call looks to an agent like a slow service; a
  refusal it can reason about is more useful than a wait it cannot see.
- A call that never reaches the service spends nothing: validation and the confirmation gate
  both run before the budget is taken. The concurrency slot is returned on every path out,
  including a failed composite step and a truncated response.
- The counting is per process, and the guide says so. A deployment running several workers
  needs a shared counter, which a generated artifact cannot be.

### Planning

- **Grouping follows the tags a document declares**, falling back to the first path segment
  only where none exist. A declared grouping is a source fact where a path prefix is an
  inference, and the confidence recorded for the decision says so.
- Operation `tags` are now kept in the IR rather than swept. Every one of the 40 operations in
  a real third-party specification carried a tag that nothing read, which is what the
  completeness sweep exists to surface.
- `api_semantic_ir` schema version raised to `0.6.0`.

### Evaluation

- A bulk delete naming identifiers now removes those and leaves the rest. It previously
  cleared the whole collection, so an agent asked to remove one track was scored against a
  store that removed all of them.
- Merged multi-run results keep per-run outcomes and report against a run that failed, so a
  task recorded as failed can no longer show every oracle passing.
- Three corpus checks run in the build: no oracle may assert a record identifier the agent
  could not know, pin the operation a goal must be reached by, or count records in a
  collection an agent writes into.

- A **model-backed driver**. It sees the goal and the tools and nothing else, never the
  oracles, the fixture, or the reference solution. Both arms get the same model, decoding
  settings, system prompt, budget and starting state; the only difference permitted is the
  tool list.
- The driver protocol is now **turn-based**. It previously asked for a whole plan before any
  call ran, which no agent can supply: a goal like "add the first track of an artist's newest
  album to a playlist" cannot name the track until a lookup has returned it.

### Fixed

- **Generated tool schemas were never checked for being valid schemas.** The compiler
  validated arguments *against* them but never validated them, and the source specification
  writes `"maximum": "50"` and `"additionalProperties": "true"` as strings, so **22 of 40
  tools in both arms carried schemas the API rejects outright**. Schema keywords are now
  interpreted like any other source value, and a composed schema that is still invalid is
  refused rather than emitted.

- **Boolean fields were coerced rather than interpreted.** A real specification writes
  `"required": "false"` as a string, and `bool("false")` is `True`, so **every optional
  parameter of the benchmark API became required**: 92 of 92 inputs, where the truth is 31.
  A generated tool would have demanded values the service never wanted. Booleans are now
  interpreted strictly, a string that spells a boolean is accepted and reported as a
  `malformed_boolean` ambiguity, and anything else defaults to false and is reported.
- A singleton `PUT` mutated nothing, and arguments outside a request body never reached the
  stored record, so a value like `volume_percent` could not be asserted on.
- The evaluation store keyed collections by the last path segment, so `/me/tracks` and
  `/playlists/{id}/tracks` were the same collection and a final-state assertion could not
  tell saving a track to a library from adding one to a playlist.
- A `PUT` with no record identifier created a record instead of setting a singleton.
- Reads returned nothing from the store even when the fixture held the answer, so no
  retrieval oracle could pass regardless of what an agent did.
- Name derivation split possessives, turning "Get an Artist's Albums" into
  `get_artist_s_albums`. Single-letter tokens are now dropped. Found only by running against
  a specification nobody here wrote.


### Safety

- **Call budgets are enforced rather than declared.** Policy has always scaled calls per
  minute, concurrency and a daily budget by risk, and the generated server counted nothing, so
  a destructive tool with a budget of two calls a minute would make two hundred. Fourth in a
  row of the same shape after the confirmation time to live, the credential placement and the
  retry policy.
- Over budget, a call is refused rather than queued, and the refusal names the limit, the
  number allowed and when it lifts. A queued call looks to an agent like a slow service; a
  refusal it can reason about is more useful than a wait it cannot see.
- A call that never reaches the service spends nothing: validation and the confirmation gate
  both run before the budget is taken. The concurrency slot is returned on every path out,
  including a failed composite step and a truncated response.
- The counting is per process, and the guide says so. A deployment running several workers
  needs a shared counter, which a generated artifact cannot be.

### Planning

- **Grouping follows the tags a document declares**, falling back to the first path segment
  only where none exist. A declared grouping is a source fact where a path prefix is an
  inference, and the confidence recorded for the decision says so.
- Operation `tags` are now kept in the IR rather than swept. Every one of the 40 operations in
  a real third-party specification carried a tag that nothing read, which is what the
  completeness sweep exists to surface.
- `api_semantic_ir` schema version raised to `0.6.0`.

### Evaluation harness

**Fixed before release:** a read task could be passed by an agent that made no calls at all.
Every read-side oracle was negative, asking only whether something bad had happened, so an
idle agent satisfied all of them. A positive `retrieval` oracle now checks that the
information asked for was actually returned, evaluated against the recorded response rather
than by a judge, and the contract refuses a retrieval oracle carrying no assertion.

- New contracts: evaluation corpus `0.2.0` and evaluation run `0.1.0`. The previous
  single-task schema is removed; it had no envelope, so nothing recorded which service a task
  addressed or which revision it was written against.
- **Tasks name source operations, never tool names**, so one corpus can score both the
  baseline and the semantic surface. The harness resolves each operation to whichever tool a
  surface exposes, and reports an operation a surface omits rather than skipping it.
- **A stateful mock service**, seeded per task and discarded afterwards, so success can be
  judged by the state the service ends in rather than by whether the trace looked right.
- **Deterministic oracles** for final state, absence of mutation, prohibited operations and
  confirmation adherence. No oracle consults a model, and none can.
- **Evaluation traces** recorded separately from audit events, since an audit event carries
  digests and never values while an evaluator needs the values.
- Metrics for calls, unnecessary calls, unmapped operations, argument validity, unsafe
  actions, confirmation failures and context size. Latency and token cost are present and
  null, because neither has a source without a model in the loop.
- CLI gains `evaluate`.

**This phase produced no comparison and no findings, deliberately.** The only driver replays
the solution a task records, so it is correct by construction and scores every surface
identically; a test asserts exactly that. Nothing here is evidence about surface quality.


### Security and governance synthesis

- Policy manifest raised to `0.2.0` with Python models, a producer and golden artifacts. It
  was previously the only contract in the repository that nothing generated.
- **Data sensitivity** is a separate axis from the side-effect class, carrying public,
  internal, confidential, personal and financial. A read of financial data is still
  financial, so folding the label into read/write/destructive would hide it on exactly the
  operations that leak such data.
- **Least privilege.** Security requirement alternatives are now kept separately in the IR
  (`0.5.0`) rather than only as their union, and policy selects the narrowest requirement
  that grants access. A scopeless credential such as an admin key is not treated as
  narrowest, because it looks smallest by count while granting the most.
- **Two-call confirmation.** A prepare call issues a token naming the effect, bound to a
  digest of the arguments; the execute call refuses without it. Confirming one action cannot
  authorise another. This is what lifts the blocker on a composite spanning a change.
- **Fail closed.** A tool whose policy cannot be derived is disabled with `policy_unresolved`
  rather than emitted with defaults.
- **Output ceilings and redaction** enforced at invocation. Exceeding the ceiling returns a
  structured refusal rather than truncated data.
- **Structured audit events** that record digests of arguments and never their values, so
  enabling auditing cannot become a way of logging the data policy protects.
- Retry policy derived from inferred idempotency; rate and concurrency budgets scaled by
  risk; writes excluded from production environments by default.
- CLI gains `policy`; `generate` gains `--enforce-policy`.

### Fixed

- Duplicate mapping keys were silently dropped by both `yaml.safe_load` and `json.loads`,
  so a specification with two `get` entries under one path lost one with no record. This
  defeated the completeness sweep, because the dropped key never reached the parser to be
  swept. Both formats now refuse the document and name the key and line.


### Semantic tool-surface planning

- New contract `tool_overlay.schema.json` at `0.1.0`. The overlay is where human decisions
  live, and it is what makes semantic judgement compatible with deterministic regeneration:
  the planner proposes, the overlay records what was accepted, and rebuilding is a pure
  function of specification plus overlay. It is digest-bound, so decisions made about other
  bytes are refused rather than silently applied.
- API Semantic IR to `0.4.0` (adds `route`); tool plan to `0.3.0` (adds `decisions`, and
  `group` and `output_fields` on an artifact).
- **Semantic planner.** Task-oriented names derived from summaries rather than operation
  identifiers, surface-kind selection across tool and resource, grouping by path prefix,
  output projection, omission proposals for deprecated operations, and prepare-then-execute
  composite proposals.
- **Decision records.** Every rename, reclassification, omission, grouping, projection,
  composite and approval carries a rationale, a confidence and provenance. A planner
  decision must sit below confidence 1.0 and a recorded human decision at exactly 1.0.
- **Agent-suitability scoring** on plan artifacts, as an average of named readiness signals
  rather than an opaque number, with the missing signals listed.
- **Human review report**, rendered deterministically as Markdown. This is the artifact the
  approval gate depends on.
- Composites are represented end to end and refused executable with a named blocker until
  confirmation semantics exist.
- CLI gains `review` and `overlay-restamp`; `plan` and `generate` gain `--planner` and
  `--overlay`.


### Baseline tool-surface generation

- New contract `mcp_tool_surface.schema.json` at `0.1.0`, with `ToolSurface` and
  `ToolDescriptor` models. The surface binds to no MCP SDK and performs no I/O, because the
  SDK ordering constraint requires stable policy models that do not exist yet.
- **Emission gate.** A tool is executable only when its source operation carries no blocking
  ambiguity, its risk is classified, and any write, destructive or privileged artifact has
  been approved. Refused tools are still emitted carrying their blockers, so the surface
  stays auditable. The contract itself rejects a descriptor that is executable while
  carrying blockers, or disabled without a reason.
- **Flat input schemas.** One property per argument whatever its wire location, with
  `additionalProperties: false`, plus explicit argument bindings recording how each value
  returns to the wire. An agent produces a flat argument object and should not have to reason
  about transport.
- **Deterministic mock runtime.** Responses are synthesised from declared response schemas,
  seeded so runs are byte-identical, and a declared example is always preferred. Invocation
  refuses disabled tools, unknown tools, and arguments that fail the input schema.
- A generated surface cannot be built from a plan whose `source_digest` disagrees with the
  IR, so a plan reviewed against one revision cannot be applied to another.
- CLI gains `generate`; `validate` now also validates the surface and lists disabled tools.

### OpenAPI ingestion

- API Semantic IR raised to `0.3.0`. The tool plan contract is unchanged and stays at
  `0.2.0`; the two are versioned independently.
- **Reference resolution.** Local pointers always resolve. External files resolve only
  inside an explicitly allowed directory, checked against real paths so `../` cannot escape.
  Remote references are refused unconditionally and never fetched. A recursive schema keeps
  its innermost `$ref` and is reported rather than rejected; an over-deep chain raises.
- **Completeness sweep.** Every key an adapter does not consume is now reported as
  `unconsumed_key`, or `vendor_extension` for `x-` prefixed keys. "Nothing is dropped
  silently" is a structural guarantee rather than a per-construct promise.
- **Multi-document provenance.** `ServiceIR.source_documents` records the URI, digest and
  role of every document loaded, so reproducibility does not weaken when refs span files.
- **Typed authentication.** `AuthSchemeIR` carries a closed type with conditional field
  validation. `AuthRequirementIR` replaces `required_scopes` and distinguishes an explicit
  `security: []`, which disables authentication, from no security declared at all.
- **Language-based side-effect escalation.** Whole-token destructive verbs in an
  `operationId` or `summary` raise a write to destructive, carrying their own provenance
  record. Escalation never lowers a class, and a read described destructively is flagged as
  a blocking conflict rather than reclassified.
- **Pagination hints.** Cursor, page-number, offset-limit and link-header shapes are
  proposed from parameter, response-field and header evidence, always `inferred`.
- **Coverage.** Parameter `content`, `style`, `explode`, `allowReserved` and `deprecated`;
  response headers and examples; operation `deprecated`; path-level and operation-level
  servers; the declared spec version.

### Fixed

- The parser raised on any OpenAPI parameter that omitted `required`, which is legal and
  normal for query parameters. Provenance for the defaulted value was not emitted, so the
  contract rejected the instance. Every committed fixture happened to declare `required`
  explicitly, so the case was untested.
- `README.md` and the OpenAPI adapter docstring claimed "nothing is dropped silently" when
  the guarantee held only for recognized-but-unresolvable constructs.

### Contracts

- API Semantic IR and tool plan schemas raised to `0.2.0`. Breaking: typed inputs, outputs
  and faults replace free-form objects, per-field provenance is required, and the version
  is pinned with `const` so a document written against another version fails loudly.
- Split `SideEffectClass` (IR operations) from `RiskClass` (plan artifacts). Only the plan
  may express `privileged`, and the baseline planner never assigns it.
- Added `ToolPlan`, the previously missing Python model for the tool-plan document.
- Added `Ambiguity`: unresolved constructs are recorded rather than silently dropped, with
  a `blocking` flag that gates later code generation.
- Added `ServiceIR.source_digest`, tying every generated artifact to the exact
  specification bytes it was compiled from.

### Evaluation

- **A bodyless `POST` commands rather than creates.** A creation needs something to create
  from, so `POST /me/player/next` skips a track where `POST /playlists` makes a playlist. The
  old model invented a record for every skip, which the module's own docstring had listed as a
  known limitation. On the Spotify document it affects three operations.
- **Every derived effect names the rule that produced it and how much that rule is worth.** A
  result judged against a guess and one judged against a convention that always holds looked
  identical from the outside.
- `scripts/effect_coverage.py` reports the distribution over a real specification. On the 40
  operations of the Spotify document, 35 are modelled at 0.8 confidence or above and 5 below,
  which is the measurement the issue asked for rather than an assurance.

### Remote references

- **A specification with remote references now compiles, and ingestion still never reaches
  the network.** Those two facts are only compatible because fetching is a separate command:
  `vendor-refs` fetches each remote document once over HTTPS, pins it by digest and records a
  lock, and ingestion reads the pinned files, verifies them, and refuses anything the lock
  does not name.
- A reference the lock does not already name requires `--record`, and is refused **before
  anything is fetched**. Trusting a source should be a decision someone made rather than
  something that happened while a build ran.
- An upstream edit is visible rather than adopted: a document that now serves different bytes
  fails the vendor step, and bytes edited in the cache after locking fail at compile time with
  nothing loaded.
- Reproducibility is unchanged in kind. A compile needs the specification, the lock and the
  cache, and neither the network nor the clock. There is no time-based revalidation, because
  a cache that refreshed itself would reintroduce exactly the dependency this avoids.
- `--refs-lock` is accepted by every command that parses a document.

### Documentation

- **A deployment page for the controls a generated artifact cannot provide.** The manifest has
  always recorded server-side authorization, confused-deputy exposure and identity propagation
  as requirements it never reports as satisfied, which is honest and, alone, useless. The
  guide now says what to do about each.
- It also states the limits on the controls the artifact does enforce: call budgets are counted
  per process, so four replicas are four budgets, and confirmation tokens live in memory, so a
  restart forgets outstanding ones.

### Ingestion

- OpenAPI: request bodies, path-item parameter inheritance with operation override,
  response and fault separation, servers, security schemes and required scopes. Request
  bodies and path-item parameters were previously dropped entirely.
- OpenAPI: source pointers are now RFC 6901 JSON Pointers with correct escaping. The
  previous form was ambiguous for any templated path.
- WSDL: ports, bindings, style, transport, SOAPAction, endpoint addresses and message
  parts. The previous adapter read operation names only.
- WSDL: WSDL 2.0 and malformed documents are rejected explicitly instead of returning an
  empty operation list; external entity resolution, network access and DTD loading are
  disabled.
- Method-derived side-effect and idempotency classes are marked `inferred` with confidence
  below 1.0, so they are distinguishable from source facts. SOAP operations are never
  classified by inference and carry a blocking ambiguity until a human classifies them.

### Tooling

- `scripts/verify_repo.py` runs every check and reports all failures, and now covers
  `scripts/` with ruff and mypy plus a new example schema-validation step. It previously
  exited 1 on a clean checkout.
- Added `scripts/validate_examples.py` and `scripts/regen_golden.py`.
- Pinned ruff and mypy to exact versions and declared an explicit lint rule selection, so
  the gate does not change meaning between machines.
- CLI: added `plan` and `validate` commands alongside `inspect`.

## Unreleased

### Fixed

- **A WSDL message with no parts is a void response, not an unresolved type.** WSDL permits an
  empty message and services use it the way HTTP uses 204. Reading it as unresolved blocked
  the operation, so two of the forty documents in the third-party collection were refused for
  declaring their response empty clearly. Found by asking the completeness sweep what it was
  still reporting, rather than by guessing where to look.

## 0.5.0

Released 2026-08-13. Remote references resolve without ingestion ever reaching the network, an
operation that only accepts work says so, grouping follows the tags a document declares, and
the evaluation store reports which of its effects are guesses.

Minor rather than patch because the IR contract gained fields: an IR written by 0.4.0 no
longer validates. Surfaces, plans, policy manifests and evaluation runs are unchanged.

### Evaluation

- **A bodyless `POST` commands rather than creates.** A creation needs something to create
  from, so `POST /me/player/next` skips a track where `POST /playlists` makes a playlist. The
  old model invented a record for every skip, which the module's own docstring had listed as a
  known limitation. On the Spotify document it affects three operations.
- **Every derived effect names the rule that produced it and how much that rule is worth.** A
  result judged against a guess and one judged against a convention that always holds looked
  identical from the outside.
- `scripts/effect_coverage.py` reports the distribution over a real specification. On the 40
  operations of the Spotify document, 35 are modelled at 0.8 confidence or above and 5 below,
  which is the measurement the issue asked for rather than an assurance.

### Remote references

- **A specification with remote references now compiles, and ingestion still never reaches
  the network.** Those two facts are only compatible because fetching is a separate command:
  `vendor-refs` fetches each remote document once over HTTPS, pins it by digest and records a
  lock, and ingestion reads the pinned files, verifies them, and refuses anything the lock
  does not name.
- A reference the lock does not already name requires `--record`, and is refused **before
  anything is fetched**. Trusting a source should be a decision someone made rather than
  something that happened while a build ran.
- An upstream edit is visible rather than adopted: a document that now serves different bytes
  fails the vendor step, and bytes edited in the cache after locking fail at compile time with
  nothing loaded.
- Reproducibility is unchanged in kind. A compile needs the specification, the lock and the
  cache, and neither the network nor the clock. There is no time-based revalidation, because
  a cache that refreshed itself would reintroduce exactly the dependency this avoids.
- `--refs-lock` is accepted by every command that parses a document.

### Documentation

- **A deployment page for the controls a generated artifact cannot provide.** The manifest has
  always recorded server-side authorization, confused-deputy exposure and identity propagation
  as requirements it never reports as satisfied, which is honest and, alone, useless. The
  guide now says what to do about each.
- It also states the limits on the controls the artifact does enforce: call budgets are counted
  per process, so four replicas are four budgets, and confirmation tokens live in memory, so a
  restart forgets outstanding ones.

### Ingestion

- **An operation that only accepts work now says so.** HTTP 202 means the request was taken,
  not carried out, and a surface silent about that lets an agent read acceptance as completion
  and report a goal met before anything has happened. The IR records an `async_job`, and the
  planner states it in the tool description, which is where a model looks. One of the shipped
  examples turns out to be exactly this case.
- A `Location` header on that response is recorded as the poll target, marked inferred rather
  than source. Where a document declares acceptance and names nowhere to look, that is
  reported as such rather than filled in with a convention nobody promised.
- **`info.description` and `info.termsOfService` are kept rather than swept.** The description
  becomes the generated server's instructions, capped in length, so an agent is told the
  domain it works in instead of inferring it from tool names.
- `api_semantic_ir` schema version raised to `0.7.0`.

## 0.4.0

Released 2026-08-13. Call budgets are enforced rather than declared, grouping follows the
tags a document declares, a reclassified read is addressable, and tool selection is measured.

Minor rather than patch because three contracts moved: an IR, a surface or an evaluation run
written by 0.3.0 no longer validates. The policy manifest is unchanged.

### Safety

- **Call budgets are enforced rather than declared.** Policy has always scaled calls per
  minute, concurrency and a daily budget by risk, and the generated server counted nothing, so
  a destructive tool with a budget of two calls a minute would make two hundred. Fourth in a
  row of the same shape after the confirmation time to live, the credential placement and the
  retry policy.
- Over budget, a call is refused rather than queued, and the refusal names the limit, the
  number allowed and when it lifts. A queued call looks to an agent like a slow service; a
  refusal it can reason about is more useful than a wait it cannot see.
- A call that never reaches the service spends nothing: validation and the confirmation gate
  both run before the budget is taken. The concurrency slot is returned on every path out,
  including a failed composite step and a truncated response.
- The counting is per process, and the guide says so. A deployment running several workers
  needs a shared counter, which a generated artifact cannot be.

### Planning

- **Grouping follows the tags a document declares**, falling back to the first path segment
  only where none exist. A declared grouping is a source fact where a path prefix is an
  inference, and the confidence recorded for the decision says so.
- Operation `tags` are now kept in the IR rather than swept. Every one of the 40 operations in
  a real third-party specification carried a tag that nothing read, which is what the
  completeness sweep exists to surface.
- `api_semantic_ir` schema version raised to `0.6.0`.

### Evaluation

- **Tool selection is measured.** A run records the operations the agent reached for and the
  proportion of its calls that selected an operation the task permits. Measured against the
  permitted set the task declares, never against the reference solution: scoring an agent on
  how closely it retraced an annotator's route is the defect that made an earlier corpus
  unusable.
- The rate is `null` when a task rules nothing out, because a rate over an unstated constraint
  would be 1.0 for every agent on every task, which reads like a measurement and is not. It is
  also 1.0 by construction under the replay driver, and only carries information under the
  model-backed one.
- `evaluation_run` schema version raised to `0.2.0`.

### Resources

- **A reclassified read is now addressable.** The planner has always been able to propose that
  an addressable read become a resource, and code generation emitted a tool anyway, so the
  reclassification survived as far as the plan and was discarded at the last step.
- The surface records a `uri_template` whose placeholders are the operation's path parameters,
  and the generated server registers it with `@mcp.resource` rather than `@mcp.tool`. The
  scheme is the service identifier, so two surfaces mounted alongside each other cannot
  collide on a shared path.
- An operation whose inputs the address cannot express stays a tool, and so does every SOAP
  operation, since they are all a POST to one endpoint and what distinguishes them is the
  envelope.
- `mcp_tool_surface` schema version raised to `0.3.0`.

## 0.3.0

Released 2026-08-13. The generated server acts on the retry policy the manifest derives.

Minor rather than patch because a server emitted by 0.2.0 behaves differently: it made one
attempt regardless of policy, and now a `safe` tool retries a rate limit or a gateway failure.
No contract changed, so an artifact written by 0.2.0 still validates.

### Safety

- **The derived retry policy is now acted on.** The manifest computed `retry` and
  `idempotency_key_required` per tool and the generated server read neither, so `never`
  retried nothing only because nothing retried at all. That is the third value in a row that
  was derived, written into the artifact and acted on by nobody, after the confirmation time
  to live and the credential placement.
- A `safe` policy retries 429 and the gateway codes, and transport failures where nothing was
  answered. **500 is deliberately not retried**: it may mean the effect happened and the
  answer was lost, which is precisely what the policy exists to prevent. Client errors are
  never retried.
- A tool whose policy requires an idempotency key generates one per invocation and holds it
  across that invocation's retries. A fresh key per attempt would make every retry a new
  operation; a key derived from the arguments would make two deliberate identical calls
  collide.
- `Retry-After` is honoured over the backoff curve, because a service that names its window
  knows better than any curve here. Attempts are bounded at three, so a wedged upstream is
  reported rather than hammered.
- The SOAP path retries on the same transport codes and sends no idempotency key, because
  WSDL declares nothing equivalent and a header invented here would be honoured by nobody.

### Evaluation

- **A bodyless `POST` commands rather than creates.** A creation needs something to create
  from, so `POST /me/player/next` skips a track where `POST /playlists` makes a playlist. The
  old model invented a record for every skip, which the module's own docstring had listed as a
  known limitation. On the Spotify document it affects three operations.
- **Every derived effect names the rule that produced it and how much that rule is worth.** A
  result judged against a guess and one judged against a convention that always holds looked
  identical from the outside.
- `scripts/effect_coverage.py` reports the distribution over a real specification. On the 40
  operations of the Spotify document, 35 are modelled at 0.8 confidence or above and 5 below,
  which is the measurement the issue asked for rather than an assurance.

### Remote references

- **A specification with remote references now compiles, and ingestion still never reaches
  the network.** Those two facts are only compatible because fetching is a separate command:
  `vendor-refs` fetches each remote document once over HTTPS, pins it by digest and records a
  lock, and ingestion reads the pinned files, verifies them, and refuses anything the lock
  does not name.
- A reference the lock does not already name requires `--record`, and is refused **before
  anything is fetched**. Trusting a source should be a decision someone made rather than
  something that happened while a build ran.
- An upstream edit is visible rather than adopted: a document that now serves different bytes
  fails the vendor step, and bytes edited in the cache after locking fail at compile time with
  nothing loaded.
- Reproducibility is unchanged in kind. A compile needs the specification, the lock and the
  cache, and neither the network nor the clock. There is no time-based revalidation, because
  a cache that refreshed itself would reintroduce exactly the dependency this avoids.
- `--refs-lock` is accepted by every command that parses a document.

### Documentation

- The README and the guide now lead with what is demonstrated, and state what is not
  immediately after. Nothing about the four inconclusive comparisons was removed or softened;
  it was placed after the reader knows what the software does, because a null result about
  one component was reading as a verdict on all of it.

## 0.2.0

Released 2026-08-13. Two correctness fixes for generated servers, both of which affect
anyone running a server emitted by 0.1.0.

The minor rather than the patch position because the policy manifest contract changed: a
manifest written by 0.1.0 no longer validates, which is a break even though the reason for it
is a fix.

### Safety

- **A generated server could not authenticate to most services.** Every scheme was sent as
  `Authorization: Bearer`, whatever the document declared, so an API key in a service-named
  header or HTTP basic produced a 401 on every call. The compiler had parsed the correct
  placement and discarded it at the last step.
- The policy manifest now records `required_schemes` beside `required_scopes`, taken from the
  alternative least-privilege selection chose, and the emitted server places each credential
  where the specification said it goes: the named header, query parameter or cookie for an
  API key, base64 for HTTP basic, a bearer token for OAuth2. A scheme that cannot be placed
  sends nothing rather than something plausible.
- One environment variable per scheme rather than one per service, so a surface that
  legitimately needs two credentials can express that, and `serve` reports the variables a
  deployment must set instead of leaving them to be found through 401s.
- `policy_manifest` schema version raised to `0.3.0`.

## 0.1.0

- Initial build.
