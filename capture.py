"""
Capture Pipeline for Open Brain
Processes text through metadata extraction (Claude API), embedding generation, and database storage.
Supports automatic splitting of long brain dumps into individual thoughts.
"""

import os
import re
import json
import uuid
import logging
from typing import Optional

import anthropic
import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Valid categories
VALID_CATEGORIES = {"person", "project", "idea", "decision", "admin", "general"}

# Splitter config
SPLIT_THRESHOLD = 50  # words — anything shorter is already a single thought

# Module-level singletons
_embedding_model: Optional[SentenceTransformer] = None
_anthropic_client: Optional[anthropic.Anthropic] = None


def get_embedding_model() -> SentenceTransformer:
    """Load embedding model as singleton."""
    global _embedding_model
    if _embedding_model is None:
        model_name = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        logger.info(f"Loading embedding model: {model_name}")
        try:
            _embedding_model = SentenceTransformer(model_name)
            logger.info("Embedding model loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            raise RuntimeError(f"Embedding model failed to load: {e}")
    return _embedding_model


def get_anthropic_client() -> anthropic.Anthropic:
    """Get Anthropic client as singleton."""
    global _anthropic_client
    if _anthropic_client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY not set")
        _anthropic_client = anthropic.Anthropic(api_key=api_key)
    return _anthropic_client


def get_db_connection():
    """Get database connection."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise EnvironmentError("DATABASE_URL not set")
    return psycopg2.connect(database_url)


# Stage 1: Input Validation

def validate_input(text: str) -> str:
    """Validate and clean input text."""
    # Step 1.1.1: Reject empty input
    if text is None or not text.strip():
        raise ValueError("Input text cannot be empty")

    # Step 1.1.2: Trim and normalize
    clean_text = text.strip()
    clean_text = re.sub(r"\s+", " ", clean_text)
    return clean_text


# Stage 1.5: Thought Splitter

SPLIT_PROMPT = """You are a thought splitter. Break this text into separate, self-contained thoughts or ideas.

Rules:
- Each thought should make sense on its own without context from the others
- Preserve the original meaning and detail — do NOT summarize or shorten
- If someone is mentioned, include their name in each relevant thought
- If a thought references a project, include the project name
- Keep action items attached to their relevant thought
- If the entire text is about one topic, return it as a single item
- Preserve any [voice] or [source:] prefixes on the first thought only

Return ONLY a JSON array of strings. No explanation. No markdown. No code blocks.

Input: {text}"""


def split_with_haiku(text: str) -> list[str]:
    """Use Haiku to split long text into individual thoughts."""
    try:
        client = get_anthropic_client()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": SPLIT_PROMPT.format(text=text)}]
        )
        raw = response.content[0].text.strip()

        # Clean markdown code blocks if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        thoughts = json.loads(raw)

        if not isinstance(thoughts, list) or len(thoughts) == 0:
            logger.warning("Splitter returned non-list or empty, falling back to single thought")
            return [text]

        # Filter out empty strings
        thoughts = [t.strip() for t in thoughts if isinstance(t, str) and t.strip()]
        return thoughts if thoughts else [text]

    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"Splitter failed ({type(e).__name__}: {e}), falling back to single thought")
        return [text]


def maybe_split(text: str) -> list[str]:
    """Split long text into individual thoughts. Short text passes through as-is."""
    word_count = len(text.split())
    if word_count <= SPLIT_THRESHOLD:
        return [text]
    return split_with_haiku(text)


# Stage 2: Claude Metadata Extraction

CLASSIFICATION_PROMPT = '''You are a personal knowledge classifier. Extract structured metadata from this thought or note.

Return ONLY valid JSON with these exact fields:
{{
  "category": "<one of: person | project | idea | decision | admin | general>",
  "people": ["<name>", ...],
  "topics": ["<topic>", ...],
  "action_item": "<specific next action if present, else null>",
  "summary": "<one sentence summary, max 15 words>",
  "confidence": <float 0.0-1.0>
}}

No explanation. No markdown. No code blocks. JSON only. If uncertain, use "general" category and lower confidence.

Input: {text}'''


def build_classification_prompt(text: str) -> str:
    """Build the classification prompt with input text."""
    return CLASSIFICATION_PROMPT.format(text=text)


def call_claude_api(prompt: str) -> str:
    """Call Claude API for metadata extraction."""
    client = get_anthropic_client()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()


def parse_claude_response(raw_json: str) -> Optional[dict]:
    """Parse JSON response from Claude."""
    try:
        # Remove any markdown code blocks if present
        cleaned = raw_json.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning(f"Failed to parse JSON from Claude: {raw_json[:100]}")
        return None


def validate_metadata_fields(data: Optional[dict]) -> Optional[dict]:
    """Validate that all required fields are present."""
    if data is None:
        return None

    required_keys = {"category", "people", "topics", "action_item", "summary", "confidence"}
    if not all(key in data for key in required_keys):
        missing = required_keys - set(data.keys())
        logger.warning(f"Missing required fields: {missing}")
        return None

    return data


def coerce_metadata_values(data: dict, original_text: str) -> dict:
    """Coerce and validate field values."""
    result = {}

    # Category: must be one of valid values
    cat = str(data.get("category", "general")).lower()
    result["category"] = cat if cat in VALID_CATEGORIES else "general"

    # People: must be list
    people = data.get("people")
    if people is None:
        result["people"] = []
    elif isinstance(people, str):
        result["people"] = [people] if people else []
    else:
        result["people"] = list(people)

    # Topics: must be list
    topics = data.get("topics")
    result["topics"] = list(topics) if topics else []

    # Action item: string or None
    action = data.get("action_item")
    result["action_item"] = str(action) if action else None

    # Summary: non-empty string
    summary = data.get("summary")
    if not summary or not str(summary).strip():
        result["summary"] = original_text[:50] + ("..." if len(original_text) > 50 else "")
    else:
        result["summary"] = str(summary).strip()

    # Confidence: float 0.0-1.0
    try:
        conf = float(data.get("confidence", 0.0))
        result["confidence"] = max(0.0, min(1.0, conf))
    except (TypeError, ValueError):
        result["confidence"] = 0.0

    return result


def extract_metadata(text: str) -> dict:
    """Extract metadata using Claude API with full error handling."""
    try:
        prompt = build_classification_prompt(text)
        raw_response = call_claude_api(prompt)
        parsed = parse_claude_response(raw_response)
        validated = validate_metadata_fields(parsed)

        if validated is None:
            # Fallback for parse/validation failures
            logger.warning("Claude response validation failed, using fallback")
            return {
                "category": "general",
                "people": [],
                "topics": [],
                "action_item": None,
                "summary": text[:50] + ("..." if len(text) > 50 else ""),
                "confidence": 0.0,
                "_error": "Failed to parse Claude response"
            }

        return coerce_metadata_values(validated, text)

    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return {
            "category": "general",
            "people": [],
            "topics": [],
            "action_item": None,
            "summary": text[:50] + ("..." if len(text) > 50 else ""),
            "confidence": 0.0,
            "_error": str(e)
        }


# Stage 3: Embedding Generation

def generate_embedding(text: str) -> Optional[list]:
    """Generate embedding vector for text."""
    try:
        model = get_embedding_model()
        embedding = model.encode(text, convert_to_numpy=True)
        return embedding.tolist()
    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        return None


# Stage 4: Database Storage

def insert_memory(conn, text: str, metadata: dict, embedding: Optional[list],
                  session_id: Optional[str] = None) -> int:
    """Insert memory row into database."""
    with conn.cursor() as cur:
        # Format embedding for pgvector
        embedding_str = str(embedding) if embedding else None

        cur.execute("""
            INSERT INTO memories (raw_text, embedding, category, people, topics,
                                  action_item, summary, confidence, source, needs_review, session_id)
            VALUES (%s, %s::vector, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            text,
            embedding_str,
            metadata["category"],
            metadata["people"],
            metadata["topics"],
            metadata["action_item"],
            metadata["summary"],
            metadata["confidence"],
            "telegram",
            metadata.get("_error") is not None or metadata["confidence"] < 0.6,
            session_id
        ))

        memory_id = cur.fetchone()[0]
        conn.commit()
        return memory_id


