"""Case corpus: loading, validation and snapshot building."""
from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile


KINDS = {"clean", "seeded"}
SEVERITIES = ("blocking", "should_fix", "nit")
VERDICTS = ("false_positive", "promoted")
# Files that describe the workflow around a change rather than the change itself.
# Reviewers see product code only; `--exclude` extends this list per repository.
EXCLUDED_PREFIXES = (".github/", ".agents/", ".codex/", ".claude/", ".cursor/")
EXCLUDED_FILES = {"AGENTS.md", "PLANS.md", "CLAUDE.md", "CODEOWNERS"}
# Text in a reviewer packet that could only have been written after the defect was found.
HINDSIGHT = (r"\bfixed (?:by|in|later)\b", r"\blater fix(?:ed)?\b", r"\bsubsequent(?:ly)? (?:fix|revert)",
             r"\bwas (?:reverted|broken)\b", r"\bintroduced (?:a|the) (?:bug|regression|defect)\b",
             r"\bthis (?:bug|defect|regression) was\b", r"\bfollow-up fix\b")


class CaseError(Exception):
    """A case directory that cannot be used as ground truth."""


def git(cwd, *args, input=None):
    result = subprocess.run(["git", "-C", str(cwd), *args], input=input, capture_output=True)
    if result.returncode:
        raise CaseError(f"git {' '.join(args[:2])} failed: {result.stderr.decode(errors='replace')[-800:]}")
    return result.stdout


def candidate_hash(diff):
    return hashlib.sha256(diff.encode()).hexdigest()


def diff_files(diff):
    """Paths named by a unified diff, in sorted order."""
    names = set()
    for line in diff.splitlines():
        if line.startswith("diff --git a/"):
            rest = line[len("diff --git a/"):]
            names.add(rest.split(" b/", 1)[0])
    return sorted(names)


def hindsight_problems(meta):
    """Phrases in the reviewer packet that leak knowledge of a later fix.

    The packet (objective and acceptance) must be writable from the pull
    request as it stood at merge time. A reference to a pull request or issue
    numbered after the source pull request is treated as leakage too.
    """
    texts = [meta.get("objective", "")] + list(meta.get("acceptance", []))
    problems = []
    source_pr = (meta.get("source") or {}).get("pr")
    for text in texts:
        for pattern in HINDSIGHT:
            if re.search(pattern, text, re.I):
                problems.append(f"packet text matches hindsight pattern {pattern!r}: {text[:80]!r}")
        if isinstance(source_pr, int):
            for number in re.findall(r"#(\d+)", text):
                if int(number) > source_pr:
                    problems.append(f"packet text references #{number}, later than the source PR #{source_pr}")
    return problems


def load_case(directory):
    directory = Path(directory)
    meta_path, diff_path = directory / "case.json", directory / "candidate.diff"
    if not meta_path.is_file() or not diff_path.is_file():
        raise CaseError(f"{directory}: needs case.json and candidate.diff")
    try:
        meta = json.loads(meta_path.read_text())
    except ValueError as exc:
        raise CaseError(f"{directory}: case.json is not JSON ({exc})")
    diff = diff_path.read_text()
    required = {"id", "kind", "repo", "base_sha", "objective", "acceptance", "files", "expected"}
    missing = required - set(meta)
    if missing:
        raise CaseError(f"{directory}: case.json missing {sorted(missing)}")
    if meta["id"] != directory.name:
        raise CaseError(f"{directory}: id must equal the directory name")
    if meta["kind"] not in KINDS:
        raise CaseError(f"{directory}: kind must be one of {sorted(KINDS)}")
    if not isinstance(meta["base_sha"], str) or not re.fullmatch(r"[0-9a-f]{40}", meta["base_sha"]):
        raise CaseError(f"{directory}: base_sha must be a full commit SHA")
    if not isinstance(meta["objective"], str) or not meta["objective"].strip():
        raise CaseError(f"{directory}: objective must be a non-empty string")
    if not isinstance(meta["acceptance"], list) or not all(isinstance(a, str) and a for a in meta["acceptance"]):
        raise CaseError(f"{directory}: acceptance must be a list of non-empty strings")
    if meta["files"] != diff_files(diff):
        raise CaseError(f"{directory}: files {meta['files']} do not match the diff {diff_files(diff)}")
    if not diff.strip():
        raise CaseError(f"{directory}: candidate.diff is empty")
    seen = set()
    for item in meta["expected"]:
        for key in ("id", "file", "severity", "keywords", "description"):
            if key not in item:
                raise CaseError(f"{directory}: expected finding missing {key}")
        if item["id"] in seen:
            raise CaseError(f"{directory}: duplicate expected id {item['id']}")
        seen.add(item["id"])
        if item["severity"] not in SEVERITIES:
            raise CaseError(f"{directory}: expected severity must be one of {SEVERITIES}")
        if item["file"] not in meta["files"]:
            raise CaseError(f"{directory}: expected file {item['file']} is not in the diff")
        if not isinstance(item["keywords"], list) or not item["keywords"]:
            raise CaseError(f"{directory}: expected finding {item['id']} needs keywords")
    if meta["kind"] == "seeded" and not any(e["severity"] == "blocking" for e in meta["expected"]):
        raise CaseError(f"{directory}: a seeded case needs at least one blocking expectation")
    if meta["kind"] == "clean" and any(e["severity"] == "blocking" for e in meta["expected"]):
        raise CaseError(f"{directory}: a clean case cannot expect a blocking finding")
    for item in meta.get("adjudications", []):
        if not isinstance(item, dict) or not {"finding", "verdict", "note"} <= set(item):
            raise CaseError(f"{directory}: each adjudication needs finding, verdict and note")
        if item["verdict"] not in VERDICTS:
            raise CaseError(f"{directory}: adjudication verdict must be one of {VERDICTS}")
    author = meta.get("author", "history" if meta["kind"] == "clean" else "unknown")
    if not isinstance(author, str) or not author:
        raise CaseError(f"{directory}: author must be a non-empty string")
    return dict(meta, author=author, diff=diff, candidate=candidate_hash(diff), directory=str(directory))


