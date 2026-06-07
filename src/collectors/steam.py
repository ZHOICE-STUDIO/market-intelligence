"""Steam collector.

For each appid it captures:
  - metadata  -> games + listings  (name, devs, genres, base price)
  - review summary -> listing_snapshots (score, positive/negative counts, price)
  - review text    -> reviews        (for sentiment mining)
  - SteamSpy       -> owner/ccu estimates (coarse; stored, flagged in code)

No API key required. Be polite: small delays, capped review pulls.

Usage:
    python -m src.collectors.steam 413150 1794680 --reviews 300
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from typing import Any

from src.collectors.base import (
    finish_run,
    get_json,
    make_session,
    parse_owners,
    platform_id,
    start_run,
)
from src.db.database import get_connection

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
APPREVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
STEAMSPY_URL = "https://steamspy.com/api.php"

PLATFORM_CODE = "steam"


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------
def fetch_appdetails(session, appid: int) -> dict | None:
    data = get_json(
        session, APPDETAILS_URL,
        params={"appids": appid, "cc": "us", "l": "english"},
    )
    entry = data.get(str(appid), {})
    if not entry.get("success"):
        return None
    return entry.get("data")


def fetch_review_summary(session, appid: int) -> dict:
    data = get_json(
        session, APPREVIEWS_URL.format(appid=appid),
        params={"json": 1, "language": "all", "purchase_type": "all",
                "num_per_page": 0},
    )
    return data.get("query_summary", {})


def fetch_reviews(session, appid: int, limit: int) -> list[dict]:
    """Pull up to `limit` recent reviews via cursor pagination."""
    out: list[dict] = []
    cursor = "*"
    seen_cursors: set[str] = set()
    while len(out) < limit:
        data = get_json(
            session, APPREVIEWS_URL.format(appid=appid),
            params={
                "json": 1, "filter": "recent", "language": "all",
                "purchase_type": "all", "num_per_page": 100, "cursor": cursor,
            },
        )
        batch = data.get("reviews", [])
        if not batch:
            break
        out.extend(batch)
        cursor = data.get("cursor", "")
        if not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
        time.sleep(0.6)  # be polite
    return out[:limit]


def fetch_steamspy(session, appid: int) -> dict:
    return get_json(
        session, STEAMSPY_URL,
        params={"request": "appdetails", "appid": appid},
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def upsert_game_and_listing(conn: sqlite3.Connection, appid: int,
                            details: dict, steamspy: dict) -> int:
    """Return listing_id, creating game + listing if new. Genres synced."""
    pid = platform_id(conn, PLATFORM_CODE)

    # Reuse existing listing's game if we've seen this appid before.
    row = conn.execute(
        "SELECT listing_id, game_id FROM listings "
        "WHERE platform_id = ? AND external_id = ?",
        (pid, str(appid)),
    ).fetchone()

    name = details.get("name") or steamspy.get("name") or f"app_{appid}"
    developers = ", ".join(details.get("developers", []) or []) or None
    publishers = ", ".join(details.get("publishers", []) or []) or None
    genres = [g["description"] for g in details.get("genres", []) or []]
    is_indie = 1 if any(g.lower() == "indie" for g in genres) else 0
    release_date = (details.get("release_date") or {}).get("date")

    price = details.get("price_overview") or {}
    base_price = (price.get("initial") or 0) / 100 if price else None

    if row is None:
        cur = conn.execute(
            "INSERT INTO games (canonical_name, developer, publisher, "
            "is_indie, first_release_date) VALUES (?, ?, ?, ?, ?)",
            (name, developers, publishers, is_indie, release_date),
        )
        game_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO listings (game_id, platform_id, external_id, "
            "store_url, release_date, base_price_usd) VALUES (?, ?, ?, ?, ?, ?)",
            (game_id, pid, str(appid),
             f"https://store.steampowered.com/app/{appid}",
             release_date, base_price),
        )
        listing_id = cur.lastrowid
    else:
        listing_id, game_id = row["listing_id"], row["game_id"]
        conn.execute(
            "UPDATE games SET developer = ?, publisher = ?, is_indie = ?, "
            "first_release_date = ?, updated_at = datetime('now') "
            "WHERE game_id = ?",
            (developers, publishers, is_indie, release_date, game_id),
        )

    # Sync genres (platform-agnostic taxonomy, tagged source='steam').
    for g in genres:
        conn.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (g,))
        gid = conn.execute(
            "SELECT genre_id FROM genres WHERE name = ?", (g,)
        ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO game_genres (game_id, genre_id, source) "
            "VALUES (?, ?, 'steam')",
            (game_id, gid),
        )

    conn.commit()
    return listing_id


def insert_snapshot(conn: sqlite3.Connection, listing_id: int, details: dict,
                    summary: dict, steamspy: dict) -> None:
    price = details.get("price_overview") or {}
    price_usd = (price.get("final") or 0) / 100 if price else None
    discount = price.get("discount_percent")

    total = summary.get("total_reviews")
    positive = summary.get("total_positive")
    score_pct = (positive / total * 100) if total else None

    owners_min, owners_max = parse_owners(steamspy.get("owners"))
    ccu = steamspy.get("ccu")

    conn.execute(
        "INSERT INTO listing_snapshots (listing_id, price_usd, discount_pct, "
        "review_count_total, review_count_positive, review_score_pct, ccu, "
        "estimated_owners_min, estimated_owners_max) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (listing_id, price_usd, discount, total, positive, score_pct, ccu,
         owners_min, owners_max),
    )
    conn.commit()


def insert_reviews(conn: sqlite3.Connection, listing_id: int,
                   reviews: list[dict]) -> int:
    n = 0
    for r in reviews:
        author = r.get("author") or {}
        cur = conn.execute(
            "INSERT OR IGNORE INTO reviews (listing_id, external_review_id, "
            "posted_at, is_positive, votes_up, votes_funny, "
            "playtime_at_review_min, language, body) "
            "VALUES (?, ?, datetime(?, 'unixepoch'), ?, ?, ?, ?, ?, ?)",
            (
                listing_id,
                r.get("recommendationid"),
                r.get("timestamp_created"),
                1 if r.get("voted_up") else 0,
                r.get("votes_up"),
                r.get("votes_funny"),
                author.get("playtime_at_review"),
                r.get("language"),
                r.get("review"),
            ),
        )
        n += cur.rowcount  # 1 on insert, 0 when ignored (already stored)
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def collect_app(conn, session, appid: int, review_limit: int) -> bool:
    details = fetch_appdetails(session, appid)
    if details is None:
        print(f"  [skip] appid {appid}: no store data (delisted/region-locked?)")
        return False
    # SteamSpy is best-effort; failure shouldn't abort the app.
    try:
        steamspy = fetch_steamspy(session, appid)
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] SteamSpy failed for {appid}: {e}")
        steamspy = {}

    summary = fetch_review_summary(session, appid)
    listing_id = upsert_game_and_listing(conn, appid, details, steamspy)
    insert_snapshot(conn, listing_id, details, summary, steamspy)

    reviews = fetch_reviews(session, appid, review_limit) if review_limit else []
    insert_reviews(conn, listing_id, reviews)

    print(f"  [ok] {details.get('name')} (appid {appid}): "
          f"{summary.get('total_reviews', 0)} reviews summarized, "
          f"{len(reviews)} review texts stored")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect Steam app data.")
    parser.add_argument("appids", nargs="+", type=int, help="Steam appids")
    parser.add_argument("--reviews", type=int, default=200,
                        help="Max review texts per app (0 = summary only)")
    args = parser.parse_args(argv)

    session = make_session()
    conn = get_connection()
    run_id = start_run(conn, PLATFORM_CODE, "steam.py")
    seen = 0
    try:
        for appid in args.appids:
            print(f"Collecting appid {appid} ...")
            if collect_app(conn, session, appid, args.reviews):
                seen += 1
            time.sleep(1.0)
        finish_run(conn, run_id, "success", seen)
    except Exception as e:  # noqa: BLE001
        finish_run(conn, run_id, "failed", seen, notes=str(e))
        print(f"Run failed: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"Done. {seen}/{len(args.appids)} apps collected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
