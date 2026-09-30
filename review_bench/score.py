"""Deterministic scoring of reviewer results against case ground truth.

Matching is keyword based and deliberately simple: a finding hits an
expectation when it names the expected file and its text contains at least
`min_hits` of the expectation's keywords. Two recall figures are reported:

- blocking recall: the finding must also carry severity `blocking`. This is
  the headline number, because a reviewer that downgrades a real defect to a
  nit would not stop an automated fix loop.
- lenient recall: any severity counts. The gap between the two shows how often
  a reviewer sees the defect but misjudges it.

Keyword matching over-counts: a long finding on the right file can contain a
keyword by accident. `min_hits` of 2 or more reduces that; the `misses`
command and hand audits of hits are the real check. See docs/METHODOLOGY.md.
"""
from __future__ import annotations

from itertools import combinations
from math import sqrt
from statistics import mean


def keyword_hits(finding, expected):
    text = (finding["summary"] + " " + finding["failure_scenario"]).lower()
    return sum(1 for keyword in expected["keywords"] if keyword.lower() in text)


def matches(finding, expected, min_hits=1):
    """A finding hits an expectation when it names the file and enough keywords."""
    if not finding["file"].endswith(expected["file"]):
        return False
    return keyword_hits(finding, expected) >= max(1, min_hits)


def score_call(case, result, min_hits=1):
    """Per-call outcome: which expectations were caught, at what severity, and what was spurious."""
    findings = result["findings"] if result else []
    caught = {}
    matched = set()
    for expected in case["expected"]:
        hits = [f for f in findings if matches(f, expected, min_hits)]
        matched.update(id(f) for f in hits)
        caught[expected["id"]] = {
            "hit": bool(hits),
            "blocking": any(f["severity"] == "blocking" for f in hits),
            "severity_hit": any(f["severity"] == expected["severity"] for f in hits),
        }
    spurious = [f for f in findings if id(f) not in matched]
    return {"caught": caught,
            "spurious_blocking": sum(1 for f in spurious if f["severity"] == "blocking"),
            "spurious_should_fix": sum(1 for f in spurious if f["severity"] == "should_fix"),
            "nits": sum(1 for f in findings if f["severity"] == "nit"),
            "findings": len(findings)}


def rescore(cases, rows, min_hits=1):
    """Score stored results against the cases' current expectations.

    Expectations change after adjudication, so the score saved at run time is
    only a snapshot; every report recomputes from the stored findings.
    """
    by_case = {c["id"]: c for c in cases}
    for row in rows:
        row["score"] = score_call(by_case[row["case"]], row["result"] if row["valid"] else None, min_hits)
    return rows


def wilson(hits, n, z=1.96):
    """Wilson score interval for a proportion; (None, None) when there is no data."""
    if not n:
        return None, None
    p = hits / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def summarize(cases, rows, criteria):
    """Aggregate rows (one per case x backend x repeat) into per-backend metrics."""
    by_case = {c["id"]: c for c in cases}
    backends = sorted({r["backend"] for r in rows})
    blocking_expectations = [(c["id"], e["id"]) for c in cases for e in c["expected"] if e["severity"] == "blocking"]
    summary = {}
    for backend in backends:
        mine = [r for r in rows if r["backend"] == backend]
        valid = [r for r in mine if r["valid"]]
        strict, lenient, pooled_hits, pooled_n = [], [], 0, 0
        for case_id, expected_id in blocking_expectations:
            attempts = [r for r in valid if r["case"] == case_id]
            if attempts:
                strict.append(mean(1.0 if r["score"]["caught"][expected_id]["blocking"] else 0.0 for r in attempts))
                lenient.append(mean(1.0 if r["score"]["caught"][expected_id]["hit"] else 0.0 for r in attempts))
                pooled_hits += sum(1 for r in attempts if r["score"]["caught"][expected_id]["blocking"])
                pooled_n += len(attempts)
        clean_rows = [r for r in valid if by_case[r["case"]]["kind"] == "clean"]
        seeded_rows = [r for r in valid if by_case[r["case"]]["kind"] == "seeded"]
        latencies = [r["seconds"] for r in mine if r.get("seconds") is not None]
        metrics = {
            "calls": len(mine), "valid": len(valid), "errors": len([r for r in mine if r.get("error")]),
            "compliance": len(valid) / len(mine) if mine else 0.0,
            "blocking_recall": mean(strict) if strict else None,
            "blocking_recall_ci": wilson(pooled_hits, pooled_n),
            "blocking_expectation_calls": pooled_n,
            "lenient_recall": mean(lenient) if lenient else None,
            "false_blocking_per_clean_case": (sum(r["score"]["spurious_blocking"] for r in clean_rows) / len(clean_rows)
                                             if clean_rows else None),
            "false_blocking_total_on_clean": sum(r["score"]["spurious_blocking"] for r in clean_rows),
            "clean_cases_reviewed": len({r["case"] for r in clean_rows}),
            "false_blocking_per_seeded_case": (sum(r["score"]["spurious_blocking"] for r in seeded_rows) / len(seeded_rows)
                                              if seeded_rows else None),
            "mean_nits": mean(r["score"]["nits"] for r in valid) if valid else None,
            "mean_findings": mean(r["score"]["findings"] for r in valid) if valid else None,
            "mean_seconds": mean(latencies) if latencies else None,
        }
        metrics["pass"] = passes(metrics, criteria)
        summary[backend] = metrics
    return summary


