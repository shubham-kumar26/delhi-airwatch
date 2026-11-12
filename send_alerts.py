"""Send AQI alert emails to subscribers.

Usage:
  python send_alerts.py        send real alerts
  python send_alerts.py dry    show who would be emailed, send nothing
  python send_alerts.py test   send one test email to the sending address
"""
import os
import sys
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import psycopg2
from dotenv import load_dotenv

load_dotenv(r"C:\Users\HP\.env")  # laptop only; on GitHub the secrets are used instead

MODE = sys.argv[1] if len(sys.argv) > 1 else "live"
GMAIL = (os.getenv("GMAIL_ADDRESS") or "").strip()
PASSWORD = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
DASHBOARD = (os.getenv("DASHBOARD_URL") or "").strip().rstrip("/")
DB = (os.getenv("DATABASE_URL") or "").strip()
COOLDOWN_HOURS = 12
IST = timezone(timedelta(hours=5, minutes=30))


def category(a):
    return ("Good" if a <= 50 else "Satisfactory" if a <= 100 else "Moderate" if a <= 200
            else "Poor" if a <= 300 else "Very Poor" if a <= 400 else "Severe")


def smtp_connect():
    server = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60)
    server.login(GMAIL, PASSWORD)
    return server


def build_email(to_addr, city, aqi, limit, newest, token):
    unsub = f"{DASHBOARD}/?unsubscribe={token}" if DASHBOARD else "(unsubscribe link unavailable)"
    msg = EmailMessage()
    msg["Subject"] = f"AirWatch alert: {city} AQI is {aqi} ({category(aqi)})"
    msg["From"] = f"AirWatch India <{GMAIL}>"
    msg["To"] = to_addr
    msg.set_content(
        f"Hello,\n\n"
        f"The air quality in {city} is now above your limit.\n\n"
        f"  Current AQI: {aqi} ({category(aqi)})\n"
        f"  Your limit:  {limit}\n"
        f"  Newest reading: {newest:%d %b %Y, %I:%M %p} IST\n\n"
        f"Consider reducing outdoor exercise and keeping windows closed while it stays high. "
        f"Air is usually cleaner in the afternoon than late evening and night.\n\n"
        f"See live data: {DASHBOARD or 'the AirWatch India dashboard'}\n\n"
        f"The AQI shown is a PM-based AQI (PM2.5 and PM10, CPCB method), updated hourly "
        f"with a delay of a few hours. It is for information only and is not medical advice.\n\n"
        f"You are receiving this because you signed up for alerts. "
        f"Unsubscribe at any time: {unsub}\n"
    )
    return msg


# ---------- test mode ----------
if MODE == "test":
    if not GMAIL or not PASSWORD:
        raise SystemExit("GMAIL_ADDRESS or GMAIL_APP_PASSWORD is missing.")
    msg = EmailMessage()
    msg["Subject"] = "AirWatch India test email"
    msg["From"] = f"AirWatch India <{GMAIL}>"
    msg["To"] = GMAIL
    msg.set_content("If you can read this, email sending works.")
    with smtp_connect() as s:
        s.send_message(msg)
    print("Test email sent. Check the inbox (and spam folder) of the sending address.")
    raise SystemExit(0)

# ---------- live / dry mode ----------
if not DB:
    raise SystemExit("DATABASE_URL is missing.")
if MODE == "live" and (not GMAIL or not PASSWORD):
    raise SystemExit("GMAIL_ADDRESS or GMAIL_APP_PASSWORD is missing.")

conn = psycopg2.connect(DB, connect_timeout=30)
cur = conn.cursor()

# current AQI per city, using each station's newest row with an AQI (within 12 hours)
cur.execute("""
select city, round(avg(aqi))::int as aqi, max(ts) as newest
from (
  select distinct on (station_id) station_id, city, ts, aqi
  from station_aqi
  where aqi is not null
  order by station_id, ts desc
) t
where ts > now() - interval '12 hours'
group by city
""")
city_aqi = {c: (a, n) for c, a, n in cur.fetchall()}
print("Current AQI by city:", {c: v[0] for c, v in city_aqi.items()})

cur.execute("""
select subscriber_id, email, city, aqi_limit, unsubscribe_token, last_alert_at
from subscribers
where active and consent
""")
subs = cur.fetchall()
print("Active subscribers:", len(subs))

now = datetime.now(timezone.utc)
due = []
for sid, email, city, limit, token, last_alert in subs:
    if city not in city_aqi:
        continue
    aqi, newest = city_aqi[city]
    if aqi <= limit:
        continue
    if last_alert and now - last_alert < timedelta(hours=COOLDOWN_HOURS):
        continue
    due.append((sid, email, city, aqi, limit, newest, token))

print("Alerts due:", len(due))
if MODE == "dry":
    for sid, email, city, aqi, limit, newest, token in due:
        print(f"  would email subscriber {sid}: {city} AQI {aqi} above limit {limit}")
    raise SystemExit(0)

sent = failed = 0
if due:
    server = smtp_connect()
    for sid, email, city, aqi, limit, newest, token in due:
        try:
            server.send_message(build_email(email, city, aqi, limit,
                                            newest.astimezone(IST), token))
            cur.execute("update subscribers set last_alert_at = now() where subscriber_id = %s", (sid,))
            cur.execute("insert into events (subscriber_id, event_type, detail) values (%s, 'alert_sent', %s)",
                        (sid, f"{city} AQI {aqi} above {limit}"))
            conn.commit()
            sent += 1
        except Exception as e:
            conn.rollback()
            failed += 1
            print(f"  failed for subscriber {sid}: {str(e)[:100]}")
    server.quit()

cur.execute(
    "insert into pipeline_runs (source, rows_loaded, rows_flagged, status, note) values (%s,%s,%s,%s,%s)",
    ("email_alerts", sent, failed, "ok" if failed == 0 else "partial", f"{len(subs)} subscribers"),
)
conn.commit()
cur.close()
conn.close()
print(f"Emails sent: {sent} | failed: {failed}")
