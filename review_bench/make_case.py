"""Build cases: clean ones from a merge commit, seeded ones from a clean case plus a mutation."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

from .cases import (EXCLUDED_FILES, EXCLUDED_PREFIXES, CaseError, apply_diff, diff_files, git,
                    load_case, snapshot)


def included(path, exclude=()):
    return (path not in EXCLUDED_FILES and not path.startswith(EXCLUDED_PREFIXES)
            and not any(path.startswith(e) for e in exclude))


def write_case(directory, meta, diff):
    directory = Path(directory)
    if directory.exists():
        raise CaseError(f"{directory} already exists; remove it deliberately first")
    directory.mkdir(parents=True)
    (directory / "candidate.diff").write_text(diff)
    (directory / "case.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    return load_case(directory)


def merge_diff(repo, base, head, exclude=()):
    """The reviewable diff between two commits: workflow files and binaries excluded."""
    names = [n for n in git(repo, "diff", "--name-only", base, head).decode().splitlines() if included(n, exclude)]
    if not names:
        raise CaseError("No reviewable files in that change")
    diff = git(repo, "diff", "--binary", base, head, "--", *names).decode(errors="replace")
    if "GIT binary patch" in diff:
        raise CaseError("Binary content in diff; exclude those paths")
    return diff


def clean_case(repo, merge_sha, *, case_id, cases_root, repo_url, objective, acceptance, exclude=(),
               kind="clean", expected=(), author="history", notes=None, source=None):
    """The merged change of one merge commit against its first parent, minus workflow files.

    With kind="seeded" and expected findings this records a historical defect: a merged
    change whose bug a later fix established as ground truth, authored by nobody here.
    """
    base = git(repo, "rev-parse", merge_sha + "^1").decode().strip()
    head = git(repo, "rev-parse", merge_sha).decode().strip()
    diff = merge_diff(repo, base, head, exclude)
    meta = {"id": case_id, "kind": kind, "repo": repo_url, "base_sha": base, "author": author,
            "source": dict(source or {}, merge=head), "objective": objective, "acceptance": acceptance,
            "files": diff_files(diff), "expected": list(expected),
            "notes": notes if notes is not None else
            "Merged change believed free of blocking defects; adjudicate any blocking finding by hand."}
    return write_case(Path(cases_root) / case_id, meta, diff)


def apply_edits(tree, edits):
    """Exact single-occurrence text replacements; the safest way to author a mutation."""
    for edit in edits:
        for key in ("file", "old", "new"):
            if key not in edit or not isinstance(edit[key], str):
                raise CaseError(f"edit needs string {key}")
        path = Path(tree) / edit["file"]
        if not path.is_file():
            raise CaseError(f"edit target {edit['file']} does not exist after the clean diff")
        text = path.read_text()
        if text.count(edit["old"]) != 1:
            raise CaseError(f"edit text occurs {text.count(edit['old'])} times in {edit['file']}; must be exactly once")
        path.write_text(text.replace(edit["old"], edit["new"]))


def seeded_case(repo, clean_dir, *, case_id, cases_root, expected, mutation=None, edits=None, notes="",
                author="unknown"):
    """Apply the clean diff, then a mutation (diff file or exact edits); the case diff is base -> mutated."""
    clean = load_case(clean_dir)
    if (mutation is None) == (edits is None):
        raise CaseError("give exactly one of a mutation diff or an edits list")
    with tempfile.TemporaryDirectory() as tmp:
        tree = snapshot(repo, clean["base_sha"], Path(tmp) / "tree")
        apply_diff(tree, clean["diff"])
        git(tree, "add", "-A")
        git(tree, "-c", "user.email=bench@example.invalid", "-c", "user.name=bench", "commit", "-q", "-m", "clean")
        if mutation is not None:
            apply_diff(tree, Path(mutation).read_text())
        else:
            apply_edits(tree, edits)
        mutation_text = git(tree, "diff", "HEAD").decode(errors="replace")
        if not mutation_text.strip():
            raise CaseError("mutation changed nothing")
        git(tree, "add", "-A")
        diff = git(tree, "diff", "--cached", "--binary", "HEAD~1").decode(errors="replace")
    meta = {"id": case_id, "kind": "seeded", "repo": clean["repo"], "base_sha": clean["base_sha"], "author": author,
            "source": dict(clean.get("source", {}), clean_case=clean["id"]), "objective": clean["objective"],
            "acceptance": clean["acceptance"], "files": diff_files(diff),
            "expected": list(expected) + [e for e in clean["expected"]], "notes": notes}
    case = write_case(Path(cases_root) / case_id, meta, diff)
    (Path(cases_root) / case_id / "mutation.diff").write_text(mutation_text)
    return case
