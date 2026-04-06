#!/usr/bin/env python3
"""
remote_import.py — Run on the open-brain server to bulk import chunks.

Reads a staged JSON file and feeds each chunk through the capture pipeline.
Meant to be SCP'd to the server and run directly.

Usage:
    python remote_import.py all_chunks.json
    python remote_import.py all_chunks.json --dry-run
    python remote_import.py all_chunks.json --start 100    # Resume from chunk 100
    python remote_import.py all_chunks.json --limit 50     # Only import 50 chunks (for testing)
    python remote_import.py all_chunks.json --workers 8    # Run 8 concurrent workers
"""

import asyncio
import json
import sys
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Add the open-brain directory to path so we can import capture
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()


def capture_sync(text: str) -> dict:
    """Run the async capture function in its own event loop (thread-safe)."""
    from capture import capture
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(capture(text))
    finally:
        loop.close()


def run_import(args):
    staging_path = Path(args.staging_file)
    if not staging_path.exists():
        print(f"ERROR: File not found: {staging_path}")
        sys.exit(1)

    with open(staging_path, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    total = len(chunks)
    start = args.start
    limit = args.limit or (total - start)
    end = min(start + limit, total)
    subset = chunks[start:end]
    workers = args.workers

    print(f"Loaded {total} chunks from {staging_path}")
    print(f"Importing chunks {start} to {end - 1} ({len(subset)} chunks)")
    print(f"Concurrency: {workers} workers")

    if args.dry_run:
        print("DRY RUN — no data will be imported\n")
        for i, chunk in enumerate(subset, start):
            text = chunk["text"]
            print(f"[{i}/{total}] ({len(text)} chars) {chunk.get('handler', '?')} | {text[:100]}...")
        print(f"\nDry run complete. {len(subset)} chunks would be imported.")
        return

    # Preload the embedding model before spawning threads
    print("Preloading embedding model...")
    from capture import get_embedding_model
    get_embedding_model()
    print("Model loaded. Starting import...\n")

    lock = threading.Lock()
    stats = {"imported": 0, "failed": 0, "completed": 0}
    errors = []
    start_time = time.time()

    def process_chunk(item):
        i, chunk = item
        idx = start + i
        text = chunk["text"]

        try:
            result = capture_sync(text)

            with lock:
                stats["completed"] += 1
                if result.get("success"):
                    stats["imported"] += 1
                    if stats["imported"] % 50 == 0 or stats["imported"] == 1:
                        elapsed = time.time() - start_time
                        rate = stats["imported"] / elapsed if elapsed > 0 else 0
                        remaining_chunks = len(subset) - stats["completed"]
                        remaining_time = remaining_chunks / rate if rate > 0 else 0
                        print(f"[{idx}/{total}] Imported #{stats['imported']} | "
                              f"{rate:.1f}/sec | ~{remaining_time / 60:.0f}min remaining | "
                              f"{chunk.get('handler', '?')}")
                else:
                    stats["failed"] += 1
                    err = result.get("error", "unknown")
                    errors.append(f"[{idx}] {err}")
                    print(f"[{idx}/{total}] FAILED: {err}")

        except Exception as e:
            with lock:
                stats["completed"] += 1
                stats["failed"] += 1
                errors.append(f"[{idx}] {type(e).__name__}: {e}")
                print(f"[{idx}/{total}] ERROR: {e}")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(process_chunk, (i, chunk)) for i, chunk in enumerate(subset)]
        # Wait for all to complete
        for future in as_completed(futures):
            future.result()  # Propagate any uncaught exceptions

    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"Import complete in {elapsed / 60:.1f} minutes")
    print(f"  Imported: {stats['imported']}")
    print(f"  Failed: {stats['failed']}")
    print(f"  Total attempted: {len(subset)}")
    print(f"  Rate: {stats['imported'] / elapsed:.1f} chunks/sec")
    print(f"  Workers: {workers}")

    if errors:
        print(f"\nFirst 10 errors:")
        for err in errors[:10]:
            print(f"  {err}")

    # Save progress marker
    progress_file = staging_path.with_suffix(".progress")
    with open(progress_file, "w") as f:
        json.dump({
            "last_completed": start + len(subset) - 1,
            "imported": stats["imported"],
            "failed": stats["failed"],
            "resume_from": start + len(subset),
        }, f, indent=2)
    print(f"\nProgress saved to {progress_file}")
    print(f"To resume: python remote_import.py {staging_path} --start {start + len(subset)}")


def main():
    parser = argparse.ArgumentParser(description="Bulk import chunks into Open Brain")
    parser.add_argument("staging_file", help="Path to staged chunks JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Preview without importing")
    parser.add_argument("--start", type=int, default=0, help="Start from chunk N (for resuming)")
    parser.add_argument("--limit", type=int, default=None, help="Max chunks to import (for testing)")
    parser.add_argument("--workers", type=int, default=8, help="Number of concurrent workers (default: 8)")
    args = parser.parse_args()

    run_import(args)


if __name__ == "__main__":
    main()
