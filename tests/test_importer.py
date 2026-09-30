"""Importer tests against a fake GitHub API and a local squash-merged repository."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from review_bench import importer
from review_bench.__main__ import main
from review_bench.cases import CaseError, check_applies, load_case
from review_bench.prompt import build_prompt

REPO_URL = "https://github.com/acme/widgets"


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.email=f@example.invalid", "-c", "user.name=f", *args],
                          capture_output=True, check=True).stdout.decode()


def make_upstream(home):
    """main with three squash-merged 'pull requests' and one two-commit rebase merge."""
    repo = Path(home) / "upstream"
    repo.mkdir()
    sh(repo, "init", "-q", "-b", "main")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "core.py").write_text("def total(xs):\n    return sum(xs)\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "index.md").write_text("# docs\n")
    (repo / ".github").mkdir()
    (repo / ".github" / "ci.yml").write_text("ci\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "init")
    shas = {"init": sh(repo, "rev-parse", "HEAD").strip()}
    # PR 1: squash merge touching product code, tests and a workflow file.
    (repo / "pkg" / "core.py").write_text("def total(xs):\n    return sum(xs)\n\n\ndef mean(xs):\n    return sum(xs) / len(xs)\n")
    (repo / "pkg" / "test_core.py").write_text("from pkg.core import mean\n\n\ndef test_mean():\n    assert mean([2, 4]) == 3\n")
    (repo / ".github" / "ci.yml").write_text("ci v2\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "Add mean (#1)")
    shas["pr1"] = sh(repo, "rev-parse", "HEAD").strip()
    # PR 2: docs only.
    (repo / "docs" / "index.md").write_text("# docs\n\nmore\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "Docs (#2)")
    shas["pr2"] = sh(repo, "rev-parse", "HEAD").strip()
    # PR 3: rebase merge of two commits.
    (repo / "pkg" / "extra.py").write_text("X = 1\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "extra part 1")
    (repo / "pkg" / "core.py").write_text("def total(xs):\n    return sum(xs)\n\n\ndef mean(xs):\n    if not xs:\n        return 0\n    return sum(xs) / len(xs)\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "extra part 2")
    shas["pr3"] = sh(repo, "rev-parse", "HEAD").strip()
    # PR 4: a later fix that references PR 1.
    (repo / "pkg" / "core.py").write_text("def total(xs):\n    return sum(xs)\n\n\ndef mean(xs):\n    if not xs:\n        raise ValueError('empty')\n    return sum(xs) / len(xs)\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "Fix mean on empty input (#4)")
    shas["pr4"] = sh(repo, "rev-parse", "HEAD").strip()
    return repo, shas


class FakeGitHub:
    def __init__(self, shas):
        self.shas = shas
        self.calls = []
        user = {"login": "dev", "type": "User"}
        bot = {"login": "dependabot[bot]", "type": "Bot"}
        self.pulls = {
            1: {"number": 1, "title": "Add mean", "body": "Adds `mean`.\n\n- mean divides the sum by the count\n- [ ] todo\n\nCloses #10",
                "merged_at": "2026-02-01T00:00:00Z", "created_at": "2026-01-30T00:00:00Z", "merge_commit_sha": shas["pr1"],
                "user": user, "commits": 1, "changed_files": 3, "html_url": "https://github.com/acme/widgets/pull/1"},
            2: {"number": 2, "title": "Docs", "body": "", "merged_at": "2026-02-02T00:00:00Z", "created_at": "2026-02-01T00:00:00Z",
                "merge_commit_sha": shas["pr2"], "user": user, "commits": 1, "changed_files": 1,
                "html_url": "https://github.com/acme/widgets/pull/2"},
            3: {"number": 3, "title": "Extra module and empty mean", "body": "two commits", "merged_at": "2026-02-03T00:00:00Z",
                "created_at": "2026-02-02T00:00:00Z", "merge_commit_sha": shas["pr3"], "user": user, "commits": 2,
                "changed_files": 2, "html_url": "https://github.com/acme/widgets/pull/3"},
            4: {"number": 4, "title": "Fix mean on empty input", "body": "Regression from #1", "merged_at": "2026-02-04T00:00:00Z",
                "created_at": "2026-02-03T00:00:00Z", "merge_commit_sha": shas["pr4"], "user": user, "commits": 1,
                "changed_files": 1, "html_url": "https://github.com/acme/widgets/pull/4"},
            5: {"number": 5, "title": "Bump dep", "body": "", "merged_at": "2026-02-05T00:00:00Z", "created_at": "2026-02-04T00:00:00Z",
                "merge_commit_sha": shas["pr4"], "user": bot, "commits": 1, "changed_files": 1,
                "html_url": "https://github.com/acme/widgets/pull/5"},
            6: {"number": 6, "title": "Abandoned", "body": "", "merged_at": None, "created_at": "2026-02-05T00:00:00Z",
                "merge_commit_sha": None, "user": user, "commits": 1, "changed_files": 1, "html_url": "x"},
        }
        self.files = {1: ["pkg/core.py", "pkg/test_core.py", ".github/ci.yml"], 2: ["docs/index.md"],
                      3: ["pkg/core.py", "pkg/extra.py"], 4: ["pkg/core.py"], 5: ["pyproject.toml"]}

    def get(self, path, **params):
        self.calls.append((path, params))
        parts = path.strip("/").split("/")
        if path == "/repos/acme/widgets/pulls":
            page, per_page = params["page"], params["per_page"]
            ordered = sorted(self.pulls.values(), key=lambda p: p["number"], reverse=True)
            return ordered[(page - 1) * per_page: page * per_page]
        number = int(parts[4])
        if parts[3] == "pulls" and len(parts) == 5:
            return self.pulls[number]
        if parts[3] == "pulls" and parts[5] == "files":
            return [{"filename": f} for f in self.files[number]]
        if parts[3] == "pulls" and parts[5] == "reviews":
            return [{"state": "APPROVED", "body": "LGTM", "html_url": "r", "submitted_at": "2026-01-31T00:00:00Z"}] if number == 1 else []
        if parts[3] == "pulls" and parts[5] == "comments":
            return [{"path": "pkg/core.py", "line": 5, "body": "what about empty lists?", "html_url": "c"}] if number == 1 else []
        if parts[3] == "issues" and len(parts) == 5:
            return {"number": number, "html_url": f"i{number}", "title": "Need a mean helper", "body": "please", "created_at": "2026-01-01T00:00:00Z"}
        if parts[3] == "issues" and parts[5] == "comments":
            return []
        if parts[3] == "issues" and parts[5] == "timeline":
            if number == 1:
                return [{"event": "cross-referenced", "created_at": "2026-02-03T12:00:00Z",
                         "source": {"issue": {"number": 4, "title": "Fix mean on empty input", "html_url": "p4", "state": "closed",
                                              "pull_request": {"url": "x"}, "body": "Regression from #1"}}},
                        {"event": "cross-referenced", "created_at": "2026-01-31T00:00:00Z",
                         "source": {"issue": {"number": 10, "title": "older", "html_url": "i10", "state": "closed", "body": ""}}}]
            return []
        raise AssertionError(path)


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.upstream, self.shas = make_upstream(self.home)
        self.gh = FakeGitHub(self.shas)
        self.out = self.home / "proposals"

    def import_batch(self, **kwargs):
        return importer.import_batch(REPO_URL, self.upstream, self.out, gh=self.gh, per_page=3, **kwargs)

    def test_parse_repo_url(self):
        self.assertEqual(importer.parse_repo_url("https://github.com/acme/widgets.git"), ("acme", "widgets"))
        self.assertEqual(importer.parse_repo_url("git@github.com:acme/widgets"), ("acme", "widgets"))
        with self.assertRaises(CaseError):
            importer.parse_repo_url("https://example.com/x")
        self.assertEqual(importer.linked_issue_numbers("Fixes #12, closes https://github.com/a/b/issues/7 and see #3"), [7, 12])

    def test_batch_proposes_skips_and_resumes(self):
        made = self.import_batch(batch=2)
        self.assertEqual([p.name for p in made], ["pr-4", "pr-3"])
        state = json.loads((self.out / "acme--widgets" / "state.json").read_text())
        self.assertEqual(state["seen"]["6"], "skipped: not merged")
        self.assertEqual(state["seen"]["5"], "skipped: opened by a bot")
        self.assertEqual(state["next_page"], 2)  # page 1 (6, 5, 4) finished; page 2 stopped at PR 3, the second proposal
        made = self.import_batch(batch=5)
        self.assertEqual([p.name for p in made], ["pr-1"])
        state = json.loads((self.out / "acme--widgets" / "state.json").read_text())
        self.assertEqual(state["seen"]["2"], "skipped: no product code among the changed files")
        self.assertTrue(state["exhausted"])
        self.assertEqual(self.import_batch(batch=5), [])
        self.assertEqual(sorted(p.name for p in (self.out / "acme--widgets").glob("pr-*")), ["pr-1", "pr-3", "pr-4"])

    def test_proposal_pins_shas_and_separates_evidence(self):
        self.import_batch(batch=5)
        proposal = json.loads((self.out / "acme--widgets" / "pr-1" / "proposal.json").read_text())
        self.assertEqual((proposal["base_sha"], proposal["merge_sha"], proposal["merge_style"], proposal["base_verified"]),
                         (self.shas["init"], self.shas["pr1"], "squash", True))
        self.assertEqual(proposal["files"], ["pkg/core.py", "pkg/test_core.py"])
        self.assertEqual(proposal["excluded_files"], [".github/ci.yml"])
        self.assertTrue(proposal["has_tests"])
        self.assertEqual(proposal["packet"]["acceptance_draft"], ["mean divides the sum by the count"])
        self.assertEqual(proposal["urls"]["compare"], f"https://github.com/acme/widgets/compare/{self.shas['init']}...{self.shas['pr1']}")
        self.assertEqual([i["number"] for i in proposal["evidence"]["linked_issues"]], [10])
        self.assertEqual(proposal["evidence"]["review_comments"][0]["body"], "what about empty lists?")
        later = proposal["evidence"]["later_references"]
        self.assertEqual([(l["number"], l["kind"]) for l in later], [(4, "pull_request")])  # the older cross-reference is dropped
        self.assertIsNone(proposal["ruling"])
        diff = (self.out / "acme--widgets" / "pr-1" / "candidate.diff").read_text()
        self.assertIn("+def mean(xs):", diff)
        self.assertNotIn("ci v2", diff)
        rebase = json.loads((self.out / "acme--widgets" / "pr-3" / "proposal.json").read_text())
        self.assertEqual((rebase["base_sha"], rebase["merge_style"], rebase["base_verified"]), (self.shas["pr2"], "rebase", True))
        self.assertEqual(rebase["files"], ["pkg/core.py", "pkg/extra.py"])

    def test_limits_skip_large_changes(self):
        made = self.import_batch(batch=5, max_files=1)
        self.assertEqual([p.name for p in made], ["pr-4"])
        state = json.loads((self.out / "acme--widgets" / "state.json").read_text())
        self.assertIn("files changed (limit 1)", state["seen"]["3"])
        self.import_batch(batch=5, max_lines=1)  # already-seen PRs are not revisited
        self.assertEqual(sorted(p.name for p in (self.out / "acme--widgets").glob("pr-*")), ["pr-4"])

    def test_since_stops_at_older_pull_requests(self):
        made = self.import_batch(batch=5, since="2026-02-02T12:00:00Z")
        self.assertEqual([p.name for p in made], ["pr-4"])  # PR 3 was created before the cutoff, so the scan stops there
        state = json.loads((self.out / "acme--widgets" / "state.json").read_text())
        self.assertTrue(state["exhausted"])
        self.assertNotIn("3", state["seen"])
        self.assertNotIn("1", state["seen"])

    def test_specific_pull_requests_leave_the_cursor_alone(self):
        made = self.import_batch(numbers=[1, 5, 6, 1])
        self.assertEqual([p.name for p in made], ["pr-1"])
        state = json.loads((self.out / "acme--widgets" / "state.json").read_text())
        self.assertEqual((state["next_page"], state["exhausted"]), (1, False))
        self.assertEqual(state["seen"]["5"], "skipped: opened by a bot")
        self.assertEqual(state["seen"]["6"], "skipped: not merged")
        self.assertEqual([p.name for p in self.import_batch(batch=5)], ["pr-4", "pr-3"])  # 1 is already proposed

    def test_state_file_is_bound_to_one_repository(self):
        self.import_batch(batch=1)
        state_path = self.out / "acme--widgets" / "state.json"
        state_path.write_text(json.dumps(dict(json.loads(state_path.read_text()), repo="https://github.com/acme/other")))
        with self.assertRaisesRegex(CaseError, "belongs to"):
            self.import_batch(batch=1)

    def ruled(self, number, ruling):
        path = self.out / "acme--widgets" / f"pr-{number}" / "proposal.json"
        proposal = json.loads(path.read_text())
        proposal["ruling"] = ruling
        path.write_text(json.dumps(proposal))
        return path.parent

    def test_promote_requires_a_complete_ruling(self):
        self.import_batch(batch=5)
        cases = self.home / "cases"
        with self.assertRaisesRegex(CaseError, "no ruling"):
            importer.promote(self.out / "acme--widgets" / "pr-1", cases)
        base = {"status": "seeded", "ruled_by": "reviewer", "ruled_at": "2026-03-01", "rationale": "PR 4 shows mean divides by zero on []",
                "objective": "Add a mean helper to core", "acceptance": ["mean returns the arithmetic mean of a non-empty list"],
                "expected": [{"id": "empty-mean", "file": "pkg/core.py", "severity": "blocking",
                              "keywords": ["empty", "ZeroDivisionError", "len(xs)", "division by zero"],
                              "description": "mean([]) raises ZeroDivisionError"}]}
        for change, message in ((dict(status="maybe"), "status"), (dict(ruled_by=""), "ruled_by"),
                                (dict(rationale="short"), "rationale"), (dict(acceptance=[]), "acceptance"),
                                (dict(expected=[]), "blocking expected"),
                                (dict(expected=[dict(base["expected"][0], file="pkg/nope.py")]), "not in the diff"),
                                (dict(expected=[dict(base["expected"][0], keywords=["a"])]), "three keywords"),
                                (dict(objective="Add mean (fixed by #4)"), "later than the source PR"),
                                (dict(acceptance=["mean handles [] (this bug was found later)"]), "hindsight"),
                                (dict(status="clean"), "cannot carry a blocking")):
            self.ruled(1, dict(base, **change))
            with self.assertRaisesRegex(CaseError, message):
                importer.promote(self.out / "acme--widgets" / "pr-1", cases)
        self.ruled(1, dict(base, status="rejected"))
        with self.assertRaisesRegex(CaseError, "rejected"):
            importer.promote(self.out / "acme--widgets" / "pr-1", cases)

    def test_promote_writes_case_and_provenance_without_leaking(self):
        self.import_batch(batch=5)
        ruling = {"status": "seeded", "ruled_by": "jane-doe", "ruled_at": "2026-03-01",
                  "rationale": "PR 4 fixed a ZeroDivisionError on empty input that this change introduced",
                  "objective": "Add a mean helper to core", "acceptance": ["mean returns the arithmetic mean of a list"],
                  "expected": [{"id": "empty-mean", "file": "pkg/core.py", "severity": "blocking",
                                "keywords": ["empty", "ZeroDivisionError", "len(xs)", "division by zero"],
                                "description": "mean([]) raises ZeroDivisionError"}]}
        directory = self.ruled(1, ruling)
        case = importer.promote(directory, self.home / "cases")
        self.assertEqual(case["id"], "widgets-pr1-add-a-mean-helper-to-core")
        self.assertEqual((case["kind"], case["author"], case["base_sha"]), ("seeded", "history", self.shas["init"]))
        self.assertEqual(case["source"]["pr"], 1)
        self.assertEqual(case["source"]["url"], "https://github.com/acme/widgets/pull/1")
        check_applies(self.upstream, case)
        provenance = json.loads((Path(case["directory"]) / "provenance.json").read_text())
        self.assertEqual(provenance["ruling"]["ruled_by"], "jane-doe")
        self.assertEqual(provenance["proposal"]["evidence"]["later_references"][0]["number"], 4)
        prompt = build_prompt(case, has_tools=False)
        for leak in ("Fix mean on empty input", "Regression from #1", "what about empty lists", "ZeroDivisionError", "provenance", "jane-doe"):
            self.assertNotIn(leak, prompt)
        self.assertEqual(importer.leak_check(case["directory"]), [])
        # A clean ruling on the same proposal produces a clean case with a chosen id.
        self.ruled(4, {"status": "clean", "ruled_by": "reviewer", "ruled_at": "2026-03-01",
                       "rationale": "small fix, tests cover it, nothing referenced it afterwards",
                       "objective": "Raise on empty input to mean", "acceptance": ["mean([]) raises ValueError"]})
        clean = importer.promote(self.out / "acme--widgets" / "pr-4", self.home / "cases", case_id="widgets-pr4-clean")
        self.assertEqual((clean["kind"], clean["expected"]), ("clean", []))
        with self.assertRaisesRegex(CaseError, "already exists"):
            importer.promote(directory, self.home / "cases")

    def test_leak_check_catches_evidence_in_packet(self):
        self.import_batch(batch=5)
        self.ruled(1, {"status": "clean", "ruled_by": "r", "ruled_at": "d", "rationale": "believed clean on inspection",
                       "objective": "Add mean; see fix mean on empty input", "acceptance": ["ok"]})
        case = importer.promote(self.out / "acme--widgets" / "pr-1", self.home / "cases")
        self.assertEqual(len(importer.leak_check(case["directory"])), 1)

    def test_cli_import_proposals_and_promote(self):
        from unittest.mock import patch
        with patch.object(importer, "GitHub", lambda token: self.gh), patch.object(importer, "github_token", lambda: None):
            self.assertEqual(main(["import", "--repo-url", REPO_URL, "--clone", str(self.upstream), "--out", str(self.out),
                                   "--batch", "5"]), 0)
        self.assertEqual(main(["proposals", str(self.out / "acme--widgets")]), 0)
        self.assertEqual(main(["--cases", str(self.home / "cases"), "promote", str(self.out / "acme--widgets" / "pr-1")]), 1)
        rows = importer.proposal_status(self.out / "acme--widgets")
        self.assertEqual([(r["pr"], r["status"], r["later_references"]) for r in rows], [(1, "unruled", 1), (3, "unruled", 0), (4, "unruled", 0)])

    def test_resolve_base_falls_back_unverified(self):
        sha, style, verified = importer.resolve_base(self.upstream, self.shas["pr3"], 2, ["pkg/core.py", "pkg/renamed.py"])
        self.assertEqual((style, verified), ("squash", False))
        self.assertEqual(sha, sh(self.upstream, "rev-parse", self.shas["pr3"] + "^1").strip())


if __name__ == "__main__":
    unittest.main()
