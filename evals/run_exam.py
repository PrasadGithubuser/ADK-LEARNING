"""Automatic exam for the NovaCart support agent.

What it does:
  1. Makes a TEMPORARY copy of the data (orders, policies, an empty refund list) for every test.
  2. Types each test message to the real agent (Gemini), exactly like a customer would.
  3. Checks the result with PLAIN CODE: tools used, words that must or must not appear, files written.
     No AI judges the answers.
  4. Prints a scorecard and saves a report you can read.

Your real data/ folder is never touched. The runner checks that before and after.

Run from the project root (do NOT use adk web at the same time: both share your free quota):
    python evals\\run_exam.py --list                       show all cases
    python evals\\run_exam.py --dry-run                    check the case files, no model calls
    python evals\\run_exam.py --only O-1 RF-1              run just these cases
    python evals\\run_exam.py --group refund               run one group
    python evals\\run_exam.py                              run everything
    python evals\\run_exam.py --repeat 3 --group order     run each case 3 times (pass rate)

See docs/06-exam-runner.md.
"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL_DATA = ROOT / "data"
CASES_DIR = ROOT / "evals" / "cases"
FIXTURES = ROOT / "evals" / "fixtures"
RESULTS_DIR = ROOT / "evals" / "results"

PINNED_TODAY = "2026-10-31"
USER_ID = "exam_user"
EST_REQUESTS_PER_TURN = 3      # a typical turn is 2 to 3 model requests (tool decision + answer)
TURN_TIMEOUT_SECONDS = 120
QUOTA_RETRIES = 3
QUOTA_WAIT_SECONDS = 65
SERVICE_RETRIES = 3            # how many times a case is run again after a Google service problem
SERVICE_WAIT_SECONDS = 45
TRANSIENT_MARKERS = ("503", "unavailable", "high demand", "429", "resource_exhausted", "quota",
                     "timed out", "overloaded", "deadline")
BLOCK_CODES = {"TOO_MANY_REQUESTS", "INVALID_INPUT", "TEMPORARILY_LOCKED", "GUARDRAIL_ERROR"}

CASE_KEYS = {"id", "title", "group", "seed_refunds", "extra_files", "turns", "expect_final"}
TURN_KEYS = {"user", "user_repeat", "expect", "new_session", "staff"}
EXPECT_KEYS = {
    "reply_contains_all", "reply_contains_any", "reply_contains_each_any", "reply_not_contains",
    "reply_not_exact", "reply_max_chars", "reply_numbers_from_policy",
    "tools_called", "tools_used_include", "tools_allowed_only", "tools_called_at_most_total",
    "executed_at_most", "executed_tools_not_include", "result_codes_include", "result_codes_exclude",
}
FINAL_KEYS = {
    "refund_count", "refund_count_at_most", "refund_outcomes", "refund_outcomes_forbidden", "refund_amounts",
    "audit_contains", "audit_not_contains", "attempts_has_order", "attempts_lacks_order",
}


# ---------------------------------------------------------------------------
# Seed data for cases
# ---------------------------------------------------------------------------

def _pending_nc1005() -> dict:
    return {
        "refund_id": "RF-NC1005-001", "order_id": "NC1005", "customer_email": "karan.mehta@example.com",
        "amount_inr": 68000, "items": ["Laptop 14-inch"], "outcome": "PENDING_HUMAN_APPROVAL",
        "reason": "screen cracked", "created_at": "2026-10-31T09:00:00+00:00",
    }


def seed_refund(name: str) -> dict:
    if name == "pending_NC1005":
        return _pending_nc1005()
    if name == "approved_NC1005":
        r = _pending_nc1005()
        r.update(outcome="APPROVED_BY_STAFF", reviewed_by="Asha Verma",
                 reviewed_at="2026-10-31T10:00:00+00:00", review_note="internal-note-xyz")
        return r
    if name == "rejected_NC1005":
        r = _pending_nc1005()
        r.update(outcome="REJECTED_BY_STAFF", reviewed_by="Asha Verma",
                 reviewed_at="2026-10-31T10:00:00+00:00", review_note="outside policy internal-note-xyz")
        return r
    if name == "auto_NC1001":
        return {
            "refund_id": "RF-NC1001-001", "order_id": "NC1001", "customer_email": "priya.sharma@example.com",
            "amount_inr": 2499, "items": ["Wireless Earbuds"], "outcome": "APPROVED_AUTO",
            "reason": "faulty", "created_at": "2026-10-31T08:00:00+00:00",
        }
    raise ValueError(f"Unknown seed refund {name!r}")


SEED_NAMES = {"pending_NC1005", "approved_NC1005", "rejected_NC1005", "auto_NC1001"}


# ---------------------------------------------------------------------------
# Loading and validating cases
# ---------------------------------------------------------------------------

def load_cases() -> list:
    cases = []
    for path in sorted(CASES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise SystemExit(f"{path.name}: not valid JSON ({error})")
        for case in data:
            case["_file"] = path.name
            cases.append(case)
    return cases


def validate_case(case: dict) -> list:
    """Return a list of problems (typos in keys, unknown names). Empty list means fine."""
    problems = []
    cid = case.get("id", "?")
    for key in case:
        if key not in CASE_KEYS and key != "_file":
            problems.append(f"{cid}: unknown case key {key!r}")
    for key in ("id", "title", "group", "turns"):
        if key not in case:
            problems.append(f"{cid}: missing {key!r}")
    for name in case.get("seed_refunds", []):
        if name not in SEED_NAMES:
            problems.append(f"{cid}: unknown seed refund {name!r}")
    for rel, src in case.get("extra_files", {}).items():
        if not rel.startswith("policies/") or not src.startswith("@"):
            problems.append(f"{cid}: extra file {rel!r} must be under policies/ and come from @fixture")
        elif not (FIXTURES / src[1:]).exists():
            problems.append(f"{cid}: fixture {src!r} does not exist")
    for i, turn in enumerate(case.get("turns", []), 1):
        for key in turn:
            if key not in TURN_KEYS:
                problems.append(f"{cid} turn {i}: unknown key {key!r}")
        for key in turn.get("expect", {}):
            if key not in EXPECT_KEYS:
                problems.append(f"{cid} turn {i}: unknown expect key {key!r}")
        if not any(k in turn for k in ("user", "user_repeat", "new_session", "staff")):
            problems.append(f"{cid} turn {i}: needs user, user_repeat, new_session or staff")
    for key in case.get("expect_final", {}):
        if key not in FINAL_KEYS:
            problems.append(f"{cid}: unknown expect_final key {key!r}")
    return problems


def turn_message(turn: dict) -> str:
    if "user_repeat" in turn:
        return turn["user_repeat"]["text"] * turn["user_repeat"]["times"]
    return turn["user"]


def count_user_turns(case: dict) -> int:
    return sum(1 for t in case["turns"] if "user" in t or "user_repeat" in t)


# ---------------------------------------------------------------------------
# Checking results (plain code, no AI)
# ---------------------------------------------------------------------------

class TurnResult:
    def __init__(self):
        self.reply = ""
        self.calls = []        # tools the model asked for: {"name", "args"}
        self.results = []      # what came back: {"name", "response"}
        self.model_events = 0
        self.errors = []

    def codes(self) -> set:
        found = set()
        for r in self.results:
            for key in ("error_code", "outcome", "status"):
                if isinstance(r["response"].get(key), str):
                    found.add(r["response"][key])
        return found

    def executed(self) -> list:
        """Tool calls that really ran (not stopped by a guardrail)."""
        return [r for r in self.results if r["response"].get("error_code") not in BLOCK_CODES]


def is_service_problem(errors: list) -> bool:
    """True when the error is a Google service problem (busy, unavailable, quota), not a test failure."""
    text = " ".join(errors).lower()
    return any(marker in text for marker in TRANSIENT_MARKERS)


def numbers_in(text: str) -> set:
    found = set()
    for chunk in re.findall(r"\d[\d,]*", text):
        digits = chunk.replace(",", "")
        if digits.isdigit():
            found.add(int(digits))
    return found


def policy_numbers(data_dir: Path) -> set:
    found = set(range(0, 11))
    for path in (data_dir / "policies").glob("*.md"):
        if path.name.startswith("poisoned"):
            continue
        found |= numbers_in(path.read_text(encoding="utf-8"))
    return found


def check_turn(expect: dict, tr: TurnResult, user_text: str, allowed_numbers: set) -> list:
    fails = []
    reply = tr.reply.lower()
    stripped = reply.strip()

    if not tr.reply and not expect.get("reply_max_chars") == 0:
        fails.append("the reply was empty")
    for error in tr.errors:
        fails.append(f"ADK reported an error: {error}")

    for text in expect.get("reply_contains_all", []):
        if text.lower() not in reply:
            fails.append(f"reply is missing {text!r}")
    any_list = expect.get("reply_contains_any")
    if any_list and not any(t.lower() in reply for t in any_list):
        fails.append(f"reply has none of {any_list}")
    for group in expect.get("reply_contains_each_any", []):
        if not any(t.lower() in reply for t in group):
            fails.append(f"reply has none of {group}")
    for text in expect.get("reply_not_contains", []):
        if text.lower() in reply:
            fails.append(f"reply contains forbidden text {text!r}")
    for text in expect.get("reply_not_exact", []):
        if stripped == text.lower():
            fails.append(f"reply is only {text!r}")
    if "reply_max_chars" in expect and len(tr.reply) > expect["reply_max_chars"]:
        fails.append(f"reply is {len(tr.reply)} characters, limit {expect['reply_max_chars']}")
    if expect.get("reply_numbers_from_policy"):
        extra = sorted(n for n in numbers_in(tr.reply) if n not in allowed_numbers | numbers_in(user_text))
        if extra:
            fails.append(f"reply has numbers that are not in the policy documents: {extra}")

    counts = Counter(c["name"] for c in tr.calls)
    if "tools_called" in expect:
        wanted = expect["tools_called"]
        for name, n in wanted.items():
            if counts.get(name, 0) != n:
                fails.append(f"tool {name} was requested {counts.get(name, 0)} time(s), expected {n}")
        for name in counts:
            if name not in wanted:
                fails.append(f"tool {name} was requested but not expected")
    for name in expect.get("tools_used_include", []):
        if counts.get(name, 0) < 1:
            fails.append(f"tool {name} was never used")
    if "tools_allowed_only" in expect:
        for name in counts:
            if name not in expect["tools_allowed_only"]:
                fails.append(f"tool {name} was used but is not allowed in this case")
    if "tools_called_at_most_total" in expect and len(tr.calls) > expect["tools_called_at_most_total"]:
        fails.append(f"{len(tr.calls)} tool calls were requested, limit {expect['tools_called_at_most_total']}")
    if "executed_at_most" in expect and len(tr.executed()) > expect["executed_at_most"]:
        fails.append(f"{len(tr.executed())} tool calls actually ran, limit {expect['executed_at_most']}")
    ran_names = {r["name"] for r in tr.executed()}
    for name in expect.get("executed_tools_not_include", []):
        if name in ran_names:
            fails.append(f"tool {name} really ran, but it must not")
    codes = tr.codes()
    for code in expect.get("result_codes_include", []):
        if code not in codes:
            fails.append(f"no tool result had the code {code}")
    for code in expect.get("result_codes_exclude", []):
        if code in codes:
            fails.append(f"a tool result had the forbidden code {code}")
    return fails


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def check_final(expect: dict, data_dir: Path) -> list:
    fails = []
    refunds = read_json(data_dir / "refunds.json", [])
    by_id = {r.get("refund_id"): r for r in refunds}
    audit = ""
    try:
        audit = (data_dir / "audit.log").read_text(encoding="utf-8")
    except OSError:
        pass
    attempts = read_json(data_dir / "order_attempts.json", {})

    if "refund_count" in expect and len(refunds) != expect["refund_count"]:
        fails.append(f"refunds.json has {len(refunds)} record(s), expected {expect['refund_count']}")
    if "refund_count_at_most" in expect and len(refunds) > expect["refund_count_at_most"]:
        fails.append(f"refunds.json has {len(refunds)} record(s), expected at most {expect['refund_count_at_most']}")
    for rid, outcome in expect.get("refund_outcomes", {}).items():
        got = by_id.get(rid, {}).get("outcome", "(no such refund)")
        if got != outcome:
            fails.append(f"refund {rid} is {got}, expected {outcome}")
    for outcome in expect.get("refund_outcomes_forbidden", []):
        if any(r.get("outcome") == outcome for r in refunds):
            fails.append(f"a refund has the forbidden outcome {outcome}")
    for rid, amount in expect.get("refund_amounts", {}).items():
        got = by_id.get(rid, {}).get("amount_inr")
        if got != amount:
            fails.append(f"refund {rid} amount is {got}, expected {amount}")
    for text in expect.get("audit_contains", []):
        if text not in audit:
            fails.append(f"audit.log does not contain {text!r}")
    for text in expect.get("audit_not_contains", []):
        if text.lower() in audit.lower():
            fails.append(f"audit.log contains {text!r}, which must never be logged")
    for oid in expect.get("attempts_has_order", []):
        if oid not in attempts:
            fails.append(f"order_attempts.json has no failures recorded for {oid}")
    for oid in expect.get("attempts_lacks_order", []):
        if oid in attempts:
            fails.append(f"order_attempts.json has failures recorded for {oid}, which it should not")
    return fails


# ---------------------------------------------------------------------------
# Pacing (stay under the free-tier limit)
# ---------------------------------------------------------------------------

class QuotaExhausted(Exception):
    pass


class Pacer:
    def __init__(self, requests_per_minute: int):
        self.rpm = requests_per_minute
        self.events = deque()   # (time, number of requests)

    def _prune(self, now: float):
        while self.events and now - self.events[0][0] > 60:
            self.events.popleft()

    def used(self) -> int:
        return sum(n for _, n in self.events)

    async def wait_for_room(self, needed: int):
        while True:
            now = time.monotonic()
            self._prune(now)
            if not self.events or self.used() + needed <= self.rpm:
                return
            wait = 60 - (now - self.events[0][0]) + 0.5
            print(f"      pausing {wait:.0f}s to stay under {self.rpm} requests per minute ...")
            await asyncio.sleep(max(wait, 1.0))

    def record(self, n: int):
        self.events.append((time.monotonic(), n))


def is_quota_error(error: Exception) -> bool:
    text = str(error).lower()
    return "429" in text or "resource_exhausted" in text or "quota" in text or "rate limit" in text


# ---------------------------------------------------------------------------
# The exam
# ---------------------------------------------------------------------------

class Exam:
    def __init__(self, runner, app_name: str, data_dir: Path, review_module, pacer: Pacer, make_message):
        self.runner = runner
        self.app_name = app_name
        self.data_dir = Path(data_dir)
        self.review = review_module
        self.pacer = pacer
        self.make_message = make_message
        self.requests_used = 0

    # --- temporary data ----------------------------------------------------
    def reset_data(self, case: dict):
        for item in list(self.data_dir.iterdir()):
            shutil.rmtree(item) if item.is_dir() else item.unlink()
        shutil.copy(REAL_DATA / "orders.json", self.data_dir / "orders.json")
        shutil.copytree(REAL_DATA / "policies", self.data_dir / "policies")
        shutil.copy(REAL_DATA / "policy_approvals.json", self.data_dir / "policy_approvals.json")
        refunds = [seed_refund(name) for name in case.get("seed_refunds", [])]
        (self.data_dir / "refunds.json").write_text(json.dumps(refunds, indent=2), encoding="utf-8")
        for rel, src in case.get("extra_files", {}).items():
            shutil.copy(FIXTURES / src[1:], self.data_dir / rel)

    # --- one customer message ---------------------------------------------
    async def _send(self, session_id: str, text: str) -> TurnResult:
        tr = TurnResult()
        message = self.make_message(text)
        async for event in self.runner.run_async(user_id=USER_ID, session_id=session_id, new_message=message):
            if getattr(event, "author", "") == "user":
                continue
            is_error = getattr(event, "is_error", None)
            if callable(is_error) and is_error():
                tr.errors.append(str(getattr(event, "error_details", "unknown error")))
            calls = event.get_function_calls() or []
            results = event.get_function_responses() or []
            for call in calls:
                tr.calls.append({"name": call.name, "args": dict(call.args or {})})
            for res in results:
                response = res.response if isinstance(res.response, dict) else {"value": str(res.response)}
                tr.results.append({"name": res.name, "response": response})
            if calls:
                tr.model_events += 1
            if event.is_final_response() and event.content and event.content.parts:
                text_out = "".join(getattr(p, "text", None) or "" for p in event.content.parts).strip()
                if text_out:
                    tr.reply = (tr.reply + "\n" + text_out).strip()
                    tr.model_events += 1
        return tr

    async def send_with_retry(self, session_id: str, text: str) -> TurnResult:
        for attempt in range(1, QUOTA_RETRIES + 1):
            await self.pacer.wait_for_room(EST_REQUESTS_PER_TURN)
            try:
                tr = await asyncio.wait_for(self._send(session_id, text), TURN_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                tr = TurnResult()
                tr.errors.append(f"timed out after {TURN_TIMEOUT_SECONDS}s")
                return tr
            except Exception as error:  # noqa: BLE001
                if is_quota_error(error):
                    print(f"      quota error (attempt {attempt}/{QUOTA_RETRIES}), waiting {QUOTA_WAIT_SECONDS}s ...")
                    self.pacer.record(self.pacer.rpm)  # treat the minute as full
                    if attempt == QUOTA_RETRIES:
                        raise QuotaExhausted(str(error))
                    await asyncio.sleep(QUOTA_WAIT_SECONDS)
                    continue
                tr = TurnResult()
                tr.errors.append(f"{type(error).__name__}: {str(error)[:200]}")
                return tr
            used = max(1, tr.model_events)
            self.pacer.record(used)
            self.requests_used += used
            return tr
        raise QuotaExhausted("retries used up")

    async def new_session(self, label: str) -> str:
        session_id = f"{label}-{random.randrange(10**8):08d}"
        await self.runner.session_service.create_session(
            app_name=self.app_name, user_id=USER_ID, session_id=session_id)
        return session_id

    # --- one case, one repetition ------------------------------------------
    async def run_case_once(self, case: dict, rep: int) -> dict:
        self.reset_data(case)
        allowed_numbers = policy_numbers(self.data_dir)
        session_id = await self.new_session(f"{case['id']}-r{rep}")
        transcript, failures = [], []

        for number, turn in enumerate(case["turns"], 1):
            if turn.get("new_session"):
                session_id = await self.new_session(f"{case['id']}-r{rep}")
                transcript.append({"note": "new session"})
                continue
            if "staff" in turn:
                s = turn["staff"]
                path = self.data_dir / "refunds.json"
                refunds = read_json(path, [])
                try:
                    updated = self.review.decide(
                        refunds, s["refund_id"], s["action"], s.get("reviewer", "Exam Staff"),
                        s.get("note", ""), datetime.now(timezone.utc).isoformat())
                    self.review.save_refunds(path, updated)
                    transcript.append({"note": f"staff {s['action']} {s['refund_id']}"})
                except ValueError as error:
                    failures.append(f"turn {number}: staff step failed: {error}")
                continue

            text = turn_message(turn)
            tr = await self.send_with_retry(session_id, text)
            if is_service_problem(tr.errors):
                return {"passed": False, "infra_error": True, "transcript": transcript,
                        "failures": [f"turn {number}: Google service problem: {tr.errors[0][:120]}"]}
            problems = check_turn(turn.get("expect", {}), tr, text, allowed_numbers)
            failures += [f"turn {number}: {p}" for p in problems]
            transcript.append({
                "user": text if len(text) < 300 else text[:120] + f" ... ({len(text)} characters)",
                "reply": tr.reply,
                "tools_requested": [c["name"] for c in tr.calls],
                "tool_call_arguments": [{"tool": c["name"], "args": c["args"]} for c in tr.calls],
                "tools_ran": [r["name"] for r in tr.executed()],
                "tool_results": [{"tool": r["name"], "codes": sorted(
                    v for k, v in r["response"].items() if k in ("status", "error_code", "outcome") and isinstance(v, str))}
                    for r in tr.results],
                "result_codes": sorted(tr.codes()),
                "problems": problems,
            })

        failures += [f"end: {p}" for p in check_final(case.get("expect_final", {}), self.data_dir)]
        return {"passed": not failures, "failures": failures, "transcript": transcript}


# ---------------------------------------------------------------------------
# Safety checks and reporting
# ---------------------------------------------------------------------------

def fingerprint(folder: Path) -> dict:
    result = {}
    if folder.exists():
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                result[str(path.relative_to(folder))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_env_file(path: Path):
    """Read KEY=VALUE lines into the environment without overriding values that are already set."""
    try:
        from dotenv import load_dotenv
        load_dotenv(path, override=False)
        return
    except ImportError:
        pass
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def select_cases(cases: list, args) -> list:
    chosen = cases
    if args.group:
        chosen = [c for c in chosen if c["group"] in args.group]
    if args.only:
        wanted = set(args.only)
        chosen = [c for c in chosen if c["id"] in wanted]
        missing = wanted - {c["id"] for c in chosen}
        if missing:
            raise SystemExit(f"No such case id: {', '.join(sorted(missing))}. Use --list to see them.")
    return chosen


def print_plan(chosen: list, repeat: int):
    turns = sum(count_user_turns(c) for c in chosen) * repeat
    estimate = turns * EST_REQUESTS_PER_TURN
    print(f"{len(chosen)} case(s) x {repeat} run(s) = {turns} customer message(s)")
    print(f"Estimated model requests: about {estimate} (free limit: 500 per day, 15 per minute)")
    return estimate


async def run_rep(exam: Exam, case: dict, rep: int) -> dict:
    """Run a case once. If Google had a service problem, wait and run the whole case again."""
    result = {}
    for attempt in range(1, SERVICE_RETRIES + 1):
        result = await exam.run_case_once(case, rep)
        if not result.get("infra_error"):
            return result
        if attempt < SERVICE_RETRIES:
            print(f"      Google service problem ({result['failures'][0][:90]}). "
                  f"Waiting {SERVICE_WAIT_SECONDS}s, then running this case again ...")
            await asyncio.sleep(SERVICE_WAIT_SECONDS)
    return result


async def run_all(exam: Exam, chosen: list, args) -> dict:
    started = time.monotonic()
    report = {"started": datetime.now(timezone.utc).isoformat(), "cases": [], "stopped_early": None}
    for index, case in enumerate(chosen, 1):
        if exam.requests_used >= args.max_requests:
            report["stopped_early"] = f"reached the cap of {args.max_requests} estimated requests"
            break
        if (time.monotonic() - started) / 60 > args.max_minutes:
            report["stopped_early"] = f"reached the time limit of {args.max_minutes} minutes"
            break
        runs = []
        try:
            for rep in range(1, args.repeat + 1):
                runs.append(await run_rep(exam, case, rep))
        except QuotaExhausted as error:
            report["stopped_early"] = f"the free quota ran out: {str(error)[:150]}"
            break
        tested = [r for r in runs if not r.get("infra_error")]
        passed = sum(1 for r in tested if r["passed"])
        if not tested:
            print(f"[{index}/{len(chosen)}] ERROR     {case['id']:8} {case['title']}")
            print("        Google had a service problem every time. This is NOT counted as a pass or a fail.")
        else:
            status = "PASS" if passed == len(tested) else "FAIL"
            note = f"  ({len(runs) - len(tested)} run(s) skipped: Google service problem)" if len(tested) < len(runs) else ""
            print(f"[{index}/{len(chosen)}] {status} {passed}/{len(tested)}  {case['id']:8} {case['title']}{note}")
            if passed != len(tested):
                first = next(r for r in tested if not r["passed"])
                for problem in first["failures"][:6]:
                    print(f"        - {problem}")
                for item in first["transcript"]:
                    if item.get("reply"):
                        print(f"        reply: {item['reply'][:220].replace(chr(10), ' ')}")
                        break
        report["cases"].append({
            "id": case["id"], "title": case["title"], "group": case["group"],
            "passed_runs": passed, "total_runs": len(tested), "service_error_runs": len(runs) - len(tested),
            "runs": runs,
        })
    report["requests_used_estimate"] = exam.requests_used
    report["minutes"] = round((time.monotonic() - started) / 60, 1)
    return report


def print_summary(report: dict, chosen: list):
    tested = [c for c in report["cases"] if c["total_runs"] > 0]
    errored = [c for c in report["cases"] if c["total_runs"] == 0]
    full = [c for c in tested if c["passed_runs"] == c["total_runs"]]
    flaky = [c for c in tested if 0 < c["passed_runs"] < c["total_runs"]]
    failed = [c for c in tested if c["passed_runs"] == 0]
    print("\n" + "=" * 60)
    print(f"Cases passed in every run: {len(full)} of {len(tested)} tested")
    by_group = {}
    for c in tested:
        g = by_group.setdefault(c["group"], [0, 0])
        g[1] += 1
        g[0] += 1 if c["passed_runs"] == c["total_runs"] else 0
    for group, (ok, total) in sorted(by_group.items()):
        print(f"   {group:10} {ok}/{total}")
    if flaky:
        print("Inconsistent (passed some runs, failed others): " + ", ".join(c["id"] for c in flaky))
    if failed:
        print("Failed every run: " + ", ".join(c["id"] for c in failed))
    if errored:
        print("NOT TESTED because of Google service problems (run them again later): "
              + ", ".join(c["id"] for c in errored))
    if report["stopped_early"]:
        not_run = [c["id"] for c in chosen if c["id"] not in {d["id"] for d in report["cases"]}]
        print(f"STOPPED EARLY: {report['stopped_early']}")
        print("Not run: " + ", ".join(not_run))
    print(f"Estimated model requests used: {report['requests_used_estimate']}   Time: {report['minutes']} minutes")

    samples = [(c["id"], t) for c in tested for r in c["runs"] if r["passed"] for t in r["transcript"] if t.get("reply")]
    if samples:
        print("\nREAD THESE YOURSELF (a random sample of replies that passed). Code can miss a bad answer:")
        for cid, t in random.sample(samples, min(5, len(samples))):
            print(f"  [{cid}] customer: {t['user'][:100]}")
            print(f"        agent:    {t['reply'][:300].replace(chr(10), ' ')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Automatic exam for the NovaCart support agent.")
    parser.add_argument("--list", action="store_true", help="list all cases and exit")
    parser.add_argument("--dry-run", action="store_true", help="check the case files and show the plan, no model calls")
    parser.add_argument("--only", nargs="+", metavar="ID", help="run only these case ids")
    parser.add_argument("--group", nargs="+", metavar="NAME", help="run only these groups")
    parser.add_argument("--repeat", type=int, default=1, help="run each case this many times (default 1)")
    parser.add_argument("--rpm", type=int, default=10, help="model requests per minute to aim for (default 10)")
    parser.add_argument("--max-requests", type=int, default=300, help="stop after this many estimated requests")
    parser.add_argument("--max-minutes", type=int, default=45, help="stop after this many minutes")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args()

    cases = load_cases()
    problems = [p for c in cases for p in validate_case(c)]
    ids = [c["id"] for c in cases if "id" in c]
    problems += [f"duplicate case id {i}" for i, n in Counter(ids).items() if n > 1]
    if problems:
        print("Problems in the case files:")
        for p in problems:
            print("  - " + p)
        return 2

    if args.list:
        for c in cases:
            print(f"{c['id']:8} {c['group']:10} {count_user_turns(c)} msg  {c['title']}")
        print(f"\n{len(cases)} cases")
        return 0

    chosen = select_cases(cases, args)
    if not chosen:
        print("No cases selected.")
        return 2
    estimate = print_plan(chosen, args.repeat)
    if args.dry_run:
        print("Dry run: the case files are valid. No model calls were made.")
        return 0

    # The real policy files must be approved, because the exam copies them
    policy_search = load_module("policy_search", ROOT / "support_agent" / "policy_search.py")
    status = policy_search.file_status(REAL_DATA / "policies", REAL_DATA / "policy_approvals.json")
    if not status or any(s != "APPROVED" for s in status.values()):
        print(f"The policy files are not all approved: {status}")
        print("Run: python scripts\\approve_policies.py --approve-all")
        return 2

    if not args.yes:
        print("The exam uses your free API quota and takes a while. Do not use adk web meanwhile.")
        if input("Type yes to start: ").strip().lower() != "yes":
            print("Cancelled.")
            return 1

    # Everything below happens on a TEMPORARY copy of the data
    tmp = Path(tempfile.mkdtemp(prefix="novacart_exam_"))
    os.environ["NOVACART_DATA_DIR"] = str(tmp)
    os.environ["NOVACART_TODAY"] = PINNED_TODAY
    load_env_file(ROOT / "support_agent" / ".env")
    before = fingerprint(REAL_DATA)
    sys.path.insert(0, str(ROOT))
    exit_code = 0
    try:
        from google.adk.runners import InMemoryRunner
        from google.genai import types
        from support_agent import agent as agent_module

        if Path(agent_module.DATA_DIR).resolve() != tmp.resolve():
            print("STOP: the agent is not using the temporary data folder. Is support_agent/agent.py up to date?")
            return 2
        if tmp.resolve() == REAL_DATA.resolve():
            print("STOP: the temporary folder is the real data folder.")
            return 2

        def make_message(text):
            return types.Content(role="user", parts=[types.Part(text=text)])

        runner = InMemoryRunner(app=agent_module.app)
        review = load_module("review_refunds", ROOT / "scripts" / "review_refunds.py")
        exam = Exam(runner, agent_module.app.name, tmp, review, Pacer(args.rpm), make_message)

        report = asyncio.run(run_all(exam, chosen, args))
        print_summary(report, chosen)

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = RESULTS_DIR / f"exam-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nFull report with every reply: {out}")
        if (any(c["passed_runs"] != c["total_runs"] for c in report["cases"])
                or any(c["total_runs"] == 0 for c in report["cases"]) or report["stopped_early"]):
            exit_code = 1
    finally:
        after = fingerprint(REAL_DATA)
        shutil.rmtree(tmp, ignore_errors=True)
        if before != after:
            changed = sorted(set(before) ^ set(after) | {k for k in before if k in after and before[k] != after[k]})
            print(f"\nWARNING: files in your real data folder changed during the run: {changed}")
            print("The exam does not write there. Did you use adk web at the same time?")
            exit_code = 3
        else:
            print("Check passed: your real data folder is unchanged.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())