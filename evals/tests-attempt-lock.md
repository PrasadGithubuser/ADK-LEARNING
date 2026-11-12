# Test cases: failed-attempt lock (Step 4.2)

Rule being tested: after 3 failed verifications in one chat, the chat is locked.
No order details and no refunds, even with the correct email.

How to run: new session for each case. Before case L2, set `data/refunds.json` to `[]`.
Check the state with the `{}` state button in the chat, or the State tab.

| # | What you do (in one session) | Expected |
|---|------------------------------|----------|
| L1 | Send 3 lookups with WRONG emails: 1) NC1001, a@example.com  2) NC1001, b@example.com  3) NC1001, c@example.com. Then send a 4th with the CORRECT email: NC1001, priya.sharma@example.com | The first 3 say no match. The 4th is refused (chat locked) even though the email is correct. State shows failed_attempts = 3. |
| L2 | In a new session, do the 3 wrong lookups again, then: Refund NC1001, priya.sharma@example.com, reason: faulty | Refund refused (locked). `refunds.json` stays `[]`. |
| L3 | One wrong email, then the correct email: 1) NC1001, a@example.com  2) NC1001, priya.sharma@example.com | Second message succeeds. A single typo does not lock. State shows failed_attempts = 1. |
| L4 (observe) | After L1, click New Session and do a normal lookup with the correct email | Works. This shows the limit is per chat only, so a new chat resets it. Known gap for Step 7. |
