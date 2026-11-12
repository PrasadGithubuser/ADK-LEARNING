"""Checks the guardrail rules. No AI, no API calls, no ADK needed.

Run from the project root:   python evals\\check_guardrails.py
"""

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("guardrails", ROOT / "support_agent" / "guardrails.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

results = []


def record(ok, text, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + text + ("" if ok else f"   {detail}"))


GOOD = {"order_id": "NC1003", "customer_email": "anita.rao@example.com"}

print("Input validation")
record(g.validate_tool_args("get_order_status", GOOD) is None, "a normal order lookup is accepted")
record(g.validate_tool_args("get_order_status", {"order_id": " nc1009 ", "customer_email": "ANITA.RAO@EXAMPLE.COM"}) is None,
       "lower case ID, spaces and capital letters in the email are accepted")
for bad_id in ("NC12", "XX1001", "NC1001; DROP", "", None, "NC10011", "NC1001 NC1002"):
    record(g.validate_tool_args("get_order_status", {"order_id": bad_id, "customer_email": "a@example.com"}) is not None,
           f"order ID {bad_id!r} is rejected")
for bad_email in ("not-an-email", "a@b", "a b@example.com", "x@example.com' OR '1'='1", "", None,
                  "a" * 250 + "@example.com"):
    shown = bad_email if bad_email is None or len(bad_email) < 30 else bad_email[:12] + "..."
    record(g.validate_tool_args("get_order_status", {"order_id": "NC1003", "customer_email": bad_email}) is not None,
           f"email {shown!r} is rejected")
record(g.validate_tool_args("request_refund", {**GOOD, "reason": "x" * 501}) is not None, "a reason over 500 characters is rejected")
record(g.validate_tool_args("request_refund", {**GOOD, "reason": "x" * 500}) is None, "a reason of exactly 500 characters is accepted")
record(g.validate_tool_args("search_policies", {"query": "x" * 201}) is not None, "a search over 200 characters is rejected")
record(g.validate_tool_args("search_policies", {"query": "laptop warranty"}) is None, "a normal policy search is accepted")
record(g.validate_tool_args("some_other_tool", {}) is None, "tools the guard does not know are left alone")
record(g.message_too_long("x" * 2001) and not g.message_too_long("x" * 2000), "messages over 2000 characters are too long")

print("\nFailed attempts per order")
t0 = datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc)
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "attempts.json"
    tracker = g.AttemptTracker(path)
    for i in range(4):
        tracker.record_failure("NC1001", now=t0 + timedelta(minutes=i))
    record(not tracker.is_locked("NC1001", now=t0 + timedelta(minutes=4)), "4 failures do not lock an order")
    tracker.record_failure("NC1001", now=t0 + timedelta(minutes=4))
    record(tracker.is_locked("NC1001", now=t0 + timedelta(minutes=5)), "5 failures lock the order")
    record(not tracker.is_locked("NC1003", now=t0 + timedelta(minutes=5)), "other orders are not affected")
    record(tracker.is_locked(" nc1001 ", now=t0 + timedelta(minutes=5)), "the lock applies however the ID is written")
    record(g.AttemptTracker(path).is_locked("NC1001", now=t0 + timedelta(minutes=5)),
           "the lock survives a restart (a new tracker reads the same file)")
    record(not tracker.is_locked("NC1001", now=t0 + timedelta(minutes=16)),
           "the lock ends by itself after the 15-minute window")
    tracker.record_failure("NC1005", now=t0 + timedelta(minutes=40))
    kept = json.loads(path.read_text(encoding="utf-8"))
    record("NC1001" not in kept, "old failures are removed from the file")

    broken = Path(tmp) / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    safe = g.AttemptTracker(broken)
    record(not safe.is_locked("NC1001", now=t0), "a broken attempts file does not crash anything")
    safe.record_failure("NC1001", now=t0)
    record(g.AttemptTracker(broken).is_locked("NC1001", now=t0) is False and bool(json.loads(broken.read_text(encoding="utf-8"))),
           "a broken attempts file is replaced with a valid one")

