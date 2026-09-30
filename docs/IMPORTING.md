# Reproducing the pydantic example

This walks through how `cases/pydantic` was built and run, so that the batch
can be regenerated, extended, or re-ruled by someone who disagrees with a
ruling. Every command runs from the repository root.

## 1. Choose the source repository

The importer works with any GitHub repository, but a source is only useful for
this benchmark when its history lets you tell clean changes from broken ones:

- a permissive licence, so diffs can be redistributed (pydantic is MIT);
- merged pull requests with descriptive bodies and linked issues;
- fix pull requests that name the change that introduced the bug ("regression
  from #13518"), which is what turns a merged change into a historical seed;
- focused tests per change, so a ruling can be checked by running them.

pydantic was chosen over black and marshmallow on those grounds: several
hundred merged pull requests a year, dozens of fix PRs a year whose body names
the causing PR, and a test suite that runs per file. The trade-off is that a
mature project's fixes are subtle, so the seeded tier is hard.

## 2. Clone and propose

```bash
git clone https://github.com/pydantic/pydantic ~/src/pydantic
export GITHUB_TOKEN=...   # or be logged in with `gh`; unauthenticated calls are limited to 60 per hour

# Specific pull requests: the three causing PRs found by reading fix PRs, plus recent fixes as clean candidates
python -m review_bench import --repo-url https://github.com/pydantic/pydantic --clone ~/src/pydantic \
  --pr 13518 --pr 13573 --pr 13665 --pr 6414 --pr 11883 --pr 11890 \
  --pr 13537 --pr 13690 --pr 13711 --pr 13731 --pr 13859 --pr 13611 --pr 13523 --pr 13521

# Or scan recent merged pull requests, ten at a time, resumably
python -m review_bench import --repo-url https://github.com/pydantic/pydantic --clone ~/src/pydantic \
  --since 2026-06-01 --batch 10
python -m review_bench proposals proposals/pydantic--pydantic
```

The causing pull requests were found by searching merged fix PRs whose body
contains "regression" or "introduced in", then reading each body for the
number it blames. For two fixes with no number in the body, `git log -S` on
the fixed expression found the commit that added it.

Each proposal directory holds `candidate.diff` (workflow files stripped) and
`proposal.json` with the pinned `base_sha` and `merge_sha`, how the base was
resolved (`merge_style`, `base_verified`), source URLs, the pull request title
and body excerpt, a draft acceptance list taken from the body's bullet points,
and the evidence: linked issues, reviews, review comments, discussion, and
every issue or pull request that referenced this one after it merged.

## 3. Rule

A ruling goes into the proposal's `ruling` block. The rulings for this batch
are recorded in `proposals/pydantic--pydantic/rulings-2026-09-30.py` so they
can be read and re-applied; each one cites the evidence it relied on.

| PR | Ruling | Why |
| --- | --- | --- |
| 13518 | seeded | #13537, same day: `secrets.compare_digest()` on `str` raises on non-ASCII |
| 13573 | seeded | #13690, three weeks later: bare `MutableSequence` dispatched to the sequence schema |
| 13665 | seeded | #13711, nine days later: serialization format applied to validation-mode schemas |
| 6414 | clean, should_fix expectation | #13428 (2026) fixed `hash(type(self.mode))`; valid but degenerate, no concrete failure |
| 13711 | clean, should_fix expectation | #13840 reported a numeric default under a string schema; arises from default serialization |
| 13537, 13690, 13523, 13859, 13611, 13731, 11890 | clean | nothing referenced them afterwards, or the follow-up touched files outside the diff |
| 11883 | rejected | the later fix changed `smart_deepcopy()`, which is not in the diff; the defect cannot name a file a reviewer saw |
| 13521 | rejected | only a downstream project's workaround references it; not established as a pydantic regression |

Two rules shaped the packets. The objective and acceptance items were written
from the pull request title, body and linked issue as they stood at merge
time, never from the fix. And a defect is only expected when it sits on a
line in a file the diff touches; a consequence that surfaces elsewhere is
recorded in the rationale but not scored.

These rulings were drafted by an AI assistant from the evidence above and are
marked as pending review in each `provenance.json`. Treat them as a proposal
until the repository owner has confirmed them; the point of separating
proposals from cases is that a ruling is a decision someone signs.

## 4. Promote and check

```bash
for d in proposals/pydantic--pydantic/pr-*; do python -m review_bench --cases cases/pydantic promote "$d"; done
python -m review_bench --cases cases/pydantic check --repo ~/src/pydantic
```

`promote` refuses a ruling without a ruler and rationale, a seeded ruling
without a blocking expectation, an expectation on a file outside the diff, and
packet text that names a later pull request or reads like hindsight. It writes
the evidence to `provenance.json` beside the case; `check` warns if anything
from that file shows up in the reviewer packet.

## 5. Run

```bash
python -m review_bench --cases cases/pydantic run --repo ~/src/pydantic \
  --backend codex:gpt-6-luna:high \
  --backend muse:muse-spark-1.3-contributor:high \
  --backend claude:claude-haiku-4-5-20251001:high \
  --results results/pydantic-2026-09-30/results.jsonl --repeats 3 --parallel 3 --timeout 900
python -m review_bench --cases cases/pydantic misses --results results/pydantic-2026-09-30/results.jsonl
```

The rendered report for that run is in
[pydantic-2026-09-30.md](results/pydantic-2026-09-30.md). Three inexpensive
reviewers were chosen deliberately: the batch is meant to show the harness
working end to end, not to rank frontier models.

## 6. Read the result honestly

- Three blocking expectations over three repeats is nine outcomes per backend.
  The interval column in the report is wide for a reason.
- All three seeded cases merged in July and August 2026 and their fixes are
  public; whether a given model saw them in training is not known.
- The two rejected proposals are not failures of the importer. They are the
  cases where evidence did not pin a defect to a line a reviewer could name,
  and the honest outcome is to leave them out.
