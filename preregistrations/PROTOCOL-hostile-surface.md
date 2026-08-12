# Protocol: does surface design matter when the surface is hostile?

**Status: closed. The threshold fired and the experiment is not runnable.** See the outcome at the foot of this document. What follows is the protocol exactly as it was written before the index was computed; nothing in it has been revised.

**Original status: protocol, not yet a registration.** A `PreRegistration` requires the digest of the
specification it is about, and this one names a specification that has not been fetched. The
contract refuses a registration without those bytes, which is correct, so this document fixes
everything that can be fixed in advance and the registration follows when the specification is
in hand. Nothing here may be revised after the specification is read.

## Why this experiment exists, and the trap it has to avoid

Four pre-registered comparisons found no difference in agent task success between a
one-tool-per-operation surface and a semantically designed one. Three on Spotify, one on TMDB,
none close: the arms agreed on 23 of 24 tasks, then 23 of 24, then 23 of 24, then **34 of 34**.

Running a fifth after four nulls is how a null gets converted into a positive by selection. So
the hypothesis is narrower than the original and has to be, because it must explain the four
nulls rather than ignore them:

> **Surface design affects agent task success only when the surface is hostile enough that a
> capable model cannot compensate for it.**

Spotify and TMDB are clean, consistently named consumer APIs of 40 and 54 operations. A
capable model reads them unaided, which is the obvious reading of four nulls and is what this
hypothesis says. It is falsifiable in the direction that matters: if a hostile surface also
shows no difference, the design claim is dead rather than conditional, and this protocol
commits to reporting that.

## The candidate set, fixed now

**AppWorld** (StonyBrookNLP/appworld, Apache-2.0): nine simulated applications, roughly 457
APIs, shipping raw OpenAPI specifications *and* 750 human-authored tasks evaluated by outcome
rather than by trajectory.

The candidate set is the nine AppWorld application specifications and nothing else. It is
fixed here because a candidate set assembled after measuring is not a candidate set.

**Why the set is restricted to specifications that arrive with third-party goals.** A hostile
API is easy to find — Kubernetes, the AWS APIs, any large generated surface. None of them
comes with tasks, and authoring goals here would reintroduce exactly the circularity that made
the Spotify corpus unusable for judging composition. A hostile surface with goals I wrote is
not evidence about anything.

## Hostility, measured before any run

Hostility is a property of a specification, so it is computed from the specification alone and
can never see a result. The index is the mean of five components, each in [0, 1]:

| Component | Definition |
|---|---|
| Scale | `min(operations / 200, 1)` |
| Unnamed | fraction of operations whose summary is absent or equal to the operation identifier |
| Collision | fraction of operations whose derived name collides with another before disambiguation |
| Arguments | `min(median inputs per operation / 10, 1)` |
| Depth | `min(max schema nesting depth / 8, 1)` |

Spotify and TMDB are scored on the same index and reported alongside, so a reader can see what
this experiment varied relative to the four that found nothing.

## Selection rule

The application with the highest hostility index is the subject. Ties break by operation
count, then alphabetically.

**Threshold.** If the highest-scoring application scores below **0.40**, the experiment is
declared not runnable and that is the reported outcome: no benchmark available to this project
carries both a hostile surface and third-party goals, and the conditional hypothesis therefore
remains untested. Reporting that is the commitment; going to find a hostile API and writing my
own goals for it is not.

## What is fixed in advance

- **Arms.** Baseline one-tool-per-operation against the semantic planner, both approved
  identically, both offered only executable tools.
- **Corpus.** AppWorld tasks for the selected application, chosen by a rule stated in the
  registration before any is read, and no more than 40 so each oracle can be authored with
  care.
- **Oracles.** Authored from the goal alone. AppWorld evaluates by outcome, so where its own
  evaluation is usable it is preferred to anything authored here.
- **Primary metric.** Task success rate. **Primary test.** McNemar's exact, two-sided, alpha
  0.05, needing at least six discordant pairs all favouring one arm.
- **Success.** All three runs of a task must satisfy every oracle.
- **Falsification.** Six or more discordant pairs favouring the baseline. Also equal success
  with no reduction in calls, since a designed surface that saves nothing is one nobody needed.
