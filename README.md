# Open Brain

A persistent memory system for AI assistants. Captures context from conversations, categorizes it automatically, and makes it searchable — so your AI never starts from zero.

## What It Does

Open Brain turns ephemeral AI conversations into persistent, searchable memory:

- **Auto-capture**: Claude Code hooks automatically send file edits, bash commands, and session summaries to Open Brain
- **Auto-categorize**: Each memory is classified (project, admin, idea, decision, etc.) and summarized by an LLM
- **Semantic search**: pgvector embeddings enable similarity search across all memories
- **Gate enforcement**: Claude Code hooks enforce that the AI queries Open Brain before every turn — no more context loss between sessions

## Architecture

```
Telegram Bot ──→ Open Brain API ──→ PostgreSQL + pgvector
                      ↑                    ↓
Claude Code Hooks ────┘              MCP Server ──→ Claude Code
```

**Components:**
- `capture.py` — Telegram bot that receives messages and stores them as memories
- `mcp_server.py` (on the server) — MCP HTTP server exposing `search_brain`, `list_recent`, `add_memory`
- `hooks/` — Claude Code hooks for automatic capture and gate enforcement
- `bulk_import.py` — Import existing data (Claude.ai exports, text files) into Open Brain
- `remote_import.py` — Import data from remote sources

## Claude Code Hooks

The `hooks/` directory contains three Python scripts that integrate Open Brain with [Claude Code](https://docs.anthropic.com/en/docs/claude-code):

### Gate System (search enforcement)

Forces Claude to query Open Brain at the start of every turn before doing any other work. This prevents context loss across sessions.

- **`ob-gate-init.py`** (UserPromptSubmit) — Sets a "pending" flag at the start of each user turn
- **`ob-gate-check.py`** (PreToolUse) — Blocks all tool calls until the flag is satisfied by a targeted Open Brain query. Generic queries (no WHERE/ILIKE) are rejected.

### Capture System (auto-memory)

Automatically captures context from Claude Code sessions into Open Brain.

- **`open-brain-capture.py`** (PostToolUse + Stop) — Sends file writes, edits, significant bash commands, and session summaries to the Open Brain REST API

### Installation

1. Copy the hook files to `~/.claude/hooks/`:
   ```bash
   cp hooks/*.py ~/.claude/hooks/
   ```

2. Merge `hooks/settings.example.json` into your `~/.claude/settings.json`

3. Set your API endpoint:
   ```bash
   export OPEN_BRAIN_API_URL="http://your-open-brain-host:8765/api/add"
   ```

4. Customize `OB_PATTERNS` in `ob-gate-check.py` to match your access methods (SSH hosts, database IPs, etc.)

See `hooks/settings.example.json` for the full Claude Code settings structure.

## Server Setup

Open Brain runs as an LXC container (or any Linux host) with:

- **PostgreSQL** with pgvector extension for semantic search
- **Python venv** with sentence-transformers for embeddings
- **systemd services** for the Telegram bot and MCP server

### Requirements

- PostgreSQL 14+ with pgvector
- Python 3.10+
- ~2GB RAM (for embedding model)

### Database Schema

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE memories (
    id SERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    summary TEXT,
    category TEXT DEFAULT 'general',
    embedding vector(384),
    session_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON memories USING ivfflat (embedding vector_cosine_ops);
```

### Environment Variables

Copy `.env.example` and fill in your values:

```bash
cp .env.example .env
```

Required:
- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `DATABASE_URL` — PostgreSQL connection string
- `ANTHROPIC_API_KEY` — for LLM-powered categorization

## Tools

- **`bulk_import.py`** — Import Claude.ai conversation exports, text files, or JSON chunks
- **`remote_import.py`** — Import from remote sources over SSH
- **`dedup_exact.py`** — Remove exact-duplicate memories
- **`migrate_session_id.sql`** — Add session tracking to older installations

## How the Gate Works

The gate system uses Claude Code's hook protocol to enforce memory retrieval:

1. **UserPromptSubmit** → `ob-gate-init.py` writes `/tmp/claude-ob-gate-{session}` = "pending"
2. **PreToolUse** → `ob-gate-check.py` checks the flag:
   - If "satisfied" → allow the tool call
   - If "pending" → classify the tool call:
     - Targeted OB query (SQL with WHERE, MCP with search terms) → flip to "satisfied", allow
     - Generic OB query (no filters) → **block** with guidance
     - Not OB-related → **block** until OB is queried
     - Capture-only (api/add) → allow but don't flip gate
   - No flag file → fail-open (don't brick sessions)
3. **PostToolUse** → `open-brain-capture.py` sends context to OB after Write/Edit/Bash
4. **Stop** → `open-brain-capture.py` captures session summary + runs capture audit

Every gate decision is audit-logged to `~/.claude/hooks/ob-audit.log` as JSON-lines.

## License

MIT
