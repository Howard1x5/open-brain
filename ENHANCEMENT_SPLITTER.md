# Enhancement: Voice/Text Dump Splitter

**Parent Spec:** SPEC.md, Task 3 (Capture Pipeline)
**Priority:** High — directly improves the primary input workflow
**Effort:** Small (~30 lines in capture.py + schema migration for session_id)

---

## Problem

A 4-minute voice note or long text dump produces a single memory with one embedding vector. Semantic search works best on focused, single-topic chunks. A rambling dump that touches 5 topics gets a diluted embedding that matches weakly against any individual topic search.

## Solution

Add a **split step** after transcription but before the main capture pipeline. When input text exceeds a threshold (e.g., 300+ characters or 50+ words), send it to Haiku to decompose into individual thoughts before processing each one independently.

---

## Implementation

### Step 1: Detect Long Input

In `capture.py`, after input validation and before metadata extraction:

```python
SPLIT_THRESHOLD = 50  # words — anything shorter is already a single thought

async def maybe_split(text: str) -> list[str]:
    """Split long text into individual thoughts. Short text passes through as-is."""
    word_count = len(text.split())
    if word_count <= SPLIT_THRESHOLD:
        return [text]
    return await split_with_haiku(text)
```

### Step 2: Haiku Split Prompt

```python
SPLIT_PROMPT = """You are a thought splitter. Break this text into separate, self-contained thoughts or ideas.

Rules:
- Each thought should make sense on its own without context from the others
- Preserve the original meaning and detail — do NOT summarize or shorten
- If someone is mentioned, include their name in each relevant thought
- If a thought references a project, include the project name
- Keep action items attached to their relevant thought
- If the entire text is about one topic, return it as a single item

Return ONLY a JSON array of strings. No explanation. No markdown.

Input: {text}"""
```

### Step 3: Process Each Thought

```python
async def capture(text: str, telegram_message_id: int = None) -> dict:
    clean_text = validate_and_clean(text)

    # Split long input into individual thoughts
    thoughts = await maybe_split(clean_text)

    # Generate a session_id if multiple thoughts (links them together)
    session_id = str(uuid.uuid4()) if len(thoughts) > 1 else None

    results = []
    for thought in thoughts:
        result = await capture_single(thought, telegram_message_id, session_id)
        results.append(result)

    return build_combined_result(results)
```

### Step 4: Schema Change — Add session_id

```sql
ALTER TABLE memories ADD COLUMN session_id UUID DEFAULT NULL;
CREATE INDEX idx_memories_session_id ON memories(session_id) WHERE session_id IS NOT NULL;
```

This column links thoughts that came from the same brain dump. Query: `SELECT * FROM memories WHERE session_id = %s ORDER BY id` to reconstruct a full dump.

### Step 5: Update Telegram Confirmation

For multi-thought dumps, the bot reply should show what was split:

```
🧠 Split into 4 thoughts:
  ✓ person | "Sarah considering consulting, unhappy post-reorg" | 0.91
  ✓ project | "ARGUS self-improvement loop ready for Sprint 1" | 0.88
  ✓ idea | "SOC triage mode as stepping stone to full automation" | 0.85
  ✓ admin | "Blade server arriving next week, need to buy drives" | 0.93
Reply "fix: <correction>" to any filing confirmation above
```

---

## Edge Cases

- **Single topic, long text:** Haiku returns a single-item array → processed as normal, no session_id
- **Haiku returns invalid JSON:** Fall back to storing the full text as one memory (same as current behavior)
- **Very long dump (2000+ words):** Still works — Haiku can handle the context, and each split thought goes through normal capture
- **Text input (not voice):** Same logic applies — long text messages get split too
- **fix: correction on a split:** User replies to one of the split confirmations — correction applies to that specific memory only

## Cost

- One additional Haiku call per long input (~$0.0001 per split)
- Slightly more DB rows (desirable — better search granularity)
- No additional embedding model calls beyond what each thought already requires

---

## Testing

- [ ] Short text (<50 words) passes through without splitting
- [ ] 4-minute voice transcript (~600 words) splits into 3-6 thoughts
- [ ] Each split thought has correct category, people, topics
- [ ] All thoughts from one dump share the same session_id
- [ ] Single-topic long text returns as one thought (not artificially split)
- [ ] Haiku failure falls back to single-memory behavior
- [ ] Telegram confirmation shows all split thoughts
- [ ] `search_brain("blade server")` finds the specific thought, not the whole dump
