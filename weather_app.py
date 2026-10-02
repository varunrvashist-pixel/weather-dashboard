import base64
from datetime import datetime
import json
import os
import re
from google.oauth2.service_account import Credentials
import gspread
import numpy as np
import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(page_title="Weather Station Dashboard", layout="wide")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

LACROSSE_SHEET_ID = "1NwM9U45ulkX_bTh5OVW5Sucah5VkacV7G1dj9uYXDXw"
TEMPEST_SHEET_ID = "1krSreOTSO_JkXZy_aVzsMKtOgQNUombxadCT6JqUCjQ"
DIY_SHEET_ID = "1YdRqfRsdRBIKEtmVNGujmUIpSGTWbYyVejcGfbVcQbI"


# ---------------- MICROCLIMATE & DEW POINT HELPERS ----------------
def clean_float(val):
  """Safely strips degree signs, %, and text, returning a float or None."""
  if val is None or pd.isna(val):
    return None
  try:
    c = (
        str(val)
        .replace("°F", "")
        .replace("F", "")
        .replace("°", "")
        .replace("%", "")
        .replace("hPa", "")
        .replace("mph", "")
        .strip()
    )
    return float(c) if c else None
  except Exception:
    return None


def compute_dew_point(temp_raw, hum_raw):
  """Safely computes dew point in Fahrenheit using the Magnus-Tetens approximation."""
  try:
    tf = clean_float(temp_raw)
    rh = clean_float(hum_raw)
    if tf is None or rh is None or rh <= 0 or rh > 100:
      return None

    tc = (tf - 32.0) * 5.0 / 9.0
    a, b = 17.27, 237.7
    alpha = ((a * tc) / (b + tc)) + np.log(rh / 100.0)
    dew_c = (b * alpha) / (a - alpha)
    return round((dew_c * 9.0 / 5.0) + 32.0, 1)
  except Exception:
    return None


def detect_pacific_high_pressure(df_history, current_pressure_hpa=None):
  """Detects Pacific High subsidence: high absolute pressure (>1016 hPa) and steady/rising trend."""
  if current_pressure_hpa is None and (
      df_history.empty or "Pressure" not in df_history.columns
  ):
    return {
        "status": "Neutral",
        "trend_3h": 0.0,
        "detail": "Standard baseline pressure",
    }

  press_col = find_col(
      df_history, ["Pressure", "Pressure (hPa)", "Barometric Pressure", "press"]
  )
  trend = 0.0

  if press_col and not df_history.empty:
    numeric_p = pd.to_numeric(
        df_history[press_col]
        .astype(str)
        .str.replace("hPa", "", regex=False)
        .str.strip(),
        errors="coerce",
    ).dropna()
    if len(numeric_p) >= 2:
      curr = numeric_p.iloc[-1]
      baseline_idx = max(0, len(numeric_p) - 60)
      prev = numeric_p.iloc[baseline_idx]
      trend = round(float(curr - prev), 1)
      if current_pressure_hpa is None:
        current_pressure_hpa = curr

  p_val = current_pressure_hpa if current_pressure_hpa is not None else 1013.25

  if p_val >= 1018.0 and trend >= 0.0:
    return {
        "status": "Strong High (Inversion Lid)",
        "trend_3h": trend,
        "detail": "Subsidence trapping marine layer low",
    }
  elif p_val >= 1014.0 and trend >= -0.5:
    return {
        "status": "Moderate High",
        "trend_3h": trend,
        "detail": "Stable coastal high pressure",
    }
  elif trend < -1.5:
    return {
        "status": "Falling Pressure",
        "trend_3h": trend,
        "detail": "Trough or weak inversion",
    }
  else:
    return {
        "status": "Neutral",
        "trend_3h": trend,
        "detail": "Standard baseline pressure",
    }


# ---------------- METAR SERVICE ----------------
@st.cache_data(ttl=300)
def get_metar_data(station_code="KSQL"):
  url = "https://aviationweather.gov/api/data/metar"
  params = {"ids": station_code, "format": "json"}
  try:
    res = requests.get(url, params=params, timeout=10)
    data = res.json()
    if data:
      return data[0]
  except Exception:
    return None
  return None


