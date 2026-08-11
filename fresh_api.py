"""
The fresh-tenders API. One dataset, served from fresh_tenders.db.

Everything here is read-only. The data is refreshed by fresh_scraper.py, which
runs on a schedule on the same host -- see SETUP.txt.

Auth: every endpoint except / and /health needs  X-API-Key: <key>  matching
config.API_ACCESS_KEY.

Endpoints:
    GET /                       what's available (no key needed)
    GET /health                 liveness (no key needed)
    GET /status                 row counts + when the scraper last ran
    GET /delta                  ONLY what changed since a date (daily poll)
    GET /tenders                the feed, filterable and paginated
    GET /tenders/all            the ENTIRE feed in one response
    GET /tenders/{result_id}    one tender with its ranked bidder list
    GET /bidders?name=...       every tender a bidder took part in

The whole feed is ~2.5 K tenders, so /tenders/all is genuinely servable in a
single response -- which is the point of this API existing separately from the
677 K one, where that was impossible.
"""
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException

import config
import fresh_store

app = FastAPI(
    title="Tender247 Fresh Tenders API",
    description="Read-only API over the account's Fresh Results feed, refreshed daily.",
    version="2.0.0",
)

public = APIRouter()


def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    if config.API_ACCESS_KEY and x_api_key != config.API_ACCESS_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")


router = APIRouter(dependencies=[Depends(require_api_key)])

BIDDER_COLS = "bidder_name, technical_status, financial_status, aoc_status, bid_value, bidder_rank"

#: Sorts L1/L2/... and bare 1/2/... ranks numerically; unranked bidders last.
RANK_ORDER = ("CASE WHEN bidder_rank = '' THEN 999999 "
              "ELSE CAST(REPLACE(REPLACE(bidder_rank,'L',''),'l','') AS INTEGER) END")


def _connect():
    conn = sqlite3.connect(fresh_store.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


STAGES = ("Technical", "Financial", "AOC")


def _attach_stage_dates(conn, items: list) -> None:
    """
    Adds technical_date / financial_date / aoc_date to each tender, plus a
    stage_history block.

    tender247 exposes only the CURRENT stage and its date. These per-stage
    dates are built by this system observing transitions night after night, so
    a tender first met at AOC will only ever have aoc_date -- its earlier dates
    were never published and cannot be recovered.
    """
    if not items:
        return
    ids = [i["result_id"] for i in items]
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT result_id, stage, stage_date FROM stage_history "
        f"WHERE result_id IN ({marks})", ids,
    ).fetchall()
    by_id = {}
    for row in rows:
        by_id.setdefault(row["result_id"], {})[row["stage"]] = row["stage_date"]

    for item in items:
        hist = by_id.get(item["result_id"], {})
        for stage in STAGES:
            item[f"{stage.lower()}_date"] = hist.get(stage) or ""
        item["stage_history"] = hist


def _bidders_for(conn, result_ids: list) -> dict:
    """One query for a whole page rather than one per tender."""
    if not result_ids:
        return {}
    marks = ",".join("?" * len(result_ids))
    rows = conn.execute(
        f"SELECT result_id, {BIDDER_COLS} FROM bidders "
        f"WHERE result_id IN ({marks}) ORDER BY result_id, {RANK_ORDER}",
        result_ids,
    ).fetchall()
    out = {}
    for row in rows:
        item = dict(row)
        out.setdefault(item.pop("result_id"), []).append(item)
    return out


def _filters(stage, organization, location, tender_number, winner,
             date_from, date_to, since, search):
    where, params = [], []
    if stage:
        where.append("stage = ?"); params.append(stage)
    if organization:
        where.append("organization_name LIKE ?"); params.append(f"%{organization}%")
    if location:
        where.append("location LIKE ?"); params.append(f"%{location}%")
    if tender_number:
        where.append("tender_number = ?"); params.append(tender_number)
    if winner:
        where.append("winner_bidder_name LIKE ?"); params.append(f"%{winner}%")
    if date_from:
        where.append("status_update_date >= ?"); params.append(date_from)
    if date_to:
        where.append("status_update_date <= ?"); params.append(date_to)
    if since:
        where.append("first_seen_at >= ?"); params.append(since)
    if search:
        where.append("(title LIKE ? OR tender_number LIKE ? OR organization_name LIKE ?)")
        params += [f"%{search}%"] * 3
    return (f"WHERE {' AND '.join(where)}" if where else ""), params


