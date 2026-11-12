# Guardrails: Security Guards for the Agent

**Status:** Implemented (Step 7)
**Code:** `support_agent/guardrails.py` (the rules), `support_agent/guardrails_plugin.py` (the ADK plugin),
`support_agent/agent.py` (registers the plugin on an `App`)
**Tests:** `python evals\check_guardrails.py` (no AI) and `tests-guardrails.md` (live cases)
**Cost:** nothing. Plain Python, no installs, no paid services, and no model quota is used by the guards.

## 1. The problem in simple words

Before this step, the agent had some protection, but with gaps:

- A stranger could guess emails for an order. The 3-strikes lock only lasted for **one chat**, so opening a new chat
  reset it.
- Strange input (a huge email, an order ID full of symbols) reached the tools.
- A very long message, or one message asking for ten lookups, could use up a lot of model quota.
- Nothing recorded what the agent did, so there was no way to investigate a problem later.

## 2. The idea: guards at the doors

Think of the agent as a building with doors:

1. **Door 1:** a customer message arrives.
2. **Door 2:** just before a tool runs.
3. **Door 3:** just after a tool has run.

A **plugin** is a security guard at the building entrance who covers all these doors, for every tool.
Without it, we would need to put a guard inside every tool, and each new tool could forget one.
ADK's documentation recommends plugins for security guardrails because they are easier to keep consistent.

The plugin only decides **when** to check. The checks themselves are plain Python in `guardrails.py`, so they can
be tested without ADK and without any AI.

## 3. The six guards

| # | Guard | Where | What it does |
|---|-------|-------|--------------|
| 1 | Message length | Door 1 | A message over 2,000 characters is refused with a short reply, before the model sees it. This saves quota. |
| 2 | Input check | Door 2 | The order ID must look like `NC1234`, the email must look like an email (and not be huge), the reason is under 500 characters, a policy search is under 200 characters. Bad input never reaches the tool. |
| 3 | Per-order limit | Doors 2 and 3 | After a failed verification (door 3), the failure is counted against that **order ID**. At 5 failures within 15 minutes the order is blocked (door 2), even from a new chat. Old failures drop out of the window, so the block ends by itself. |
| 4 | Tool-call counter | Door 2 | One customer message may trigger at most 6 tool calls. This stops "check these 10 orders" floods. |
| 5 | Audit log | Doors 1, 2, 3 | Every call, block and result is written to `data/audit.log`, one line each. |
| 6 | Values must come from the customer | Door 2 | The order ID and the email passed to a tool must appear in what the customer typed in this chat. If the model invents a value, the call is stopped before the tool runs. |

The existing 3-strikes lock inside each chat stays as a second layer.

## 4. The audit log

An audit log is a diary of what the agent did. An example line:

```
{"time": "2026-11-03T10:15:02+00:00", "event": "tool_result", "tool": "get_order_status",
 "session": "bad72fc4", "order_id": "NC1005", "email": "k***@example.com", "status": "error", "error_code": "NOT_FOUND"}
```

**Privacy rules for the log:**
- Emails are masked (`k***@example.com`).
- Free text typed by a customer (the refund reason, a policy question) is **never** copied in. Only its length is.
- Search results and order details are not logged.

If someone is guessing emails, the log would show many `NOT_FOUND` lines for one order in a row.

## 5. What the agent does when a guard says no

The guard returns an error code to the model, and the prompt tells the model what to say:

| Code | Meaning | What the customer hears |
|------|---------|--------------------------|
| `INVALID_INPUT` | Bad order ID, email, or too-long text | Please check that detail (order IDs look like NC1234) |
| `TEMPORARILY_LOCKED` | Too many failed attempts for that order | Try again later, or talk to a human |
| `TOO_MANY_REQUESTS` | Too many tool calls in one message | Ask for fewer items at a time |
| `GUARDRAIL_ERROR` | The guard itself failed | The request could not be checked, try later |

## 6. Design decisions and why

- **Fail closed:** if the guard code itself has an error, the tool call is blocked. A broken guard must not let
  a request through. (The message-length check is the one exception, because it is about cost, not security.)
