"""Core data models, orchestration, and project mapping.

The dataclasses in `events` are the contract every adapter and renderer must
honor. Touching them is a breaking change for the whole pipeline; do it
deliberately and update DESIGN.md §4.
"""
