"""Propose cases from a GitHub repository's merged pull requests.

The importer never produces a scored case. It walks merged pull requests,
pins the exact base and merge commits, extracts the reviewable diff, and
collects evidence: linked issues, review comments, and anything that
referenced the pull request after it merged (a later fix, a regression
report). Everything lands in a proposal directory:

    proposals/<owner>--<name>/pr-<number>/proposal.json
    proposals/<owner>--<name>/pr-<number>/candidate.diff

A proposal becomes a case only through `promote`, which requires a human
ruling in `proposal.json`: clean or seeded, the specific defect, its severity,
and a rationale. The reviewer packet (objective and acceptance) must be
writable from the pull request as it stood at merge time; `promote` rejects
text that leaks a later fix. Evidence goes to `provenance.json` beside the
case and never into a reviewer prompt.

Importing is resumable. `state.json` records the next page to scan and every
pull request already proposed or skipped, so a rerun continues where the last
one stopped and never rewrites an existing proposal.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .cases import CaseError, diff_files, git, hindsight_problems, load_case
from .make_case import included, write_case

API = "https://api.github.com"
LINK = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*(?:#|https://github\.com/[\w.-]+/[\w.-]+/issues/)(\d+)", re.I)
DOC_SUFFIXES = (".md", ".rst", ".txt")
RULING_STATUSES = ("clean", "seeded", "rejected")
EXCERPT = 1500


class ImportError_(CaseError):
    """The importer could not continue; the state file allows a resume."""


# --- GitHub access -----------------------------------------------------------

def github_token():
    """GITHUB_TOKEN or GH_TOKEN from the environment, else whatever the gh CLI is logged in with."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    for argv in (["gh", "auth", "token"], ["gh", "config", "get", "-h", "github.com", "oauth_token"]):
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        candidate = result.stdout.strip()
        if result.returncode == 0 and candidate and re.fullmatch(r"[\w.-]+", candidate):
            return candidate
    return None


class GitHub:
    """Minimal REST client. Unauthenticated calls are limited to 60 per hour, so pass a token."""

    def __init__(self, token=None, sleep=time.sleep):
        self.token = token
        self.sleep = sleep
        self.calls = 0

    def get(self, path, **params):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "review-bench-importer"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, headers=headers)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    self.calls += 1
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
                    reset = int(exc.headers.get("X-RateLimit-Reset", "0"))
                    wait = max(1, min(reset - int(time.time()), 900))
                    raise ImportError_(f"GitHub rate limit reached; resets in {wait}s. Rerun to resume.")
                if exc.code >= 500 and attempt < 2:
                    self.sleep(2 * (attempt + 1))
                    continue
                raise ImportError_(f"GitHub {exc.code} for {path}: {exc.read()[:300]!r}")
            except urllib.error.URLError as exc:
                raise ImportError_(f"GitHub request failed for {path}: {exc}")
        raise ImportError_(f"GitHub request failed repeatedly for {path}")


def parse_repo_url(url):
    match = re.match(r"^(?:https?://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url.strip())
    if not match:
        raise ImportError_(f"not a GitHub repository URL: {url}")
    return match.group(1), match.group(2)


def linked_issue_numbers(text):
    return sorted({int(n) for n in LINK.findall(text or "")})


def excerpt(text, limit=EXCERPT):
    text = (text or "").replace("\r\n", "\n").strip()
    return text if len(text) <= limit else text[:limit] + " [...]"


# --- local clone ---------------------------------------------------------------

def ensure_clone(repo_url, clone):
    """Clone the repository once; later runs fetch only when a commit is missing."""
    clone = Path(clone)
    if not (clone / ".git").is_dir():
        clone.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["git", "clone", "--quiet", repo_url, str(clone)], capture_output=True)
        if result.returncode:
            raise ImportError_(f"git clone failed: {result.stderr.decode(errors='replace')[-500:]}")
    return clone


def has_commit(clone, sha):
    return subprocess.run(["git", "-C", str(clone), "cat-file", "-e", sha + "^{commit}"], capture_output=True).returncode == 0


