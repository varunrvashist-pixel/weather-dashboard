import base64
from datetime import datetime
import json
import os
import re
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials
import gspread
import numpy as np
import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(
    page_title="Weather Station Dashboard",
    page_icon="🌦️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------- THEME & GLASSMORPHIC STYLING ----------------
st.markdown(
    """
<style>
    /* Dark background canvas */
    .stApp {
        background-color: #0d1117;
        color: #e6edf3;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }

    /* Elevated Glass Cards */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(22, 27, 34, 0.75) !important;
        border: 1px solid rgba(240, 246, 252, 0.1) !important;
        border-radius: 14px !important;
        padding: 1.15rem !important;
        backdrop-filter: blur(12px) !important;
        -webkit-backdrop-filter: blur(12px) !important;
        box-shadow: 0 8px 24px rgba(1, 4, 9, 0.4) !important;
        margin-bottom: 0.85rem !important;
    }

    /* Metric Card Typography */
    div[data-testid="stMetricValue"] {
        font-size: 1.85rem !important;
        font-weight: 700 !important;
        color: #f0f6fc !important;
        letter-spacing: -0.02em !important;
    }
    div[data-testid="stMetricLabel"] {
        color: #8b949e !important;
        font-size: 0.82rem !important;
        text-transform: uppercase !important;
        letter-spacing: 0.06em !important;
        font-weight: 600 !important;
    }

    /* Compact Divider */
    hr {
        margin: 1.5rem 0 !important;
        border-color: rgba(240, 246, 252, 0.08) !important;
    }

    /* Expander Styling */
    div[data-testid="stExpander"] {
        background: rgba(22, 27, 34, 0.5) !important;
        border: 1px solid rgba(240, 246, 252, 0.08) !important;
        border-radius: 10px !important;
    }
</style>
""",
    unsafe_allow_html=True,
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

LACROSSE_SHEET_ID = "1NwM9U45ulkX_bTh5OVW5Sucah5VkacV7G1dj9uYXDXw"
TEMPEST_SHEET_ID = "1krSreOTSO_JkXZy_aVzsMKtOgQNUombxadCT6JqUCjQ"
DIY_SHEET_ID = "1YdRqfRsdRBIKEtmVNGujmUIpSGTWbYyVejcGfbVcQbI"


# ---------------- MICROCLIMATE & DEW POINT HELPERS ----------------
def clean_float(val):
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
  """Magnus-Tetens formula for dew point in Fahrenheit."""
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


def calculate_current_fog_risk(temp_raw, hum_raw):
  """Calculates instantaneous fog propensity from live observations."""
  t = clean_float(temp_raw)
  h = clean_float(hum_raw)
  if t is None or h is None:
    return "Unknown", None, None

  dew = compute_dew_point(t, h)
  spread = round(t - dew, 1) if dew is not None else None

  if (spread is not None and spread <= 2.5) or h >= 90:
    risk = "High"
  elif (spread is not None and spread <= 4.0) or h >= 82:
    risk = "Moderate"
  else:
    risk = "Low"

  return risk, dew, spread


def get_live_badge(latest_ts):
  """Timezone-corrected reporting badge in Pacific Time."""
  if pd.isna(latest_ts):
    return '<span style="color:#8b949e; font-size:0.75rem; font-weight:600;">⚪ OFFLINE</span>'

  now = datetime.now(ZoneInfo("America/Los_Angeles")).replace(tzinfo=None)
  raw_time = pd.to_datetime(latest_ts).replace(tzinfo=None)

  diff_min = max(0, int((now - raw_time).total_seconds() / 60))
  if diff_min <= 10:
    return '<span style="background:rgba(46,160,67,0.15); color:#3fb950; border:1px solid rgba(46,160,67,0.4); padding:2px 8px; border-radius:12px; font-size:0.72rem; font-weight:600;">🟢 LIVE</span>'
  elif diff_min <= 60:
    return f'<span style="background:rgba(210,153,34,0.15); color:#d29922; border:1px solid rgba(210,153,34,0.4); padding:2px 8px; border-radius:12px; font-size:0.72rem; font-weight:600;">🟡 {diff_min}m AGO</span>'
  else:
    hrs = diff_min // 60
    return f'<span style="background:rgba(248,81,73,0.15); color:#f85149; border:1px solid rgba(248,81,73,0.4); padding:2px 8px; border-radius:12px; font-size:0.72rem; font-weight:600;">🔴 {hrs}h AGO</span>'


def get_fog_badge(risk):
  if risk == "High":
    return '<span style="background:rgba(248,81,73,0.18); color:#ff7b72; border:1px solid rgba(248,81,73,0.45); padding:4px 12px; border-radius:16px; font-weight:700; font-size:0.85rem; letter-spacing:0.04em;">⚠️ HIGH RISK</span>'
  elif risk == "Moderate":
    return '<span style="background:rgba(210,153,34,0.18); color:#e3b341; border:1px solid rgba(210,153,34,0.45); padding:4px 12px; border-radius:16px; font-weight:700; font-size:0.85rem; letter-spacing:0.04em;">⚡ MODERATE</span>'
  elif risk == "Low":
    return '<span style="background:rgba(46,160,67,0.18); color:#56d364; border:1px solid rgba(46,160,67,0.45); padding:4px 12px; border-radius:16px; font-weight:700; font-size:0.85rem; letter-spacing:0.04em;">✓ LOW / CLEAR</span>'
  return '<span style="color:#8b949e; font-size:0.85rem;">N/A</span>'


def detect_pacific_high_pressure(df_history, current_pressure_hpa=None):
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
        "detail": (
            "Subsidence compressing marine layer; fog trapped low along"
            " Peninsula"
        ),
    }
  elif p_val >= 1014.0 and trend >= -0.5:
    return {
        "status": "Moderate High",
        "trend_3h": trend,
        "detail": "Stable coastal atmospheric pattern",
    }
  elif trend < -1.5:
    return {
        "status": "Falling Pressure",
        "trend_3h": trend,
        "detail": "Approaching trough or weakening inversion layer",
    }
  else:
    return {
        "status": "Neutral",
        "trend_3h": trend,
        "detail": "Normal baseline pressure",
    }


# ---------------- METAR REGIONAL REFERENCE ----------------
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
  label = f"🛫 Regional Reference • {station_code} METAR & Inversion Check"

  with st.expander(label):
    if not obs:
      st.caption(f"METAR data currently unavailable for {station_code}.")
      return

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
    ceiling = "Clear"
    for layer in clouds:
      cover = layer.get("cover")
      base = layer.get("base")
      if cover in ["BKN", "OVC"]:
        ceiling = f"{cover} @ {base} ft"
        break
      elif cover in ["FEW", "SCT"] and ceiling == "Clear":
        ceiling = f"{cover} @ {base} ft"

    altim_mb = f"{obs.get('altim')} hPa" if obs.get("altim") else "N/A"
    fltcat = obs.get("fltcat", "N/A")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Airport Temp", temp_f)
    c2.metric("Airport Dew Point", dewp_f)
    c3.metric(
        "T - Td Spread",
        spread_str,
        help="Spread ≤ 3°F indicates saturation & high fog likelihood",
    )
    c4.metric("Airport Wind", wind_str)

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Visibility", visib_str)
    c6.metric("Ceiling", ceiling)
    c7.metric("Pressure", altim_mb)
    c8.metric("Flight Category", fltcat)

    st.code(obs.get("rawOb", ""), language="text")


# ---------------- GOOGLE SHEETS & INGESTION ----------------
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

  st.error("Missing GCP credentials. Please configure GCP_KEY_BASE64 in Streamlit secrets.")
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
  return pd.to_datetime(ts).strftime("%b %d, %-I:%M:%S %p")


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

  raw_t = latest[t_col] if t_col else (latest.iloc[1] if len(latest) > 1 else None)
  raw_w = latest[w_col] if w_col else (latest.iloc[2] if len(latest) > 2 else None)
  raw_h = latest[h_col] if h_col else (latest.iloc[3] if len(latest) > 3 else None)
  raw_p = latest[p_col] if p_col else None

  val_t = clean_float(raw_t)
  val_w = clean_float(raw_w)
  val_h = clean_float(raw_h)
  val_p = clean_float(raw_p)

  return {
      "temp_str": f"{val_t} °F" if val_t is not None else "N/A",
      "wind_str": f"{val_w} mph" if val_w is not None else "N/A",
      "hum_str": f"{val_h} %" if val_h is not None else "N/A",
      "press_str": f"{val_p} hPa" if val_p is not None else "N/A",
      "raw_t": val_t,
      "raw_h": val_h,
      "raw_ts": latest["Timestamp"],
      "time_str": format_display_time(latest["Timestamp"]),
  }


# ---------------- TEMPEST DYNAMIC HORIZON FORECAST ENGINE ----------------
def analog_forecast(df, hours_ahead=2):
  min_records = 30 if hours_ahead < 24 else 60
  if len(df) < min_records:
    return (
        None,
        f"Requires more logged Tempest observations to project +{hours_ahead} hours.",
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
    return None, "Current Tempest temperature or humidity could not be parsed."

  curr_dew = compute_dew_point(curr_t, curr_h)

  min_past_hours = max(12, hours_ahead + 2)
  past_df = df_clean[
      df_clean["Timestamp"] < (curr_time - pd.Timedelta(hours=min_past_hours))
  ].copy()

  target_hour = curr_time.hour
  past_df["hour_diff"] = (past_df["Timestamp"].dt.hour - target_hour).abs()
  candidates = past_df[
      (past_df["hour_diff"] <= 1) | (past_df["hour_diff"] >= 23)
  ].copy()

  if len(candidates) < 2:
    return None, f"Not enough matching historical hours for +{hours_ahead}h."

  t_target = t_col if t_col else df_clean.columns[1]
  h_target = h_col if h_col else df_clean.columns[3]

  candidates["num_t"] = candidates[t_target].apply(clean_float)
  candidates["num_h"] = candidates[h_target].apply(clean_float)
  candidates = candidates.dropna(subset=["num_t", "num_h"])

  if candidates.empty:
    return None, "Historical records lack usable numeric observations."

  candidates["diff"] = np.sqrt(
      (candidates["num_t"] - curr_t) ** 2 + (candidates["num_h"] - curr_h) ** 2
  )
  best_matches = candidates.nsmallest(5, "diff")

  future_temps = []
  future_hums = []
  window_margin = pd.Timedelta(minutes=30) if hours_ahead < 24 else pd.Timedelta(hours=1)

  for _, match_row in best_matches.iterrows():
    match_time = match_row["Timestamp"]
    target_future = match_time + pd.Timedelta(hours=hours_ahead)

    future_window = df_clean[
        (df_clean["Timestamp"] >= target_future - window_margin)
        & (df_clean["Timestamp"] <= target_future + window_margin)
    ]
    if not future_window.empty:
      val_t = clean_float(future_window.iloc[0][t_target])
      val_h = clean_float(future_window.iloc[0][h_target])
      if val_t is not None:
        future_temps.append(val_t)
      if val_h is not None:
        future_hums.append(val_h)

  if not future_temps:
    return None, f"Found matches, but subsequent +{hours_ahead}h records were not logged."

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


# ---------------- SLEEK THEMED CHARTS ----------------
def render_styled_chart(
    clean_df,
    col_name,
    title,
    color_hex,
    fill_rgba,
    tick_format,
    day_x_range,
    y_range=None,
):
  fig = px.line(clean_df, x="Timestamp", y=col_name, title=title)
  fig.update_traces(
      line=dict(color=color_hex, width=2.5),
      fill="tozeroy",
      fillcolor=fill_rgba,
      hovertemplate="%{x|%b %d, %-I:%M %p}<br><b>%{y}</b><extra></extra>",
  )
  yaxis_dict = dict(
      showgrid=True,
      gridcolor="rgba(240,246,252,0.06)",
      color="#8b949e",
      zeroline=False,
  )
  if y_range:
    yaxis_dict["range"] = y_range

  layout_args = dict(
      paper_bgcolor="rgba(0,0,0,0)",
      plot_bgcolor="rgba(0,0,0,0)",
      font=dict(color="#8b949e", family="-apple-system, sans-serif"),
      hovermode="x unified",
      margin=dict(l=10, r=10, t=35, b=10),
      xaxis=dict(
          showgrid=False,
          tickformat=tick_format,
          color="#8b949e",
          linecolor="rgba(240,246,252,0.1)",
      ),
      yaxis=yaxis_dict,
  )
  if day_x_range:
    layout_args["xaxis_range"] = day_x_range
  fig.update_layout(**layout_args)
  return fig


def render_station_charts(df, station_name, tab_name, metar_station="KSQL"):
  if df.empty:
    st.info(f"No logged data available for {station_name} in this timeframe.")
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
      fig_temp = render_styled_chart(
          clean_temp_df,
          temp_col,
          f"{station_name} • Temperature Trend",
          "#f97316",
          "rgba(249, 115, 22, 0.08)",
          tick_format,
          day_x_range,
      )
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
      fig_wind = render_styled_chart(
          clean_wind_df,
          wind_col,
          f"{station_name} • Wind Speed Trend",
          "#06b6d4",
          "rgba(6, 182, 212, 0.08)",
          tick_format,
          day_x_range,
      )
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
      fig_hum = render_styled_chart(
          clean_hum_df,
          hum_col,
          f"{station_name} • Relative Humidity Trend",
          "#3b82f6",
          "rgba(59, 130, 246, 0.08)",
          tick_format,
          day_x_range,
      )
      st.plotly_chart(
          fig_hum,
          use_container_width=True,
          config=chart_config,
          key=f"{prefix}_hum_chart",
      )

  render_metar_dropdown(metar_station)

  # Barometric Pressure Chart (Soft Purple)
  # Barometric Pressure Chart (Soft Purple)
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
        # Custom zoom range for DIY BME280 pressure (985 to 1000 hPa)
        press_y_range = (
            [985.0, 1000.0] if "diy" in station_name.lower() else None
        )

        fig_press = render_styled_chart(
            clean_press_df,
            press_col,
            f"{station_name} • Barometric Pressure Trend",
            "#a855f7",
            "rgba(168, 85, 247, 0.08)",
            tick_format,
            day_x_range,
            y_range=press_y_range,
        )
        st.plotly_chart(
            fig_press,
            use_container_width=True,
            config=chart_config,
            key=f"{prefix}_press_chart",
        )


# ---------------- DASHBOARD UI ----------------
@st.fragment(run_every="180s")
def render_dashboard():
  top_l, top_r = st.columns([2, 1])
  with top_l:
    st.markdown(
        "<h2 style='margin-bottom:0.2rem;'>🌦️ Weather Command Center</h2>",
        unsafe_allow_html=True,
    )
    st.caption("Real-Time Multi-Station Microclimate & Fog Prediction")
  with top_r:
    station_view = st.radio(
        "Station View",
        ["⚡ La Crosse & Tempest", "🛠️ DIY BME280 Station"],
        horizontal=True,
        label_visibility="collapsed",
    )

  df_lacrosse = load_sheet_data(LACROSSE_SHEET_ID)
  df_tempest = load_sheet_data(TEMPEST_SHEET_ID)
  df_diy = load_sheet_data(DIY_SHEET_ID)

  m_tempest = extract_station_metrics(df_tempest, "tempest")

  # ---------------- OBSERVATION CARDS ----------------
  if station_view == "⚡ La Crosse & Tempest":
    col_lacrosse, col_tempest = st.columns(2)

    with col_lacrosse:
      with st.container(border=True):
        m_lacrosse = extract_station_metrics(df_lacrosse, "lacrosse")
        status_tag = (
            get_live_badge(m_lacrosse["raw_ts"]) if m_lacrosse else "⚪ Offline"
        )
        st.markdown(
            f"<div style='display:flex; justify-content:space-between;"
            " align-items:center;'><b>🏡 La Crosse"
            f" Station</b>{status_tag}</div>",
            unsafe_allow_html=True,
        )
        if m_lacrosse:
          m1, m2, m3 = st.columns(3)
          m1.metric("Temperature", m_lacrosse["temp_str"])
          m2.metric("Wind Speed", m_lacrosse["wind_str"])
          m3.metric("Humidity", m_lacrosse["hum_str"])
          st.caption(f"Latest observation: {m_lacrosse['time_str']}")
        else:
          st.warning("No live data available for La Crosse.")

    with col_tempest:
      with st.container(border=True):
        status_tag = (
            get_live_badge(m_tempest["raw_ts"]) if m_tempest else "⚪ Offline"
        )
        st.markdown(
            f"<div style='display:flex; justify-content:space-between;"
            " align-items:center;'><b>⚡ Tempest"
            f" Station</b>{status_tag}</div>",
            unsafe_allow_html=True,
        )
        if m_tempest:
          m1, m2, m3 = st.columns(3)
          m1.metric("Temperature", m_tempest["temp_str"])
          m2.metric("Wind Speed", m_tempest["wind_str"])
          m3.metric("Humidity", m_tempest["hum_str"])
          st.caption(f"Latest observation: {m_tempest['time_str']}")
        else:
          st.warning("No live data available for Tempest.")

  else:
    with st.container(border=True):
      m_diy = extract_station_metrics(df_diy, "diy")
      status_tag = get_live_badge(m_diy["raw_ts"]) if m_diy else "⚪ Offline"
      st.markdown(
          f"<div style='display:flex; justify-content:space-between;"
          f" align-items:center;'><b>🛠️ DIY BME280 Station</b>{status_tag}</div>",
          unsafe_allow_html=True,
      )
      if m_diy:
        m1, m2, m3 = st.columns(3)
        m1.metric("Temperature", m_diy["temp_str"])
        m2.metric("Humidity", m_diy["hum_str"])
        m3.metric("Barometric Pressure", m_diy["press_str"])
        st.caption(f"Latest observation: {m_diy['time_str']}")
      else:
        st.warning("No live data available for DIY Station.")

  # ---------------- 1. CURRENT TEMPEST FOG RISK & DEW POINT (BEFORE HIGH SYSTEM) ----------------
  if m_tempest and m_tempest["raw_t"] is not None and m_tempest["raw_h"] is not None:
    live_risk, live_dew, live_spread = calculate_current_fog_risk(
        m_tempest["raw_t"], m_tempest["raw_h"]
    )

    with st.container(border=True):
      st.markdown(
          "<div style='display:flex; justify-content:space-between;"
          " align-items:center; margin-bottom:0.75rem;'><span"
          " style='font-weight:700; font-size:1.05rem;'>🌫️ Current Fog Risk &"
          " Dew Point — *Tempest*</span>"
          f" {get_fog_badge(live_risk)}</div>",
          unsafe_allow_html=True,
      )

      col_f1, col_f2, col_f3 = st.columns(3)
      col_f1.metric("Current Fog Risk", live_risk)
      col_f2.metric(
          "Current Dew Point",
          f"{live_dew}°F" if live_dew is not None else "N/A",
          delta=f"{live_spread}°F spread" if live_spread is not None else None,
          delta_color="inverse",
      )
      col_f3.metric("Current Humidity", m_tempest["hum_str"])

      st.caption(
          f"Live calculation via Tempest: temperature is {live_spread}°F above"
          " saturation. (Spreads ≤ 3°F indicate imminent fog formation)."
      )

  # ---------------- 2. SYNOPTIC PACIFIC HIGH INVERSION STATUS (AFTER / BEHIND) ----------------
  target_press_df = (
      df_diy
      if not df_diy.empty
      else (df_tempest if not df_tempest.empty else df_lacrosse)
  )
  high_p_info = detect_pacific_high_pressure(target_press_df)

  with st.container(border=True):
    h1, h2 = st.columns([1, 2])
    with h1:
      st.metric(
          "Pacific High System",
          high_p_info["status"],
          delta=f"{high_p_info['trend_3h']:+} hPa (3hr trend)",
      )
    with h2:
      st.markdown(
          "<span style='font-size:0.8rem; color:#8b949e;"
          " text-transform:uppercase; font-weight:600;'>Synoptic Inversion"
          " State</span>",
          unsafe_allow_html=True,
      )
      st.write(high_p_info["detail"])

  # ---------------- 3. DYNAMIC FORECAST CARD WITH TIME HORIZON SWITCHER ----------------
  if not df_tempest.empty:
    with st.container(border=True):
      fc_header_l, fc_header_r = st.columns([2, 1])
      with fc_header_l:
        st.markdown(
            "<span style='font-weight:700; font-size:1.05rem;'>🔮 Historical"
            " Pattern Forecast</span>",
            unsafe_allow_html=True,
        )
      with fc_header_r:
        forecast_horizon_str = st.radio(
            "Forecast Horizon",
            ["+1 hr", "+3 hrs", "+5 hrs", "+24 hrs"],
            index=1,
            horizontal=True,
            label_visibility="collapsed",
            key="forecast_horizon_picker",
        )

      horizon_map = {"+1 hr": 1, "+3 hrs": 3, "+5 hrs": 5, "+24 hrs": 24}
      selected_hours = horizon_map.get(forecast_horizon_str, 3)

      forecast, err = analog_forecast(df_tempest, hours_ahead=selected_hours)
      if forecast:
        st.markdown(
            f"<div style='display:flex; justify-content:space-between;"
            " align-items:center; margin-bottom:0.75rem;'><span"
            " style='color:#8b949e; font-size:0.85rem;'>Tempest Pattern"
            f" Projection ({forecast_horizon_str})</span>"
            f" {get_fog_badge(forecast['fog_risk'])}</div>",
            unsafe_allow_html=True,
        )
        fc1, fc2, fc3, fc4 = st.columns(4)
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
        fc4.metric("Pattern Matches", f"{forecast['matches']} days")

        st.caption(
            f"Calculated via Tempest historical analogs ({forecast_horizon_str}"
            f" ahead). Closest pattern matched: **{forecast['closest_date']}**."
        )
      elif err:
        st.caption(f"Forecast model info: {err}")

  # ---------------- TIMEFRAME SELECTOR & CHARTS ----------------
  timeframe = st.radio(
      "Select Timeframe",
      ["Daily", "Weekly", "Monthly", "All Time"],
      horizontal=True,
      label_visibility="collapsed",
  )

  duration_key = timeframe.lower()

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