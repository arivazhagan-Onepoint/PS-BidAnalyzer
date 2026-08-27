import json
import os
from datetime import datetime, timedelta
import pytz
import holidays

# Paths
BASE_DIR             = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_DIR      = os.path.join(BASE_DIR, "credentials")
SERVICE_ACCOUNT_FILE = os.path.join(CREDENTIALS_DIR, "service_account.json")

os.makedirs(CREDENTIALS_DIR, exist_ok=True)

# Load project-level config
_project = json.load(open(os.path.join(BASE_DIR, "project_config.json"), encoding="utf-8"))

# Google Sheets
SCOPES           = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
ENVIRONMENT      = _project["google_sheets"].get("environment", "N/A")
TARGET_FOLDER_ID = _project["google_sheets"]["target_folder_id"]
SHEET_NAME       = _project["google_sheets"]["sheet_name"]
# Secondary tabs that manually-set human decisions are collected into by the
# maintenance flow's extract step (see analyzer/maintain_knowledge.py) — NoBid(Human)
# rows into the first, Bid(Human) rows into the second. Each tab's header row
# (row 1) may hold any subset of DATASET_FIELDS in any order; columns are matched
# by name at copy time.
NOBIDS_SHEET_NAME = _project["google_sheets"].get("nobids_tab_name", "PS NoBids")
BIDS_SHEET_NAME   = _project["google_sheets"].get("bids_tab_name", "PS Bids")

# Email notifications (SMTP). Non-secret settings live in the "notifications"
# block of project_config.json; optional SMTP credentials (for relays that
# require auth) live in the gitignored credentials/smtp_credentials.json.
NOTIFICATIONS = _project.get("notifications", {})

# Google Drive locations, from the "google_drive_locations" block of
# project_config.json. Used only by the detailed-analysis stage: "Source_Docs"
# holds Onepoint's own evidence sheets (ingested into the corpus), "Tender_Docs"
# holds one subfolder of buyer documents per tender, and "Analysis_Reports" is
# where every brief is published. Configured rather than coded so each
# environment can read and publish to its own folders — and so this file stays
# the one place a Drive target is set. Read them through drive_location().
DRIVE_LOCATIONS = _project.get("google_drive_locations", {})


def drive_location(key: str) -> str:
    """The Drive folder ID configured under ``google_drive_locations[key]``.

    Raises rather than falling back to a default, because a wrong or absent
    folder ID does not fail loudly further down: Drive answers "no such folder"
    exactly as it answers "empty folder", so a run would report no documents and
    produce a brief that reads as complete. Validated on read rather than at
    import, so the modules that never touch Drive are unaffected by a missing
    block.
    """
    value = (DRIVE_LOCATIONS.get(key) or "").strip()
    if not value:
        raise ValueError(
            f'google_drive_locations.{key} is missing or empty in '
            f'project_config.json. Add the Drive folder ID (the last part of the '
            f'folder URL): "google_drive_locations": {{"{key}": "<folder id>"}}'
        )
    return value


# FTS API
FTS_API_BASE = "https://www.find-tender.service.gov.uk/api/1.0"
PORTAL_URL   = "https://www.find-tender.service.gov.uk/Notice"
PORTAL_NAME  = "Find-A-Tender"

# Date logic (UK timezone)
UK_TIMEZONE = pytz.timezone('Europe/London')


# Dataset fields — canonical column order for Google Sheets
DATASET_FIELDS = [
    "Portal Name",
    "Adapter",
    "Direct URL",
    "ID",
    "OCID",
    "Name",
    "Bid Qualification",
    "Bid Qualification Reason(System)",
    "Bid Qualification Reason(Human)",
    "Published On",
    "Clarification Due Date",
    "Tender Due Date",
    "Bid Qualification Date",
    "PME_Flag",
    "Procurement Stage",
    "Total Contract Value",
    "Contract Duration",
    "Annual Contract Value",
    "Tender Description",
    "Buyer Name",
    "CPV Code",
    "CPV Description",
    "SC_Flag",
    "Country",
    "Locality",
    "SME_Flag",
    "Comments",
    "Processed Date",
    "Last Modified Date",
    "Created Date",
]

# Logging
LOG_FILE   = os.path.join(BASE_DIR, "tender_scraper.log")
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
