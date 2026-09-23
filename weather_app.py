import base64
import json
import os
import re
import gspread
from google.oauth2.service_account import Credentials
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
        temp_f = f"{round((temp_c * 9 / 5) + 32, 1)}°F" if temp_c is not None else "N/A"

        dewp_c = obs.get("dewp")
        dewp_f = f"{round((dewp_c * 9 / 5) + 32, 1)}°F" if dewp_c is not None else "N/A"

        spread_str = "N/A"
        if temp_c is not None and dewp_c is not None:
            spread_f = round((temp_c - dewp_c) * 9 / 5, 1)
            spread_str = f"{spread_f}°F"

        wspd_kt = obs.get("wspd")
        wdir = obs.get("wdir")
        if wspd_kt is not None:
            wspd_mph = round(wspd_kt * 1.15078, 1)
            wind_str = f"{wspd_mph} mph ({wdir}°)" if wdir is not None else f"{wspd_mph} mph"
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
        col3.metric("T-Td Spread", spread_str, help="Spread ≤ 3°F indicates high fog probability")
        col4.metric("Wind", wind_str)

        col5, col6, col7, col8 = st.columns(4)
        col5.metric("Visibility", visib_str)
        col6.metric("Cloud / Ceiling", ceiling)
        col7.metric("Pressure", altim_mb)
        col8.metric("Flight Cat", fltcat)

        st.code(obs.get("rawOb", ""), language="text")


def get_google_sheets_client():
    if os.path.exists("cloud_key.json"):
        creds = Credentials.from_service_account_file(
            "cloud_key.json", scopes=SCOPES
        )
        return gspread.authorize(creds)

    try:
        if "GCP_KEY_BASE64" in st.secrets:
            key_json = base64.b64decode(st.secrets["GCP_KEY_BASE64"]).decode(
                "utf-8"
            )
            creds_info = json.loads(key_json)
            creds = Credentials.from_service_account_info(
                creds_info, scopes=SCOPES
            )
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
            (temp_df["Timestamp"] >= start_of_day) & (temp_df["Timestamp"] < end_of_day)
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


