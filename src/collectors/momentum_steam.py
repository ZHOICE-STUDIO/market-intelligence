"""Steam review-momentum collector.

Backfills monthly review volume per game from Steam's review histogram
(`appreviewhistogram`). Because the histogram covers a game's entire lifetime,
one request yields years of retroactive time-series -- so we can measure which
genres are heating up or cooling down RIGHT NOW without waiting weeks to
accumulate our own snapshots.

One request per app, so it's the slowest collector. Fetches run concurrently
across a thread pool (network-bound), while writes stay on the main thread
(SQLite is single-writer); UPSERT keeps re-runs idempotent.

Usage:
    python -m src.collectors.momentum_steam               # all, parallel
    python -m src.collectors.momentum_steam --limit 10    # testing
    python -m src.collectors.momentum_steam --workers 20  # more concurrency
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from src.collectors.base import (
    finish_run,
    get_json,
    make_session,
    platform_id,
    start_run,
)
from src.db.database import get_connection

HISTOGRAM_URL = "https://store.steampowered.com/appreviewhistogram/{appid}"
PLATFORM_CODE = "steam"


def _month_start(unix_ts: int) -> str:
    """Unix seconds -> 'YYYY-MM-01' (histogram rollups are monthly)."""
    d = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    return f"{d.year:04d}-{d.month:02d}-01"


def fetch_histogram(session, appid: int) -> list[dict]:
    data = get_json(session, HISTOGRAM_URL.format(appid=appid),
                    params={"l": "english"})
    return (data.get("results") or {}).get("rollups") or []


def store_history(conn, listing_id: int, rollups: list[dict]) -> int:
    n = 0
    for r in rollups:
        ts = r.get("date")
        if ts is None:
            continue
        conn.execute(
            "INSERT INTO review_history (listing_id, period_start, up, down) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(listing_id, period_start) DO UPDATE SET "
            "up = excluded.up, down = excluded.down",
            (listing_id, _month_start(ts),
             r.get("recommendations_up"), r.get("recommendations_down")),
        )
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill Steam review momentum.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max listings (for testing)")
    parser.add_argument("--workers", type=int, default=12,
                        help="Concurrent histogram requests (network-bound)")
    args = parser.parse_args(argv)

    session = make_session()
    conn = get_connection()
    pid = platform_id(conn, PLATFORM_CODE)
    run_id = start_run(conn, PLATFORM_CODE, "momentum_steam.py")

    sql = ("SELECT listing_id, external_id FROM listings WHERE platform_id = ? "
           "ORDER BY listing_id")
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    listings = conn.execute(sql, (pid,)).fetchall()
    total = len(listings)

    def fetch_one(row):
        """Network only -- runs on a worker thread (no DB access here)."""
        return row["listing_id"], fetch_histogram(session, int(row["external_id"]))

    done = 0
    try:
        # Fan out fetches across workers; collect + write as each completes.
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(fetch_one, r): r for r in listings}
            for fut in as_completed(futures):
                row = futures[fut]
                try:
                    listing_id, rollups = fut.result()
                    store_history(conn, listing_id, rollups)
                    conn.commit()
                    done += 1
                except Exception as e:  # noqa: BLE001
                    print(f"  [warn] appid {int(row['external_id'])}: {e}")
                if done % 250 == 0 and done:
                    print(f"  {done}/{total} games done", flush=True)
        finish_run(conn, run_id, "success", done)
    except Exception as e:  # noqa: BLE001
        finish_run(conn, run_id, "failed", done, notes=str(e))
        print(f"Run failed: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"Done. Review history backfilled for {done}/{total} games.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
