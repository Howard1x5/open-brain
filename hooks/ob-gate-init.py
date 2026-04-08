#!/usr/bin/env python3
"""
Open Brain Gate — UserPromptSubmit hook (v2).

Writes a per-session flag file to 'pending' at the start of each user turn.
Also saves the user's prompt text for keyword-matching enforcement in the gate check.

Flag file: /tmp/claude-ob-gate-{session_id}
Prompt file: /tmp/claude-ob-prompt-{session_id}

Setup:
  Add to ~/.claude/settings.json under hooks.UserPromptSubmit (matcher: "")
"""

import json
import sys
from datetime import datetime
from pathlib import Path

GATE_DIR = Path("/tmp")
GATE_PREFIX = "claude-ob-gate-"
PROMPT_PREFIX = "claude-ob-prompt-"
LOG_FILE = Path.home() / ".claude" / "hooks" / "ob-gate.log"


def log(msg: str):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


def extract_user_message(data: dict) -> str:
    """Try multiple paths to find the user's message text."""
    # Try message.content (Claude Code hook protocol)
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
    # Try direct fields
    for key in ("user_message", "prompt", "content", "text"):
        val = data.get(key, "")
        if isinstance(val, str) and val:
            return val
    return ""


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}

    session_id = data.get("session_id", "default")
    gate_file = GATE_DIR / f"{GATE_PREFIX}{session_id}"
    prompt_file = GATE_DIR / f"{PROMPT_PREFIX}{session_id}"

    # Always reset to 'pending' at the start of a new user turn
    gate_file.write_text("pending")

    # Save user prompt for keyword matching in gate check
    user_msg = extract_user_message(data)
    prompt_file.write_text(user_msg[:2000])

    log(
        f"[INIT] session={session_id} prompt_len={len(user_msg)} "
        f"keys={list(data.keys())}"
    )

    # Emit the reminder to stdout (shown to assistant via hook output)
    print(
        "[OPEN BRAIN GATE — PENDING] "
        "Before any tool call this turn, query Open Brain with a TARGETED search "
        "relevant to the user's request. Generic queries are rejected. "
        "No skip allowed — every turn must query."
    )


if __name__ == "__main__":
    main()
