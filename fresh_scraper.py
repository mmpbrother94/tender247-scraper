"""
The fresh-tenders scraper: one job, run once a night.

Pulls tender247's "Fresh Results" tab (tab_id=1) -- the account's own
category-scoped feed, ~2.5 K tenders -- and the participating-bidder list for
each one, into fresh_tenders.db.

This is deliberately the whole feed every night, not just "today's" rows. The
feed is small enough that a full sweep costs ~15 seconds, and re-reading it is
the only reliable way to notice a tender moving Technical -> Financial -> AOC,
which is where the winner and the L1/L2 bid values actually appear. A
new-rows-only scraper would capture each tender at its least useful moment and
never revisit it.

Authentication is plain HTTP too (see fresh_auth.py) -- no browser, no
Playwright, no display. The whole pipeline is `requests` plus sqlite3, so it
runs anywhere Python does, including a cron job on shared hosting. That is what
makes it independent of any one machine.
"""
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import config
import fresh_auth
import fresh_store

logger = logging.getLogger(__name__)

SEARCH_URL = "https://analyticsapi.tender247.com/result/api/get-result-analytics-search"
COUNT_URL = "https://analyticsapi.tender247.com/result/api/get-result-analytics-search-count"
BIDDER_URL = "https://analyticsapi.tender247.com/result/api/get-participating-bidder"

FRESH_TAB_ID = 1

#: The Result page defaults to a narrow publication-date window; widening it is
#: what makes the API return the account's whole history rather than a slice.
DATE_FROM = "2015-01-01"
DATE_TO = "2030-12-31"

PAGE_SIZE = 20000          # the feed fits in a single page at this size
BIDDER_WORKERS = 24
HTTP_TIMEOUT = 120


def _headers(token: str) -> dict:
    return {"Authorization": token, "Content-Type": "application/json", "Accept": "application/json"}


def mint_token() -> tuple:
    """Logs in over plain HTTP and returns (bearer_token, payload_template)."""
    info = fresh_auth.login()
    return info["token"], fresh_auth.build_search_payload(info)


def _body(payload: dict, **overrides) -> dict:
    body = dict(payload)
    body.update({
        "tab_id": FRESH_TAB_ID,
        "publication_date_from": DATE_FROM,
        "publication_date_to": DATE_TO,
    })
    body.update(overrides)
    return body


def parse_tender(record: dict) -> dict:
    return {
        "result_id": record.get("result_id"),
        "tender_number": record.get("tender_number") or "",
        "title": (record.get("result_brief") or "").strip(),
        "location": record.get("location") or "",
        "organization_name": record.get("organization_name") or "",
        "organization_type": record.get("organization_type_name") or "",
        "tender_value": record.get("tender_value"),
        "contract_value": record.get("contract_value"),
        "stage": record.get("stage") or "",
        "winner_bidder_name": record.get("winner_bidder_name") or "",
        "submission_date": record.get("submission_date") or "",
        "status_update_date": record.get("status_update_date") or "",
        "created_date": record.get("created_date") or "",
        "mail_date": record.get("mail_date") or "",
        "tender_result_id": record.get("tender_result_id"),
        "tender_result_created_date": record.get("tender_result_created_date") or "",
        "is_favorite": int(bool(record.get("is_favorite"))),
    }


def parse_bidder(result_id: int, record: dict) -> dict:
    return {
        "result_id": result_id,
        "bidder_name": (record.get("bidder_name") or "").strip(),
        "technical_status": int(bool(record.get("technical_status"))),
        "financial_status": int(bool(record.get("financial_status"))),
        "aoc_status": int(bool(record.get("aoc_status"))),
        "bid_value": record.get("bid_value"),
        "bidder_rank": record.get("bidder_rank") or "",
    }


#: tender247's analytics endpoints occasionally hang well past a normal
#: response time -- an unretried ReadTimeout cost a whole night's run on
#: 2026-08-09. Retry with backoff so a transient stall doesn't lose a day.
MAX_ATTEMPTS = 4
RETRY_BACKOFF = (10, 30, 60)


def _post_with_retry(session, url: str, token: str, body: dict, what: str):
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = session.post(url, headers=_headers(token), json=body, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last = exc
            if attempt == MAX_ATTEMPTS:
                break
            wait = RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]
            logger.warning("%s failed (attempt %d/%d): %s -- retrying in %ds",
                           what, attempt, MAX_ATTEMPTS, str(exc)[:120], wait)
            time.sleep(wait)
    raise RuntimeError(f"{what} failed after {MAX_ATTEMPTS} attempts: {last}")


def fetch_count(session, token: str, payload: dict) -> int:
    resp = _post_with_retry(session, COUNT_URL, token, _body(payload), "count")
    return resp.json()["Data"]["resultcount"]


