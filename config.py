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

# Email notifications (SMTP). Non-secret settings live in the "notifications"
# block of project_config.json; optional SMTP credentials (for relays that
# require auth) live in the gitignored credentials/smtp_credentials.json.
NOTIFICATIONS = _project.get("notifications", {})

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
