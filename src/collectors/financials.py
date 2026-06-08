"""Public-company financials collector.

Most game studios are private and have no public financials. A few dozen are
publicly listed (or are owned by a listed parent). This collector maps the
developer/publisher names we already store to stock tickers, pulls real
financials from public markets via yfinance, normalizes everything to USD, and
writes them to `company_financials` keyed by the DB name so the dashboard can
show a studio's actual revenue / profit / market cap next to its Steam footprint.

Where a label is a subsidiary (e.g. Rockstar -> Take-Two), the financials are
the listed PARENT's and `is_parent` is set so the UI can say so honestly.

Usage:
    python -m src.collectors.financials
"""
from __future__ import annotations

import sys
from datetime import date

from src.db.database import get_connection

# DB developer/publisher name -> (ticker, listed entity, is_parent)
# is_parent=True means the ticker is a diversified parent, not this label alone.
COMPANY_TICKERS: dict[str, tuple[str, str, bool]] = {
    "Electronic Arts":                         ("EA",          "Electronic Arts",        False),
    "Ubisoft":                                 ("UBI.PA",      "Ubisoft Entertainment",  False),
    "CAPCOM Co., Ltd.":                        ("9697.T",      "Capcom",                 False),
    "Capcom":                                  ("9697.T",      "Capcom",                 False),
    "KOEI TECMO GAMES CO., LTD.":              ("3635.T",      "Koei Tecmo",             False),
    "Square Enix":                             ("9684.T",      "Square Enix Holdings",   False),
    "SEGA":                                    ("6460.T",      "Sega Sammy Holdings",    True),
    "BANDAI NAMCO Entertainment":              ("7832.T",      "Bandai Namco Holdings",  True),
    "Paradox Interactive":                     ("PDX.ST",      "Paradox Interactive",    False),
    "Devolver Digital":                        ("DEVO.L",      "Devolver Digital",       False),
    "Team17":                                  ("EVPL.L",      "Everplay Group (Team17)", False),
    "tinyBuild":                               ("TBLD.L",      "tinyBuild",              False),
    "Nacon":                                   ("NACON.PA",    "Nacon",                  False),
    "505 Games":                               ("DIB.MI",      "Digital Bros",           True),
    "THQ Nordic":                              ("EMBRAC-B.ST", "Embracer Group",         True),
    "Deep Silver":                             ("EMBRAC-B.ST", "Embracer Group",         True),
    "2K":                                      ("TTWO",        "Take-Two Interactive",   True),
    "Rockstar Games":                          ("TTWO",        "Take-Two Interactive",   True),
    "Bethesda Softworks":                      ("MSFT",        "Microsoft",              True),
    "Xbox Game Studios":                       ("MSFT",        "Microsoft",              True),
    "Activision":                              ("MSFT",        "Microsoft",              True),
    "Warner Bros. Interactive Entertainment":  ("WBD",         "Warner Bros. Discovery", True),
    "CD PROJEKT RED":                          ("CDR.WA",      "CD Projekt",             False),
    "CD PROJEKT":                              ("CDR.WA",      "CD Projekt",             False),
    "Konami":                                  ("9766.T",      "Konami Group",           True),
    "Konami Digital Entertainment":            ("9766.T",      "Konami Group",           True),
    "Frontier Developments":                   ("FDEV.L",      "Frontier Developments",  False),
    "Focus Entertainment":                     ("ALPUL.PA",    "Pullup Entertainment",   False),
    "Nintendo":                                ("7974.T",      "Nintendo",               False),
}


def _fx_to_usd(session_cache: dict, currency: str):
    """USD per 1 unit of `currency` (1.0 for USD). Cached. None if unavailable."""
    if not currency or currency == "USD":
        return 1.0
    if currency in session_cache:
        return session_cache[currency]
    import yfinance as yf
    rate = None
    try:
        hist = yf.Ticker(f"{currency}USD=X").history(period="5d")
        if not hist.empty:
            rate = float(hist["Close"].dropna().iloc[-1])
    except Exception:  # noqa: BLE001
        rate = None
    session_cache[currency] = rate
    return rate


def fetch_ticker(ticker: str, fx_cache: dict) -> dict | None:
    """Return USD-normalized financials for a ticker, or None on failure."""
    import yfinance as yf
    try:
        info = yf.Ticker(ticker).info
    except Exception as e:  # noqa: BLE001
        print(f"  ! {ticker}: {e}", file=sys.stderr)
        return None
    if not info or info.get("marketCap") is None and info.get("totalRevenue") is None:
        return None
    cur = info.get("financialCurrency") or info.get("currency") or "USD"
    fx = _fx_to_usd(fx_cache, cur)
    if fx is None:
        print(f"  ! {ticker}: no FX for {cur}, skipping", file=sys.stderr)
        return None

    def usd(v):
        return round(v * fx) if isinstance(v, (int, float)) else None

    return {
        "ticker": ticker,
        "currency": cur,
        "market_cap_usd": usd(info.get("marketCap")),
        "revenue_usd": usd(info.get("totalRevenue")),
        "net_income_usd": usd(info.get("netIncomeToCommon")),
        "gross_profit_usd": usd(info.get("grossProfits")),
        "profit_margin": info.get("profitMargins"),
        "employees": info.get("fullTimeEmployees"),
    }


def main(argv: list[str] | None = None) -> int:
    conn = get_connection()
    today = date.today().isoformat()
    fx_cache: dict = {}
    by_ticker: dict[str, dict | None] = {}
    written = 0

    try:
        for company, (ticker, listed, is_parent) in COMPANY_TICKERS.items():
            if ticker not in by_ticker:
                by_ticker[ticker] = fetch_ticker(ticker, fx_cache)
                fin = by_ticker[ticker]
                tag = "ok" if fin else "FAILED"
                print(f"  {ticker:12} {listed:28} {tag}")
            fin = by_ticker[ticker]
            if not fin:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO company_financials "
                "(company, ticker, listed_name, is_parent, currency, market_cap_usd, "
                " revenue_usd, net_income_usd, gross_profit_usd, profit_margin, "
                " employees, as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (company, ticker, listed, 1 if is_parent else 0, fin["currency"],
                 fin["market_cap_usd"], fin["revenue_usd"], fin["net_income_usd"],
                 fin["gross_profit_usd"], fin["profit_margin"], fin["employees"], today),
            )
            written += 1
        conn.commit()
    finally:
        conn.close()

    print(f"Done. {written} company rows written "
          f"({sum(1 for v in by_ticker.values() if v)}/{len(by_ticker)} tickers ok).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
