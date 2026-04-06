# MCP SERVER IMPLEMENTATION GUIDE

**Parent Spec:** SPEC.md, Task 5
**Purpose:** Exposes the Open Brain database to any MCP-compatible AI client. Runs in two modes: stdio (for Claude Code) and HTTP on port 8765 (for Claude browser). Provides three tools: search_brain, list_recent, add_memory.

---

## Stage 1: MCP Server Setup

### Step 1.1: Initialize MCP Server

**Substep 1.1.1: Create MCP Server Instance**
- Input: Server name string "open-brain"
- Action: `server = Server("open-brain")` using the `mcp` Python SDK
- Output: Server instance
- Test: No exception on instantiation

**Substep 1.1.2: Load Environment and DB Connection**
- Input: `.env` file
- Action: Load dotenv; establish Postgres connection using `DATABASE_URL`; verify connection with `SELECT 1`
- Output: Active DB connection
- Test: `SELECT 1` returns result; connection object is not None

**Substep 1.1.3: Initialize Embedding Model**
- Input: `EMBEDDING_MODEL` env var
- Action: Load sentence-transformers model as singleton (same pattern as capture.py — share the module if possible, or reload)
- Output: Loaded model
- Test: Model loads without exception

---

## Stage 2: search_brain Tool

### Step 2.1: Register and Validate Tool

**Substep 2.1.1: Register Tool with MCP**
- Input: Server instance
- Action: Register `search_brain` tool with schema: `{query: string (required), limit: integer (default 5, max 20)}`
- Output: Tool registered
- Test: Tool appears in server's tool list

**Substep 2.1.2: Validate Input Parameters**
- Input: Tool call arguments dict
- Action: Check `query` is non-empty string; check `limit` is int 1-20; if invalid, return MCP error response (not exception)
- Output: Validated query string, validated limit int
- Test: Empty query → MCP error with message; limit=100 → clamped to 20

### Step 2.2: Execute Semantic Search

**Substep 2.2.1: Generate Query Embedding**
- Input: Query string
- Action: `embedding = model.encode(query).tolist()`
- Output: List of 384 floats
- Test: Output length is 384

**Substep 2.2.2: Run Cosine Similarity Query**
- Input: Query embedding, limit
- Action: Execute SQL:
  ```sql
  SELECT id, raw_text, category, people, topics, action_item, 
         created_at, confidence,
         1 - (embedding <=> %s::vector) as similarity
  FROM memories
  WHERE embedding IS NOT NULL
  ORDER BY embedding <=> %s::vector
  LIMIT %s
  ```
- Output: List of result rows
- Test: Query for "Sarah career" returns the Sarah/job memory with similarity > 0.7

**Substep 2.2.3: Handle No Results**
- Input: Empty result list
- Action: Return formatted message: `"No memories found matching '{query}'. Try different search terms."`
- Test: Search for "xyzzyplugh" returns no-results message, not empty response

### Step 2.3: Format Search Results

**Substep 2.3.1: Format Each Result Row**
- Input: Single result row dict
- Action: Format as:
  ```
  [{category} | {created_at.strftime('%Y-%m-%d')}] {raw_text}
  Similarity: {similarity:.2f} | People: {', '.join(people) or 'none'} | Topics: {', '.join(topics[:3]) or 'none'}
  ```
- Output: Formatted string for one result
- Test: Output contains category, date, raw_text, similarity score

**Substep 2.3.2: Combine Results into Response**
- Input: List of formatted result strings
- Action: Join with `\n\n`; prepend `"Found {n} memories:\n\n"`
- Output: Complete response string
- Test: Two results → response contains "Found 2 memories"

---

## Stage 3: list_recent Tool

### Step 3.1: Register and Execute

**Substep 3.1.1: Register Tool**
- Input: Server instance
- Action: Register `list_recent` with schema: `{days: integer (default 7), category: string (optional, one of the 6 categories)}`
- Output: Tool registered
- Test: Tool appears in tool list

**Substep 3.1.2: Build and Execute Query**
- Input: Validated days int, optional category string
- Action: Build query:
  ```sql
  SELECT id, raw_text, category, created_at, confidence
  FROM memories
  WHERE created_at >= NOW() - INTERVAL '%s days'
  [AND category = %s IF category provided]
  ORDER BY created_at DESC
  LIMIT 50
  ```
