"""Shared collector infrastructure: HTTP session with retries + run logging.

Every platform collector builds on this so retry/backoff and bookkeeping are
identical across Steam, mobile, etc.
"""
from __future__ import annotations

import sqlite3
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

USER_AGENT = "zhoice-market-intelligence/0.1 (research)"


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


@retry(
    retry=retry_if_exception_type((requests.RequestException,)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)
def get_json(session: requests.Session, url: str, params: dict | None = None,
             timeout: int = 20) -> Any:
    """GET with exponential backoff. Raises on repeated failure."""
    resp = session.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def parse_owners(owners: str | None) -> tuple[int | None, int | None]:
    """SteamSpy owners string '1,000,000 .. 2,000,000' -> (1000000, 2000000)."""
    if not owners or ".." not in owners:
        return None, None
    try:
        lo, hi = owners.split("..")
        return int(lo.replace(",", "").strip()), int(hi.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None, None


def platform_id(conn: sqlite3.Connection, code: str) -> int:
    row = conn.execute(
        "SELECT platform_id FROM platforms WHERE code = ?", (code,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown platform code: {code!r}")
    return row[0]


def start_run(conn: sqlite3.Connection, platform_code: str, collector: str) -> int:
    pid = platform_id(conn, platform_code)
    cur = conn.execute(
        "INSERT INTO collection_runs (platform_id, collector, status) "
        "VALUES (?, ?, 'running')",
        (pid, collector),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str,
               items_seen: int = 0, notes: str | None = None) -> None:
    conn.execute(
        "UPDATE collection_runs SET finished_at = datetime('now'), "
        "status = ?, items_seen = ?, notes = ? WHERE run_id = ?",
        (status, items_seen, notes, run_id),
    )
    conn.commit()
