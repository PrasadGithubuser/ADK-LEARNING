"""Checks the refund code against docs/02-refund-rules.md. No AI, no API calls.

Run from the project root:   python evals\\check_refund_rules.py
"""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Load refund_logic.py directly by file path (avoids starting the whole agent).
spec = importlib.util.spec_from_file_location("refund_logic", ROOT / "support_agent" / "refund_logic.py")
refund_logic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refund_logic)
evaluate_refund = refund_logic.evaluate_refund

TODAY = date(2026, 10, 31)
orders = {o["order_id"]: o for o in json.loads((ROOT / "data" / "orders.json").read_text(encoding="utf-8"))}

# Expected results copied from section 3 of docs/02-refund-rules.md
EXPECTED = {
    "NC1001": ("APPROVED_AUTO", 2499),
    "NC1002": ("REJECTED_WINDOW_EXPIRED", None),
    "NC1003": ("REJECTED_NOT_DELIVERED", None),
    "NC1004": ("REJECTED_NOT_DELIVERED", None),
    "NC1005": ("PENDING_HUMAN_APPROVAL", 68000),
    "NC1006": ("REJECTED_CANCELLED", None),
    "NC1007": ("PENDING_HUMAN_APPROVAL", 7999),
    "NC1008": ("REJECTED_NON_RETURNABLE", None),
    "NC1009": ("APPROVED_AUTO", 1797),
    "NC1010": ("APPROVED_AUTO", 3299),
}

results = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   got={got} want={want}"))


# 1. The ten fictional orders
for oid, (outcome, amount) in EXPECTED.items():
    d = evaluate_refund(orders[oid], [], TODAY)
    check(f"{oid} outcome", d["outcome"], outcome)
    if amount is not None:
        check(f"{oid} amount", d.get("amount_inr"), amount)


def make_order(delivered, price=1000, category="accessories", qty=1):
    return {"order_id": "NCX", "status": "delivered", "delivered_date": delivered,
            "items": [{"name": "Test item", "category": category, "price_inr": price, "qty": qty}]}


# 2. Edge cases around the limits
check("day 30 is allowed", evaluate_refund(make_order("2026-10-01"), [], TODAY)["outcome"], "APPROVED_AUTO")
check("day 31 is rejected", evaluate_refund(make_order("2026-09-30"), [], TODAY)["outcome"], "REJECTED_WINDOW_EXPIRED")
check("Rs 5000 is automatic", evaluate_refund(make_order("2026-10-20", price=5000), [], TODAY)["outcome"], "APPROVED_AUTO")
check("Rs 5001 needs a human", evaluate_refund(make_order("2026-10-20", price=5001), [], TODAY)["outcome"], "PENDING_HUMAN_APPROVAL")

# 3. One refund per order
check("second refund is blocked", evaluate_refund(orders["NC1001"], [{"order_id": "NC1001"}], TODAY)["outcome"], "REJECTED_ALREADY_REFUNDED")

# 4. Mixed order: software is skipped, the rest is refunded
mixed = make_order("2026-10-20")
mixed["items"].append({"name": "Antivirus", "category": "software", "price_inr": 1200, "qty": 1})
check("mixed order refunds only non-software", evaluate_refund(mixed, [], TODAY).get("amount_inr"), 1000)

print()
print(f"{sum(results)} of {len(results)} checks passed")
sys.exit(0 if all(results) else 1)