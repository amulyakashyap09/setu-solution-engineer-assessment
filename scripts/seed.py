#!/usr/bin/env python3
"""Load a JSON file of events into the database.

    python -m scripts.seed --file data/sample_events.json

Goes through the same validation and ingest path the HTTP endpoint uses,
so seeding exercises the real idempotency logic rather than a shortcut
bulk INSERT. Re-running it is safe: every event is a replay the second
time, and the transaction state is unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402

from app.db import connect, init_db  # noqa: E402
from app.ingest import ingest_events  # noqa: E402
from app.schemas import EventIn  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the reconciliation database")
    parser.add_argument("--file", default="data/sample_events.json")
    parser.add_argument("--database", default=None, help="Overrides DATABASE_PATH")
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--reset", action="store_true",
                        help="Delete the database file before seeding")
    args = parser.parse_args()

    source = Path(args.file)
    if not source.exists():
        print(f"error: {source} not found", file=sys.stderr)
        return 1

    if args.reset and args.database and args.database != ":memory:":
        for suffix in ("", "-wal", "-shm"):
            Path(args.database + suffix).unlink(missing_ok=True)

    init_db(args.database)
    conn = connect(args.database)

    raw = json.loads(source.read_text())
    if isinstance(raw, dict):
        raw = raw.get("events", [])
    print(f"read {len(raw)} records from {source}")

    valid: list[EventIn] = []
    rejected = 0
    for index, record in enumerate(raw):
        try:
            valid.append(EventIn.model_validate(record))
        except ValidationError as exc:
            rejected += 1
            if rejected <= 5:
                print(f"  rejected record {index}: {exc.errors()[0]['msg']}")

    created = duplicates = 0
    started = time.perf_counter()
    for start in range(0, len(valid), args.batch_size):
        chunk = valid[start:start + args.batch_size]
        results, _ = ingest_events(conn, chunk)
        created += sum(1 for r in results if r.status == "created")
        duplicates += sum(1 for r in results if r.status == "duplicate")
        print(f"  ingested {min(start + args.batch_size, len(valid))}/{len(valid)}")
    elapsed = time.perf_counter() - started

    stats = conn.execute(
        """SELECT (SELECT COUNT(*) FROM merchants)    AS merchants,
                  (SELECT COUNT(*) FROM transactions) AS transactions,
                  (SELECT COUNT(*) FROM events)       AS events"""
    ).fetchone()

    print(
        f"\ndone in {elapsed:.2f}s"
        f"\n  created     {created}"
        f"\n  duplicates  {duplicates}"
        f"\n  rejected    {rejected}"
        f"\n  merchants   {stats['merchants']}"
        f"\n  transactions{stats['transactions']:>7}"
        f"\n  events      {stats['events']}"
    )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