print("\nPrivacy of the audit log")
record(g.mask_email("karan.mehta@example.com") == "k***@example.com", "emails are masked")
record(g.mask_email("nonsense") == "***" and g.mask_email(None) == "***", "strange email values are masked too")
event = g.build_audit_event(
    "tool_call", "request_refund",
    {"order_id": "nc1005", "customer_email": "karan.mehta@example.com", "reason": "my secret words here"},
    session_id="abcdef123456",
)
text = json.dumps(event)
record("karan.mehta" not in text, "an audit event never contains the full email")
record("secret" not in text and event["reason_length"] == 20, "an audit event never contains the reason text, only its length")
record(event["order_id"] == "NC1005" and event["session"] == "abcdef12", "the order ID is kept and the session ID is shortened")
with tempfile.TemporaryDirectory() as tmp:
    log = g.AuditLog(Path(tmp) / "audit.log")
    log.write(event)
    log.write({"event": "second"})
    lines = (Path(tmp) / "audit.log").read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in lines]
    record(len(parsed) == 2 and "time" in parsed[0], "the audit log gets one JSON line per event, with a time")
    unwritable = g.AuditLog(Path(tmp) / "missing-folder" / "audit.log")
    try:
        unwritable.write(event)
        ok = True
    except Exception:
        ok = False
    record(ok, "a log that cannot be written never crashes the agent")

print("\nTool call counter")
counter = g.ToolCallCounter()
allowed = [counter.allow("req-1") for _ in range(7)]
record(allowed == [True] * 6 + [False], "6 tool calls are allowed for one message, the 7th is not")
record(counter.allow("req-2"), "a different message starts from zero")
small = g.ToolCallCounter(max_tracked=100)
for i in range(5000):
    small.allow(f"req-{i}")
record(len(small._counts) <= 100, "memory stays small after thousands of requests")

print("\nValues must come from the customer")
said = "Where is my order NC1003? My email is Anita.Rao@Example.com"
record(g.check_grounded("get_order_status", GOOD, said) is None, "an order ID and email the customer typed are accepted (capital letters do not matter)")
record(g.check_grounded("get_order_status", {"order_id": "NC1003", "customer_email": "someone.else@example.com"}, said) is not None,
       "an email the customer never typed is blocked")
record(g.check_grounded("get_order_status", {"order_id": "NC1009", "customer_email": "anita.rao@example.com"}, said) is not None,
       "an order ID the customer never typed is blocked")
record(g.check_grounded("get_refund_status", {"order_id": "NC1005", "customer_email": "made.up@example.com"}, "What is the status of RF-NC1005-001?") is not None,
       "a refund ID alone does not allow an invented email")
record(g.check_grounded("get_refund_status", {"order_id": "NC1005", "customer_email": "karan.mehta@example.com"},
                        "status of RF-NC1005-001, karan.mehta@example.com") is None,
       "an order ID inside a refund ID is accepted when the email was given")
record(g.check_grounded("get_order_status", {"order_id": "NC1009", "customer_email": "a@example.com"}, "order nc-1009 email a@example.com") is None,
       "an order ID written as nc-1009 is accepted")
record(g.check_grounded("search_policies", {"query": "warranty"}, "anything") is None, "tools without an order ID are not checked")
record(g.check_grounded("get_order_status", GOOD, "") is not None, "with no customer text at all, an order lookup is not grounded")

print("\nWiring")
agent_source = (ROOT / "support_agent" / "agent.py").read_text(encoding="utf-8")
record("plugins=[NovaCartGuardrails(" in agent_source and "app = App(" in agent_source,
       "agent.py registers the guardrails plugin on the App")
record(agent_source.count("NOT_FOUND_RESPONSE") >= 4, "all three tools return the NOT_FOUND error code")

print()
print(f"{sum(results)} of {len(results)} checks passed")
sys.exit(0 if all(results) else 1)