def sweep_tenders(session, token: str, payload: dict) -> tuple:
    """Pages the whole Fresh Results feed. Returns (new, updated, seen)."""
    total = fetch_count(session, token, payload)
    logger.info("Fresh feed reports %d tenders.", total)

    new = updated = seen = 0
    page_no = 1
    while True:
        started = time.time()
        resp = _post_with_retry(
            session, SEARCH_URL, token,
            _body(payload, page_no=page_no, record_per_page=PAGE_SIZE),
            f"search page {page_no}",
        )
        records = resp.json().get("Data") or []
        if not records:
            break

        rows = [parse_tender(r) for r in records if r.get("result_id") is not None]
        n_new, n_upd = fresh_store.upsert_tenders(rows)
        new += n_new
        updated += n_upd
        seen += len(records)
        logger.info("  page %d: %d tenders (%d new, %d updated) in %.0fs",
                    page_no, len(records), n_new, n_upd, time.time() - started)

        if len(records) < PAGE_SIZE:
            break
        page_no += 1

    return new, updated, seen


class _Token:
    """Shares one Bearer token across worker threads, re-minting it on 401."""

    def __init__(self, token: str):
        self._token = token
        self._gen = 0
        self._lock = threading.Lock()

    @property
    def value(self):
        with self._lock:
            return self._token, self._gen

    def refresh(self, stale_gen: int) -> str:
        with self._lock:
            if stale_gen != self._gen:
                return self._token
            logger.info("Token expired; re-authenticating.")
            self._token, _ = mint_token()
            self._gen += 1
            return self._token


def _fetch_bidders(session, holder: _Token, result_id: int):
    """Returns (result_id, [raw bidders]) or (result_id, None) on failure."""
    for attempt in (1, 2):
        token, gen = holder.value
        try:
            resp = session.post(BIDDER_URL, headers=_headers(token),
                                json={"result_id": result_id}, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            if attempt == 2:
                logger.warning("result_id=%s network error: %s", result_id, exc)
                return result_id, None
            time.sleep(2)
            continue

        if resp.status_code in (401, 403):
            holder.refresh(gen)
            continue
        if resp.status_code != 200:
            if attempt == 2:
                logger.warning("result_id=%s HTTP %s", result_id, resp.status_code)
                return result_id, None
            time.sleep(2)
            continue
        try:
            return result_id, resp.json().get("Data") or []
        except ValueError:
            return result_id, None
    return result_id, None


def sweep_bidders(session, token: str, workers: int = BIDDER_WORKERS) -> int:
    """Fetches bidder lists for tenders that need one. Returns new-bidder-row count."""
    todo = fresh_store.result_ids_needing_bidders()
    if not todo:
        logger.info("Every tender already has a current bidder list.")
        return 0

    logger.info("Fetching bidders for %d tenders on %d workers...", len(todo), workers)
    holder = _Token(token)
    bidder_rows, log_rows = [], []
    failed = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_fetch_bidders, session, holder, rid) for rid in todo]
        for future in as_completed(futures):
            result_id, bidders = future.result()
            if bidders is None:
                # Not logged as fetched, so the next run retries it.
                failed += 1
                continue
            for raw in bidders:
                bidder_rows.append(parse_bidder(result_id, raw))
            log_rows.append((result_id, len(bidders)))

    new_rows = fresh_store.insert_bidders(bidder_rows)
    fresh_store.mark_fetched(log_rows)
    logger.info("Bidders: %d tenders fetched, %d new rows, %d failed (retry next run).",
                len(log_rows), new_rows, failed)
    return new_rows


def run(workers: int = BIDDER_WORKERS) -> dict:
    """One complete fresh-tenders scrape."""
    started = time.time()
    fresh_store.init_db()
    logger.info("=== Fresh tender scrape starting ===")

    token, payload = mint_token()

    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(
        pool_connections=workers, pool_maxsize=workers, max_retries=0))

    try:
        new, updated, seen = sweep_tenders(session, token, payload)
        new_bidders = sweep_bidders(session, token, workers)
    except Exception as exc:
        elapsed = int(time.time() - started)
        fresh_store.log_run(0, 0, 0, elapsed, ok=False, note=str(exc)[:300])
        logger.exception("Fresh scrape FAILED after %ds", elapsed)
        raise

    elapsed = int(time.time() - started)
    fresh_store.log_run(new, updated, new_bidders, elapsed)
    summary = {
        "tenders_seen": seen,
        "new_tenders": new,
        "updated_tenders": updated,
        "new_bidder_rows": new_bidders,
        "seconds": elapsed,
        **fresh_store.counts(),
    }
    logger.info("=== Fresh scrape finished in %ds: %s ===", elapsed, summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(config.LOG_DIR / "fresh.log", encoding="utf-8")],
    )
    run()
