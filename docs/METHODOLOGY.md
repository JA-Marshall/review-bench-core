# Methodology

This document defines what the numbers mean, what the harness deliberately does
not do, and where the measurement is known to be weak. Read it before citing a
result.

## The question being answered

An automated fix loop needs a reviewer that can be trusted as a gate: when it
says "blocking", a correction round starts; when it says nothing, the change
proceeds to CI and a human. Two failure modes matter and they trade off:

- **Missed defects**: the reviewer lets a broken change through. Measured as
  blocking recall on seeded cases.
- **False alarms**: the reviewer blocks a good change. Measured as spurious
  blocking findings per clean case.

Everything else (nits, should-fix findings, latency) is reported but does not
decide the pass verdict.

## Cases and ground truth

A case is a unified diff against a pinned base commit of a public repository,
with an objective and acceptance items that describe the intended change, and
a list of expected findings.

- **Clean** cases are merged changes believed free of blocking defects. Merge
  is evidence, not proof. Any blocking finding a reviewer raises on a clean
  case is adjudicated once by hand and recorded in `adjudications`. A real
  finding turns the case into a seeded one.
- **Seeded** cases carry at least one blocking defect. Historical seeds are
  merged changes whose defect a later fix established (`author: history`).
  Injected seeds are a clean case plus a small mutation authored by a person
  or by a model, and the author is recorded so recall can be split by author.

Every expected finding names a file, a severity, a description and a list of
keywords. The description is for people; the keywords are what the scorer
uses.

## Matching and metrics

A reviewer finding **hits** an expectation when its `file` ends with the
expected file path and the text of its summary and failure scenario contains
at least `min_keyword_hits` of the expected keywords, case-insensitively.

Per backend:

- **Blocking recall**: over every blocking expectation with at least one valid
  call, the mean fraction of repeats in which a finding hit it *and* carried
  severity `blocking`. A reviewer that sees the defect but files it as a nit
  gets no credit here, because it would not stop the loop.
- **Lenient recall**: the same with any severity. The gap between the two is
  how often the reviewer sees the defect but misjudges it.
- **95% CI**: a Wilson interval over the pooled (hit, call) outcomes for
  blocking expectations. Repeats of one case are not independent, so the
  interval is optimistic; it exists to stop people reading a difference of
  0.05 between two reviewers as a result.
- **False blocking per clean case**: spurious blocking findings (those hitting
  no expectation) on valid clean calls, divided by the number of those calls.
- **False blocking per seeded case**: the same on seeded calls. Without it a
  reviewer that emits five blocking findings per case would score perfect
  recall and pay only on the clean side.
- **Compliance**: valid calls over all calls. A call is valid when the result
  is a JSON object with the right keys, echoes the candidate hash, lists
  exactly the diff's files, and every finding is well-formed. The default
  criterion is 1.0: a reviewer that sometimes returns garbage cannot gate a
  loop.

The pass verdict applies the thresholds in `criteria.json` to blocking recall,
both false-blocking rates (the seeded one only when the criterion is present)
and compliance.

**Pairs**: for every two backends, over the calls both completed on the same
case and repeat, union recall (either reported it as blocking) and
intersection recall (both did), plus the union and intersection of their false
blocking findings on clean cases. This is the "should I add a second reviewer"
table.

**Recall by seed author**: blocking recall split by who wrote the seeded
defect. A reviewer that only scores well on seeds from its own model family is
showing author bias, and this table is where it shows.

Scores are recomputed from the stored findings every time a report is
rendered, so expectations can be corrected after a run without rerunning it.

## What is deliberately not done

- No LLM judge. Matching is keyword-based so that a result can be reproduced
  from the results file with no model. The cost is stated below.
- No partial credit for a finding on the wrong file, however well it describes
  the defect.
- No cost accounting in the verdict. Token usage is stored per call where the
  backend reports it; latency is reported.
- Workflow files (`.github/`, agent instruction files, and anything passed
  with `--exclude`) are stripped from diffs so reviewers see product code.

## Known weaknesses

These are real and they bound what a result means.

1. **Keyword matching over-counts.** A long finding on the right file can
   contain two keywords by accident while describing something else. The
   `misses` command lists what was returned for every miss; auditing a sample
   of *hits* by hand is the only check on false hits, and it should be done
   before a headline number is quoted. Raising `min_keyword_hits` trades
   accidental hits for missed terse findings.
2. **Keyword matching under-counts.** A reviewer can describe the defect in
   words the keyword list did not anticipate. Keywords should be symbol names,
   literals, operators and plain effects, never category names, and every
   miss should be read once to see whether the expectation, not the reviewer,
   was wrong.
3. **Small corpora.** A batch of ten cases from one repository says something
   about those ten cases. The interval column is there so that nobody reads
   0.83 versus 0.78 as a ranking.
4. **Clean is a belief.** A clean case was merged by people who did not find a
   blocking defect. Reviewers sometimes do. Until a blocking finding on a clean
   case has been adjudicated, it counts as a false alarm, which penalises the
   reviewer that was right.
5. **Hindsight leakage.** For historical seeds the objective and acceptance
   items must be writable from the pull request at merge time. The `promote`
   and `check` commands reject or warn on text that names a later pull request
   or reads like hindsight, but a subtly steering acceptance item can still get
   through a lint. Reviewers of rulings should read the packet as the original
   author would have.
6. **Training-data contamination.** Public pull requests and their fixes are
   in model training data. Recording `merged_at` on every historical case
   allows results to be split by whether the merge predates a model's
   training cutoff, but the harness does not know cutoffs and does not do the
   split for you. Prefer recent pull requests and say when they merged.
7. **Injected seeds are synthetic.** Mutations authored for the benchmark skew
   toward defects visible in the diff alone. Historical seeds are the more
   honest tier and should be preferred when there are enough of them.
8. **Repeats are not independent samples.** A reviewer that is deterministic
   at temperature zero contributes the same outcome three times. Repeats
   measure stability, not more evidence.

## Reporting a result honestly

State the corpus and its size, the criteria file, the number of repeats, the
backends with model and effort, the merge dates of the cases relative to the
models' cutoffs where known, and whether hits were audited by hand. Publish the
rendered report and the case rulings, not just the summary table.
