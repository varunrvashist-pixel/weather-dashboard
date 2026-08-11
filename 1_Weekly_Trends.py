import streamlit as st

import weather_data as wd
import charts

st.set_page_config(page_title="Weekly Trends", page_icon="📅", layout="wide")
st.title("📅 Weekly Trends")
st.caption("Last 7 days — solid lines are each day's high and low, the grey dotted line is the daily average.")
st.markdown("---")

df = wd.load_readings()
summary = wd.daily_summary(df, days=7)

if summary.empty:
    st.info("Not enough history yet to show weekly trends. Check back once a few days of readings have logged.")
else:
    st.plotly_chart(
        charts.weekly_line_chart(summary, "temp", "Temperature", "°F", "#E07A5F"),
        use_container_width=True,
    )
    st.plotly_chart(
        charts.weekly_line_chart(summary, "wind", "Wind Speed", "mph", "#3D5A80"),
        use_container_width=True,
    )
    st.plotly_chart(
        charts.weekly_line_chart(summary, "humid", "Humidity", "%", "#81B29A"),
        use_container_width=True,
    )

st.markdown("---")
if st.button("⬅ Back to Today"):
    st.switch_page("weather_station.py")