def passes(metrics, criteria):
    checks = []
    if metrics["blocking_recall"] is not None:
        checks.append(metrics["blocking_recall"] >= criteria["min_blocking_recall"])
    if metrics["false_blocking_per_clean_case"] is not None:
        checks.append(metrics["false_blocking_per_clean_case"] <= criteria["max_false_blocking_per_clean_case"])
    if metrics["false_blocking_per_seeded_case"] is not None and "max_false_blocking_per_seeded_case" in criteria:
        checks.append(metrics["false_blocking_per_seeded_case"] <= criteria["max_false_blocking_per_seeded_case"])
    checks.append(metrics["compliance"] >= criteria["min_compliance"])
    return all(checks) if checks else False


def ensembles(cases, rows):
    """Union and intersection of every backend pair, aligned by case and repeat.

    Recall here is blocking recall: a pair catches a defect only when at least
    one (union) or both (intersection) reported it as blocking.
    """
    backends = sorted({r["backend"] for r in rows})
    index = {(r["backend"], r["case"], r["repeat"]): r for r in rows if r["valid"]}
    by_case = {c["id"]: c for c in cases}
    out = {}
    for a, b in combinations(backends, 2):
        keys = [(c, rep) for (bk, c, rep) in index if bk == a and (b, c, rep) in index]
        if not keys:
            continue
        union_hits, inter_hits, union_fp, inter_fp, clean_n = [], [], 0, 0, 0
        for case_id, rep in keys:
            ra, rb = index[(a, case_id, rep)], index[(b, case_id, rep)]
            for e in by_case[case_id]["expected"]:
                if e["severity"] != "blocking":
                    continue
                ha, hb = ra["score"]["caught"][e["id"]]["blocking"], rb["score"]["caught"][e["id"]]["blocking"]
                union_hits.append(ha or hb)
                inter_hits.append(ha and hb)
            if by_case[case_id]["kind"] == "clean":
                clean_n += 1
                union_fp += max(ra["score"]["spurious_blocking"], rb["score"]["spurious_blocking"])
                inter_fp += min(ra["score"]["spurious_blocking"], rb["score"]["spurious_blocking"])
        out[f"{a} + {b}"] = {
            "union_recall": mean(union_hits) if union_hits else None,
            "intersection_recall": mean(inter_hits) if inter_hits else None,
            "union_false_blocking_per_clean_case": union_fp / clean_n if clean_n else None,
            "intersection_false_blocking_per_clean_case": inter_fp / clean_n if clean_n else None,
            "paired_calls": len(keys)}
    return out


def recall_by_author(cases, rows):
    """Blocking recall per backend per seed author: the check for same-family author bias."""
    by_case = {c["id"]: c for c in cases}
    out = {}
    for row in rows:
        if not row["valid"]:
            continue
        case = by_case[row["case"]]
        for expected in case["expected"]:
            if expected["severity"] != "blocking":
                continue
            bucket = out.setdefault(row["backend"], {}).setdefault(case["author"], [])
            bucket.append(1.0 if row["score"]["caught"][expected["id"]]["blocking"] else 0.0)
    return {backend: {author: {"recall": mean(hits), "expectations": len(hits)} for author, hits in authors.items()}
            for backend, authors in out.items()}


def misses(cases, rows):
    """Every blocking expectation a valid call failed to report as blocking, with the findings returned instead."""
    by_case = {c["id"]: c for c in cases}
    out = []
    for row in rows:
        if not row["valid"]:
            continue
        for expected in by_case[row["case"]]["expected"]:
            outcome = row["score"]["caught"][expected["id"]]
            wanted = outcome["blocking"] if expected["severity"] == "blocking" else outcome["hit"]
            if not wanted:
                out.append({"case": row["case"], "backend": row["backend"], "repeat": row["repeat"],
                            "expected": expected, "downgraded": outcome["hit"],
                            "findings": row["result"]["findings"]})
    return out
