import os
import time
import asyncio
from datetime import datetime
import pytz
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
from lacrosse_view import LaCrosse

# --- CONFIGURATION ---
load_dotenv()

USERNAME = "rvashist@gmail.com"
PASSWORD = "weather2807"
TARGET_LOCATION_NAME = "Backyard"

# Spreadsheet Configuration
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Weather Database").strip()
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "cloud_key.json").strip()
LOCAL_TIMEZONE = os.environ.get("LOCAL_TIMEZONE", "America/New_York").strip()

# PRD Section 2: Sanity boundaries for filtering sensor glitches (Imperial Scales)
TEMP_MIN, TEMP_MAX = -40.0, 130.0
WIND_MIN, WIND_MAX = 0.0, 150.0
HUMID_MIN, HUMID_MAX = 0.0, 100.0

async def get_clean_sensor_data():
    api = LaCrosse()
    try:
        await api.login(USERNAME, PASSWORD)
        locations = await api.get_locations()
        
        target_loc = next((loc for loc in locations if loc.name.strip().lower() == TARGET_LOCATION_NAME.lower()), None)
        if not target_loc:
            try:
                await api.logout()
            except Exception:
                pass
            return None
            
        now_ts = int(time.time())
        start_ts = now_ts - 1800  # 30-minute tracking window
        
        sensors = await api.get_sensors(target_loc, tz=LOCAL_TIMEZONE, start=start_ts, end=now_ts)
        
        temp = None
        wind = None
        humid = None
        
        for sensor in sensors:
            if not hasattr(sensor, "sensor_field_names") or not hasattr(sensor, "data"):
                continue
                
            for field in sensor.sensor_field_names:
                field_lower = field.lower()
                
                try:
                    field_blob = sensor.data.get(field, {})
                    values_list = field_blob.get("values", []) if isinstance(field_blob, dict) else []
                    
                    if not values_list:
                        continue
                        
                    val = values_list[-1].get("s")
                    if val is None:
                        continue
                        
                    num_val = float(val)
                    
                    # 1. Temperature conversion (C -> F)
                    if "ambient" in field_lower or "temp" in field_lower:
                        converted_val = (num_val * 9/5) + 32
                        if TEMP_MIN <= converted_val <= TEMP_MAX:
                            temp = round(converted_val, 1)
                            
                    # 2. Wind Handling: Explicitly look for speed, drop headings/direction
                    elif "wind" in field_lower:
                        if "heading" in field_lower or "direction" in field_lower or "dir" in field_lower:
                            continue  # Skip wind heading degrees
                        
                        if "speed" in field_lower or "gust" in field_lower or "velocity" in field_lower:
                            if WIND_MIN <= num_val <= WIND_MAX:
                                wind = round(num_val, 1)
                                
                    # 3. Humidity handling
                    elif "humidity" in field_lower or "rh" in field_lower:
                        if HUMID_MIN <= num_val <= HUMID_MAX:
                            humid = round(num_val, 1)
                except (ValueError, TypeError, KeyError, IndexError):
                    continue
                    
        try:
            await api.logout()
        except Exception:
            pass
            
        return {"temp": temp, "wind": wind, "humid": humid}
        
    except Exception as e:
        print(f"⚠️ Error extracting data via client library: {e}")
        try:
            await api.logout()
        except Exception:
            pass
        return None

def update_google_sheet(sensor_data):
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_SERVICE_ACCOUNT_FILE, scope)
        client = gspread.authorize(creds)
        sheet = client.open(GOOGLE_SHEET_NAME).sheet1
        
        tz = pytz.timezone(LOCAL_TIMEZONE)
        current_time = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
        
        temp_val = sensor_data.get("temp") if sensor_data.get("temp") is not None else "N/A"
        wind_val = sensor_data.get("wind") if sensor_data.get("wind") is not None else "N/A"
        humid_val = sensor_data.get("humid") if sensor_data.get("humid") is not None else "N/A"
        
        row = [current_time, temp_val, wind_val, humid_val]
        sheet.append_row(row)
        print(f"📦 [Logged Verified Data] Temp: {temp_val}°F | Wind: {wind_val} mph | Humid: {humid_val}%")
            
    except Exception as e:
        print(f"❌ Spreadsheet write failed: {e}")

async def main_loop():
    print(f"🚀 Initializing Backyard weather loop via La Crosse client library...")
    while True:
        try:
            metrics = await get_clean_sensor_data()
            if metrics and (metrics["temp"] is not None or metrics["wind"] is not None or metrics["humid"] is not None):
                update_google_sheet(metrics)
            else:
                print("⚠️ Sync skipped: Telemetry fields currently empty on this cycle.")
        except Exception as e:
            print(f"⚠️ Connection stream offline: {e}")
            
        await asyncio.sleep(65)

if __name__ == "__main__":
    if not USERNAME or not PASSWORD:
        print("❌ Missing account configuration credentials.")
    else:
        asyncio.run(main_loop())