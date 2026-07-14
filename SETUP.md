# PS BidAnalyzer Tool — Setup Guide

This guide sets up the **Bid Analyzer**: the tool that reads tenders from the
**PS Tender Tracker** Google Sheet, scores each against Onepoint's capabilities
via the **Google Gemini** API, and writes back a Bid / NoBid / TBD qualification.

## Prerequisites

- Python 3.10 or newer (developed on 3.14)
- A Google Cloud project with the **Google Sheets API** and **Google Drive API** enabled
- Access to the Google Drive folder containing (or that will contain) the **PS Tender Tracker** sheet
- A **Google Gemini** API key (from [Google AI Studio](https://aistudio.google.com/apikey))

> The analyzer does **not** create the sheet — it locates an existing sheet by
> name in the configured folder. The sheet must already exist and be populated
> (row 1 = summary, row 2 = headers, row 3+ = tender data) by an upstream process.

---

## Step 1: Install dependencies

```bash
pip install -r requirements.txt
```

---

## Step 2: Create a Google service account

The analyzer authenticates with a **service account** (no browser login / OAuth flow).

### 2a. Create the service account

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Select (or create) your project and enable the **Google Sheets API** and **Google Drive API**.
3. Go to **IAM & Admin → Service Accounts → Create Service Account**.
4. Give it a name (e.g. `bid-analyzer`) and create it. No project roles are required.

### 2b. Create and download a key

1. Open the service account → **Keys → Add Key → Create new key → JSON**.
2. Download the JSON key and save it as:

```
credentials/service_account.json
```

### 2c. Grant the service account access to the sheet

The service account has its own email address (e.g. `bid-analyzer@your-project.iam.gserviceaccount.com`, shown on its details page).

- **Share the Drive folder** (the one in `project_config.json → target_folder_id`) with that email as **Editor**, **or**
- Share the **PS Tender Tracker** sheet directly with that email as **Editor**.

Without this, the analyzer can't find or write to the sheet.

---

## Step 3: Add your Gemini API key

Create `credentials/gemini_credentials.json`:

```json
{
  "gemini_api_key": "..."
}
```

Get the key from [Google AI Studio](https://aistudio.google.com/apikey). Make sure the key has access to the model set in `analyzer/config.py` (`GEMINI_MODEL`, default `gemini-3.1-flash-lite`).

> **Backup provider:** the project previously used OpenRouter, and that path is
> kept as a reference/backup (`analyzer/openrouter_client.py`, the `OPENROUTER_*`
> settings, and `credentials/openrouter_credentials.json` with
> `{ "openrouter_api_key": "sk-or-v1-..." }`). It is **not** used by the active
> analyzer and there is no runtime fallback.

> **Important:** everything in `credentials/` is excluded from git by `.gitignore`. Never commit key files.

---

## Step 4: Populate the Onepoint capability context

Scoring is grounded **only** on this file:

```
analyzer/knowledge/onepoint_capabilities.md
```

Populate it with Onepoint's capabilities, past performance, accreditations, and target markets (see the sources listed in `Requirements.md`). If it's missing or empty the analyzer still runs, but logs a warning and produces low-confidence scores.

---

## Step 5: Point at the right sheet

Edit `project_config.json`:

```json
{
  "google_sheets": {
    "environment": "Dev",
    "target_folder_id": "<your Google Drive folder ID>",
    "sheet_name": "PS Tender Tracker"
  }
}
```

| Field | Description |
|-------|-------------|
| `environment` | Free-text label for the environment (e.g. `Dev`, `Prod`) |
| `target_folder_id` | ID of the Drive folder containing the sheet (from the folder URL) |
| `sheet_name` | Exact name of the target Google Sheet **and** its tab |

---

## Step 6: Run the Bid Analyzer

```bash
# From the project root:
python -m analyzer.main                     # analyse today's window (UK time)
python -m analyzer.main --date 2026-07-03   # analyse a specific day (backfill / rerun)
python -m analyzer.main --limit 5           # cap to the first 5 qualifying rows (quick test)
```

### What happens on each run

1. Authenticates with the service account and opens the **PS Tender Tracker** sheet.
2. Reads all tender rows.
3. Selects rows whose `Bid Qualification` is `PreQualified` or `ReCheck` **and** whose `Last Modified Date` falls in the target one-day window.
4. Scores each selected tender against the Onepoint capability context (with automatic retries on transient Gemini errors).
5. Writes back `Bid Qualification` (`Bid(AI)` / `TBD(AI)` / `NoBid(AI)`), the reason, the date, an appended `Comments` entry, and the control columns; colours each changed row (white / yellow / red).
6. Logs an end-of-run summary.

### Output locations

| Output | Location |
|--------|----------|
| Updated sheet | `PS Tender Tracker` in your configured Drive folder |
| Run log | `analyzer/analyzer.log` (also echoed to the console) |

---

## File structure

```
PS BidAnalyzer Tool/
├── config.py                          # Shared config — column schema, sheet/folder, credential paths
├── project_config.json                # Sheet name, Drive folder ID, environment
├── requirements.txt
├── README.md
├── SETUP.md                           # This file
├── credentials/
│   ├── service_account.json           # Google service account key (you provide — not in git)
│   ├── gemini_credentials.json        # { "gemini_api_key": "..." } (you provide — not in git)
│   ├── openrouter_credentials.json    # { "openrouter_api_key": "..." } (backup provider — not in git)
│   └── .gitignore
└── analyzer/
    ├── main.py                        # Entry point — read sheet → analyse → write back
    ├── analyzer.py                    # Core analyze_tender(): prompt + score → qualification
    ├── gemini_client.py               # Gemini client wrapper (active provider)
    ├── openrouter_client.py           # OpenRouter/OpenAI client wrapper (backup, unused)
    ├── onepoint_context.py            # Loads the Onepoint capability context
    ├── sheets_client.py               # Google Sheets read/write + row colouring
    ├── config.py                      # Analyzer settings (model, thresholds, retries, window)
    └── knowledge/
        └── onepoint_capabilities.md   # Capability context injected into the prompt (you populate)
```

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `Sheet '…' not found in folder …` | Sheet doesn't exist in the configured folder, or the service account lacks access. Check `project_config.json` and share the folder/sheet with the service account email. |
| `FileNotFoundError: service_account.json` | Missing `credentials/service_account.json` (Step 2b). |
| `gemini_api_key is not set …` | `credentials/gemini_credentials.json` is missing or lacks the `gemini_api_key` field (Step 3). |
| `404 NOT_FOUND … model … no longer available` | The `GEMINI_MODEL` in `analyzer/config.py` isn't available on your key. Pick an available model (e.g. `gemini-3.1-flash-lite`, `gemini-3.5-flash`). |
| "Analysis will proceed with NO company context" warning | `analyzer/knowledge/onepoint_capabilities.md` is missing or empty (Step 4). |
| `Analyzer failed … after N attempts` | The model kept returning incomplete/transient responses; the row is marked `NoBid(AI)` for manual review. Re-run later. |
| `HttpError 403/429` | Sheets/Drive API rate limit — the tool retries automatically with backoff. |
| Nothing analysed / all "Out of window" | No rows have `Last Modified Date` on the target day. Use `--date YYYY-MM-DD` to target the right day. |
