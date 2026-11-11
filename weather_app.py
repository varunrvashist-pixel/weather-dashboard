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
    page_title="DIY Weather Station Dashboard",
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
</style>
""",
    unsafe_allow_html=True,
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# We are focusing purely on the BME280 sheet now
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
        .replace("in", "")
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


def detect_pacific_high_pressure(df_history):
  """Calculates Pacific High inversion status quietly in the background."""
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
  """Extracts core current metrics directly for the UI."""
  if df.empty:
    return None

  latest = df.iloc[-1]

  t_col = find_col(df, ["Temperature", "Temp", "Temp (F)", "temp_f"])
  h_col = find_col(df, ["Humidity", "Humidity (%)", "Relative Humidity", "hum"])
  w_col = find_col(df, ["Wind Speed", "Wind", "Wind_Speed", "wind_mph"])
  r_col = find_col(df, ["Rain", "Rainfall", "Precipitation", "Rain (in)"])

  val_t = clean_float(latest[t_col] if t_col else None)
  val_h = clean_float(latest[h_col] if h_col else None)
  val_w = clean_float(latest[w_col] if w_col else None)
  val_r = clean_float(latest[r_col] if r_col else None)

  return {
      "temp_str": f"{val_t} °F" if val_t is not None else "N/A",
      "hum_str": f"{val_h} %" if val_h is not None else "N/A",
      "wind_str": f"{val_w} mph" if val_w is not None else "TBD",
      "rain_str": f"{val_r} in" if val_r is not None else "0.0 in",
      "raw_ts": latest["Timestamp"],
      "time_str": format_display_time(latest["Timestamp"]),
  }


# ---------------- DYNAMIC HORIZON FORECAST ENGINE (BME280 DRIVEN) ----------------
def analog_forecast(df, hours_ahead=2):
  min_records = 30 if hours_ahead < 24 else 60
  if len(df) < min_records:
    return (
        None,
        f"Requires more logged BME280 observations to project +{hours_ahead} hours.",
    )

  df_clean = df.sort_values("Timestamp").copy()
  current = df_clean.iloc[-1]
  curr_time = current["Timestamp"]

  t_col = find_col(df_clean, ["Temperature", "Temp", "Temp (F)", "temp_f"])
  h_col = find_col(df_clean, ["Humidity", "Humidity (%)", "Relative Humidity"])
  w_col = find_col(df_clean, ["Wind Speed", "Wind", "wind_mph"])
  r_col = find_col(df_clean, ["Rain", "Rainfall", "Precipitation"])

  curr_t = clean_float(current[t_col] if t_col else None)
  curr_h = clean_float(current[h_col] if h_col else None)

  if curr_t is None or curr_h is None:
    return None, "Current BME280 temperature or humidity could not be parsed."

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

  candidates["num_t"] = candidates[t_col].apply(clean_float)
  candidates["num_h"] = candidates[h_col].apply(clean_float)
  candidates = candidates.dropna(subset=["num_t", "num_h"])

  if candidates.empty:
    return None, "Historical records lack usable numeric observations."

  candidates["diff"] = np.sqrt(
      (candidates["num_t"] - curr_t) ** 2 + (candidates["num_h"] - curr_h) ** 2
  )
  best_matches = candidates.nsmallest(5, "diff")

  future_temps = []
  future_hums = []
  future_winds = []
  future_rains = []
  window_margin = pd.Timedelta(minutes=30) if hours_ahead < 24 else pd.Timedelta(hours=1)

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

      if w_col:
        val_w = clean_float(future_window.iloc[0].get(w_col))
        if val_w is not None: future_winds.append(val_w)
      if r_col:
        val_r = clean_float(future_window.iloc[0].get(r_col))
        if val_r is not None: future_rains.append(val_r)

  if not future_temps:
    return None, f"Found matches, but subsequent +{hours_ahead}h records were not logged."

  pred_t = round(float(np.mean(future_temps)), 1)
  pred_h = round(float(np.mean(future_hums)), 1)
  pred_dew = compute_dew_point(pred_t, pred_h)
  
  pred_w = round(float(np.mean(future_winds)), 1) if future_winds else "TBD"
  pred_r = round(float(np.mean(future_rains)), 2) if future_rains else 0.0

  delta_t = round(pred_t - curr_t, 1)

  # Core Fog Logic
  pred_spread = pred_t - pred_dew if pred_dew is not None else 99.0
  if pred_h >= 88 or pred_spread <= 2.5:
    fog_flag = "High"
  elif pred_h >= 80 or pred_spread <= 4.0:
    fog_flag = "Moderate"
  else:
    fog_flag = "Low"

  # Hidden Synoptic Pacific High check: bumps fog risk up if subsidence is active
  high_p_info = detect_pacific_high_pressure(df_clean)
  if high_p_info["status"] == "Strong High (Inversion Lid)" and fog_flag == "Moderate":
    fog_flag = "High"

  return {
      "pred_temp": pred_t,
      "pred_wind": pred_w,
      "pred_rain": pred_r,
      "delta_temp": delta_t,
      "fog_risk": fog_flag,
      "matches": len(future_temps),
      "closest_date": best_matches.iloc[0]["Timestamp"].strftime("%b %d, %Y"),
  }, None


# ---------------- SLEEK THEMED CHARTS ----------------
def render_styled_chart(
    clean_df, col_name, title, color_hex, fill_rgba, tick_format, day_x_range, y_range=None
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


def render_station_charts(df, tab_name):
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
  wind_col = find_col(plot_df, ["Wind Speed", "Wind", "wind_mph"])
  rain_col = find_col(plot_df, ["Rain", "Rainfall", "Precipitation"])
  press_col = find_col(plot_df, ["Pressure", "Pressure (hPa)", "Barometric Pressure"])

  chart_config = {"responsive": True, "displayModeBar": False}

  # Temp Chart
  if temp_col:
    plot_df[temp_col] = pd.to_numeric(
        plot_df[temp_col].astype(str).str.replace("°F", "", regex=False).str.strip(), errors="coerce"
    )
    clean_temp = plot_df.dropna(subset=[temp_col, "Timestamp"]).sort_values("Timestamp")
    if not clean_temp.empty:
      st.plotly_chart(render_styled_chart(
          clean_temp, temp_col, "Temperature Trend", "#f97316", "rgba(249, 115, 22, 0.08)", tick_format, day_x_range
      ), use_container_width=True, config=chart_config)

  # Hum Chart
  if hum_col:
    plot_df[hum_col] = pd.to_numeric(
        plot_df[hum_col].astype(str).str.replace("%", "", regex=False).str.strip(), errors="coerce"
    )
    clean_hum = plot_df.dropna(subset=[hum_col, "Timestamp"]).sort_values("Timestamp")
    if not clean_hum.empty:
      st.plotly_chart(render_styled_chart(
          clean_hum, hum_col, "Relative Humidity Trend", "#3b82f6", "rgba(59, 130, 246, 0.08)", tick_format, day_x_range
      ), use_container_width=True, config=chart_config)

  # Wind Chart
  if wind_col:
    plot_df[wind_col] = pd.to_numeric(
        plot_df[wind_col].astype(str).str.replace("mph", "", regex=False).str.strip(), errors="coerce"
    )
    clean_wind = plot_df.dropna(subset=[wind_col, "Timestamp"]).sort_values("Timestamp")
    if not clean_wind.empty:
      st.plotly_chart(render_styled_chart(
          clean_wind, wind_col, "Wind Speed Trend", "#06b6d4", "rgba(6, 182, 212, 0.08)", tick_format, day_x_range
      ), use_container_width=True, config=chart_config)

  # Rain Chart
  if rain_col:
    plot_df[rain_col] = pd.to_numeric(
        plot_df[rain_col].astype(str).str.replace("in", "", regex=False).str.strip(), errors="coerce"
    )
    clean_rain = plot_df.dropna(subset=[rain_col, "Timestamp"]).sort_values("Timestamp")
    if not clean_rain.empty:
      st.plotly_chart(render_styled_chart(
          clean_rain, rain_col, "Rainfall Trend", "#60a5fa", "rgba(96, 165, 250, 0.08)", tick_format, day_x_range
      ), use_container_width=True, config=chart_config)

  # Pressure Chart
  if press_col:
    plot_df[press_col] = pd.to_numeric(
        plot_df[press_col].astype(str).str.replace("hPa", "", regex=False).str.strip(), errors="coerce"
    )
    clean_press = plot_df.dropna(subset=[press_col, "Timestamp"]).sort_values("Timestamp")
    if not clean_press.empty:
      st.plotly_chart(render_styled_chart(
          clean_press, press_col, "Barometric Pressure Trend", "#a855f7", "rgba(168, 85, 247, 0.08)", tick_format, day_x_range, y_range=[985.0, 1005.0]
      ), use_container_width=True, config=chart_config)


# ---------------- DASHBOARD UI ----------------
@st.fragment(run_every="180s")
def render_dashboard():
  st.markdown(
      "<h2 style='margin-bottom:0.2rem;'>🌦️ DIY Weather Command Center</h2>",
      unsafe_allow_html=True,
  )
  st.caption("Real-Time Microclimate Monitoring & Fog Prediction via BME280")

  df_diy = load_sheet_data(DIY_SHEET_ID)

  # ---------------- 1. CURRENT OBSERVATIONS CARD ----------------
  with st.container(border=True):
    m_diy = extract_station_metrics(df_diy)
    status_tag = get_live_badge(m_diy["raw_ts"]) if m_diy else "⚪ OFFLINE"
    
    st.markdown(
        f"<div style='display:flex; justify-content:space-between;"
        f" align-items:center;'><b>🛠️ Current Conditions (BME280)</b>{status_tag}</div>",
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

  # ---------------- 2. DYNAMIC FORECAST CARD WITH TIME HORIZON SWITCHER ----------------
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
            key="forecast_horizon_picker",
        )

      horizon_map = {"+1 hr": 1, "+3 hrs": 3, "+5 hrs": 5, "+24 hrs": 24}
      selected_hours = horizon_map.get(forecast_horizon_str, 3)

      forecast, err = analog_forecast(df_diy, hours_ahead=selected_hours)
      
      if forecast:
        st.markdown(
            f"<div style='display:flex; justify-content:space-between;"
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
        fc3.metric(
            "Projected Wind", 
            f"{forecast['pred_wind']} mph" if forecast["pred_wind"] != "TBD" else "TBD"
        )
        fc4.metric("Projected Rain", f"{forecast['pred_rain']} in")

        st.caption(
            f"Calculated via BME280 historical analogs ({forecast_horizon_str}"
            f" ahead). Closest pattern matched: **{forecast['closest_date']}**."
        )
      elif err:
        st.caption(f"Forecast model info: {err}")

  # ---------------- 3. TIMEFRAME SELECTOR & TREND CHARTS ----------------
  timeframe = st.radio(
      "Select Timeframe",
      ["Daily", "Weekly", "Monthly", "All Time"],
      horizontal=True,
      label_visibility="collapsed",
  )

  duration_key = timeframe.lower()
  render_station_charts(filter_by_duration(df_diy, duration_key), duration_key)

render_dashboard()