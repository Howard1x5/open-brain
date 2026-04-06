#!/usr/bin/env python3
"""
bulk_import.py — Scan local directories, extract meaningful chunks, and import into Open Brain.

Usage:
    python bulk_import.py scan                          # Show what files would be processed
    python bulk_import.py scan -v                       # Show individual file paths
    python bulk_import.py extract                       # Extract chunks to staging file
    python bulk_import.py extract --out chunks.json
    python bulk_import.py extract-claude                # Extract Claude.ai export data
    python bulk_import.py extract-claude --out claude_chunks.json
    python bulk_import.py merge a.json b.json -o all.json  # Merge staging files
    python bulk_import.py import staged.json            # Import staged chunks into Open Brain
    python bulk_import.py import staged.json --dry-run
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Customize these paths to point at your own directories to scan
SCAN_DIRECTORIES = [
    Path.home() / "Documents",
]

# Path to Claude.ai data export (download from claude.ai/settings)
CLAUDE_EXPORT_DIR = Path.home() / "claude-export"

# File extensions to process, grouped by handler type
HANDLER_MAP = {
    "markdown": [".md"],
    "python": [".py"],
    "shell": [".sh", ".bash", ".zsh"],
    "yaml": [".yaml", ".yml"],
    "text": [".txt"],
    "json_config": [".json"],
}

ALL_EXTENSIONS = set()
for exts in HANDLER_MAP.values():
    ALL_EXTENSIONS.update(exts)

# Directories to skip entirely
SKIP_DIRS = {
    "venv", ".venv", "env", "ENV", "node_modules", "__pycache__",
    ".git", ".tox", ".mypy_cache", ".pytest_cache", "dist",
    "build", "egg-info", ".eggs", "site-packages", ".dist-info",
    ".cache", ".ruff_cache", "claude.ai-data-export",
}

# File name patterns to never import (matched against filename, case-insensitive)
SKIP_FILE_PATTERNS = [
    r"^\.env",             # .env, .env.example, .env.local, etc.
    r"credentials",
    r"secret",
    r"token",
    r"password",
    r"\.pem$",
    r"\.key$",
    r"\.crt$",
    r"\.p12$",
    r"\.pfx$",
    r"\.keystore$",
    r"^package-lock\.json$",
    r"^yarn\.lock$",
    r"^poetry\.lock$",
    r"^Pipfile\.lock$",
]

# Text files known to be noise — matched against full path (case-insensitive)
TEXT_FILE_SKIP_PATTERNS = [
    r"capitalsquiz",              # Generated quiz files
    r"madlibs_",                  # Madlibs exercise output
    r"testbackup",               # Test backup data
    r"testFIle",                 # Test files
    r"robots\.txt$",             # Web crawler config
    r"guests\.txt$",             # Sample data
    r"requirements\.txt$",       # Pip dependency lists
    r"backup_\d{8}",            # Backup directory copies (duplicates)
    r"argus_stdout\.txt$",       # Debug/test stdout logs
    # improvement-structure-ideas: handled by transcript cleaner, not skipped
]

# Max file size to process (skip huge generated files)
MAX_FILE_SIZE = 50_000  # 50KB — if a text file is bigger than this, it's likely generated

# Min chunk size to bother importing
MIN_CHUNK_LENGTH = 30  # characters — skip tiny fragments

# Max chunk size for a single memory
MAX_CHUNK_LENGTH = 2000  # characters — longer chunks get split

# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def should_skip_dir(dirname: str) -> bool:
    """Check if a directory name matches skip patterns."""
    lower = dirname.lower()
    for skip in SKIP_DIRS:
        if lower == skip.lower() or lower.endswith(".dist-info") or lower.endswith(".egg-info"):
            return True
    return False


def should_skip_file(filename: str) -> bool:
    """Check if a filename matches sensitive/skip patterns."""
    lower = filename.lower()
    for pattern in SKIP_FILE_PATTERNS:
        if re.search(pattern, lower):
            return True
    return False


def should_skip_text_file(filepath: Path) -> bool:
    """Check if a text file matches known noise patterns."""
    path_str = str(filepath).lower()
    for pattern in TEXT_FILE_SKIP_PATTERNS:
        if re.search(pattern, path_str):
            return True
    return False


def scan_files(directories: list[Path]) -> list[Path]:
    """Walk directories and return list of importable file paths."""
    found = []
    for base_dir in directories:
        if not base_dir.exists():
            continue
        for root, dirs, files in os.walk(base_dir):
            # Prune skip directories in-place
            dirs[:] = [d for d in dirs if not should_skip_dir(d)]

            for fname in files:
                if should_skip_file(fname):
                    continue

                fpath = Path(root) / fname
                suffix = fpath.suffix.lower()

                if suffix not in ALL_EXTENSIONS:
                    continue

                # Filter text files against noise patterns
                if suffix == ".txt" and should_skip_text_file(fpath):
                    continue

                # Skip oversized files
                try:
                    if fpath.stat().st_size > MAX_FILE_SIZE:
                        continue
                    if fpath.stat().st_size == 0:
                        continue
                except OSError:
                    continue

                found.append(fpath)

    return sorted(found)


# ---------------------------------------------------------------------------
# Content extractors — one per file type
# ---------------------------------------------------------------------------

def _prefix(filepath: Path) -> str:
    """Create a source prefix showing where the chunk came from."""
    try:
        rel = filepath.relative_to(Path.home())
        return f"[source: ~/{rel}]"
    except ValueError:
        return f"[source: {filepath}]"


def extract_markdown(filepath: Path, content: str) -> list[dict]:
    """Split markdown by ## headers. Each section becomes a chunk."""
    prefix = _prefix(filepath)
    chunks = []

    # Split by level-2 or level-1 headers
    sections = re.split(r'^(#{1,2}\s+.+)$', content, flags=re.MULTILINE)

    if len(sections) <= 1:
        # No headers — treat whole file as one chunk
        text = content.strip()
        if len(text) >= MIN_CHUNK_LENGTH:
            chunks.append({
                "text": f"{prefix}\n{text}",
                "source_file": str(filepath),
                "handler": "markdown",
            })
        return chunks

    # sections alternates: [preamble, header1, body1, header2, body2, ...]
    # Process preamble if it has content
    preamble = sections[0].strip()
    if preamble and len(preamble) >= MIN_CHUNK_LENGTH:
        chunks.append({
            "text": f"{prefix}\n{preamble}",
            "source_file": str(filepath),
            "handler": "markdown",
        })

    # Process header+body pairs
    i = 1
    while i < len(sections) - 1:
        header = sections[i].strip()
        body = sections[i + 1].strip() if i + 1 < len(sections) else ""
        combined = f"{header}\n{body}".strip()

        if len(combined) >= MIN_CHUNK_LENGTH:
            for sub_chunk in _split_long_text(combined, MAX_CHUNK_LENGTH):
                chunks.append({
                    "text": f"{prefix}\n{sub_chunk}",
                    "source_file": str(filepath),
                    "handler": "markdown",
                })
        i += 2

    return chunks


