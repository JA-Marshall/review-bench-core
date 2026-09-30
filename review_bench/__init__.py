"""Benchmark for automated code reviewers.

A case is a diff against a pinned commit of a target repository plus the
defects a competent reviewer must report. Backends review each case in a fresh
context; scoring is deterministic so the harness is testable without any model.
"""
