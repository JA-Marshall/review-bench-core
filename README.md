# review-bench

A benchmark harness for automated code reviewers. It answers two questions
about a reviewer before it is trusted as a gate inside an automated fix loop:

- Does it catch the defects that matter, and call them blocking?
- Does it stay quiet on good code?

It also reports what any pair of reviewers would do together, and whether a
reviewer only scores well on defects written by its own model family.

The harness is a small, dependency-free Python package. Cases are diffs
against pinned commits of a target repository; reviewer backends run agentic
CLIs or an OpenAI-compatible API in a fresh context per call; scoring is
deterministic, so everything is testable without a model.

## Quick start (no model, no network)

```bash
git clone https://github.com/JA-Marshall/review-bench-core
cd review-bench-core
python -m unittest discover -s tests

# Build the deterministic example target and run the null and oracle reviewers on the synthetic cases
python -m review_bench example --target /tmp/example-target
python -m review_bench --cases cases/synthetic check --repo /tmp/example-target
python -m review_bench --cases cases/synthetic run --repo /tmp/example-target \
  --backend oracle --backend null --results results/example/r.jsonl --repeats 2
```

`oracle` reports exactly the expected findings and `null` reports nothing;
between them they exercise the whole pipeline and give the ceiling and floor
every real reviewer must land between.

## Running real reviewers

```bash
# A local clone of the target repository is required; cases pin commits in it.
git clone https://github.com/pydantic/pydantic ~/src/pydantic
python -m review_bench --cases cases/pydantic check --repo ~/src/pydantic

python -m review_bench --cases cases/pydantic run --repo ~/src/pydantic \
  --backend codex:gpt-5.6-sol:high --backend claude:claude-opus-5-5:high \
  --backend cursor:grok-4.7-high --backend muse:muse-spark-1.3-contributor:high \
  --results results/pydantic/results.jsonl --repeats 3 --parallel 4

python -m review_bench --cases cases/pydantic report --results results/pydantic/results.jsonl
python -m review_bench --cases cases/pydantic misses --results results/pydantic/results.jsonl
```

Backends, given as `name[:model[:effort]]`:

| Backend | Runs | Sees |
| --- | --- | --- |
| `codex` | Codex CLI, read-only sandbox | prompt plus a snapshot of the repository with the candidate applied |
| `claude` | Claude Code CLI, restricted to Read, Grep and Glob | same |
| `cursor` | Cursor agent CLI in ask mode | same |
| `muse` | Muse Code CLI, headless with writes, shell and web disabled | same |
| `openai` | any OpenAI-compatible chat endpoint (`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`) | prompt only |
| `null`, `oracle` | no model | used by the tests |

Every call runs in a fresh context. Tool-using backends get a throwaway
snapshot under `work/`; results append to a JSON Lines file, one row per call,
and a run can be resumed: calls that already produced a valid result are
skipped, failed calls are retried. Pass criteria live in `criteria.json`.

## Cases

`cases/<corpus>/<id>/case.json` plus `candidate.diff`. A case is a diff against
one pinned commit (`base_sha`) of the repository named in `repo`. Two kinds:

- `clean`: a merged change believed free of blocking defects. Any blocking
  finding a reviewer raises on it is adjudicated once by hand and recorded in
  the case's `adjudications` list; if it is real, the case is re-ruled seeded.
- `seeded`: a change that carries at least one blocking defect. Either a
  historical one (a merged pull request whose bug a later fix established,
  `author: history`) or an injected one (a clean case plus a `mutation.diff`,
  with the author model or person recorded).

Expected findings carry `file`, `severity`, `keywords` and a description. A
reviewer finding matches when it names the file and contains at least
`min_keyword_hits` keywords; blocking recall additionally requires the finding
to be reported as blocking. See [docs/METHODOLOGY.md](docs/METHODOLOGY.md) for
the exact metrics and their known weaknesses.

Two corpora ship with the repository:

- `cases/synthetic`: six cases on the deterministic example target, clean by
  construction plus three hand-written mutations. They exist so the pipeline
  can be exercised anywhere.
- `cases/pydantic`: a small audited batch drawn from merged pull requests to
  [pydantic](https://github.com/pydantic/pydantic), with rulings and
  provenance. [docs/IMPORTING.md](docs/IMPORTING.md) reproduces it from
  scratch and reports what several reviewers scored on it.

## Building cases from a public repository

```bash
# Propose a batch from recent merged pull requests (resumable; GITHUB_TOKEN or `gh auth` recommended)
python -m review_bench import --repo-url https://github.com/pydantic/pydantic --clone ~/src/pydantic \
  --since 2026-06-01 --batch 10
# Or propose specific pull requests
python -m review_bench import --repo-url https://github.com/pydantic/pydantic --clone ~/src/pydantic --pr 13518 --pr 13537

python -m review_bench proposals proposals/pydantic--pydantic
# Read proposals/pydantic--pydantic/pr-13518/proposal.json, fill in its "ruling" block, then:
python -m review_bench --cases cases/pydantic promote proposals/pydantic--pydantic/pr-13518
```

A proposal pins the base and merge commits, records the source URLs, and
collects evidence: linked issues, review comments, the discussion, and every
issue or pull request that referenced the change after it merged. A proposal
is never scored. `promote` requires a ruling with a named ruler, a rationale,
the objective and acceptance items written from the pull request as it stood
at merge time, and, for a seeded case, the specific defect with its severity.
It refuses packet text that leaks a later fix, and it writes the evidence to
`provenance.json`, which no reviewer prompt ever includes.

Hand-built cases remain available:

```bash
python -m review_bench make-clean --repo ~/target --merge <sha> --id clean-<name> --repo-url <url> \
  --objective "..." --acceptance "..." [--exclude docs/]
python -m review_bench make-seeded --repo ~/target --from cases/<corpus>/clean-<name> --id seeded-<name> \
  --edits edits.json --expected expected.json --author <you>
# Ask another model for subtle defects; review the proposal file; accept the ones that are real
python -m review_bench propose-seeds --repo ~/target --backend codex --from clean-<name> --count 3
python -m review_bench accept-seeds proposals/seeds/<author>--clean-<name>.json --repo ~/target
```

## Reports

`run` and `report` write a Markdown summary beside the results file: per
backend, blocking recall with a 95% Wilson interval, lenient recall (any
severity), false blocking findings per clean and per seeded case, mean nits,
compliance and latency; then every backend pair's union and intersection;
then blocking recall per seed author; then one row per call. `misses` prints
every expectation a call failed to report as blocking, with the findings it
returned instead, which is where hand audits of the keyword matching start.

## Tests and CI

```bash
python -m unittest discover -s tests -v
```

The suite uses a fixture repository, a fake GitHub API and the `null` and
`oracle` backends, so it needs no API keys. CI also rebuilds the example
target, runs the pipeline on it, and checks that every shipped case still
applies and leaks nothing from its provenance into the reviewer packet.

## Licence

MIT. Diffs under `cases/pydantic` are excerpts of pydantic, which is also
MIT-licensed; see `cases/pydantic/THIRD_PARTY_NOTICE.md`.
