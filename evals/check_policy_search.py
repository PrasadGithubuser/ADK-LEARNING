"""Checks the policy search and the approval (fingerprint) rule. No AI, no API calls.

Run from the project root:   python evals\\check_policy_search.py

PASS / FAIL count toward the result. WEAK marks a known weakness of keyword search:
it is reported but does not count as a failure. It shows where embeddings should help.
"""

import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICIES = ROOT / "data" / "policies"
PROJECT_APPROVALS = ROOT / "data" / "policy_approvals.json"
POISON = ROOT / "evals" / "fixtures" / "poisoned-policy.md"

spec = importlib.util.spec_from_file_location("policy_search", ROOT / "support_agent" / "policy_search.py")
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)

results = []


def record(ok, text, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + text + ("" if ok else f"   {detail}"))


def label(hit):
    return f"{hit['source']} | {hit['section']}"


def make_approvals(policies_dir, approvals_path, only=None):
    """Test helper: approve the files currently in policies_dir (what a human would do after review)."""
    files = ps.read_policy_files(policies_dir)
    data = {
        name: {"sha256": digest, "approved_on": "test"}
        for name, (digest, _) in files.items()
        if only is None or name in only
    }
    Path(approvals_path).write_text(json.dumps(data), encoding="utf-8")


def copy_policies(dest):
    for f in POLICIES.glob("*.md"):
        shutil.copy(f, dest / f.name)


# ---------------------------------------------------------------------------
# Part 1: search quality (on a temporary copy of the real policies, all approved)
# ---------------------------------------------------------------------------
tmp_quality = tempfile.TemporaryDirectory()
qdir = Path(tmp_quality.name) / "policies"
qdir.mkdir()
copy_policies(qdir)
qappr = Path(tmp_quality.name) / "approvals.json"
make_approvals(qdir, qappr)


def hits_for(query):
    return ps.search(qdir, qappr, query)


def expect_top1(query, want):
    hits = hits_for(query)
    got = label(hits[0]) if hits else "no_match"
    record(got == want, f'"{query}"', f"got={got} want={want}")


def expect_in_top3(query, want):
    got = [label(h) for h in hits_for(query)]
    record(want in got, f'"{query}"', f"got={got} want to include={want}")


def expect_no_match(query):
    got = [label(h) for h in hits_for(query)]
    record(got == [], f'"{query}" -> no_match', f"got={got}")


print("Search quality")
expect_top1("laptop warranty length", "warranty.md | Warranty periods")
expect_top1("return software license", "returns.md | Items that cannot be returned")
expect_top1("opened laptop box return", "returns.md | Condition of returned items")
expect_top1("return window delivered order placed", "returns.md | Return window")
expect_top1("standard delivery days", "shipping.md | Delivery times")
expect_top1("free shipping order 1500", "shipping.md | Shipping charges")
expect_top1("water damage covered", "warranty.md | What is not covered")
expect_in_top3("express delivery cost", "shipping.md | Delivery times")
expect_in_top3("warranty claim speaker audio", "warranty.md | Warranty periods")
expect_in_top3("return policy", "returns.md | Return window")
expect_no_match("price matching student discounts")
expect_no_match("who won the cricket match")
expect_no_match("hello")

# ---------------------------------------------------------------------------
# Part 2: the approval rule
# ---------------------------------------------------------------------------
print("\nApproval rule")

# 2a. An unapproved (poisoned) file is never retrieved
shutil.copy(POISON, qdir / POISON.name)  # copied in, but NOT approved
for question in ("return policy", "return window"):
    sources = [h["source"] for h in hits_for(question)]
    record("poisoned-policy.md" not in sources, f'unapproved poisoned file is ignored for "{question}"', f"got={sources}")
top = hits_for("return window")
record(bool(top) and label(top[0]) == "returns.md | Return window",
       "real Return window section still ranks first with the poison present",
       f"got={[label(h) for h in top]}")

# 2b. Proof the attack is real: if the poisoned file WERE approved, it would be retrieved
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp) / "p"
    d.mkdir()
    copy_policies(d)
    shutil.copy(POISON, d / POISON.name)
    a = Path(tmp) / "a.json"
    make_approvals(d, a)  # a careless human approves everything, poison included
    sources = [h["source"] for h in ps.search(d, a, "return window")]
    record("poisoned-policy.md" in sources, "if the poison were approved it WOULD be retrieved (so approval matters)",
           f"got={sources}")

