import os, requests, psycopg2
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv(r"C:\Users\HP\.env")
DB = os.getenv("DATABASE_URL").strip()

CITIES = {
    "Delhi-NCR": (28.6139, 77.2090),
    "Mumbai": (19.0760, 72.8777),
    "Pune": (18.5204, 73.8567),
    "Kolkata": (22.5726, 88.3639),
    "Bengaluru": (12.9716, 77.5946),
}

now = datetime.now(timezone.utc)
conn = psycopg2.connect(DB, connect_timeout=30)
cur = conn.cursor()
loaded, errors = 0, []

for city, (lat, lon) in CITIES.items():
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m",
                "past_days": 30, "forecast_days": 1, "timezone": "UTC",
            },
            timeout=30,
        )
        r.raise_for_status()
        h = r.json()["hourly"]
    except Exception as e:
        errors.append(f"{city}: {str(e)[:80]}")
        continue

    n = 0
    for t, temp, hum, ws, wd in zip(h["time"], h["temperature_2m"],
                                    h["relative_humidity_2m"],
                                    h["wind_speed_10m"], h["wind_direction_10m"]):
        ts = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        if ts > now:
            continue  # store only observed hours, not forecast hours
        cur.execute(
            """insert into weather (city, ts, temperature, humidity, wind_speed, wind_dir)
               values (%s,%s,%s,%s,%s,%s)
               on conflict (city, ts) do update set
                 temperature = excluded.temperature, humidity = excluded.humidity,
                 wind_speed = excluded.wind_speed, wind_dir = excluded.wind_dir""",
            (city, ts, temp, hum, ws, wd),
        )
        n += 1
    conn.commit()
    loaded += n
    print(city, "->", n, "hourly rows")

cur.execute(
    "insert into pipeline_runs (source, rows_loaded, rows_flagged, status, note) values (%s,%s,%s,%s,%s)",
    ("open_meteo", loaded, 0, "ok" if not errors else "partial", "; ".join(errors)[:500]),
)
conn.commit()

cur.execute("select count(*) from readings")
print("Total readings in database:", cur.fetchone()[0])
cur.execute("select count(*) from weather")
print("Total weather rows in database:", cur.fetchone()[0])
cur.close()
conn.close()
print("Errors:", len(errors))
