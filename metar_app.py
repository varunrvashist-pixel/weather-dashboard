import base64
from datetime import datetime
import json
import math
import os
import socket
import sys
import time
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials
import gspread
import requests

# Prevent indefinite hangs on Google Sheets or API calls
socket.setdefaulttimeout(20)

# ---------------- CONFIGURATION ----------------
METAR_SHEET_ID = "1ZMwL4PO-vWTFPPk829Z6NCm5853zbh_ykmMn6mhBZaQ"
KEY_FILE_PATH = "cloud_key.json"
ICAO_STATION = os.getenv("METAR_STATION", "KSQL")

# Airports typically update METARs hourly (or more often during rapid weather changes)
POLL_INTERVAL_SECONDS = 900  # 15 minutes

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_gspread_client():
  """Authenticates with Google Sheets using service account key."""
  if os.path.exists(KEY_FILE_PATH):
    creds = Credentials.from_service_account_file(KEY_FILE_PATH, scopes=SCOPES)
    return gspread.authorize(creds)

  if "GCP_KEY_BASE64" in os.environ:
    key_json = base64.b64decode(os.environ["GCP_KEY_BASE64"]).decode("utf-8")
    creds_info = json.loads(key_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)

  raise FileNotFoundError(
      f"Could not find '{KEY_FILE_PATH}' or 'GCP_KEY_BASE64' environment"
      " variable."
  )


def compute_relative_humidity(temp_c, dewp_c):
  """Calculates Relative Humidity (%) from Celsius temp and dew point via Magnus formula."""
  if temp_c is None or dewp_c is None:
    return None
  a, b = 17.27, 237.7
  rh = 100.0 * (
      math.exp((a * dewp_c) / (b + dewp_c))
      / math.exp((a * temp_c) / (b + temp_c))
  )
  return round(max(0.0, min(100.0, rh)), 1)


def parse_visibility(vis_raw):
  """Converts METAR visibility (e.g., '10+', '2.5', 10) into a numeric float in miles."""
  if vis_raw is None:
    return None
  try:
    cleaned = str(vis_raw).replace("+", "").strip()
    if "/" in cleaned:
      parts = cleaned.split()
      total = 0.0
      for p in parts:
        if "/" in p:
          num, den = p.split("/")
          total += float(num) / float(den)
        else:
          total += float(p)
      return round(total, 2)
    return round(float(cleaned), 2)
  except Exception:
    return None


def fetch_metar_observation():
  """Fetches the latest METAR observation from NOAA Aviation Weather API."""
  url = f"https://aviationweather.gov/api/data/metar?ids={ICAO_STATION}&format=json"
  response = requests.get(url, timeout=15)
  response.raise_for_status()
  data = response.json()

  if not data:
    raise ValueError(f"No METAR data returned for {ICAO_STATION}.")

  obs = data[0]
  obs_epoch = obs.get("obsTime")

  if obs_epoch:
    dt_local = datetime.fromtimestamp(
        obs_epoch, tz=ZoneInfo("America/Los_Angeles")
    )
    obs_time_str = dt_local.strftime("%Y-%m-%d %H:%M:%S")
  else:
    obs_time_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

  # Temperature (°F) & Relative Humidity (%)
  temp_c = obs.get("temp")
  dewp_c = obs.get("dewp")
  temp_f = round((temp_c * 9 / 5) + 32, 1) if temp_c is not None else None
  humidity = compute_relative_humidity(temp_c, dewp_c)

  # Wind Speed (mph)
  wspd_kt = obs.get("wspd")
  wind_mph = round(wspd_kt * 1.15078, 1) if wspd_kt is not None else 0.0

  # Altimeter Pressure (hPa)
  pressure = obs.get("altim")

  # Precipitation (inches, defaults to 0.0 if not reported)
  precip = obs.get("precip")
  rain_in = round(float(precip), 2) if precip is not None else 0.0

  # Visibility (miles) & Normalized Fog Index (0.0 = Clear, 1.0 = Zero Visibility)
  visib_mi = parse_visibility(obs.get("visib"))
  if visib_mi is not None:
    capped_vis = min(visib_mi, 10.0)
    fog_index = round(1.0 - (capped_vis / 10.0), 2)
  else:
    fog_index = None

  return {
      "obs_time": obs_time_str,
      "obs_epoch": obs_epoch,
      "temp_f": temp_f,
      "humidity": humidity,
      "wind_mph": wind_mph,
      "pressure": pressure,
      "rain_in": rain_in,
      "visib_mi": visib_mi,
      "fog_index": fog_index,
  }


def run_service():
  print("=" * 60, flush=True)
  print(f"Starting METAR Ingestion Service ({ICAO_STATION})", flush=True)
  print(f"Target Sheet ID: {METAR_SHEET_ID}", flush=True)
  print(f"Poll Interval:   {POLL_INTERVAL_SECONDS}s", flush=True)
  print("=" * 60, flush=True)

  try:
    gc = get_gspread_client()
    sheet = gc.open_by_key(METAR_SHEET_ID).sheet1
    print("Connected to Google Sheets successfully.\n", flush=True)
  except Exception as e:
    print(f"Failed to authenticate with Google Sheets: {e}", flush=True)
    time.sleep(10)
    return

  last_logged_epoch = None

  while True:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
      reading = fetch_metar_observation()
      obs_epoch = reading["obs_epoch"]

      if obs_epoch and obs_epoch == last_logged_epoch:
        print(
            f"[{now_str}] Duplicate METAR observation ({reading['obs_time']})."
            " Skipping.",
            flush=True,
        )
      else:
        # 8 columns: Timestamp, Temperature, Humidity, Wind Speed, Pressure, Rain, Visibility, Fog
        row = [
            reading["obs_time"],
            reading["temp_f"],
            reading["humidity"],
            reading["wind_mph"],
            reading["pressure"],
            reading["rain_in"],
            reading["visib_mi"],
            reading["fog_index"],
        ]
        sheet.append_row(row, value_input_option="USER_ENTERED")
        last_logged_epoch = obs_epoch

        print(
            f"[{now_str}] Logged {ICAO_STATION} -> Temp: {reading['temp_f']}°F |"
            f" Hum: {reading['humidity']}% | Wind: {reading['wind_mph']} mph |"
            f" Press: {reading['pressure']} hPa | Vis: {reading['visib_mi']} mi"
            f" | FogIdx: {reading['fog_index']}",
            flush=True,
        )

    except requests.exceptions.RequestException as req_err:
      print(f"[{now_str}] Network/API Error: {req_err}", flush=True)
    except Exception as err:
      print(f"[{now_str}] Error: {err}", flush=True)

    time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
  while True:
    try:
      run_service()
    except KeyboardInterrupt:
      print("\nProcess manually halted.", flush=True)
      sys.exit(0)
    except BaseException as critical_err:
      print(
          f"CRITICAL PROCESS ERROR: {critical_err}. Retrying in 10s...",
          flush=True,
      )
      time.sleep(10)