def fetch_commit(clone, sha):
    if has_commit(clone, sha):
        return
    subprocess.run(["git", "-C", str(clone), "fetch", "--quiet", "origin"], capture_output=True)
    if not has_commit(clone, sha):
        raise ImportError_(f"commit {sha} is not reachable from origin; a force-pushed merge cannot be pinned")


def resolve_base(clone, merge_sha, pr_commits, pr_files):
    """Pin the base commit for a merge, squash or rebase merge.

    Returns (base_sha, style, verified). Verified means the local diff between
    base and merge names exactly the files GitHub lists for the pull request.
    """
    parents = git(clone, "rev-list", "--parents", "-n", "1", merge_sha).decode().split()[1:]
    if len(parents) == 2:
        return parents[0], "merge", True
    expected = set(pr_files)
    candidates = [("squash", merge_sha + "^1")]
    if pr_commits and pr_commits > 1:
        candidates.append(("rebase", f"{merge_sha}~{pr_commits}"))
    for style, ref in candidates:
        try:
            sha = git(clone, "rev-parse", ref).decode().strip()
            names = set(git(clone, "diff", "--name-only", sha, merge_sha).decode().splitlines())
        except CaseError:
            continue
        if names == expected:
            return sha, style, True
    return parents[0], "squash", False


# --- proposals -----------------------------------------------------------------

def is_bot(pr):
    user = pr.get("user") or {}
    return user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def skip_reason(pr, *, since):
    if not pr.get("merged_at"):
        return "not merged"
    if since and pr.get("created_at", "") < since:
        return f"created before {since}"
    if is_bot(pr):
        return "opened by a bot"
    if not pr.get("merge_commit_sha"):
        return "no merge commit"
    return None


def reviewable_files(files, exclude):
    names = [f["filename"] for f in files]
    kept = [n for n in names if included(n, exclude)]
    code = [n for n in kept if not n.startswith("docs/") and not n.lower().endswith(DOC_SUFFIXES)]
    return kept, [n for n in names if n not in kept], code


def acceptance_draft(body):
    """Bullet lines from the pull request body, as a starting point for acceptance items."""
    items = []
    for line in (body or "").replace("\r\n", "\n").splitlines():
        stripped = line.strip()
        if re.match(r"^[-*]\s+\S", stripped) and not re.match(r"^[-*]\s+\[[ x]\]", stripped, re.I):
            text = re.sub(r"^[-*]\s+", "", stripped)
            if 12 <= len(text) <= 240 and not text.startswith("http"):
                items.append(text)
    return items[:8]


def evidence_for(gh, owner, name, pr, merged_at):
    number = pr["number"]
    reviews = gh.get(f"/repos/{owner}/{name}/pulls/{number}/reviews", per_page=50)
    comments = gh.get(f"/repos/{owner}/{name}/pulls/{number}/comments", per_page=100)
    discussion = gh.get(f"/repos/{owner}/{name}/issues/{number}/comments", per_page=50)
    issues = []
    for issue_number in linked_issue_numbers(pr.get("body")):
        try:
            issue = gh.get(f"/repos/{owner}/{name}/issues/{issue_number}")
        except ImportError_:
            continue
        issues.append({"number": issue_number, "url": issue.get("html_url"), "title": issue.get("title"),
                       "body": excerpt(issue.get("body")), "created_at": issue.get("created_at")})
    later = []
    try:
        timeline = gh.get(f"/repos/{owner}/{name}/issues/{number}/timeline", per_page=100)
    except ImportError_:
        timeline = []
    for event in timeline:
        if event.get("event") != "cross-referenced" or event.get("created_at", "") <= merged_at:
            continue
        source = (event.get("source") or {}).get("issue") or {}
        if not source.get("number"):
            continue
        later.append({"number": source["number"], "url": source.get("html_url"), "title": source.get("title"),
                      "kind": "pull_request" if source.get("pull_request") else "issue",
                      "state": source.get("state"), "referenced_at": event.get("created_at"),
                      "body": excerpt(source.get("body"), 600)})
    return {
        "linked_issues": issues,
        "reviews": [{"state": r.get("state"), "body": excerpt(r.get("body"), 600), "url": r.get("html_url"),
                     "submitted_at": r.get("submitted_at")} for r in reviews if r.get("body") or r.get("state") != "COMMENTED"],
        "review_comments": [{"path": c.get("path"), "line": c.get("line") or c.get("original_line"),
                             "body": excerpt(c.get("body"), 600), "url": c.get("html_url")} for c in comments],
        "discussion": [{"body": excerpt(c.get("body"), 600), "url": c.get("html_url"), "created_at": c.get("created_at")}
                       for c in discussion],
        "later_references": later,
    }


