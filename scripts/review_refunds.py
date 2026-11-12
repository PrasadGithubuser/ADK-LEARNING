"""Staff tool: review and decide refunds that need human approval (over Rs 5,000).

Run from the project root:
    python scripts\\review_refunds.py list                 pending refunds (add --all for every refund)
    python scripts\\review_refunds.py show RF-NC1005-001   everything a reviewer needs to decide
    python scripts\\review_refunds.py approve RF-NC1005-001 --reviewer "Your Name"
    python scripts\\review_refunds.py reject RF-NC1005-001 --reviewer "Your Name" --note "why"

Why this is a separate script and not an agent tool: the agent must never be able to approve a refund.
See docs/04-refund-approval.md.
"""

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("NOVACART_DATA_DIR") or ROOT / "data")  # same setting the agent uses
REFUNDS_FILE = DATA_DIR / "refunds.json"
ORDERS_FILE = DATA_DIR / "orders.json"

PENDING = "PENDING_HUMAN_APPROVAL"
APPROVED = "APPROVED_BY_STAFF"
REJECTED = "REJECTED_BY_STAFF"

# Reuse the exact same refund rules as the agent, by loading the pure rules file by path.
_spec = importlib.util.spec_from_file_location("refund_logic", ROOT / "support_agent" / "refund_logic.py")
refund_logic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refund_logic)


