# CAPTURE PIPELINE IMPLEMENTATION GUIDE

**Parent Spec:** SPEC.md, Task 3
**Purpose:** Takes raw text input, extracts structured metadata via Claude API, generates a local vector embedding, stores everything in Postgres, and returns a result dict. This is the core of the entire system — every other component depends on it working correctly and failing gracefully.

---

## Stage 1: Input Validation

### Step 1.1: Validate and Clean Input Text

**Substep 1.1.1: Reject Empty Input**
- Input: Raw string from caller
- Action: Check if string is None, empty, or whitespace-only
- Output: Raise `ValueError("Input text cannot be empty")` if invalid, else pass through
- Test: `capture("")` raises ValueError; `capture("  ")` raises ValueError; `capture("hello")` does not raise

**Substep 1.1.2: Trim and Normalize**
- Input: Non-empty raw string
- Action: Strip leading/trailing whitespace, collapse internal multiple spaces to single space
- Output: Clean string ready for processing
- Test: `"  hello   world  "` → `"hello world"`

---

## Stage 2: Claude Metadata Extraction

### Step 2.1: Build Claude API Request

**Substep 2.1.1: Construct Classification Prompt**
- Input: Clean text string
- Action: Insert text into the classification prompt template (see prompt below)
- Output: Complete prompt string ready for API call
- Test: Output contains the input text; output contains the JSON schema definition

**Classification Prompt Template:**
```
You are a personal knowledge classifier. Extract structured metadata from this thought or note.

Return ONLY valid JSON with these exact fields:
{
  "category": "<one of: person | project | idea | decision | admin | general>",
  "people": ["<name>", ...],
  "topics": ["<topic>", ...],
  "action_item": "<specific next action if present, else null>",
  "summary": "<one sentence summary, max 15 words>",
  "confidence": <float 0.0-1.0>
}

No explanation. No markdown. No code blocks. JSON only. If uncertain, use "general" category and lower confidence.

Input: {text}
```

**Substep 2.1.2: Call Claude API**
- Input: Complete prompt string
- Action: Call `anthropic.messages.create` with model `claude-haiku-4-5-20251001`, max_tokens 300, the prompt as user message
- Output: Raw API response object
- Test: Response has `.content[0].text` attribute; no exception thrown
- Note: Use Haiku not Sonnet — this runs on every capture, cost matters

**Substep 2.1.3: Extract Text from Response**
- Input: Raw API response object
- Action: Extract `response.content[0].text`, strip whitespace
- Output: Raw JSON string
- Test: Output is a non-empty string; does not contain "```"

### Step 2.2: Parse and Validate Claude Response

**Substep 2.2.1: Parse JSON**
- Input: Raw JSON string from Claude
- Action: Call `json.loads()` wrapped in try/except
- Output: Python dict on success; `None` on `json.JSONDecodeError`
- Test: Valid JSON string → dict; `"not json"` → None; `""` → None

**Substep 2.2.2: Validate Required Fields**
- Input: Parsed dict (or None)
- Action: Check that dict is not None and contains all required keys: category, people, topics, action_item, summary, confidence
- Output: Validated dict on success; `None` if any field missing
- Test: Complete dict → same dict returned; dict missing "confidence" → None

**Substep 2.2.3: Validate Field Values**
- Input: Validated dict
- Action: Coerce/validate each field:
  - `category`: must be one of the 6 allowed values; if not, set to "general"
  - `people`: must be list; if string, wrap in list; if None, set to []
  - `topics`: must be list; if None, set to []
  - `action_item`: must be string or None
  - `summary`: must be non-empty string; if empty, set to first 50 chars of input text
  - `confidence`: must be float 0.0–1.0; if outside range, clamp to range
- Output: Fully coerced metadata dict
- Test: `category="PERSON"` → `"person"`; `people=None` → `[]`; `confidence=1.5` → `1.0`

**Substep 2.2.4: Handle Total Claude Failure**
- Input: Any exception from steps 2.1.2 through 2.2.3
- Action: Catch all exceptions; construct fallback metadata dict with category="general", empty lists, summary=first 50 chars of input, confidence=0.0
- Output: Fallback metadata dict with `_error` key containing exception message
- Test: When Claude API is mocked to raise an exception, function returns fallback dict not exception

---

## Stage 3: Embedding Generation

### Step 3.1: Load Embedding Model

**Substep 3.1.1: Load Model (Singleton)**
- Input: Model name string from env var `EMBEDDING_MODEL`
- Action: Load `SentenceTransformer(model_name)` — use module-level singleton so model loads once per process, not once per capture
- Output: Loaded model object
- Test: First call loads model; second call returns same object without reloading; model directory exists in `~/open-brain/models/`

