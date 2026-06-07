"""Export aggregated market data for the web dashboard.

Reads the SQLite DB and writes dashboard/data.js (a single
`window.DASHBOARD_DATA = {...}` assignment). Using a .js file instead of fetching
.json means the dashboard opens straight from disk on desktop AND works on
GitHub Pages -- no local server, no CORS headaches.

Revenue is ESTIMATED via the Boxleiter method: sales ~= total_reviews * 30,
revenue ~= sales * price. It is a rough proxy, labelled as such in the UI.

Usage:
    python -m src.export_dashboard
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from src.db.database import REPO_ROOT, get_connection

OUT_PATH = REPO_ROOT / "dashboard" / "data.js"
BOXLEITER_MULTIPLIER = 30  # reviews -> estimated units sold (conservative)


def _latest_snapshot_rows(conn):
    """One row per game using its most recent snapshot, with genres/themes."""
    sql = """
    WITH latest AS (
        SELECT s.*,
               ROW_NUMBER() OVER (PARTITION BY s.listing_id
                                  ORDER BY s.captured_at DESC, s.snapshot_id DESC) rn
        FROM listing_snapshots s
    )
    SELECT g.game_id, g.canonical_name, g.developer, g.is_indie,
           g.first_release_date, g.summary,
           l.base_price_usd, l.external_id AS appid,
           ls.price_usd, ls.review_count_total, ls.review_score_pct,
           ls.estimated_owners_min, ls.estimated_owners_max, ls.ccu
    FROM games g
    JOIN listings l ON g.game_id = l.game_id
    JOIN latest ls ON l.listing_id = ls.listing_id AND ls.rn = 1
    """
    return conn.execute(sql).fetchall()


def _game_genres(conn, source="igdb-genre"):
    rows = conn.execute(
        "SELECT gg.game_id, ge.name FROM game_genres gg "
        "JOIN genres ge ON gg.genre_id = ge.genre_id WHERE gg.source = ?",
        (source,),
    ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["game_id"], []).append(r["name"])
    return out


def _est_revenue(reviews, price):
    if not reviews or price is None:
        return 0.0
    return reviews * BOXLEITER_MULTIPLIER * price


def build_payload(conn) -> dict:
    rows = _latest_snapshot_rows(conn)
    genres_by_game = _game_genres(conn, "igdb-genre")
    themes_by_game = _game_genres(conn, "igdb-theme")

    games = []
    for r in rows:
        price = r["price_usd"] if r["price_usd"] is not None else r["base_price_usd"]
        reviews = r["review_count_total"] or 0
        game_genres = genres_by_game.get(r["game_id"], [])
        # Discovery games never got is_indie (SteamSpy `all` has no genre);
        # derive it from IGDB genres instead so the indie filter is accurate.
        is_indie = 1 if any(x.lower() == "indie" for x in game_genres) else 0
        games.append({
            "name": r["canonical_name"],
            "developer": r["developer"],
            "indie": is_indie,
            "year": (r["first_release_date"] or "")[:4] or None,
            "price": round(price, 2) if price is not None else None,
            "reviews": reviews,
            "score": round(r["review_score_pct"], 1) if r["review_score_pct"] else None,
            "owners": r["estimated_owners_max"],
            "ccu": r["ccu"],
            "genres": game_genres,
            "themes": themes_by_game.get(r["game_id"], []),
            "rev": round(_est_revenue(reviews, price)),
            "summary": r["summary"],
            "appid": r["appid"],
        })

    # All aggregation (KPIs, genre opportunity, themes, year trend) happens in
    # the browser so filters (indie-only, price range, search) recompute live.
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "games": games,
        "notes": {
            "revenue": f"Estimated revenue = reviews x {BOXLEITER_MULTIPLIER} x price (Boxleiter method). A rough proxy, not actual sales.",
            "bias": "Universe = top ~1000 games by ownership, so it skews toward established hits. Newer/smaller games are under-represented until the long tail is added.",
        },
    }


def main() -> int:
    conn = get_connection()
    try:
        payload = build_payload(conn)
    finally:
        conn.close()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        "window.DASHBOARD_DATA = " + json.dumps(payload, ensure_ascii=False) + ";",
        encoding="utf-8",
    )
    print(f"Wrote {OUT_PATH} ({len(payload['games'])} games)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
