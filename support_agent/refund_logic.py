"""Refund decision logic for NovaCart (see docs/02-refund-rules.md).

Pure code: no AI, no ADK, no file access. Easy to test.
"""

import os
from datetime import date

REFUND_WINDOW_DAYS = 30                 # R3
AUTO_APPROVE_LIMIT_INR = 5000           # R7
NON_REFUNDABLE_CATEGORIES = {"software"}  # R4


def get_today() -> date:
    """Today's date. Tests can pin it with the NOVACART_TODAY environment variable."""
    pinned = os.environ.get("NOVACART_TODAY")
    if pinned:
        try:
            return date.fromisoformat(pinned)
        except ValueError:
            pass
    return date.today()


def evaluate_refund(order: dict, existing_refunds: list, today: date) -> dict:
    """Decide the outcome for a refund request on an already-verified order (R2 to R8)."""
    # R5: one refund per order
    if any(r["order_id"] == order["order_id"] for r in existing_refunds):
        return {"outcome": "REJECTED_ALREADY_REFUNDED"}

    if order["status"] == "cancelled":
        return {"outcome": "REJECTED_CANCELLED"}

    # R2: only delivered orders
    if order["status"] != "delivered" or not order.get("delivered_date"):
        return {"outcome": "REJECTED_NOT_DELIVERED"}

    # R3: 30-day window (day 30 is still allowed)
    days_since_delivery = (today - date.fromisoformat(order["delivered_date"])).days
    if days_since_delivery > REFUND_WINDOW_DAYS:
        return {"outcome": "REJECTED_WINDOW_EXPIRED"}

    # R4: software is not refundable
    refundable = [i for i in order["items"] if i["category"] not in NON_REFUNDABLE_CATEGORIES]
    if not refundable:
        return {"outcome": "REJECTED_NON_RETURNABLE"}

    # R6: amount comes from code, never from the model
    amount = sum(i["price_inr"] * i["qty"] for i in refundable)

    # R7 / R8: approval limit
    outcome = "APPROVED_AUTO" if amount <= AUTO_APPROVE_LIMIT_INR else "PENDING_HUMAN_APPROVAL"
    return {"outcome": outcome, "amount_inr": amount, "items": [i["name"] for i in refundable]}


# What a customer may see about a refund. Never the reason text, the staff name, or staff notes.
CUSTOMER_VISIBLE_FIELDS = (
    "refund_id", "order_id", "outcome", "amount_inr", "items", "created_at", "reviewed_at",
)


def customer_view(record: dict) -> dict:
    """Return only the safe fields of a refund record."""
    return {key: record[key] for key in CUSTOMER_VISIBLE_FIELDS if key in record}