import os
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials

load_dotenv()

# Safely attempt to read Streamlit secrets, then fallback to environment variables
try:
    GOOGLE_SHEET_NAME = st.secrets.get("GOOGLE_SHEET_NAME")
except Exception:
    GOOGLE_SHEET_NAME = None

if not GOOGLE_SHEET_NAME:
    GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Weather Database").strip()

GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "cloud_key.json").strip()


def load_readings():
    """Fetches historical records from Google Sheets and parses timestamps."""
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            GOOGLE_SERVICE_ACCOUNT_FILE, scope
        )
        client = gspread.authorize(creds)
        sheet = client.open(GOOGLE_SHEET_NAME).sheet1

        records = sheet.get_all_records()
        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)

        # Standardize column names
        df.columns = [c.strip() for c in df.columns]

        # Standardize timestamp parsing
        if "Timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["Timestamp"])

        if "Temperature" in df.columns:
            df["temp"] = pd.to_numeric(df["Temperature"], errors="coerce")

        if "Wind Speed" in df.columns:
            df["wind"] = pd.to_numeric(df["Wind Speed"], errors="coerce")

        if "Humidity" in df.columns:
            df["humid"] = pd.to_numeric(df["Humidity"], errors="coerce")

        return df
    except Exception as e:
        st.error(f"Error loading readings: {e}")
        return pd.DataFrame()


def latest_reading(df):
    """Returns the most recent single reading row as a dictionary."""
    if df.empty or "timestamp" not in df.columns:
        return None

    sorted_df = df.sort_values("timestamp")
    last_row = sorted_df.iloc[-1]

    return {
        "timestamp": last_row.get("timestamp"),
        "temp": last_row.get("temp"),
        "wind": last_row.get("wind"),
        "humid": last_row.get("humid"),
    }


def today_readings(df):
    """Filters the dataframe to include only today's records."""
    if df.empty or "timestamp" not in df.columns:
        return pd.DataFrame()

    today_date = pd.Timestamp.now().date()
    return df[df["timestamp"].dt.date == today_date].copy()