import os
import asyncio
import time
from datetime import datetime, timedelta
import base64
import json
import pytz
import gspread
from google.oauth2.service_account import Credentials
from lacrosse_view import LaCrosse

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "Weather Station Data")
TIMEZONE = "America/Los_Angeles"

def get_google_sheets_client():
    b64_key = os.getenv("GCP_KEY_BASE64")
    if b64_key:
        key_json = base64.b64decode(b64_key).decode("utf-8")
        creds_info = json.loads(key_json)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    elif os.path.exists("cloud_key.json"):
        creds = Credentials.from_service_account_file("cloud_key.json", scopes=SCOPES)
    else:
        raise FileNotFoundError("Missing Google credentials.")
    return gspread.authorize(creds)

def is_plausible(temp, wind, hum):
    try:
        t, w, h = float(temp), float(wind), float(hum)
        return (-40 <= t <= 140) and (0 <= w < 150) and (0 <= h <= 100)
    except (ValueError, TypeError):
        return False

async def main():
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
            print("No locations found.")
            return

        location = locations[0]
        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=15)

        sensors = await api.get_sensors(
            location,
            tz=TIMEZONE,
            start=int(time.mktime(start_time.timetuple())),
            end=int(time.mktime(end_time.timetuple()))
        )

        if not sensors:
            print("No sensor data returned.")
            return

        sensor = sensors[0]
        temp_val, wind_val, hum_val = None, None, None

        for field in sensor.sensor_field_names:
            try:
                val = sensor.data[field]["values"][-1]["s"]
                if "Temp" in field:
                    raw_t = float(val)
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
                print(f"Logged reading successfully: {[now_str, temp_val, wind_val, hum_val]}")
            else:
                print("Implausible reading ignored.")
        else:
            print("Missing one or more sensor readings.")
    except Exception as e:
        print(f"Error during fetch: {e}")
    finally:
        try:
            await api.logout()
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(main())