def extract_python(filepath: Path, content: str) -> list[dict]:
    """Extract module docstring, class/function signatures with docstrings."""
    prefix = _prefix(filepath)
    chunks = []

    # Module-level docstring
    module_doc_match = re.match(r'^(?:#!/.*\n)?(?:#.*\n)*\s*("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\')', content)
    if module_doc_match:
        doc = module_doc_match.group(1).strip('"\' \n')
        if len(doc) >= MIN_CHUNK_LENGTH:
            chunks.append({
                "text": f"{prefix}\nModule docstring: {doc}",
                "source_file": str(filepath),
                "handler": "python",
            })

    # Class and function definitions with their docstrings
    pattern = re.compile(
        r'^((?:class|def)\s+\w+[^:]*:)\s*\n'
        r'\s*("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\')?',
        re.MULTILINE
    )

    for match in pattern.finditer(content):
        signature = match.group(1).strip()
        docstring = match.group(2)

        if docstring:
            doc_text = docstring.strip('"\' \n')
            combined = f"{signature}\n{doc_text}"
        else:
            combined = signature

        if len(combined) >= MIN_CHUNK_LENGTH:
            chunks.append({
                "text": f"{prefix}\n{combined}",
                "source_file": str(filepath),
                "handler": "python",
            })

    # If we got nothing from structured extraction, grab header comments
    if not chunks:
        header_lines = []
        for line in content.split("\n")[:30]:
            if line.startswith("#") and not line.startswith("#!"):
                header_lines.append(line.lstrip("# ").strip())
            elif header_lines and not line.strip():
                break
            elif header_lines:
                break
        if header_lines:
            header_text = " ".join(header_lines)
            if len(header_text) >= MIN_CHUNK_LENGTH:
                chunks.append({
                    "text": f"{prefix}\n{header_text}",
                    "source_file": str(filepath),
                    "handler": "python",
                })

    return chunks


