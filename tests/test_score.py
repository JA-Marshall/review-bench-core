import unittest

from review_bench.score import ensembles, matches, misses, passes, rescore, score_call, summarize, wilson

CRITERIA = {"min_blocking_recall": 0.8, "max_false_blocking_per_clean_case": 0.1, "min_compliance": 1.0}
EXPECTED = {"id": "atomic", "file": "app/stock.py", "severity": "blocking",
            "keywords": ["atomic", "transaction"], "description": "dropped transaction"}
SEEDED = {"id": "seeded-1", "kind": "seeded", "expected": [EXPECTED]}
CLEAN = {"id": "clean-1", "kind": "clean", "expected": []}


def finding(file="app/stock.py", severity="blocking", summary="No transaction around the move", scenario="x"):
    return {"file": file, "severity": severity, "summary": summary, "failure_scenario": scenario}


def row(case, backend, repeat, result, valid=True, error=None):
    return {"case": case["id"], "backend": backend, "repeat": repeat, "valid": valid, "error": error,
            "seconds": 1.0, "result": result, "score": score_call(case, result if valid else None)}


class MatchTests(unittest.TestCase):
    def test_keyword_and_file_required(self):
        self.assertTrue(matches(finding(), EXPECTED))
        self.assertTrue(matches(finding(file="/snap/app/stock.py", summary="", scenario="ATOMIC block missing"), EXPECTED))
        self.assertFalse(matches(finding(file="app/other.py"), EXPECTED))
        self.assertFalse(matches(finding(summary="typo in comment"), EXPECTED))

    def test_min_hits_demands_more_keywords(self):
        self.assertTrue(matches(finding(), EXPECTED, min_hits=1))
        self.assertFalse(matches(finding(), EXPECTED, min_hits=2))
        self.assertTrue(matches(finding(summary="no atomic transaction"), EXPECTED, min_hits=2))

    def test_score_call_separates_caught_and_spurious(self):
        result = {"findings": [finding(), finding(severity="nit", summary="rename"), finding(file="a.py", severity="blocking", summary="crash")]}
        score = score_call(SEEDED, result)
        self.assertEqual(score["caught"], {"atomic": {"hit": True, "blocking": True, "severity_hit": True}})
        self.assertEqual((score["spurious_blocking"], score["nits"], score["findings"]), (1, 1, 3))
        self.assertEqual(score_call(SEEDED, None)["caught"], {"atomic": {"hit": False, "blocking": False, "severity_hit": False}})

    def test_caught_at_lower_severity_is_a_hit_but_not_blocking(self):
        score = score_call(SEEDED, {"findings": [finding(severity="should_fix")]})
        self.assertEqual(score["caught"]["atomic"], {"hit": True, "blocking": False, "severity_hit": False})


