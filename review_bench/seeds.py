"""Seed proposals from other models, so the corpus is not authored by one model family.

A proposal is a suggestion, not a case. Someone reads the proposal file, checks
that the defect is real and that the keywords describe it, and only then runs
`accept-seeds`. Every accepted case records its author model, and the report
splits recall by author so same-family bias is visible.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from . import backends as B
from .cases import CaseError, candidate_tree, load_case
from .make_case import seeded_case
from .prompt import canonical

EDIT_SCHEMA = {"type": "object", "properties": {"file": {"type": "string"}, "old": {"type": "string"},
                                                "new": {"type": "string"}},
               "required": ["file", "old", "new"], "additionalProperties": False}
EXPECTED_SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string"}, "file": {"type": "string"},
    "severity": {"type": "string", "enum": ["blocking", "should_fix", "nit"]},
    "keywords": {"type": "array", "items": {"type": "string"}}, "description": {"type": "string"}},
    "required": ["id", "file", "severity", "keywords", "description"], "additionalProperties": False}
PROPOSAL_SCHEMA = {"type": "object", "properties": {"proposals": {"type": "array", "items": {
    "type": "object", "properties": {
        "slug": {"type": "string"}, "category": {"type": "string"},
        "edits": {"type": "array", "items": EDIT_SCHEMA},
        "expected": {"type": "array", "items": EXPECTED_SCHEMA},
        "notes": {"type": "string"}},
    "required": ["slug", "category", "edits", "expected", "notes"], "additionalProperties": False}}},
    "required": ["proposals"], "additionalProperties": False}

# Defect categories that are too easy or too artificial to be worth another seed.
# Extend this list as a corpus grows so authors keep finding new ground.
DEFAULT_FORBIDDEN = [
    "deleting an entire validation or guard block",
    "removing a lock, transaction or atomic block outright",
    "removing an expiry or timeout check outright",
    "adding an unused variable or a syntax error",
    "changing a user-facing string or log message only",
    "renaming a symbol without changing behaviour",
]

BRIEF = """You are authoring test cases for a code-review benchmark. Below is a merged change
(the DIFF) to the repository in the working directory, with the change already applied.
Propose {count} distinct defects to inject into this change. Each defect must be:
- realistic: the kind of slip a competent engineer makes, not vandalism;
- subtle: prefer a wrong comparison direction, an off-by-one at a boundary, swapped
  arguments, a field left out of an update, a dropped rounding or normalisation step,
  a wrong default, a condition that is right for one branch and wrong for another,
  or a change whose consequence only shows in a caller outside the diff;
- consequential: a concrete input or state must produce a wrong result, a crash, lost
  data or a silently skipped safeguard. Explain it in the description;
- inside the diff: edit only lines this change added or touched, in files it touched;
- small: one to three exact text replacements.
Do not use any of these categories, which the corpus already covers:
{forbidden}

For each defect give exact edits. Copy the `old` text verbatim from the file so it
occurs exactly once, including indentation and newlines. `new` is the replacement.
Give one or two expected findings: the file, severity blocking, and six to ten
keywords. A keyword is a single word or a two-word fragment that a reviewer's
sentence would plausibly contain verbatim: the function or variable name, the
operator or literal involved (for example "and", "> 200", "max_age"), and the
plain-English effect ("bypass", "never fires", "wrong host"). Never use abstract
category phrases such as "boolean conjunction" or "boundary comparison"; reviewers
do not write those. Avoid generic words such as "error" or "check". Add a
one-sentence description of the failure. Use a short kebab-case slug and name the defect category.
Respond with a single JSON object matching this schema and nothing else:
{schema}
CASE:
{case}
DIFF:
"""


def author_label(spec):
    name, model, _ = B.parse_spec(spec)
    return re.sub(r"[^a-z0-9]+", "-", f"{name}-{model}".lower()).strip("-")


def build_brief(clean, count, forbidden, categories=()):
    packet = {"objective": clean["objective"], "acceptance": clean["acceptance"], "files": clean["files"]}
    steer = ("Every defect must belong to one of these categories:\n"
             + "\n".join("- " + c for c in categories) + "\n\n") if categories else ""
    return (BRIEF.format(count=count, forbidden="\n".join("- " + f for f in forbidden),
                         schema=canonical(PROPOSAL_SCHEMA), case=canonical(packet)).replace("CASE:\n", steer + "CASE:\n")
            + clean["diff"])


def propose(clean, spec, repo, work_root, timeout, *, count=3, forbidden=DEFAULT_FORBIDDEN, categories=()):
    name, model, effort = B.parse_spec(spec)
    if name not in B.HAS_TOOLS and name != "openai":
        raise CaseError("seed authors must be a real model backend")
    tree = candidate_tree(repo, clean, work_root) if name in B.HAS_TOOLS else Path(work_root).resolve() / "no-tree"
    tree.mkdir(parents=True, exist_ok=True)
    result, usage, raw = B.BACKENDS[name](clean, build_brief(clean, count, forbidden, categories), tree, model, effort, timeout,
                                          schema=PROPOSAL_SCHEMA)
    if not isinstance(result, dict) or not isinstance(result.get("proposals"), list):
        raise CaseError("author returned no proposals list")
    return {"author": author_label(spec), "spec": spec, "clean_case": clean["id"], "usage": usage,
            "proposals": result["proposals"]}


def accept(proposal_file, repo, cases_root, *, only=None):
    """Turn reviewed proposals into seeded cases; every failure is reported, none is silent."""
    record = json.loads(Path(proposal_file).read_text())
    clean_dir = Path(cases_root) / record["clean_case"]
    author = record["author"]
    made, failures = [], []
    for proposal in record["proposals"]:
        slug = re.sub(r"[^a-z0-9]+", "-", str(proposal.get("slug", "")).lower()).strip("-")
        if only and slug not in only:
            continue
        case_id = f"seeded-{author}-{slug}"
        try:
            if not slug:
                raise CaseError("proposal has no slug")
            expected = proposal.get("expected") or []
            if not any(e.get("severity") == "blocking" for e in expected):
                raise CaseError("proposal needs a blocking expectation")
            for e in expected:
                short = [k for k in e.get("keywords", []) if len(str(k).split()) <= 2]
                if len(short) < 3:
                    raise CaseError(f"expectation {e.get('id')} needs at least three keywords of one or two words; "
                                    "abstract phrases never match a reviewer's sentence")
            case = seeded_case(repo, clean_dir, case_id=case_id, cases_root=cases_root, expected=expected,
                               edits=proposal.get("edits") or [], author=author,
                               notes=f"[{proposal.get('category', '')}] {proposal.get('notes', '')}")
            made.append(case["id"])
        except CaseError as exc:
            failures.append({"slug": slug or "?", "error": str(exc)})
    return made, failures