def extract_shell(filepath: Path, content: str) -> list[dict]:
    """Extract header comments and purpose from shell scripts."""
    prefix = _prefix(filepath)
    chunks = []

    lines = content.split("\n")
    comment_lines = []
    for line in lines[:40]:
        stripped = line.strip()
        if stripped.startswith("#") and not stripped.startswith("#!"):
            comment_lines.append(stripped.lstrip("# ").strip())
        elif comment_lines and not stripped:
            break
        elif comment_lines and not stripped.startswith("#"):
            break

    if comment_lines:
        header_text = "\n".join(comment_lines)
        if len(header_text) >= MIN_CHUNK_LENGTH:
            chunks.append({
                "text": f"{prefix}\nShell script: {header_text}",
                "source_file": str(filepath),
                "handler": "shell",
            })

    return chunks


def extract_yaml(filepath: Path, content: str) -> list[dict]:
    """Import YAML config files as-is if small, skip if too large."""
    prefix = _prefix(filepath)
    chunks = []

    if len(content) <= MAX_CHUNK_LENGTH and len(content) >= MIN_CHUNK_LENGTH:
        chunks.append({
            "text": f"{prefix}\nConfig file:\n{content.strip()}",
            "source_file": str(filepath),
            "handler": "yaml",
        })
    elif len(content) > MAX_CHUNK_LENGTH:
        summary_lines = []
        for line in content.split("\n")[:50]:
            stripped = line.strip()
            if stripped.startswith("#") or (stripped and not stripped.startswith(" ") and ":" in stripped):
                summary_lines.append(stripped)
        if summary_lines:
            summary = "\n".join(summary_lines)
            if len(summary) >= MIN_CHUNK_LENGTH:
                chunks.append({
                    "text": f"{prefix}\nYAML config summary:\n{summary}",
                    "source_file": str(filepath),
                    "handler": "yaml",
                })

    return chunks


def _is_transcript(content: str) -> bool:
    """Detect if content is a YouTube transcript with inline timestamps."""
    # Count lines that are just timestamps like "0:06", "1:24", "12:34"
    timestamp_lines = len(re.findall(r'^\d{1,2}:\d{2}$', content, re.MULTILINE))
    total_lines = len(content.strip().split("\n"))
    # If >15% of lines are timestamps, it's a transcript
    return total_lines > 10 and timestamp_lines / total_lines > 0.15


