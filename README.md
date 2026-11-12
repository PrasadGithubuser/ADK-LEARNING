# NovaCart Support Agent

A customer support agent for a **fictional** electronics store, built with Google ADK as a learning project for production-style agents.

Author: Bhavani Prasad Yegade

> All data in this project is fictional. Do not put real customer data in it: the free Gemini API tier may use prompts to improve Google products.

## Core idea

The AI understands and talks. Plain code does anything exact, safe, or final.

A prompt cannot guarantee behavior, so every rule that matters (refund rules, amounts, approvals, limits, security) is enforced in code, not in the prompt.

## What the agent can do

| Tool | What it does |
|---|---|
| `get_order_status(order_id, customer_email)` | Shows the status of an order |
| `request_refund(order_id, customer_email, reason)` | Requests a refund; rules are checked by code |
| `get_refund_status(order_id, customer_email)` | Shows the status of a refund |
| `search_policies(query)` | Answers policy questions (returns, shipping, warranty) |

If order ID and email do not match, the agent gives one generic "not found" message. It never reveals which part was wrong.

## Refund rules (enforced in `refund_logic.py`)

- Only delivered orders can be refunded.
- Within 30 days of delivery (day 30 is allowed).
- Software is not refundable.
- One refund per order.
- The amount is calculated by code.
- Up to Rs 5,000: approved automatically.
- Above Rs 5,000: waits for human approval (`PENDING_HUMAN_APPROVAL`).
- Customers only see safe fields. They never see internal reasons, staff names, or notes.

The agent has **no** function to approve a refund, and a test checks this. Approval is done by staff with a separate script.

## Safety design

- **Guardrails plugin** (runs in code, outside the model):
  - Messages over 2,000 characters are refused before any model call.
  - Input checks: order ID format `NC` + 4 digits, email format, length limits.
  - The order ID and email must appear in the customer's own words (stops the model from inventing them).
  - Chat lock after 3 failed verifications, plus a per-order lock (5 failures in 15 minutes across chats).
  - Maximum 6 tool calls per message.
  - Audit log with masked emails and no free text.
  - Fails closed: if a guard errors, the request is blocked.
- **Approved policy files only:** policy files are searched only if their SHA-256 fingerprint is listed in `data/policy_approvals.json`. Edited or unapproved files are ignored. After editing a policy file, approve it again.
- **Human approval for large refunds:** see "Staff commands" below.

## Setup (Windows, PowerShell)

Requirements: Python 3.13, Google ADK 2.12.0, a Gemini API key from Google AI Studio.

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create `support_agent\.env` with:

```
GOOGLE_GENAI_USE_VERTEXAI=FALSE
GOOGLE_API_KEY=your-key-here
NOVACART_TODAY=2026-10-31
```

- Never share or commit `.env`.
- Do not add `NOVACART_DATA_DIR` to `.env`. Only the exam runner sets it.
- `NOVACART_TODAY` fixes the date so tests give the same results.

## Run the agent

Run this from the project folder (the folder that contains `support_agent`):

```powershell
adk web
```

Open the address shown in the terminal, then choose **support_agent** in the dropdown at the top left.

Model: `gemini-3.5-flash-lite` on the free tier (15 requests per minute, 500 per day). Google "503 high demand" errors are Google's capacity, not your quota.

## Staff commands

Refund approvals (the script needs an action; running it alone gives an error):

```powershell
python scripts\review_refunds.py list
python scripts\review_refunds.py list --all
python scripts\review_refunds.py show REFUND_ID
python scripts\review_refunds.py approve REFUND_ID --reviewer NAME
python scripts\review_refunds.py reject REFUND_ID --reviewer NAME --note "reason"
```

`show` displays full details and re-checks the rules automatically. A rejection needs a note. Approve and reject are final.

Policy approvals:

```powershell
python scripts\approve_policies.py
python scripts\approve_policies.py --approve FILE
python scripts\approve_policies.py --approve-all
```

## Tests

Code checks (no AI, no API key needed):

```powershell
python evals\check_refund_rules.py
python evals\check_policy_search.py
python evals\check_refund_review.py
python evals\check_guardrails.py
```

These contain 116 checks in total (21 + 24 + 20 + 51). One expected "WEAK" line in the policy search check is a known limitation.

Automatic exam (uses the Gemini API, 70 cases: orders, refunds, policy, approval, guardrails):

```powershell
python evals\run_exam.py --list
python evals\run_exam.py --dry-run
python evals\run_exam.py --group orders
```

- It runs on a temporary copy of the data, so real data is not changed.
- It uses plain-code checks only (no AI judge).
- Google errors (503, quota) are reported as "not tested", not as failures.
- Close `adk web` before running it, because both share the same quota.
- Reports are saved in `evals\results\`.
- A full run needs about 240 requests, so plan around the daily limit (resets at 1:30 PM IST).
- Always read a failing reply yourself before blaming the agent: several early failures were wrong test expectations.

## Known limitations

- Policy search matches words, not meaning (for example, "send it back" can miss "return"). Embeddings or hybrid search is a possible upgrade.
- A stranger who guesses an order ID can lock that order for up to 15 minutes.
- The model sometimes phrases things in ways a strict test does not expect.

## Project layout

```
support_agent/   agent, refund logic, policy search, guardrails
data/            orders, refunds, policies, approvals, audit log
scripts/         staff tools (refund review, policy approval)
evals/           code checks, exam runner, test cases
docs/            full documentation (start with 00-project-guide.md)
```

## Documentation

- `docs/00-project-guide.md`: full guide
- `docs/02-refund-rules.md`, `03-knowledge-base-security.md`, `04-refund-approval.md`, `05-guardrails.md`, `06-exam-runner.md`

ADK documentation: https://adk.dev