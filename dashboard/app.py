import os
import re
import pandas as pd
import psycopg2
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(page_title="AirWatch India", layout="wide")


def secret(name, required=False):
    """Read a secret from Streamlit Cloud, or from the .env file on a laptop."""
    try:
        return str(st.secrets[name]).strip()
    except Exception:
        from dotenv import load_dotenv
        load_dotenv(r"C:\Users\HP\.env")
        value = (os.getenv(name) or "").strip()
        if required and not value:
            st.error(f'{name} not found. Add it under Settings > Secrets as: {name} = "..."')
            st.stop()
        return value


def db_url():
    return secret("DATABASE_URL", required=True)


@st.cache_data(ttl=600)
def q(sql, params=None):
    conn = psycopg2.connect(db_url(), connect_timeout=30)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        cur.close()
        return pd.DataFrame(rows, columns=cols)
    finally:
        conn.close()


def write(statements):
    """Run several (sql, params) statements in one transaction. Not cached."""
    conn = psycopg2.connect(db_url(), connect_timeout=30)
    try:
        cur = conn.cursor()
        result = None
        for sql, params in statements:
            cur.execute(sql, params)
            if cur.description:
                result = cur.fetchall()
        conn.commit()
        cur.close()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def send_confirmation(to_email, token):
    """Send the 'please confirm' email through Brevo."""
    api_key = secret("BREVO_API_KEY")
    sender = secret("SENDER_EMAIL")
    base = secret("DASHBOARD_URL").rstrip("/")
    if not api_key or not sender or not base:
        raise RuntimeError("BREVO_API_KEY, SENDER_EMAIL or DASHBOARD_URL is missing")
    link = f"{base}/?confirm={token}"
    text = (
        "Hello,\n\n"
        "Someone (hopefully you) asked to receive air quality alerts from AirWatch India.\n\n"
        f"Please confirm your email by opening this link:\n{link}\n\n"
        "If you did not sign up, just ignore this email. You will not receive any alerts.\n"
    )
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": api_key, "content-type": "application/json",
                 "accept": "application/json"},
        json={"sender": {"name": "AirWatch India", "email": sender},
              "to": [{"email": to_email}],
              "subject": "Please confirm your AirWatch India alerts",
              "textContent": text},
        timeout=30,
    )
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"Brevo error {r.status_code}")


def category(a):
    if pd.isna(a):
        return "n/a"
    a = float(a)
    return ("Good" if a <= 50 else "Satisfactory" if a <= 100 else "Moderate" if a <= 200
            else "Poor" if a <= 300 else "Very Poor" if a <= 400 else "Severe")


# ---- confirm link: ?confirm=<token> ----
confirm_token = st.query_params.get("confirm")
if confirm_token:
    try:
        res = write([
            ("update subscribers set confirmed = true, active = true where confirm_token = %s "
             "returning subscriber_id", (confirm_token,)),
        ])
        if res:
            write([("insert into events (subscriber_id, event_type) values (%s, 'confirmed')",
                    (res[0][0],))])
            st.success("Your email is confirmed. You will now get an alert when your city's "
                       "AQI goes above your limit.")
        else:
            st.warning("This confirmation link is not valid.")
    except Exception:
        st.error("Something went wrong. Please try the link again in a few minutes.")
    st.stop()

# ---- unsubscribe link: ?unsubscribe=<token> ----
token = st.query_params.get("unsubscribe")
if token:
    try:
        res = write([
            ("update subscribers set active = false where unsubscribe_token = %s "
             "returning subscriber_id", (token,)),
        ])
        if res:
            write([("insert into events (subscriber_id, event_type) values (%s, 'unsubscribe')",
                    (res[0][0],))])
            st.success("You have been unsubscribed. You will not get any more alert emails.")
        else:
            st.warning("This unsubscribe link is not valid.")
    except Exception:
        st.error("Something went wrong. Please try the link again in a few minutes.")
    st.stop()

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
for c in ["aqi", "pm25", "pm10"]:
    latest[c] = pd.to_numeric(latest[c])
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

# ---- email alert signup (double opt-in) ----
st.subheader("Get an email alert")
st.write("Choose a city and an AQI limit. You will get an email when the city's AQI goes above it. "
         "We will first send you a link to confirm your email. "
         "You can unsubscribe at any time using the link in each alert.")
with st.form("signup", clear_on_submit=True):
    f1, f2, f3 = st.columns([2, 1, 1])
    email = f1.text_input("Email address")
    sub_city = f2.selectbox("City", sorted(latest["city"].unique()), key="sub_city")
    limit = f3.select_slider("Alert me above AQI", options=[100, 150, 200, 250, 300, 350, 400],
                             value=200, key="sub_limit")
    consent = st.checkbox("I agree to receive air quality alert emails from AirWatch India "
                          "and understand I can unsubscribe at any time.")
    submitted = st.form_submit_button("Subscribe")