def load_cases(root, ids=None):
    root = Path(root)
    if not root.is_dir():
        raise CaseError(f"No case directory at {root}")
    cases = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        if ids and directory.name not in ids:
            continue
        cases.append(load_case(directory))
    if ids:
        unknown = set(ids) - {c["id"] for c in cases}
        if unknown:
            raise CaseError(f"Unknown case ids: {sorted(unknown)}")
    if not cases:
        raise CaseError(f"No cases under {root}")
    return cases


def snapshot(repo, base_sha, destination):
    """Materialise one commit of the target repo as a fresh throwaway Git repo."""
    destination = Path(destination)
    destination.mkdir(parents=True)
    with tempfile.TemporaryFile() as archive:
        archive.write(git(repo, "archive", "--format=tar", base_sha))
        archive.seek(0)
        with tarfile.open(fileobj=archive) as tar:
            # The "tar" filter rejects absolute member names and path traversal. The stricter "data"
            # filter is not used: Python 3.10's backport resolves relative symlink targets against
            # the wrong directory and rejects in-tree links such as pydantic's tests/pydantic_core.
            tar.extractall(destination, filter="tar") if hasattr(tarfile, "tar_filter") else tar.extractall(destination)
    git(destination, "init", "-q", "-b", "bench")
    git(destination, "-c", "user.email=bench@example.invalid", "-c", "user.name=bench", "add", "-A")
    git(destination, "-c", "user.email=bench@example.invalid", "-c", "user.name=bench",
        "commit", "-q", "--allow-empty", "-m", "base " + base_sha)
    return destination


def apply_diff(tree, diff):
    git(tree, "apply", "--whitespace=nowarn", "-", input=diff.encode())


def check_applies(repo, case):
    """Raise CaseError unless the case diff applies to its base commit."""
    with tempfile.TemporaryDirectory() as tmp:
        tree = snapshot(repo, case["base_sha"], Path(tmp) / "tree")
        try:
            apply_diff(tree, case["diff"])
        except CaseError as exc:
            raise CaseError(f"{case['id']}: candidate.diff does not apply to {case['base_sha'][:12]}: {exc}")


def candidate_tree(repo, case, work_root):
    """Cached working tree holding the candidate source for tool-using backends."""
    # Absolute: CLI backends run with the tree as cwd and pass it as --cd/--workspace.
    tree = Path(work_root).resolve() / "trees" / f"{case['id']}-{case['candidate'][:12]}"
    if tree.is_dir():
        return tree
    tree.parent.mkdir(parents=True, exist_ok=True)
    # Backends run in parallel processes; one builds the tree while the others wait.
    with tree.with_name(tree.name + ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not tree.is_dir():
            partial = tree.with_name(tree.name + ".partial")
            if partial.exists():
                shutil.rmtree(partial)
            snapshot(repo, case["base_sha"], partial)
            apply_diff(partial, case["diff"])
            partial.rename(tree)
    return tree
