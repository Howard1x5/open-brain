# BOT IMPLEMENTATION GUIDE

**Parent Spec:** SPEC.md, Task 4
**Purpose:** Telegram bot that receives text and voice messages from the allowed user, routes them through the capture pipeline, handles voice transcription, sends confirmation replies, and processes fix: correction commands.

---

## Stage 1: Bot Initialization

### Step 1.1: Load and Validate Configuration

**Substep 1.1.1: Load Environment Variables**
- Input: `.env` file
- Action: Load with `python-dotenv`; extract `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`, `DATABASE_URL`, `ANTHROPIC_API_KEY`
- Output: Config dict with all four values
- Test: All four keys present and non-empty; raise `EnvironmentError` listing which keys are missing if any are absent

**Substep 1.1.2: Validate Allowed User ID is Integer**
- Input: `TELEGRAM_ALLOWED_USER_ID` string from env
- Action: Cast to int; raise `ValueError` if not castable
- Output: Integer user ID
- Test: `"123456789"` → `123456789`; `"abc"` → ValueError with message "TELEGRAM_ALLOWED_USER_ID must be a numeric Telegram user ID"

### Step 1.2: Initialize Components

**Substep 1.2.1: Initialize Capture Pipeline**
- Input: Config
- Action: Import capture module; trigger embedding model load at startup (not on first message)
- Output: Capture module ready; embedding model loaded and cached
- Test: No exception on startup; log line "Embedding model loaded" appears before first message

**Substep 1.2.2: Build Telegram Application**
- Input: Bot token
- Action: `Application.builder().token(token).build()`
- Output: Telegram Application object
- Test: No exception; application object has `.run_polling` method

---

## Stage 2: Message Authorization

### Step 2.1: Authorization Check (Applied to ALL handlers)

**Substep 2.1.1: Extract Sender User ID**
- Input: Telegram Update object
- Action: Extract `update.effective_user.id`
- Output: Integer sender ID
- Test: Returns correct ID for real messages

**Substep 2.1.2: Compare Against Allowed ID**
- Input: Sender ID, allowed ID from config
- Action: If sender ID != allowed ID: log warning with sender ID; return immediately without processing or replying
- Output: None (silent ignore for unauthorized); continue for authorized
- Test: Message from wrong ID produces no reply and no DB write; message from correct ID proceeds

---

## Stage 3: Text Message Handler

### Step 3.1: Handle Incoming Text

**Substep 3.1.1: Check for fix: Prefix**
- Input: Message text string
- Action: Check if text starts with `fix:` (case-insensitive, strip whitespace)
- Output: Boolean — is this a correction command?
- Test: `"fix: this should be a project"` → True; `"fix: "` → True; `"great idea"` → False

**Substep 3.1.2: Route to Correct Handler**
- Input: Boolean from 3.1.1, update object
- Action: If fix: → call correction handler (Stage 5); else → call capture handler (Step 3.2)
- Output: Routed to correct handler
- Test: fix: message goes to correction handler; regular text goes to capture

### Step 3.2: Process Text Capture

**Substep 3.2.1: Call Capture Pipeline**
- Input: Message text, telegram message ID
- Action: `await capture(text, telegram_message_id=message.message_id)`
- Output: Result dict from capture pipeline
- Test: Result dict has all expected keys

**Substep 3.2.2: Send Confirmation Reply**
- Input: Result dict, update object
- Action: `await update.message.reply_text(result["confirmation_message"])`
- Output: Telegram reply sent
- Test: User receives reply within 10 seconds of sending message

**Substep 3.2.3: Handle Capture Exception**
- Input: Any exception from capture pipeline
- Action: Log full traceback; send Telegram reply: `"⚠️ Something went wrong storing that. Please try again."`
- Output: User notified; no crash
- Test: When capture is mocked to raise, bot sends error message and stays running

---

## Stage 4: Voice Message Handler

### Step 4.1: Download Voice File

**Substep 4.1.1: Get File Object from Telegram**
- Input: Update with voice message
- Action: `file = await update.message.voice.get_file()`
- Output: Telegram File object with `file_path`
- Test: File object has valid file_path attribute

**Substep 4.1.2: Download to Temp File**
- Input: Telegram File object
- Action: Create temp file with `.ogg` suffix using `tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)`; download with `await file.download_to_drive(temp_path)`
- Output: Local file path string pointing to downloaded .ogg file
- Test: File exists at path after download; file size > 0

**Substep 4.1.3: Handle Download Failure**
- Input: Exception from download
- Action: Send Telegram reply: `"⚠️ Could not download voice message. Please try again."`; clean up temp file if it exists; return
- Test: When download is mocked to fail, user gets error message, no crash

