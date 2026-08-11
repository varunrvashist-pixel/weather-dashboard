import streamlit as st
from datetime import datetime, timedelta
import pandas as pd
from prophet import Prophet
import plotly.graph_objects as go

# Keep your existing helper module connections exactly the same
import weather_data as wd
import charts

STALE_AFTER_MINUTES = 15

st.set_page_config(page_title="Backyard Weather Station", page_icon="🏡", layout="wide")
st.title("🏡 Varun's Live Backyard Weather Station")
st.markdown("---")

# Use your working collection-safe load methods
df = wd.load_readings()
reading = wd.latest_reading(df)

if reading:
    is_stale = datetime.now() - reading["timestamp"] > timedelta(minutes=STALE_AFTER_MINUTES)
 
    col1, col2, col3 = st.columns(3)
    col1.metric(
        label="🌡️ Temperature",
        value=f"{reading['temp']:.1f} °F" if reading["temp"] is not None else "N/A",
    )
    col2.metric(
        label="💨 Wind Speed",
        value=f"{reading['wind']:.1f} mph" if reading["wind"] is not None else "Glitched/Pending",
    )
    col3.metric(
        label="💧 Humidity",
        value=f"{reading['humid']:.0f}%" if reading["humid"] is not None else "N/A",
    )
 
    st.markdown("---")
    timestamp_str = reading["timestamp"].strftime("%I:%M:%S %p")
    if is_stale:
        st.warning(f"⚠️ Last reading is from {timestamp_str} — no newer data yet.")
    else:
        st.caption(f"🔄 Reading as of {timestamp_str}")

    # --- HIGH-ACCURACY PROPHET FORECASTING BLOCK ---
    st.markdown("---")
    st.subheader("🔮 Tuned 2-Hour Temperature Forecast")
    
    if not df.empty and "temp" in df.columns and reading["temp"] is not None:
        try:
            # 1. Format the dataframe copy for Prophet strictly within frontend memory
            prophet_df = df[['timestamp', 'temp']].rename(columns={'timestamp': 'ds', 'temp': 'y'})
            prophet_df = prophet_df.dropna().sort_values('ds')
            
            # Remove localized timezone metadata to keep Prophet from panicking
            if prophet_df['ds'].dt.tz is not None:
                prophet_df['ds'] = prophet_df['ds'].dt.tz_localize(None)

            # 2. Strategy 2: Diurnal Tuning Configuration
            model = Prophet(
                changepoint_prior_scale=0.4,  # Highly reactive to recent morning trend shifts
                daily_seasonality=False,       # Override standard slow defaults
                yearly_seasonality=False,
                weekly_seasonality=False
            )
            model.add_seasonality(name='daily', period=1, fourier_order=25)  # Capture sharp solar curves
            model.fit(prophet_df)
            
            # Construct lookahead dataframe (12 steps * 10 min = 2 hours)
            future = model.make_future_dataframe(periods=12, freq='10min', include_history=False)
            
            # Anchor setup
            actual_time = prophet_df.iloc[-1]['ds']
            if future.empty or future.iloc[0]['ds'] > actual_time:
                future = pd.concat([pd.DataFrame({'ds': [actual_time]}), future], ignore_index=True)
                
            forecast = model.predict(future)
            
            # 3. Strategy 1: Live Offset Shift Alignment
            actual_temp = float(reading['temp'])
            predicted_now = forecast.iloc[0]['yhat']
            offset = actual_temp - predicted_now
            
            # Instantly match chart to physical yard conditions
            forecast['yhat'] += offset
            forecast['yhat_lower'] += offset
            forecast['yhat_upper'] += offset
            
            # Render the Forecast Interactive Plotly Chart
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=forecast['ds'], y=forecast['yhat'],
                mode='lines', name='Predicted Trend',
                line=dict(color='#9b5de5', width=3),
                hovertemplate='<b>Time</b>: %{x|%I:%M %p}<br><b>Forecast</b>: %{y:.1f}°F<extra></extra>'
            ))
            fig.add_trace(go.Scatter(
                x=pd.concat([forecast['ds'], forecast['ds'][::-1]]),
                y=pd.concat([forecast['yhat_upper'], forecast['yhat_lower'][::-1]]),
                fill='toself', fillcolor='rgba(155, 93, 229, 0.1)',
                line=dict(color='rgba(255,255,255,0)'), showlegend=False, hoverinfo="skip"
            ))
            fig.update_layout(
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                margin=dict(l=20, r=20, t=20, b=20), height=350,
                xaxis=dict(showgrid=True, gridcolor="#262730"),
                yaxis=dict(showgrid=True, gridcolor="#262730")
            )
            st.plotly_chart(fig, use_container_width=True)
            
        except Exception as forecast_err:
            st.info("Gathering more background metric patterns to build trend forecast charts...")
    else:
        st.info("Insufficient historical points recorded to run forecast modeling.")

    # --- HISTORICAL CHARTS SECTION ---
    st.markdown("---")
    st.subheader("Today's Historical Activity")
    today_df = wd.today_readings(df)
 
    if today_df.empty:
        st.info("No readings logged yet today.")
    else:
        st.plotly_chart(
            charts.daily_line_chart(today_df, "temp", "Temperature", "°F", "#E07A5F"),
            use_container_width=True,
        )
        st.plotly_chart(
            charts.daily_line_chart(today_df, "wind", "Wind Speed", "mph", "#3D5A80"),
            use_container_width=True,
        )
        st.plotly_chart(
            charts.daily_line_chart(today_df, "humid", "Humidity", "%", "#81B29A"),
            use_container_width=True,
        )
 
    st.markdown("---")
    if st.button("📅 View Weekly Trends →", use_container_width=True):
        st.switch_page("1_Weekly_Trends.py")
 
else:
    st.error("⚠️ Connection Gateway Offline.")
    st.caption("We couldn't reach the weather data store. Please try refreshing in a minute.")