def render_metar_dropdown(station_code="KSQL"):
  obs = get_metar_data(station_code)
  label = f"🛫 {station_code} Airport Reference (METAR & Fog Indicators)"

  with st.expander(label):
    if not obs:
      st.write(f"METAR data currently unavailable for {station_code}.")
      return

    st.markdown(
        """
            <style>
            [data-testid="stMetricValue"] {
                font-size: 1.15rem !important;
            }
            [data-testid="stMetricLabel"] {
                font-size: 0.75rem !important;
            }
            div[data-testid="stCodeBlock"] pre {
                font-size: 0.75rem !important;
            }
            </style>
            """,
        unsafe_allow_html=True,
    )

    temp_c = obs.get("temp")
    temp_f = (
        f"{round((temp_c * 9 / 5) + 32, 1)}°F" if temp_c is not None else "N/A"
    )

    dewp_c = obs.get("dewp")
    dewp_f = (
        f"{round((dewp_c * 9 / 5) + 32, 1)}°F" if dewp_c is not None else "N/A"
    )

    spread_str = "N/A"
    if temp_c is not None and dewp_c is not None:
      spread_f = round((temp_c - dewp_c) * 9 / 5, 1)
      spread_str = f"{spread_f}°F"

    wspd_kt = obs.get("wspd")
    wdir = obs.get("wdir")
    if wspd_kt is not None:
      wspd_mph = round(wspd_kt * 1.15078, 1)
      wind_str = (
          f"{wspd_mph} mph ({wdir}°)" if wdir is not None else f"{wspd_mph} mph"
      )
    else:
      wind_str = "N/A"

    visib = obs.get("visib", "N/A")
    visib_str = f"{visib} SM" if visib != "N/A" else "N/A"

    clouds = obs.get("clouds", [])
    ceiling = "Clear / None"
    for layer in clouds:
      cover = layer.get("cover")
      base = layer.get("base")
      if cover in ["BKN", "OVC"]:
        ceiling = f"{cover} at {base} ft"
        break
      elif cover in ["FEW", "SCT"] and ceiling == "Clear / None":
        ceiling = f"{cover} at {base} ft"

    altim_mb = f"{obs.get('altim')} hPa" if obs.get("altim") else "N/A"
    fltcat = obs.get("fltcat", "N/A")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Temp", temp_f)
    col2.metric("Dew Point", dewp_f)
    col3.metric(
        "T-Td Spread",
        spread_str,
        help="Spread ≤ 3°F indicates high fog probability",
    )
    col4.metric("Wind", wind_str)

    col5, col6, col7, col8 = st.columns(4)
    col5.metric("Visibility", visib_str)
    col6.metric("Cloud / Ceiling", ceiling)
    col7.metric("Pressure", altim_mb)
    col8.metric("Flight Cat", fltcat)

    st.code(obs.get("rawOb", ""), language="text")


# ---------------- GOOGLE SHEETS & DATA PROCESSING ----------------
def get_google_sheets_client():
  if os.path.exists("cloud_key.json"):
    creds = Credentials.from_service_account_file(
        "cloud_key.json", scopes=SCOPES
    )
    return gspread.authorize(creds)

  try:
    if "GCP_KEY_BASE64" in st.secrets:
      key_json = base64.b64decode(st.secrets["GCP_KEY_BASE64"]).decode("utf-8")
      creds_info = json.loads(key_json)
      creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
      return gspread.authorize(creds)
  except Exception:
    pass

  st.error("Missing credentials. Please add GCP_KEY_BASE64 to Streamlit secrets.")
  st.stop()


def find_col(df, options):
  for opt in options:
    for c in df.columns:
      if opt.lower() == str(c).strip().lower():
        return c
  return None


