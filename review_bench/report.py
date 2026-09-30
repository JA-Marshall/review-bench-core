"""Markdown summary of a results file."""
from __future__ import annotations

from .score import ensembles, recall_by_author, rescore, summarize


def fmt(value, digits=2):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def fmt_ci(ci):
    low, high = ci if ci else (None, None)
    return "-" if low is None else f"{low:.2f}-{high:.2f}"


def render(cases, rows, criteria):
    min_hits = criteria.get("min_keyword_hits", 1)
    rows = rescore(cases, rows, min_hits)
    summary = summarize(cases, rows, criteria)
    seeded_limit = criteria.get("max_false_blocking_per_seeded_case")
    lines = ["# Review benchmark summary", "",
             f"Cases: {len(cases)} ({sum(c['kind'] == 'clean' for c in cases)} clean, "
             f"{sum(c['kind'] == 'seeded' for c in cases)} seeded). Calls: {len(rows)}.", "",
             f"Pass criteria: blocking recall >= {criteria['min_blocking_recall']}, false blocking findings per clean "
             f"case <= {criteria['max_false_blocking_per_clean_case']}"
             + (f", per seeded case <= {seeded_limit}" if seeded_limit is not None else "")
             + f", compliance >= {criteria['min_compliance']}. A hit needs the expected file and at least "
             f"{min_hits} keyword(s); blocking recall also needs severity blocking.", "",
             "| Backend | Pass | Blocking recall | 95% CI | Lenient recall | False blocking / clean case | "
             "False blocking / seeded case | Mean nits | Compliance | Errors | Mean seconds |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for backend, m in summary.items():
        lines.append(f"| {backend} | {'PASS' if m['pass'] else 'FAIL'} | {fmt(m['blocking_recall'])} | "
                     f"{fmt_ci(m['blocking_recall_ci'])} | {fmt(m['lenient_recall'])} | "
                     f"{fmt(m['false_blocking_per_clean_case'])} | {fmt(m['false_blocking_per_seeded_case'])} | "
                     f"{fmt(m['mean_nits'])} | {fmt(m['compliance'])} | {m['errors']} | {fmt(m['mean_seconds'], 0)} |")
    pairs = ensembles(cases, rows)
    if pairs:
        lines += ["", "## Pairs", "",
                  "| Pair | Union recall | Intersection recall | Union false blocking / clean | Intersection false blocking / clean | Paired calls |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for pair, m in pairs.items():
            lines.append(f"| {pair} | {fmt(m['union_recall'])} | {fmt(m['intersection_recall'])} | "
                         f"{fmt(m['union_false_blocking_per_clean_case'])} | "
                         f"{fmt(m['intersection_false_blocking_per_clean_case'])} | {m['paired_calls']} |")
    by_author = recall_by_author(cases, rows)
    authors = sorted({a for authors in by_author.values() for a in authors})
    if len(authors) > 1:
        lines += ["", "## Blocking recall by seed author", "",
                  "| Backend | " + " | ".join(authors) + " |", "| --- |" + " --- |" * len(authors)]
        for backend, per_author in by_author.items():
            cells = [f"{fmt(per_author[a]['recall'])} (n={per_author[a]['expectations']})" if a in per_author else "-"
                     for a in authors]
            lines.append(f"| {backend} | " + " | ".join(cells) + " |")
    lines += ["", "## Per case", "",
              "| Case | Kind | Backend | Repeat | Valid | Caught (blocking) | Caught (any severity) | Spurious blocking | Nits | Error |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in sorted(rows, key=lambda r: (r["case"], r["backend"], r["repeat"])):
        strict = ", ".join(k for k, v in row["score"]["caught"].items() if v["blocking"]) or "-"
        lenient = ", ".join(k for k, v in row["score"]["caught"].items() if v["hit"]) or "-"
        lines.append(f"| {row['case']} | {row['kind']} | {row['backend']} | {row['repeat']} | {row['valid']} | {strict} | "
                     f"{lenient} | {row['score']['spurious_blocking']} | {row['score']['nits']} | "
                     f"{(row.get('error') or '')[:80]} |")
    return "\n".join(lines) + "\n"
