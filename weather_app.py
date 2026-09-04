import os
import asyncio
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from lacrosse_view import LaCrosse
import streamlit as st
import pandas as pd

# Load environment variables
load_dotenv()

# Google Sheets Setup
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "Weather Station Data")

def get_google_sheets_client():
    """
    Authenticates using the Service Account.
    Checks Streamlit Cloud secrets first, then falls back to local key.json.
    """
    if "gcp_service_account" in st.secrets:
        creds_info = dict(st.secrets["gcp_service_account"])
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    elif os.path.exists("cloud key.json"):
        creds = Credentials.from_service_account_file("cloud key.json", scopes=SCOPES)
    else:
        raise FileNotFoundError("No service account credentials found in st.secrets or key.json.")
    
    return gspread.authorize(creds)

async def fetch_and_log_weather():
    print("🚀 Fetching latest weather data...")
    try:
        gc = get_google_sheets_client()
        sheet = gc.open(SPREADSHEET_NAME).sheet1
    except Exception as e:
        print(f"❌ Error opening spreadsheet: {e}")
        return

    try:
        api = LaCrosse()
        await api.login("rvashist@gmail.com", "weather2807")
        locations = await api.get_locations()
        if not locations:
            return
            
        location = locations[0]
        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=5)
        
        sensors = await api.get_sensors(
            location, 
            tz="America/Los_Angeles", 
            start=int(time.mktime(start_time.timetuple())), 
            end=int(time.mktime(end_time.timetuple()))
        )
        
        if not sensors:
            return
        sensor = sensors[0]
        
        temperature = "N/A"
        wind_speed = "N/A"
        humidity = "N/A"
        
        for field in sensor.sensor_field_names:
            try:
                if "Temp" in field:
                    temperature = sensor.data[field]["values"][-1]["s"]
                elif "Speed" in field:
                    wind_speed = sensor.data[field]["values"][-1]["s"]
                elif "Humidity" in field:
                    humidity = sensor.data[field]["values"][-1]["s"]
            except (KeyError, IndexError):
                pass 

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row_data = [now, temperature, wind_speed, humidity]
        
        sheet.append_row(row_data)
        print(f"📦 Successfully logged row: {row_data}")
        
    except Exception as e:
        print(f"❌ Error fetching/logging weather data: {e}")
    finally:
        try:
            await api.logout()
        except Exception:
            pass

# --- 1. RUN THE LOGGER ---
if not st.session_state.get("data_fetched"):
    asyncio.run(fetch_and_log_weather())
    st.session_state["data_fetched"] = True

# --- 2. BUILD THE VISUAL APP ---
st.set_page_config(page_title="Backyard Weather", page_icon="🌤️", layout="wide")
st.title("Backyard Weather Station 🌤️")

try:
    gc = get_google_sheets_client()
    sheet = gc.open(SPREADSHEET_NAME).sheet1
    
    raw_data = sheet.get_all_values()
    df = pd.DataFrame(raw_data, columns=["Timestamp", "Temperature", "Wind Speed", "Humidity"])
    
    if not df.empty and df.iloc[0]["Timestamp"] == "Timestamp":
        df = df[1:]
        
    st.subheader("Live Conditions")
    latest = df.iloc[-1]
    
    col1, col2, col3 = st.columns(3)
    col1.metric("Temperature", f"{latest['Temperature']} °C")
    col2.metric("Wind Speed", f"{latest['Wind Speed']} mph")
    col3.metric("Humidity", f"{latest['Humidity']} %")
    
    st.subheader("Recent Sensor Logs")
    st.dataframe(df.tail(10))

except Exception as e:
    st.error(f"Could not load visual dashboard: {e}")