def _clean_transcript(content: str) -> str:
    """Strip timestamps and rejoin a YouTube transcript into flowing text."""
    lines = content.strip().split("\n")
    cleaned = []

    # Extract title (first non-empty line) and URL (line starting with http)
    title = ""
    url = ""
    author = ""
    body_start = 0

    for i, line in enumerate(lines[:10]):
        stripped = line.strip()
        if not stripped:
            continue
        if not title and stripped and not stripped.startswith("http"):
            title = stripped
        elif stripped.startswith("http"):
            url = stripped
        elif "|" in stripped:
            author = stripped
        # Detect where body starts (first timestamp or text after metadata)
        if re.match(r'^\d{1,2}:\d{2}$', stripped):
            body_start = i
            break
        body_start = i + 1

    # Process body: strip timestamp lines, join text
    text_lines = []
    for line in lines[body_start:]:
        stripped = line.strip()
        # Skip pure timestamp lines
        if re.match(r'^\d{1,2}:\d{2}$', stripped):
            continue
        # Skip empty lines
        if not stripped:
            continue
        text_lines.append(stripped)

    # Join into flowing text, then re-paragraph at sentence boundaries
    raw_text = " ".join(text_lines)
    # Clean up double spaces
    raw_text = re.sub(r'\s+', ' ', raw_text).strip()

    # Build header
    header_parts = []
    if title:
        header_parts.append(f"Video: {title}")
    if author:
        header_parts.append(f"Channel: {author}")
    if url:
        header_parts.append(f"URL: {url}")
    header = "\n".join(header_parts)

    if header:
        return f"{header}\n\n{raw_text}"
    return raw_text


def extract_text(filepath: Path, content: str) -> list[dict]:
    """Split plain text by paragraphs. Auto-detects and cleans transcripts."""
    prefix = _prefix(filepath)
    chunks = []

    # Detect and clean YouTube transcripts
    if _is_transcript(content):
        cleaned = _clean_transcript(content)
        if len(cleaned) < MIN_CHUNK_LENGTH:
            return chunks

        # Split cleaned transcript into ~1500 char chunks at sentence boundaries
        # (slightly under MAX to leave room for prefix)
        for sub_chunk in _split_long_text(cleaned, MAX_CHUNK_LENGTH - len(prefix) - 50):
            chunks.append({
                "text": f"{prefix}\n[transcript]\n{sub_chunk}",
                "source_file": str(filepath),
                "handler": "transcript",
            })
        return chunks

    # Regular text handling
    paragraphs = re.split(r'\n\s*\n', content.strip())
    for para in paragraphs:
        para = para.strip()
        if len(para) >= MIN_CHUNK_LENGTH:
            for sub_chunk in _split_long_text(para, MAX_CHUNK_LENGTH):
                chunks.append({
                    "text": f"{prefix}\n{sub_chunk}",
                    "source_file": str(filepath),
                    "handler": "text",
                })

    return chunks


def extract_json_config(filepath: Path, content: str) -> list[dict]:
    """Import small JSON config files, skip large data files."""
    prefix = _prefix(filepath)
    chunks = []

    if len(content) > 5000:
        return chunks

    if len(content) >= MIN_CHUNK_LENGTH:
        chunks.append({
            "text": f"{prefix}\nJSON config:\n{content.strip()}",
            "source_file": str(filepath),
            "handler": "json_config",
        })

    return chunks


# ---------------------------------------------------------------------------
# Claude.ai export extractors
# ---------------------------------------------------------------------------

def extract_claude_memories(export_dir: Path) -> list[dict]:
    """Extract Claude.ai's built-in memories — already distilled, highest value."""
    memories_file = export_dir / "memories.json"
    if not memories_file.exists():
        print(f"  Warning: {memories_file} not found")
        return []

    data = json.loads(memories_file.read_text(encoding="utf-8"))
    chunks = []

    for entry in data:
        # Global conversation memory
        global_mem = entry.get("conversations_memory", "")
        if global_mem and len(global_mem) >= MIN_CHUNK_LENGTH:
            for sub in _split_long_text(global_mem, MAX_CHUNK_LENGTH):
                chunks.append({
                    "text": f"[source: claude.ai/memories/global]\n{sub}",
                    "source_file": str(memories_file),
                    "handler": "claude_memory",
                })

        # Per-project memories
        project_mems = entry.get("project_memories", {})
        for project_id, mem_text in project_mems.items():
            if mem_text and len(mem_text) >= MIN_CHUNK_LENGTH:
                for sub in _split_long_text(mem_text, MAX_CHUNK_LENGTH):
                    chunks.append({
                        "text": f"[source: claude.ai/memories/project/{project_id}]\n{sub}",
                        "source_file": str(memories_file),
                        "handler": "claude_memory",
                    })

    return chunks


