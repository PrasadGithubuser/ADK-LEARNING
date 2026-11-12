"""Guardrail rules for the NovaCart support agent.

Plain Python: no AI, no ADK, no network, no paid services. Easy to test on its own.
The ADK plugin (guardrails_plugin.py) only calls these functions at the right moments.
See docs/05-guardrails.md for the reasoning.
"""

import json
import logging
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits (change them here)
# ---------------------------------------------------------------------------
ORDER_ID_PATTERN = re.compile(r"NC\d{4}")
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MAX_EMAIL_LENGTH = 254
MAX_REASON_LENGTH = 500
MAX_QUERY_LENGTH = 200
MAX_MESSAGE_LENGTH = 2000          # characters in one customer message
MAX_FAILED_PER_ORDER = 5           # failed verifications allowed for one order ...
WINDOW_MINUTES = 15                # ... within this many minutes
MAX_TOOL_CALLS_PER_REQUEST = 6     # tool calls allowed for one customer message

ORDER_TOOLS = {"get_order_status", "request_refund", "get_refund_status"}


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def normalize_order_id(value) -> str:
    return str(value or "").strip().upper()


def validate_tool_args(tool_name: str, args: Optional[dict]) -> Optional[str]:
    """Return an error message if the tool's input is not acceptable, otherwise None."""
    args = args or {}
    if tool_name in ORDER_TOOLS:
        if not ORDER_ID_PATTERN.fullmatch(normalize_order_id(args.get("order_id"))):
            return "The order ID is not valid. Order IDs look like NC1234."
        email = str(args.get("customer_email") or "").strip()
        if len(email) > MAX_EMAIL_LENGTH or not EMAIL_PATTERN.fullmatch(email):
            return "The email address is not valid."
    if tool_name == "request_refund":
        if len(str(args.get("reason") or "")) > MAX_REASON_LENGTH:
            return f"The reason is too long. Please keep it under {MAX_REASON_LENGTH} characters."
    if tool_name == "search_policies":
        if len(str(args.get("query") or "")) > MAX_QUERY_LENGTH:
            return "The search is too long."
    return None


def check_grounded(tool_name: str, args: Optional[dict], customer_text: str) -> Optional[str]:
    """The order ID and the email passed to a tool must come from what the CUSTOMER wrote.

    The model sometimes invents a value to fill a required field (for example an email nobody typed).
    Returns an error message if a value did not come from the customer, otherwise None.
    """
    if tool_name not in ORDER_TOOLS:
        return None
    args = args or {}
    squashed = re.sub(r"[^A-Za-z0-9]", "", customer_text or "").upper()   # "nc-1009" and "RF-NC1005-001" still contain the ID
    if normalize_order_id(args.get("order_id")) not in squashed:
        return "The customer has not given this order ID. Ask the customer for it."
    email = str(args.get("customer_email") or "").strip().lower()
    if email not in (customer_text or "").lower():
        return "The customer has not given this email address. Ask the customer for it."
    return None


def message_too_long(text: str) -> bool:
    return len(text or "") > MAX_MESSAGE_LENGTH


# ---------------------------------------------------------------------------
# Privacy helpers
# ---------------------------------------------------------------------------

def mask_email(value) -> str:
    """karan.mehta@example.com -> k***@example.com"""
    text = str(value or "").strip()
    if "@" not in text:
        return "***"
    local, _, domain = text.rpartition("@")
    return f"{local[:1]}***@{domain[:60]}"


def build_audit_event(kind: str, tool_name: str, args: Optional[dict], session_id=None, **extra) -> dict:
    """Build one audit log entry. Free text from customers is never copied in, only its length."""
    args = args or {}
    event = {"event": kind, "tool": tool_name}
    if session_id:
        event["session"] = str(session_id)[:8]
    if "order_id" in args:
        event["order_id"] = normalize_order_id(args["order_id"])[:12]
    if "customer_email" in args:
        event["email"] = mask_email(args["customer_email"])
    if "reason" in args:
        event["reason_length"] = len(str(args["reason"]))
    if "query" in args:
        event["query_length"] = len(str(args["query"]))
    event.update(extra)
    return event


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class AuditLog:
    """Appends one JSON line per event. A failure to write never breaks the agent."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, event: dict) -> None:
        record = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), **event}
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("Could not write to the audit log at %s", self.path)


# ---------------------------------------------------------------------------
# Failed-attempt limit per order (works across chats)
# ---------------------------------------------------------------------------

class AttemptTracker:
    """Counts failed verifications per ORDER ID, in a file, so a new chat does not reset the count.

    An order is blocked while it has MAX_FAILED_PER_ORDER failures inside the time window.
    Old failures drop out of the window by themselves, so a block is temporary.
    """

    def __init__(self, path, max_failures: int = MAX_FAILED_PER_ORDER, window_minutes: int = WINDOW_MINUTES):
        self.path = Path(path)
        self.max_failures = max_failures
        self.window = timedelta(minutes=window_minutes)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, list)}

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            logger.warning("Could not save the attempts file at %s", self.path)

    def _recent(self, stamps: list, now: datetime) -> list:
        recent = []
        for stamp in stamps:
            try:
                when = datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                continue
            if when > now - self.window:
                recent.append(stamp)
        return recent

    def is_locked(self, order_id: str, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            stamps = self._load().get(normalize_order_id(order_id), [])
            return len(self._recent(stamps, now)) >= self.max_failures

    def record_failure(self, order_id: str, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        key = normalize_order_id(order_id)
        with self._lock:
            data = self._load()
            # keep the file small: drop expired entries for every order
            data = {k: self._recent(v, now) for k, v in data.items()}
            data = {k: v for k, v in data.items() if v}
            data.setdefault(key, []).append(now.isoformat())
            self._save(data)
            return len(data[key])


# ---------------------------------------------------------------------------
# Tool-call counter (one customer message must not trigger a flood of tool calls)
# ---------------------------------------------------------------------------

class ToolCallCounter:
    def __init__(self, limit: int = MAX_TOOL_CALLS_PER_REQUEST, max_tracked: int = 1000):
        self.limit = limit
        self.max_tracked = max_tracked
        self._counts = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, request_id) -> bool:
        """Count one tool call for this request. False means the limit was passed."""
        key = str(request_id)
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            self._counts.move_to_end(key)
            while len(self._counts) > self.max_tracked:
                self._counts.popitem(last=False)  # forget the oldest requests so memory stays small
            return self._counts[key] <= self.limit