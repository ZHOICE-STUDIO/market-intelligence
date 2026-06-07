"""IGDB enrichment collector.

Fills the gaps SteamSpy can't: genre, sub-genre (themes), and an accurate
release date. IGDB links to Steam via its `external_games` table
(external_game_source = 1 means Steam; the old `category` field is deprecated),
so we send the Steam appids we already have and get matched games back in
batches -- a few hundred per request instead of one-by-one.

Auth: OAuth client-credentials against Twitch (IGDB is a Twitch/Amazon service).
Credentials live in .env (IGDB_CLIENT_ID / IGDB_CLIENT_SECRET).

Usage:
    python -m src.collectors.igdb              # enrich all Steam listings
    python -m src.collectors.igdb --limit 200  # only first N (testing)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from src.collectors.base import finish_run, make_session, platform_id, start_run
from src.db.database import REPO_ROOT, get_connection

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IGDB_BASE = "https://api.igdb.com/v4"
STEAM_SOURCE = 1  # IGDB external_game_source value for Steam
BATCH = 200                  # appids per query
PLATFORM_CODE = "steam"


def get_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(TOKEN_URL, params={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }, timeout=20)
    resp.raise_for_status()
    return resp.json()["access_token"]


def igdb_query(session, endpoint: str, body: str, client_id: str,
               token: str) -> list[dict]:
    """POST an Apicalypse query. Retries once on 429 (rate limit)."""
    url = f"{IGDB_BASE}/{endpoint}"
    headers = {"Client-ID": client_id, "Authorization": f"Bearer {token}"}
    for attempt in range(3):
        resp = session.post(url, headers=headers, data=body, timeout=30)
        if resp.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return []


def steam_listings(conn: sqlite3.Connection, pid: int,
                   limit: int | None) -> list[tuple[str, int]]:
    """Return (steam_appid, game_id) for Steam listings to enrich."""
    sql = ("SELECT external_id, game_id FROM listings WHERE platform_id = ? "
           "ORDER BY listing_id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [(r["external_id"], r["game_id"])
            for r in conn.execute(sql, (pid,)).fetchall()]


def _unix_to_iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def apply_enrichment(conn: sqlite3.Connection, game_id: int, game: dict) -> None:
    """Update release date + concept summary; insert genres and themes."""
    release = _unix_to_iso(game.get("first_release_date"))
    summary = game.get("summary")
    conn.execute(
        "UPDATE games SET "
        "first_release_date = COALESCE(?, first_release_date), "
        "summary = COALESCE(?, summary), "
        "updated_at = datetime('now') WHERE game_id = ?",
        (release, summary, game_id),
    )

    def add_tags(items, source):
        for it in items or []:
            name = it.get("name")
            if not name:
                continue
            conn.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (name,))
            gid = conn.execute(
                "SELECT genre_id FROM genres WHERE name = ?", (name,)
            ).fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO game_genres (game_id, genre_id, source) "
                "VALUES (?, ?, ?)",
                (game_id, gid, source),
            )

    add_tags(game.get("genres"), "igdb-genre")
    add_tags(game.get("themes"), "igdb-theme")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enrich games via IGDB.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max listings to enrich (for testing)")
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env")
    client_id = os.getenv("IGDB_CLIENT_ID")
    client_secret = os.getenv("IGDB_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Missing IGDB_CLIENT_ID / IGDB_CLIENT_SECRET in .env",
              file=sys.stderr)
        return 1

    token = get_token(client_id, client_secret)
    session = make_session()
    conn = get_connection()
    pid = platform_id(conn, PLATFORM_CODE)
    run_id = start_run(conn, PLATFORM_CODE, "igdb.py")

    listings = steam_listings(conn, pid, args.limit)
    appid_to_game = {appid: gid for appid, gid in listings}
    appids = list(appid_to_game.keys())
    matched = 0

    try:
        for i in range(0, len(appids), BATCH):
            chunk = appids[i:i + BATCH]
            uid_list = ",".join(f'"{a}"' for a in chunk)
            body = (
                "fields uid, game.first_release_date, game.summary, "
                "game.genres.name, game.themes.name; "
                f"where external_game_source = {STEAM_SOURCE} "
                f"& uid = ({uid_list}); "
                "limit 500;"
            )
            rows = igdb_query(session, "external_games", body, client_id, token)
            for row in rows:
                uid = row.get("uid")
                game = row.get("game")
                if not uid or not isinstance(game, dict):
                    continue
                game_id = appid_to_game.get(uid)
                if game_id is None:
                    continue
                apply_enrichment(conn, game_id, game)
                matched += 1
            conn.commit()
            print(f"  batch {i // BATCH + 1}: {len(chunk)} sent, "
                  f"{matched} matched so far")
            time.sleep(0.3)  # IGDB allows ~4 req/s; stay well under
        finish_run(conn, run_id, "success", matched)
    except Exception as e:  # noqa: BLE001
        finish_run(conn, run_id, "failed", matched, notes=str(e))
        print(f"Run failed: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"Done. {matched}/{len(appids)} games matched & enriched in IGDB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
