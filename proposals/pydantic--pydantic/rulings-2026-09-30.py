"""Rulings for the first audited pydantic batch, applied to proposal.json files.

Kept beside the proposals so the reasoning is reviewable and re-runnable.
Run from the repository root: python proposals/pydantic--pydantic/rulings-2026-09-30.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RULED_BY = "assistant draft (Claude), pending review by JA-Marshall"
RULED_AT = "2026-09-30"

RULINGS = {
    13518: {
        "status": "seeded", "case_id": "pydantic-pr13518-secret-constant-time-compare",
        "rationale": "PR #13537, merged the same day, encodes both values to bytes before secrets.compare_digest() because "
                     "the str form raises TypeError on non-ASCII input; its author says the regression was found while reading "
                     "this commit. The defect is on lines this diff adds in pydantic/types.py.",
        "objective": "Compare SecretStr and SecretBytes values in constant time and make both types hashable",
        "acceptance": ["SecretStr and SecretBytes instances compare equal exactly when their secret values are equal",
                       "Equality uses a constant-time comparison",
                       "Hashing is consistent with equality",
                       "Comparing with an instance of another type returns False"],
        "expected": [{"id": "compare-digest-non-ascii", "file": "pydantic/types.py", "severity": "blocking",
                      "keywords": ["compare_digest", "non-ASCII", "ASCII", "TypeError", "encode", "unicode", "bytes"],
                      "description": "secrets.compare_digest() only accepts ASCII when given str arguments, so comparing two "
                                     "SecretStr values that contain non-ASCII characters raises TypeError instead of returning a bool."}],
    },
    13573: {
        "status": "seeded", "case_id": "pydantic-pr13573-type-lookup-dispatch-table",
        "rationale": "PR #13690, merged three weeks later, calls this an unreleased regression: bare collections.abc.MutableSequence "
                     "was in LIST_TYPES and produced a list schema, and the new dispatch table maps it to _sequence_schema(Any), "
                     "an is-instance check with no coercion and no mutability check. A review bot flagged the MutableSequence entry "
                     "during review. The wrong mapping is on a line this diff adds.",
        "objective": "Replace the sequential type-list checks in core schema generation with dictionary dispatch on the bare type or "
                     "origin, as an optimisation with no change in the schemas produced",
        "acceptance": ["Every type the removed TYPES lists matched produces the same core schema as before",
                       "Bare and subscripted forms of each collection type dispatch to the handler they used before",
                       "Types not in the tables still reach the existing fallback paths"],
        "expected": [{"id": "mutable-sequence-dispatch", "file": "pydantic/_internal/_generate_schema.py", "severity": "blocking",
                      "keywords": ["MutableSequence", "_sequence_schema", "_list_schema", "LIST_TYPES", "is_instance", "coercion",
                                   "bare", "mutability"],
                      "description": "Bare collections.abc.MutableSequence used to be in LIST_TYPES and validate as a list; the new "
                                     "table maps it to _sequence_schema(Any), so a str passes validation unchanged and nothing is "
                                     "coerced to a list."}],
    },
    13665: {
        "status": "seeded", "case_id": "pydantic-pr13665-temporal-json-schema-format",
        "rationale": "The linked issue #13664 asked for serialization schemas to match model_dump_json() output. PR #13711, merged nine "
                     "days later as an unreleased regression fix, limits the change to serialization mode: _common_temporal_schema() "
                     "as added here consults the serialization format in both modes, so a validation-mode schema declares a datetime "
                     "as a number while validation still accepts ISO 8601 strings.",
        "objective": "Make the JSON Schema for date, time, datetime and timedelta fields reflect the ser_json_temporal setting, so "
                     "serialization schemas match model_dump_json() output",
        "acceptance": ["With ser_json_temporal set to seconds or milliseconds, temporal fields are described as numbers in the "
                       "serialization JSON schema",
                       "With iso8601 or no setting, the string schemas and their formats are unchanged",
                       "ser_json_temporal takes precedence over ser_json_timedelta for timedelta fields; existing ser_json_timedelta "
                       "behaviour is otherwise unchanged"],
        "expected": [{"id": "validation-schema-uses-serialization-format", "file": "pydantic/json_schema.py", "severity": "blocking",
                      "keywords": ["validation schema", "validation mode", "mode=", "both modes", "regardless of mode",
                                   "ISO 8601", "accepts strings", "still accepts", "'number'"],
                      "description": "_common_temporal_schema applies the serialization format regardless of the JSON schema mode, so "
                                     "a validation-mode schema says a datetime field is a number while the validator accepts and "
                                     "expects ISO 8601 strings."}],
    },
    13537: {
        "status": "clean", "case_id": "pydantic-pr13537-secret-non-ascii-equality",
        "rationale": "Small fix with tests for non-ASCII and surrogate values, reviewed and approved; the only later cross-references "
                     "are unrelated repositories linking the PR. No pydantic issue or fix references it.",
        "objective": "Fix SecretStr equality raising TypeError on non-ASCII values while keeping the constant-time comparison",
        "acceptance": ["SecretStr values containing non-ASCII characters compare equal when their secrets are equal",
                       "The comparison remains constant-time",
                       "SecretBytes behaviour is unchanged"],
        "expected": [],
    },
    13690: {
        "status": "clean", "case_id": "pydantic-pr13690-mutable-sequence-list-schema",
        "rationale": "Two-line fix restoring the previous list schema for bare MutableSequence, with a regression test; approved; "
                     "nothing referenced it afterwards.",
        "objective": "Validate bare collections.abc.MutableSequence with the list schema, as before the type-lookup refactor",
        "acceptance": ["TypeAdapter(MutableSequence) rejects non-sequence input such as a str and coerces valid input to a list",
                       "Subscripted MutableSequence[T] behaviour is unchanged"],
        "expected": [],
    },
    13523: {
        "status": "clean", "case_id": "pydantic-pr13523-linear-schema-gathering",
        "rationale": "Performance fix that visits each schema object once; the PR body explains how per-encounter reference counting "
                     "is preserved and the diff carries a regression test. Nothing referenced it afterwards.",
        "objective": "Traverse each core schema object once in gather_schemas_for_cleaning(), so schema cleaning is linear in the "
                     "number of schemas rather than exponential in the number of paths",
        "acceptance": ["Each schema object is visited once even when several paths reach it",
                       "Definition reference counting keeps its per-encounter semantics",
                       "Creating chains of interconnected models completes in linear time"],
        "expected": [],
    },
    13859: {
        "status": "clean", "case_id": "pydantic-pr13859-pop-schema-stacks-in-finally",
        "rationale": "Wraps two context-manager yields in try/finally with tests for the failed-build path described in issue #13857. "
                     "Nothing referenced it afterwards.",
        "objective": "Always pop the field name and model type stacks in schema generation, even when schema generation raises inside "
                     "the with block",
        "acceptance": ["A schema build that raises and is caught leaves no stale entry on either stack",
                       "Subsequent Self annotations and handler.field_name resolve correctly after a failed build"],
        "expected": [],
    },
    13611: {
        "status": "clean", "case_id": "pydantic-pr13611-pipeline-keeps-compiled-pattern",
        "rationale": "Passes the compiled pattern through instead of its source string; tests cover re.ASCII and re.IGNORECASE; "
                     "changes requested and then approved. Nothing referenced it afterwards.",
        "objective": "Keep the compiled re.Pattern, with its flags, when a pattern constraint is applied in the experimental pipeline",
        "acceptance": ["A constraint built from a compiled pattern honours its flags such as re.ASCII and re.IGNORECASE",
                       "Constraints built from pattern strings behave as before"],
        "expected": [],
    },
    13731: {
        "status": "clean", "case_id": "pydantic-pr13731-smart-union-field-count",
        "rationale": "Rust change in the vendored pydantic-core validator with Python tests for both input modes and union orders, "
                     "approved by a maintainer. Nothing referenced it afterwards. Included so the batch has one non-Python diff.",
        "objective": "Count validated model fields once in smart union member selection, for both Python and JSON input",
        "acceptance": ["A model member with a before validator does not outrank a larger member because its fields were counted twice",
                       "Nested field counts are preserved; extra keys and defaults are not counted as local fields"],
        "expected": [],
    },
    11890: {
        "status": "clean", "case_id": "pydantic-pr11890-keep-mock-validators-on-rebuild",
        "rationale": "The change is correct as written. The later PR #12513 applied the same treatment to rebuild_dataclass(), which "
                     "lives in a file this diff does not touch, so the omission cannot be an expected finding on this diff; the "
                     "thread-safety follow-ups (#11851, #13438) concern the wider rebuild logic.",
        "objective": "Do not delete mock validators and serializers in model_rebuild(), so a concurrent instantiation during a rebuild "
                     "cannot fall back to a parent class validator",
        "acceptance": ["Real validators, serializers and core schemas are still deleted before rebuilding",
                       "Mock instances are left in place"],
        "expected": [],
    },
    6414: {
        "status": "clean", "case_id": "pydantic-pr6414-hashable-json-schema-metadata",
        "rationale": "PR #13428 (2026) corrected __hash__ to hash the mode value: as written it hashes type(self.mode), so every "
                     "instance shares one of two hash values. Equal objects still hash equal, so the hash contract holds and no "
                     "concrete failure follows; that makes it should_fix, not blocking, and the case stays clean. Merged in 2023, "
                     "so it is in every model's training data.",
        "objective": "Make WithJsonSchema and Examples hashable so they can be used as Annotated metadata inside Union and Optional",
        "acceptance": ["Annotated[int, WithJsonSchema(...)] inside a Union generates a JSON schema without a hashability error",
                       "Equal instances hash equal"],
        "expected": [{"id": "hash-of-mode-type", "file": "pydantic/json_schema.py", "severity": "should_fix",
                      "keywords": ["hash(type", "type(self.mode)", "self.mode", "constant", "same hash", "collide", "type rather than",
                                   "hash the value"],
                      "description": "__hash__ returns hash(type(self.mode)), the hash of str or NoneType, so all instances with a "
                                     "string mode share one hash value; valid but degenerate."}],
    },
    13711: {
        "status": "clean", "case_id": "pydantic-pr13711-temporal-format-serialization-only",
        "rationale": "Restricts the temporal format to serialization mode with tests. Issue #13840, three weeks later, reported that a "
                     "temporal field with a default now gets a numeric serialized default under a validation schema that says string; "
                     "that comes from how defaults are serialized rather than from these lines, so it is recorded as should_fix and "
                     "the case stays clean.",
        "objective": "Apply ser_json_temporal and ser_json_timedelta formats only to serialization-mode JSON schemas; validation "
                     "schemas keep the ISO 8601 string form",
        "acceptance": ["Validation-mode schemas for date, time, datetime and timedelta are string schemas regardless of the temporal "
                       "configuration",
                       "Serialization-mode schemas still reflect the configured format"],
        "expected": [{"id": "numeric-default-under-string-type", "file": "pydantic/json_schema.py", "severity": "should_fix",
                      "keywords": ["default", "numeric default", "contradict", "serialized default", "default_schema", "'number'",
                                   "under type"],
                      "description": "A temporal field with a default gets a serialized numeric default while the validation schema "
                                     "now declares the field a string."}],
    },
    11883: {
        "status": "rejected",
        "rationale": "PR #12522 later special-cased MISSING in smart_deepcopy() because deep-copying the sentinel raised TypeError "
                     "during model_construct(). The sentinel class lives in pydantic-core and smart_deepcopy() is not in this diff, so "
                     "the defect cannot be attributed to a file a reviewer of this change would name. The diff is also 540 lines of "
                     "docs and lockfile churn around the feature.",
    },
    13521: {
        "status": "rejected",
        "rationale": "The only later references are a downstream project (psygnal) hitting a behaviour change in 2.14.0a1 with "
                     "annotation-free subclasses and working around it on their side. Whether pydantic treats that as a regression is "
                     "not established, so on this evidence the case is neither clean nor seeded.",
    },
}


def main():
    for number, ruling in RULINGS.items():
        path = ROOT / f"pr-{number}" / "proposal.json"
        proposal = json.loads(path.read_text())
        proposal["ruling"] = dict(ruling, ruled_by=RULED_BY, ruled_at=RULED_AT)
        path.write_text(json.dumps(proposal, indent=1, sort_keys=True) + "\n")
        print(f"ruled pr-{number}: {ruling['status']}")


if __name__ == "__main__":
    main()
