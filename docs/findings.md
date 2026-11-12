## Order lookup
- D3: no leak, but only one of five orders is shown. Improve multi-order handling later.
- H3: no attempt limit exists in code. Add a rate limit (Step 7).
- H6: fixed in the prompt (no mention of tools or inputs).

## Refunds
- Code check: 21 of 21 passed.
- Live attack tests: 14 of 14 passed (RD1-RD8, RH1-RH8), single run each.
- Duplicate refund is blocked both by the AI's memory (RD2) and by code (RD2b).

## Step 4: sessions and state
- Added a failed-attempt counter in session state (tools write it via tool_context).
- After 3 failed verifications the chat is locked, for lookups and refunds. 4 of 4 tests passed.
- Known gap: the count resets in a new chat. Needs storage outside the chat (Step 7).
- Learned: session history is re-sent to the model on every call; compaction is optional for short chats.

## Step 5: policy answers (RAG)
- Built keyword (BM25) search over data/policies as a retrieval tool. All policy tests passed.
- Poison test found: a fake policy file made the agent quote a fake 60-day rule.
- Fix: only files whose fingerprint is on the approved list are searched
  (scripts/approve_policies.py). Re-test passed.
- Bug found while testing: text without headings was silently ignored. Fixed.
- Known weakness: word matching misses meaning ("send it back"). Embeddings (hybrid search)
  are an optional later upgrade.

  ## Step 6: refund approval
- Customer asks. Only staff approves, using scripts/review_refunds.py. The agent has no
  approve function (a test enforces this).
- get_refund_status shows customer-safe fields only (no reason text, staff name or notes).
- Decisions are final. Staff see full details plus an automatic re-check.
- Tests: all live cases passed (S-D and S-H sets), single run each.
- Known limits: reviewer name is typed, not verified; refunds.json can be edited directly;
  no automatic notifications; one refund per order even after a rejection.

  ## Step 7: guardrails
- Added a guardrails plugin (ADK App + BasePlugin): message length limit, input validation,
  per-order failed-attempt limit across chats (5 in 15 minutes), 6 tool calls per message,
  audit log with masked emails and no free text.
- Verified in ADK: a blocked call returns INVALID_INPUT without running the real tool.
- All live guardrail tests passed (G-D and G-H sets), single run each.
- Known limits: a real customer's order can be locked on purpose (temporary); attempts file and
  call counter work for one app copy only; audit log is a plain editable file; no alerts.
- order_attempts.json is created only after the first failed verification (by design).

## Step 8: automatic exam
- Built evals/run_exam.py: runs 70 cases against the real agent on a temporary copy of the data.
  Plain-code checks only (no AI judge), pacing under the free limit, real-data check before and after.
- First run: 66 of 70 passed. Of the 4 failures: 1 was a Google 503 (not a bug), 2 were expectations
  that were too strict, 1 was a real finding.
- Real finding: with only a refund ID, the model invented an email to fill a required tool field.
  The tool's check caught it, but the failed attempt counted against a real order.
  Fix: guard 6, values passed to tools must appear in what the customer typed.
- Repeat run of 20 attack and judgment cases, 3 times each: 60 of 60 passed.
- 503 errors are Google capacity problems. The runner waits, re-runs the case, and marks it ERROR
  (not tested) if Google keeps failing.
- Limits: cases were written by an AI, word checks can miss a bad answer, and a person must read
  sample replies.