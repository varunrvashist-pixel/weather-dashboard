import time
import asyncio
import aiohttp
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from lacrosse_view import LaCrosse

# --- CONFIGURATION ---
USERNAME = "rvashist@gmail.com"
PASSWORD = "weather2807"
GOOGLE_SHEET_NAME = "Weather Database"

def get_google_sheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("cloud_key.json", scope)
    client = gspread.authorize(creds)
    return client.open(GOOGLE_SHEET_NAME).sheet1

async def fetch_weather_data_async():
    if not USERNAME or not PASSWORD:
        print("❌ Error: Missing Username or Password inside test_cloud.py")
        return None, None, None

    async with aiohttp.ClientSession() as session:
        api = LaCrosse()
        api.websession = session
        try:
            await api.login(USERNAME, PASSWORD)
            await asyncio.sleep(1)
            locations = await api.get_locations()
            
            temp, wind, humid = None, None, None
            for location in locations:
                for device in location.devices:
                    for obs in device.obs:
                        # CRITICAL BUG FIX: Using strict exact equality prevents 
                        # matching with "WindDirection" fields entirely.
                        if obs.sensor_type == "Temperature":
                            temp = (float(obs.value) * 9/5) + 32
                        elif obs.sensor_type == "WindSpeed":
                            wind = float(obs.value) * 2.23694
                        elif obs.sensor_type == "Humidity":
                            humid = float(obs.value)
            return temp, wind, humid
        except Exception as e:
            print(f"❌ Error pulling from La Crosse API: {e}")
            return None, None, None

async def main():
    print("🚀 Initializing Decoupled Ingestion Streamer Pipeline via test_cloud.py...")
    sheet = get_google_sheet()
    
    while True:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        temp, wind, humid = await fetch_weather_data_async()
        
        # Guardrail against anomalous hardware sensor spikes
        if wind is not None and (wind >= 120.0 or wind < 0.0):
            print(f"⚠️ Glitch filtered: Wind Speed ({wind:.1f} mph) out of bounds. Setting to empty.")
            wind = None
            
        if temp is not None:
            wind_string = f"{wind:.1f}" if wind is not None else ""
            sheet.append_row([timestamp, f"{temp:.2f}", wind_string, f"{humid:.1f}"])
            print(f"📦 Successfully logged reading at {timestamp} -> Temp: {temp:.2f}°F, Wind: {wind_string} mph, Humid: {humid:.1f}%")
        else:
            print(f"❌ [{timestamp}] Failed to fetch valid sensor data from account stream.")
            
        await asyncio.sleep(300) # Sleep 5 minutes

if __name__ == "__main__":
    asyncio.run(main())