if submitted:
    email_clean = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_clean):
        st.error("Please enter a valid email address.")
    elif not consent:
        st.error("Please tick the consent box to subscribe.")
    else:
        try:
            sid, ctoken = None, None
            row = write([
                ("""insert into subscribers (email, city, aqi_limit, consent, active, confirmed)
                    values (%s, %s, %s, true, true, false)
                    on conflict (email) do nothing
                    returning subscriber_id, confirm_token""",
                 (email_clean, sub_city, int(limit))),
            ])
            if row:
                sid, ctoken = row[0]
                write([("insert into events (subscriber_id, event_type, detail) values (%s, 'signup', %s)",
                        (sid, f"{sub_city} above {limit}"))])
            else:
                existing = write([("select subscriber_id, active, confirmed from subscribers "
                                   "where email = %s", (email_clean,))])
                sid, is_active, is_confirmed = existing[0]
                if is_active and is_confirmed:
                    st.info("This email is already subscribed. To change your settings, unsubscribe "
                            "using the link in an alert email, then sign up again.")
                    sid = None
                else:
                    recent = write([("select 1 from events where subscriber_id = %s "
                                     "and event_type = 'confirmation_sent' "
                                     "and created_at > now() - interval '10 minutes'", (sid,))])
                    if recent:
                        st.info("A confirmation email was sent a moment ago. "
                                "Please check your inbox and spam folder.")
                        sid = None
                    else:
                        upd = write([("""update subscribers set city = %s, aqi_limit = %s,
                                         consent = true, active = true, confirmed = false,
                                         confirm_token = md5(random()::text || clock_timestamp()::text)
                                         where subscriber_id = %s returning confirm_token""",
                                      (sub_city, int(limit), sid))])
                        ctoken = upd[0][0]
            if sid:
                send_confirmation(email_clean, ctoken)
                write([("insert into events (subscriber_id, event_type, detail) values (%s, 'confirmation_sent', %s)",
                        (sid, f"{sub_city} above {limit}"))])
                st.success("Almost done. We sent you a confirmation email. Click the link in it to "
                           "start receiving alerts (check your spam folder if you do not see it).")
        except Exception:
            st.error("Sorry, signup failed. Please try again in a few minutes.")

# ---- weather ----
st.subheader("Current weather")
weather = q("""
select distinct on (city) city, ts, temperature, humidity, wind_speed, wind_dir
from weather
order by city, ts desc
""")
weather = weather.rename(columns={"temperature": "Temp (C)", "humidity": "Humidity (%)",
                                  "wind_speed": "Wind (km/h)", "wind_dir": "Wind dir (deg)"})
st.dataframe(weather.drop(columns=["ts"]), hide_index=True, width="stretch")

# ---- 7-day trend ----
st.subheader("AQI over the last 7 days")
trend = q("""
select city, date_trunc('hour', ts) as hour, round(avg(aqi)) as aqi
from station_aqi
where aqi is not null and ts > now() - interval '7 days'
group by 1, 2
order by 2
""")
trend["aqi"] = pd.to_numeric(trend["aqi"])
st.plotly_chart(px.line(trend, x="hour", y="aqi", color="city"), width="stretch")

# ---- hour of day pattern ----
st.subheader("Typical day: average PM2.5 by hour (Indian time)")
c1, c2 = st.columns([1, 2])
selected = c1.selectbox("City", sorted(latest["city"].unique()), key="pattern_city")
skip = c1.checkbox("Exclude the Diwali period (7-10 Nov)", value=False)
extra = ""
if skip:
    extra = "and (r.ts at time zone 'Asia/Kolkata')::date not between '2026-11-07' and '2026-11-10'"
pattern = q(f"""
select extract(hour from r.ts at time zone 'Asia/Kolkata')::int as hour_ist,
       round(avg(r.pm25)::numeric, 0) as avg_pm25
from readings r
join stations s using (station_id)
where s.city = %s and r.pm25 is not null {extra}
group by 1
order by 1
""", params=(selected,))
pattern["avg_pm25"] = pd.to_numeric(pattern["avg_pm25"])
c2.plotly_chart(px.bar(pattern, x="hour_ist", y="avg_pm25"), width="stretch")

# ---- station table ----
st.subheader("All stations")
table = latest[["city", "name", "aqi", "category", "pm25", "pm10", "hours_ago", "freshness"]] \
    .sort_values(["city", "aqi"], ascending=[True, False])
st.dataframe(table, hide_index=True, width="stretch")
st.caption("Data: OpenAQ (government monitors) and Open-Meteo (weather). "
           "Fresh = reading within 6 hours. Some readings during Diwali were extremely high.")
