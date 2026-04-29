#!/usr/bin/env python3
"""
Open Brain Gate — UserPromptSubmit hook (v3 — auto-search).

On every user turn:
  1. Save the prompt to /tmp/claude-ob-prompt-{session_id}
  2. POST the prompt to OB /api/search (top 5 results)
  3. Inject results into context via stdout
  4. Flip gate to 'satisfied' so PreToolUse doesn't block

If the auto-search fails (network/HTTP error), fall back to v2 behavior:
write 'pending' and print the manual-query reminder.

Flag file: /tmp/claude-ob-gate-{session_id}
Prompt file: /tmp/claude-ob-prompt-{session_id}
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

GATE_DIR = Path("/tmp")
GATE_PREFIX = "claude-ob-gate-"
PROMPT_PREFIX = "claude-ob-prompt-"
LOG_FILE = Path.home() / ".claude" / "hooks" / "ob-gate.log"

OB_SEARCH_URL = os.getenv(
    "OPEN_BRAIN_SEARCH_URL", "http://localhost:8765/api/search"
)
SEARCH_TIMEOUT = 8
SEARCH_LIMIT = 5
PROMPT_TRUNCATE = 500  # chars sent to /api/search


def log(msg: str):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


def extract_user_message(data: dict) -> str:
    msg = data.get("message", {})
    if isinstance(msg, dict):
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content if isinstance(b, dict)]
            joined = " ".join(t for t in texts if t)
            if joined:
                return joined
    for key in ("user_message", "prompt", "content", "text"):
        val = data.get(key, "")
        if isinstance(val, str) and val:
            return val
    return ""


def search_ob(query: str):
    """POST to /api/search. Returns list of result dicts or None on failure."""
    if not query.strip():
        return None
    try:
        payload = json.dumps({
            "query": query[:PROMPT_TRUNCATE],
            "limit": SEARCH_LIMIT,
        }).encode("utf-8")
        req = Request(
            OB_SEARCH_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=SEARCH_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("results", [])
    except URLError as e:
        log(f"[SEARCH-FAIL URLError] {e}")
        return None
    except Exception as e:
        log(f"[SEARCH-FAIL {type(e).__name__}] {e}")
        return None


def format_results(results) -> str:
    """Format search results as a compact context block."""
    if not results:
        return ""
    lines = ["[OPEN BRAIN — top relevant memories auto-injected]"]
    for i, r in enumerate(results, 1):
        cat = r.get("category", "?")
        sim = r.get("similarity", 0)
        created = r.get("created_at", "")[:10]
        summary = (r.get("summary") or r.get("raw_text") or "")[:200]
        lines.append(
            f"{i}. ({cat}, {created}, sim={sim:.2f}) {summary}"
        )
    lines.append("[OPEN BRAIN GATE — SATISFIED automatically by hook]")
    return "\n".join(lines)


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}

    session_id = data.get("session_id", "default")
    gate_file = GATE_DIR / f"{GATE_PREFIX}{session_id}"
    prompt_file = GATE_DIR / f"{PROMPT_PREFIX}{session_id}"

    user_msg = extract_user_message(data)
    prompt_file.write_text(user_msg[:2000])

    results = search_ob(user_msg) if user_msg else None

    if results is not None:
        # Auto-search succeeded — satisfy gate, inject context.
        gate_file.write_text("satisfied")
        log(
            f"[INIT auto-search OK] session={session_id} "
            f"prompt_len={len(user_msg)} results={len(results)}"
        )
        print(format_results(results))
    else:
        # Fallback: behave like v2.
        gate_file.write_text("pending")
        log(
            f"[INIT auto-search FALLBACK] session={session_id} "
            f"prompt_len={len(user_msg)}"
        )
        print(
            "[OPEN BRAIN GATE — PENDING] "
            "Auto-search failed; query Open Brain manually with a TARGETED search "
            "before any tool call this turn."
        )


if __name__ == "__main__":
    main()
