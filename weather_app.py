import os
import asyncio
import time
from datetime import datetime, timedelta
import base64
import json
import pytz
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from lacrosse_view import LaCrosse
import streamlit as st
import pandas as pd
import plotly.express as px

# Load local environment variables if present
load_dotenv()

# Configuration
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "Weather Station Data")
TIMEZONE = "America/Los_Angeles"

def get_google_sheets_client():
    """Authenticates using Base64 secret on Streamlit Cloud, or local key files."""
    if "GCP_KEY_BASE64" in st.secrets:
        key_json = base64.b64decode(st.secrets["GCP_KEY_BASE64"]).decode("utf-8")
        creds_info = json.loads(key_json)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    elif os.path.exists("cloud_key.json"):
        creds = Credentials.from_service_account_file("cloud_key.json", scopes=SCOPES)
    elif os.path.exists("key.json"):
        creds = Credentials.from_service_account_file("key.json", scopes=SCOPES)
    elif "gcp_service_account" in st.secrets:
        creds_info = dict(st.secrets["gcp_service_account"])
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    else:
        raise FileNotFoundError("Could not find GCP credentials in st.secrets or local files.")
    return gspread.authorize(creds)

def is_plausible(temp, wind, hum):
    """Filters out impossible sensor readings before logging."""
    try:
        t = float(temp)
        w = float(wind)
        h = float(hum)
        if not (-40 <= t <= 140): return False
        if not (0 <= w < 150): return False
        if not (0 <= h <= 100): return False
        return True
    except (ValueError, TypeError):
        return False

async def fetch_and_log_weather():
    """Fetches the latest reading from La Crosse View and logs to Google Sheets."""
    try:
        gc = get_google_sheets_client()
        sheet = gc.open(SPREADSHEET_NAME).sheet1
    except Exception as e:
        print(f"Sheet access error: {e}")
        return

    try:
        api = LaCrosse()
        await api.login("rvashist@gmail.com", "weather2807")
        locations = await api.get_locations()
        if not locations:
            return

        location = locations[0]
        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=10)

        sensors = await api.get_sensors(
            location,
            tz=TIMEZONE,
            start=int(time.mktime(start_time.timetuple())),
            end=int(time.mktime(end_time.timetuple()))
        )

        if not sensors:
            return
        sensor = sensors[0]

        temp_val, wind_val, hum_val = None, None, None

        for field in sensor.sensor_field_names:
            try:
                val = sensor.data[field]["values"][-1]["s"]
                if "Temp" in field:
                    raw_t = float(val)
                    # Convert to °F if reading is reported in Celsius
                    temp_val = round((raw_t * 9/5) + 32, 1) if raw_t < 45 else round(raw_t, 1)
                elif "Speed" in field:
                    wind_val = round(float(val), 1)
                elif "Humidity" in field:
                    hum_val = round(float(val), 1)
            except (KeyError, IndexError, ValueError):
                pass

        if temp_val is not None and wind_val is not None and hum_val is not None:
            if is_plausible(temp_val, wind_val, hum_val):
                tz = pytz.timezone(TIMEZONE)
                now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
                sheet.append_row([now_str, temp_val, wind_val, hum_val])
                print(f"Logged reading: {[now_str, temp_val, wind_val, hum_val]}")
            else:
                print("Ignored implausible reading.")
    except Exception as e:
        print(f"Fetch error: {e}")
    finally:
        try:
            await api.logout()
        except Exception:
            pass

# Run data sync check once per browser session
if "data_fetched" not in st.session_state:
    asyncio.run(fetch_and_log_weather())
    st.session_state["data_fetched"] = True

# --- UI PAGE SETUP ---
st.set_page_config(page_title="Backyard Weather", page_icon="🌤️", layout="wide")
st.title("Backyard Weather Station 🌤️")

try:
    gc = get_google_sheets_client()
    sheet = gc.open(SPREADSHEET_NAME).sheet1
    raw_data = sheet.get_all_values()

    if len(raw_data) <= 1:
        st.info("No weather logs recorded yet. Waiting for initial readings...")
        st.stop()

    df = pd.DataFrame(raw_data[1:], columns=["Timestamp", "Temperature", "Wind Speed", "Humidity"])
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    df["Temperature"] = pd.to_numeric(df["Temperature"], errors="coerce")
    df["Wind Speed"] = pd.to_numeric(df["Wind Speed"], errors="coerce")
    df["Humidity"] = pd.to_numeric(df["Humidity"], errors="coerce")
    df = df.dropna(subset=["Timestamp"]).sort_values("Timestamp")

    # Time calculations
    tz = pytz.timezone(TIMEZONE)
    now_local = datetime.now(tz)
    latest = df.iloc[-1]
    latest_time = latest["Timestamp"].tz_localize(tz) if latest["Timestamp"].tzinfo is None else latest["Timestamp"].astimezone(tz)

    is_stale = (now_local - latest_time) > timedelta(minutes=30)
    time_display = latest_time.strftime("%I:%M %p")

    # Top Snapshot Row
    col1, col2, col3 = st.columns(3)
    col1.metric("Temperature", f"{latest['Temperature']} °F")
    col2.metric("Wind Speed", f"{latest['Wind Speed']} mph")
    col3.metric("Humidity", f"{latest['Humidity']} %")

    if is_stale:
        st.warning(f"⚠️ Readings as of {time_display} — no newer data received.")
    else:
        st.caption(f"Last updated: {time_display}")

    st.divider()

    # Filter for today (12:00 AM to 11:59 PM)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)
    df_today = df[df["Timestamp"] >= today_start.replace(tzinfo=None)].copy()

    st.subheader("Today's Trends")

    if df_today.empty:
        st.info("No readings recorded yet today.")
    else:
        # Temperature Chart
        fig_temp = px.line(
            df_today, x="Timestamp", y="Temperature",
            title="Temperature (°F)"
        )
        fig_temp.update_xaxes(range=[today_start.replace(tzinfo=None), today_end.replace(tzinfo=None)])
        fig_temp.update_traces(
            mode="lines+markers",
            hovertemplate="%{y:.1f} °F<br>%{x|%I:%M %p}<extra></extra>"
        )
        st.plotly_chart(fig_temp, use_container_width=True)

        # Wind Speed Chart
        fig_wind = px.line(
            df_today, x="Timestamp", y="Wind Speed",
            title="Wind Speed (mph)"
        )
        fig_wind.update_xaxes(range=[today_start.replace(tzinfo=None), today_end.replace(tzinfo=None)])
        fig_wind.update_traces(
            mode="lines+markers",
            hovertemplate="%{y:.1f} mph<br>%{x|%I:%M %p}<extra></extra>"
        )
        st.plotly_chart(fig_wind, use_container_width=True)

        # Humidity Chart
        fig_hum = px.line(
            df_today, x="Timestamp", y="Humidity",
            title="Humidity (%)"
        )
        fig_hum.update_xaxes(range=[today_start.replace(tzinfo=None), today_end.replace(tzinfo=None)])
        fig_hum.update_traces(
            mode="lines+markers",
            hovertemplate="%{y:.1f} %<br>%{x|%I:%M %p}<extra></extra>"
        )
        st.plotly_chart(fig_hum, use_container_width=True)

    st.divider()

    # Page 2 link if the file exists
    if os.path.exists("pages/1_Weekly_Trends.py"):
        st.page_link("pages/1_Weekly_Trends.py", label="View Weekly Trends →", icon="📈")

except Exception as e:
    st.error(f"Could not load visual dashboard: {e}")