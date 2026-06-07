"""Steam discovery collector (Tier 1: broad, cheap market sweep).

Pulls SteamSpy's `all` endpoint, which returns games ranked by owner count,
1000 per page -- INCLUDING the long tail of games that flopped. That long tail
is what defeats survivorship bias: to judge whether a genre is a real
opportunity we must see the failures, not only the hits.

Per game it stores a lightweight snapshot (owners, ccu, price, positive/negative
review counts) WITHOUT hitting per-app endpoints, so a single request seeds
~1000 games. NOTE: the `all` endpoint does NOT return genre/tags or release
date -- those need enrichment (IGDB batch, or per-app steam.py on a subset).

SteamSpy rate-limits the `all` request to ~1/minute, so multi-page runs sleep
60s between pages.

Usage:
    python -m src.collectors.discover_steam --pages 1          # top 1000
    python -m src.collectors.discover_steam --pages 5 --start 0 # top 5000
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time

from src.collectors.base import (
    finish_run,
    get_json,
    make_session,
    parse_owners,
    platform_id,
    start_run,
)
from src.db.database import get_connection

STEAMSPY_URL = "https://steamspy.com/api.php"
PLATFORM_CODE = "steam"
PAGE_DELAY_SEC = 60  # SteamSpy `all` rate limit


def fetch_all_page(session, page: int) -> dict:
    """One page (~1000 games) of SteamSpy `all`, keyed by appid."""
    return get_json(session, STEAMSPY_URL,
                    params={"request": "all", "page": page})


def upsert_discovery(conn: sqlite3.Connection, pid: int, appid: int,
                     info: dict) -> None:
    """Create game + listing if new, then insert a lightweight snapshot."""
    row = conn.execute(
        "SELECT listing_id, game_id FROM listings "
        "WHERE platform_id = ? AND external_id = ?",
        (pid, str(appid)),
    ).fetchone()

    name = info.get("name") or f"app_{appid}"
    developer = info.get("developer") or None
    publisher = info.get("publisher") or None
    genres = [g.strip() for g in (info.get("genre") or "").split(",") if g.strip()]
    is_indie = 1 if any(g.lower() == "indie" for g in genres) else 0

    # SteamSpy prices are integer cents as strings.
    def cents(v):
        try:
            return int(v) / 100
        except (TypeError, ValueError):
            return None

    base_price = cents(info.get("initialprice"))
    price_usd = cents(info.get("price"))

    if row is None:
        cur = conn.execute(
            "INSERT INTO games (canonical_name, developer, publisher, is_indie) "
            "VALUES (?, ?, ?, ?)",
            (name, developer, publisher, is_indie),
        )
        game_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO listings (game_id, platform_id, external_id, "
            "store_url, base_price_usd) VALUES (?, ?, ?, ?, ?)",
            (game_id, pid, str(appid),
             f"https://store.steampowered.com/app/{appid}", base_price),
        )
        listing_id = cur.lastrowid
    else:
        listing_id, game_id = row["listing_id"], row["game_id"]

    # Genres (coarse). Tags come later via deep enrichment.
    for g in genres:
        conn.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (g,))
        gid = conn.execute(
            "SELECT genre_id FROM genres WHERE name = ?", (g,)
        ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO game_genres (game_id, genre_id, source) "
            "VALUES (?, ?, 'steamspy')",
            (game_id, gid),
        )

    owners_min, owners_max = parse_owners(info.get("owners"))
    positive = info.get("positive")
    negative = info.get("negative")
    total = None
    score_pct = None
    if positive is not None and negative is not None:
        total = positive + negative
        score_pct = (positive / total * 100) if total else None

    conn.execute(
        "INSERT INTO listing_snapshots (listing_id, price_usd, "
        "review_count_total, review_count_positive, review_score_pct, ccu, "
        "estimated_owners_min, estimated_owners_max) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (listing_id, price_usd, total, positive, score_pct,
         info.get("ccu"), owners_min, owners_max),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover Steam games (Tier 1).")
    parser.add_argument("--pages", type=int, default=1,
                        help="How many pages of ~1000 games to pull")
    parser.add_argument("--start", type=int, default=0, help="Starting page")
    args = parser.parse_args(argv)

    session = make_session()
    conn = get_connection()
    pid = platform_id(conn, PLATFORM_CODE)
    run_id = start_run(conn, PLATFORM_CODE, "discover_steam.py")
    seen = 0
    try:
        for i in range(args.pages):
            page = args.start + i
            print(f"Fetching page {page} (~1000 games) ...")
            data = fetch_all_page(session, page)
            if not data:
                print("  empty page, stopping.")
                break
            for appid_str, info in data.items():
                try:
                    upsert_discovery(conn, pid, int(appid_str), info)
                    seen += 1
                except (ValueError, sqlite3.Error) as e:
                    print(f"  [warn] appid {appid_str}: {e}")
            conn.commit()
            print(f"  page {page} done. total games seen: {seen}")
            if i < args.pages - 1:
                time.sleep(PAGE_DELAY_SEC)
        finish_run(conn, run_id, "success", seen)
    except Exception as e:  # noqa: BLE001
        finish_run(conn, run_id, "failed", seen, notes=str(e))
        print(f"Run failed: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"Done. {seen} games in universe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
