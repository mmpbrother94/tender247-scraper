# Tender247 Scraper

Automated collection of tender listings from Tender247, exposed through a small HTTP API and
scheduled to refresh daily on a cPanel host.

## Components

| File | Role |
|---|---|
| `fresh_auth.py` | Session login and cookie handling |
| `fresh_scraper.py` | Listing traversal and record extraction |
| `fresh_store.py` | SQLite persistence layer |
| `fresh_api.py` | HTTP API serving the collected tenders |
| `config.py` | Configuration loaded from environment |
| `passenger_wsgi.py` | WSGI entry point for cPanel Passenger |

## Configuration

All credentials come from a `.env` file, which is **not** committed. Copy the template and
fill it in:

```bash
cp .env.example .env
```

Required keys: `TENDER_USER`, `TENDER_PASS`, `TARGET_URL`, `API_ACCESS_KEY`, plus the
`CPANEL_SFTP_*` values used by the deployment step.

## Running

```bash
pip install -r requirements.txt
python fresh_scraper.py     # collect
python fresh_api.py         # serve
```

## Scheduled deployment

`deploy/` holds the systemd units that run the scraper on a timer and keep the API up:

- `tender247-api.service` — long-running API process
- `tender247-daily.service` + `tender247-daily.timer` — once-daily collection run

`SETUP.txt` covers host provisioning. `Tender247_Fresh_API.postman_collection.json` (in
`exports/`, untracked) documents the API surface.

## Note

The scraped database and bulk exports are excluded from version control — they hold harvested
tender data and run to tens of megabytes.

## Author

Built by **Manohar Kumar Sah** ([@mmpbrother94](https://github.com/mmpbrother94)).
