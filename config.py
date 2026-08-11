"""
Central configuration for the Tender247 scraper.
All tunables live here so main.py / auth.py / parser.py stay declarative.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Load .env from this file's own directory, not the process's cwd -- a
# scheduled task can run with an arbitrary/no working directory set.
load_dotenv(BASE_DIR / ".env")

# --- Credentials & target (from .env) ---
TENDER_USER = os.getenv("TENDER_USER")
TENDER_PASS = os.getenv("TENDER_PASS")
TARGET_URL = os.getenv("TARGET_URL", "https://www.tender247.com/auth/tender")
RESULTS_PAGE_URL = "https://www.tender247.com/auth/analytics/analytics-result"

if not TENDER_USER or not TENDER_PASS:
    raise RuntimeError(
        "TENDER_USER / TENDER_PASS missing. Copy .env.example to .env and fill in credentials."
    )

# --- Paths ---
COOKIES_PATH = BASE_DIR / "cookies.json"   # holds full Playwright storage_state (see auth.py)
DB_PATH = BASE_DIR / "tenders_vault.db"
EXPORT_DIR = BASE_DIR / "exports"
LOG_DIR = BASE_DIR / "logs"

EXPORT_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# --- Login modal selectors ---
# tender247.com is a Next.js/Radix SPA: the login form only exists in the DOM
# after clicking this header trigger, and auth state lives in localStorage
# (accessToken/user_id/bidder_id/...), not in a session cookie. Verified
# 2026-07-27 against the live site.
SELECTORS = {
    # Site was redesigned ~2026-07-31: the old "Sign Up/log in" text is gone.
    # Desktop header now shows "Log in"; the mobile menu shows "Sign Up / Log in".
    "login_trigger_button": 'button:text-is("Log in"), button:has-text("Sign Up / Log in")',
    "login_username_input": "input[name='emailId']",
    "login_password_input": "input[name='password']",
    "login_submit_button": "button[type='submit']",
    "login_success_marker": "input[name='emailId']",  # success = this detaches from the DOM
}

# --- Closed Tenders (Archive) API ---
# The account's own dashboard shows "25.51 K Closed Tenders (2026)" -- that
# figure comes from this endpoint (confirmed via network capture 2026-07-27
# by clicking that exact stat). It's year-scoped in the URL path itself and
# is what makes it the correct, account-scoped source of closed-tender data
# (as opposed to the Result page's "Tender Results" tab, which turned out to
# return platform-wide data across all bidders -- tens of thousands of
# irrelevant records per month).
ARCHIVE_API_URL_TEMPLATE = "https://www.tender247.com/apigateway/T247ArchiveTenders/api/{year}/auth/search-tender"
ARCHIVE_COUNT_API_URL_TEMPLATE = "https://www.tender247.com/apigateway/T247ArchiveTenders/api/{year}/auth/tender-user-count"
CLOSED_TENDERS_YEAR = 2026

# --- Tender Results API ---
# The "Fresh Results" tab (tab_id=1, the page's own default -- do NOT switch
# tabs) is correctly scoped to this account's subscribed categories, unlike
# the "Tender Results" tab, which turned out to return platform-wide data
# across all bidders (tens of thousands of irrelevant records per month).
# This is a first-class dataset in its own right (matching the Result
# page's "Fresh Results (N.NN K)" badge) and is also used to look up
# winner_bidder_name by tender_number for closed tenders that already have
# a published result.
RESULTS_API_URL = "https://analyticsapi.tender247.com/result/api/get-result-analytics-search"
TENDER_STATUS_CLOSED = 3

# Confirmed via network capture 2026-07-27: real result data exists back to
# at least 2022 for this account. Defaulting to Jan 2026 per explicit
# request; bump this back to "2022-01-01" (or earlier -- check first) for a
# full historical pull later.
RESULTS_HISTORY_START_DATE = "2026-01-01"

# --- API server ---
# If set, api_server.py requires this exact value in the X-API-Key header on
# every request (except /health). Leave blank in .env only for local-only
# use; set it before exposing the API beyond your own machine/network.
API_ACCESS_KEY = os.getenv("API_ACCESS_KEY", "")

# --- cPanel sync (pushes tenders_vault.db to the cPanel-hosted API after
# each scrape run, since the scraper itself can't run on cPanel) ---
CPANEL_SFTP_HOST = os.getenv("CPANEL_SFTP_HOST", "")
CPANEL_SFTP_PORT = int(os.getenv("CPANEL_SFTP_PORT", "22"))
CPANEL_SFTP_USER = os.getenv("CPANEL_SFTP_USER", "")
CPANEL_SFTP_PASS = os.getenv("CPANEL_SFTP_PASS", "")
CPANEL_REMOTE_DB_PATH = os.getenv("CPANEL_REMOTE_DB_PATH", "")  # e.g. /home/cpaneluser/tender247_api/tenders_vault.db

# --- Behavior tuning ---
COOKIE_MAX_AGE_HOURS = 8            # JWT observed with an ~11h lifetime; stay comfortably under it
BULK_RECORD_PER_PAGE = 100
BULK_CHECKPOINT_EVERY_N_PAGES = 10
BULK_SLEEP_JITTER_RANGE = (1.5, 3.5)     # seconds, random.uniform between page requests
DAILY_ARCHIVE_PAGES_TO_SCAN = 3          # daily mode: how many most-recent closed-tender pages to re-check
DAILY_LOOKBACK_DAYS = 5                  # daily mode: how many days back to re-scan for new Result records
DAILY_RECORD_PER_PAGE = 100
PAGE_LOAD_TIMEOUT_MS = 30000
