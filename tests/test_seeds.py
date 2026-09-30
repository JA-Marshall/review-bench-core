import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_bench import make_case, seeds
from review_bench.__main__ import main
from review_bench.cases import load_case
from review_bench.report import render
from review_bench.runner import load_rows
from review_bench.score import misses, recall_by_author, score_call

from tests.fixture import EXPECTED, make_repo

CRITERIA = {"min_blocking_recall": 0.8, "max_false_blocking_per_clean_case": 0.1, "min_compliance": 1.0}
GOOD = {"slug": "Float Money", "category": "numeric type", "notes": "float for money",
        "edits": [{"file": "shop/money.py", "old": "return Decimal(price) * quantity", "new": "return float(price) * quantity"}],
        "expected": EXPECTED}
BAD_EDIT = dict(GOOD, slug="missing", edits=[{"file": "shop/money.py", "old": "not there", "new": "x"}])
NO_BLOCKING = dict(GOOD, slug="soft", expected=[dict(EXPECTED[0], severity="nit")])


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.repo, self.base, self.head = make_repo(self.home)
        self.cases = self.home / "cases"
        make_case.clean_case(self.repo, self.head, case_id="clean-guard", cases_root=self.cases,
                             repo_url="u", objective="Reject negative quantities", acceptance=["raises on negative"])

    def write_proposals(self, proposals, author="codex-gpt-5-6-sol"):
        path = self.home / "proposals.json"
        path.write_text(json.dumps({"author": author, "spec": "codex", "clean_case": "clean-guard", "proposals": proposals}))
        return path

    def test_author_label_and_brief(self):
        self.assertEqual(seeds.author_label("codex:gpt-5.6-sol:high"), "codex-gpt-5-6-sol")
        self.assertEqual(seeds.author_label("cursor:grok-4.7-high"), "cursor-grok-4-7-high")
        brief = seeds.build_brief(load_case(self.cases / "clean-guard"), 4, ["removing a lock"])
        self.assertIn("Propose 4 distinct defects", brief)
        self.assertIn("- removing a lock", brief)
        self.assertIn('"proposals"', brief)
        self.assertIn("quantity must not be negative", brief)
        steered = seeds.build_brief(load_case(self.cases / "clean-guard"), 2, [], ["boolean operator flip"])
        self.assertIn("must belong to one of these categories", steered)
        self.assertIn("- boolean operator flip", steered)
        self.assertLess(steered.index("categories"), steered.index("CASE:"))

    def test_accept_makes_cases_and_reports_failures(self):
        made, failures = seeds.accept(self.write_proposals([GOOD, BAD_EDIT, NO_BLOCKING]), self.repo, self.cases)
        self.assertEqual(made, ["seeded-codex-gpt-5-6-sol-float-money"])
        self.assertEqual([f["slug"] for f in failures], ["missing", "soft"])
        self.assertIn("occurs 0 times", failures[0]["error"])
        self.assertIn("blocking", failures[1]["error"])
        case = load_case(self.cases / made[0])
        self.assertEqual(case["author"], "codex-gpt-5-6-sol")
        self.assertIn("[numeric type]", case["notes"])
        self.assertIn("float(price)", case["diff"])
        self.assertEqual(load_case(self.cases / "clean-guard")["author"], "history")

    def test_accept_rejects_abstract_keyword_lists(self):
        vague = dict(GOOD, slug="vague", expected=[dict(EXPECTED[0], keywords=["numeric type confusion", "money arithmetic precision loss", "float"])])
        made, failures = seeds.accept(self.write_proposals([vague]), self.repo, self.cases)
        self.assertEqual(made, [])
        self.assertIn("one or two words", failures[0]["error"])
        self.assertIn("Never use abstract", seeds.build_brief(load_case(self.cases / "clean-guard"), 1, []))

    def test_accept_only_selected_slugs(self):
        made, failures = seeds.accept(self.write_proposals([GOOD, BAD_EDIT]), self.repo, self.cases, only=["float-money"])
        self.assertEqual((made, failures), (["seeded-codex-gpt-5-6-sol-float-money"], []))

    def test_propose_with_fake_codex_uses_proposal_schema(self):
        fake = self.home / "bin"
        fake.mkdir()
        code = r"""import json, sys
args = sys.argv[1:]
schema = json.load(open(args[args.index('--output-schema') + 1]))
assert 'proposals' in schema['properties']
assert 'Propose 2 distinct defects' in sys.stdin.read()
open(args[args.index('-o') + 1], 'w').write(json.dumps({'proposals': [GOOD]}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))
""".replace("GOOD", repr(GOOD))
        (fake / "codex").write_text("#!" + sys.executable + "\n" + code)
        (fake / "codex").chmod(0o755)
        with patch.dict(os.environ, {"PATH": str(fake) + os.pathsep + os.environ["PATH"]}):
            record = seeds.propose(load_case(self.cases / "clean-guard"), "codex", self.repo, self.home / "work", 30, count=2)
        self.assertEqual(record["author"], "codex-gpt-5-6-sol")
        self.assertEqual(record["proposals"][0]["slug"], "Float Money")
        with self.assertRaises(Exception):
            seeds.propose(load_case(self.cases / "clean-guard"), "null", self.repo, self.home / "work", 30)

    def test_historical_seeded_case_from_merge(self):
        case = make_case.clean_case(self.repo, self.head, case_id="seeded-history-guard", cases_root=self.cases,
                                    repo_url="u", objective="o", acceptance=["a"], kind="seeded",
                                    expected=[{"id": "guard", "file": "shop/money.py", "severity": "blocking",
                                               "keywords": ["negative"], "description": "d"}], notes="from fix PR 2")
        self.assertEqual((case["kind"], case["author"], case["notes"]), ("seeded", "history", "from fix PR 2"))

    def test_recall_by_author_and_misses(self):
        seeds.accept(self.write_proposals([GOOD]), self.repo, self.cases)
        seeds.accept(self.write_proposals([dict(GOOD, slug="grok-float")], author="cursor-grok"), self.repo, self.cases)
        cases = [load_case(self.cases / "seeded-codex-gpt-5-6-sol-float-money"), load_case(self.cases / "seeded-cursor-grok-grok-float")]
        hit = {"file": "shop/money.py", "severity": "blocking", "summary": "float used for money", "failure_scenario": "x"}
        rows = [
            {"case": cases[0]["id"], "backend": "b", "repeat": 0, "valid": True, "result": {"findings": [hit]}, "score": score_call(cases[0], {"findings": [hit]})},
            {"case": cases[1]["id"], "backend": "b", "repeat": 0, "valid": True, "result": {"findings": []}, "score": score_call(cases[1], {"findings": []})},
        ]
        by_author = recall_by_author(cases, rows)
        self.assertEqual(by_author["b"]["codex-gpt-5-6-sol"], {"recall": 1.0, "expectations": 1})
        self.assertEqual(by_author["b"]["cursor-grok"], {"recall": 0.0, "expectations": 1})
        missed = misses(cases, rows)
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["expected"]["id"], "float-money")
        for row in rows:
            row.update(kind="seeded", error=None, seconds=1.0)
        text = render(cases, rows, CRITERIA)
        self.assertIn("## Blocking recall by seed author", text)
        self.assertIn("| b | 1.00 (n=1) | 0.00 (n=1) |", text)

    def test_cli_accept_and_misses(self):
        path = self.write_proposals([GOOD])
        base = ["--cases", str(self.cases)]
        self.assertEqual(main(base + ["accept-seeds", str(path), "--repo", str(self.repo)]), 0)
        results = self.home / "r.jsonl"
        self.assertEqual(main(base + ["run", "--repo", str(self.repo), "--backend", "null", "--results", str(results),
                                      "--work", str(self.home / "work"), "--ids", "seeded-codex-gpt-5-6-sol-float-money"]), 0)
        self.assertEqual(len(load_rows(results)), 1)
        self.assertEqual(main(base + ["misses", "--results", str(results)]), 0)
        self.assertEqual(main(base + ["accept-seeds", str(self.write_proposals([BAD_EDIT])), "--repo", str(self.repo)]), 1)


if __name__ == "__main__":
    unittest.main()
