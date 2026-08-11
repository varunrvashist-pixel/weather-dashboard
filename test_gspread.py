"""test_gspread.py

Quick smoke-test to validate Google Sheets service-account authentication used by the app.

Usage (bash):
  export GOOGLE_SERVICE_ACCOUNT_FILE="/full/path/to/cloud_key.json"
  export GOOGLE_SHEET_NAME="Weather Database"
  python test_gspread.py

On Windows PowerShell:
  $env:GOOGLE_SERVICE_ACCOUNT_FILE="C:\full\path\cloud_key.json"
  $env:GOOGLE_SHEET_NAME="Weather Database"
  python test_gspread.py

This script will print the service-account type and client_email and attempt to open the sheet name.
"""

import os
import json
import sys

try:
    import gspread
    from google.oauth2 import service_account
except Exception as e:
    print("Missing dependency:", e)
    print("Install requirements: pip install gspread google-auth")
    sys.exit(1)


FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "cloud_key.json")
SHEET = os.environ.get("GOOGLE_SHEET_NAME", "Weather Database")

print("Testing Google Sheets service account file:", FILE)
print("Sheet name:", SHEET)
print("Exists:", os.path.exists(FILE))

if not os.path.exists(FILE):
    print("ERROR: Service account file not found at the path above. Set GOOGLE_SERVICE_ACCOUNT_FILE or place the file next to this script.")
    sys.exit(2)

with open(FILE, "r", encoding="utf-8") as f:
    info = json.load(f)

print("type:", info.get("type"))
print("client_email:", info.get("client_email"))

scopes = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

try:
    creds = service_account.Credentials.from_service_account_file(FILE, scopes=scopes)
    client = gspread.authorize(creds)
    sh = client.open(SHEET)
    print("OK - opened sheet:\n ", sh.title)
except Exception as e:
    print("Failed to open sheet:", repr(e))
    # Helpful hint for common invalid_grant causes
    msg = str(e)
    if "invalid_grant" in msg or "Invalid JWT Signature" in msg:
        print("\nHint: invalid_grant/Invalid JWT Signature usually means the key has been revoked or is not a service-account key, or the system clock is skewed.")
        print(" - Check that 'type' in the JSON is 'service_account'.")
        print(" - Ensure the JSON key hasn't been deleted/revoked in Google Cloud Console.")
        print(" - Ensure the spreadsheet is shared with the 'client_email' from the JSON.")
        print(" - Ensure your machine's clock is accurate (NTP sync).")
    sys.exit(3)
