"""
Browser-free authentication against tender247.

The original scraper drove a real Chromium through the login modal, which meant
it could only run on a machine with Playwright and a browser installed. That is
the single reason the pipeline was tied to one PC.

It turns out no browser is needed. Logging in is two plain HTTP calls:

  1. POST www.tender247.com/apigateway/T247ApiTender/api/auth/login
         {email_id, password}  ->  site token + user_id
  2. POST analyticsapi.tender247.com/dashboardapi/api/user-login-by-userid
         {user_id}, Bearer <site token>  ->  analytics token, bidder_id,
                                             user_email_service_query_id

The analytics token is what every data endpoint wants, and steps 1-2 also hand
back the bidder_id / user_query_id that the search payload needs -- so the
whole request template can be built from scratch instead of being sniffed off a
live page. That makes this runnable from anywhere, including a cron job on
shared hosting.

Tokens last ~5 hours (iat/exp on the JWT); a run takes under a minute, so a
single login per run is plenty, and callers can just re-login on a 401.
"""
import base64
import json
import logging
import time

import requests

import config

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.tender247.com/apigateway/T247ApiTender/api/auth/login"
ANALYTICS_LOGIN_URL = "https://analyticsapi.tender247.com/dashboardapi/api/user-login-by-userid"

BROWSER_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Origin": "https://www.tender247.com",
    "Referer": "https://www.tender247.com/",
}


class LoginError(RuntimeError):
    pass


def _decode_jwt_claims(token: str) -> dict:
    """Reads a JWT's payload. No signature check -- we only want the claims."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def _unwrap(body: dict) -> dict:
    """tender247 returns Data as either a dict or a single-element list."""
    data = body.get("Data")
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def login(session: requests.Session = None) -> dict:
    """
    Returns everything needed to talk to the data APIs:

        {token, user_id, bidder_id, user_query_id, company_name, expires_at}

    `token` is the analytics Bearer token, already prefixed.
    """
    if not config.TENDER_USER or not config.TENDER_PASS:
        raise LoginError("TENDER_USER / TENDER_PASS are not set in .env")

    session = session or requests.Session()
    session.headers.update(BROWSER_HEADERS)

    resp = session.post(
        LOGIN_URL,
        json={"email_id": config.TENDER_USER, "password": config.TENDER_PASS},
        timeout=90,
    )
    if resp.status_code != 200:
        raise LoginError(f"Login returned HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    if not body.get("Success"):
        raise LoginError(f"Login rejected: {body.get('Message')}")

    site = _unwrap(body)
    site_token = site.get("token")
    user_id = site.get("user_id")
    if not site_token or not user_id:
        raise LoginError(f"Login succeeded but returned no token/user_id: {list(site)}")

    resp = session.post(
        ANALYTICS_LOGIN_URL,
        headers={"Authorization": f"Bearer {site_token}"},
        json={"user_id": user_id},
        timeout=90,
    )
    if resp.status_code != 200:
        raise LoginError(f"Analytics login returned HTTP {resp.status_code}: {resp.text[:200]}")
    analytics = _unwrap(resp.json())

    token = analytics.get("token")
    if not token:
        raise LoginError(f"Analytics login returned no token: {list(analytics)}")

    claims = _decode_jwt_claims(token)
    session_info = {
        "token": token if token.lower().startswith("bearer ") else f"Bearer {token}",
        "user_id": analytics.get("user_id") or user_id,
        "bidder_id": analytics.get("bidder_id"),
        "user_query_id": analytics.get("user_email_service_query_id") or 0,
        "company_name": analytics.get("company_name") or site.get("person_name") or "",
        "valid_upto": analytics.get("valid_upto"),
        "expires_at": claims.get("exp"),
    }

    if session_info["expires_at"]:
        mins = (session_info["expires_at"] - time.time()) / 60
        logger.info("Logged in as %s (user_id=%s, bidder_id=%s); token valid ~%.0f min.",
                    session_info["company_name"], session_info["user_id"],
                    session_info["bidder_id"], mins)
    else:
        logger.info("Logged in as %s (user_id=%s).",
                    session_info["company_name"], session_info["user_id"])
    return session_info


def build_search_payload(session_info: dict, **overrides) -> dict:
    """
    The get-result-analytics-search body, constructed from the login response
    rather than sniffed from a live page load.

    Field names and defaults mirror exactly what the site itself sends; only
    tab_id, the date range and pagination are meant to be overridden.
    """
    payload = {
        "result_id": 0,
        "search_text": "",
        "contract_date_from": "",
        "contract_date_to": "",
        "publication_date_from": "",
        "publication_date_to": "",
        "state_ids": "",
        "city_ids": "",
        "keyword_ids": None,
        "name_of_website": "",
        "organization_id": 0,
        "organization_type_name": "",
        "tender_value_operator": 0,
        "contract_value_operator": 0,
        "contract_value_from": 0,
        "contract_value_to": 0,
        "tender_value_from": 0,
        "tender_value_to": 0,
        "bidder_name": "",
        "participant_name": "",
        "winner_bidder": "",
        "stage": "",
        "sort_by": 3,
        "sort_type": 2,
        "page_no": 1,
        "record_per_page": 20,
        "tender_status": 3,
        "search_by_split_word": False,
        "user_id": session_info["user_id"],
        "user_query_id": session_info["user_query_id"],
        "tender_number": "",
        "tab_id": 1,
        "product_id": 0,
        "search_by": 1,
        "sub_industry_id": 0,
        "bidder_id": session_info["bidder_id"],
        "mail_date": "",
        "status_update_date_from": "",
        "status_update_date_to": "",
        "l1_bidder": "",
    }
    payload.update(overrides)
    return payload


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    info = login()
    safe = {k: (v[:30] + "..." if k == "token" else v) for k, v in info.items()}
    print(json.dumps(safe, indent=2))