@st.cache_data(ttl=180)
def load_sheet_data(sheet_id):
  if not sheet_id:
    return pd.DataFrame()
  try:
    gc = get_google_sheets_client()
    sheet = gc.open_by_key(sheet_id).sheet1
    records = sheet.get_all_records()
    if not records:
      return pd.DataFrame()
    df = pd.DataFrame(records)

    time_col = find_col(
        df, ["Date/Time", "Timestamp", "Date", "Time", "Datetime", "Logged At"]
    )
    if time_col:
      df["Timestamp"] = pd.to_datetime(df[time_col], errors="coerce")
      df = df.dropna(subset=["Timestamp"])
      return df.sort_values("Timestamp")
    else:
      return pd.DataFrame()
  except Exception as e:
    st.error(f"Error loading sheet {sheet_id}: {e}")
    return pd.DataFrame()


def format_display_time(ts):
  if pd.isna(ts):
    return "N/A"
  return pd.to_datetime(ts).strftime("%b %d, %Y, %-I:%M:%S %p")


def filter_by_duration(df, duration):
  if df.empty or "Timestamp" not in df.columns:
    return df.copy()

  temp_df = df.copy()
  latest_time = temp_df["Timestamp"].max()

  if duration == "daily":
    start_of_day = latest_time.normalize()
    end_of_day = start_of_day + pd.Timedelta(days=1)
    return temp_df[
        (temp_df["Timestamp"] >= start_of_day)
        & (temp_df["Timestamp"] < end_of_day)
    ].copy()
  elif duration == "weekly":
    cutoff = latest_time - pd.Timedelta(days=7)
  elif duration == "monthly":
    cutoff = latest_time - pd.Timedelta(days=30)
  elif duration == "all":
    return temp_df
  else:
    return temp_df

  return temp_df[temp_df["Timestamp"] >= cutoff].copy()


def extract_station_metrics(df, station_name="tempest"):
  """Extracts temperature, wind, humidity, and dew point with positional fallbacks."""
  if df.empty:
    return None

  latest = df.iloc[-1]

  t_col = find_col(
      df,
      [
          "Temperature",
          "Temp",
          "Outdoor Temp",
          "Air Temp",
          "Temp (F)",
          "temp_f",
          "air_temperature",
      ],
  )
  w_col = find_col(
      df,
      [
          "Wind Speed",
          "Wind",
          "Wind_Speed",
          "WindSpeed",
          "Wind (mph)",
          "wind_mph",
          "wind_avg",
      ],
  )
  h_col = find_col(
      df,
      [
          "Humidity",
          "Humidity (%)",
          "Outdoor Humidity",
          "Relative Humidity",
          "hum",
          "relative_humidity",
      ],
  )
  p_col = find_col(
      df, ["Pressure", "Pressure (hPa)", "Barometric Pressure", "press"]
  )

  # Column position fallbacks (col 1=Temp, col 2=Wind, col 3=Humidity)
  raw_t = latest[t_col] if t_col else (latest.iloc[1] if len(latest) > 1 else None)
  raw_w = latest[w_col] if w_col else (latest.iloc[2] if len(latest) > 2 else None)
  raw_h = latest[h_col] if h_col else (latest.iloc[3] if len(latest) > 3 else None)
  raw_p = latest[p_col] if p_col else None

  val_t = clean_float(raw_t)
  val_w = clean_float(raw_w)
  val_h = clean_float(raw_h)
  val_p = clean_float(raw_p)

  dew_pt = compute_dew_point(val_t, val_h)
  spread = (
      round(val_t - dew_pt, 1)
      if (val_t is not None and dew_pt is not None)
      else None
  )

  return {
      "temp_str": f"{val_t} °F" if val_t is not None else "N/A",
      "wind_str": f"{val_w} mph" if val_w is not None else "N/A",
      "hum_str": f"{val_h} %" if val_h is not None else "N/A",
      "press_str": f"{val_p} hPa" if val_p is not None else "N/A",
      "dew_str": f"{dew_pt} °F" if dew_pt is not None else "N/A",
      "spread_str": f"{spread}° spread" if spread is not None else None,
      "time_str": format_display_time(latest["Timestamp"]),
  }