@public.get("/")
def index():
    return {
        "service": "Tender247 Fresh Tenders API",
        "what": "The account's Fresh Results feed, re-scraped every day.",
        "auth": "Header  X-API-Key: <your key>  on everything except / and /health",
        "docs": "/docs",
        "endpoints": {
            "/status": "Row counts and when the scraper last ran",
            "/tenders/all": "THE WHOLE FEED IN ONE REQUEST - no paging",
            "/delta": ("ONLY what changed. ?since=YYYY-MM-DD or ?days=1. Returns "
                       "`new` (never seen before) and `updated` (stage/winner moved) "
                       "separately. Poll this daily instead of re-pulling everything."),
            "/tenders": ("Filterable + paginated. Filters: stage, organization, location, "
                         "tender_number, winner, date_from, date_to, since, search, "
                         "with_bidders, page, page_size"),
            "/tenders/{result_id}": "One tender with its ranked bidder list",
            "/bidders?name=...": "Every tender a bidder took part in (partial match)",
        },
        "examples": [
            "/tenders/all",
            "/delta?days=1",
            "/delta?since=2026-08-09",
            "/tenders?stage=AOC&page_size=100",
            "/tenders?since=2026-08-08",
            "/tenders?search=road&page_size=50",
            "/bidders?name=aman enterprises",
        ],
    }


@public.get("/health")
def health():
    return {"status": "ok"}


@router.get("/status")
def status():
    conn = _connect()
    try:
        last = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        counts = {
            "tenders": conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0],
            "bidders": conn.execute("SELECT COUNT(*) FROM bidders").fetchone()[0],
        }
        stages = conn.execute(
            "SELECT stage, COUNT(*) n FROM tenders GROUP BY stage ORDER BY n DESC"
        ).fetchall()
        newest = conn.execute("SELECT MAX(first_seen_at) FROM tenders").fetchone()[0]
    finally:
        conn.close()
    return {
        "counts": counts,
        "by_stage": [dict(r) for r in stages],
        "newest_tender_seen_at": newest,
        "last_scrape": dict(last) if last else None,
    }