def extract_claude_conversations(export_dir: Path) -> list[dict]:
    """Extract conversation summaries and distill key messages."""
    conv_file = export_dir / "conversations.json"
    if not conv_file.exists():
        print(f"  Warning: {conv_file} not found")
        return []

    data = json.loads(conv_file.read_text(encoding="utf-8"))
    chunks = []

    for conv in data:
        name = conv.get("name", "Untitled")
        created = conv.get("created_at", "")[:10]  # YYYY-MM-DD
        summary = conv.get("summary", "")
        messages = conv.get("chat_messages", [])

        if not messages:
            continue

        # Tier 1: Conversation summaries (Claude-generated, high signal)
        if summary and len(summary) >= MIN_CHUNK_LENGTH:
            summary_text = f"[source: claude.ai/conversation] [date: {created}] [topic: {name}]\nConversation summary:\n{summary}"
            for sub in _split_long_text(summary_text, MAX_CHUNK_LENGTH):
                chunks.append({
                    "text": sub,
                    "source_file": str(conv_file),
                    "handler": "claude_conversation_summary",
                })

        # Tier 2: Extract substantive human messages (decisions, requirements, ideas)
        # These capture the user's own thinking and instructions
        for msg in messages:
            if msg.get("sender") != "human":
                continue

            text = msg.get("text", "")
            if not text or len(text) < 100:
                continue

            # Skip very long messages (likely pasted code or data)
            if len(text) > 5000:
                continue

            # Only include messages that look like they contain decisions/ideas
            # (not just "ok" or "yes" or pasted error logs)
            if _is_substantive_human_message(text):
                msg_date = msg.get("created_at", created)[:10]
                tagged = f"[source: claude.ai/conversation] [date: {msg_date}] [topic: {name}]\n{text}"
                for sub in _split_long_text(tagged, MAX_CHUNK_LENGTH):
                    chunks.append({
                        "text": sub,
                        "source_file": str(conv_file),
                        "handler": "claude_human_message",
                    })

    return chunks


def _is_substantive_human_message(text: str) -> bool:
    """Heuristic: is this a meaningful human message worth storing?"""
    # Must have multiple sentences or bullet points
    sentences = len(re.findall(r'[.!?]\s', text))
    bullets = len(re.findall(r'^\s*[-*]\s', text, re.MULTILINE))
    words = len(text.split())

    # Short messages with few sentences are likely just responses
    if words < 20:
        return False

    # Messages with structure (bullets, multiple sentences) are likely substantive
    if sentences >= 2 or bullets >= 2:
        return True

    # Longer messages (50+ words) even without structure may contain ideas
    if words >= 50:
        return True

    return False


