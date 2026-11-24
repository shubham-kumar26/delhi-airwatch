"""Make and save PM2.5 forecasts for every city.

For each city it forecasts the next 30 hours after the newest reading (30, because readings
arrive a few hours late and we want to cover the next 24 hours from now). It saves four
forecasts per hour so their live accuracy can be compared later:
  persistence      the last observed value
  same_hour_yday   the same hour yesterday (two days ago beyond 24 hours ahead)
  prophet          Prophet on log(PM2.5) with a daily cycle and a Diwali event
  blend            the average of prophet and same_hour_yday  (the one the dashboard shows)

Settings (environment variables):
  MIN_HOURS_BETWEEN  skip if a forecast was already made this recently (default 5)
  FORCE              set to 1 to forecast anyway
Usage: python make_forecast.py
"""
import logging
import os
import sys
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

load_dotenv(r"C:\Users\HP\.env")  # laptop only; on GitHub the secrets are used instead

DB = (os.getenv("DATABASE_URL") or "").strip()
MIN_HOURS_BETWEEN = float(os.getenv("MIN_HOURS_BETWEEN", "5"))
FORCE = os.getenv("FORCE", "0") == "1"
HORIZON = 30
DIWALI = pd.Timestamp("2026-11-08")   # check the date on a calendar


def load_series(conn):
    """Hourly average PM2.5 per city on a UTC hourly index (missing hours = NaN)."""
    cur = conn.cursor()
    cur.execute("""
        select s.city, date_trunc('hour', r.ts) as ts, avg(r.pm25) as pm25
        from readings r
        join stations s using (station_id)
        where r.pm25 is not null
        group by 1, 2
        order by 1, 2
    """)
    rows = cur.fetchall()
    cur.close()
    df = pd.DataFrame(rows, columns=["city", "ts", "pm25"])
    df["pm25"] = pd.to_numeric(df["pm25"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None)
    out = {}
    for city, g in df.groupby("city"):
        out[city] = g.groupby("ts")["pm25"].mean().sort_index().asfreq("h")
    return out


def prophet_forecast(history, targets):
    from prophet import Prophet
    for name in ("cmdstanpy", "prophet"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
        logging.getLogger(name).disabled = True
    hist = history.dropna()
    train = pd.DataFrame({"ds": hist.index, "y": np.log1p(hist.values)})
    holidays = pd.DataFrame({"holiday": "diwali", "ds": [DIWALI],
                             "lower_window": -1, "upper_window": 2})
    m = Prophet(daily_seasonality=True, weekly_seasonality=False, yearly_seasonality=False,
                changepoint_prior_scale=0.1, holidays=holidays, uncertainty_samples=0)
    m.fit(train)
    fc = m.predict(pd.DataFrame({"ds": targets}))
    return np.clip(np.expm1(fc["yhat"].values), 0, None)


def forecast_city(s, use_prophet=True):
    """Return a DataFrame (target_ts, model, pred) for the next HORIZON hours after the newest reading."""
    history = s.dropna()
    last_obs = history.index.max()
    targets = pd.date_range(last_obs + pd.Timedelta(hours=1), periods=HORIZON, freq="h")
    last_value = float(history.iloc[-1])

    preds = {"persistence": np.full(HORIZON, last_value)}
    lags = np.where(np.arange(1, HORIZON + 1) <= 24, 24, 48)   # beyond 24 h ahead, use two days ago
    yday = np.array([s.get(t - pd.Timedelta(hours=int(l)), np.nan) for t, l in zip(targets, lags)],
                    dtype=float)
    preds["same_hour_yday"] = np.where(np.isnan(yday), last_value, yday)
    if use_prophet:
        preds["prophet"] = prophet_forecast(s, targets)
        preds["blend"] = (preds["prophet"] + preds["same_hour_yday"]) / 2
    rows = []
    for model, p in preds.items():
        for t, v in zip(targets, p):
            rows.append((t, model, float(v)))
    return pd.DataFrame(rows, columns=["target_ts", "model", "pred"]), last_obs


def main():
    if not DB:
        raise SystemExit("DATABASE_URL is missing.")
    conn = psycopg2.connect(DB, connect_timeout=30)
    cur = conn.cursor()
    cur.execute("select max(made_at) from forecasts")
    last_made = cur.fetchone()[0]
    now = datetime.now(timezone.utc)
    if last_made and not FORCE and (now - last_made) < timedelta(hours=MIN_HOURS_BETWEEN):
        print(f"A forecast was made {(now - last_made).total_seconds() / 3600:.1f} hours ago. "
              f"Skipping (set FORCE=1 to run anyway).")
        return

    series = load_series(conn)
    made_at = now.replace(minute=0, second=0, microsecond=0)
    total, problems = 0, []
    for city, s in series.items():
        try:
            if s.dropna().empty or (pd.Timestamp(now.replace(tzinfo=None)) - s.dropna().index.max()) > pd.Timedelta(hours=48):
                problems.append(f"{city}: newest reading is more than 48 hours old, skipped")
                continue
            fc, last_obs = forecast_city(s)
            rows = [(city, made_at, t.to_pydatetime().replace(tzinfo=timezone.utc), m, p)
                    for t, m, p in fc.itertuples(index=False)]
            execute_values(cur, """
                insert into forecasts (city, made_at, target_ts, model, predicted_pm25)
                values %s
                on conflict (city, made_at, target_ts, model) do nothing""", rows)
            conn.commit()
            total += len(rows)
            nxt = fc[fc["model"] == "blend"].head(6)["pred"].round(0).astype(int).tolist()
            print(f"{city}: newest reading {last_obs:%d %b %H:%M} UTC, saved {len(rows)} rows, "
                  f"next blend values {nxt}")
        except Exception as e:
            conn.rollback()
            problems.append(f"{city}: {str(e)[:100]}")
            print(f"{city}: ERROR {str(e)[:100]}")

    cur.execute("insert into pipeline_runs (source, rows_loaded, rows_flagged, status, note) "
                "values (%s,%s,%s,%s,%s)",
                ("forecast", total, len(problems), "ok" if not problems else "partial",
                 "; ".join(problems)[:500]))
    conn.commit()
    cur.close()
    conn.close()
    print(f"Forecast rows saved: {total} | problems: {len(problems)}")
    for p in problems:
        print("  ", p)


if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    main()
