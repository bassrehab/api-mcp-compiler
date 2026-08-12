"""Generation of transport-independent tool surfaces from an IR and a reviewed plan.

Code generation is deliberately separate from planning and from policy. This package turns
a plan plus the IR it references into tool descriptors, and refuses executable status to
anything that has not cleared the safety gate. It binds to no SDK and performs no I/O.
"""