# ---------------- ANALOG FORECAST (WITH PROJECTED DEW POINT) ----------------
def analog_forecast(df, hours_ahead=2):
  """Predicts weather and dew point by finding matching historical patterns."""
  if len(df) < 30:
    return (
        None,
        "Need at least a few days of data to find matching historical patterns.",
    )

  df_clean = df.sort_values("Timestamp").copy()
  current = df_clean.iloc[-1]
  curr_time = current["Timestamp"]

  t_col = find_col(
      df_clean,
      [
          "Temperature",
          "Temp",
          "Outdoor Temp",
          "Air Temp",
          "Temp (F)",
          "temp_f",
          "air_temperature",
      ],
  )
  h_col = find_col(
      df_clean,
      [
          "Humidity",
          "Humidity (%)",
          "Outdoor Humidity",
          "Relative Humidity",
          "hum",
          "relative_humidity",
      ],
  )

  raw_curr_t = (
      current[t_col] if t_col else (current.iloc[1] if len(current) > 1 else None)
  )
  raw_curr_h = (
      current[h_col] if h_col else (current.iloc[3] if len(current) > 3 else None)
  )

  curr_t = clean_float(raw_curr_t)
  curr_h = clean_float(raw_curr_h)

  if curr_t is None or curr_h is None:
    return None, "Current temperature or humidity readings could not be parsed."

  curr_dew = compute_dew_point(curr_t, curr_h)

  past_df = df_clean[
      df_clean["Timestamp"] < (curr_time - pd.Timedelta(hours=12))
  ].copy()

  target_hour = curr_time.hour
  past_df["hour_diff"] = (past_df["Timestamp"].dt.hour - target_hour).abs()
  candidates = past_df[
      (past_df["hour_diff"] <= 1) | (past_df["hour_diff"] >= 23)
  ].copy()

  if len(candidates) < 2:
    return None, "Not enough matching hours logged in past history yet."

  t_target = t_col if t_col else df_clean.columns[1]
  h_target = h_col if h_col else df_clean.columns[3]

  candidates["num_t"] = candidates[t_target].apply(clean_float)
  candidates["num_h"] = candidates[h_target].apply(clean_float)
  candidates = candidates.dropna(subset=["num_t", "num_h"])

  if candidates.empty:
    return None, "No valid historical records found to match."

  candidates["diff"] = np.sqrt(
      (candidates["num_t"] - curr_t) ** 2 + (candidates["num_h"] - curr_h) ** 2
  )
  best_matches = candidates.nsmallest(5, "diff")

  future_temps = []
  future_hums = []

  for _, match_row in best_matches.iterrows():
    match_time = match_row["Timestamp"]
    target_future = match_time + pd.Timedelta(hours=hours_ahead)

    future_window = df_clean[
        (df_clean["Timestamp"] >= target_future - pd.Timedelta(minutes=30))
        & (df_clean["Timestamp"] <= target_future + pd.Timedelta(minutes=30))
    ]
    if not future_window.empty:
      val_t = clean_float(future_window.iloc[0][t_target])
      val_h = clean_float(future_window.iloc[0][h_target])
      if val_t is not None:
        future_temps.append(val_t)
      if val_h is not None:
        future_hums.append(val_h)

  if not future_temps:
    return (
        None,
        "Matching historical moments found, but lacked follow-up readings.",
    )

  pred_t = round(float(np.mean(future_temps)), 1)
  pred_h = round(float(np.mean(future_hums)), 1)
  pred_dew = compute_dew_point(pred_t, pred_h)

  delta_t = round(pred_t - curr_t, 1)
  delta_h = round(pred_h - curr_h, 1)
  delta_dew = (
      round(pred_dew - curr_dew, 1)
      if (pred_dew is not None and curr_dew is not None)
      else None
  )

  pred_spread = pred_t - pred_dew if pred_dew is not None else 99.0
  if pred_h >= 88 or pred_spread <= 2.5:
    fog_flag = "High"
  elif pred_h >= 80 or pred_spread <= 4.0:
    fog_flag = "Moderate"
  else:
    fog_flag = "Low"

  return {
      "pred_temp": pred_t,
      "pred_hum": pred_h,
      "pred_dew": pred_dew,
      "delta_temp": delta_t,
      "delta_hum": delta_h,
      "delta_dew": delta_dew,
      "fog_risk": fog_flag,
      "matches": len(future_temps),
      "closest_date": best_matches.iloc[0]["Timestamp"].strftime("%b %d, %Y"),
  }, None


