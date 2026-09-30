"""A deterministic example target repository and the synthetic cases built on it.

`python -m review_bench example --target DIR` builds a tiny Python package
through four commits with fixed authorship and timestamps, so every commit
SHA is the same on every machine. The synthetic cases under `cases/synthetic`
pin those SHAs; the test-suite rebuilds the target and checks that the
shipped cases still apply. No network, no API key, no model.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from .make_case import clean_case, seeded_case

ENV = {"GIT_AUTHOR_NAME": "example", "GIT_AUTHOR_EMAIL": "example@example.invalid",
       "GIT_COMMITTER_NAME": "example", "GIT_COMMITTER_EMAIL": "example@example.invalid",
       "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
       "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

MONEY_V1 = '''"""Money helpers. Every amount is a Decimal with two places."""
from decimal import Decimal, ROUND_HALF_UP

PENNY = Decimal("0.01")


def to_pennies(amount):
    return Decimal(amount).quantize(PENNY, rounding=ROUND_HALF_UP)


def line_total(unit_price, quantity):
    if quantity < 0:
        raise ValueError("quantity must not be negative")
    return to_pennies(Decimal(unit_price) * quantity)
'''

MONEY_V2 = MONEY_V1 + '''

def apply_discount(total, percent):
    """Reduce a line total by a whole-number percentage, rounded to the penny."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    factor = (Decimal(100) - Decimal(percent)) / Decimal(100)
    return to_pennies(Decimal(total) * factor)
'''

PAGING_V1 = '''"""Offset pagination over an in-memory sequence."""


def page(items, offset, limit):
    if offset < 0 or limit <= 0:
        raise ValueError("offset must be >= 0 and limit > 0")
    return list(items[offset:offset + limit])
'''

PAGING_V2 = '''"""Offset pagination over an in-memory sequence."""


def page(items, offset, limit):
    """Return (items on this page, whether another page follows)."""
    if offset < 0 or limit <= 0:
        raise ValueError("offset must be >= 0 and limit > 0")
    window = list(items[offset:offset + limit])
    has_next = offset + limit < len(items)
    return window, has_next
'''

TOKENS_V1 = '''"""Single-use access tokens."""
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Token:
    value: str
    issued_at: datetime
    expires_at: datetime
'''

TOKENS_V2 = TOKENS_V1 + '''

def is_valid(token, now):
    """A token is usable strictly before its expiry instant."""
    if token.issued_at > now:
        return False
    return token.expires_at > now
'''

TEST_MONEY_V1 = '''from decimal import Decimal

from ledger.money import line_total


def test_line_total_rounds_to_pennies():
    assert line_total("1.005", 1) == Decimal("1.01")
'''

TEST_MONEY_V2 = TEST_MONEY_V1 + '''

def test_discount_rounds_to_pennies():
    from ledger.money import apply_discount
    assert apply_discount("19.99", 15) == Decimal("16.99")
'''

TEST_PAGING_V1 = '''from ledger.paging import page


def test_page_slices():
    assert page([1, 2, 3, 4, 5], 2, 2) == [3, 4]
'''

TEST_PAGING_V2 = '''from ledger.paging import page


def test_page_slices():
    assert page([1, 2, 3, 4, 5], 2, 2) == ([3, 4], True)


def test_last_page_has_no_next():
    assert page([1, 2, 3, 4], 2, 2) == ([3, 4], False)
'''

TEST_TOKENS_V2 = '''from datetime import datetime, timedelta

from ledger.tokens import Token, is_valid


def test_expired_token_is_rejected():
    now = datetime(2026, 1, 1, 12, 0)
    token = Token("t", now - timedelta(hours=2), now - timedelta(hours=1))
    assert not is_valid(token, now)
