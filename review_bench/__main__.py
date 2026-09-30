"""Command line: python -m review_bench <command> ..."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import example, importer, make_case, seeds
from .cases import CaseError, check_applies, hindsight_problems, load_case, load_cases
from .report import render
from .runner import load_rows, run
from .score import misses, rescore

ROOT = Path(__file__).resolve().parent.parent


def criteria_from(path):
    return json.loads(Path(path).read_text())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="review_bench", description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "cases", help="case root (default: cases/)")
    parser.add_argument("--criteria", type=Path, default=ROOT / "criteria.json")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate every case and confirm each diff applies to its base commit")
    check.add_argument("--repo", type=Path, required=True, help="local checkout of the target repository")
    check.add_argument("--ids", nargs="*")

    runp = sub.add_parser("run", help="review cases with one or more backends")
    runp.add_argument("--repo", type=Path, required=True)
    runp.add_argument("--backend", action="append", required=True, help="name[:model[:effort]]; repeatable")
    runp.add_argument("--results", type=Path, required=True, help="JSON lines file to append to")
    runp.add_argument("--repeats", type=int, default=1)
    runp.add_argument("--timeout", type=int, default=2700, help="seconds per call")
    runp.add_argument("--work", type=Path, default=ROOT / "work")
    runp.add_argument("--ids", nargs="*")
    runp.add_argument("--no-resume", action="store_true")
    runp.add_argument("--parallel", type=int, default=1, help="concurrent reviewer calls")
    runp.add_argument("--variant", default="", help="reviewer prompt variant, e.g. callers")

    rep = sub.add_parser("report", help="summarise a results file as Markdown")
    rep.add_argument("--results", type=Path, required=True)
    rep.add_argument("--output", type=Path)

    miss = sub.add_parser("misses", help="list every missed expectation with the findings returned instead")
    miss.add_argument("--results", type=Path, required=True)

    clean = sub.add_parser("make-clean", help="create a clean case from a merge commit by hand")
    clean.add_argument("--repo", type=Path, required=True)
    clean.add_argument("--merge", required=True)
    clean.add_argument("--id", required=True)
    clean.add_argument("--repo-url", required=True)
    clean.add_argument("--objective", required=True)
    clean.add_argument("--acceptance", action="append", required=True)
    clean.add_argument("--exclude", action="append", default=[])
    clean.add_argument("--kind", choices=["clean", "seeded"], default="clean",
                       help="seeded records a historical defect; give --expected from the later fix")
    clean.add_argument("--expected", type=Path, help="JSON list of expected findings (historical seeded case)")
    clean.add_argument("--author", default="history")
    clean.add_argument("--notes")

    seed = sub.add_parser("make-seeded", help="create a seeded case from a clean case and a mutation")
    seed.add_argument("--repo", type=Path, required=True)
    seed.add_argument("--from", dest="clean_dir", type=Path, required=True)
    seed.add_argument("--id", required=True)
    seed.add_argument("--mutation", type=Path, help="unified diff against the clean candidate")
    seed.add_argument("--edits", type=Path, help="JSON list of {file, old, new} exact replacements")
    seed.add_argument("--expected", type=Path, required=True, help="JSON list of expected findings")
    seed.add_argument("--author", default="human")
    seed.add_argument("--notes", default="")

    prop = sub.add_parser("propose-seeds", help="ask a model to propose subtle defects for a clean case")
    prop.add_argument("--repo", type=Path, required=True)
    prop.add_argument("--backend", required=True, help="author model: name[:model[:effort]]")
    prop.add_argument("--from", dest="clean_id", required=True)
    prop.add_argument("--count", type=int, default=3)
    prop.add_argument("--category", action="append", default=[], help="restrict defects to these categories; repeatable")
    prop.add_argument("--output", type=Path, default=ROOT / "proposals" / "seeds")
    prop.add_argument("--timeout", type=int, default=2700)
    prop.add_argument("--work", type=Path, default=ROOT / "work")

    acc = sub.add_parser("accept-seeds", help="turn a reviewed seed proposal file into seeded cases")
    acc.add_argument("proposal_file", type=Path)
    acc.add_argument("--repo", type=Path, required=True)
    acc.add_argument("--only", nargs="*", help="slugs to accept; default all")

    imp = sub.add_parser("import", help="propose cases from a GitHub repository's merged pull requests (resumable)")
    imp.add_argument("--repo-url", required=True, help="https://github.com/<owner>/<name>")
    imp.add_argument("--clone", type=Path, required=True, help="local clone to create or reuse")
    imp.add_argument("--out", type=Path, default=ROOT / "proposals")
    imp.add_argument("--batch", type=int, default=10, help="new proposals to write this run")
    imp.add_argument("--since", help="ignore pull requests created before this ISO date (scan stops there)")
    imp.add_argument("--max-files", type=int, default=15)
    imp.add_argument("--max-lines", type=int, default=600, help="changed lines in the reviewable diff")
    imp.add_argument("--exclude", action="append", default=[], help="path prefix to leave out of diffs; repeatable")
    imp.add_argument("--pr", action="append", type=int, default=[], help="propose these pull requests only; repeatable")

    stat = sub.add_parser("proposals", help="list proposals and their ruling status")
    stat.add_argument("root", type=Path, help="proposals/<owner>--<name>")

    prom = sub.add_parser("promote", help="turn a ruled proposal into a case; evidence goes to provenance.json")
    prom.add_argument("proposal_dir", type=Path)
    prom.add_argument("--id", help="case id (default: <repo>-pr<N>-<objective slug>)")

    ex = sub.add_parser("example", help="build the deterministic example target and, optionally, its synthetic cases")
    ex.add_argument("--target", type=Path, required=True, help="directory to create the example repository in")
    ex.add_argument("--build-cases", action="store_true", help="also write the synthetic cases under --cases")

    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            cases = load_cases(args.cases, args.ids)
            for case in cases:
                check_applies(args.repo, case)
                warnings = hindsight_problems(case)
                if Path(case["directory"], "provenance.json").is_file():
                    warnings += importer.leak_check(case["directory"])
                print(f"ok {case['id']} ({case['kind']}, {len(case['files'])} files, {len(case['expected'])} expected)")
                for warning in warnings:
                    print(f"   warning: {warning}")
            print(f"{len(cases)} case(s) valid")
        elif args.command == "run":
            cases = load_cases(args.cases, args.ids)
            run(cases, args.backend, args.repo, args.results, repeats=args.repeats, work_root=args.work,
                timeout=args.timeout, resume=not args.no_resume, parallel=args.parallel, variant=args.variant)
            rows = load_rows(args.results)
            # The results file may hold cases outside --ids; summarise everything in it.
            text = render(load_cases(args.cases, sorted({r["case"] for r in rows})), rows, criteria_from(args.criteria))
            args.results.with_suffix(".md").write_text(text)
            print(text)
        elif args.command == "report":
            rows = load_rows(args.results)
            cases = load_cases(args.cases, sorted({r["case"] for r in rows}))
            text = render(cases, rows, criteria_from(args.criteria))
            (args.output or args.results.with_suffix(".md")).write_text(text)
            print(text)
        elif args.command == "misses":
            rows = load_rows(args.results)
            cases = load_cases(args.cases, sorted({r["case"] for r in rows}))
            min_hits = criteria_from(args.criteria).get("min_keyword_hits", 1)
            for miss in misses(cases, rescore(cases, rows, min_hits)):
                how = "downgraded" if miss["downgraded"] else "missed"
                print(f"## {miss['case']} x {miss['backend']} #{miss['repeat']}: {how} {miss['expected']['id']}")
                print(f"   expected: {miss['expected']['description']}")
                print(f"   keywords: {', '.join(miss['expected']['keywords'])}")
                for f in miss["findings"]:
                    print(f"   returned [{f['severity']}] {f['file']}: {f['summary']}")
                if not miss["findings"]:
                    print("   returned nothing")
        elif args.command == "make-clean":
            case = make_case.clean_case(args.repo, args.merge, case_id=args.id, cases_root=args.cases,
                                        repo_url=args.repo_url, objective=args.objective,
                                        acceptance=args.acceptance, exclude=args.exclude, kind=args.kind,
                                        expected=json.loads(args.expected.read_text()) if args.expected else (),
                                        author=args.author, notes=args.notes)
            print(f"created {case['id']} ({case['kind']}) with {len(case['files'])} files")
        elif args.command == "make-seeded":
            case = make_case.seeded_case(args.repo, args.clean_dir, case_id=args.id, cases_root=args.cases,
                                         mutation=args.mutation, expected=json.loads(args.expected.read_text()),
                                         edits=json.loads(args.edits.read_text()) if args.edits else None,
                                         notes=args.notes, author=args.author)
            print(f"created {case['id']} with {len(case['expected'])} expected finding(s)")
        elif args.command == "propose-seeds":
            clean = load_case(args.cases / args.clean_id)
            record = seeds.propose(clean, args.backend, args.repo, args.work, args.timeout, count=args.count,
                                   categories=args.category)
            record["categories"] = args.category
            args.output.mkdir(parents=True, exist_ok=True)
            suffix = "--" + "-".join(c.split()[0].lower() for c in args.category) if args.category else ""
            path = args.output / f"{record['author']}--{clean['id']}{suffix}.json"
            path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
            print(f"{len(record['proposals'])} proposal(s) written to {path}; review them, then accept-seeds")
        elif args.command == "accept-seeds":
            made, failures = seeds.accept(args.proposal_file, args.repo, args.cases, only=args.only)
            for name in made:
                print(f"created {name}")
            for failure in failures:
                print(f"rejected {failure['slug']}: {failure['error']}")
            return 1 if failures and not made else 0
        elif args.command == "import":
            made = importer.import_batch(args.repo_url, args.clone, args.out, batch=args.batch, since=args.since,
                                         max_files=args.max_files, max_lines=args.max_lines, exclude=args.exclude,
                                         numbers=args.pr)
            for directory in made:
                print(f"proposed {directory}")
            print(f"{len(made)} proposal(s) written; add a ruling to each proposal.json, then promote")
        elif args.command == "proposals":
            for row in importer.proposal_status(args.root):
                print(f"{row['dir']:<10} {row['status']:<9} files={row['files']:<3} later_refs={row['later_references']:<2} {row['title'][:70]}")
        elif args.command == "promote":
            case = importer.promote(args.proposal_dir, args.cases, case_id=args.id)
            print(f"created {case['id']} ({case['kind']}) with {len(case['files'])} files, {len(case['expected'])} expected")
        elif args.command == "example":
            shas = example.build_target(args.target)
            print(f"built example target at {args.target} ({len(shas)} commits, head {shas[-1][:12]})")
            if args.build_cases:
                for case_id in example.build_cases(args.target, args.cases):
                    print(f"created {case_id}")
    except (CaseError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
