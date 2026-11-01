#!/usr/bin/env python3
"""Pull Ultrahuman daily metrics and upsert them into Supabase.

Environment:
  UH_TOKEN, UH_EMAIL                   Ultrahuman Partner API credentials
  SUPABASE_URL, SUPABASE_SERVICE_KEY   Supabase project URL and service-role key
  START_DATE      first day to backfill (default 2025-02-01)
  REFRESH_DAYS    always re-fetch this many most recent days (default 3), because the
                  ring syncs late and a day is not final until a day or two afterwards

Behaviour: fetches every day from START_DATE that is missing in health_uh_daily, plus the
last REFRESH_DAYS days, then upserts. Safe to run repeatedly.

Local test without network:  python3 uh_sync.py --from-dir <folder of YYYY-MM-DD.json>
(prints what it would write and does not touch Supabase).
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from uh_extract import extract_obj

UH_URL = "https://partner.ultrahuman.com/api/v1/metrics"
HERE_TZ = ZoneInfo("Europe/London")


def http(method, url, headers=None, body=None, retries=4):
    data = None if body is None else json.dumps(body).encode()
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            return e.code, e.read().decode(errors="replace")[:300]
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise
    return 0, None


def iso(ts):
    return None if ts is None else dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


INT_COLS = ("sleep_score", "recovery_index", "movement_index", "sleep_rhr", "avg_hrv", "avg_hr",
            "total_sleep_min", "time_in_bed_min", "efficiency", "restorative_pct", "deep_min",
            "light_min", "rem_min", "awake_min", "cycles_full", "cycles_partial", "steps",
            "vo2_max", "spo2")


def prep_daily(row):
    r = dict(row)
    for k in INT_COLS:                      # the API sometimes returns 4310.0 for whole numbers
        v = r.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            r[k] = int(round(v))
        elif isinstance(v, str):
            try:
                r[k] = int(round(float(v)))
            except ValueError:
                r[k] = None
    r["bedtime_start"] = iso(r.get("bedtime_start"))
    r["bedtime_end"] = iso(r.get("bedtime_end"))
    r["fetched_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return r


class Supa:
    def __init__(self, url, key):
        self.url = url.rstrip("/") + "/rest/v1"
        self.h = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}

    def existing_dates(self):
        out, offset = set(), 0
        while True:
            s, body = http("GET", f"{self.url}/health_uh_daily?select=date&order=date&limit=1000&offset={offset}", self.h)
            if s != 200:
                raise SystemExit(f"Could not read existing dates: HTTP {s} {body}")
            out.update(x["date"] for x in body)
            if len(body) < 1000:
                return out
            offset += 1000

    def trips(self):
        s, body = http("GET", f"{self.url}/health_trips?select=start_date,end_date,tz", self.h)
        return body if s == 200 and isinstance(body, list) else []

    def upsert(self, table, rows, conflict):
        for i in range(0, len(rows), 25):
            chunk = rows[i:i + 25]
            h = dict(self.h, Prefer="resolution=merge-duplicates,return=minimal")
            s, body = http("POST", f"{self.url}/{table}?on_conflict={conflict}", h, chunk)
            if s not in (200, 201, 204):
                raise SystemExit(f"Upsert into {table} failed: HTTP {s} {body}")


def trip_tz(trips, d):
    """Timezone the night dated d was slept in. Nights are dated by the day you wake, so the night before
    departure is at home and the night before the return day is abroad: start < d <= end."""
    ds = d.isoformat() if hasattr(d, "isoformat") else d
    for t in trips:
        if t["start_date"] < ds <= t["end_date"]:
            return t["tz"]
    return None


def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)


def main():
    if "--from-dir" in sys.argv:
        folder = sys.argv[sys.argv.index("--from-dir") + 1]
        n = 0
        for name in sorted(os.listdir(folder)):
            if name.endswith(".json"):
                row, night, ok = extract_obj(name[:10], json.load(open(os.path.join(folder, name))))
                prep_daily(row)
                n += 1
        print(f"dry run: parsed {n} day files without errors")
        return

    token, email = os.environ["UH_TOKEN"], os.environ["UH_EMAIL"]
    supa = Supa(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    start = dt.date.fromisoformat(os.environ.get("START_DATE", "2025-02-01"))
    refresh = int(os.environ.get("REFRESH_DAYS", "3"))
    today = dt.datetime.now(HERE_TZ).date()

    trips = supa.trips()
    have = supa.existing_dates()
    wanted = [d for d in daterange(start, today)
              if d.isoformat() not in have or (today - d).days < refresh]
    print(f"{len(have)} days already stored; fetching {len(wanted)} day(s)", flush=True)

    daily, night, failed = [], [], []
    for d in wanted:
        q = urllib.parse.urlencode({"email": email, "date": d.isoformat()})
        s, body = http("GET", f"{UH_URL}?{q}", {"Authorization": token})
        if s != 200 or not isinstance(body, dict):
            failed.append((d.isoformat(), s))
            print(f"  {d} failed: HTTP {s}", flush=True)
            continue
        row, n, ok = extract_obj(d.isoformat(), body)
        row["timezone"] = trip_tz(trips, d) or row.get("timezone") or "Europe/London"
        daily.append(prep_daily(row))
        if ok and (n["hr"] or n["hrv"] or n["temp"] or n["stages"]):
            night.append(n)
        if len(daily) >= 50:                      # flush as we go so a long backfill is resumable
            supa.upsert("health_uh_daily", daily, "date")
            supa.upsert("health_uh_night", night, "date")
            print(f"  saved through {d}", flush=True)
            daily, night = [], []
        time.sleep(0.25)
    if daily:
        supa.upsert("health_uh_daily", daily, "date")
        supa.upsert("health_uh_night", night, "date")
    print(f"done. {len(failed)} failed day(s): {failed[:10]}", flush=True)
    if failed and len(failed) == len(wanted):
        sys.exit(1)


if __name__ == "__main__":
    main()
