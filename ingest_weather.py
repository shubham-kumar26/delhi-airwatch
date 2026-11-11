import os, time, requests, psycopg2
from psycopg2.extras import execute_batch
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv(r"C:\Users\HP\.env")  # laptop only; GitHub uses its secrets
DB = os.getenv("DATABASE_URL").strip()
PAST_DAYS = int(os.getenv("PAST_DAYS", "30"))

CITIES = {
    "Delhi-NCR": (28.6139, 77.2090),
    "Mumbai": (19.0760, 72.8777),
    "Pune": (18.5204, 73.8567),
    "Kolkata": (22.5726, 88.3639),
    "Bengaluru": (12.9716, 77.5946),
}

def fetch(lat, lon):
    last = None
    for attempt in range(4):
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m",
                "past_days": PAST_DAYS, "forecast_days": 1, "timezone": "UTC",
            },
            timeout=60,
        )
        last = r.status_code
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(15 * (attempt + 1))  # wait longer each time
            continue
        r.raise_for_status()
        return r.json()["hourly"]
    raise RuntimeError(f"gave up after retries (last status {last})")

now = datetime.now(timezone.utc)
conn = psycopg2.connect(DB, connect_timeout=30)
cur = conn.cursor()
loaded, errors = 0, []

for city, (lat, lon) in CITIES.items():
    try:
        h = fetch(lat, lon)
        rows = []
        for t, temp, hum, ws, wd in zip(h["time"], h["temperature_2m"],
                                        h["relative_humidity_2m"],
                                        h["wind_speed_10m"], h["wind_direction_10m"]):
            ts = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
            if ts > now:
                continue  # only observed hours, not forecast hours
            rows.append((city, ts, temp, hum, ws, wd))
        execute_batch(cur, """
            insert into weather (city, ts, temperature, humidity, wind_speed, wind_dir)
            values (%s,%s,%s,%s,%s,%s)
            on conflict (city, ts) do update set
              temperature = excluded.temperature, humidity = excluded.humidity,
              wind_speed = excluded.wind_speed, wind_dir = excluded.wind_dir
        """, rows, page_size=500)
        conn.commit()
        loaded += len(rows)
        print(city, "->", len(rows), "hourly rows")
    except Exception as e:
        conn.rollback()
        errors.append(f"{city}: {str(e)[:100]}")
        print(city, "ERROR:", str(e)[:100])
    time.sleep(2)

cur.execute(
    "insert into pipeline_runs (source, rows_loaded, rows_flagged, status, note) values (%s,%s,%s,%s,%s)",
    ("open_meteo", loaded, 0, "ok" if not errors else "partial", "; ".join(errors)[:500]),
)
conn.commit()
cur.execute("select count(*) from weather")
print("Total weather rows in database:", cur.fetchone()[0])
print("Errors:", len(errors))
cur.close()
conn.close()