**Substep 3.1.2: Handle Model Load Failure**
- Input: Exception from model load
- Action: Log full traceback; raise `RuntimeError("Embedding model failed to load: {e}")` — this is a fatal startup error, not a per-request error
- Test: When model path is invalid, RuntimeError is raised with descriptive message

### Step 3.2: Generate Embedding

**Substep 3.2.1: Encode Text**
- Input: Clean text string, loaded model
- Action: Call `model.encode(text, convert_to_numpy=True)`, convert result to Python list with `.tolist()`
- Output: List of 384 floats
- Test: Output is a list; `len(output) == 384`; all elements are floats; same input always produces same output

**Substep 3.2.2: Handle Encoding Failure**
- Input: Exception from encode
- Action: Log error; return `None` (embedding is optional — memory can be stored without it, just won't be searchable by semantic similarity)
- Test: When model.encode is mocked to raise, function returns None not exception

---

## Stage 4: Database Storage

### Step 4.1: Build Database Record

**Substep 4.1.1: Construct Memory Row Dict**
- Input: Clean text, metadata dict, embedding list (or None)
- Action: Build dict with all columns: raw_text, embedding, category, people, topics, action_item, summary (from metadata), source="telegram", confidence
- Output: Complete row dict ready for INSERT
- Test: All required keys present; embedding is either list of 384 floats or None

### Step 4.2: Insert Memory Row

**Substep 4.2.1: Execute INSERT**
- Input: Row dict, database connection
- Action: Execute parameterized INSERT into `memories` table, use `%s::vector` cast for embedding column, return the new row's `id`
- Output: Integer memory ID
- Test: Row exists in DB after insert; `SELECT COUNT(*) FROM memories` increases by 1

**Substep 4.2.2: Handle Insert Failure**
- Input: Exception from INSERT
- Action: Rollback transaction; log full error; raise `RuntimeError("Database insert failed: {e}")` — caller handles this
- Test: When DB connection is mocked to fail, RuntimeError is raised and no partial row left in DB

### Step 4.3: Insert Inbox Log Row

**Substep 4.3.1: Log the Capture**
- Input: Memory ID, raw input text, status string, telegram message ID (optional)
- Action: INSERT into `inbox_log` table with all fields
- Output: Log row created
- Test: inbox_log row exists after capture with correct memory_id foreign key

**Substep 4.3.2: Handle Log Failure Gracefully**
- Input: Exception from inbox_log INSERT
- Action: Log warning but do NOT raise — the memory was already saved successfully, the log is audit trail not critical path
- Test: When inbox_log INSERT is mocked to fail, capture still returns success result

---

## Stage 5: Build and Return Result

### Step 5.1: Construct Result Dict

**Substep 5.1.1: Build Success Result**
- Input: Memory ID, metadata dict
- Action: Build result dict: `{success: True, memory_id: int, category: str, summary: str, confidence: float, needs_review: bool}`
- `needs_review` is True when confidence < 0.6 OR metadata had `_error` key
- Output: Result dict
- Test: confidence 0.91 → needs_review False; confidence 0.31 → needs_review True

**Substep 5.1.2: Build Confirmation Message String**
- Input: Result dict
- Action: Format human-readable string for Telegram reply:
  - If needs_review=False: `"✓ Filed as {category} | \"{summary}\" | confidence: {confidence:.2f}\nReply 'fix: <correction>' if wrong"`
  - If needs_review=True: `"? Couldn't classify confidently ({confidence:.2f}).\nRepost with a prefix: person: / project: / idea: / decision: / admin:"`
- Output: String ready to send as Telegram message
- Test: Both branches produce non-empty strings; confidence shown to 2 decimal places

---

## Public Interface

The capture pipeline exposes a single async function:

```python
async def capture(text: str, telegram_message_id: int = None) -> dict:
    """
    Process text through the full capture pipeline.
    Returns result dict with keys: success, memory_id, category, summary, 
    confidence, needs_review, confirmation_message
    Never raises — all errors result in needs_review=True with error details.
    """
```

---

## TESTING CHECKLIST

Run `python test_capture.py` — all items must pass:

- [ ] `capture("Met Sarah today, she's thinking about leaving her job")` returns success=True
- [ ] Returned category is "person"
- [ ] "Sarah" appears in people list
- [ ] Confidence is >= 0.6
- [ ] Memory row exists in DB with correct fields
- [ ] Inbox log row exists with correct memory_id
- [ ] `capture("")` raises ValueError
- [ ] `capture("that thing")` returns needs_review=True (low confidence)
- [ ] Embedding in DB has 384 dimensions
- [ ] Semantic search for "career" finds the Sarah memory

If ANY item is unchecked, the capture pipeline is incomplete. Do not proceed to Task 4.
