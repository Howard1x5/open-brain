# PROJECT SPEC: open-brain

**Created:** March 2026
**Status:** Draft
**Version:** 1.1

---

## 1. CONTEXT

### What This Is

A persistent, agent-readable memory system. The user captures thoughts via Telegram (text or voice from phone or laptop), they get embedded and stored in a local Postgres database, and any Claude session (Claude Code or browser) can query accumulated context via MCP server. One brain, every AI tool, zero SaaS dependencies.

### Infrastructure

```
Physical Machine: Gaming PC running Proxmox hypervisor
└── Ubuntu 24.04 LXC container (2 vCPU, 2GB RAM, 20GB disk)
    ├── Docker
    │   └── ankane/pgvector container  (Postgres 16 + pgvector pre-built)
    ├── Python services (native on Ubuntu, managed by systemd)
    │   ├── bot.py           ← Telegram bot
    │   └── mcp_server.py    ← MCP server
    └── Local model files (~250MB total)
        ├── all-MiniLM-L6-v2  (embeddings, 384-dim, no API cost)
        └── whisper-base      (voice transcription, no API cost)
```

Claude API (Haiku) is called only for metadata extraction — category, people, topics, summary. Embeddings and transcription are fully local. When the Proxmox machine is busy with other VMs, this system slows down gracefully — it does not crash. When the blade server arrives, the LXC container migrates directly via Proxmox with no reconfiguration needed.

### What Success Looks Like

User sends a Telegram message or voice note, receives a confirmation reply within 10 seconds showing what was filed and how it was classified. Any Claude Code session can call `search_brain("Sarah career")` and get relevant memories back, even if the word "career" never appeared in the original note.

### What Failure Looks Like

- Capture works but search returns nothing relevant (embedding pipeline broken)
- Voice messages don't get transcribed (Whisper not wired up correctly)
- MCP server connects in Claude Code but returns empty results
- Services don't survive a server reboot
- Claude Code writes to DB but confidence threshold logic is ignored

### Workflow Context

This is standalone infrastructure. It runs persistently on the server. The user's only job is to send Telegram messages. Everything else is automated.

---

## 2. BEHAVIOR

### Phase Breakdown

**Phase 1: Capture (Telegram → Postgres)**
When a text message arrives at the Telegram bot from the allowed user ID:
1. Pass raw text to capture pipeline
2. Capture pipeline calls Claude API for metadata extraction
3. Capture pipeline generates local embedding
4. Store memory row in Postgres
5. Reply to Telegram with confirmation or clarification request

When a voice message arrives:
1. Download .ogg file from Telegram servers
2. Transcribe with local Whisper (base model)
3. Pass transcript to same capture pipeline as text
4. Reply with transcription shown + filing confirmation

**Phase 2: Query (MCP → Claude Code / Browser)**
When Claude Code calls `search_brain(query)`:
1. Generate embedding for query
2. Run cosine similarity search in Postgres
3. Return top results with text, category, date, similarity score

**Phase 3: Correction (Telegram fix: reply)**
When user replies to a bot confirmation message with `fix: <correction>`:
1. Identify which memory the replied-to message corresponds to
2. Re-run capture pipeline on corrected text
3. Update the existing memory row
4. Confirm update in Telegram

### Edge Cases

- Message from non-allowed Telegram user ID → silently ignore, do not reply
- Claude API returns malformed JSON → log error, file with `needs_review` status, ask user to repost
- Claude API is down → store raw text with null metadata, mark `needs_review`, notify user
- Voice file download fails → reply "Could not download voice message, please try again"
- Whisper transcription produces empty string → reply asking user to resend
- Embedding model fails to load → log error, store text without embedding, mark `needs_review`
- Postgres connection fails → log error with full traceback, notify user via Telegram if possible

### Depth Directive

Every edge case listed above must be handled explicitly. Do not skip error handling. Every failure mode should result in either a recovery action or a clear user notification, never silent failure.

---

## 3. CONSTRAINTS

### Must NOT Do