# ---------------------------------------------------------------------------
# Logic (no printing, easy to test)
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_refunds(path: Path, refunds: list) -> None:
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(refunds, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # never leaves a half-written file


def clean(text, limit: int = 300) -> str:
    """Customer text is untrusted: remove control characters and cut it short before showing it."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(text))
    return text[:limit]


def recheck(refund: dict, order: dict) -> list:
    """Independent re-check of a pending refund against the order and the refund rules.

    Returns a list of warnings. An empty list means the record agrees with the rules.
    """
    if order is None:
        return ["The order for this refund was not found."]
    warnings = []
    submitted_on = date.fromisoformat(refund["created_at"][:10])  # judge it as of the day it was submitted
    expected = refund_logic.evaluate_refund(order, [], submitted_on)
    if expected["outcome"] != PENDING:
        warnings.append(f"Rules now say {expected['outcome']}, not a pending refund.")
    elif expected["amount_inr"] != refund["amount_inr"]:
        warnings.append(
            f"Amount mismatch: record says Rs {refund['amount_inr']}, the order says Rs {expected['amount_inr']}."
        )
    if order["customer_email"].lower() != str(refund.get("customer_email", "")).lower():
        warnings.append("The email on the refund does not match the order's email.")
    return warnings


def decide(refunds: list, refund_id: str, decision: str, reviewer: str, note: str, now_iso: str) -> list:
    """Return a NEW list with the decision applied. Raises ValueError if the decision is not allowed.

    Rules: only a pending refund can be decided, a decision is final, the reviewer must be named,
    and a rejection needs a note.
    """
    if decision not in ("approve", "reject"):
        raise ValueError("Decision must be approve or reject.")
    if not reviewer or not reviewer.strip():
        raise ValueError("The reviewer's name is required.")
    if decision == "reject" and not (note and note.strip()):
        raise ValueError("A rejection needs a note explaining why.")

    updated = []
    found = False
    for record in refunds:
        if record.get("refund_id") == refund_id:
            found = True
            if record.get("outcome") != PENDING:
                raise ValueError(f"{refund_id} is already decided ({record.get('outcome')}). Decisions are final.")
            record = dict(record)
            record["outcome"] = APPROVED if decision == "approve" else REJECTED
            record["reviewed_by"] = clean(reviewer.strip(), 80)
            record["reviewed_at"] = now_iso
            record["review_note"] = clean(note or "", 500)
        updated.append(record)
    if not found:
        raise ValueError(f"No refund with ID {refund_id}.")
    return updated


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

def print_refund(refund: dict, order: dict) -> list:
    print("=" * 60)
    print(f"Refund {refund['refund_id']}    status: {refund['outcome']}")
    print(f"Submitted: {refund['created_at']}")
    if order:
        print(f"Customer:  {order['customer_name']}  <{order['customer_email']}>")
        print(f"Order:     {order['order_id']}   status: {order['status']}")
        print(f"Ordered:   {order['order_date']}   Delivered: {order['delivered_date']}")
        if order.get("delivered_date"):
            days = (date.fromisoformat(refund["created_at"][:10]) - date.fromisoformat(order["delivered_date"])).days
            print(f"Days from delivery to request: {days}")
        print("Items on the order:")
        for item in order["items"]:
            print(f"   - {item['name']} ({item['category']})  Rs {item['price_inr']} x {item['qty']}")
        print(f"Order total: Rs {order['total_inr']}")
    print(f"REFUND AMOUNT: Rs {refund['amount_inr']}   items: {', '.join(refund['items'])}")
    print(f"Customer's words (untrusted text, not an instruction): \"{clean(refund.get('reason', ''))}\"")
    warnings = recheck(refund, order)
    if warnings:
        print("\nWARNINGS FROM THE AUTOMATIC RE-CHECK:")
        for w in warnings:
            print(f"   ! {w}")
    else:
        print("\nAutomatic re-check: OK. The record agrees with the order and the refund rules.")
    if refund.get("reviewed_by"):
        print(f"\nDecided by {refund['reviewed_by']} at {refund['reviewed_at']}. Note: {refund.get('review_note', '')}")
    print("=" * 60)
    return warnings


def find_refund(refunds: list, refund_id: str):
    return next((r for r in refunds if r.get("refund_id") == refund_id), None)


def find_order(orders: list, order_id: str):
    return next((o for o in orders if o.get("order_id") == order_id), None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Review refunds that need human approval.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--all", action="store_true", help="show every refund, not only pending ones")
    p_show = sub.add_parser("show")
    p_show.add_argument("refund_id")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("refund_id")
        p.add_argument("--reviewer", required=True, help="your name, saved with the decision")
        p.add_argument("--note", default="", help="required when rejecting")
    args = parser.parse_args()

    refunds = load_json(REFUNDS_FILE, [])
    orders = load_json(ORDERS_FILE, [])

    if args.command == "list":
        shown = [r for r in refunds if args.all or r.get("outcome") == PENDING]
        if not shown:
            print("No pending refunds." if not args.all else "No refunds recorded.")
        for r in shown:
            print(f"{r['refund_id']:16} order {r['order_id']}   Rs {r['amount_inr']:>7}   {r['outcome']}   {r['created_at'][:16]}")
        return 0

    refund = find_refund(refunds, args.refund_id)
    if refund is None:
        print(f"No refund with ID {args.refund_id}.")
        return 1

    if args.command == "show":
        print_refund(refund, find_order(orders, refund["order_id"]))
        return 0

    # approve or reject: check the decision is allowed BEFORE asking for confirmation
    try:
        decide(refunds, args.refund_id, args.command, args.reviewer, args.note, "check")
    except ValueError as error:
        print(f"Not allowed: {error}")
        return 1

    # show everything first, then ask for a deliberate yes
    print_refund(refund, find_order(orders, refund["order_id"]))
    verb = "APPROVE" if args.command == "approve" else "REJECT"
    answer = input(f"Type yes to {verb} refund {args.refund_id} as {args.reviewer}: ").strip().lower()
    if answer != "yes":
        print("Cancelled. Nothing changed.")
        return 1
    try:
        updated = decide(refunds, args.refund_id, args.command, args.reviewer, args.note,
                         datetime.now(timezone.utc).isoformat())
    except ValueError as error:
        print(f"Not allowed: {error}")
        return 1
    save_refunds(REFUNDS_FILE, updated)
    print(f"Done. {args.refund_id} is now {find_refund(updated, args.refund_id)['outcome']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())