import os
import pandas as pd
import psycopg2
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="AirWatch India", layout="wide")


def db_url():
    try:
        return st.secrets["DATABASE_URL"]          # on Streamlit Cloud
    except Exception:
        from dotenv import load_dotenv             # on your laptop
        load_dotenv(r"C:\Users\HP\.env")
        url = os.getenv("DATABASE_URL")
        if not url:
            st.error('DATABASE_URL not found. Add it under Settings > Secrets as: '
                     'DATABASE_URL = "postgresql://..."')
            st.stop()
        return url


@st.cache_data(ttl=600)
def q(sql, params=None):
    conn = psycopg2.connect(db_url().strip(), connect_timeout=30)
    try:
        return pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()


def category(a):
    if pd.isna(a):
        return "n/a"
    a = float(a)
    return ("Good" if a <= 50 else "Satisfactory" if a <= 100 else "Moderate" if a <= 200
            else "Poor" if a <= 300 else "Very Poor" if a <= 400 else "Severe")


st.title("AirWatch India")
st.caption("Near real-time air quality for 36 stations in 5 cities. Updated hourly. "
           "AQI shown is a PM-based AQI: CPCB method, 24-hour averages of PM2.5 and PM10, "
           "at least 16 hours of data, highest sub-index wins.")

# ---- latest reading per station (newest row that has an AQI) ----
latest = q("""
select distinct on (station_id) station_id, city, name, ts, pm25, pm10, aqi, category
from station_aqi
where aqi is not null
order by station_id, ts desc
""")
latest["ts"] = pd.to_datetime(latest["ts"], utc=True)
latest["aqi"] = pd.to_numeric(latest["aqi"])
now = pd.Timestamp.now(tz="UTC")
latest["hours_ago"] = ((now - latest["ts"]).dt.total_seconds() / 3600).round(1)
latest["freshness"] = latest["hours_ago"].apply(
    lambda h: "Fresh" if h <= 6 else "Delayed" if h <= 24 else "Stale")

newest = latest["ts"].max().tz_convert("Asia/Kolkata")
st.write(f"**Newest reading:** {newest:%d %b %Y, %I:%M %p} IST "
         f"(stations report with a delay of a few hours)")

# ---- city cards ----
st.subheader("Current AQI by city")
city_aqi = latest.groupby("city").agg(aqi=("aqi", "mean"), stations=("station_id", "count")).round(0)
city_aqi = city_aqi.sort_values("aqi", ascending=False)
cols = st.columns(len(city_aqi))
for col, (city_name, row) in zip(cols, city_aqi.iterrows()):
    col.metric(city_name, "n/a" if pd.isna(row["aqi"]) else int(row["aqi"]),
               f"{category(row['aqi'])} | {int(row['stations'])} stations", delta_color="off")

# ---- weather ----
st.subheader("Current weather")
weather = q("""
select distinct on (city) city, ts, temperature, humidity, wind_speed, wind_dir
from weather
order by city, ts desc
""")
weather = weather.rename(columns={"temperature": "Temp (C)", "humidity": "Humidity (%)",
                                  "wind_speed": "Wind (km/h)", "wind_dir": "Wind dir (deg)"})
st.dataframe(weather.drop(columns=["ts"]), hide_index=True, use_container_width=True)

# ---- 7-day trend ----
st.subheader
