"""Send AQI alert emails to subscribers using the Brevo email API.

Usage:
  python send_alerts.py        send real alerts
  python send_alerts.py dry    show who would be emailed, send nothing
  python send_alerts.py test   send one test email to the sender address
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv(r"C:\Users\HP\.env")  # laptop only; on GitHub the secrets are used instead

MODE = sys.argv[1] if len(sys.argv) > 1 else "live"
API_KEY = (os.getenv("BREVO_API_KEY") or "").strip()
SENDER_EMAIL = (os.getenv("SENDER_EMAIL") or "").strip()
SENDER_NAME = (os.getenv("SENDER_NAME") or "AirWatch India").strip()
DASHBOARD = (os.getenv("DASHBOARD_URL") or "").strip().rstrip("/")
DB = (os.getenv("DATABASE_URL") or "").strip()
COOLDOWN_HOURS = 12
IST = timezone(timedelta(hours=5, minutes=30))


def category(a):
    return ("Good" if a <= 50 else "Satisfactory" if a <= 100 else "Moderate" if a <= 200
            else "Poor" if a <= 300 else "Very Poor" if a <= 400 else "Severe")


def advice(aqi):
    """General precautions for each CPCB AQI band (not medical advice)."""
    if aqi <= 50:
        tips = ["Air quality is good. A great time for outdoor exercise and fresh air.",
                "Open your windows to ventilate your home."]
    elif aqi <= 100:
        tips = ["Air quality is acceptable. Most people can carry on as normal.",
                "People with asthma or lung or heart conditions may notice mild discomfort."]
    elif aqi <= 200:
        tips = ["Children, older adults and people with asthma, lung or heart conditions "
                "should reduce long or heavy outdoor exertion.",
                "Keep any prescribed medicines handy.",
                "Everyone else can stay active outdoors but may prefer shorter sessions."]
    elif aqi <= 300:
        tips = ["Everyone should cut down on prolonged or heavy outdoor activity. "
                "Prefer indoor exercise.",
                "Keep windows closed during the worst hours and use an air purifier if you have one.",
                "If you go out, a well-fitted N95/FFP2-type mask helps. Ordinary cloth or surgical "
                "masks do not filter fine particles well.",
                "Children, older adults and people with heart or lung conditions should be extra careful."]
    elif aqi <= 400:
        tips = ["Avoid outdoor exercise. Limit time outside to what is necessary.",
                "Wear a well-fitted N95/FFP2-type mask if you go out. Ordinary cloth or surgical "
                "masks do not filter fine particles well.",
                "Keep windows closed and use an air purifier if you have one.",
                "Children, older adults and people with heart or lung conditions should stay "
                "indoors as much as possible."]
    else:
        tips = ["Health alert for everyone. Stay indoors as much as you can and avoid all "
                "outdoor exercise.",
                "Keep windows and doors closed. Use an air purifier, or spend time in one "
                "cleaner room, if you can.",
                "For any essential trip outside, wear a well-fitted N95/FFP2-type mask.",
                "If you or someone you care for has trouble breathing, chest pain or other severe "
                "symptoms, contact a doctor or emergency services."]
    return "\n".join("- " + t for t in tips)


def send_email(to_addr, subject, text):
    """Send one email through Brevo. Raises an error if Brevo refuses it."""
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": API_KEY, "content-type": "application/json",
                 "accept": "application/json"},
        json={
            "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
            "to": [{"email": to_addr}],
            "subject": subject,
            "textContent": text,
        },
        timeout=30,
    )
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"Brevo error {r.status_code}: {r.text[:200]}")


def alert_text(city, aqi, limit, newest, token):
    unsub = f"{DASHBOARD}/?unsubscribe={token}" if DASHBOARD else "(unsubscribe link unavailable)"
    return (
        f"Hello,\n\n"
        f"The air quality in {city} is now above your limit.\n\n"
        f"  Current AQI: {aqi} ({category(aqi)})\n"
        f"  Your limit:  {limit}\n"
        f"  Newest reading: {newest:%d %b %Y, %I:%M %p} IST\n\n"
        f"What you can do at this level:\n{advice(aqi)}\n\n"
        f"See live data: {DASHBOARD or 'the AirWatch India dashboard'}\n\n"
        f"The AQI shown is a PM-based AQI (PM2.5 and PM10, CPCB method), updated hourly "
        f"with a delay of a few hours. The tips are general precautions based on the CPCB AQI "
        f"categories. This email is for information only and is not medical advice.\n\n"
        f"You are receiving this because you signed up for alerts. "
        f"Unsubscribe at any time: {unsub}\n"
    )


# ---------- test mode ----------
if MODE == "test":
    if not API_KEY or not SENDER_EMAIL:
        raise SystemExit("BREVO_API_KEY or SENDER_EMAIL is missing.")
    send_email(SENDER_EMAIL, "AirWatch India test email",
               "If you can read this, email sending works.")
    print("Test email sent. Check the inbox (and spam folder) of the sender address.")
    raise SystemExit(0)

# ---------- live / dry mode ----------
if not DB:
    raise SystemExit("DATABASE_URL is missing.")
if MODE == "live" and (not API_KEY or not SENDER_EMAIL):
    raise SystemExit("BREVO_API_KEY or SENDER_EMAIL is missing.")

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
for sid, email, city, aqi, limit, newest, token in due:
    try:
        send_email(email, f"AirWatch alert: {city} AQI is {aqi} ({category(aqi)})",
                   alert_text(city, aqi, limit, newest.astimezone(IST), token))
        cur.execute("update subscribers set last_alert_at = now() where subscriber_id = %s", (sid,))
        cur.execute("insert into events (subscriber_id, event_type, detail) values (%s, 'alert_sent', %s)",
                    (sid, f"{city} AQI {aqi} above {limit}"))
        conn.commit()
        sent += 1
    except Exception as e:
        conn.rollback()
        failed += 1
        print(f"  failed for subscriber {sid}: {str(e)[:150]}")

cur.execute(
    "insert into pipeline_runs (source, rows_loaded, rows_flagged, status, note) values (%s,%s,%s,%s,%s)",
    ("email_alerts", sent, failed, "ok" if failed == 0 else "partial", f"{len(subs)} subscribers"),
)
conn.commit()
cur.close()
conn.close()
print(f"Emails sent: {sent} | failed: {failed}")
