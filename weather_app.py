import base64
import json
import os
import re
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="Weather Station Dashboard", layout="wide")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

LACROSSE_SHEET_ID = "1NwM9U45ulkX_bTh5OVW5Sucah5VkacV7G1dj9uYXDXw"
TEMPEST_SHEET_ID = "1krSreOTSO_JkXZy_aVzsMKtOgQNUombxadCT6JqUCjQ"
DIY_SHEET_ID = "1YdRqfRsdRBIKEtmVNGujmUIpSGTWbYyVejcGfbVcQbI"


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


def render_station_charts(df, station_name, tab_name):
    if df.empty:
        st.info(f"No data available for {station_name} in this timeframe.")
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

    st.divider()

    timeframe = st.radio(
        "Select Timeframe",
        [
            "📅 Daily (12:00 AM - 11:59 PM)",
            "🗓️ Weekly (Last 7 Days)",
            "📆 Monthly (Last 30 Days)",
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
                filter_by_duration(df_lacrosse, duration_key), "La Crosse", duration_key
            )
        with c2:
            render_station_charts(
                filter_by_duration(df_tempest, duration_key), "Tempest", duration_key
            )

    elif station_view == "🛠️ DIY BME280 Station":
        render_station_charts(
            filter_by_duration(df_diy, duration_key), "DIY Station", duration_key
        )


render_dashboard()