def insert_inbox_log(conn, memory_id: int, raw_input: str, status: str,
                     telegram_message_id: Optional[int] = None):
    """Insert inbox log row (non-critical)."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO inbox_log (memory_id, telegram_message_id, raw_input, status)
                VALUES (%s, %s, %s, %s)
            """, (memory_id, telegram_message_id, raw_input, status))
            conn.commit()
    except Exception as e:
        logger.warning(f"Failed to insert inbox log: {e}")
        # Don't raise - inbox log is not critical


# Stage 5: Build and Return Result

def build_result(memory_id: int, metadata: dict) -> dict:
    """Build the result dictionary."""
    needs_review = metadata["confidence"] < 0.6 or "_error" in metadata

    return {
        "success": True,
        "memory_id": memory_id,
        "category": metadata["category"],
        "summary": metadata["summary"],
        "confidence": metadata["confidence"],
        "needs_review": needs_review
    }


def build_confirmation_message(result: dict) -> str:
    """Build human-readable confirmation message."""
    if result["needs_review"]:
        return (
            f"? Couldn't classify confidently ({result['confidence']:.2f}).\n"
            f"Repost with a prefix: person: / project: / idea: / decision: / admin:"
        )
    else:
        return (
            f"✓ Filed as {result['category']} | \"{result['summary']}\" | "
            f"confidence: {result['confidence']:.2f}\n"
            f"Reply 'fix: <correction>' if wrong"
        )