def propose_pr(gh, clone, owner, name, pr, *, exclude, max_files, max_lines):
    """Build one proposal for a merged pull request, or return a skip reason."""
    number = pr["number"]
    detail = gh.get(f"/repos/{owner}/{name}/pulls/{number}")
    if detail.get("changed_files", 0) > max_files:
        return None, f"{detail['changed_files']} files changed (limit {max_files})"
    files = gh.get(f"/repos/{owner}/{name}/pulls/{number}/files", per_page=100)
    kept, dropped, code = reviewable_files(files, exclude)
    if not code:
        return None, "no product code among the changed files"
    merge_sha = detail["merge_commit_sha"]
    fetch_commit(clone, merge_sha)
    base_sha, style, verified = resolve_base(clone, merge_sha, detail.get("commits"), [f["filename"] for f in files])
    diff = git(clone, "diff", "--binary", base_sha, merge_sha, "--", *kept).decode(errors="replace")
    if "GIT binary patch" in diff:
        return None, "binary content in the reviewable diff"
    if not diff.strip():
        return None, "empty diff after exclusions"
    changed = sum(1 for line in diff.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---")))
    if changed > max_lines:
        return None, f"{changed} changed lines (limit {max_lines})"
    merged_at = detail["merged_at"]
    proposal = {
        "schema": 1,
        "repo": f"https://github.com/{owner}/{name}",
        "pr": number,
        "title": detail.get("title") or "",
        "merged_at": merged_at,
        "urls": {"pr": detail.get("html_url"),
                 "merge_commit": f"https://github.com/{owner}/{name}/commit/{merge_sha}",
                 "base_commit": f"https://github.com/{owner}/{name}/commit/{base_sha}",
                 "compare": f"https://github.com/{owner}/{name}/compare/{base_sha}...{merge_sha}"},
        "base_sha": base_sha, "merge_sha": merge_sha, "merge_style": style, "base_verified": verified,
        "commits": detail.get("commits"), "files": diff_files(diff), "excluded_files": dropped,
        "has_tests": any("test" in n.lower() for n in kept),
        "packet": {"objective": detail.get("title") or "", "acceptance_draft": acceptance_draft(detail.get("body")),
                   "body_excerpt": excerpt(detail.get("body"))},
        "evidence": evidence_for(gh, owner, name, detail, merged_at),
        "ruling": None,
    }
    return (proposal, diff), None


def load_state(path, repo_url):
    if Path(path).is_file():
        state = json.loads(Path(path).read_text())
        if state.get("repo") != repo_url:
            raise ImportError_(f"{path} belongs to {state.get('repo')}, not {repo_url}")
        return state
    return {"repo": repo_url, "next_page": 1, "exhausted": False, "seen": {}}


def save_state(path, state):
    Path(path).write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def write_proposal(root, proposal, diff):
    directory = root / f"pr-{proposal['pr']}"
    directory.mkdir()
    (directory / "candidate.diff").write_text(diff)
    (directory / "proposal.json").write_text(json.dumps(proposal, indent=1, sort_keys=True) + "\n")
    return directory


def import_batch(repo_url, clone, out, *, batch=10, since=None, max_files=15, max_lines=600, exclude=(),
                 gh=None, per_page=50, numbers=()):
    """Propose up to `batch` new cases; returns the proposal directories written.

    Pull requests are scanned newest first by creation date, so `since` is a
    creation-date cutoff: once the scan reaches a pull request created before
    it, nothing older can qualify and the state is marked exhausted. With
    `numbers`, only those pull requests are proposed and the scan cursor is
    left alone; a pull request already seen is not proposed twice.
    """
    owner, name = parse_repo_url(repo_url)
    gh = gh or GitHub(github_token())
    clone = ensure_clone(repo_url, clone)
    root = Path(out) / f"{owner}--{name}"
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    state = load_state(state_path, repo_url)
    made = []
    if numbers:
        for number in numbers:
            key = str(number)
            if key in state["seen"]:
                continue
            pr = gh.get(f"/repos/{owner}/{name}/pulls/{number}")
            reason = skip_reason(pr, since=None)
            if reason is None:
                try:
                    built, reason = propose_pr(gh, clone, owner, name, pr, exclude=exclude,
                                               max_files=max_files, max_lines=max_lines)
                except CaseError as exc:
                    built, reason = None, f"error: {exc}"
                if built:
                    made.append(write_proposal(root, *built))
                    state["seen"][key] = "proposed"
                    save_state(state_path, state)
                    continue
            state["seen"][key] = "skipped: " + reason
            save_state(state_path, state)
        return made
    while len(made) < batch and not state["exhausted"]:
        page = gh.get(f"/repos/{owner}/{name}/pulls", state="closed", sort="created", direction="desc",
                      per_page=per_page, page=state["next_page"])
        if not page:
            state["exhausted"] = True
            break
        for pr in page:
            key = str(pr["number"])
            if key in state["seen"]:
                continue
            if since and pr.get("created_at", "") < since:
                state["exhausted"] = True  # pages are newest first; nothing older can qualify
                break
            reason = skip_reason(pr, since=since)
            if reason is None:
                try:
                    built, reason = propose_pr(gh, clone, owner, name, pr, exclude=exclude,
                                               max_files=max_files, max_lines=max_lines)
                except CaseError as exc:
                    if isinstance(exc, ImportError_) and "rate limit" in str(exc):
                        save_state(state_path, state)
                        raise
                    built, reason = None, f"error: {exc}"
                if built:
                    made.append(write_proposal(root, *built))
                    state["seen"][key] = "proposed"
                    save_state(state_path, state)
                    if len(made) >= batch:
                        break
                    continue
            state["seen"][key] = "skipped: " + reason
            save_state(state_path, state)
        else:
            state["next_page"] += 1
            if len(page) < per_page:
                state["exhausted"] = True
    save_state(state_path, state)
    return made


# --- rulings -------------------------------------------------------------------

RULING_TEMPLATE = {
    "status": "clean | seeded | rejected",
    "ruled_by": "who made the call",
    "ruled_at": "YYYY-MM-DD",
    "rationale": "why, citing the evidence you relied on",
    "objective": "one sentence, written only from what the PR said at merge time",
    "acceptance": ["what a correct change must do, in the PR's own terms"],
    "expected": [{"id": "kebab-id", "file": "path/in/diff.py", "severity": "blocking",
                  "keywords": ["six", "to", "ten", "concrete", "words"], "description": "the defect and its consequence"}],
    "case_id": "optional explicit case id",
}


def slug(text, limit=40):
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:limit].rstrip("-") or "change"


