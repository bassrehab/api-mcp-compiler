# Evaluation

The point of the evaluation harness is to answer one question honestly: does a semantically
planned surface do better than one tool per operation?

So far, four pre-registered comparisons say **no measurable difference**, and this page says so
before it says anything else.

## How a run is scored

A task names **source operations**, not tool names. That single choice is what lets one corpus
score both planners without favouring either, since a baseline tool and a semantic tool that
came from the same operation are recognised as the same capability under different names.

Success is decided by the state the service ends in, never by a model:

| Oracle | Checks |
|---|---|
| final state | The store ends in the state the goal asked for. |
| `no_mutation` | A read-only task left the store untouched. |
| prohibited operations | Nothing outside the task's remit was called. |
| confirmation adherence | A destructive call carried a valid confirmation. |

### Tool selection

A run also records which operations the agent reached for, and how many of its calls selected
an operation the task permits.

| Field | Meaning |
|---|---|
| `selected_operations` | The distinct operations the agent reached for, in the order it first reached for each. |
| `selection_rate` | Proportion of calls that selected an operation the task permits, or `null`. |

Two things about this metric are deliberate.

**It is measured against the permitted set the task declares, never against the reference
solution.** Scoring an agent on how closely it retraced an annotator's route is the defect that
made an earlier corpus unusable: a different route to the same outcome is a different route,
not a worse answer.

**It is null when the task rules nothing out.** A rate computed against an unstated constraint
would be 1.0 for every agent on every task, which reads like a measurement and is not.

It is a narrow metric on purpose. It reports reaching for a tool the task rules out, and says
nothing about reaching for too many, which `unnecessary_calls` already covers. Under the
deterministic replay driver it is 1.0 by construction, because the driver follows a recorded
path; it only carries information under the model-backed driver, where there is a selection to
score.

No safety or success number depends on a judge. Latency and token cost are recorded as `null`
under a deterministic driver rather than estimated, because a fabricated number sitting in the
same record as measured ones is indistinguishable from a measured one.

## The store is an approximation, and says which parts it is sure of

Success is judged against the state a mock service ends in, and that mock derives what each
call does from the route and the side-effect class. That is a REST-convention approximation. A
service that does not follow the conventions is modelled wrongly.

What changed is that it now says so per call. Every derived effect names the rule that
produced it and how much that rule is worth:

| Rule | Confidence | When |
|---|---|---|
| `read` | 1.0 | A read, which never mutates. |
| `identified` | 0.9 | A write on a route ending in a record identifier. |
| `destructive` | 0.9 | A destructive operation. |
| `put_singleton` | 0.85 | `PUT` with no record identifier, which replaces one thing. |
| `action_segment` | 0.8 | A write whose final segment is an action verb. |
| `collection_post` | 0.75 | A `POST` carrying a body to a collection root. |
| `bodyless_command` | 0.6 | A `POST` with no body, which commands rather than creates. |

`scripts/effect_coverage.py` prints the distribution for a specification, which is how "how
often does the approximation hold" stops being a question nobody can answer. On the 40
operations of the Spotify document: 35 are modelled at 0.8 or above, and 5 below.

A task whose success depends on a low-confidence call should state its expectation directly
rather than rely on the model. That escape hatch always existed and was only useful to someone
who knew which operations to distrust.

## Two drivers

**The replay driver** follows a recorded solution path. It is deterministic, needs no model,
and is what the committed golden evaluation artifacts use.

**The model-backed driver** sees the goal and the tools and nothing else, never the recorded
path. It is turn-based: the driver returns the next call given the trace so far, because no
agent can name an identifier that a lookup has not yet returned. Only executable tools are
offered, and parallel tool use is disabled so that one turn maps to one decision.

## Pre-registration

Before a model-backed comparison runs, a document fixes the hypothesis, corpus, arms, model,
success definition, equal-budget conditions, primary test, threshold, falsification condition,
and the list of things that may not change afterwards.

The document is digested. Every evaluation run records that digest, and the comparison refuses
to combine runs whose digests do not match. A result therefore cannot be attached to a
hypothesis written after the fact, and a model cannot be swapped between arms without the
mismatch showing.

Registrations are append-only. A superseded one is never edited or deleted, because the record
of what was believed beforehand is the only thing that makes a later result worth anything.

The primary test is McNemar's exact test, two-sided, alpha 0.05. On a 24-task corpus it needs
at least six discordant pairs all favouring the same arm. Fewer is pre-committed as
inconclusive whatever the raw difference.

## What the comparisons found

| Registration | Result |
|---|---|
| `spotify-002` | Inconclusive. Baseline 13 of 24, semantic 14 of 24, one discordant pair. |
| `spotify-003` | Inconclusive. Corrected harness and oracles, wider budget. Baseline 17 of 24, semantic 16 of 24, one discordant pair. |
| `spotify-004` | Inconclusive. Store honours paging and filtering. Baseline 20 of 24, semantic 21 of 24, one discordant pair. |
| `tmdb-002` | Inconclusive. Baseline 28 of 34, semantic 28 of 34, **zero** discordant pairs. |

The nominal direction reversed across the Spotify runs, which is the clearest available
evidence that a one-task gap is noise. The TMDB run is a stronger null still: the arms agreed
on every one of the 34 tasks.

That run also carried the composition claim. Calls fell 7.4 percent, from 312 to 289, the agent
reached for a composite on 29 of 34 tasks, and 18 of the 29 offered were used at least once. So
the registration's second falsification condition, equal success with **no** reduction in
calls, is not met. Composition changed what the agent did without changing what it achieved.
Context bytes rose 20 percent, because a composite returns the last step's payload while the
baseline agent often stopped at the smaller one it needed.

## What the runs did establish

Mostly facts about the instrument, and they were only findable by pointing the system at a
specification and a task set written by other people:

- A parser that marked every optional argument required.
- Generated schemas no JSON Schema validator would accept, on 22 of 40 tools, because the
  source writes `"maximum": "50"` as a string.
- A store in which a pure read mutated state, failing every `no_mutation` oracle.
- A bulk delete that emptied the collection it was asked to remove one item from.
- Oracles that scored the route an annotator took rather than the outcome the goal asked for.

Each is fixed, and the rules that would have prevented the last class now fail the build.

## The experiment that was declared not runnable

After four nulls, the honest next hypothesis is narrower: surface design may affect task
success only when the surface is hostile enough that a capable model cannot compensate for it.

Testing that needs a hostile API **that arrives with third-party goals**, since authoring goals
for a hostile API chosen afterwards would reintroduce the circularity that already cost this
project a corpus. A protocol was written first, fixing a hostility index computed from the
specification alone, a candidate set, a selection rule, and a threshold of 0.40 below which the
experiment would be declared not runnable.

The best available candidate scored 0.186, below both benchmarks that had already produced
nulls. The experiment was declared not runnable, which is the outcome the protocol committed to
reporting, before any tokens were spent.

## Running a comparison

```bash
.venv/bin/python -m api_mcp_compiler.cli evaluate SPEC CORPUS
```

The model-backed comparison lives in `scripts/run_comparison.py` and requires an API key in the
environment. It applies the overlay to the semantic arm only, since the overlay carries the
human decisions that arm is entitled to, and reports against a failing run rather than hiding
it.
