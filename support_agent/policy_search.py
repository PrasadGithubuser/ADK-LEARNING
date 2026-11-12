"""Keyword search over the NovaCart policy documents (BM25 ranking), APPROVED files only.

Pure Python: no AI, no ADK. Easy to test on its own.
See docs/03-knowledge-base-security.md for why only approved files are searched.

How it works:
1. Approval: a policy file is used only if its fingerprint (hash) matches the approved list.
   Anything else (new, edited, unlisted) is ignored until a human approves it.
2. Chunking: every approved file is split into pieces, one per "##" heading.
3. Scoring: each piece is scored against the question words with BM25
   (rare words count more than common words, and long pieces are not unfairly favored).
4. Retrieval: the best few pieces are returned. If nothing scores high enough, nothing is returned.
"""

import hashlib
import json
import logging
import math
import re
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)

K1 = 1.5                 # how quickly repeating a word stops adding score
B = 0.75                 # how much long pieces are penalised
MIN_SCORE = 1.0          # below this, a piece is treated as "no match"
MAX_PASSAGE_CHARS = 1200  # cap on what is handed to the model (limits context flooding)

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "to", "of", "in", "on", "at",
    "for", "and", "or", "my", "me", "i", "it", "its", "do", "does", "did", "can", "could",
    "you", "your", "we", "our", "how", "what", "when", "where", "which", "who", "will", "would",
    "should", "if", "so", "this", "that", "with", "from", "by", "as", "any", "there", "about",
    "have", "has", "had", "please", "tell", "me",
    # "policy" is in the title of every policy document, so it says nothing about the topic
    "policy", "policies",
}


# ---------------------------------------------------------------------------
# Approval (fingerprints)
# ---------------------------------------------------------------------------

def file_hash(raw: bytes) -> str:
    """SHA-256 fingerprint of a file's content.

    Windows and Linux line endings are made the same first, so the same text always gives the same hash.
    """
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def load_approvals(approvals_file: Path) -> dict:
    """Return {file name: approved hash}. A missing or broken list means NOTHING is approved."""
    try:
        data = json.loads(Path(approvals_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        name: entry["sha256"]
        for name, entry in data.items()
        if isinstance(entry, dict) and isinstance(entry.get("sha256"), str)
    }


def read_policy_files(policies_dir: Path) -> dict:
    """Return {file name: (hash, text)}. Each file is read once, so the hash and the text always match."""
    files = {}
    policies_dir = Path(policies_dir)
    if not policies_dir.exists():
        return files
    for path in sorted(policies_dir.glob("*.md")):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        files[path.name] = (file_hash(raw), raw.decode("utf-8", errors="replace"))
    return files


def file_status(policies_dir: Path, approvals_file: Path) -> dict:
    """Return {file name: APPROVED | CHANGED | NEW | MISSING}."""
    files = read_policy_files(policies_dir)
    approved = load_approvals(approvals_file)
    status = {}
    for name, (digest, _) in files.items():
        if name not in approved:
            status[name] = "NEW"
        elif approved[name] == digest:
            status[name] = "APPROVED"
        else:
            status[name] = "CHANGED"
    for name in approved:
        if name not in files:
            status[name] = "MISSING"
    return status


# ---------------------------------------------------------------------------
# Chunking and scoring
# ---------------------------------------------------------------------------

def _stem(word: str) -> str:
    """Very light stemming so 'returns', 'returned' and 'return' match each other."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    for suffix in ("ing", "ed", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix) and not word.endswith("ss"):
            stem = word[: -len(suffix)]
            if suffix != "s" and len(stem) > 2 and stem[-1] == stem[-2]:
                stem = stem[:-1]  # shipping -> shipp -> ship
            return stem
    return word


def _tokenize(text: str) -> list:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [_stem(w) for w in words if w not in STOPWORDS]


def _split_markdown(text: str) -> list:
    """Return (title, heading, body) for every section that has a body."""
    title = ""
    heading = ""
    lines = []
    sections = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((title, heading, "\n".join(lines).strip()))
            heading = line[3:].strip()
            lines = []
        elif line.startswith("# "):
            title = line[2:].strip()
        else:
            lines.append(line)
    sections.append((title, heading, "\n".join(lines).strip()))
    # Text with no "##" heading is still kept (the file title becomes its heading),
    # so a document without headings can never silently disappear from the search.
    return [(t, h or t or "General", b) for t, h, b in sections if b]


def _build_index(files: dict, approved: dict) -> dict:
    chunks = []
    for name, (digest, text) in sorted(files.items()):
        if approved.get(name) != digest:
            continue  # not approved: never searched
        for _title, heading, body in _split_markdown(text):
            chunks.append({
                "source": name,
                "section": heading,
                "text": body,
                "tokens": _tokenize(f"{heading} {body}"),
            })
    doc_freq = Counter()
    for chunk in chunks:
        doc_freq.update(set(chunk["tokens"]))
    total = sum(len(c["tokens"]) for c in chunks)
    return {
        "chunks": chunks,
        "doc_freq": doc_freq,
        "avg_len": (total / len(chunks)) if chunks else 0.0,
    }


# The index is rebuilt only when a policy file or the approved list changes.
_cache = {"signature": None, "index": None}


def _get_index(policies_dir: Path, approvals_file: Path) -> dict:
    files = read_policy_files(policies_dir)
    approved = load_approvals(approvals_file)
    signature = (
        tuple(sorted((name, digest) for name, (digest, _) in files.items())),
        tuple(sorted(approved.items())),
    )
    if _cache["index"] is None or _cache["signature"] != signature:
        for name, (digest, _) in sorted(files.items()):
            if approved.get(name) != digest:
                reason = "not on the approved list" if name not in approved else "changed since it was approved"
                logger.warning("Policy file %s is %s: IGNORED until a human approves it.", name, reason)
        _cache["index"] = _build_index(files, approved)
        _cache["signature"] = signature
    return _cache["index"]


def _score(query_terms: list, chunk: dict, index: dict) -> float:
    n = len(index["chunks"])
    counts = Counter(chunk["tokens"])
    length = len(chunk["tokens"])
    score = 0.0
    for term in set(query_terms):
        tf = counts.get(term, 0)
        if tf == 0:
            continue
        df = index["doc_freq"][term]
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        norm = tf + K1 * (1 - B + B * length / index["avg_len"])
        score += idf * tf * (K1 + 1) / norm
    return score


def search(policies_dir: Path, approvals_file: Path, query: str, top_k: int = 3) -> list:
    """Return up to top_k passages from APPROVED policy files, best first. Empty list = no match."""
    index = _get_index(policies_dir, approvals_file)
    if not index["chunks"]:
        return []
    query_terms = _tokenize(query)
    if not query_terms:
        return []

    scored = [(_score(query_terms, chunk, index), chunk) for chunk in index["chunks"]]
    scored.sort(key=lambda pair: pair[0], reverse=True)

    results = []
    for score, chunk in scored[:top_k]:
        if score < MIN_SCORE:
            break
        results.append({
            "source": chunk["source"],
            "section": chunk["section"],
            "text": chunk["text"][:MAX_PASSAGE_CHARS],
            "score": round(score, 2),
        })
    return results