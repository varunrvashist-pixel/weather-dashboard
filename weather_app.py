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
import plotly.graph_objects as go
import requests
import streamlit as st

st.set_page_config(
    page_title="Weather Station Command Center",
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
    
    /* Top Navigation Button Styling */
    div.stButton > button {
        border-radius: 8px !important;
        border: 1px solid rgba(240, 246, 252, 0.2) !important;
        background-color: rgba(22, 27, 34, 0.8) !important;
        color: #e6edf3 !important;
        font-weight: 600 !important;
        padding: 0.5rem 0.75rem !important;
        font-size: 0.88rem !important;
        transition: all 0.2s ease-in-out;
    }
    div.stButton > button:hover {
        border-color: #ff4b4b !important;
        color: #ff4b4b !important;
    }

    /* Active / Selected Tab Style (Red) */
    div.stButton > button[kind="primary"] {
        background-color: #da3633 !important;
        border-color: #f85149 !important;
        color: #ffffff !important;
        box-shadow: 0 0 10px rgba(218, 54, 51, 0.4) !important;
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
METAR_SHEET_ID = "1ZMwL4PO-vWTFPPk829Z6NCm5853zbh_ykmMn6mhBZaQ"


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
        .replace("in", "")
        .strip()
    )
    return float(c) if c else None
  except Exception:
    return None


def compute_dew_point(temp_raw, hum_raw):
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


def compute_relative_humidity(temp_c, dewp_c):
  """Calculates Relative Humidity (%) from Celsius temp and dew point via Magnus formula."""
  try:
    if temp_c is None or dewp_c is None:
      return None
    a, b = 17.27, 237.7
    rh = 100.0 * (
        np.exp((a * dewp_c) / (b + dewp_c))
        / np.exp((a * temp_c) / (b + temp_c))
    )
    return round(float(np.clip(rh, 0.0, 100.0)), 1)
  except Exception:
    return None


def normalize_fog_signals(df_diy, df_metar, prox_min=10.0, prox_max=500.0):
  """Maps BME280 proximity readings and METAR visibility/fog onto a 0.0 (Clear) to 1.0 (Dense Fog) scale."""
  diy_out = df_diy.copy()
  metar_out = df_metar.copy()

  if not metar_out.empty:
    fog_col = find_col(metar_out, ["Fog", "Fog Index"])
    vis_col = find_col(metar_out, ["Visibility", "visib"])
    if fog_col:
      metar_out["Fog Index"] = pd.to_numeric(
          metar_out[fog_col], errors="coerce"
      ).clip(0.0, 1.0)
    elif vis_col:
      vis = pd.to_numeric(metar_out[vis_col], errors="coerce").clip(0.0, 10.0)
      metar_out["Fog Index"] = (1.0 - (vis / 10.0)).round(2)

  if not diy_out.empty:
    prox_col = find_col(
        diy_out, ["Proximity", "Prox", "Fog", "proximity", "IR Proximity"]
    )
    if prox_col:
      prox = pd.to_numeric(diy_out[prox_col], errors="coerce")
      scaled = (prox - prox_min) / (prox_max - prox_min)
      diy_out["Fog Index"] = scaled.clip(0.0, 1.0).round(2)

  return diy_out, metar_out


def classify_fog_label(val):
  """Maps a 0.0-1.0 Fog Index to 6 human-readable hover levels."""
  if val is None or pd.isna(val):
    return "N/A"
  if val < 0.10:
    return f"Clear ({val:.2f})"
  elif val < 0.25:
    return f"Trace Mist ({val:.2f})"
  elif val < 0.45:
    return f"Light Fog ({val:.2f})"
  elif val < 0.65:
    return f"Moderate Fog ({val:.2f})"
  elif val < 0.85:
    return f"Thick Fog ({val:.2f})"
  else:
    return f"Dense Fog ({val:.2f})"


def get_live_badge(latest_ts, max_live_mins=10):
  if pd.isna(latest_ts):
    return '<span style="color:#8b949e; font-size:0.75rem; font-weight:600;">⚪ OFFLINE</span>'

  now = datetime.now(ZoneInfo("America/Los_Angeles")).replace(tzinfo=None)
  raw_time = pd.to_datetime(latest_ts).replace(tzinfo=None)

  diff_min = max(0, int((now - raw_time).total_seconds() / 60))
  if diff_min <= max_live_mins:
    return '<span style="background:rgba(46,160,67,0.15); color:#3fb950; border:1px solid rgba(46,160,67,0.4); padding:2px 8px; border-radius:12px; font-size:0.72rem; font-weight:600;">🟢 LIVE</span>'
  elif diff_min <= 90:
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


def detect_pacific_high_pressure(df_history):
  if df_history.empty:
    return {"status": "Neutral"}

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

      if curr >= 1018.0 and trend >= 0.0:
        return {"status": "Strong High (Inversion Lid)"}
      elif curr >= 1014.0 and trend >= -0.5:
        return {"status": "Moderate High"}
  return {"status": "Neutral"}


# ---------------- GOOGLE SHEETS & METAR INGESTION ----------------
def get_google_sheets_client():
  if os.path.exists("cloud_key.json"):
    creds = Credentials.from_service_account_file("cloud_key.json", scopes=SCOPES)
    return gspread.authorize(creds)

  try:
    if "GCP_KEY_BASE64" in st.secrets:
      key_json = base64.b64decode(st.secrets["GCP_KEY_BASE64"]).decode("utf-8")
      creds_info = json.loads(key_json)
      creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
      return gspread.authorize(creds)
  except Exception:
    pass

  st.error("Missing GCP credentials.")
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


@st.cache_data(ttl=300)
def fetch_live_metar_banner(icao="KSQL"):
  """Fetches the latest raw METAR string & flight category for the live banner."""
  try:
    url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data:
      return None
    obs = data[0]
    return {
        "flt_cat": obs.get("fltCat", "N/A"),
        "raw_ob": obs.get("rawOb", "N/A"),
    }
  except Exception:
    return None


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


def extract_station_metrics(df):
  """Extracts core current metrics without falling back to wrong columns."""
  if df.empty:
    return None

  latest = df.iloc[-1]

  t_col = find_col(
      df,
      [
          "Temperature",
          "Temp",
          "Temp (F)",
          "temp_f",
          "Outdoor Temp",
          "Air Temp",
          "air_temperature",
      ],
  )
  h_col = find_col(
      df,
      [
          "Humidity",
          "Humidity (%)",
          "Relative Humidity",
          "hum",
          "Outdoor Humidity",
          "relative_humidity",
      ],
  )
  w_col = find_col(
      df,
      [
          "Wind Speed",
          "Wind Speed (mph)",
          "Wind",
          "Wind_Speed",
          "wind_mph",
          "Wind (mph)",
          "wind_avg",
          "WindSpeed",
      ],
  )
  r_col = find_col(df, ["Rain", "Rainfall", "Precipitation", "Rain (in)"])
  p_col = find_col(
      df, ["Pressure", "Pressure (hPa)", "Barometric Pressure", "press"]
  )
  v_col = find_col(df, ["Visibility", "visib"])
  f_col = find_col(df, ["Fog", "Fog Index"])

  val_t = clean_float(latest[t_col]) if t_col else None
  val_h = clean_float(latest[h_col]) if h_col else None
  val_w = clean_float(latest[w_col]) if w_col else None
  val_r = clean_float(latest[r_col]) if r_col else None
  val_p = clean_float(latest[p_col]) if p_col else None
  val_v = clean_float(latest[v_col]) if v_col else None
  val_f = clean_float(latest[f_col]) if f_col else None

  return {
      "temp_str": f"{val_t} °F" if val_t is not None else "N/A",
      "hum_str": f"{val_h} %" if val_h is not None else "N/A",
      "wind_str": f"{val_w} mph" if val_w is not None else "TBD",
      "rain_str": f"{val_r} in" if val_r is not None else "0.0 in",
      "press_str": f"{val_p} hPa" if val_p is not None else "N/A",
      "vis_str": f"{val_v} mi" if val_v is not None else "N/A",
      "fog_val": val_f,
      "raw_ts": latest["Timestamp"],
      "time_str": format_display_time(latest["Timestamp"]),
  }


# ---------------- DYNAMIC HORIZON FORECAST ENGINE (BME280 DRIVEN) ----------------
def analog_forecast(df, hours_ahead=2):
  min_records = 30 if hours_ahead < 24 else 60
  if len(df) < min_records:
    return (
        None,
        f"Requires more logged BME280 observations to project +{hours_ahead}"
        " hours.",
    )

  df_clean = df.sort_values("Timestamp").copy()
  current = df_clean.iloc[-1]
  curr_time = current["Timestamp"]

  t_col = find_col(df_clean, ["Temperature", "Temp", "Temp (F)", "temp_f"])
  h_col = find_col(df_clean, ["Humidity", "Humidity (%)", "Relative Humidity"])
  w_col = find_col(
      df_clean, ["Wind Speed", "Wind Speed (mph)", "Wind", "wind_mph"]
  )
  r_col = find_col(df_clean, ["Rain", "Rainfall", "Precipitation"])

  curr_t = clean_float(current[t_col] if t_col else None)
  curr_h = clean_float(current[h_col] if h_col else None)

  if curr_t is None or curr_h is None:
    return None, "Current BME280 temperature or humidity could not be parsed."

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

  candidates["num_t"] = candidates[t_col].apply(clean_float)
  candidates["num_h"] = candidates[h_col].apply(clean_float)
  candidates = candidates.dropna(subset=["num_t", "num_h"])

  if candidates.empty:
    return None, "Historical records lack usable numeric observations."

  candidates["diff"] = np.sqrt(
      (candidates["num_t"] - curr_t) ** 2 + (candidates["num_h"] - curr_h) ** 2
  )
  best_matches = candidates.nsmallest(5, "diff")

  future_temps, future_hums, future_rains = [], [], []
  window_margin = (
      pd.Timedelta(minutes=30) if hours_ahead < 24 else pd.Timedelta(hours=1)
  )

  for _, match_row in best_matches.iterrows():
    match_time = match_row["Timestamp"]
    target_future = match_time + pd.Timedelta(hours=hours_ahead)

    future_window = df_clean[
        (df_clean["Timestamp"] >= target_future - window_margin)
        & (df_clean["Timestamp"] <= target_future + window_margin)
    ]
    if not future_window.empty:
      val_t = clean_float(future_window.iloc[0].get(t_col))
      val_h = clean_float(future_window.iloc[0].get(h_col))
      if val_t is not None:
        future_temps.append(val_t)
      if val_h is not None:
        future_hums.append(val_h)

      if r_col:
        val_r = clean_float(future_window.iloc[0].get(r_col))
        if val_r is not None:
          future_rains.append(val_r)

  pred_wind_str = "TBD"
  if w_col:
    best_match_time = best_matches.iloc[0]["Timestamp"]
    end_time = best_match_time + pd.Timedelta(hours=hours_ahead)

    continuous_wind_window = df_clean[
        (df_clean["Timestamp"] >= best_match_time)
        & (df_clean["Timestamp"] <= end_time)
    ]

    if not continuous_wind_window.empty:
      wind_series = continuous_wind_window[w_col].apply(clean_float).dropna()
      if not wind_series.empty:
        avg_wind = wind_series.mean()
        gusts = wind_series[wind_series > avg_wind]

        if gusts.empty:
          pred_wind_str = f"{round(avg_wind, 1)} mph"
        else:
          pred_wind_str = f"{round(gusts.min(), 1)} - {round(gusts.max(), 1)} mph"

  if not future_temps:
    return (
        None,
        f"Found matches, but subsequent +{hours_ahead}h records were not"
        " logged.",
    )

  pred_t = round(float(np.mean(future_temps)), 1)
  pred_h = round(float(np.mean(future_hums)), 1)
  pred_dew = compute_dew_point(pred_t, pred_h)

  pred_r = round(float(np.mean(future_rains)), 2) if future_rains else 0.0
  delta_t = round(pred_t - curr_t, 1)

  pred_spread = pred_t - pred_dew if pred_dew is not None else 99.0
  if pred_h >= 88 or pred_spread <= 2.5:
    fog_flag = "High"
  elif pred_h >= 80 or pred_spread <= 4.0:
    fog_flag = "Moderate"
  else:
    fog_flag = "Low"

  high_p_info = detect_pacific_high_pressure(df_clean)
  if (
      high_p_info["status"] == "Strong High (Inversion Lid)"
      and fog_flag == "Moderate"
  ):
    fog_flag = "High"

  return {
      "pred_temp": pred_t,
      "pred_wind": pred_wind_str,
      "pred_rain": pred_r,
      "delta_temp": delta_t,
      "fog_risk": fog_flag,
      "matches": len(future_temps),
      "closest_date": best_matches.iloc[0]["Timestamp"].strftime("%b %d, %Y"),
  }, None


# ---------------- SINGLE-STATION CHARTS ----------------
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
  show_markers = len(clean_df) <= 15
  fig = px.line(
      clean_df,
      x="Timestamp",
      y=col_name,
      title=title,
      markers=show_markers,
  )
  fig.update_traces(
      line=dict(color=color_hex, width=2.5),
      marker=dict(size=7, color=color_hex),
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


def render_station_charts(
    df,
    tab_name,
    include_wind=False,
    include_rain=True,
    custom_press_range=None,
):
  if df.empty:
    st.info("No logged data available in this timeframe.")
    return

  plot_df = df.copy()
  day_x_range = None
  tick_format = "%b %d, %-I:%M %p"

  if tab_name == "daily" and not plot_df.empty:
    day_start = plot_df["Timestamp"].max().normalize()
    day_end = day_start + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    day_x_range = [day_start, day_end]
    tick_format = "%-I:%M %p"

  temp_col = find_col(plot_df, ["Temperature", "Temp", "Temp (F)", "temp_f"])
  hum_col = find_col(plot_df, ["Humidity (%)", "Humidity", "Relative Humidity"])
  wind_col = find_col(
      plot_df, ["Wind Speed", "Wind Speed (mph)", "Wind", "wind_mph"]
  )
  rain_col = find_col(plot_df, ["Rain", "Rainfall", "Precipitation"])
  press_col = find_col(
      plot_df, ["Pressure", "Pressure (hPa)", "Barometric Pressure"]
  )
  vis_col = find_col(plot_df, ["Visibility", "visib"])
  fog_col = find_col(plot_df, ["Fog", "Fog Index"])

  chart_config = {"responsive": True, "displayModeBar": False}

  if temp_col:
    plot_df[temp_col] = pd.to_numeric(
        plot_df[temp_col]
        .astype(str)
        .str.replace("°F", "", regex=False)
        .str.strip(),
        errors="coerce",
    )
    clean_temp = plot_df.dropna(subset=[temp_col, "Timestamp"]).sort_values(
        "Timestamp"
    )
    if not clean_temp.empty:
      st.plotly_chart(
          render_styled_chart(
              clean_temp,
              temp_col,
              "Temperature Trend",
              "#f97316",
              "rgba(249, 115, 22, 0.08)",
              tick_format,
              day_x_range,
          ),
          use_container_width=True,
          config=chart_config,
      )

  if hum_col:
    plot_df[hum_col] = pd.to_numeric(
        plot_df[hum_col]
        .astype(str)
        .str.replace("%", "", regex=False)
        .str.strip(),
        errors="coerce",
    )
    clean_hum = plot_df.dropna(subset=[hum_col, "Timestamp"]).sort_values(
        "Timestamp"
    )
    if not clean_hum.empty:
      st.plotly_chart(
          render_styled_chart(
              clean_hum,
              hum_col,
              "Relative Humidity Trend",
              "#3b82f6",
              "rgba(59, 130, 246, 0.08)",
              tick_format,
              day_x_range,
          ),
          use_container_width=True,
          config=chart_config,
      )

  if include_wind and wind_col:
    plot_df[wind_col] = pd.to_numeric(
        plot_df[wind_col]
        .astype(str)
        .str.replace("mph", "", regex=False)
        .str.strip(),
        errors="coerce",
    )
    clean_wind = plot_df.dropna(subset=[wind_col, "Timestamp"]).sort_values(
        "Timestamp"
    )
    if not clean_wind.empty:
      st.plotly_chart(
          render_styled_chart(
              clean_wind,
              wind_col,
              "Wind Speed Trend",
              "#06b6d4",
              "rgba(6, 182, 212, 0.08)",
              tick_format,
              day_x_range,
          ),
          use_container_width=True,
          config=chart_config,
      )

  if include_rain and rain_col:
    plot_df[rain_col] = pd.to_numeric(
        plot_df[rain_col]
        .astype(str)
        .str.replace("in", "", regex=False)
        .str.strip(),
        errors="coerce",
    )
    clean_rain = plot_df.dropna(subset=[rain_col, "Timestamp"]).sort_values(
        "Timestamp"
    )
    if not clean_rain.empty:
      st.plotly_chart(
          render_styled_chart(
              clean_rain,
              rain_col,
              "Rainfall Trend",
              "#60a5fa",
              "rgba(96, 165, 250, 0.08)",
              tick_format,
              day_x_range,
          ),
          use_container_width=True,
          config=chart_config,
      )

  if press_col:
    plot_df[press_col] = pd.to_numeric(
        plot_df[press_col]
        .astype(str)
        .str.replace("hPa", "", regex=False)
        .str.strip(),
        errors="coerce",
    )
    clean_press = plot_df.dropna(subset=[press_col, "Timestamp"]).sort_values(
        "Timestamp"
    )
    if not clean_press.empty:
      st.plotly_chart(
          render_styled_chart(
              clean_press,
              press_col,
              "Barometric Pressure Trend",
              "#a855f7",
              "rgba(168, 85, 247, 0.08)",
              tick_format,
              day_x_range,
              y_range=custom_press_range,
          ),
          use_container_width=True,
          config=chart_config,
      )

  if vis_col:
    plot_df[vis_col] = pd.to_numeric(plot_df[vis_col], errors="coerce")
    clean_vis = plot_df.dropna(subset=[vis_col, "Timestamp"]).sort_values(
        "Timestamp"
    )
    if not clean_vis.empty:
      st.plotly_chart(
          render_styled_chart(
              clean_vis,
              vis_col,
              "Visibility Trend (Miles)",
              "#10b981",
              "rgba(16, 185, 129, 0.08)",
              tick_format,
              day_x_range,
              y_range=[0.0, 10.5],
          ),
          use_container_width=True,
          config=chart_config,
      )

  if fog_col:
    plot_df[fog_col] = pd.to_numeric(plot_df[fog_col], errors="coerce")
    clean_fog = plot_df.dropna(subset=[fog_col, "Timestamp"]).sort_values(
        "Timestamp"
    )
    if not clean_fog.empty:
      st.plotly_chart(
          render_styled_chart(
              clean_fog,
              fog_col,
              "Normalized Fog Index (0 = Clear, 1 = Dense Fog)",
              "#f43f5e",
              "rgba(244, 63, 94, 0.08)",
              tick_format,
              day_x_range,
              y_range=[0.0, 1.05],
          ),
          use_container_width=True,
          config=chart_config,
      )


# ---------------- COMBINED MULTI-LINE CHARTS ----------------
def render_combined_chart(
    df_dict,
    col_options,
    title,
    y_suffix,
    tick_format,
    day_x_range,
    y_range=None,
):
  fig = go.Figure()
  colors = {
      "Tempest": "#06b6d4",
      "DIY BME280": "#f97316",
      "METAR (KSQL)": "#10b981",
  }
  has_data = False

  for station_name, df in df_dict.items():
    if df.empty:
      continue
    col = find_col(df, col_options)
    if col:
      plot_df = df.copy()
      plot_df[col] = (
          plot_df[col].astype(str).str.replace(r"[^\d.-]", "", regex=True)
      )
      plot_df[col] = pd.to_numeric(plot_df[col], errors="coerce")
      clean_df = plot_df.dropna(subset=[col, "Timestamp"]).sort_values(
          "Timestamp"
      )

      if not clean_df.empty:
        has_data = True
        trace_mode = "lines+markers" if len(clean_df) <= 15 else "lines"
        fig.add_trace(
            go.Scatter(
                x=clean_df["Timestamp"],
                y=clean_df[col],
                mode=trace_mode,
                name=station_name,
                line=dict(color=colors.get(station_name, "#ffffff"), width=2.5),
                marker=dict(size=7, color=colors.get(station_name, "#ffffff")),
                hovertemplate=(
                    f"%{{x|%b %d, %-I:%M %p}}<br><b>%{{y}} {y_suffix}</b>"
                    f"<extra>{station_name}</extra>"
                ),
            )
        )

  if not has_data:
    return None

  yaxis_dict = dict(
      showgrid=True,
      gridcolor="rgba(240,246,252,0.06)",
      color="#8b949e",
      zeroline=False,
  )
  if y_range:
    yaxis_dict["range"] = y_range

  layout_args = dict(
      title=title,
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
      legend=dict(
          orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
      ),
  )
  if day_x_range:
    layout_args["xaxis_range"] = day_x_range

  fig.update_layout(**layout_args)
  return fig


# ---------------- MAIN APP NAVIGATION BUTTONS ----------------
if "current_page" not in st.session_state:
  st.session_state.current_page = "DIY_BME280"

nav_cols = st.columns(5)

tabs = [
    ("🛠️ BME280", "DIY_BME280"),
    ("⚡ Tempest", "TEMPEST"),
    ("🏡 La Crosse", "LACROSSE"),
    ("✈️ METAR", "METAR"),
    ("📊 Station Comparison", "COMPARISON"),
]

for col, (label, page_key) in zip(nav_cols, tabs):
  with col:
    is_active = st.session_state.current_page == page_key
    if st.button(
        label,
        type="primary" if is_active else "secondary",
        use_container_width=True,
    ):
      st.session_state.current_page = page_key
      st.rerun()

st.markdown(
    "<hr style='margin-top: 0.5rem; margin-bottom: 1.5rem;'>",
    unsafe_allow_html=True,
)


# ---------------- RENDER SELECTED DASHBOARD ----------------
@st.fragment(run_every="180s")
def render_dashboard(selected_page):

  if selected_page == "DIY_BME280":
    st.markdown(
        "<h2 style='margin-bottom:0.2rem;'>🌦️ DIY Weather Command Center</h2>",
        unsafe_allow_html=True,
    )
    st.caption("Real-Time Microclimate Monitoring & Fog Prediction via BME280")

    df_diy = load_sheet_data(DIY_SHEET_ID)

    with st.container(border=True):
      m_diy = extract_station_metrics(df_diy)
      status_tag = get_live_badge(m_diy["raw_ts"]) if m_diy else "⚪ OFFLINE"

      st.markdown(
          "<div style='display:flex; justify-content:space-between;"
          " align-items:center;'><b>🛠️ Current Conditions"
          f" (BME280)</b>{status_tag}</div>",
          unsafe_allow_html=True,
      )

      if m_diy:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Temperature", m_diy["temp_str"])
        m2.metric("Humidity", m_diy["hum_str"])
        m3.metric("Wind Speed", m_diy["wind_str"])
        m4.metric("Rain", m_diy["rain_str"])
        st.caption(f"Latest observation: {m_diy['time_str']}")
      else:
        st.warning("No live data available for DIY Station.")

    if not df_diy.empty:
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
              key="fc_picker",
          )

        horizon_map = {"+1 hr": 1, "+3 hrs": 3, "+5 hrs": 5, "+24 hrs": 24}
        forecast, err = analog_forecast(
            df_diy, hours_ahead=horizon_map.get(forecast_horizon_str, 3)
        )

        if forecast:
          st.markdown(
              "<div style='display:flex; justify-content:space-between;"
              " align-items:center; margin-bottom:0.75rem;'><span"
              " style='color:#8b949e; font-size:0.85rem;'>BME280 Pattern"
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
          fc2.metric("Fog Propensity", forecast["fog_risk"])
          fc3.metric("Projected Wind", forecast["pred_wind"])
          fc4.metric("Projected Rain", f"{forecast['pred_rain']} in")
          st.caption(
              "Calculated via BME280 historical analogs"
              f" ({forecast_horizon_str} ahead). Closest pattern matched:"
              f" **{forecast['closest_date']}**."
          )
        elif err:
          st.caption(f"Forecast model info: {err}")

    timeframe = st.radio(
        "Select Timeframe",
        ["Daily", "Weekly", "Monthly", "All Time"],
        horizontal=True,
        label_visibility="collapsed",
        key="diy_tf",
    )
    render_station_charts(
        filter_by_duration(df_diy, timeframe.lower()),
        timeframe.lower(),
        include_wind=False,
        custom_press_range=[985.0, 1005.0],
    )

  elif selected_page == "TEMPEST":
    st.markdown(
        "<h2 style='margin-bottom:0.2rem;'>⚡ Tempest Weather Station</h2>",
        unsafe_allow_html=True,
    )
    st.caption("Real-Time Telemetry via WeatherFlow Tempest")

    df_tempest = load_sheet_data(TEMPEST_SHEET_ID)

    with st.container(border=True):
      m_tempest = extract_station_metrics(df_tempest)
      status_tag = (
          get_live_badge(m_tempest["raw_ts"]) if m_tempest else "⚪ OFFLINE"
      )
      st.markdown(
          "<div style='display:flex; justify-content:space-between;"
          f" align-items:center;'><b>⚡ Current Conditions</b>{status_tag}</div>",
          unsafe_allow_html=True,
      )
      if m_tempest:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Temperature", m_tempest["temp_str"])
        m2.metric("Wind Speed", m_tempest["wind_str"])
        m3.metric("Humidity", m_tempest["hum_str"])
        m4.metric("Pressure", m_tempest["press_str"])
        st.caption(f"Latest observation: {m_tempest['time_str']}")
      else:
        st.warning("No live data available for Tempest.")

    timeframe = st.radio(
        "Select Timeframe",
        ["Daily", "Weekly", "Monthly", "All Time"],
        horizontal=True,
        label_visibility="collapsed",
        key="tempest_tf",
    )
    render_station_charts(
        filter_by_duration(df_tempest, timeframe.lower()),
        timeframe.lower(),
        include_wind=True,
    )

  elif selected_page == "LACROSSE":
    st.markdown(
        "<h2 style='margin-bottom:0.2rem;'>🏡 La Crosse Weather Station</h2>",
        unsafe_allow_html=True,
    )
    st.caption("Local Ambient Telemetry via La Crosse Sensors")

    df_lacrosse = load_sheet_data(LACROSSE_SHEET_ID)

    with st.container(border=True):
      m_lacrosse = extract_station_metrics(df_lacrosse)
      status_tag = (
          get_live_badge(m_lacrosse["raw_ts"]) if m_lacrosse else "⚪ OFFLINE"
      )
      st.markdown(
          "<div style='display:flex; justify-content:space-between;"
          f" align-items:center;'><b>🏡 Current Conditions</b>{status_tag}</div>",
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

    timeframe = st.radio(
        "Select Timeframe",
        ["Daily", "Weekly", "Monthly", "All Time"],
        horizontal=True,
        label_visibility="collapsed",
        key="lacrosse_tf",
    )
    render_station_charts(
        filter_by_duration(df_lacrosse, timeframe.lower()),
        timeframe.lower(),
        include_wind=True,
    )

  elif selected_page == "METAR":
    st.markdown(
        "<h2 style='margin-bottom:0.2rem;'>✈️ Aviation METAR Feed (KSQL)</h2>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Historical San Carlos Airport Log & Visibility-Based Fog Tracking"
    )

    df_metar = load_sheet_data(METAR_SHEET_ID)
    live_banner = fetch_live_metar_banner("KSQL")

    with st.container(border=True):
      m_metar = extract_station_metrics(df_metar)
      status_tag = (
          get_live_badge(m_metar["raw_ts"], max_live_mins=65)
          if m_metar
          else "⚪ OFFLINE"
      )

      fog_val = m_metar["fog_val"] if m_metar else None
      if fog_val is not None:
        fog_risk = (
            "High"
            if fog_val >= 0.7
            else ("Moderate" if fog_val >= 0.3 else "Low")
        )
      else:
        fog_risk = "N/A"

      cat_badge = ""
      if live_banner and live_banner.get("flt_cat"):
        flt_cat = live_banner["flt_cat"]
        cat_color = (
            "#3fb950"
            if flt_cat == "VFR"
            else ("#d29922" if flt_cat == "MVFR" else "#f85149")
        )
        cat_badge = (
            f"<span style='background:rgba(240,246,252,0.08); color:{cat_color};"
            f" border:1px solid {cat_color}; padding:2px 10px;"
            " border-radius:12px; font-size:0.75rem;"
            f" font-weight:700; margin-left:6px;'>{flt_cat}</span>"
        )

      st.markdown(
          "<div style='display:flex; justify-content:space-between;"
          " align-items:center;'><b>🛫 KSQL Surface"
          f" Report</b><div>{get_fog_badge(fog_risk)} {cat_badge}"
          f" {status_tag}</div></div>",
          unsafe_allow_html=True,
      )

      if m_metar:
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Temperature", m_metar["temp_str"])
        m2.metric("Humidity", m_metar["hum_str"])
        m3.metric("Wind Speed", m_metar["wind_str"])
        m4.metric("Pressure", m_metar["press_str"])
        m5.metric("Visibility", m_metar["vis_str"])

        if live_banner and live_banner.get("raw_ob"):
          st.markdown(
              "<div style='margin-top:0.8rem; padding:0.65rem;"
              " background:rgba(13,17,23,0.8); border-radius:8px;"
              " font-family:monospace; font-size:0.85rem;"
              f" color:#79c0ff;'>{live_banner['raw_ob']}</div>",
              unsafe_allow_html=True,
          )
        st.caption(
            f"Latest logged observation: {m_metar['time_str']} • Normalized Fog"
            f" Index: {fog_val if fog_val is not None else 'N/A'}"
        )
      else:
        st.warning("No logged data available in the METAR Google Sheet yet.")

    timeframe = st.radio(
        "Select Timeframe",
        ["Daily", "Weekly", "Monthly", "All Time"],
        horizontal=True,
        label_visibility="collapsed",
        key="metar_tf",
    )
    render_station_charts(
        filter_by_duration(df_metar, timeframe.lower()),
        timeframe.lower(),
        include_wind=True,
        include_rain=False,
        custom_press_range=[1005.0, 1030.0],
    )

  else:
    # ---------------- STATION COMPARISON PAGE ----------------
    comp_header_l, comp_header_r = st.columns([2, 1])
    with comp_header_l:
      st.markdown(
          "<h2 style='margin-bottom:0.2rem;'>📊 Station Comparison</h2>",
          unsafe_allow_html=True,
      )
      st.caption(
          "Validate DIY BME280 Sensors Against Tempest & KSQL METAR"
      )
    with comp_header_r:
      comp_station_choice = st.radio(
          "Compare With",
          ["Tempest", "METAR (KSQL)"],
          index=0,
          horizontal=True,
          label_visibility="collapsed",
          key="comp_station_picker",
      )

    df_diy = load_sheet_data(DIY_SHEET_ID)
    df_tempest = load_sheet_data(TEMPEST_SHEET_ID)
    df_metar = load_sheet_data(METAR_SHEET_ID)

    # Normalize proximity sensor & METAR visibility to 0.0 - 1.0 Fog Index
    df_diy_norm, df_metar_norm = normalize_fog_signals(df_diy, df_metar)

    if comp_station_choice == "Tempest":
      with st.container(border=True):
        m_tempest = extract_station_metrics(df_tempest)
        status_tag = (
            get_live_badge(m_tempest["raw_ts"]) if m_tempest else "⚪ OFFLINE"
        )
        st.markdown(
            "<div style='display:flex; justify-content:space-between;"
            f" align-items:center;'><b>⚡ Tempest Station</b>{status_tag}</div>",
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
        m_metar = extract_station_metrics(df_metar_norm)
        status_tag = (
            get_live_badge(m_metar["raw_ts"], max_live_mins=65)
            if m_metar
            else "⚪ OFFLINE"
        )
        st.markdown(
            "<div style='display:flex; justify-content:space-between;"
            f" align-items:center;'><b>🛫 KSQL METAR Station</b>{status_tag}</div>",
            unsafe_allow_html=True,
        )
        if m_metar:
          m1, m2, m3, m4 = st.columns(4)
          m1.metric("Temperature", m_metar["temp_str"])
          m2.metric("Humidity", m_metar["hum_str"])
          m3.metric("Visibility", m_metar["vis_str"])
          m4.metric(
              "Fog Index",
              f"{m_metar['fog_val']}"
              if m_metar["fog_val"] is not None
              else "N/A",
          )
          st.caption(f"Latest observation: {m_metar['time_str']}")
        else:
          st.warning("No live data available for KSQL METAR.")

    st.markdown("<br>", unsafe_allow_html=True)
    timeframe = st.radio(
        "Select Timeframe",
        ["Daily", "Weekly", "Monthly", "All Time"],
        horizontal=True,
        label_visibility="collapsed",
        key="comp_tf",
    )
    duration_key = timeframe.lower()

    if comp_station_choice == "Tempest":
      df_dict_all = {
          "Tempest": filter_by_duration(df_tempest, duration_key),
          "DIY BME280": filter_by_duration(df_diy_norm, duration_key),
      }
    else:
      df_dict_all = {
          "METAR (KSQL)": filter_by_duration(df_metar_norm, duration_key),
          "DIY BME280": filter_by_duration(df_diy_norm, duration_key),
      }

    day_x_range = None
    tick_format = "%b %d, %-I:%M %p"
    if duration_key == "daily":
      latest_ts = None
      for df in df_dict_all.values():
        if not df.empty:
          latest_ts = df["Timestamp"].max()
          break
      if latest_ts:
        day_start = latest_ts.normalize()
        day_end = day_start + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        day_x_range = [day_start, day_end]
        tick_format = "%-I:%M %p"

    chart_config = {"responsive": True, "displayModeBar": False}

    # If comparing against METAR, show the Normalized 0-1 Fog Index overlay right at the top
    if comp_station_choice == "METAR (KSQL)":
      fig_fog = render_combined_chart(
          df_dict_all,
          ["Fog Index", "Fog"],
          "Fog Validation: Proximity Sensor vs. KSQL METAR (0 = Clear, 1 = Dense Fog)",
          "",
          tick_format,
          day_x_range,
          y_range=[0.0, 1.05],
      )
      if fig_fog:
        st.plotly_chart(fig_fog, use_container_width=True, config=chart_config)

    fig_temp = render_combined_chart(
        df_dict_all,
        ["Temperature", "Temp", "Temp (F)", "temp_f", "Outdoor Temp", "Air Temp"],
        "Temperature Trend",
        "°F",
        tick_format,
        day_x_range,
    )
    if fig_temp:
      st.plotly_chart(fig_temp, use_container_width=True, config=chart_config)

    fig_hum = render_combined_chart(
        df_dict_all,
        ["Humidity (%)", "Humidity", "Relative Humidity", "hum", "Outdoor Humidity"],
        "Relative Humidity Trend",
        "%",
        tick_format,
        day_x_range,
    )
    if fig_hum:
      st.plotly_chart(fig_hum, use_container_width=True, config=chart_config)

    fig_wind = render_combined_chart(
        df_dict_all,
        ["Wind Speed", "Wind Speed (mph)", "Wind", "wind_mph", "Wind_Speed"],
        "Wind Speed Trend",
        "mph",
        tick_format,
        day_x_range,
    )
    if fig_wind:
      st.plotly_chart(fig_wind, use_container_width=True, config=chart_config)

    if comp_station_choice == "Tempest":
      fig_rain = render_combined_chart(
          df_dict_all,
          ["Rain", "Rainfall", "Precipitation"],
          "Rainfall Trend",
          "in",
          tick_format,
          day_x_range,
      )
      if fig_rain:
        st.plotly_chart(fig_rain, use_container_width=True, config=chart_config)


render_dashboard(st.session_state.current_page)