- Must not accept Telegram messages from any user ID other than `TELEGRAM_ALLOWED_USER_ID`
- Must not make any OpenAI API calls (Anthropic only for LLM; local model for embeddings)
- Must not store API keys or secrets in any file that gets committed to git
- Must not install system packages without checking if they're already present first
- Must not use synchronous blocking calls inside the async Telegram bot handlers
- Must not hardcode any paths — use environment variables or derive from `__file__`

### Directory Structure

```
~/open-brain/
├── .env                        # secrets — never commit
├── .env.example                # template with key names, no values
├── .gitignore                  # includes .env, models/, __pycache__/
├── requirements.txt
├── setup.sql                   # database schema
├── setup.sh                    # one-shot install script
├── capture.py                  # core processing pipeline
├── bot.py                      # Telegram bot
├── mcp_server.py               # MCP server (stdio + HTTP)
├── test_capture.py             # validation tests
└── README.md                   # manual steps user must complete
```

### Hard Requirements

- Ubuntu 24.04 LXC container on Proxmox (2 vCPU, 2GB RAM, 20GB disk)
- Docker installed on the LXC container (for Postgres only)
- Postgres via `ankane/pgvector` Docker image (eliminates pgvector compilation entirely)
- Python 3.10+ (native on Ubuntu)
- Embedding model: `all-MiniLM-L6-v2` via sentence-transformers (384 dimensions, local)
- Whisper model: `base` (local transcription, no API)
- Telegram bot library: `python-telegram-bot` v20+ (async)
- MCP library: `mcp` Python SDK
- All secrets loaded from `.env` via `python-dotenv`
- Both systemd services must auto-restart on failure and start on boot

### Environment Variables Required

```
ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_ID=
DATABASE_URL=postgresql://open-brain:open-brain@localhost/open-brain
WHISPER_MODEL=base
EMBEDDING_MODEL=all-MiniLM-L6-v2
MCP_HTTP_PORT=8765
```

---

## 4. EXAMPLES

### Good Capture Output

Input: `"Met with Sarah today, she mentioned she's thinking about leaving her job to start a consulting business, unhappy since the reorg"`

Expected DB row:
```json
{
  "category": "person",
  "people": ["Sarah"],
  "topics": ["career transition", "consulting", "reorg"],
  "action_item": null,
  "summary": "Sarah considering leaving job to consult, unhappy post-reorg",
  "confidence": 0.91
}
```

Expected Telegram reply:
```
✓ Filed as person | "Sarah considering leaving job to consult" | confidence: 0.91
Reply "fix: <correction>" if wrong
```

### Good Search Output

Query: `"Sarah career"`

Expected result even though "career" never appeared in the note:
```
1. [person | 2026-03-05] Sarah considering leaving job to consult, unhappy post-reorg
   similarity: 0.84
```

### Low Confidence Output

Input: `"that thing we talked about"`

Expected Telegram reply:
```
? Couldn't classify confidently (0.31). 
Repost with a prefix: person: / project: / idea: / decision: / admin:
```

### Bad Output (Avoid This)

Claude Code builds capture.py as one 300-line function that does everything. When the Claude API returns slightly malformed JSON, the whole function throws an unhandled exception and the bot crashes. The user gets no reply and has no idea what happened. This is the failure mode to architect against — every substep must fail gracefully and independently.

---

## 5. TASK DECOMPOSITION

### Task 0: Create Ubuntu LXC Container in Proxmox
- **Input:** Proxmox host with available resources
- **Action:** Walk user through creating Ubuntu 24.04 LXC container with 2 vCPU, 2GB RAM, 20GB disk. Enable nesting feature (required for Docker inside LXC). Assign static IP.
- **Output:** SSH-accessible Ubuntu container
- **Validation:** `ssh user@<container-ip>` connects successfully; `uname -a` shows Ubuntu 24.04
- **Checkpoint:** Yes — confirm SSH access before proceeding
- **Complexity:** Simple
- **Note:** This is a guided walkthrough task, not automated. Claude Code provides the exact Proxmox UI steps.