### Step 4.2: Transcribe Voice File

**Substep 4.2.1: Load Whisper Model (Singleton)**
- Input: Model name from env var `WHISPER_MODEL` (default: "base")
- Action: Load `whisper.load_model(model_name)` — singleton, load once per process
- Output: Loaded Whisper model
- Test: Model loads without exception; second call returns cached model

**Substep 4.2.2: Transcribe Audio File**
- Input: Local .ogg file path, loaded Whisper model
- Action: `result = model.transcribe(file_path)`; extract `result["text"]`; strip whitespace
- Output: Transcript string
- Test: Known audio file produces non-empty transcript

**Substep 4.2.3: Validate Transcript**
- Input: Transcript string
- Action: If empty or whitespace-only: send reply `"🎙 Could not transcribe audio. Please try again or send as text."`; clean up temp file; return
- Output: Valid transcript string, or early return
- Test: Empty transcript triggers error reply; non-empty passes through

**Substep 4.2.4: Clean Up Temp File**
- Input: Temp file path
- Action: `os.unlink(temp_path)` wrapped in try/except (never crash on cleanup failure)
- Output: Temp file deleted
- Test: File no longer exists after processing

### Step 4.3: Process Voice Capture

**Substep 4.3.1: Prepend Voice Indicator to Text**
- Input: Transcript string
- Action: Prepend `"[voice] "` to transcript before passing to capture pipeline
- Output: Tagged text string
- Test: Stored raw_text in DB starts with "[voice] "

**Substep 4.3.2: Call Capture Pipeline**
- Input: Tagged transcript text, telegram message ID
- Action: Same as text capture — `await capture(text, telegram_message_id=message.message_id)`
- Output: Result dict
- Test: Result dict has all expected keys

**Substep 4.3.3: Send Voice Confirmation Reply**
- Input: Transcript, result dict
- Action: Send reply showing transcript AND filing confirmation:
  `"🎙 Transcribed: \"{transcript[:100]}{'...' if len(transcript) > 100 else ''}\"\n\n{result['confirmation_message']}"`
- Output: Telegram reply sent
- Test: Reply contains both the transcript preview and the filing confirmation

---

## Stage 5: Correction Handler

### Step 5.1: Parse Correction Command

**Substep 5.1.1: Extract Correction Text**
- Input: Message text (starts with "fix:")
- Action: Strip "fix:" prefix (case-insensitive), strip whitespace from remainder
- Output: Correction text string
- Test: `"fix: this should be a project"` → `"this should be a project"`; `"FIX:  idea about the API"` → `"idea about the API"`

**Substep 5.1.2: Find Original Memory via Reply**
- Input: Update object
- Action: Check if this message is a reply (`update.message.reply_to_message`); if so, get the replied-to message ID; look up `inbox_log` for a row with matching `telegram_message_id`; get the `memory_id`
- Output: Integer memory ID, or None if not found
- Test: Reply to bot confirmation message → correct memory_id found; non-reply fix: → None

**Substep 5.1.3: Handle Memory Not Found**
- Input: None from 5.1.2
- Action: Send reply: `"? Couldn't find the original memory. Try replying directly to the filing confirmation message."`; return
- Test: fix: sent as new message (not reply) triggers this response

### Step 5.2: Update Memory

**Substep 5.2.1: Re-run Capture on Correction Text**
- Input: Correction text string
- Action: Call capture pipeline on correction text to get new metadata and embedding
- Output: New result dict
- Test: Result dict has success=True

**Substep 5.2.2: Update Existing Memory Row**
- Input: Memory ID, new metadata, new embedding
- Action: UPDATE memories SET category=..., people=..., topics=..., action_item=..., summary=..., confidence=..., embedding=..., raw_text=... WHERE id=memory_id
- Output: Row updated in DB
- Test: SELECT on memory_id shows new values

**Substep 5.2.3: Send Update Confirmation**
- Input: New result dict
- Action: Send reply: `"✓ Updated: {result['confirmation_message']}"`
- Test: User receives confirmation of updated filing

---

## TESTING CHECKLIST

Manually test each of these before marking Task 4 complete:

- [ ] Send a text message → receive ✓ filing confirmation
- [ ] Send a voice note → receive 🎙 transcription + filing confirmation  
- [ ] Send message from a different Telegram account → receive no reply
- [ ] Reply to a confirmation with `fix: this should be a project` → receive update confirmation
- [ ] Send `fix:` as a new message (not a reply) → receive helpful error
- [ ] Send an empty-sounding voice note → receive error message, bot stays running
- [ ] Kill and restart bot service → first message after restart still works (model reloads)

If ANY item is unchecked, the bot is incomplete. Do not proceed to Task 5.
