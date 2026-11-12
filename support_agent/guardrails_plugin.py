"""ADK plugin: the security guard at the building entrance.

A plugin is registered once on the App and applies to every agent and every tool.
It only decides WHEN to check. The checks themselves are in guardrails.py (plain Python).
See docs/05-guardrails.md.
"""

import logging
from typing import Any, Optional

from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from . import guardrails

logger = logging.getLogger(__name__)

TOO_LONG_REPLY = (
    "Your message is too long for me to handle. Please send a shorter message "
    "with just the details I need."
)


def _message_text(content) -> str:
    """All the text in a message, or an empty string if there is none."""
    parts = getattr(content, "parts", None) or []
    return "".join(getattr(part, "text", None) or "" for part in parts)


def _error(code: str, message: str) -> dict:
    return {"status": "error", "error_code": code, "error_message": message}


class NovaCartGuardrails(BasePlugin):
    def __init__(self, audit_file, attempts_file):
        super().__init__(name="novacart_guardrails")
        self.audit = guardrails.AuditLog(audit_file)
        self.tracker = guardrails.AttemptTracker(attempts_file)
        self.counter = guardrails.ToolCallCounter()

    # Door 1: a customer message arrives, before anything else happens
    async def before_run_callback(self, *, invocation_context: InvocationContext) -> Optional[types.Content]:
        try:
            text = _message_text(getattr(invocation_context, "user_content", None))
            if guardrails.message_too_long(text):
                self.audit.write({"event": "message_refused", "reason": "too_long", "length": len(text)})
                return types.Content(role="model", parts=[types.Part(text=TOO_LONG_REPLY)])
        except Exception:
            # A failure here only means a long message gets through. It is not a security decision.
            logger.exception("Message length check failed")
        return None

    # Door 2: just before a tool runs
    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> Optional[dict]:
        session_id = _safe(lambda: tool_context.session.id)
        try:
            name = tool.name
            args = tool_args or {}

            # one message must not cause a flood of tool calls
            request_id = _safe(lambda: tool_context.invocation_id) or session_id or "unknown"
            if not self.counter.allow(request_id):
                self.audit.write(guardrails.build_audit_event(
                    "tool_blocked", name, args, session_id, reason="too_many_tool_calls"))
                return _error("TOO_MANY_REQUESTS", "Too many requests in one message. Ask for fewer items at a time.")

            # bad input is stopped here, before the tool ever sees it
            problem = guardrails.validate_tool_args(name, args)
            if problem:
                self.audit.write(guardrails.build_audit_event(
                    "tool_blocked", name, args, session_id, reason="invalid_input"))
                return _error("INVALID_INPUT", problem)

            # the order ID and email must come from what the customer wrote, not from the model's imagination
            customer_text = _customer_text(tool_context)
            if customer_text:
                invented = guardrails.check_grounded(name, args, customer_text)
                if invented:
                    self.audit.write(guardrails.build_audit_event(
                        "tool_blocked", name, args, session_id, reason="value_not_from_customer"))
                    return _error("INVALID_INPUT", invented)
            elif name in guardrails.ORDER_TOOLS:
                # We could not read the customer's messages. The tool's own check still protects the data.
                self.audit.write(guardrails.build_audit_event(
                    "grounding_skipped", name, args, session_id))

            # an order with too many recent failed verifications is blocked, even in a new chat
            if name in guardrails.ORDER_TOOLS and self.tracker.is_locked(args.get("order_id")):
                self.audit.write(guardrails.build_audit_event(
                    "tool_blocked", name, args, session_id, reason="order_temporarily_locked"))
                return _error("TEMPORARILY_LOCKED",
                              "Too many failed verification attempts for this order. Please try again later.")

            self.audit.write(guardrails.build_audit_event("tool_call", name, args, session_id))
            return None  # all clear: let the tool run
        except Exception:
            # A guard that fails must not let the request through: fail closed.
            logger.exception("Guardrail check failed")
            return _error("GUARDRAIL_ERROR", "This request could not be checked right now. Please try again later.")

    # Door 3: just after a tool ran
    async def after_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext, result: Any
    ) -> Optional[dict]:
        try:
            if not isinstance(result, dict):
                return None
            session_id = _safe(lambda: tool_context.session.id)
            args = tool_args or {}

            # a failed verification counts against that ORDER, whichever chat it came from
            if result.get("error_code") == "NOT_FOUND" and args.get("order_id"):
                self.tracker.record_failure(args["order_id"])

            summary = {"status": result.get("status")}
            for key in ("error_code", "outcome", "refund_id", "amount_inr"):
                if key in result:
                    summary[key] = result[key]
            self.audit.write(guardrails.build_audit_event("tool_result", tool.name, args, session_id, **summary))
        except Exception:
            logger.exception("Audit step failed")
        return None


def _customer_text(tool_context) -> str:
    """Everything the customer has typed in this chat so far."""
    texts = []
    try:
        for event in tool_context.session.events or []:
            if getattr(event, "author", None) == "user":
                texts.append(_message_text(getattr(event, "content", None)))
    except Exception:
        pass
    try:
        texts.append(_message_text(tool_context.user_content))
    except Exception:
        pass
    return "\n".join(t for t in texts if t)


def _safe(getter):
    try:
        return getter()
    except Exception:
        return None