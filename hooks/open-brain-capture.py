#!/usr/bin/env python3
"""
Open Brain auto-capture hook for Claude Code (v2).

Receives hook events on stdin (JSON) and sends memories to Open Brain via REST API.

Hook types handled:
  - Stop: captures a session summary (what was asked, what was done, decisions made)
          AND verifies that captures actually happened this session
  - PostToolUse (Write/Edit): captures file creation/modification events
  - PostToolUse (Bash): captures significant bash commands (SSH, git, curl to OB)

v2 changes:
  - Stop hook captures richer context (not just last_assistant_message)
  - Stop hook runs capture AUDIT — checks if memories were created during session
  - PostToolUse also captures significant Bash commands
  - Tracks capture count per session in a temp file for audit
"""

import json
import sys
import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

# REST endpoint — goes through blade Proxmox port forward to Open Brain MCP server
API_URL = os.getenv("OPEN_BRAIN_API_URL", "http://localhost:8765/api/add")

# Max text length to store per memory
MAX_TEXT_LENGTH = 4000

# Min text length worth storing
MIN_TEXT_LENGTH = 20

# Min words for Stop hook (skip short acknowledgments)
MIN_STOP_WORDS = 10

# Session capture counter file
CAPTURE_COUNT_PREFIX = "claude-ob-captures-"

LOG_FILE = os.path.expanduser("~/.claude/hooks/open-brain-capture.log")
AUDIT_FILE = os.path.expanduser("~/.claude/hooks/ob-capture-audit.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ob-hook] %(levelname)s %(message)s",
    filename=LOG_FILE,
    filemode="a",
)
logger = logging.getLogger(__name__)


def truncate(text: str, max_len: int = MAX_TEXT_LENGTH) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "... [truncated]"


def increment_capture_count(session_id: str):
    """Track how many captures happened this session."""
    count_file = Path("/tmp") / f"{CAPTURE_COUNT_PREFIX}{session_id}"
    try:
        current = int(count_file.read_text().strip()) if count_file.exists() else 0
        count_file.write_text(str(current + 1))
    except Exception:
        pass


def get_capture_count(session_id: str) -> int:
    """Get how many captures happened this session."""
    count_file = Path("/tmp") / f"{CAPTURE_COUNT_PREFIX}{session_id}"
    try:
        return int(count_file.read_text().strip()) if count_file.exists() else 0
    except Exception:
        return 0


def send_to_open_brain(text: str, session_id: str = ""):
    """POST text to Open Brain REST API. 30s timeout, 1 retry on URLError."""
    import time
    if len(text.strip()) < MIN_TEXT_LENGTH:
        logger.info("Skipping — text too short")
        return False

    payload = json.dumps({"text": text}).encode("utf-8")
    last_err = None
    for attempt in (1, 2):
        try:
            req = Request(API_URL, data=payload, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                logger.info(
                    "Stored memory %s (%s): %s%s",
                    result.get("memory_id"),
                    result.get("category"),
                    result.get("summary", "")[:60],
                    " (retry)" if attempt == 2 else "",
                )
                if session_id:
                    increment_capture_count(session_id)
                return True
        except URLError as e:
            last_err = e
            logger.warning("Attempt %d failed (URLError): %s", attempt, e)
            if attempt == 1:
                time.sleep(2)
                continue
            logger.error("Failed to reach Open Brain API after retry: %s", e)
            return False
        except Exception as e:
            logger.error("Unexpected error sending to Open Brain: %s", e)
            return False
    return False


def handle_stop(data: dict):
    """Handle Stop hook — capture session context, run audit, surface count."""
    session_id = data.get("session_id", "")
    message = data.get("last_assistant_message", "")

    if message and len(message.strip()) >= MIN_TEXT_LENGTH:
        word_count = len(message.split())
        if word_count >= MIN_STOP_WORDS:
            raw_text = f"[source: claude-code session] {truncate(message)}"
            send_to_open_brain(raw_text, session_id)

    audit_capture(session_id)

    count = get_capture_count(session_id)
    if count > 0:
        print(f"[OB ✓ {count} memories captured this session]")
    else:
        print("[OB ⚠ 0 memories captured this session — check ~/.claude/hooks/open-brain-capture.log]")


def audit_capture(session_id: str):
    """Check if captures actually happened this session. Log result."""
    count = get_capture_count(session_id)
    ts = datetime.now().isoformat()

    try:
        audit_path = Path(AUDIT_FILE)
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": ts,
            "session": session_id[:16] if session_id else "unknown",
            "captures": count,
            "verdict": "OK" if count > 0 else "WARNING_NO_CAPTURES",
        }
        with audit_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    if count == 0:
        logger.warning(
            "CAPTURE AUDIT: Session %s ended with 0 captures! "
            "Data may have been lost.",
            session_id[:16] if session_id else "unknown",
        )
    else:
        logger.info(
            "CAPTURE AUDIT: Session %s ended with %d captures.",
            session_id[:16] if session_id else "unknown",
            count,
        )


def handle_post_tool_use(data: dict):
    """Handle PostToolUse hook — capture file writes, edits, and significant bash."""
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    session_id = data.get("session_id", "")

    if tool_name == "Write":
        file_path = tool_input.get("file_path", "unknown")
        content = tool_input.get("content", "")

        if len(content.strip()) < MIN_TEXT_LENGTH:
            return

        raw_text = (
            f"[source: claude-code file write] File created/overwritten: {file_path}\n\n"
            f"{truncate(content)}"
        )
        send_to_open_brain(raw_text, session_id)

    elif tool_name == "Edit":
        file_path = tool_input.get("file_path", "unknown")
        old_string = tool_input.get("old_string", "")
        new_string = tool_input.get("new_string", "")

        if len(new_string.strip()) < MIN_TEXT_LENGTH and len(old_string.strip()) < MIN_TEXT_LENGTH:
            return

        raw_text = (
            f"[source: claude-code file edit] File edited: {file_path}\n\n"
            f"Changed:\n{truncate(old_string, 1500)}\n\nTo:\n{truncate(new_string, 1500)}"
        )
        send_to_open_brain(raw_text, session_id)

    elif tool_name == "Bash":
        # Capture significant bash commands (git commits, deployments, SSH work)
        cmd = tool_input.get("command", "")
        if not cmd or len(cmd.strip()) < MIN_TEXT_LENGTH:
            return

        # Only capture commands that represent meaningful work
        significant_patterns = [
            "git commit", "git push", "git merge",
            "ssh ", "scp ",
            "docker ", "systemctl ",
            "pip install", "npm install",
            "curl.*api/add",  # OB captures
        ]
        is_significant = any(p in cmd.lower() for p in significant_patterns if ".*" not in p)
        # Handle regex-like patterns
        if not is_significant:
            import re
            for p in significant_patterns:
                if ".*" in p:
                    if re.search(p.replace(".*", ".*"), cmd, re.IGNORECASE):
                        is_significant = True
                        break

        if is_significant:
            raw_text = (
                f"[source: claude-code bash] Command executed:\n{truncate(cmd, 2000)}"
            )
            send_to_open_brain(raw_text, session_id)


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        data = json.loads(raw)
        hook_event = data.get("hook_event_name", "")

        if hook_event == "Stop":
            handle_stop(data)
        elif hook_event == "PostToolUse":
            handle_post_tool_use(data)

    except json.JSONDecodeError as e:
        logger.error("Failed to parse stdin JSON: %s", e)
    except Exception as e:
        logger.error("Unexpected error: %s", e)


if __name__ == "__main__":
    main()
