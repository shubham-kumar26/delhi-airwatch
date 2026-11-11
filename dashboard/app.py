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
        return os.getenv("DATABASE_URL")

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

# ---- latest reading per station ----
latest = q("""
    select distinct on (station_id) station_id, city, name, ts, pm25, pm10, aqi, category
    from station_aqi
    where aqi is not null
    order by station_id, ts desc
""")
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
for col, (city, row) in zip(cols, city_aqi.iterrows()):
    col.metric(city, "n/a" if pd.isna(row["aqi"]) else int(row["aqi"]),
               f"{category(row['aqi'])} | {int(row['stations'])} stations", delta_color="off")

# ---- weather ----
st.subheader("Current weather")
weather = q("""
    select distinct on (city) city, ts, temperature, humidity, wind_speed, wind_dir
    from weather order by city, ts desc
""")
weather = weather.rename(columns={"temperature": "Temp (C)", "humidity": "Humidity (%)",
                                  "wind_speed": "Wind (km/h)", "wind_dir": "Wind dir (deg)"})
st.dataframe(weather.drop(columns=["ts"]), hide_index=True, use_container_width=True)

# ---- 7-day trend ----
st.subheader("AQI over the last 7 days")
trend = q("""
    select city, date_trunc('hour', ts) as hour, round(avg(aqi)) as aqi
    from station_aqi
    where aqi is not null and ts > now() - interval '7 days'
    group by 1, 2 order by 2
""")
trend["aqi"] = pd.to_numeric(trend["aqi"])
st.plotly_chart(px.line(trend, x="hour", y="aqi", color="city"), use_container_width=True)

# ---- hour of day pattern ----
st.subheader("Typical day: average PM2.5 by hour (Indian time)")
c1, c2 = st.columns([1, 2])
city = c1.selectbox("City", sorted(latest["city"].unique()))
skip = c1.checkbox("Exclude the Diwali period (7-10 Nov)", value=False)
extra = "and (r.ts at time zone 'Asia/Kolkata')::date not between '2026-11-07' and '2026-11-10'" if skip else ""
pattern = q(f"""
    select extract(hour from r.ts at time zone 'Asia/Kolkata')::int as hour_ist,
           round(avg(r.pm25)::numeric, 0) as avg_pm25
    from readings r join stations s using (station_id)
    where s.city = %s and r.pm25 is not null {extra}
    group by 1 order by 1
""", params=(city,))
pattern["avg_pm25"] = pd.to_numeric(pattern["avg_pm25"])
c2.plotly_chart(px.bar(pattern, x="hour_ist", y="avg_pm25"), use_container_width=True)

# ---- station table ----
st.subheader("All stations")
table = latest[["city", "name", "aqi", "category", "pm25", "pm10", "hours_ago", "freshness"]] \
    .sort_values(["city", "aqi"], ascending=[True, False])
st.dataframe(table, hide_index=True, use_container_width=True)
st.caption("Data: OpenAQ (government monitors) and Open-Meteo (weather). "
           "Fresh = reading within 6 hours. Some readings during Diwali were extremely high.")
