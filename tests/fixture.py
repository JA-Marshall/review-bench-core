"""A tiny target repository with one base commit and one merged change."""
import json
from pathlib import Path
import subprocess


def sh(cwd, *args, input=None):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.email=f@example.invalid", "-c", "user.name=f", *args],
                          input=input, capture_output=True, check=True).stdout.decode()


BASE_MONEY = '''from decimal import Decimal


def line_total(price, quantity):
    return Decimal(price) * quantity
'''
CHANGED_MONEY = '''from decimal import Decimal


def line_total(price, quantity):
    if quantity < 0:
        raise ValueError("quantity must not be negative")
    return Decimal(price) * quantity
'''
MUTATION = '''diff --git a/shop/money.py b/shop/money.py
--- a/shop/money.py
+++ b/shop/money.py
@@ -4,4 +4,4 @@
 def line_total(price, quantity):
     if quantity < 0:
         raise ValueError("quantity must not be negative")
-    return Decimal(price) * quantity
+    return float(price) * quantity
'''
EXPECTED = [{"id": "float-money", "file": "shop/money.py", "severity": "blocking",
             "keywords": ["float", "Decimal", "precision"], "description": "Money maths switched from Decimal to float"}]


def make_repo(home):
    repo = Path(home) / "target"
    repo.mkdir()
    sh(repo, "init", "-q", "-b", "main")
    (repo / "shop").mkdir()
    (repo / "shop" / "money.py").write_text(BASE_MONEY)
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / "task.md").write_text("plan\n")
    (repo / "AGENTS.md").write_text("rules\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "base")
    base = sh(repo, "rev-parse", "HEAD").strip()
    (repo / "shop" / "money.py").write_text(CHANGED_MONEY)
    (repo / "docs" / "plans" / "task.md").write_text("plan updated\n")
    (repo / "AGENTS.md").write_text("rules updated\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "guard negative quantity")
    head = sh(repo, "rev-parse", "HEAD").strip()
    return repo, base, head


def write_expected(home):
    path = Path(home) / "expected.json"
    path.write_text(json.dumps(EXPECTED))
    return path


def write_mutation(home):
    path = Path(home) / "mutation.diff"
    path.write_text(MUTATION)
    return path