@router.get("/tenders/all")
def all_tenders(with_bidders: bool = True):
    """
    The complete feed in a single response -- no pagination, no page counting.

    Safe here precisely because this dataset is small (~2.5 K tenders, a few MB
    with bidders nested). Do not copy this pattern onto a large table.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM tenders ORDER BY first_seen_at DESC, result_id DESC"
        ).fetchall()
        items = [dict(r) for r in rows]
        _attach_stage_dates(conn, items)
        if with_bidders:
            by_id = _bidders_for(conn, [i["result_id"] for i in items])
            for item in items:
                item["bidders"] = by_id.get(item["result_id"], [])
    finally:
        conn.close()
    return {
        "total": len(items),
        "bidder_rows": sum(len(i.get("bidders", [])) for i in items),
        "items": items,
    }


@router.get("/delta")
def delta(since: Optional[str] = None, days: int = 1, with_bidders: bool = True):
    """
    Only what changed -- the endpoint to poll daily instead of re-pulling
    everything.

    Splits the answer in two, because they mean different things:
      new     -- tenders this scraper had never seen before (first_seen_at)
      updated -- tenders it already had, whose stage/winner/values moved
                 (last_updated_at). This is where a Technical tender becoming
                 AOC shows up, with its winner and L1/L2 bid values.

    Pass `since` as a date (2026-08-09) or timestamp (2026-08-09 01:30:00).
    Omit it and `days` is used instead, defaulting to the last 24 hours.

    Note: everything looks "new" on the first day, because first_seen_at is
    when this database first saw a row, and the database was created today.
    From the following day the delta is genuine.
    """
    conn = _connect()
    try:
        if since:
            cutoff = since
        else:
            cutoff = conn.execute(
                "SELECT datetime('now', ?)", [f"-{max(days, 1)} days"]
            ).fetchone()[0]

        new_rows = conn.execute(
            "SELECT * FROM tenders WHERE first_seen_at >= ? "
            "ORDER BY first_seen_at DESC, result_id DESC", [cutoff],
        ).fetchall()
        updated_rows = conn.execute(
            "SELECT * FROM tenders WHERE last_updated_at >= ? AND last_updated_at > first_seen_at "
            "AND first_seen_at < ? ORDER BY last_updated_at DESC", [cutoff, cutoff],
        ).fetchall()

        new_items = [dict(r) for r in new_rows]
        updated_items = [dict(r) for r in updated_rows]
        _attach_stage_dates(conn, new_items + updated_items)
        if with_bidders:
            by_id = _bidders_for(
                conn, [i["result_id"] for i in new_items + updated_items]
            )
            for item in new_items + updated_items:
                item["bidders"] = by_id.get(item["result_id"], [])
    finally:
        conn.close()

    return {
        "since": cutoff,
        "new_count": len(new_items),
        "updated_count": len(updated_items),
        "total_changed": len(new_items) + len(updated_items),
        "new": new_items,
        "updated": updated_items,
    }


@router.get("/tenders")
def list_tenders(
    page: int = 1,
    page_size: int = 100,
    stage: Optional[str] = None,
    organization: Optional[str] = None,
    location: Optional[str] = None,
    tender_number: Optional[str] = None,
    winner: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    since: Optional[str] = None,
    search: Optional[str] = None,
    with_bidders: bool = True,
):
    """
    `since` filters on first_seen_at -- when this scraper first saw the tender.
    That is the right field for "what is new", because status_update_date is
    tender247's own date and can be months older than the day it appeared.
    """
    page = max(page, 1)
    page_size = min(max(page_size, 1), 5000)
    offset = (page - 1) * page_size

    where_sql, params = _filters(stage, organization, location, tender_number,
                                 winner, date_from, date_to, since, search)
    conn = _connect()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM tenders {where_sql}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM tenders {where_sql} "
            f"ORDER BY first_seen_at DESC, result_id DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        items = [dict(r) for r in rows]
        _attach_stage_dates(conn, items)
        if with_bidders:
            by_id = _bidders_for(conn, [i["result_id"] for i in items])
            for item in items:
                item["bidders"] = by_id.get(item["result_id"], [])
    finally:
        conn.close()

    total_pages = (total + page_size - 1) // page_size if total else 0
    return {
        "total": total, "page": page, "page_size": page_size,
        "total_pages": total_pages, "has_more": page < total_pages,
        "next_page": page + 1 if page < total_pages else None,
        "items": items,
    }


@router.get("/tenders/{result_id}")
def get_tender(result_id: int):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM tenders WHERE result_id = ?", [result_id]).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="result_id not found")
        bidders = conn.execute(
            f"SELECT {BIDDER_COLS} FROM bidders WHERE result_id = ? ORDER BY {RANK_ORDER}",
            [result_id],
        ).fetchall()
    finally:
        conn.close()
    item = dict(row)
    conn2 = _connect()
    try:
        _attach_stage_dates(conn2, [item])
    finally:
        conn2.close()
    item["bidders"] = [dict(b) for b in bidders]
    return item


def _bidder_lookup(name: str, page: int, page_size: int):
    page = max(page, 1)
    page_size = min(max(page_size, 1), 5000)
    offset = (page - 1) * page_size
    like = f"%{name}%"

    conn = _connect()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM bidders WHERE bidder_name LIKE ?", [like]
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT b.{BIDDER_COLS.replace(', ', ', b.')},
                   t.result_id, t.tender_number, t.title, t.organization_name,
                   t.location, t.stage, t.tender_value, t.contract_value,
                   t.winner_bidder_name, t.status_update_date
            FROM bidders b
            JOIN tenders t ON t.result_id = b.result_id
            WHERE b.bidder_name LIKE ?
            ORDER BY t.status_update_date DESC
            LIMIT ? OFFSET ?
            """,
            [like, page_size, offset],
        ).fetchall()
    finally:
        conn.close()
    total_pages = (total + page_size - 1) // page_size if total else 0
    return {"total": total, "page": page, "page_size": page_size,
            "total_pages": total_pages, "items": [dict(r) for r in rows]}


@router.get("/bidders")
def bidder_history_query(name: str, page: int = 1, page_size: int = 200):
    """
    Bidder lookup by query parameter. Preferred over the path form: bidder
    names contain spaces, dots and slashes, and query strings survive the
    proxy/WSGI chain intact where path segments do not.
    """
    return _bidder_lookup(name, page, page_size)


@router.get("/bidders/{bidder_name}")
def bidder_history_path(bidder_name: str, page: int = 1, page_size: int = 200):
    """
    Path form, kept for convenience.

    Behind Passenger the path arrives still percent-encoded, so a name with a
    space reaches here as "aman%20enterprises" and matches nothing. Decoding
    defensively fixes that; it is a no-op when the server already decoded.
    """
    from urllib.parse import unquote_plus
    name = bidder_name
    if "%" in name or "+" in name:
        name = unquote_plus(name)
    return _bidder_lookup(name, page, page_size)


app.include_router(public)
app.include_router(router)