'''

STEPS = [
    ("Initial ledger package", {
        "README.md": "# ledger\n\nA tiny example package for the review benchmark.\n",
        "ledger/__init__.py": "", "ledger/money.py": MONEY_V1, "ledger/paging.py": PAGING_V1,
        "ledger/tokens.py": TOKENS_V1, "tests/test_money.py": TEST_MONEY_V1, "tests/test_paging.py": TEST_PAGING_V1,
        ".github/workflows/ci.yml": "name: CI\non: [push]\njobs: {}\n"}),
    ("Apply percentage discounts to line totals", {
        "ledger/money.py": MONEY_V2, "tests/test_money.py": TEST_MONEY_V2,
        ".github/workflows/ci.yml": "name: CI\non: [push, pull_request]\njobs: {}\n"}),
    ("Report whether a page has a next page", {
        "ledger/paging.py": PAGING_V2, "tests/test_paging.py": TEST_PAGING_V2}),
    ("Reject expired tokens", {
        "ledger/tokens.py": TOKENS_V2, "tests/test_tokens.py": TEST_TOKENS_V2}),
]


def sh(cwd, *args):
    env = dict(os.environ, **ENV)
    return subprocess.run(["git", "-C", str(cwd), *args], env=env, capture_output=True, check=True).stdout.decode()


def build_target(destination):
    """Create the example repository; returns the commit SHAs in order."""
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"{destination} is not empty")
    destination.mkdir(parents=True, exist_ok=True)
    sh(destination, "init", "-q", "-b", "main")
    shas = []
    for message, files in STEPS:
        for name, content in files.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        sh(destination, "add", "-A")
        sh(destination, "commit", "-q", "-m", message)
        shas.append(sh(destination, "rev-parse", "HEAD").strip())
    return shas


CASES = [
    {"id": "synthetic-clean-discount", "step": 1, "objective": "Apply a whole-number percentage discount to a line total, rounded to the penny",
     "acceptance": ["apply_discount rejects percentages outside 0 to 100", "The result is quantised to two decimal places",
                    "Money stays Decimal throughout"]},
    {"id": "synthetic-clean-next-page", "step": 2, "objective": "Return whether another page follows the requested page",
     "acceptance": ["page returns the window and a has_next flag", "has_next is False on the final page"]},
    {"id": "synthetic-clean-token-expiry", "step": 3, "objective": "Reject tokens at or after their expiry instant and before issue",
     "acceptance": ["is_valid returns False once now reaches expires_at", "Tokens issued in the future are invalid"]},
]

SEEDS = [
    {"id": "synthetic-seeded-discount-float", "from": "synthetic-clean-discount",
     "edits": [{"file": "ledger/money.py", "old": "return to_pennies(Decimal(total) * factor)",
                "new": "return round(float(total) * float(factor), 2)"}],
     "expected": [{"id": "float-money", "file": "ledger/money.py", "severity": "blocking",
                   "keywords": ["float", "Decimal", "rounding", "precision", "to_pennies", "binary"],
                   "description": "The discount is computed in binary float and returned as float, so totals lose penny precision and no longer round half up."}],
     "notes": "Money maths silently switched from Decimal to float."},
    {"id": "synthetic-seeded-next-page-off-by-one", "from": "synthetic-clean-next-page",
     "edits": [{"file": "ledger/paging.py", "old": "has_next = offset + limit < len(items)",
                "new": "has_next = offset + limit <= len(items)"}],
     "expected": [{"id": "has-next-off-by-one", "file": "ledger/paging.py", "severity": "blocking",
                   "keywords": ["has_next", "<=", "off-by-one", "last page", "empty page", "one more page", "exactly"],
                   "description": "When the window ends exactly at the end of the sequence has_next is True, so callers request an empty page."}],
     "notes": "Boundary comparison flipped from < to <=."},
    {"id": "synthetic-seeded-expiry-inverted", "from": "synthetic-clean-token-expiry",
     "edits": [{"file": "ledger/tokens.py", "old": "return token.expires_at > now", "new": "return token.expires_at < now"}],
     "expected": [{"id": "expiry-inverted", "file": "ledger/tokens.py", "severity": "blocking",
                   "keywords": ["expires_at", "inverted", "reversed", "expired", "accepts", "rejects valid", "<"],
                   "description": "The expiry comparison is reversed: expired tokens are accepted and live tokens rejected."}],
     "notes": "Comparison direction inverted."},
]


def build_cases(target, cases_root, repo_url="https://example.invalid/ledger"):
    """Build the synthetic cases against an example target; returns the case ids."""
    shas = [sh(target, "rev-list", "--reverse", "HEAD").split()[i] for i in range(len(STEPS))]
    made = []
    for spec in CASES:
        case = clean_case(target, shas[spec["step"]], case_id=spec["id"], cases_root=cases_root, repo_url=repo_url,
                          objective=spec["objective"], acceptance=spec["acceptance"],
                          source={"example_step": spec["step"]},
                          notes="Synthetic example change, clean by construction.")
        made.append(case["id"])
    for spec in SEEDS:
        case = seeded_case(target, Path(cases_root) / spec["from"], case_id=spec["id"], cases_root=cases_root,
                           expected=spec["expected"], edits=spec["edits"], notes=spec["notes"], author="synthetic")
        made.append(case["id"])
    return made
