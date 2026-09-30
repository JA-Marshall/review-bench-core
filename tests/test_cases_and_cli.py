import json
import os
from pathlib import Path
import tempfile
import unittest

from review_bench.__main__ import main
from review_bench.cases import CaseError, candidate_tree, check_applies, diff_files, hindsight_problems, load_case, load_cases
from review_bench import make_case
from review_bench.prompt import build_prompt, validate_result
from review_bench.runner import load_rows

from tests.fixture import EXPECTED, make_repo, write_expected, write_mutation

CRITERIA = Path(__file__).resolve().parent.parent / "criteria.json"


class CaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.repo, self.base, self.head = make_repo(self.home)
        self.cases = self.home / "cases"

    def clean(self):
        return make_case.clean_case(self.repo, self.head, case_id="clean-guard", cases_root=self.cases,
                                    repo_url="https://example.invalid/target", objective="Reject negative quantities",
                                    acceptance=["line_total raises on negative quantity"])

    def seeded(self):
        return make_case.seeded_case(self.repo, self.cases / "clean-guard", case_id="seeded-float", cases_root=self.cases,
                                     mutation=write_mutation(self.home), expected=EXPECTED, notes="money as float")

    def test_clean_case_excludes_workflow_files_and_applies(self):
        case = self.clean()
        self.assertEqual(case["files"], ["docs/plans/task.md", "shop/money.py"])
        self.assertEqual((case["kind"], case["base_sha"], case["expected"]), ("clean", self.base, []))
        self.assertNotIn("AGENTS.md", case["diff"])
        self.assertNotIn(".github", case["diff"])
        check_applies(self.repo, case)
        with self.assertRaises(CaseError):
            self.clean()  # never overwrite an existing case

    def test_exclude_prefixes_are_configurable(self):
        case = make_case.clean_case(self.repo, self.head, case_id="clean-code-only", cases_root=self.cases,
                                    repo_url="u", objective="o", acceptance=["a"], exclude=["docs/"])
        self.assertEqual(case["files"], ["shop/money.py"])

    def test_seeded_case_contains_mutation_and_expectation(self):
        self.clean()
        case = self.seeded()
        self.assertIn("+    return float(price) * quantity", case["diff"])
        self.assertIn("quantity must not be negative", case["diff"])
        self.assertEqual(case["expected"][0]["id"], "float-money")
        self.assertTrue((self.cases / "seeded-float" / "mutation.diff").is_file())
        check_applies(self.repo, case)
        self.assertNotEqual(case["candidate"], load_case(self.cases / "clean-guard")["candidate"])

    def test_seeded_case_from_edits(self):
        self.clean()
        edits = [{"file": "shop/money.py", "old": "return Decimal(price) * quantity", "new": "return float(price) * quantity"}]
        case = make_case.seeded_case(self.repo, self.cases / "clean-guard", case_id="seeded-edit", cases_root=self.cases,
                                     expected=EXPECTED, edits=edits)
        self.assertIn("+    return float(price) * quantity", case["diff"])
        mutation = (self.cases / "seeded-edit" / "mutation.diff").read_text()
        self.assertIn("-    return Decimal(price) * quantity", mutation)
        self.assertNotIn("+    if quantity < 0", mutation)  # the clean change is context, not part of the mutation
        check_applies(self.repo, case)
        for bad, message in (([dict(edits[0], old="missing text")], "occurs 0 times"),
                             ([dict(edits[0], file="nope.py")], "does not exist"),
                             (None, "exactly one of")):
            with self.assertRaisesRegex(CaseError, message):
                make_case.seeded_case(self.repo, self.cases / "clean-guard", case_id="seeded-bad", cases_root=self.cases,
                                      expected=EXPECTED, edits=bad)

    def test_validation_errors(self):
        self.clean()
        meta_path = self.cases / "clean-guard" / "case.json"
        meta = json.loads(meta_path.read_text())
        for change, message in ((dict(kind="replay"), "kind"), (dict(files=["x.py"]), "files"),
                                (dict(base_sha="abc"), "base_sha"), (dict(objective=" "), "objective"),
                                (dict(expected=[dict(EXPECTED[0])]), "clean case cannot"),
                                (dict(kind="seeded"), "seeded case needs"),
                                (dict(adjudications=[{"finding": "x", "verdict": "maybe", "note": "n"}]), "verdict"),
                                (dict(adjudications=["x"]), "adjudication needs")):
            meta_path.write_text(json.dumps(dict(meta, **change)))
            with self.assertRaisesRegex(CaseError, message):
                load_case(self.cases / "clean-guard")
        meta_path.write_text(json.dumps(dict(meta, adjudications=[{"finding": "x", "verdict": "false_positive", "note": "n"}])))
        self.assertEqual(len(load_case(self.cases / "clean-guard")["adjudications"]), 1)
        meta_path.write_text(json.dumps(dict(meta, files=["shop/money.py"])))
        (self.cases / "clean-guard" / "candidate.diff").write_text("diff --git a/shop/money.py b/shop/money.py\n--- a/shop/money.py\n+++ b/shop/money.py\n@@ -1 +1 @@\n-nonsense\n+other\n")
        with self.assertRaisesRegex(CaseError, "does not apply"):
            check_applies(self.repo, load_case(self.cases / "clean-guard"))
        with self.assertRaisesRegex(CaseError, "Unknown case ids"):
            load_cases(self.cases, ["missing"])

    def test_hindsight_lint(self):
        self.assertEqual(hindsight_problems({"objective": "Fix regression in parser", "acceptance": ["Closes #12"], "source": {"pr": 20}}), [])
        problems = hindsight_problems({"objective": "Add pagination (fixed by #31)", "acceptance": ["See #40", "was reverted later"],
                                       "source": {"pr": 30}})
        self.assertEqual(len(problems), 4)
        self.assertTrue(any("#31" in p for p in problems) and any("#40" in p for p in problems))

    def test_diff_files_and_prompt_contract(self):
        self.clean()
        case = load_case(self.cases / "clean-guard")
        self.assertEqual(diff_files(case["diff"]), ["docs/plans/task.md", "shop/money.py"])
        prompt = build_prompt(case, has_tools=True)
        self.assertIn(case["candidate"], prompt)
        self.assertIn("working directory is a snapshot", prompt)
        self.assertNotIn("working directory is a snapshot", build_prompt(case, has_tools=False))
        self.assertNotIn("notes", prompt.split("DIFF:")[0].lower().replace("nothing else", ""))
        good = {"candidate": case["candidate"], "covered_files": case["files"], "findings": []}
        self.assertEqual(validate_result(good, case), [])
        self.assertIn("candidate hash mismatch", validate_result(dict(good, candidate="x"), case))
        self.assertIn("covered_files does not equal the diff's files", validate_result(dict(good, covered_files=[]), case))
        bad = dict(good, findings=[{"file": "a", "severity": "urgent", "summary": "s", "failure_scenario": "f"}])
        self.assertEqual(validate_result(bad, case), ["finding 0 is malformed"])
        self.assertEqual(validate_result("text", case), ["result is not an object"])

    def test_cli_end_to_end_with_oracle_and_null(self):
        self.clean()
        self.seeded()
        results = self.home / "results" / "r.jsonl"
        base = ["--cases", str(self.cases), "--criteria", str(CRITERIA)]
        self.assertEqual(main(base + ["check", "--repo", str(self.repo)]), 0)
        self.assertEqual(main(base + ["run", "--repo", str(self.repo), "--backend", "oracle", "--backend", "null",
                                      "--results", str(results), "--repeats", "2", "--work", str(self.home / "work")]), 0)
        rows = load_rows(results)
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(r["valid"] for r in rows))
        summary = results.with_suffix(".md").read_text()
        self.assertIn("| oracle | PASS |", summary)
        self.assertIn("| null | FAIL |", summary)
        self.assertIn("null + oracle", summary)
        # Resume skips completed calls; report regenerates from the file.
        self.assertEqual(main(base + ["run", "--repo", str(self.repo), "--backend", "oracle",
                                      "--results", str(results), "--repeats", "2", "--work", str(self.home / "work")]), 0)
        self.assertEqual(len(load_rows(results)), 8)
        self.assertEqual(main(base + ["report", "--results", str(results)]), 0)
        self.assertEqual(main(base + ["misses", "--results", str(results)]), 0)
        # A restricted rerun into the same file must still summarise the other cases in it.
        self.assertEqual(main(base + ["run", "--repo", str(self.repo), "--backend", "oracle", "--results", str(results),
                                      "--repeats", "3", "--work", str(self.home / "work"), "--ids", "clean-guard"]), 0)
        self.assertIn("seeded-float", results.with_suffix(".md").read_text())

    def test_cli_make_commands(self):
        base = ["--cases", str(self.cases)]
        self.assertEqual(main(base + ["make-clean", "--repo", str(self.repo), "--merge", self.head, "--id", "clean-cli",
                                      "--repo-url", "u", "--objective", "o", "--acceptance", "a", "--exclude", "docs/"]), 0)
        self.assertEqual(main(base + ["make-seeded", "--repo", str(self.repo), "--from", str(self.cases / "clean-cli"),
                                      "--id", "seeded-cli", "--mutation", str(write_mutation(self.home)),
                                      "--expected", str(write_expected(self.home)), "--author", "me"]), 0)
        self.assertEqual(load_case(self.cases / "seeded-cli")["author"], "me")
        self.assertEqual(main(base + ["make-seeded", "--repo", str(self.repo), "--from", str(self.cases / "clean-cli"),
                                      "--id", "seeded-cli", "--mutation", str(write_mutation(self.home)),
                                      "--expected", str(write_expected(self.home))]), 1)

    def test_candidate_tree_is_absolute_and_cached(self):
        self.clean()
        case = load_case(self.cases / "clean-guard")
        cwd = os.getcwd()
        os.chdir(self.home)
        try:
            tree = candidate_tree(self.repo, case, "work")
        finally:
            os.chdir(cwd)
        self.assertTrue(tree.is_absolute())
        self.assertEqual(tree, (self.home / "work").resolve() / "trees" / f"clean-guard-{case['candidate'][:12]}")
        self.assertIn("quantity must not be negative", (tree / "shop" / "money.py").read_text())
        self.assertEqual(candidate_tree(self.repo, case, self.home / "work"), tree)

    def test_candidate_tree_builds_once_under_concurrency(self):
        self.clean()
        case = load_case(self.cases / "clean-guard")
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            trees = list(pool.map(lambda _: candidate_tree(self.repo, case, self.home / "work"), range(4)))
        self.assertEqual(len(set(trees)), 1)
        self.assertTrue(trees[0].is_dir())
        self.assertFalse(trees[0].with_name(trees[0].name + ".partial").exists())

    def test_parallel_run_writes_every_row_once(self):
        self.clean()
        self.seeded()
        results = self.home / "results" / "p.jsonl"
        base = ["--cases", str(self.cases), "--criteria", str(CRITERIA)]
        self.assertEqual(main(base + ["run", "--repo", str(self.repo), "--backend", "oracle", "--backend", "null",
                                      "--results", str(results), "--repeats", "3", "--parallel", "4",
                                      "--work", str(self.home / "work")]), 0)
        rows = load_rows(results)
        self.assertEqual(len(rows), 12)
        self.assertEqual(len(results.read_text().splitlines()), 12)
        self.assertEqual(len({(r["case"], r["backend"], r["repeat"]) for r in rows}), 12)
        self.assertTrue(all(r["valid"] for r in rows))

    def test_cli_reports_case_errors(self):
        self.assertEqual(main(["--cases", str(self.home / "nowhere"), "check", "--repo", str(self.repo)]), 1)


if __name__ == "__main__":
    unittest.main()
