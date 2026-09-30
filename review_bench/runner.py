"""Run cases across backends with repeats; append one JSON line per call."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
import traceback

from . import backends as B
from .cases import candidate_tree
from .prompt import build_prompt, validate_result
from .score import score_call


def review_once(case, spec, repo, work_root, timeout, variant=""):
    name, model, effort = B.parse_spec(spec)
    has_tools = name in B.HAS_TOOLS
    tree = candidate_tree(repo, case, work_root) if has_tools else Path(work_root).resolve() / "no-tree"
    tree.mkdir(parents=True, exist_ok=True)
    prompt = build_prompt(case, has_tools=has_tools, variant=variant)
    started = time.time()
    label = B.label(spec) + (f"+{variant}" if variant else "")
    row = {"case": case["id"], "kind": case["kind"], "backend": label, "model": model, "variant": variant,
           "effort": effort, "valid": False, "error": None, "problems": [], "result": None,
           "usage": {}, "seconds": None, "score": score_call(case, None)}
    try:
        result, usage, raw = B.BACKENDS[name](case, prompt, tree, model, effort, timeout)
        row.update(result=result, usage=usage, raw=raw[-20000:])
        row["problems"] = validate_result(result, case)
        row["valid"] = not row["problems"]
        if row["valid"]:
            row["score"] = score_call(case, result)
    except Exception as exc:  # noqa: BLE001 - every failure is data, recorded with its cause
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["traceback"] = traceback.format_exc()[-2000:]
    row["seconds"] = round(time.time() - started, 2)
    return row


def run(cases, specs, repo, results_path, *, repeats=1, work_root, timeout, resume=True, parallel=1, variant=""):
    """Review every (case, backend, repeat) not already done; `parallel` calls run at once.

    Calls are independent CLI or API invocations, so they parallelise cleanly; the
    results file is appended under a lock, one complete JSON line per call.
    """
    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if resume and results_path.is_file():
        for line in results_path.read_text().splitlines():
            row = json.loads(line)
            if row["valid"] or not row.get("error"):
                done.add((row["case"], row["backend"], row["repeat"]))
    suffix = f"+{variant}" if variant else ""
    jobs = [(case, spec, repeat) for case in cases for spec in specs for repeat in range(repeats)
            if (case["id"], B.label(spec) + suffix, repeat) not in done]
    rows, lock = [], threading.Lock()

    def job(case, spec, repeat):
        row = review_once(case, spec, repo, work_root, timeout, variant)
        row["repeat"] = repeat
        row["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with lock:
            with results_path.open("a") as out:
                out.write(json.dumps(row, sort_keys=True) + "\n")
            rows.append(row)
            status = "ok" if row["valid"] else ("error" if row["error"] else "invalid")
            print(f"{case['id']} x {row['backend']} #{repeat}: {status} in {row['seconds']}s "
                  f"({row['score']['findings']} findings) [{len(rows)}/{len(jobs)}]", flush=True)
        return row

    if parallel <= 1:
        for case, spec, repeat in jobs:
            job(case, spec, repeat)
    else:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = [pool.submit(job, case, spec, repeat) for case, spec, repeat in jobs]
            for future in futures:
                future.result()
    return rows


def load_rows(results_path):
    rows = [json.loads(line) for line in Path(results_path).read_text().splitlines() if line.strip()]
    latest = {}
    for row in rows:  # A rerun after an error replaces the earlier failed row.
        latest[(row["case"], row["backend"], row["repeat"])] = row
    return list(latest.values())