# ---------------- CHARTS ----------------
def render_station_charts(df, station_name, tab_name, metar_station="KSQL"):
  if df.empty:
    st.info(f"No data available for {station_name} in this timeframe.")
    render_metar_dropdown(metar_station)
    return

  plot_df = df.copy()
  prefix = f"{tab_name}_{station_name}".lower().replace(" ", "_")

  day_x_range = None
  tick_format = "%b %d, %-I:%M %p"

  if tab_name == "daily" and not plot_df.empty:
    day_start = plot_df["Timestamp"].max().normalize()
    day_end = day_start + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    day_x_range = [day_start, day_end]
    tick_format = "%-I:%M %p"

  temp_col = find_col(
      plot_df,
      [
          "Temperature",
          "Temp",
          "Outdoor Temp",
          "Air Temp",
          "Temp (F)",
          "temp_f",
      ],
  )
  wind_col = find_col(
      plot_df, ["Wind Speed", "Wind", "Wind_Speed", "WindSpeed", "Wind (mph)"]
  )
  hum_col = find_col(
      plot_df,
      [
          "Humidity (%)",
          "Humidity",
          "Outdoor Humidity",
          "Relative Humidity",
          "hum",
      ],
  )
  press_col = find_col(
      plot_df, ["Pressure", "Pressure (hPa)", "Barometric Pressure", "press"]
  )

  chart_config = {"responsive": True, "displayModeBar": False}

  if temp_col:
    plot_df[temp_col] = (
        plot_df[temp_col]
        .astype(str)
        .str.replace("°F", "", regex=False)
        .str.strip()
    )
    plot_df[temp_col] = pd.to_numeric(plot_df[temp_col], errors="coerce")
    clean_temp_df = plot_df.dropna(subset=[temp_col, "Timestamp"]).sort_values(
        "Timestamp"
    )

    if not clean_temp_df.empty:
      fig_temp = px.line(
          clean_temp_df,
          x="Timestamp",
          y=temp_col,
          title=f"{station_name} - Temperature Over Time",
      )
      layout_args = dict(
          autosize=True,
          margin=dict(l=20, r=20, t=40, b=20),
          xaxis=dict(tickformat=tick_format),
      )
      if day_x_range:
        layout_args["xaxis_range"] = day_x_range
      fig_temp.update_layout(**layout_args)
      st.plotly_chart(
          fig_temp,
          use_container_width=True,
          config=chart_config,
          key=f"{prefix}_temp_chart",
      )

  if wind_col:
    plot_df[wind_col] = (
        plot_df[wind_col]
        .astype(str)
        .str.replace("mph", "", regex=False)
        .str.strip()
    )
    plot_df[wind_col] = pd.to_numeric(plot_df[wind_col], errors="coerce")
    clean_wind_df = plot_df.dropna(subset=[wind_col, "Timestamp"]).sort_values(
        "Timestamp"
    )

    if not clean_wind_df.empty:
      fig_wind = px.line(
          clean_wind_df,
          x="Timestamp",
          y=wind_col,
          title=f"{station_name} - Wind Speed Over Time",
      )
      layout_args = dict(
          autosize=True,
          margin=dict(l=20, r=20, t=40, b=20),
          xaxis=dict(tickformat=tick_format),
      )
      if day_x_range:
        layout_args["xaxis_range"] = day_x_range
      fig_wind.update_layout(**layout_args)
      st.plotly_chart(
          fig_wind,
          use_container_width=True,
          config=chart_config,
          key=f"{prefix}_wind_chart",
      )

  if hum_col:
    plot_df[hum_col] = (
        plot_df[hum_col].astype(str).str.replace("%", "", regex=False).str.strip()
    )
    plot_df[hum_col] = pd.to_numeric(plot_df[hum_col], errors="coerce")
    clean_hum_df = plot_df.dropna(subset=[hum_col, "Timestamp"]).sort_values(
        "Timestamp"
    )

    if not clean_hum_df.empty:
      fig_hum = px.line(
          clean_hum_df,
          x="Timestamp",
          y=hum_col,
          title=f"{station_name} - Humidity Over Time",
      )
      layout_args = dict(
          autosize=True,
          margin=dict(l=20, r=20, t=40, b=20),
          xaxis=dict(tickformat=tick_format),
      )
      if day_x_range:
        layout_args["xaxis_range"] = day_x_range
      fig_hum.update_layout(**layout_args)
      st.plotly_chart(
          fig_hum,
          use_container_width=True,
          config=chart_config,
          key=f"{prefix}_hum_chart",
      )

  render_metar_dropdown(metar_station)

  if press_col:
    plot_df[press_col] = (
        plot_df[press_col]
        .astype(str)
        .str.replace("hPa", "", regex=False)
        .str.strip()
    )
    plot_df[press_col] = pd.to_numeric(plot_df[press_col], errors="coerce")
    clean_press_df = plot_df.dropna(
        subset=[press_col, "Timestamp"]
    ).sort_values("Timestamp")

    if not clean_press_df.empty:
      fig_press = px.line(
          clean_press_df,
          x="Timestamp",
          y=press_col,
          title=f"{station_name} - Pressure Over Time",
      )
      layout_args = dict(
          autosize=True,
          margin=dict(l=20, r=20, t=40, b=20),
          xaxis=dict(tickformat=tick_format),
      )
      if day_x_range:
        layout_args["xaxis_range"] = day_x_range
      fig_press.update_layout(**layout_args)
      st.plotly_chart(
          fig_press,
          use_container_width=True,
          config=chart_config,
          key=f"{prefix}_press_chart",
      )