### Task 1: Install System Dependencies
- **Input:** Fresh Ubuntu 24.04 LXC container
- **Action:** Install Docker, Python 3.10+, pip, ffmpeg (required by Whisper for audio), git. Check each before installing.
- **Output:** All dependencies present
- **Validation:** `docker --version`, `python3 --version`, `ffmpeg -version`
- **Checkpoint:** Yes — confirm all installed before proceeding
- **Complexity:** Simple

### Task 2: Database Setup with Docker
- **Input:** Docker installed
- **Action:** Pull `ankane/pgvector` image; run container with persistent volume; create openbrain database and user; run `setup.sql` schema
- **Output:** Postgres running in Docker with pgvector enabled, `memories` and `inbox_log` tables created
- **Docker run command:**
  ```bash
  docker run -d \
    --name open-brain-postgres \
    --restart unless-stopped \
    -e POSTGRES_USER=open-brain \
    -e POSTGRES_PASSWORD=open-brain \
    -e POSTGRES_DB=open-brain \
    -v open-brain-pgdata:/var/lib/postgresql/data \
    -p 5432:5432 \
    ankane/pgvector
  ```
- **Validation:** `psql $DATABASE_URL -c "\dt"` shows both tables; `\dx` shows vector extension
- **Checkpoint:** No
- **Complexity:** Simple

### Task 3: Capture Pipeline
- **Input:** A string of text
- **Action:** Extract metadata via Claude Haiku, generate local embedding, store in Postgres, return confirmation
- **Output:** Populated memory row, confirmation dict with category/summary/confidence
- **Validation:** Run `test_capture.py` — verify DB row created with all fields populated
- **Checkpoint:** Yes — test with real text before wiring to Telegram
- **Complexity:** **Complex → see CAPTURE_GUIDE.md**

### Task 4: Telegram Bot
- **Input:** Telegram messages (text and voice) from allowed user
- **Action:** Route to capture pipeline, handle voice transcription, send confirmations, handle fix: replies
- **Output:** Working bot that responds correctly to all message types
- **Validation:** Send test text message, test voice message, test fix: reply — all get correct responses
- **Checkpoint:** Yes — verify all three message types work before moving on
- **Complexity:** **Complex → see BOT_GUIDE.md**

### Task 5: MCP Server
- **Input:** Tool calls from Claude Code or HTTP client
- **Action:** Expose search_brain, list_recent, add_memory tools over stdio and HTTP
- **Output:** Working MCP server that Claude Code can query
- **Validation:** Run stdio test, run HTTP test, add to Claude Code config and verify tool appears
- **Checkpoint:** Yes — confirm Claude Code can see and call the tools
- **Complexity:** **Complex → see MCP_GUIDE.md**

### Task 6: Systemd Services
- **Input:** Working bot.py and mcp_server.py
- **Action:** Create service files, enable, start, verify persistence across reboot simulation
- **Output:** Both services running, set to restart on failure, start on boot
- **Validation:** `systemctl status open-brain-bot open-brain-mcp` both show active; `journalctl -u open-brain-bot -n 20` shows clean logs; `sudo reboot` → both services back up automatically
- **Checkpoint:** No
- **Complexity:** Simple

### Task 7: README and .env.example
- **Input:** Completed system
- **Action:** Generate README with all manual steps, generate .env.example with key names and no values
- **Output:** README.md and .env.example
- **Validation:** README contains all manual steps including Proxmox LXC creation, BotFather setup, and Claude Code MCP config
- **Checkpoint:** No
- **Complexity:** Simple

### Complex Task Reference Files
- Task 3 → CAPTURE_GUIDE.md
- Task 4 → BOT_GUIDE.md
- Task 5 → MCP_GUIDE.md

---

## 6. CHANGE LOG

*Empty on creation. Add entries here when spec is modified during build.*

### Format:
⚠️ MODIFIED — Task [N]: [What changed]
- **Original:** [What the spec said]
- **Changed to:** [What it says now]
- **Reason:** [Why]
- **Spec gap:** [What the original spec should have said]
