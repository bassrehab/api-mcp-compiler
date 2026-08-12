"""Execution runtimes for a generated tool surface.

Only a mock runtime exists. It performs no network or filesystem access, so a generated
surface can be exercised by contract tests and, later, by an evaluation harness without any
real service being reachable or any credential existing.
"""