# ---------------- MAIN DASHBOARD ----------------
@st.fragment(run_every="180s")
def render_dashboard():
  st.title("🌦️ Weather Station Dashboard")

  station_view = st.radio(
      "Station View",
      [
          "⚡ La Crosse & Tempest",
          "🛠️ DIY BME280 Station",
      ],
      horizontal=True,
  )

  df_lacrosse = load_sheet_data(LACROSSE_SHEET_ID)
  df_tempest = load_sheet_data(TEMPEST_SHEET_ID)
  df_diy = load_sheet_data(DIY_SHEET_ID)

  if station_view == "⚡ La Crosse & Tempest":
    col_lacrosse, col_tempest = st.columns(2)

    with col_lacrosse:
      st.subheader("🏡 La Crosse")
      m_lacrosse = extract_station_metrics(df_lacrosse, "lacrosse")
      if m_lacrosse:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Temp", m_lacrosse["temp_str"])
        m2.metric("Wind", m_lacrosse["wind_str"])
        m3.metric("Humidity", m_lacrosse["hum_str"])
        m4.metric(
            "Dew Point",
            m_lacrosse["dew_str"],
            delta=m_lacrosse["spread_str"],
            delta_color="inverse" if m_lacrosse["spread_str"] else "normal",
        )
        st.caption(f"Last updated: {m_lacrosse['time_str']}")
      else:
        st.warning("No data found for La Crosse station.")

    with col_tempest:
      st.subheader("⚡ Tempest")
      m_tempest = extract_station_metrics(df_tempest, "tempest")
      if m_tempest:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Temp", m_tempest["temp_str"])
        m2.metric("Wind", m_tempest["wind_str"])
        m3.metric("Humidity", m_tempest["hum_str"])
        m4.metric(
            "Dew Point",
            m_tempest["dew_str"],
            delta=m_tempest["spread_str"],
            delta_color="inverse" if m_tempest["spread_str"] else "normal",
        )
        st.caption(f"Last updated: {m_tempest['time_str']}")
      else:
        st.warning("No data found for Tempest station.")

  else:
    st.subheader("🛠️ DIY BME280 Station")
    m_diy = extract_station_metrics(df_diy, "diy")
    if m_diy:
      m1, m2, m3, m4 = st.columns(4)
      m1.metric("Temp", m_diy["temp_str"])
      m2.metric("Humidity", m_diy["hum_str"])
      m3.metric("Pressure", m_diy["press_str"])
      m4.metric(
          "Dew Point",
          m_diy["dew_str"],
          delta=m_diy["spread_str"],
          delta_color="inverse" if m_diy["spread_str"] else "normal",
      )
      st.caption(f"Last updated: {m_diy['time_str']}")
    else:
      st.warning("No data found for DIY station.")

  # ---------------- HIGH PRESSURE & SUBSIDENCE INDICATOR ----------------
  target_press_df = (
      df_diy
      if not df_diy.empty
      else (df_tempest if not df_tempest.empty else df_lacrosse)
  )
  high_p_info = detect_pacific_high_pressure(target_press_df)

  with st.container():
    h1, h2 = st.columns([1, 2])
    with h1:
      st.metric(
          "North Pacific High System",
          high_p_info["status"],
          delta=f"{high_p_info['trend_3h']:+} hPa (3hr)",
      )
    with h2:
      st.caption(
          f"**Atmospheric Inversion Status:** {high_p_info['detail']}. Strong"
          " high pressure aloft pushes down from the Pacific, acting like a lid"
          " that traps marine fog low along the Peninsula."
      )

  # ---------------- HISTORICAL PATTERN FORECAST CARD (5 COLUMNS) ----------------
  active_df = df_tempest if station_view == "⚡ La Crosse & Tempest" else df_diy
  if not active_df.empty:
    forecast, err = analog_forecast(active_df, hours_ahead=2)
    if forecast:
      st.divider()
      with st.container():
        st.markdown("#### 🔮 Historical Pattern Forecast (+2 Hours)")
        fc1, fc2, fc3, fc4, fc5 = st.columns(5)
        fc1.metric(
            "Projected Temp",
            f"{forecast['pred_temp']}°F",
            delta=f"{forecast['delta_temp']:+}°F",
        )
        fc2.metric(
            "Projected Humidity",
            f"{forecast['pred_hum']}%",
            delta=f"{forecast['delta_hum']:+}%",
        )
        fc3.metric(
            "Projected Dew Point",
            (
                f"{forecast['pred_dew']}°F"
                if forecast["pred_dew"] is not None
                else "N/A"
            ),
            delta=(
                f"{forecast['delta_dew']:+}°F"
                if forecast["delta_dew"] is not None
                else None
            ),
        )
        fc4.metric("Fog Propensity", forecast["fog_risk"])
        fc5.metric("Matching Days", f"{forecast['matches']} days")
        st.caption(
            "Analyzed against similar past days in your sheet (closest pattern:"
            f" **{forecast['closest_date']}**)."
        )
    elif err:
      st.caption(f"Forecast model info: {err}")

  st.divider()

  timeframe = st.radio(
      "Select Timeframe",
      [
          "📅 Daily",
          "🗓️ Weekly",
          "📆 Monthly",
          "♾️ All Time",
      ],
      horizontal=True,
      label_visibility="collapsed",
  )

  duration_key = (
      "daily"
      if "Daily" in timeframe
      else (
          "weekly"
          if "Weekly" in timeframe
          else ("monthly" if "Monthly" in timeframe else "all")
      )
  )

  if station_view == "⚡ La Crosse & Tempest":
    c1, c2 = st.columns(2)
    with c1:
      render_station_charts(
          filter_by_duration(df_lacrosse, duration_key),
          "La Crosse",
          duration_key,
          metar_station="KSQL",
      )
    with c2:
      render_station_charts(
          filter_by_duration(df_tempest, duration_key),
          "Tempest",
          duration_key,
          metar_station="KSFO",
      )

  elif station_view == "🛠️ DIY BME280 Station":
    render_station_charts(
        filter_by_duration(df_diy, duration_key),
        "DIY Station",
        duration_key,
        metar_station="KSQL",
    )


render_dashboard()