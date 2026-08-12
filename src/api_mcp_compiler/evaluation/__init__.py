"""Evaluation harness.

The harness exists to answer whether a semantically planned tool surface actually beats
operation-per-tool conversion. It cannot answer that yet, and this package deliberately
contains nothing that would let it appear to: there is no model driver, no judge and no
comparison report. What it provides is the machinery, built so that every safety and success
number is produced by a deterministic oracle against real service state, with no model in the
loop.
"""
