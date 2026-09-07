import os
import re
import base64
import json
import streamlit as st
import pandas as pd
import plotly.express as px
import gspread
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="Dual Weather Station Dashboard", layout="wide")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

LACROSSE_SHEET_ID = "1NwM9U45ulkX_bTh5OVW5Sucah5VkacV7G1dj9uYXDXw"
TEMPEST_SHEET_ID = "1krSreOTSO_JkXZy_aVzsMKtOgQNUombxadCT6JqUCjQ"

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

    st.error("Missing credentials. Place cloud_key.json in your app directory.")
    st.stop()

@st.cache_data(ttl=20)
def load_sheet_data(sheet_id):
    try:
        gc = get_google_sheets_client()
        sheet = gc.open_by_key(sheet_id).sheet1
        records = sheet.get_all_records()
        if not records:
            return pd.DataFrame()
        df = pd.DataFrame(records)
        df["Timestamp"] = pd.to_datetime(df["Timestamp"])
        return df.sort_values("Timestamp")
    except Exception as e:
        st.error(f"Error loading sheet {sheet_id}: {e}")
        return pd.DataFrame()

def find_col(df, options):
    for opt in options:
        for c in df.columns:
            if opt.lower() == str(c).strip().lower():
                return c
    return None

def filter_by_duration(df, duration):
    if df.empty or "Timestamp" not in df.columns:
        return df
    latest_time = df["Timestamp"].max()
    if duration == "daily":
        cutoff = latest_time - pd.Timedelta(days=1)
    elif duration == "weekly":
        cutoff = latest_time - pd.Timedelta(days=7)
    elif duration == "monthly":
        cutoff = latest_time - pd.Timedelta(days=30)
    else:
        return df
    return df[df["Timestamp"] >= cutoff]

def render_station_charts(df, station_name, tab_name):
    if df.empty:
        st.info(f"No data available for {station_name} in this timeframe.")
        return

    temp_col = find_col(df, ["Temperature", "Temp", "Outdoor Temp"])
    wind_col = find_col(df, ["Wind Speed", "Wind", "Wind_Speed", "WindSpeed"])
    hum_col = find_col(df, ["Humidity", "Outdoor Humidity"])

    prefix = f"{tab_name}_{station_name}".lower().replace(" ", "_")

    if temp_col:
        fig_temp = px.line(df, x="Timestamp", y=temp_col, title=f"{station_name} - Temperature")
        st.plotly_chart(fig_temp, use_container_width=True, key=f"{prefix}_temp")
    if wind_col:
        fig_wind = px.line(df, x="Timestamp", y=wind_col, title=f"{station_name} - Wind Speed")
        st.plotly_chart(fig_wind, use_container_width=True, key=f"{prefix}_wind")
    if hum_col:
        fig_hum = px.line(df, x="Timestamp", y=hum_col, title=f"{station_name} - Humidity")
        st.plotly_chart(fig_hum, use_container_width=True, key=f"{prefix}_hum")

@st.fragment(run_every="60s")
def render_dashboard():
    st.title("🌦️ Dual Station Weather Dashboard: La Crosse vs. Tempest")

    df_lacrosse = load_sheet_data(LACROSSE_SHEET_ID)
    df_tempest = load_sheet_data(TEMPEST_SHEET_ID)

    col_lacrosse_metrics, col_tempest_metrics = st.columns(2)

    with col_lacrosse_metrics:
        st.subheader("🏡 La Crosse Station")
        if not df_lacrosse.empty:
            latest = df_lacrosse.iloc[-1]
            temp_col = find_col(df_lacrosse, ["Temperature", "Temp", "Outdoor Temp"])
            wind_col = find_col(df_lacrosse, ["Wind Speed", "Wind", "Wind_Speed", "WindSpeed"])
            hum_col = find_col(df_lacrosse, ["Humidity", "Outdoor Humidity"])

            m1, m2, m3 = st.columns(3)
            m1.metric("Temp", f"{latest[temp_col]} °F" if temp_col else "N/A")
            m2.metric("Wind", f"{latest[wind_col]} mph" if wind_col else "N/A")
            m3.metric("Humidity", f"{latest[hum_col]} %" if hum_col else "N/A")
            st.caption(f"Last updated: {latest['Timestamp']}")

    with col_tempest_metrics:
        st.subheader("⚡ Tempest Station")
        if not df_tempest.empty:
            latest = df_tempest.iloc[-1]
            temp_col = find_col(df_tempest, ["Temperature", "Temp"])
            wind_col = find_col(df_tempest, ["Wind Speed", "Wind", "Wind_Speed", "WindSpeed"])
            hum_col = find_col(df_tempest, ["Humidity"])

            m1, m2, m3 = st.columns(3)
            m1.metric("Temp", f"{latest[temp_col]} °F" if temp_col else "N/A")
            m2.metric("Wind", f"{latest[wind_col]} mph" if wind_col else "N/A")
            m3.metric("Humidity", f"{latest[hum_col]} %" if hum_col else "N/A")
            st.caption(f"Last updated: {latest['Timestamp']}")

    st.divider()

    tab_daily, tab_weekly, tab_monthly = st.tabs(["📅 Daily (Last 24h)", "🗓️ Weekly (Last 7 Days)", "📆 Monthly (Last 30 Days)"])

    with tab_daily:
        c1, c2 = st.columns(2)
        with c1:
            render_station_charts(filter_by_duration(df_lacrosse, "daily"), "La Crosse", "daily")
        with c2:
            render_station_charts(filter_by_duration(df_tempest, "daily"), "Tempest", "daily")

    with tab_weekly:
        c1, c2 = st.columns(2)
        with c1:
            render_station_charts(filter_by_duration(df_lacrosse, "weekly"), "La Crosse", "weekly")
        with c2:
            render_station_charts(filter_by_duration(df_tempest, "weekly"), "Tempest", "weekly")

    with tab_monthly:
        c1, c2 = st.columns(2)
        with c1:
            render_station_charts(filter_by_duration(df_lacrosse, "monthly"), "La Crosse", "monthly")
        with c2:
            render_station_charts(filter_by_duration(df_tempest, "monthly"), "Tempest", "monthly")

render_dashboard()