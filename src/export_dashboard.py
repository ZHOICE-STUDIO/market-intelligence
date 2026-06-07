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

# Forecast tuning. HORIZON = how far ahead we project. 12 months ~= a focused
# indie dev cycle, so the projection lands on a realistic ship date: the point
# is to start a genre while it's still climbing toward where you'll launch.
FORECAST_HORIZON = 12
FORECAST_HISTORY = 36          # months of history fed to the model / shown on chart
FORECAST_MIN_RECENT = 3000     # min review volume in the last 6 mo (skip thin/noisy genres)


def mean(a):
    v = [x for x in a if x is not None]
    return sum(v) / len(v) if v else 0.0


def median(a):
    v = sorted(x for x in a if x is not None)
    if not v:
        return 0.0
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


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


def build_momentum(conn) -> dict:
    """Genre momentum from monthly review history: recent 6 months vs the prior
    6, plus a market-wide monthly trend. Market-wide (not affected by the
    dashboard's indie/price filters)."""
    # reviews per (month, genre); a game counts in each of its IGDB genres
    rows = conn.execute(
        "SELECT rh.period_start AS m, ge.name AS genre, "
        "SUM(COALESCE(rh.up,0)+COALESCE(rh.down,0)) AS rev "
        "FROM review_history rh "
        "JOIN listings l ON rh.listing_id = l.listing_id "
        "JOIN game_genres gg ON l.game_id = gg.game_id AND gg.source='igdb-genre' "
        "JOIN genres ge ON gg.genre_id = ge.genre_id "
        "GROUP BY rh.period_start, ge.name"
    ).fetchall()
    market = conn.execute(
        "SELECT period_start AS m, SUM(COALESCE(up,0)+COALESCE(down,0)) AS rev "
        "FROM review_history GROUP BY period_start ORDER BY period_start"
    ).fetchall()
    if not market:
        return {"available": False}

    months = [r["m"] for r in market]
    axis = months[:-1] if len(months) > 1 else months  # drop current partial month
    recent = set(axis[-6:])
    prior = set(axis[-12:-6])

    by_genre: dict[str, dict] = {}
    for r in rows:
        d = by_genre.setdefault(r["genre"], {"recent": 0, "prior": 0})
        if r["m"] in recent:
            d["recent"] += r["rev"]
        elif r["m"] in prior:
            d["prior"] += r["rev"]

    genres = []
    for name, d in by_genre.items():
        if d["recent"] < 500:          # ignore thin/noisy genres
            continue
        ratio = (d["recent"] / d["prior"]) if d["prior"] else None
        genres.append({"name": name, "recent": d["recent"], "prior": d["prior"],
                       "change_pct": round((ratio - 1) * 100, 1) if ratio else None})
    genres.sort(key=lambda x: (x["change_pct"] is None, -(x["change_pct"] or 0)))

    # Trend excludes the current partial month (axis already dropped it) so the
    # line doesn't crash misleadingly at the end.
    trend_map = {r["m"]: r["rev"] for r in market}
    trend = [{"month": m, "reviews": trend_map[m]} for m in axis[-24:]]
    return {
        "available": True,
        "as_of": axis[-1] if axis else None,
        "window": "last 6 months vs the prior 6 (review volume)",
        "genres": genres,
        "market": trend,
    }


def _add_months(month_first: str, k: int) -> str:
    """'YYYY-MM-01' + k months -> 'YYYY-MM-01'."""
    y, m, _ = month_first.split("-")
    idx = (int(y) * 12 + (int(m) - 1)) + k
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}-01"