def validate_ruling(proposal):
    ruling = proposal.get("ruling")
    problems = []
    if not isinstance(ruling, dict):
        return ["no ruling recorded; fill in proposal.json's ruling block first"]
    if ruling.get("status") not in RULING_STATUSES:
        problems.append(f"ruling.status must be one of {RULING_STATUSES}")
    if not str(ruling.get("ruled_by", "")).strip():
        problems.append("ruling.ruled_by is required: a proposal is evidence, a ruling needs an owner")
    if len(str(ruling.get("rationale", "")).strip()) < 20:
        problems.append("ruling.rationale must explain the decision (at least 20 characters)")
    if ruling.get("status") == "rejected":
        return problems
    if not str(ruling.get("objective", "")).strip():
        problems.append("ruling.objective is required")
    acceptance = ruling.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance or not all(isinstance(a, str) and a.strip() for a in acceptance):
        problems.append("ruling.acceptance must be a non-empty list of strings")
    expected = ruling.get("expected") or []
    if ruling.get("status") == "seeded" and not any(isinstance(e, dict) and e.get("severity") == "blocking" for e in expected):
        problems.append("a seeded ruling needs at least one blocking expected finding")
    if ruling.get("status") == "clean" and any(isinstance(e, dict) and e.get("severity") == "blocking" for e in expected):
        problems.append("a clean ruling cannot carry a blocking expected finding; rule it seeded instead")
    for e in expected:
        if not isinstance(e, dict) or not {"id", "file", "severity", "keywords", "description"} <= set(e):
            problems.append("each expected finding needs id, file, severity, keywords and description")
            continue
        if e["file"] not in proposal["files"]:
            problems.append(f"expected finding {e['id']} names {e['file']}, which is not in the diff")
        if not isinstance(e["keywords"], list) or len(e["keywords"]) < 3:
            problems.append(f"expected finding {e['id']} needs at least three keywords")
    problems += hindsight_problems({"objective": ruling.get("objective", ""), "acceptance": acceptance or [],
                                    "source": {"pr": proposal["pr"]}})
    return problems


