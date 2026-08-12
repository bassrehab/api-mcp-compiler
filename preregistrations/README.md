# Pre-registrations

A pre-registration states what a comparison is testing and how it will be judged, and is
written before the run that produces the numbers. Each file is digested, and every evaluation
run records the digest it was produced under, so a result cannot be attached to a hypothesis
written afterwards.

Registrations are append-only. A superseded one is never edited or deleted, because the record
of what was believed beforehand is the only thing that makes a later result worth anything.

| | Status | Why it was superseded |
|---|---|---|
| `spotify-baseline-vs-semantic-001` | superseded, never run | Written before the planner gained description rewriting and argument projection. The arms it described differed only in tool names. |
| `spotify-baseline-vs-semantic-002` | **run, result recorded** | Returned inconclusive: baseline 13 of 24, semantic 14 of 24, one discordant pair against a threshold of six. Diagnosing the failures showed the corpus setting the ceiling: a read that mutated the store, and oracles asserting the route rather than the outcome. Those could not be corrected under it, since it forbids re-authoring an oracle after results are seen. |
| `spotify-baseline-vs-semantic-003` | **run, result recorded** | Same hypothesis, corrected harness and oracles, wider call budget. Returned inconclusive: baseline 17 of 24, semantic 16 of 24, one discordant pair against a threshold of six. |

| `spotify-baseline-vs-semantic-004` | **run, result recorded** | Store honours paging and filtering. Baseline 20 of 24, semantic 21 of 24, one discordant pair. Inconclusive. The prediction that the harness change would favour the semantic arm was **not** confirmed: the nominal direction was semantic, but at one discordant pair that is the same non-evidence the two earlier runs produced. |

| `tmdb-baseline-vs-semantic-001` | **run, void** | Not a measurement. The model driver still refused to offer a composite, so every semantic episode crashed and that arm never ran. The fixtures keyed records by readable strings while the specification declares integer identifiers, so every chain was unsatisfiable and the baseline scored one task in thirty-four. The renamer was separately emitting ten tools called `get_details`, which the API refuses. |
| `tmdb-baseline-vs-semantic-002` | **run, result recorded** | Baseline 28 of 34, semantic 28 of 34, **zero** discordant pairs. Inconclusive on the primary metric. Calls fell 7.4 percent and the agent reached for a composite on 29 of 34 tasks, so the falsification clause is not met, but neither is the hypothesis supported. |

## What 002 established

The arms agreed on **every one of the 34 tasks**. Not one disagreement in either direction,
which is a stronger null than the Spotify runs produced.

The registration named a second way to fail: equal task success **with no reduction in calls**
would falsify the hypothesis, because a composite that saves nothing is a tool nobody needed.
That clause is not met. Calls fell from 312 to 289, the agent reached for a composite on 29 of
34 tasks, and 18 of the 29 offered were used at least once. Composition changed what the agent
did; it did not change what the agent achieved.

Reported as secondary and never promoted: on a per-task split the semantic arm used fewer
calls on 17 tasks and more on 9, which a two-sided sign test puts at p = 0.169. Suggestive of
a real reduction, not evidence of one, and the primary metric remains task success.

Context bytes rose 20 percent, because a composite returns the last step's payload while the
baseline agent often stopped at the smaller one it needed. That is a cost the surface pays for
collapsing the chain, and it is worth recording against the calls it saved.

## Why 001 is void rather than inconclusive

An inconclusive result is a measurement that did not separate the arms. 001 separated nothing
because one arm did not run and the other was scored against a corpus no agent could satisfy.
Reporting `0.029 against 0.0` as a finding would be reporting two bugs as evidence about a
surface.

The rule this project follows is that results may not be discarded, and crashes are not
results. The distinction is doing real work here, so it is worth stating where the line is: an
episode that raises before producing a trace, and a corpus whose answers are unreachable by
construction, are both failures of the instrument. A run that completes and disagrees with a
hypothesis is not, and no defect found afterwards may be used to set one aside.

## The harness change in 004, and its expected direction

Two runs found the arms indistinguishable, and the reason turned out not to be sample size.
The baseline agent supplied an argument the semantic surface removes 30 times in 48 eligible
calls, mostly `limit`, but the store ignored `limit` entirely, so setting it badly cost
nothing and withholding it saved nothing. The benefit projection exists to deliver could not
appear in an outcome, by construction.

The store now pages and filters as a service does. **This is expected to favour the semantic
arm**, and saying so before the run is the point: an expectation stated in advance is a
prediction, and the same sentence offered afterwards would be an excuse. The change is
symmetric, since an agent that pages too narrowly misses what it needed while one that cannot
page receives the service default, and both arms run against the same store.

## What three runs together say

| Run | Baseline | Semantic | Discordant pair |
|---|---|---|---|
| 002 | 13 of 24 | 14 of 24 | 1, favouring semantic |
| 003 | 17 of 24 | 16 of 24 | 1, favouring baseline |
| 004 | 20 of 24 | 21 of 24 | 1, favouring semantic |

Success rose from 13 to 20 and 14 to 21 as the instrument was repaired, which is the clearest
statement of how much of the early result was the corpus rather than the surfaces.

**Every run produced exactly one discordant pair, and the direction alternated.** Three
independent measurements, three different tasks disagreeing, no consistent winner. The arms
agree on 23 of 24 tasks every time. This is not a corpus too small to see a difference so much
as two surfaces that produce the same agent behaviour on this API.

The pre-committed response is therefore not a larger corpus. It is an API where surface design
has more to do.

## What the earlier runs together say

The corrections worked: both arms rose, baseline from 13 to 17 and semantic from 14 to 16,
so the earlier ceiling was the corpus rather than the surfaces.

Both runs are inconclusive, and **the nominal direction reversed between them**: semantic
ahead by one task in 002, baseline ahead by one in 003. A single discordant pair pointing a
different way each time is what no real difference looks like. Neither run licenses a claim
about which surface is better, and the reversal is the clearest evidence that the one-task
gap in either was noise.

## The revision in 003, stated plainly

The oracles were revised after 002 returned no difference. That is the move most likely to be
fitting a measurement to a desired answer, so what changed and why is recorded rather than
buried:

- Three oracles pinned the operation a goal had to be reached by. A goal asks for an outcome;
  an agent that answers "recommend me some tracks" by calling the recommendations endpoint,
  rather than the search endpoint the annotator used, is right and was marked wrong.
- Eight assertions demanded an identifier be spelled exactly. A service names the same
  playlist `pl-rock` and `spotify:playlist:pl-rock`, and the agent sending the fuller form is
  right and was marked wrong.

Both are errors on inspection. Neither was found by asking which change would improve the
result, and neither favours one arm: both arms were scored by the same wrong oracles, and both
are scored by the same corrected ones.