def extract_claude_projects(export_dir: Path) -> list[dict]:
    """Extract project docs and prompt templates from Claude.ai projects."""
    proj_file = export_dir / "projects.json"
    if not proj_file.exists():
        print(f"  Warning: {proj_file} not found")
        return []

    data = json.loads(proj_file.read_text(encoding="utf-8"))
    chunks = []

    for project in data:
        proj_name = project.get("name", "Untitled")
        description = project.get("description", "")
        prompt_template = project.get("prompt_template", "")
        docs = project.get("docs", [])

        # Project description
        if description and len(description) >= MIN_CHUNK_LENGTH:
            chunks.append({
                "text": f"[source: claude.ai/project/{proj_name}]\nProject description: {description}",
                "source_file": str(proj_file),
                "handler": "claude_project",
            })

        # Prompt template (captures working preferences)
        if prompt_template and len(prompt_template) >= MIN_CHUNK_LENGTH:
            for sub in _split_long_text(prompt_template, MAX_CHUNK_LENGTH):
                chunks.append({
                    "text": f"[source: claude.ai/project/{proj_name}/prompt]\nProject prompt template:\n{sub}",
                    "source_file": str(proj_file),
                    "handler": "claude_project_prompt",
                })

        # Project docs — only markdown-like content, skip raw code/data
        for doc in docs:
            filename = doc.get("filename", "")
            content = doc.get("content", "")

            if not content or len(content) < MIN_CHUNK_LENGTH:
                continue

            # Prioritize markdown docs, skip raw code/data files
            lower_fname = filename.lower()
            if any(lower_fname.endswith(ext) for ext in [".md", ".txt"]):
                # Use markdown extraction for structured docs
                fake_path = Path(f"claude.ai/project/{proj_name}/{filename}")
                md_chunks = extract_markdown(fake_path, content)
                for c in md_chunks:
                    c["source_file"] = str(proj_file)
                    c["handler"] = "claude_project_doc"
                    # Re-prefix with project context
                    c["text"] = c["text"].replace(
                        f"[source: {fake_path}]",
                        f"[source: claude.ai/project/{proj_name}/{filename}]"
                    )
                chunks.extend(md_chunks)

            elif any(lower_fname.endswith(ext) for ext in [".py", ".sh", ".yaml", ".yml"]):
                # For code files in projects, just grab docstrings/headers if small
                if len(content) <= 5000:
                    if lower_fname.endswith(".py"):
                        fake_path = Path(f"claude.ai/project/{proj_name}/{filename}")
                        py_chunks = extract_python(fake_path, content)
                        for c in py_chunks:
                            c["source_file"] = str(proj_file)
                            c["handler"] = "claude_project_doc"
                        chunks.extend(py_chunks)

    return chunks


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _split_long_text(text: str, max_len: int) -> list[str]:
    """Split text into chunks of max_len, breaking at sentence or line boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > max_len:
        break_at = remaining.rfind("\n", 0, max_len)
        if break_at == -1 or break_at < max_len // 2:
            break_at = remaining.rfind(". ", 0, max_len)
            if break_at == -1 or break_at < max_len // 2:
                break_at = max_len
            else:
                break_at += 1

        chunks.append(remaining[:break_at].strip())
        remaining = remaining[break_at:].strip()

    if remaining and len(remaining) >= MIN_CHUNK_LENGTH:
        chunks.append(remaining)

    return chunks


HANDLERS = {
    "markdown": extract_markdown,
    "python": extract_python,
    "shell": extract_shell,
    "yaml": extract_yaml,
    "text": extract_text,
    "json_config": extract_json_config,
}


def get_handler_type(filepath: Path) -> str | None:
    """Determine which handler to use based on file extension."""
    suffix = filepath.suffix.lower()
    for handler_type, extensions in HANDLER_MAP.items():
        if suffix in extensions:
            return handler_type
    return None


def extract_file(filepath: Path) -> list[dict]:
    """Read a file and extract chunks using the appropriate handler."""
    handler_type = get_handler_type(filepath)
    if not handler_type:
        return []

    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError):
        return []

    if not content.strip():
        return []

    handler = HANDLERS[handler_type]
    return handler(filepath, content)


# ---------------------------------------------------------------------------
# Import logic — sends chunks to Open Brain via MCP HTTP endpoint
# ---------------------------------------------------------------------------

def import_chunks(chunks: list[dict], dry_run: bool = False, delay: float = 0.5):
    """Import chunks into Open Brain via the MCP server's add_memory tool."""
    try:
        import requests
    except ImportError:
        print("ERROR: 'requests' not installed. Run: pip install requests")
        sys.exit(1)

    mcp_url = os.environ.get("MCP_HTTP_URL", "http://localhost:8765")

    total = len(chunks)
    imported = 0
    failed = 0

    for i, chunk in enumerate(chunks, 1):
        text = chunk["text"]

        if dry_run:
            print(f"[{i}/{total}] DRY RUN: Would import ({len(text)} chars) from {chunk.get('source_file', 'unknown')}")
            print(f"    Preview: {text[:120]}...")
            print()
            continue

        try:
            payload = {
                "method": "tools/call",
                "params": {
                    "name": "add_memory",
                    "arguments": {"text": text}
                }
            }
            resp = requests.post(f"{mcp_url}/mcp", json=payload, timeout=30)

            if resp.status_code == 200:
                imported += 1
                print(f"[{i}/{total}] Imported ({len(text)} chars) from {chunk.get('source_file', 'unknown')}")
            else:
                failed += 1
                print(f"[{i}/{total}] FAILED ({resp.status_code}) from {chunk.get('source_file', 'unknown')}")

        except Exception as e:
            failed += 1
            print(f"[{i}/{total}] ERROR: {e}")

        if not dry_run:
            time.sleep(delay)

    print(f"\nDone. Imported: {imported}, Failed: {failed}, Total: {total}")


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_scan(args):
    """Show what files would be processed."""
    files = scan_files(SCAN_DIRECTORIES)

    by_handler = {}
    for f in files:
        ht = get_handler_type(f)
        by_handler.setdefault(ht, []).append(f)

    print(f"Found {len(files)} importable files across {len(SCAN_DIRECTORIES)} directories\n")

    for handler_type, handler_files in sorted(by_handler.items()):
        print(f"  {handler_type}: {len(handler_files)} files")
        if args.verbose:
            for f in handler_files[:10]:
                try:
                    rel = f.relative_to(Path.home())
                    print(f"    ~/{rel}")
                except ValueError:
                    print(f"    {f}")
            if len(handler_files) > 10:
                print(f"    ... and {len(handler_files) - 10} more")
        print()

    # Claude.ai export info
    if CLAUDE_EXPORT_DIR.exists():
        print(f"Claude.ai export found at: {CLAUDE_EXPORT_DIR}")
        for fname in ["conversations.json", "projects.json", "memories.json"]:
            fpath = CLAUDE_EXPORT_DIR / fname
            if fpath.exists():
                size = fpath.stat().st_size
                print(f"  {fname}: {size / 1024:.1f} KB")
        print(f"\nRun 'extract-claude' to process Claude.ai data separately.\n")
    else:
        print(f"Claude.ai export not found at: {CLAUDE_EXPORT_DIR}\n")

    dirs_missing = [d for d in SCAN_DIRECTORIES if not d.exists()]
    if dirs_missing:
        print(f"Directories not found (will be included when created):")
        for d in dirs_missing:
            print(f"  {d}")