# Public Interface

async def capture_single(text: str, telegram_message_id: Optional[int] = None,
                         session_id: Optional[str] = None) -> dict:
    """
    Process a single thought through the capture pipeline.
    This is the core pipeline — one thought in, one memory out.
    """
    conn = None
    try:
        # Stage 2: Claude Metadata Extraction
        metadata = extract_metadata(text)

        # Stage 3: Embedding Generation
        embedding = generate_embedding(text)

        # Stage 4: Database Storage
        conn = get_db_connection()
        memory_id = insert_memory(conn, text, metadata, embedding, session_id)

        status = "needs_review" if metadata.get("_error") or metadata["confidence"] < 0.6 else "success"
        insert_inbox_log(conn, memory_id, text, status, telegram_message_id)

        # Stage 5: Build Result
        result = build_result(memory_id, metadata)
        result["confirmation_message"] = build_confirmation_message(result)

        return result

    except Exception as e:
        logger.error(f"Capture pipeline error: {e}")
        return {
            "success": False,
            "memory_id": None,
            "category": "general",
            "summary": text[:50] if text else "",
            "confidence": 0.0,
            "needs_review": True,
            "confirmation_message": "⚠️ Something went wrong storing that. Please try again.",
            "_error": str(e)
        }

    finally:
        if conn:
            conn.close()


async def capture(text: str, telegram_message_id: Optional[int] = None) -> dict:
    """
    Process text through the full capture pipeline.
    Long inputs are automatically split into individual thoughts.
    Returns result dict with keys: success, memory_id, category, summary,
    confidence, needs_review, confirmation_message
    For multi-thought inputs, returns a combined result with all thoughts.
    Never raises — all errors result in needs_review=True with error details.
    """
    try:
        # Stage 1: Input Validation
        clean_text = validate_input(text)

        # Stage 1.5: Split long input into individual thoughts
        thoughts = maybe_split(clean_text)

        if len(thoughts) == 1:
            # Single thought — standard pipeline
            return await capture_single(clean_text, telegram_message_id)

        # Multiple thoughts — process each with shared session_id
        session_id = str(uuid.uuid4())
        results = []

        for thought in thoughts:
            result = await capture_single(thought, telegram_message_id, session_id)
            results.append(result)

        # Build combined result
        successful = [r for r in results if r.get("success")]
        failed = [r for r in results if not r.get("success")]

        # Build combined confirmation message
        lines = [f"🧠 Split into {len(thoughts)} thoughts:"]
        for r in results:
            if r.get("success"):
                lines.append(
                    f"  ✓ {r['category']} | \"{r['summary']}\" | {r['confidence']:.2f}"
                )
            else:
                lines.append(f"  ⚠️ Failed to store one thought")
        lines.append("Reply 'fix: <correction>' to any filing above")

        return {
            "success": len(successful) > 0,
            "memory_id": successful[0]["memory_id"] if successful else None,
            "category": "multiple",
            "summary": f"Brain dump split into {len(thoughts)} thoughts",
            "confidence": sum(r["confidence"] for r in successful) / len(successful) if successful else 0.0,
            "needs_review": any(r.get("needs_review") for r in results),
            "confirmation_message": "\n".join(lines),
            "session_id": session_id,
            "thought_count": len(thoughts),
            "results": results
        }

    except ValueError as e:
        # Input validation error - re-raise
        raise

    except Exception as e:
        logger.error(f"Capture pipeline error: {e}")
        return {
            "success": False,
            "memory_id": None,
            "category": "general",
            "summary": text[:50] if text else "",
            "confidence": 0.0,
            "needs_review": True,
            "confirmation_message": "⚠️ Something went wrong storing that. Please try again.",
            "_error": str(e)
        }


# Preload embedding model on import
def preload_model():
    """Preload the embedding model at startup."""
    try:
        get_embedding_model()
    except Exception as e:
        logger.error(f"Failed to preload embedding model: {e}")
