#!/usr/bin/env python3
"""Load activities into Supabase.

Sources (both folders live in the repo, which is private):
  data/fit/<activityId>_ACTIVITY.fit   Garmin originals. Summary row plus a 1 Hz stream
                                        (heart rate, cadence, speed, altitude, distance).
                                        GPS is deliberately not stored in the database.
  data/strava/*.json                    Strava activity summaries saved by the HealthSync plugin.

Environment: SUPABASE_URL, SUPABASE_SERVICE_KEY
Local test:  python3 activities_sync.py --dry-run
"""
import datetime as dt
import glob
import json
import os
import re
import sys

import fitdecode

from uh_sync import Supa, http

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")


def r1(x, n=1):
    return None if x is None else round(float(x), n)


def parse_fit(path):
    act_id = re.match(r"(\d+)", os.path.basename(path)).group(1)
    sess, recs = None, []
    with fitdecode.FitReader(path) as fr:
        for f in fr:
            if not isinstance(f, fitdecode.FitDataMessage):
                continue
            if f.name == "session" and sess is None:
                sess = {x.name: x.value for x in f.fields}
            elif f.name == "record":
                recs.append({x.name: x.value for x in f.fields})
    if not sess or not recs:
        raise ValueError("no session/record data")
    start = sess["start_time"]
    sport = str(sess.get("sport") or "other")
    running = sport == "running"
    t0 = recs[0]["timestamp"]
    s = {k: [] for k in ("t", "hr", "cadence", "speed", "altitude", "distance")}
    for r in recs:
        s["t"].append(int((r["timestamp"] - t0).total_seconds()))
        s["hr"].append(r.get("heart_rate"))
        c = r.get("cadence")
        if c is not None and running:
            c = (c + (r.get("fractional_cadence") or 0)) * 2     # steps per minute, both feet
        s["cadence"].append(None if c is None else round(c))
        s["speed"].append(r1(r.get("enhanced_speed", r.get("speed")), 2))
        s["altitude"].append(r1(r.get("enhanced_altitude", r.get("altitude")), 1))
        s["distance"].append(r1(r.get("distance"), 1))
    for k in list(s):
        if k != "t" and all(v is None for v in s[k]):
            s[k] = None
    hour = start.hour
    part = "Morning" if hour < 12 else "Afternoon" if hour < 18 else "Evening"
    cad = sess.get("avg_running_cadence") or sess.get("avg_cadence")
    row = {
        "id": f"garmin:{act_id}", "source": "garmin", "sport": sport,
        "name": f"{part} {sport.replace('_', ' ')}",
        "start_time": start.isoformat(),
        "distance_m": r1(sess.get("total_distance"), 1),
        "moving_time_s": int(round(sess.get("total_timer_time") or 0)),
        "elapsed_time_s": int(round(sess.get("total_elapsed_time") or 0)),
        "avg_hr": sess.get("avg_heart_rate"), "max_hr": sess.get("max_heart_rate"),
        "elevation_gain_m": sess.get("total_ascent"),
        "avg_cadence": (cad * 2 if (cad and running) else cad),
        "device": "Garmin Instinct 2", "extra": {},
    }
    stream = dict(s, activity_id=row["id"], latlng=None)
    if running:
        row["extra"] = zone2_metrics(s)
    return row, stream


ZONE = (125, 145)          # Zone 2 heart-rate band from the training plan
RUN_CADENCE = 145          # steps per minute at or above which a second counts as running
WARMUP_S = 150


def zone2_metrics(s):
    """Per-run fitness measures for the Zone 2 plan, from the 1 Hz stream."""
    t, hr, cad, spd = s["t"], s["hr"], s["cadence"], s["speed"]
    if not hr or not cad or not spd:
        return {}
    n = len(t)
    in_zone = run = 0
    z2_speed_sum = z2_n = 0
    best = cur = 0
    for i in range(n):
        if t[i] < WARMUP_S or hr[i] is None or cad[i] is None:
            cur = 0
            continue
        running = cad[i] >= RUN_CADENCE
        run += running
        if ZONE[0] <= hr[i] <= ZONE[1]:
            in_zone += 1
            if running and spd[i]:
                z2_speed_sum += spd[i]
                z2_n += 1
        if running and hr[i] <= ZONE[1]:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    body = max(1, sum(1 for x in t if x >= WARMUP_S))
    out = {"zone2_share": round(in_zone / body, 3), "run_share": round(run / body, 3),
           "longest_zone2_run_s": best}
    if z2_n >= 60:
        out["zone2_pace_s_km"] = round(1000 / (z2_speed_sum / z2_n))
    return out


def parse_strava(path):
    a = json.load(open(path))
    return {
        "id": f"strava:{a['id']}", "source": "strava",
        "sport": a.get("sport_type") or a.get("type"), "name": a.get("name"),
        "start_time": a["start_date"],
        "distance_m": a.get("distance"), "moving_time_s": a.get("moving_time"),
        "elapsed_time_s": a.get("elapsed_time"),
        "avg_hr": a.get("average_heartrate"), "max_hr": a.get("max_heartrate"),
        "elevation_gain_m": a.get("total_elevation_gain"), "avg_cadence": None,
        "device": a.get("device_name"),
        "extra": {k: a[k] for k in ("average_speed", "average_watts", "kilojoules", "external_id") if k in a},
    }


def main():
    dry = "--dry-run" in sys.argv
    acts, streams, errors = [], [], []
    for p in sorted(glob.glob(os.path.join(ROOT, "fit", "*.fit"))):
        try:
            row, st = parse_fit(p)
            acts.append(row)
            streams.append(st)
        except Exception as e:
            errors.append((os.path.basename(p), str(e)))
    for p in sorted(glob.glob(os.path.join(ROOT, "strava", "*.json"))):
        try:
            acts.append(parse_strava(p))
        except Exception as e:
            errors.append((os.path.basename(p), str(e)))
    print(f"parsed {len(acts)} activities ({len(streams)} with streams), {len(errors)} errors", flush=True)
    for e in errors[:10]:
        print("  error:", e, flush=True)
    if dry:
        for a in acts[:3] + [x for x in acts if x["source"] == "garmin"][:2]:
            print({k: a[k] for k in ("id", "sport", "name", "start_time", "distance_m", "moving_time_s", "avg_hr")})
        return

    supa = Supa(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    supa.upsert("health_activities", acts, "id")
    print(f"upserted {len(acts)} activities", flush=True)
    s, have = http("GET", f"{supa.url}/health_activity_streams?select=activity_id&limit=5000", supa.h)
    have = {x["activity_id"] for x in have} if s == 200 else set()
    new = [x for x in streams if x["activity_id"] not in have]
    for st in new:                                  # one row per request: rows are large
        h = dict(supa.h, Prefer="resolution=merge-duplicates,return=minimal")
        status, body = http("POST", f"{supa.url}/health_activity_streams?on_conflict=activity_id", h, [st])
        if status not in (200, 201, 204):
            raise SystemExit(f"stream upsert failed for {st['activity_id']}: HTTP {status} {body}")
        print(f"  stream saved: {st['activity_id']} ({len(st['t'])} points)", flush=True)
    print(f"done. {len(new)} new stream(s), {len(streams) - len(new)} already stored", flush=True)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
