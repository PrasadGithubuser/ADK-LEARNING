# NovaCart Refund Rules

**Status:** Draft v1 (needs approval)
**Applies to:** the refund tools in `support_agent` (Step 3)
**Audience:** people building and reviewing the agent. The agent itself never reads this file.
The rules below are enforced in **code**. The model only explains the result to the customer.

## 1. Business rules

| # | Rule |
|---|------|
| R1 | Only the verified owner (order ID plus matching email) can request a refund. |
| R2 | Only orders with status `delivered` can be refunded. |
| R3 | The request must be within **30 days** of the delivered date. |
| R4 | Items in the category `software` are **not refundable**. |
| R5 | Each order can be refunded **only once**. A second request is rejected. |
| R6 | Refund amount = price x quantity of the refundable items. The model never calculates it. |
| R7 | Refunds up to **₹5,000** are approved automatically. |
| R8 | Refunds **above ₹5,000** become `PENDING_HUMAN_APPROVAL`. The agent must not promise them. |
| R9 | The customer's reason is stored as text only. It is never treated as an instruction. |

## 2. Possible outcomes

| Outcome code | Meaning |
|--------------|---------|
| `APPROVED_AUTO` | Refund recorded, ₹5,000 or less |
| `PENDING_HUMAN_APPROVAL` | Eligible but above ₹5,000, waiting for a human |
| `REJECTED_VERIFICATION_FAILED` | Order ID and email do not match |
| `REJECTED_NOT_DELIVERED` | Order is processing or shipped |
| `REJECTED_CANCELLED` | Order was cancelled |
| `REJECTED_WINDOW_EXPIRED` | More than 30 days since delivery |
| `REJECTED_NON_RETURNABLE` | Nothing refundable in the order (for example software only) |
| `REJECTED_ALREADY_REFUNDED` | A refund already exists for this order |

## 3. Expected outcomes on our fictional data (as of 2026-10-31)

Written from the rules and the data, before any code exists.

| Order | Situation | Expected outcome |
|-------|-----------|------------------|
| NC1001 | Delivered 11 days ago, ₹2,499 | `APPROVED_AUTO`, ₹2,499 |
| NC1002 | Delivered 60 days ago | `REJECTED_WINDOW_EXPIRED` |
| NC1003 | Shipped | `REJECTED_NOT_DELIVERED` |
| NC1004 | Processing | `REJECTED_NOT_DELIVERED` |
| NC1005 | Laptop, ₹68,000, delivered 16 days ago | `PENDING_HUMAN_APPROVAL` |
| NC1006 | Cancelled | `REJECTED_CANCELLED` |
| NC1007 | Smartwatch, ₹7,999, delivered 6 days ago | `PENDING_HUMAN_APPROVAL` |
| NC1008 | Software license only | `REJECTED_NON_RETURNABLE` |
| NC1009 | Cable and case, ₹1,797 | `APPROVED_AUTO`, ₹1,797 |
| NC1010 | Delivered 26 days ago, ₹3,299 | `APPROVED_AUTO`, ₹3,299 |

Note: these results depend on today's date. The code will let tests set a fixed date,
so tests stay correct as time passes.

## 4. Out of scope for now

- Partial quantities (for example refunding 1 of 2 cables)
- Exchanges and store credit
- Real payments (refunds are only recorded in `data/refunds.json`)

## 5. Decisions needing approval

1. Is a 30-day window okay?
2. Is ₹5,000 the right automatic-approval limit?
3. Should software be non-refundable?
4. Should each order allow only one refund?