def promote(proposal_dir, cases_root, *, case_id=None):
    """Turn a ruled proposal into a case; evidence goes to provenance.json, never the prompt."""
    proposal_dir = Path(proposal_dir)
    meta_path = proposal_dir / "proposal.json"
    if not meta_path.is_file():
        raise CaseError(f"{proposal_dir}: no proposal.json")
    proposal = json.loads(meta_path.read_text())
    problems = validate_ruling(proposal)
    if problems:
        raise CaseError(f"{proposal_dir}: " + "; ".join(problems))
    ruling = proposal["ruling"]
    if ruling["status"] == "rejected":
        raise CaseError(f"{proposal_dir}: ruling is rejected; nothing to promote")
    diff = (proposal_dir / "candidate.diff").read_text()
    owner, name = parse_repo_url(proposal["repo"])
    case_id = case_id or ruling.get("case_id") or f"{slug(name, 20)}-pr{proposal['pr']}-{slug(ruling['objective'])}"
    meta = {"id": case_id, "kind": ruling["status"], "repo": proposal["repo"], "base_sha": proposal["base_sha"],
            "author": "history",
            "source": {"pr": proposal["pr"], "merge": proposal["merge_sha"], "url": proposal["urls"]["pr"],
                       "merged_at": proposal["merged_at"], "merge_style": proposal["merge_style"]},
            "objective": ruling["objective"].strip(), "acceptance": [a.strip() for a in ruling["acceptance"]],
            "files": diff_files(diff), "expected": ruling.get("expected") or [],
            "notes": f"Ruled {ruling['status']} by {ruling['ruled_by']} on {ruling.get('ruled_at', '?')}; see provenance.json."}
    case = write_case(Path(cases_root) / case_id, meta, diff)
    provenance = {k: v for k, v in proposal.items() if k != "ruling"}
    (Path(cases_root) / case_id / "provenance.json").write_text(
        json.dumps({"proposal": provenance, "ruling": ruling}, indent=1, sort_keys=True) + "\n")
    return case


def proposal_status(root):
    """One line per proposal under a repository's proposal directory."""
    rows = []
    for directory in sorted(Path(root).glob("pr-*")):
        meta = json.loads((directory / "proposal.json").read_text())
        ruling = meta.get("ruling") or {}
        rows.append({"dir": directory.name, "pr": meta["pr"], "title": meta["title"], "files": len(meta["files"]),
                     "later_references": len(meta["evidence"]["later_references"]),
                     "status": ruling.get("status") or "unruled"})
    return rows


def leak_check(case_dir):
    """Strings from the evidence that must not appear in the reviewer packet."""
    case = load_case(case_dir)
    provenance_path = Path(case_dir) / "provenance.json"
    if not provenance_path.is_file():
        return []
    evidence = json.loads(provenance_path.read_text())["proposal"].get("evidence", {})
    packet = (case["objective"] + "\n" + "\n".join(case["acceptance"])).lower()
    leaks = []
    for item in evidence.get("later_references", []):
        for needle in (f"#{item['number']}", (item.get("title") or "").lower()):
            if needle and len(needle) > 3 and needle in packet:
                leaks.append(f"later reference {needle!r} appears in the reviewer packet")
    return leaks
