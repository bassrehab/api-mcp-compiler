# Contributing

## The rule that matters

Run the gate before you push:

```bash
.venv/bin/python scripts/verify_repo.py
```

Every check runs even when an earlier one fails, so a single run reports the full picture. It
must exit zero.

## House conventions

**Tests ship with the code.** A change that cannot be observed failing before the fix is not
finished.

**A check that has never been seen to fail is a hypothesis, not a gate.** When adding one,
break the thing it guards on purpose and confirm the check goes red. Two checks in this
repository were found to be decoration exactly this way.

**Nothing is dropped silently.** If the compiler cannot resolve something, it records an
ambiguity naming the construct. It does not guess, and it does not omit.

**Judgement is proposed, not applied.** A planner improvement adds a decision with a rationale
and a confidence. It does not change a surface without a recorded human decision.

**Prose is plain ASCII.** Documentation, docstrings and comments use ordinary punctuation:
commas, colons, parentheses and full stops rather than typographic dashes or smart quotes. This
keeps diffs, terminals and search predictable, and `scripts/check_docs.py` enforces it.

**Comments say why.** What the code does is visible in the code.

## Regenerating artifacts

```bash
.venv/bin/python scripts/regen_golden.py
```

Regenerates golden artifacts and the notebook's stored outputs. Do it deliberately, then read
the diff: an unexplained diff means the change was not the one intended.

If a specification changed, its overlay no longer matches and must be rebound explicitly with
`overlay-restamp` after re-reading what was approved.

## Research changes

A change to an evaluation corpus, an oracle or a planner that will be measured needs a
[pre-registration](evaluation.md) written before the run. Registrations are append-only: a
superseded one is never edited or deleted.

Do not edit a registration under `preregistrations/` for any reason, including cosmetic ones.
Each file is digested and recorded runs reference that digest; changing a byte detaches a
result from the hypothesis it was produced under.

## Scope

Synthetic and public specifications only. No credentials, customer specifications, traffic or
proprietary schemas belong in this repository. Third-party benchmark documents are fetched and
verified at the point of use, never committed.