def analog_forecast(df, hours_ahead=2):
    """Predicts weather by finding the most similar past days at this hour in your sheet history."""
    if len(df) < 50:
        return None, "Need at least a few days of data to find matching historical patterns."

    df_clean = df.sort_values("Timestamp").copy()
    current = df_clean.iloc[-1]
    curr_time = current["Timestamp"]

    t_col = find_col(df_clean, ["Temperature", "Temp", "Outdoor Temp", "Air Temp", "Temp (F)", "temp_f"])
    h_col = find_col(df_clean, ["Humidity (%)", "Humidity", "Outdoor Humidity", "Relative Humidity", "hum"])
    p_col = find_col(df_clean, ["Pressure", "Pressure (hPa)", "Barometric Pressure", "press"])

    if not t_col or not h_col:
        return None, "Required temperature and humidity columns not found."

    try:
        curr_t = float(str(current[t_col]).replace("°F", "").replace("F", "").strip())
        curr_h = float(str(current[h_col]).replace("%", "").strip())
        curr_p = float(str(current[p_col]).replace("hPa", "").strip()) if p_col and pd.notna(current[p_col]) else 1013.25
    except Exception:
        return None, "Error parsing current weather readings."

    # Exclude the last 12 hours so it matches historical past days, not earlier today
    past_df = df_clean[df_clean["Timestamp"] < (curr_time - pd.Timedelta(hours=12))].copy()

    # Match rows around the same hour of day (+/- 1 hr)
    target_hour = curr_time.hour
    past_df["hour_diff"] = (past_df["Timestamp"].dt.hour - target_hour).abs()
    candidates = past_df[(past_df["hour_diff"] <= 1) | (past_df["hour_diff"] >= 23)].copy()

    if len(candidates) < 3:
        return None, "Not enough matching hours logged in past history yet."

    candidates["num_t"] = pd.to_numeric(
        candidates[t_col].astype(str).str.replace("°F", "", regex=False).str.replace("F", "", regex=False).str.strip(),
        errors="coerce"
    )
    candidates["num_h"] = pd.to_numeric(
        candidates[h_col].astype(str).str.replace("%", "", regex=False).str.strip(),
        errors="coerce"
    )
    candidates = candidates.dropna(subset=["num_t", "num_h"])

    if candidates.empty:
        return None, "No valid historical readings found to compare."

    # Euclidean distance metric (Similarity Score)
    candidates["diff"] = np.sqrt((candidates["num_t"] - curr_t) ** 2 + (candidates["num_h"] - curr_h) ** 2)
    best_matches = candidates.nsmallest(5, "diff")

    future_temps = []
    future_hums = []

    for _, match_row in best_matches.iterrows():
        match_time = match_row["Timestamp"]
        target_future = match_time + pd.Timedelta(hours=hours_ahead)

        future_window = df_clean[
            (df_clean["Timestamp"] >= target_future - pd.Timedelta(minutes=30)) &
            (df_clean["Timestamp"] <= target_future + pd.Timedelta(minutes=30))
        ]
        if not future_window.empty:
            val_t = pd.to_numeric(
                str(future_window.iloc[0][t_col]).replace("°F", "").replace("F", "").strip(),
                errors="coerce"
            )
            val_h = pd.to_numeric(
                str(future_window.iloc[0][h_col]).replace("%", "").strip(),
                errors="coerce"
            )
            if pd.notna(val_t):
                future_temps.append(val_t)
            if pd.notna(val_h):
                future_hums.append(val_h)

    if not future_temps:
        return None, "Matching historical moments found, but lacked follow-up readings."

    pred_t = round(float(np.mean(future_temps)), 1)
    pred_h = round(float(np.mean(future_hums)), 1)
    
    # Fog propensity: High if predicted humidity is near saturation (>88%)
    fog_flag = "High" if pred_h >= 88 else ("Moderate" if pred_h >= 80 else "Low")

    return {
        "pred_temp": pred_t,
        "pred_hum": pred_h,
        "delta_temp": round(pred_t - curr_t, 1),
        "delta_hum": round(pred_h - curr_h, 1),
        "fog_risk": fog_flag,
        "matches": len(future_temps),
        "closest_date": best_matches.iloc[0]["Timestamp"].strftime("%b %d, %Y"),
    }, None


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
        plot_df, ["Temperature", "Temp", "Outdoor Temp", "Air Temp", "Temp (F)", "temp_f"]
    )
    wind_col = find_col(
        plot_df, ["Wind Speed", "Wind", "Wind_Speed", "WindSpeed", "Wind (mph)"]
    )
    hum_col = find_col(
        plot_df, ["Humidity (%)", "Humidity", "Outdoor Humidity", "Relative Humidity", "hum"]
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
        clean_temp_df = plot_df.dropna(subset=[temp_col, "Timestamp"]).sort_values("Timestamp")

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
        clean_wind_df = plot_df.dropna(subset=[wind_col, "Timestamp"]).sort_values("Timestamp")

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
            plot_df[hum_col]
            .astype(str)
            .str.replace("%", "", regex=False)
            .str.strip()
        )
        plot_df[hum_col] = pd.to_numeric(plot_df[hum_col], errors="coerce")
        clean_hum_df = plot_df.dropna(subset=[hum_col, "Timestamp"]).sort_values("Timestamp")

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
        clean_press_df = plot_df.dropna(subset=[press_col, "Timestamp"]).sort_values("Timestamp")

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
            if not df_lacrosse.empty:
                latest = df_lacrosse.iloc[-1]
                temp_col = find_col(df_lacrosse, ["Temperature", "Temp", "Outdoor Temp"])
                wind_col = find_col(df_lacrosse, ["Wind Speed", "Wind", "Wind_Speed", "WindSpeed", "Wind (mph)"])
                hum_col = find_col(df_lacrosse, ["Humidity", "Outdoor Humidity"])

                m1, m2, m3 = st.columns(3)
                m1.metric("Temp", f"{latest[temp_col]} °F" if temp_col else "N/A")
                m2.metric("Wind", f"{latest[wind_col]} mph" if wind_col else "N/A")
                m3.metric("Humidity", f"{latest[hum_col]} %" if hum_col else "N/A")
                st.caption(f"Last updated: {format_display_time(latest['Timestamp'])}")
            else:
                st.warning("No data found for La Crosse station.")

        with col_tempest:
            st.subheader("⚡ Tempest")
            if not df_tempest.empty:
                latest = df_tempest.iloc[-1]
                temp_col = find_col(df_tempest, ["Temperature", "Temp", "Outdoor Temp", "Air Temp", "Temp (F)", "temp_f"])
                wind_col = find_col(df_tempest, ["Wind Speed", "Wind", "Wind_Speed", "WindSpeed", "Wind (mph)"])
                hum_col = find_col(df_tempest, ["Humidity", "Outdoor Humidity", "Relative Humidity", "hum"])

                m1, m2, m3 = st.columns(3)
                m1.metric("Temp", f"{latest[temp_col]} °F" if temp_col else "N/A")
                m2.metric("Wind", f"{latest[wind_col]} mph" if wind_col else "N/A")
                m3.metric("Humidity", f"{latest[hum_col]} %" if hum_col else "N/A")
                st.caption(f"Last updated: {format_display_time(latest['Timestamp'])}")
            else:
                st.warning("No data found for Tempest station.")

    else:
        st.subheader("🛠️ DIY BME280 Station")
        if not df_diy.empty:
            latest = df_diy.iloc[-1]
            temp_col = find_col(df_diy, ["Temperature", "Temp"])
            hum_col = find_col(df_diy, ["Humidity (%)", "Humidity", "hum"])
            press_col = find_col(df_diy, ["Pressure", "Pressure (hPa)"])

            m1, m2, m3 = st.columns(3)
            m1.metric("Temp", f"{latest[temp_col]} °F" if temp_col else "N/A")
            m2.metric("Humidity", f"{latest[hum_col]} %" if hum_col else "N/A")
            m3.metric("Pressure", f"{latest[press_col]} hPa" if press_col else "N/A")
            st.caption(f"Last updated: {format_display_time(latest['Timestamp'])}")
        else:
            st.warning("No data found for DIY station.")

    # Analog / Historical Pattern Forecaster Card
    active_df = df_tempest if station_view == "⚡ La Crosse & Tempest" else df_diy
    if not active_df.empty:
        forecast, err = analog_forecast(active_df, hours_ahead=2)
        if forecast:
            st.divider()
            with st.container():
                st.markdown("#### 🔮 Historical Pattern Forecast (+2 Hours)")
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
                fc3.metric("Fog Propensity", forecast["fog_risk"])
                fc4.metric("Matching Days", f"{forecast['matches']} days")
                st.caption(
                    f"Analyzed against similar past days in your sheet (closest pattern: **{forecast['closest_date']}**)."
                )

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
                filter_by_duration(df_lacrosse, duration_key), "La Crosse", duration_key, metar_station="KSQL"
            )
        with c2:
            render_station_charts(
                filter_by_duration(df_tempest, duration_key), "Tempest", duration_key, metar_station="KSFO"
            )

    elif station_view == "🛠️ DIY BME280 Station":
        render_station_charts(
            filter_by_duration(df_diy, duration_key), "DIY Station", duration_key, metar_station="KSQL"
        )


render_dashboard()