- **A block on an order does not count as a new failure,** so a blocked order does not stay blocked forever.
- **The lock is temporary.** A permanent lock would let an attacker deliberately lock a real customer out of
  their own order (see the limits below).
- **The limit is tied to the order ID, not the chat.** A new chat does not help an attacker.
- **Customer text is never trusted.** It is validated, and it is kept out of the log.

## 6b. A finding from the automatic exam: the model invented an email

In exam case AP-H1 the customer gave only a refund ID (`RF-NC1005-001`) and no email. The tool
`get_refund_status` still ran. The model had **made up an email address** to fill the tool's required field,
even though the prompt said to wait until the customer gives both details.

What this taught us:

- **A prompt cannot guarantee tool behavior.** It is the same lesson as the very first calculator experiment.
- **No data leaked,** because the tool's own verification rejected the made-up email. But the failed lookup was
  **counted against order NC1005** by the per-order limit. A model that invents values could burn the attempts of
  a real customer's order.
- **The fix is code, not a better prompt:** guard 6 stops the call when the order ID or email did not come from
  the customer's own words. A blocked call is not counted as a failed attempt.

The exam case now also checks that no failed attempt was recorded for NC1005.

Limits of guard 6: it checks that the value **appears** in what the customer typed. It does not check that the
value is right (the tool does that), and it relies on reading the chat's messages. If the messages cannot be read,
the check is skipped, an audit line `grounding_skipped` is written, and the tool's own verification still applies.

## 7. Limits: what this does not protect against

- **A real customer could be locked out on purpose.** An attacker who types 5 wrong emails for someone's order
  blocks that order for up to 15 minutes. Real companies also track IP address and device, and offer another way
  to verify. We do not have that here.
- **It works for one running copy of the app.** The attempts are in a file and the call counter is in memory
  (it resets when the app restarts). A production system with many servers needs a shared store, such as a database.
- **The audit log is a plain file.** Anyone who can edit the file can change it. In production, logs go to
  protected storage that cannot be edited, with retention rules and alerts.
- **No automatic alerts.** Nobody is told when guessing happens. Production systems alert on patterns.
- **The log can grow forever.** There is no rotation or cleanup here.
- **Masking is not full anonymity.** `k***@example.com` still hints at the person.
- **On this laptop we are learning the mechanism,** not running a complete production setup.

## 8. If someone asks you to explain it

**Q: What is an ADK plugin, and why did you use one?**
A: It is a guard registered once on the app that applies to every tool, model call and message. Putting checks
inside each tool is easy to forget as the agent grows, and ADK's docs recommend plugins for security guardrails.

**Q: How do you stop someone guessing emails?**
A: Failed verifications are counted per order ID in a file, so opening a new chat does not reset the count. At 5
failures in 15 minutes the order is blocked, and the block ends by itself.

**Q: Isn't that a way to lock out a real customer?**
A: Yes, and I documented it. That is why the lock is temporary. Real systems also use IP and device checks and
another way to verify, which I did not build here.

**Q: What does the audit log contain?**
A: The time, which tool, which order, a masked email, and the result. Never the full email and never the customer's
free text, only its length.

**Q: Did your testing find anything about the model's behavior?**
A: Yes. With only a refund ID and no email, the model invented an email to fill a required tool field. The tool's own
check caught it, but the failed attempt was counted against a real order. I added a code check that every value passed to
a tool must appear in what the customer wrote, because a prompt alone cannot guarantee that.

**Q: What happens if your guard code has a bug?**
A: It fails closed: the tool call is blocked with a generic error, so a bug cannot become a way around the rules.

**Q: How much does it cost?**
A: Nothing. It is plain Python. It also saves model quota, because a too-long message is refused before it reaches the model.

**Q: Which OWASP risks does this cover?**
A: LLM10 (unbounded consumption: message length, tool-call flood, guessing), LLM02 (sensitive information
disclosure: masked and minimal logs), and LLM01 (prompt injection through fields: input validation).