- Output: Result rows
- Test: Returns rows from last N days; category filter works when provided

**Substep 3.1.3: Format and Return**
- Input: Result rows
- Action: Format each as `"[{date}] [{category}] {raw_text[:100]}{'...' if len > 100 else ''}"`; join with newlines
- Output: Formatted string
- Test: Each line starts with date in brackets

---

## Stage 4: add_memory Tool

### Step 4.1: Register and Execute

**Substep 4.1.1: Register Tool**
- Input: Server instance
- Action: Register `add_memory` with schema: `{text: string (required)}`
- Output: Tool registered

**Substep 4.1.2: Call Capture Pipeline**
- Input: Text string
- Action: Import and call the same `capture()` function used by the bot (do not duplicate logic)
- Output: Result dict from capture pipeline
- Test: Memory appears in DB after tool call

**Substep 4.1.3: Return Confirmation**
- Input: Result dict
- Action: Return `result["confirmation_message"]`
- Test: Returned string contains category and summary

---

## Stage 5: Transport Layer

This is the highest-risk stage. The stdio and HTTP transports have different initialization patterns and both must work correctly.

### Step 5.1: stdio Transport (for Claude Code)

**Substep 5.1.1: Implement stdio Entry Point**
- Input: None (invoked directly by Claude Code as subprocess)
- Action: Use `mcp.server.stdio.stdio_server()` context manager; run server event loop
- Output: Server listening on stdin/stdout
- Test: Run `echo '{"method":"tools/list"}' | python mcp_server.py` — returns tool list JSON

**Substep 5.1.2: Generate Claude Code MCP Config**
- Input: Server file path, env vars
- Action: Print the exact JSON block the user needs to add to `~/.claude/claude.json`:
  ```json
  {
    "mcpServers": {
      "open-brain": {
        "command": "python3",
        "args": ["/home/<user>/open-brain/mcp_server.py", "--stdio"],
        "env": {
          "ANTHROPIC_API_KEY": "<key>",
          "DATABASE_URL": "postgresql://open-brain:open-brain@localhost/open-brain",
          "EMBEDDING_MODEL": "all-MiniLM-L6-v2"
        }
      }
    }
  }
  ```
- Output: README section with exact config for this server's paths
- Test: Claude Code shows "open-brain" in available MCP servers after config added

### Step 5.2: HTTP Transport (for Claude Browser)

**Substep 5.2.1: Implement HTTP Entry Point**
- Input: `--http` flag or `MCP_HTTP_PORT` env var
- Action: Use `mcp.server.sse` or equivalent HTTP transport; bind to `0.0.0.0:{MCP_HTTP_PORT}`
- Output: Server listening on HTTP port
- Test: `curl http://localhost:8765/` returns valid MCP response

**Substep 5.2.2: Handle Port Already in Use**
- Input: `OSError: Address already in use`
- Action: Log clear error: `"Port {port} is already in use. Check if another instance is running: lsof -i :{port}"`; exit with code 1
- Test: Starting second instance produces helpful error message

### Step 5.3: Mode Selection

**Substep 5.3.1: Parse Mode from Arguments**
- Input: `sys.argv`
- Action: If `--stdio` in args → run stdio mode; if `--http` in args → run HTTP mode; if neither → default to stdio (Claude Code is primary use case)
- Output: Correct transport started
- Test: `python mcp_server.py --stdio` starts stdio; `python mcp_server.py --http` starts HTTP

---

## TESTING CHECKLIST

- [ ] `python mcp_server.py --stdio` starts without exception
- [ ] tools/list response contains search_brain, list_recent, add_memory
- [ ] `search_brain` with query "Sarah" returns the test memory from CAPTURE_GUIDE testing
- [ ] `search_brain` with nonsense query returns no-results message (not error)
- [ ] `list_recent` with days=7 returns memories from last 7 days
- [ ] `add_memory` with test text creates new DB row
- [ ] HTTP mode starts on port 8765 without exception
- [ ] Claude Code config added and "open-brain" appears in Claude Code's MCP server list
- [ ] Claude Code can call `search_brain` and receive results

If ANY item is unchecked, the MCP server is incomplete. Do not proceed to Task 6.
