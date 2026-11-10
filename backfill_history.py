import os, sys, time, requests, psycopg2
from psycopg2.extras import execute_batch
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv(r"C:\Users\HP\.env")
H = {"X-API-Key": os.getenv("OPENAQ_API_KEY").strip()}
DB = os.getenv("DATABASE_URL").strip()
DAYS = int(os.getenv("DAYS", "30"))
WANTED = {"pm25", "pm10", "no2", "o3", "co", "so2"}
LIMIT_STATIONS = int(sys.argv[1]) if len(sys.argv) > 1 else None
BASE = "https://api.openaq.org/v3"

def get(url, params=None):
    for _ in range(4):
        time.sleep(1.1)  # stay under the rate limit
        r = requests.get(url, headers=H, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(40)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("rate limited")

def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

now = datetime.now(timezone.utc)
start = now - timedelta(days=DAYS)
conn = psycopg2.connect(DB, connect_timeout=30)
cur = conn.cursor()
cur.execute("select station_id from stations order by station_id")
ids = [r[0] for r in cur.fetchall()]
if LIMIT_STATIONS:
    ids = ids[:LIMIT_STATIONS]

total = flagged = 0
errors = []

for sid in ids:
    try:
        loc = get(f"{BASE}/locations/{sid}")["results"][0]
        names = {s["id"]: s["parameter"]["name"] for s in loc["sensors"]}
        latest = get(f"{BASE}/locations/{sid}/latest")["results"]
        fresh = {}  # only sensors that reported in the last 72 hours
        for it in latest:
            p = names.get(it["sensorsId"])
            if p in WANTED and now - parse(it["datetime"]["utc"]) <= timedelta(hours=72):
                fresh[it["sensorsId"]] = p

        data, bad = {}, set()
        for sensor_id, p in fresh.items():
            page = 1
            while True:
                res = get(f"{BASE}/sensors/{sensor_id}/hours", {
                    "datetime_from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "datetime_to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "limit": 1000, "page": page,
                })["results"]
                for it in res:
                    t = ((it.get("period") or {}).get("datetimeFrom") or {}).get("utc")
                    v = it.get("value")
                    if not t or v is None:
                        continue
                    ts = parse(t).replace(minute=0, second=0, microsecond=0)
                    if v < 0 or (p == "pm25" and v > 1000) or (p == "pm10" and v > 1500):
                        bad.add(ts)
                        flagged += 1
                        continue
                    data.setdefault(ts, {})[p] = v
                if len(res) < 1000:
                    break
                page += 1

        rows = [(sid, ts, d.get("pm25"), d.get("pm10"), d.get("no2"), d.get("o3"),
                 d.get("co"), d.get("so2"), "bad_value" if ts in bad else None)
                for ts, d in data.items()]
        execute_batch(cur, """
            insert into readings (station_id, ts, pm25, pm10, no2, o3, co, so2, quality_flag)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (station_id, ts) do update set
              pm25 = coalesce(excluded.pm25, readings.pm25),
              pm10 = coalesce(excluded.pm10, readings.pm10),
              no2  = coalesce(excluded.no2,  readings.no2),
              o3   = coalesce(excluded.o3,   readings.o3),
              co   = coalesce(excluded.co,   readings.co),
              so2  = coalesce(excluded.so2,  readings.so2),
              quality_flag = coalesce(readings.quality_flag, excluded.quality_flag)
        """, rows, page_size=500)
        conn.commit()
        total += len(rows)
        print(sid, "->", len(rows), "hourly rows from", len(fresh), "sensors")
    except Exception as e:
        conn.rollback()
        errors.append(f"{sid}: {str(e)[:80]}")
        print(sid, "ERROR:", str(e)[:100])

cur.execute("insert into pipeline_runs (source, rows_loaded, rows_flagged, status, note) values (%s,%s,%s,%s,%s)",
            ("openaq_backfill", total, flagged, "ok" if not errors else "partial", "; ".join(errors)[:500]))
conn.commit()
cur.execute("select count(*), min(ts), max(ts) from readings")
print("Readings in database (rows, earliest, latest):", cur.fetchone())
print("Rows loaded this run:", total, "| flagged:", flagged, "| errors:", len(errors))
cur.close()
conn.close()