class SummaryTests(unittest.TestCase):
    def rows(self):
        return [
            row(SEEDED, "good", 0, {"findings": [finding()]}),
            row(SEEDED, "good", 1, {"findings": [finding()]}),
            row(CLEAN, "good", 0, {"findings": [finding(severity="nit")]}),
            row(CLEAN, "good", 1, {"findings": []}),
            row(SEEDED, "noisy", 0, {"findings": [finding(), finding(file="b.py", summary="also wrong")]}),
            row(SEEDED, "noisy", 1, {"findings": []}),
            row(CLEAN, "noisy", 0, {"findings": [finding(file="a.py", summary="spurious")]}),
            row(CLEAN, "noisy", 1, {"findings": [finding(file="a.py", summary="spurious")]}),
            row(SEEDED, "timid", 0, {"findings": [finding(severity="nit")]}),
            row(SEEDED, "broken", 0, None, valid=False, error="BackendError: boom"),
        ]

    def test_metrics_and_pass(self):
        summary = summarize([SEEDED, CLEAN], self.rows(), CRITERIA)
        good, noisy, timid, broken = summary["good"], summary["noisy"], summary["timid"], summary["broken"]
        self.assertEqual((good["blocking_recall"], good["false_blocking_per_clean_case"], good["mean_nits"]), (1.0, 0.0, 0.25))
        self.assertEqual(good["false_blocking_per_seeded_case"], 0.0)
        self.assertTrue(good["pass"])
        self.assertEqual((noisy["blocking_recall"], noisy["false_blocking_per_clean_case"]), (0.5, 1.0))
        self.assertEqual(noisy["false_blocking_per_seeded_case"], 0.5)
        self.assertFalse(noisy["pass"])
        # Reporting the defect as a nit is lenient recall, not blocking recall.
        self.assertEqual((timid["blocking_recall"], timid["lenient_recall"]), (0.0, 1.0))
        self.assertFalse(timid["pass"])
        self.assertEqual((broken["compliance"], broken["errors"], broken["blocking_recall"]), (0.0, 1, None))
        self.assertFalse(broken["pass"])
        low, high = good["blocking_recall_ci"]
        self.assertEqual(good["blocking_expectation_calls"], 2)
        self.assertLess(low, 1.0)
        self.assertEqual(high, 1.0)

    def test_seeded_false_blocking_criterion_is_optional(self):
        strict = dict(CRITERIA, max_false_blocking_per_seeded_case=0.25)
        summary = summarize([SEEDED, CLEAN], self.rows(), strict)
        self.assertTrue(summary["good"]["pass"])
        # noisy already fails on the clean side; a reviewer that is right but noisy on seeds fails only under the stricter criteria.
        rows = [row(SEEDED, "spray", r, {"findings": [finding(), finding(file="b.py", summary="also wrong")]}) for r in range(2)]
        rows += [row(CLEAN, "spray", r, {"findings": []}) for r in range(2)]
        self.assertTrue(summarize([SEEDED, CLEAN], rows, CRITERIA)["spray"]["pass"])
        self.assertFalse(summarize([SEEDED, CLEAN], rows, strict)["spray"]["pass"])

    def test_pairs_use_blocking_hits(self):
        pairs = ensembles([SEEDED, CLEAN], self.rows())
        good_noisy = pairs["good + noisy"]
        self.assertEqual(good_noisy["union_recall"], 1.0)
        self.assertEqual(good_noisy["intersection_recall"], 0.5)
        self.assertEqual(good_noisy["union_false_blocking_per_clean_case"], 1.0)
        self.assertEqual(good_noisy["intersection_false_blocking_per_clean_case"], 0.0)
        self.assertEqual(good_noisy["paired_calls"], 4)
        self.assertEqual(pairs["good + timid"]["union_recall"], 1.0)
        self.assertEqual(pairs["good + timid"]["intersection_recall"], 0.0)
        self.assertNotIn("broken + good", pairs)

    def test_rescore_applies_expectations_added_after_the_run(self):
        stale = row(CLEAN, "b", 0, {"findings": [finding(severity="should_fix")]})
        self.assertEqual(stale["score"]["caught"], {})
        later = dict(CLEAN, expected=[dict(EXPECTED, severity="should_fix")])
        rescore([later], [stale])
        self.assertEqual(stale["score"]["caught"], {"atomic": {"hit": True, "blocking": False, "severity_hit": True}})
        broken = row(SEEDED, "b", 0, None, valid=False, error="x")
        rescore([SEEDED], [broken])
        self.assertFalse(broken["score"]["caught"]["atomic"]["hit"])
        rescore([SEEDED], [stale := row(SEEDED, "b", 0, {"findings": [finding()]})], min_hits=2)
        self.assertFalse(stale["score"]["caught"]["atomic"]["hit"])

    def test_misses_reports_downgrades(self):
        rows = [row(SEEDED, "timid", 0, {"findings": [finding(severity="nit")]}),
                row(SEEDED, "blind", 0, {"findings": []}),
                row(SEEDED, "good", 0, {"findings": [finding()]})]
        missed = misses([SEEDED], rows)
        self.assertEqual([(m["backend"], m["downgraded"]) for m in missed], [("timid", True), ("blind", False)])

    def test_passes_requires_compliance(self):
        self.assertFalse(passes({"blocking_recall": 1.0, "false_blocking_per_clean_case": 0.0,
                                 "false_blocking_per_seeded_case": 0.0, "compliance": 0.9}, CRITERIA))

    def test_wilson(self):
        self.assertEqual(wilson(0, 0), (None, None))
        low, high = wilson(9, 10)
        self.assertTrue(0.55 < low < 0.62 and 0.97 < high < 1.0)
        self.assertEqual(wilson(0, 5)[0], 0.0)


if __name__ == "__main__":
    unittest.main()
