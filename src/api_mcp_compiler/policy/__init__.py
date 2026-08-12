"""Policy synthesis, kept separate from code generation.

The architecture requires policy generation to be separate from code generation, and the
reason is reviewability: a generator that also decided policy could be reviewed as neither.
This package derives a governance manifest from the IR and a plan, and code generation
consumes that manifest rather than inventing one.
"""