def cmd_extract(args):
    """Extract chunks from local files and save to staging JSON."""
    files = scan_files(SCAN_DIRECTORIES)
    print(f"Scanning {len(files)} files...")

    all_chunks = []
    files_with_chunks = 0

    for i, filepath in enumerate(files):
        chunks = extract_file(filepath)
        if chunks:
            files_with_chunks += 1
            all_chunks.extend(chunks)

        if (i + 1) % 50 == 0:
            print(f"  Processed {i + 1}/{len(files)} files, {len(all_chunks)} chunks so far...")

    out_path = Path(args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, indent=2, ensure_ascii=False)

    _print_extraction_summary(all_chunks, len(files), files_with_chunks, out_path)


def cmd_extract_claude(args):
    """Extract chunks from Claude.ai export data."""
    if not CLAUDE_EXPORT_DIR.exists():
        print(f"ERROR: Claude.ai export not found at: {CLAUDE_EXPORT_DIR}")
        sys.exit(1)

    all_chunks = []

    print("Extracting Claude.ai memories...")
    mem_chunks = extract_claude_memories(CLAUDE_EXPORT_DIR)
    all_chunks.extend(mem_chunks)
    print(f"  Memories: {len(mem_chunks)} chunks")

    print("Extracting Claude.ai conversation summaries and key messages...")
    conv_chunks = extract_claude_conversations(CLAUDE_EXPORT_DIR)
    all_chunks.extend(conv_chunks)
    print(f"  Conversations: {len(conv_chunks)} chunks")

    print("Extracting Claude.ai project docs and prompts...")
    proj_chunks = extract_claude_projects(CLAUDE_EXPORT_DIR)
    all_chunks.extend(proj_chunks)
    print(f"  Projects: {len(proj_chunks)} chunks")

    out_path = Path(args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, indent=2, ensure_ascii=False)

    print(f"\nClaude.ai extraction complete:")
    print(f"  Total chunks: {len(all_chunks)}")
    print(f"  Staging file: {out_path}")

    by_handler = {}
    for c in all_chunks:
        by_handler[c["handler"]] = by_handler.get(c["handler"], 0) + 1
    print(f"\n  Chunks by type:")
    for ht, count in sorted(by_handler.items()):
        print(f"    {ht}: {count}")

    print(f"\nReview {out_path}, then run:")
    print(f"  python bulk_import.py import {out_path}")


