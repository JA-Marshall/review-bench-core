"""Reviewer prompt and structured result contract shared by every backend."""
from __future__ import annotations

import json


FINDING_SCHEMA = {"type": "object", "properties": {
    "file": {"type": "string"},
    "severity": {"type": "string", "enum": ["blocking", "should_fix", "nit"]},
    "summary": {"type": "string"},
    "failure_scenario": {"type": "string"}},
    "required": ["file", "severity", "summary", "failure_scenario"], "additionalProperties": False}

REVIEW_SCHEMA = {"type": "object", "properties": {
    "candidate": {"type": "string"},
    "covered_files": {"type": "array", "items": {"type": "string"}},
    "findings": {"type": "array", "items": FINDING_SCHEMA}},
    "required": ["candidate", "covered_files", "findings"], "additionalProperties": False}

INSTRUCTIONS = """You are a read-only reviewer of the exact supplied candidate change.
Review the complete diff and every acceptance item. Do not edit, run checks,
delegate, commit, push or deploy. Return the supplied candidate hash, every
file the diff touches as covered_files, and concrete findings.

Severity rules:
- blocking: the change is wrong or unsafe as written. Give a concrete failure
  scenario: the input or state, and the wrong output, crash, data loss or
  incorrect record it produces. If you cannot name one, it is not blocking.
- should_fix: a real weakness that does not break the stated objective.
- nit: style or preference. Report at most three nits.

Only report defects you would bet on. A false blocking finding costs a full
correction round; a missed defect is caught later by CI and a human. Empty
findings means no actionable defects found, not proof of correctness.
"""

TOOL_NOTE = ("The working directory is a snapshot of the repository with the candidate change "
             "already applied. Read related code with targeted searches; do not survey the whole tree.\n")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


VARIANTS = {
    "": "",
    "callers": ("Whenever the diff changes a function signature, a default argument value, a return shape or a "
                "constant, search the tree for every caller and template that uses it and check each one; "
                "a consequence that only appears in a caller outside the diff is still a defect in this change. "
                "Whenever a condition joins comparisons with and/or, or compares against a limit, state the "
                "boundary case explicitly and check which side it falls on.\n"),
}


def build_prompt(case, *, has_tools, variant=""):
    packet = {"objective": case["objective"], "acceptance": case["acceptance"],
              "files": case["files"], "candidate": case["candidate"]}
    return (INSTRUCTIONS + VARIANTS[variant] + (TOOL_NOTE if has_tools else "")
            + "Respond with a single JSON object matching this schema and nothing else:\n"
            + canonical(REVIEW_SCHEMA) + "\nCASE:\n" + canonical(packet) + "\nDIFF:\n" + case["diff"])


def validate_result(result, case):
    """Return a list of contract violations; empty means the result is usable."""
    problems = []
    if not isinstance(result, dict):
        return ["result is not an object"]
    if set(result) != {"candidate", "covered_files", "findings"}:
        problems.append(f"unexpected keys {sorted(result)}")
        return problems
    if result["candidate"] != case["candidate"]:
        problems.append("candidate hash mismatch")
    if not isinstance(result["covered_files"], list) or sorted(result["covered_files"]) != case["files"]:
        problems.append("covered_files does not equal the diff's files")
    if not isinstance(result["findings"], list):
        problems.append("findings is not a list")
        return problems
    for index, finding in enumerate(result["findings"]):
        if (not isinstance(finding, dict) or set(finding) != {"file", "severity", "summary", "failure_scenario"}
                or finding["severity"] not in ("blocking", "should_fix", "nit")
                or not all(isinstance(finding[k], str) for k in ("file", "summary", "failure_scenario"))):
            problems.append(f"finding {index} is malformed")
    return problems
