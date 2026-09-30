"""The example target is deterministic and the shipped synthetic cases still match it."""
import json
from pathlib import Path
import tempfile
import unittest

from review_bench import example
from review_bench.__main__ import main
from review_bench.cases import check_applies, load_cases
from review_bench.runner import load_rows

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "cases" / "synthetic"


class ExampleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_target_is_deterministic(self):
        a = example.build_target(self.home / "a")
        b = example.build_target(self.home / "b")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 4)
        with self.assertRaises(FileExistsError):
            example.build_target(self.home / "a")

    def test_shipped_cases_match_a_rebuilt_target(self):
        target = self.home / "target"
        example.build_target(target)
        rebuilt = self.home / "cases"
        ids = example.build_cases(target, rebuilt)
        self.assertEqual(len(ids), 6)
        shipped = {c["id"]: c for c in load_cases(SHIPPED)}
        for case in load_cases(rebuilt):
            self.assertIn(case["id"], shipped)
            self.assertEqual(case["diff"], shipped[case["id"]]["diff"])
            self.assertEqual(case["base_sha"], shipped[case["id"]]["base_sha"])
            self.assertEqual(case["expected"], shipped[case["id"]]["expected"])
            check_applies(target, shipped[case["id"]])
        self.assertEqual(set(shipped), set(ids))

    def test_cli_example_run_with_oracle_and_null(self):
        target = self.home / "target"
        results = self.home / "results.jsonl"
        self.assertEqual(main(["--cases", str(self.home / "cases"), "example", "--target", str(target), "--build-cases"]), 0)
        self.assertEqual(main(["--cases", str(self.home / "cases"), "check", "--repo", str(target)]), 0)
        self.assertEqual(main(["--cases", str(self.home / "cases"), "run", "--repo", str(target), "--backend", "oracle",
                               "--backend", "null", "--results", str(results), "--work", str(self.home / "work")]), 0)
        rows = load_rows(results)
        self.assertEqual(len(rows), 12)
        text = results.with_suffix(".md").read_text()
        self.assertIn("| oracle | PASS | 1.00 |", text)
        self.assertIn("| null | FAIL | 0.00 |", text)
        self.assertEqual(main(["--cases", str(self.home / "cases"), "example", "--target", str(target)]), 1)


if __name__ == "__main__":
    unittest.main()
