import os, time, requests, psycopg2
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv(r"C:\Users\HP\.env")
H = {"X-API-Key": os.getenv("OPENAQ_API_KEY").strip()}
DB = os.getenv("DATABASE_URL").strip()
WANTED = {"pm25", "pm10", "no2", "o3", "co", "so2"}
MAX_AGE_H = 48

def get(url):
    for _ in range(3):
        time.sleep(1.1)  # stay under the rate limit
        r = requests.get(url, headers=H, timeout=30)
        if r.status_code == 429:
            time.sleep(30)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("rate limited")

def clean(param, v):
    """Return (value, flagged). Negative or impossible values become None."""
    if v is None:
        return None, False
    if v < 0 or (param == "pm25" and v > 1000) or (param == "pm10" and v > 1500):
        return None, True
    return v, False

conn = psycopg2.connect(DB, connect_timeout=30)
cur = conn.cursor()
cur.execute("select station_id from stations order by station_id")
ids = [r[0] for r in cur.fetchall()]

now = datetime.now(timezone.utc)
loaded = flagged = stations_ok = 0
errors = []

for sid in ids:
    try:
        loc = get(f"https://api.openaq.org/v3/locations/{sid}")["results"][0]
        sensor_param = {s["id"]: s["parameter"]["name"] for s in loc["sensors"]}
        latest = get(f"https://api.openaq.org/v3/locations/{sid}/latest")["results"]
    except Exception as e:
        errors.append(f"{sid}: {str(e)[:80]}")
        continue

    rows = {}  # hour -> {parameter: value}
    for item in latest:
        param = sensor_param.get(item["sensorsId"])
        if param not in WANTED:
            continue
        ts = datetime.fromisoformat(item["datetime"]["utc"].replace("Z", "+00:00"))
        if now - ts > timedelta(hours=MAX_AGE_H):
            continue  # old or dead sensor
        ts = ts.replace(minute=0, second=0, microsecond=0)
        val, bad = clean(param, item["value"])
        flagged += bad
        rows.setdefault(ts, {})[param] = (val, bad)

    if rows:
        stations_ok += 1
    for ts, vals in rows.items():
        flag = "bad_value" if any(b for _, b in vals.values()) else None
        g = lambda p: vals[p][0] if p in vals else None
        cur.execute(
            """insert into readings (station_id, ts, pm25, pm10, no2, o3, co, so2, quality_flag)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (station_id, ts) do update set
                 pm25 = coalesce(excluded.pm25, readings.pm25),
                 pm10 = coalesce(excluded.pm10, readings.pm10),
                 no2  = coalesce(excluded.no2,  readings.no2),
                 o3   = coalesce(excluded.o3,   readings.o3),
                 co   = coalesce(excluded.co,   readings.co),
                 so2  = coalesce(excluded.so2,  readings.so2),
                 quality_flag = excluded.quality_flag""",
            (sid, ts, g("pm25"), g("pm10"), g("no2"), g("o3"), g("co"), g("so2"), flag),
        )
        loaded += 1
    conn.commit()
    print(sid, "->", len(rows), "hourly rows")

status = "ok" if not errors else "partial"
cur.execute(
    "insert into pipeline_runs (source, rows_loaded, rows_flagged, status, note) values (%s,%s,%s,%s,%s)",
    ("openaq_latest", loaded, flagged, status, "; ".join(errors)[:500]),
)
conn.commit()
cur.close()
conn.close()
print("Stations with fresh data:", stations_ok, "of", len(ids))
print("Rows loaded:", loaded, "| flagged values:", flagged, "| errors:", len(errors))
for e in errors:
    print("  ", e)
