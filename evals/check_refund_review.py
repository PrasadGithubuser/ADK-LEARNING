"""Checks the staff review rules and the refund visibility rules. No AI, no API calls.

Run from the project root:   python evals\\check_refund_review.py
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


review = load("review_refunds", ROOT / "scripts" / "review_refunds.py")
logic = load("refund_logic", ROOT / "support_agent" / "refund_logic.py")
orders = {o["order_id"]: o for o in json.loads((ROOT / "data" / "orders.json").read_text(encoding="utf-8"))}

results = []


def record(ok, text, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + text + ("" if ok else f"   {detail}"))


def raises(function, *args):
    try:
        function(*args)
    except ValueError:
        return True
    return False


NOW = "2026-10-31T10:00:00+00:00"


def pending():
    return {
        "refund_id": "RF-NC1005-001", "order_id": "NC1005", "customer_email": "karan.mehta@example.com",
        "amount_inr": 68000, "items": ["Laptop 14-inch"], "outcome": "PENDING_HUMAN_APPROVAL",
        "reason": "screen cracked", "created_at": "2026-10-31T09:00:00+00:00",
    }


print("Deciding a refund")
before = [pending()]
after = review.decide(before, "RF-NC1005-001", "approve", "Asha", "", NOW)
record(after[0]["outcome"] == "APPROVED_BY_STAFF" and after[0]["reviewed_by"] == "Asha" and after[0]["reviewed_at"] == NOW,
       "approving a pending refund records the outcome, the reviewer and the time")
record(before[0]["outcome"] == "PENDING_HUMAN_APPROVAL", "the original list is not changed in place")

rejected = review.decide(before, "RF-NC1005-001", "reject", "Asha", "outside policy", NOW)
record(rejected[0]["outcome"] == "REJECTED_BY_STAFF" and rejected[0]["review_note"] == "outside policy",
       "rejecting records the outcome and the note")

record(raises(review.decide, before, "RF-NC1005-001", "reject", "Asha", "", NOW), "a rejection needs a note")
record(raises(review.decide, before, "RF-NC1005-001", "approve", "  ", "", NOW), "the reviewer must be named")
record(raises(review.decide, before, "RF-NOPE-001", "approve", "Asha", "", NOW), "an unknown refund ID is refused")
record(raises(review.decide, before, "RF-NC1005-001", "maybe", "Asha", "", NOW), "only approve or reject are allowed")

print("\nDecisions are final")
for outcome in ("APPROVED_AUTO", "APPROVED_BY_STAFF", "REJECTED_BY_STAFF"):
    done = pending()
    done["outcome"] = outcome
    record(raises(review.decide, [done], "RF-NC1005-001", "approve", "Asha", "", NOW),
           f"a refund that is already {outcome} cannot be decided again")

print("\nAutomatic re-check")
record(review.recheck(pending(), orders["NC1005"]) == [], "a normal pending refund passes the re-check")
tampered = pending()
tampered["amount_inr"] = 99999
record(any("Amount mismatch" in w for w in review.recheck(tampered, orders["NC1005"])), "a changed amount is flagged")
wrong_email = pending()
wrong_email["customer_email"] = "someone.else@example.com"
record(any("email" in w for w in review.recheck(wrong_email, orders["NC1005"])), "a different email is flagged")
expired = pending()
expired.update({"refund_id": "RF-NC1002-001", "order_id": "NC1002", "customer_email": "rahul.verma@example.com", "amount_inr": 1299})
record(bool(review.recheck(expired, orders["NC1002"])), "a refund that breaks the rules is flagged")
record(bool(review.recheck(pending(), None)), "a refund with no matching order is flagged")

print("\nWhat a customer can see")
full = pending()
full.update({"reviewed_by": "Asha", "review_note": "internal note", "reviewed_at": NOW})
view = logic.customer_view(full)
record("reason" not in view and "reviewed_by" not in view and "review_note" not in view and "customer_email" not in view,
       "the customer view never includes the reason, the staff name, the staff note or the email")
record(view["outcome"] == "PENDING_HUMAN_APPROVAL" and view["amount_inr"] == 68000, "the customer view keeps the outcome and the amount")

print("\nThe agent cannot approve anything")
agent_source = (ROOT / "support_agent" / "agent.py").read_text(encoding="utf-8")
record("reviewed_by" not in agent_source, "agent.py never writes a reviewer name")
record("def approve" not in agent_source and "def decide" not in agent_source and "def reject" not in agent_source,
       "agent.py has no approve, reject or decide function")
record("APPROVED_BY_STAFF\"" not in agent_source.split("INSTRUCTION")[0],
       "agent.py code never sets the APPROVED_BY_STAFF outcome")

print()
print(f"{sum(results)} of {len(results)} checks passed")
sys.exit(0 if all(results) else 1)