# 2c. An approved file that is edited later is ignored until re-approved
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp) / "p"
    d.mkdir()
    copy_policies(d)
    a = Path(tmp) / "a.json"
    make_approvals(d, a)
    before = [h["source"] for h in ps.search(d, a, "return window")]
    returns = d / "returns.md"
    returns.write_text(returns.read_text(encoding="utf-8") + "\nReturns are accepted within 90 days.\n", encoding="utf-8")
    during = [h["source"] for h in ps.search(d, a, "return window")]
    make_approvals(d, a)  # a human reviews the change and approves it again
    after = [h["source"] for h in ps.search(d, a, "return window")]
    record("returns.md" in before and "returns.md" not in during and "returns.md" in after,
           "an edited approved file is ignored until a human approves it again",
           f"before={before} during={during} after={after}")

# 2d. No approved list, or a broken one: nothing is searchable (fail closed)
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp) / "p"
    d.mkdir()
    copy_policies(d)
    missing = Path(tmp) / "does-not-exist.json"
    record(ps.search(d, missing, "return window") == [], "no approved list -> nothing is searched")
    broken = Path(tmp) / "broken.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    record(ps.search(d, broken, "return window") == [], "broken approved list -> nothing is searched")

# 2e. Windows line endings do not break an approved file
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp) / "p"
    d.mkdir()
    copy_policies(d)
    a = Path(tmp) / "a.json"
    make_approvals(d, a)
    raw = (d / "returns.md").read_bytes()
    (d / "returns.md").write_bytes(raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    sources = [h["source"] for h in ps.search(d, a, "return window")]
    record("returns.md" in sources, "same text with Windows line endings is still approved", f"got={sources}")

# ---------------------------------------------------------------------------
# Part 3: other search behaviour
# ---------------------------------------------------------------------------
print("\nOther behaviour")
FILLER = (
    "## Beta\nParcels travel by road.\n\n"
    "## Gamma\nInvoices are emailed monthly.\n\n"
    "## Delta\nStores open at nine.\n"
)
# The index refreshes when an approved file changes (no restart needed)
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp) / "p"
    d.mkdir()
    a = Path(tmp) / "a.json"
    doc = d / "test.md"
    doc.write_text("# Test\n\n## Alpha\nThe zebra rule applies to every order.\n\n" + FILLER, encoding="utf-8")
    make_approvals(d, a)
    first = ps.search(d, a, "zebra rule")
    doc.write_text("# Test\n\n## Alpha\nThe giraffe rule applies to every order.\n\n" + FILLER, encoding="utf-8")
    make_approvals(d, a)
    second = ps.search(d, a, "giraffe rule")
    stale = ps.search(d, a, "zebra rule")
    record(bool(first) and bool(second) and all("zebra" not in h["text"] for h in stale),
           "index refreshes after an approved file changes")

# A file with no "##" headings is still searchable
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp) / "p"
    d.mkdir()
    a = Path(tmp) / "a.json"
    (d / "plain.md").write_text("# Plain\n\nThe zebra rule applies to every order.\n", encoding="utf-8")
    (d / "other.md").write_text("# Other\n\n" + FILLER, encoding="utf-8")
    make_approvals(d, a)
    record(any(h["source"] == "plain.md" for h in ps.search(d, a, "zebra rule")),
           "file without headings is still searchable")

# ---------------------------------------------------------------------------
# Part 4: is YOUR project ready? (the real approved list covers the real policy files)
# ---------------------------------------------------------------------------
print("\nProject readiness")
status = ps.file_status(POLICIES, PROJECT_APPROVALS)
not_ok = {n: s for n, s in status.items() if s != "APPROVED"}
record(bool(status) and not not_ok, "every file in data/policies is approved in data/policy_approvals.json",
       f"not approved: {not_ok}. After reviewing the files, run: python scripts\\approve_policies.py --approve-all")

# ---------------------------------------------------------------------------
# Known weakness of keyword search (does not count as a failure)
# ---------------------------------------------------------------------------
print("\nKnown weakness")
weak_hits = [label(h) for h in hits_for("send it back")]
wanted = ["returns.md | Return window", "returns.md | How to request a return"]
print(("PASS  " if any(w in weak_hits for w in wanted) else "WEAK  ") + '"send it back"'
      + ("" if any(w in weak_hits for w in wanted) else f"   got={weak_hits}"))

tmp_quality.cleanup()

print()
print(f"{sum(results)} of {len(results)} checks passed")
sys.exit(0 if all(results) else 1)