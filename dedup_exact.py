#!/usr/bin/env python3
"""
dedup_exact.py — Remove exact duplicate memories from Open Brain.

Only removes rows where raw_text is identical. Keeps the oldest entry (lowest id).
Safe, conservative, no fuzzy matching.

Usage:
    python dedup_exact.py                # dry run (default)
    python dedup_exact.py --apply        # actually delete duplicates
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg2

DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://open-brain:open-brain@localhost/open-brain",  # NOSEC default local dev
)

FIND_DUPES_SQL = """
SELECT raw_text, count(*) as cnt, array_agg(id ORDER BY id) as ids
FROM memories
GROUP BY raw_text
HAVING count(*) > 1
ORDER BY count(*) DESC;
"""

DELETE_DUPES_SQL = """
DELETE FROM memories a
USING memories b
WHERE a.raw_text = b.raw_text
  AND a.id > b.id;
"""


def main():
    parser = argparse.ArgumentParser(description="Remove exact duplicate memories")
    parser.add_argument("--apply", action="store_true", help="Actually delete dupes (default is dry run)")
    args = parser.parse_args()

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    # Count total before
    cur.execute("SELECT count(*) FROM memories;")
    total_before = cur.fetchone()[0]

    # Find duplicates
    cur.execute(FIND_DUPES_SQL)
    dupes = cur.fetchall()

    if not dupes:
        print(f"No exact duplicates found among {total_before} memories.")
        conn.close()
        return

    # Count how many rows would be removed
    removable = sum(cnt - 1 for _, cnt, _ in dupes)

    print(f"Total memories: {total_before}")
    print(f"Duplicate groups: {len(dupes)}")
    print(f"Rows to remove: {removable}")
    print(f"Rows after dedup: {total_before - removable}")
    print()

    # Show top 10 duplicate groups
    print(f"Top {min(10, len(dupes))} duplicate groups:")
    for raw_text, cnt, ids in dupes[:10]:
        preview = raw_text[:80].replace("\n", " ")
        print(f"  {cnt}x (keep id={ids[0]}, remove {ids[1:]}) | {preview}...")
    print()

    if not args.apply:
        print("DRY RUN — no changes made. Use --apply to delete duplicates.")
        conn.close()
        return

    # Delete
    cur.execute(DELETE_DUPES_SQL)
    deleted = cur.rowcount
    conn.commit()

    # Count after
    cur.execute("SELECT count(*) FROM memories;")
    total_after = cur.fetchone()[0]

    print(f"Deleted {deleted} duplicate rows.")
    print(f"Memories: {total_before} -> {total_after}")

    conn.close()


if __name__ == "__main__":
    main()
