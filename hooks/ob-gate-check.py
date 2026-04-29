#!/usr/bin/env python3
"""
Open Brain Gate — PreToolUse hook (v2).

Blocks tool calls until the assistant has queried Open Brain this turn.

v2 changes:
  - TARGETED QUERY ENFORCEMENT: Generic queries (no WHERE/ILIKE/search terms)
    are rejected. You can't rubber-stamp the gate with ORDER BY LIMIT N.
  - AUDIT LOGGING: Every OB query attempt (accepted or rejected) is logged
    to ob-audit.log as JSON-lines for spot-checking.
  - CAPTURE-ONLY: add_memory / api/add calls are allowed but don't satisfy
    the gate — you still need to search before doing other work.

Flow:
  1. Read flag file /tmp/claude-ob-gate-{session_id}
  2. If 'satisfied' -> allow
  3. If 'pending':
     a. Classify the tool call:
        - None (not OB-related) -> BLOCK (must query OB first)
        - "targeted"    -> ALLOW, flip to satisfied
        - "read-memory" -> ALLOW, flip to satisfied
        - "generic"     -> BLOCK with "add search terms" message
        - "capture-only"-> ALLOW but do NOT flip gate
  4. No flag file -> fail-open (avoid bricking sessions)
  NO SKIP SENTINEL. Every turn must query Open Brain. No exceptions.

Output protocol (PreToolUse):
  Exit 0 + stdout "" -> allow
  Exit 2 + stderr message -> block (shown to assistant)
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

GATE_DIR = Path("/tmp")
GATE_PREFIX = "claude-ob-gate-"
PROMPT_PREFIX = "claude-ob-prompt-"
LOG_FILE = Path.home() / ".claude" / "hooks" / "ob-gate.log"
AUDIT_FILE = Path.home() / ".claude" / "hooks" / "ob-audit.log"

# ---------------------------------------------------------------------------
# Patterns that indicate an Open Brain query or capture
# ---------------------------------------------------------------------------
OB_PATTERNS = [
    r"open[-_ ]brain",                          # Any reference to "open brain"
    r"search_brain|list_recent|add_memory",     # MCP tool names
    # Add your own patterns here, e.g.:
    # r"your-ob-host\.example\.com",
    # r"your\.db\.ip\.address",
]
OB_REGEX = re.compile("|".join(OB_PATTERNS), re.IGNORECASE)

# ---------------------------------------------------------------------------
# SQL patterns that indicate actual filtering (not a generic dump)
# ---------------------------------------------------------------------------
FILTER_PATTERNS = [
    r"\bWHERE\b",
    r"\bILIKE\b",
    r"\bLIKE\b",
    r"\bSIMILAR\s+TO\b",
    r"\b@@\b",              # Full-text search operator
    r"\bts_query\b",
    r"\bsimilarity\b",
    r"\bto_tsvector\b",
    r"information_schema",  # Schema introspection is legitimate
]
FILTER_REGEX = re.compile("|".join(FILTER_PATTERNS), re.IGNORECASE)


# ===== Helpers =============================================================

def log(msg: str):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


def audit(session_id: str, tool_name: str, tool_input: dict,
          classification: str, verdict: str):
    """Write JSON-lines audit entry to ob-audit.log."""
    try:
        prompt_file = GATE_DIR / f"{PROMPT_PREFIX}{session_id}"
        prompt = "(unavailable)"
        if prompt_file.exists():
            prompt = prompt_file.read_text()[:200]

        if tool_name == "Bash":
            query = tool_input.get("command", "")[:300]
        elif tool_name == "WebFetch":
            query = tool_input.get("url", "")[:300]
        else:
            query = json.dumps(tool_input)[:300]

        entry = {
            "ts": datetime.now().isoformat(),
            "session": session_id[:16],
            "tool": tool_name,
            "classification": classification,
            "verdict": verdict,
            "prompt": prompt,
            "query": query,
        }
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_FILE.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def classify_ob_query(tool_name: str, tool_input: dict):
    """
    Classify an OB-related tool call.

    Returns one of:
      "targeted"     — query with real search/filter criteria
      "generic"      — query without filtering (ORDER BY LIMIT only)
      "read-memory"  — reading local memory files (inherently targeted)
      "capture-only" — add_memory / api/add (writing, not searching)
      None           — not OB-related at all
    """
    # --- Bash commands ---
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if not OB_REGEX.search(cmd):
            return None
        # Has actual SQL filtering?
        if FILTER_REGEX.search(cmd):
            return "targeted"
        # search_brain invocation in a bash command
        if "search_brain" in cmd:
            return "targeted"
        return "generic"

    # --- WebFetch ---
    if tool_name == "WebFetch":
        url = tool_input.get("url", "")
        if not OB_REGEX.search(url):
            return None
        if "api/add" in url:
            return "capture-only"
        return "targeted"

    # --- MCP tools ---
    if "search_brain" in tool_name:
        query = tool_input.get("query", "")
        return "targeted" if query.strip() else "generic"
    if "list_recent" in tool_name:
        return "generic"
    if "add_memory" in tool_name:
        return "capture-only"

    # --- Reading local memory files ---
    if tool_name in ("Read", "Grep", "Glob"):
        path = (
            tool_input.get("file_path", "")
            or tool_input.get("path", "")
            or tool_input.get("pattern", "")
        )
        if re.search(r"memory/.*\.md|open-brain|MEMORY\.md", path, re.IGNORECASE):
            return "read-memory"

    return None


# ===== Main ================================================================

def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}

    session_id = data.get("session_id", "default")
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    gate_file = GATE_DIR / f"{GATE_PREFIX}{session_id}"

    # ----- Fail-open: no gate file -> allow (don't brick sessions) -----
    if not gate_file.exists():
        log(f"[ALLOW no-gate] session={session_id} tool={tool_name}")
        sys.exit(0)

    state = gate_file.read_text().strip()

    # ----- Already satisfied this turn -----
    if state == "satisfied":
        log(f"[ALLOW satisfied] session={session_id} tool={tool_name}")
        sys.exit(0)

    # ----- Classify the tool call -----
    classification = classify_ob_query(tool_name, tool_input)

    # Targeted query or memory read -> satisfy gate
    if classification in ("targeted", "read-memory"):
        gate_file.write_text("satisfied")
        audit(session_id, tool_name, tool_input, classification, "ALLOW")
        log(f"[ALLOW {classification}] session={session_id} tool={tool_name}")
        sys.exit(0)

    # Generic query -> BLOCK with actionable message
    if classification == "generic":
        audit(session_id, tool_name, tool_input, classification, "REJECT")
        log(f"[REJECT generic] session={session_id} tool={tool_name}")

        reason = (
            "OPEN BRAIN GATE — GENERIC QUERY REJECTED\n\n"
            "Your Open Brain query has no search filter (WHERE/ILIKE/search terms). "
            "Generic 'ORDER BY ... LIMIT N' queries do not satisfy the gate.\n\n"
            "Fix: Add a WHERE clause with ILIKE terms relevant to the user's request.\n"
            f"User prompt saved at: /tmp/{PROMPT_PREFIX}{session_id}\n\n"
            "Example:\n"
            "  WHERE summary ILIKE '%keyword_from_user_request%'\n"
            "Or use MCP:\n"
            "  search_brain with a non-empty query string.\n"
        )
        print(reason, file=sys.stderr)
        sys.exit(2)

    # Capture-only -> allow the write but do NOT flip the gate
    if classification == "capture-only":
        audit(session_id, tool_name, tool_input, classification, "ALLOW-NO-FLIP")
        log(
            f"[ALLOW capture-only, gate stays pending] "
            f"session={session_id} tool={tool_name}"
        )
        sys.exit(0)

    # ----- Not OB-related at all -> BLOCK -----
    log(f"[BLOCK] session={session_id} tool={tool_name}")

    reason = (
        "OPEN BRAIN GATE: You must query Open Brain before any other tool calls "
        "this turn. Query via one of:\n"
        "  1. Direct DB query: connect to your Open Brain PostgreSQL instance\n"
        "     IMPORTANT: Must include WHERE/ILIKE with terms from the user's request.\n"
        "  2. Read local memory files (project memory/*.md)\n"
        "  3. MCP: search_brain with a targeted query\n"
        "\nEvery turn requires a targeted Open Brain query. No exceptions."
    )
    print(reason, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
