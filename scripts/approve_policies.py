"""Shows the approval status of every policy file and lets a HUMAN approve files.

Run from the project root:
    python scripts\\approve_policies.py                          show the status only (changes nothing)
    python scripts\\approve_policies.py --approve returns.md     approve one or more files
    python scripts\\approve_policies.py --approve-all            approve every file that is NEW or CHANGED

Status meanings:
    APPROVED  the file matches its approved fingerprint, so the agent uses it
    NEW       the file is not on the approved list, so the agent ignores it
    CHANGED   the file was edited after approval, so the agent ignores it
    MISSING   the file is on the list but no longer exists

Why this script is separate from the agent: the agent must be able to READ the approved list
but never WRITE it. See docs/03-knowledge-base-security.md.
"""

import argparse
import importlib.util
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("NOVACART_DATA_DIR") or ROOT / "data")  # same setting the agent uses
POLICIES_DIR = DATA_DIR / "policies"
APPROVALS_FILE = DATA_DIR / "policy_approvals.json"

# Load policy_search.py by file path so we reuse the exact same fingerprint code as the agent.
spec = importlib.util.spec_from_file_location("policy_search", ROOT / "support_agent" / "policy_search.py")
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


def show_status() -> dict:
    status = ps.file_status(POLICIES_DIR, APPROVALS_FILE)
    if not status:
        print(f"No policy files found in {POLICIES_DIR}")
    for name, state in sorted(status.items()):
        print(f"  {state:9} {name}")
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="Review and approve policy files.")
    parser.add_argument("--approve", nargs="+", metavar="FILE", help="approve these policy files")
    parser.add_argument("--approve-all", action="store_true", help="approve every NEW or CHANGED file")
    args = parser.parse_args()

    print("Current status:")
    status = show_status()

    if not args.approve and not args.approve_all:
        print("\nNothing changed. To approve a file, review it first, then run with --approve FILE.")
        return 0

    if args.approve_all:
        names = [n for n, s in status.items() if s in ("NEW", "CHANGED")]
    else:
        names = args.approve

    files = ps.read_policy_files(POLICIES_DIR)
    unknown = [n for n in names if n not in files]
    if unknown:
        print(f"\nThese files do not exist in {POLICIES_DIR}: {', '.join(unknown)}")
        return 1
    if not names:
        print("\nNothing to approve.")
        return 0

    print("\nYou are about to approve these files. Have you read them?")
    for name in names:
        print(f"  {name}   fingerprint {files[name][0][:16]}...")
    if input("Type yes to approve: ").strip().lower() != "yes":
        print("Cancelled. Nothing changed.")
        return 1

    try:
        data = json.loads(APPROVALS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    for name in names:
        data[name] = {"sha256": files[name][0], "approved_on": date.today().isoformat()}
    APPROVALS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    print("\nApproved. New status:")
    show_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())