def cmd_merge(args):
    """Merge multiple staging JSON files into one."""
    all_chunks = []
    for fpath in args.files:
        p = Path(fpath)
        if not p.exists():
            print(f"WARNING: Skipping missing file: {p}")
            continue
        with open(p, "r", encoding="utf-8") as f:
            chunks = json.load(f)
        all_chunks.extend(chunks)
        print(f"  Loaded {len(chunks)} chunks from {p}")

    out_path = Path(args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, indent=2, ensure_ascii=False)

    print(f"\nMerged {len(all_chunks)} total chunks into {out_path}")


def cmd_import(args):
    """Import staged chunks into Open Brain."""
    staging_path = Path(args.staging_file)
    if not staging_path.exists():
        print(f"ERROR: Staging file not found: {staging_path}")
        sys.exit(1)

    with open(staging_path, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    print(f"Loaded {len(chunks)} chunks from {staging_path}")

    if args.dry_run:
        print("DRY RUN — no data will be imported\n")

    import_chunks(chunks, dry_run=args.dry_run, delay=args.delay)


def _print_extraction_summary(chunks, files_scanned, files_with_content, out_path):
    """Print extraction summary stats."""
    print(f"\nExtraction complete:")
    print(f"  Files scanned: {files_scanned}")
    print(f"  Files with content: {files_with_content}")
    print(f"  Total chunks: {len(chunks)}")
    print(f"  Staging file: {out_path}")

    by_handler = {}
    for c in chunks:
        by_handler[c["handler"]] = by_handler.get(c["handler"], 0) + 1
    print(f"\n  Chunks by type:")
    for ht, count in sorted(by_handler.items()):
        print(f"    {ht}: {count}")

    print(f"\nReview {out_path}, then run:")
    print(f"  python bulk_import.py import {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Bulk import local files into Open Brain",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scan
    scan_parser = subparsers.add_parser("scan", help="Show what files would be processed")
    scan_parser.add_argument("-v", "--verbose", action="store_true", help="Show individual file paths")

    # extract
    extract_parser = subparsers.add_parser("extract", help="Extract chunks from local files")
    extract_parser.add_argument("--out", default="staged_chunks.json", help="Output file (default: staged_chunks.json)")

    # extract-claude
    claude_parser = subparsers.add_parser("extract-claude", help="Extract chunks from Claude.ai export")
    claude_parser.add_argument("--out", default="claude_chunks.json", help="Output file (default: claude_chunks.json)")

    # merge
    merge_parser = subparsers.add_parser("merge", help="Merge multiple staging files")
    merge_parser.add_argument("files", nargs="+", help="Staging JSON files to merge")
    merge_parser.add_argument("-o", "--out", default="all_chunks.json", help="Output file (default: all_chunks.json)")

    # import
    import_parser = subparsers.add_parser("import", help="Import staged chunks into Open Brain")
    import_parser.add_argument("staging_file", help="Path to staged chunks JSON file")
    import_parser.add_argument("--dry-run", action="store_true", help="Preview without importing")
    import_parser.add_argument("--delay", type=float, default=0.5, help="Seconds between imports (default: 0.5)")

    args = parser.parse_args()

    commands = {
        "scan": cmd_scan,
        "extract": cmd_extract,
        "extract-claude": cmd_extract_claude,
        "merge": cmd_merge,
        "import": cmd_import,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
