# Refund Approval: The Customer Asks, Only Staff Decides

**Status:** Implemented (Step 6)
**Code:** `scripts/review_refunds.py` (staff), `support_agent/agent.py` (`request_refund`, `get_refund_status`),
`support_agent/refund_logic.py`
**Tests:** `tests-refund-approval.md` (with the agent) and `python evals\check_refund_review.py` (no AI)

## 1. The problem in one sentence

A refund of more than Rs 5,000 is too risky for the AI or the customer to approve. A human staff member
must look at the facts and decide.

## 2. The design

| Who | What they can do |
|-----|------------------|
| **Customer** (talks to the agent) | Ask for a refund. Check the status of their own refund. Nothing else. |
| **The agent** | Record a refund request. Report the status. It has **no tool that approves, rejects or changes** a refund. |
| **Code** | Check the rules (30 days, no software, one refund per order) and decide whether a request is approved automatically (Rs 5,000 or less) or waits for a human (over Rs 5,000). |
| **Staff member** (a person, using `scripts/review_refunds.py`) | See every detail and approve or reject. This is the only way a big refund can be approved. |

## 3. The life of a refund

```
Customer asks  ->  code checks the rules
                       |-- rule broken            -> REJECTED_... (explained to the customer)
                       |-- Rs 5,000 or less       -> APPROVED_AUTO
                       '-- more than Rs 5,000      -> PENDING_HUMAN_APPROVAL  (waits in data/refunds.json)
                                                          |
                                      staff member reviews |
                                          |-- approve  -> APPROVED_BY_STAFF
                                          '-- reject   -> REJECTED_BY_STAFF   (needs a note)
```

A decision by staff is **final**. A refund that is already decided cannot be changed, so nobody
(including a tricked agent) can flip an outcome later.

## 4. What the staff member sees

The review screen shows everything needed to decide, not just a yes or no:

- Customer name and email
- Order ID, order date, delivery date, and how many days passed before the request
- Every item with its price and quantity, and the order total
- The refund amount and which items it covers
- The customer's own words, **labelled as untrusted text** (a customer can write anything there)
- An **automatic re-check**: our code recalculates the refund from the order and the rules, and warns
  if anything disagrees (for example, a changed amount or a different email)

The script asks for a deliberate `yes` before saving, and it records the reviewer's name and the time.

## 5. Why staff approval is NOT an ADK feature here

ADK has a built-in "tool confirmation" feature: the run pauses and asks a human to confirm. But the
human who answers is whoever is in the chat, and in our agent that is the **customer**. A customer
must never approve their own refund, so we built staff approval ourselves, outside the agent.

ADK's feature is still useful for a different job: asking the **customer** "do you want to submit
this refund?" before an action that cannot be undone. We decided to skip that for now and return to
it when we study more ADK features.

## 6. Why the staff script is separate from the agent

The same idea as the approved policy list: the agent can **read** state but must never be able to
**write** a decision. If the agent had an approve function, anyone who tricked the agent into calling it
could approve their own refund. A test (`check_refund_review.py`) checks that `agent.py` contains no approve,
reject, or decide function and never writes a reviewer name.

## 7. What a customer can see

`get_refund_status` shows the customer only: refund ID, order ID, outcome, amount, items and dates.
It never shows the customer's own reason text, the staff member's name, or the staff note.
The customer must give the order ID and the email, and failed attempts count toward the same
3-strikes lock as every other tool.

## 8. How to use it (staff)

Run from the project folder.

| What you want | Command |
|---------------|---------|
| See pending refunds | `python scripts\review_refunds.py list` |
| See everything about one refund | `python scripts\review_refunds.py show RF-NC1005-001` |
| Approve | `python scripts\review_refunds.py approve RF-NC1005-001 --reviewer "Your Name"` |
| Reject (a note is required) | `python scripts\review_refunds.py reject RF-NC1005-001 --reviewer "Your Name" --note "reason"` |
| See every refund, decided or not | `python scripts\review_refunds.py list --all` |

## 9. Limits: what this does not protect against

- **The reviewer name is typed, not verified.** Anyone who can run the script can approve. In production,
  staff log in to an admin tool with their own account and role, and every decision is logged to them.
- **Anyone who can edit `data/refunds.json` directly can change a record.** In production this is
  a database with restricted access and a change log. The re-check in the staff screen helps catch a
  tampered amount, but it is not a replacement for access control.
- **One refund per order, even after a rejection.** A customer whose refund was rejected must talk to a
  human agent. The agent will not accept a second request.
- **Nobody is notified automatically.** In production, a new pending refund would alert the review
  team, and the decision would notify the customer. Here the customer asks for the status.
- **On this laptop we are learning the mechanism**, not running a full production system.

## 10. If someone asks you to explain it

**Q: Why doesn't the agent approve big refunds?**
A: An AI can be tricked by clever wording, and a customer should never approve their own refund. The code
only decides whether a request is allowed to go to a human, and a person makes the final call.

**Q: Why is the approval tool outside the agent?**
A: If the agent had a way to approve, an attacker who manipulated the agent could use it. The agent can
only read a refund's status. A test checks that the approval code is not in the agent at all.

**Q: What does the reviewer see?**
A: The full picture: customer, order, items, dates, amount, the customer's own words marked as untrusted,
and an automatic check from our code that the numbers match the rules.

**Q: What about ADK's built-in confirmation feature?**
A: I read it. The person who confirms is whoever is in the chat, which here is the customer, so it can't
be used for staff approval. It would be useful for asking the customer to confirm an action that cannot be undone.

**Q: What would you change for real production?**
A: Staff login and roles, a real database with access control and an audit log, automatic alerts for new
pending refunds, and customer notifications.

**Q: Which risks does this address?**
A: OWASP LLM06 excessive agency (the agent cannot approve), LLM02 sensitive information disclosure (customers
never see staff names or notes), and LLM01 prompt injection (customer text is shown to staff as untrusted data).
