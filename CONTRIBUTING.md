# Contributing

Open an issue before changing the case contract, the scoring rules or the pass
criteria; those define what every published result means.

## Cases

- A case must be reproducible: pin a full commit SHA that exists on the source
  repository's default branch, and record the source URLs.
- A proposal is evidence, not proof. A merged pull request, a review comment or
  a later fix may point at a defect; only a person rules on whether the case is
  clean or seeded, which defect it carries, and how severe it is. Record who
  ruled and why in the proposal before promoting it.
- Write the objective and acceptance items from what the pull request said at
  merge time. Nothing learned from a later fix may appear in them; `promote`
  and `check` refuse text that names a later pull request or reads like
  hindsight.
- Keywords must be words a reviewer would actually write: symbol names,
  literals, operators and plain-English effects. Abstract category names never
  match anything.
- When a reviewer raises a blocking finding on a clean case, adjudicate it once
  and record the outcome in the case's `adjudications` list. If the finding is
  real, the case is not clean: re-rule it as seeded.

## Harness

- Add a focused test for every harness change. The suite uses a fixture
  repository and the `null` and `oracle` backends, so it needs no model and no
  network; CLI backends are tested against fake executables.
- Do not include private repository code, credentials, personal paths, customer
  data or raw model transcripts in a contribution. Results files under
  `results/` are ignored by Git for that reason; publish a rendered report
  instead.