def _smooth(series: list[float], win: int = 3) -> list[float]:
    """Trailing moving average to take the edge off monthly noise before fitting."""
    out = []
    for i in range(len(series)):
        lo = max(0, i - win + 1)
        chunk = series[lo:i + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def _holt(series: list[float], alpha: float = 0.5, beta: float = 0.2):
    """Holt's linear exponential smoothing (level + trend).

    A transparent, dependency-free way to extract a current level and a sustained
    slope from a noisy monthly series, then extrapolate. Returns (level, trend,
    residual_std) where residual_std drives the forecast's confidence band.
    """
    if len(series) < 2:
        return (series[0] if series else 0.0), 0.0, 0.0
    level = series[0]
    trend = series[1] - series[0]
    resids = []
    for i in range(1, len(series)):
        prev_level = level
        forecast = level + trend
        level = alpha * series[i] + (1 - alpha) * (level + trend)
        trend = beta * (level - prev_level) + (1 - beta) * trend
        resids.append(series[i] - forecast)
    std = (sum(r * r for r in resids) / len(resids)) ** 0.5 if resids else 0.0
    return level, trend, std


def build_forecast(conn) -> dict:
    """Per-genre review-volume trend (past->present) plus a HORIZON-month forecast.

    Answers "what should we build to ride a trend that's still rising when we
    ship?" by (1) reconstructing each genre's monthly review volume over its
    recent history, (2) fitting Holt linear smoothing to get its current level
    and slope, (3) projecting forward, and (4) ranking genres that are both
    rising now AND still a sizeable market at the projected ship date.
    Market-wide (not affected by the dashboard's indie/price filters)."""
    rows = conn.execute(
        "SELECT rh.period_start AS m, ge.name AS genre, "
        "SUM(COALESCE(rh.up,0)+COALESCE(rh.down,0)) AS rev "
        "FROM review_history rh "
        "JOIN listings l ON rh.listing_id = l.listing_id "
        "JOIN game_genres gg ON l.game_id = gg.game_id AND gg.source='igdb-genre' "
        "JOIN genres ge ON gg.genre_id = ge.genre_id "
        "GROUP BY rh.period_start, ge.name"
    ).fetchall()
    market = conn.execute(
        "SELECT period_start AS m, SUM(COALESCE(up,0)+COALESCE(down,0)) AS rev "
        "FROM review_history GROUP BY period_start ORDER BY period_start"
    ).fetchall()
    if not market or len(market) < 8:
        return {"available": False}

    months = [r["m"] for r in market]
    axis = months[:-1] if len(months) > 1 else months   # drop current partial month
    window = axis[-FORECAST_HISTORY:]
    fc_months = [_add_months(window[-1], h + 1) for h in range(FORECAST_HORIZON)]

    # how crowded each genre is (supply / competition signal)
    comp = {r["name"]: r["c"] for r in conn.execute(
        "SELECT ge.name AS name, COUNT(DISTINCT gg.game_id) AS c "
        "FROM game_genres gg JOIN genres ge ON gg.genre_id = ge.genre_id "
        "WHERE gg.source='igdb-genre' GROUP BY ge.name"
    ).fetchall()}

    by_genre: dict[str, dict] = {}
    for r in rows:
        by_genre.setdefault(r["genre"], {})[r["m"]] = r["rev"]

    genres = []
    for name, mser in by_genre.items():
        series = [float(mser.get(m, 0)) for m in window]
        if sum(series[-6:]) < FORECAST_MIN_RECENT:
            continue
        level, trend, std = _holt(_smooth(series, 3))
        forecast = [max(0.0, level + (h + 1) * trend) for h in range(FORECAST_HORIZON)]
        band = 1.28 * std  # ~80% interval
        now = mean(series[-3:])                      # current level (last 3 mo avg)
        proj = forecast[-1]                          # level at the ship date
        change = round((proj / now - 1) * 100, 1) if now else None
        yoy = None
        if len(series) >= 24:
            prior = sum(series[-24:-12]) or 1
            yoy = round((sum(series[-12:]) / prior - 1) * 100, 1)
        genres.append({
            "name": name,
            "history": [round(v) for v in series[-24:]],
            "months": [m[:7] for m in window[-24:]],
            "fc_months": [m[:7] for m in fc_months],
            "forecast": [round(v) for v in forecast],
            "upper": [round(v + band) for v in forecast],
            "lower": [round(max(0.0, v - band)) for v in forecast],
            "now": round(now),
            "proj": round(proj),
            "change_pct": change,            # projected change by ship date
            "yoy_pct": yoy,                  # last 12 mo vs the 12 before (momentum so far)
            "rising": trend > 0,
            "competition": comp.get(name, 0),
        })

    if not genres:
        return {"available": False}

    # "Catchable" = a market that's still big enough to matter AND projected up:
    # rising now and projected level at/above the median across qualifying genres.
    med_proj = median([g["proj"] for g in genres])
    for g in genres:
        g["catchable"] = bool(g["rising"] and g["proj"] >= med_proj
                              and (g["change_pct"] or 0) > 0)
    genres.sort(key=lambda g: (g["change_pct"] is None, -(g["change_pct"] or 0)))

    return {
        "available": True,
        "as_of": window[-1][:7],
        "horizon": FORECAST_HORIZON,
        "genres": genres,
    }


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
        "momentum": build_momentum(conn),
        "forecast": build_forecast(conn),
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