- **Inconclusive.** Fewer than six discordant pairs, reported as inconclusive. The
  pre-committed response is to report that the conditional hypothesis also failed to show, and
  to stop. There is no sixth treatment.

## What may not happen afterwards

Selecting a different application. Revising the hostility index, its threshold, or the
selection rule. Re-authoring a task or oracle. Changing the planners, the model, the budget or
the success definition. Reading AppWorld's reference solutions, which would make any later
registration against it worthless.

## The honest prior

Four nulls is real evidence. The most likely outcome of this experiment is a fifth, and the
protocol is written so that outcome is publishable rather than embarrassing. If it does show a
difference, the result is worth having precisely because the conditions were fixed while the
expectation was still that it would not.


---

# Outcome: not runnable

The index was computed over the nine AppWorld application specifications, with the two
RestBench specifications scored alongside for comparison. Every component reads the
specification and nothing else, so no result influenced any of it.

| surface | ops | scale | unnamed | collision | args | depth | **index** |
|---|---|---|---|---|---|---|---|
| spotify (RestBench, comparison) | 40 | 0.20 | 0.00 | 0.00 | 0.20 | 0.75 | **0.230** |
| tmdb (RestBench, comparison) | 54 | 0.27 | 0.00 | 0.65 | 0.10 | 0.12 | **0.229** |
| **spotify (AppWorld)** | 91 | 0.46 | 0.00 | 0.00 | 0.10 | 0.38 | **0.186** |
| splitwise | 65 | 0.33 | 0.00 | 0.00 | 0.10 | 0.50 | 0.185 |
| todoist | 56 | 0.28 | 0.00 | 0.00 | 0.10 | 0.50 | 0.176 |
| gmail | 42 | 0.21 | 0.00 | 0.05 | 0.10 | 0.50 | 0.172 |
| amazon | 66 | 0.33 | 0.00 | 0.00 | 0.10 | 0.38 | 0.161 |
| phone | 30 | 0.15 | 0.00 | 0.00 | 0.10 | 0.50 | 0.150 |
| venmo | 54 | 0.27 | 0.00 | 0.00 | 0.10 | 0.38 | 0.149 |
| simple_note | 17 | 0.09 | 0.00 | 0.00 | 0.10 | 0.50 | 0.137 |
| file_system | 26 | 0.13 | 0.00 | 0.00 | 0.10 | 0.38 | 0.121 |
| supervisor | 6 | 0.03 | 0.00 | 0.00 | 0.00 | 0.50 | 0.106 |

**The highest-scoring candidate is AppWorld's spotify at 0.186, against a threshold of 0.40.**
The experiment is therefore declared not runnable, which is the outcome this protocol
committed to reporting.

## What the numbers say beyond the threshold

**Every AppWorld application scores zero on unnamed operations.** All 457 carry a summary
distinct from their identifier. These are carefully documented simulated applications, which
is exactly what makes them a good agent benchmark and exactly what makes them useless for this
hypothesis.

**AppWorld is less hostile than the APIs that already produced four nulls.** Every one of the
nine scores below both RestBench specifications. Running here would not have tested the
conditional hypothesis at all; it would have been a fifth comparison on friendlier ground,
and a fifth null would have added nothing.

That is worth stating as a finding rather than a disappointment. The index was defined to
decide whether an experiment was worth running, and it decided against — before any tokens
were spent and before anyone could be tempted by a number.

**It also strengthens the four existing nulls.** Those were obtained on the two most hostile
surfaces in this table. The reading that a capable model copes with a clean API is now
slightly harder to sustain, because Spotify and TMDB were not the easiest surfaces available.

## What was explicitly not done

The protocol forbade going to find a hostile API and writing goals for it, and that is what
would have happened next by default. Kubernetes, the AWS APIs and any large generated surface
are all more hostile than anything here, and none arrives with third-party goals. Authoring
goals here would have reproduced the circularity that already cost this project a corpus.

## What would change the answer

A benchmark carrying both a hostile surface and human-authored goals. None is known to this
project today. Should one appear, this protocol can be reopened unchanged, because nothing in
it was written after seeing a result.
