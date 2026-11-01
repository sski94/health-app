#!/usr/bin/env python3
"""Turn one raw Ultrahuman day response into a health_uh_daily row and a health_uh_night row."""
import json, os, re, sys, glob


def first(lst, **kw):
    for x in lst or []:
        if all(x.get(k) == v for k, v in kw.items()):
            return x
    return None


def num(x):
    return None if x is None else x


def extract(path):
    date = os.path.basename(path)[:10]
    return extract_obj(date, json.load(open(path)))


def extract_obj(date, d):
    data = d.get("data") or {}
    md = {m["type"]: m.get("object") or {} for m in data.get("metric_data", [])}
    sl = md.get("Sleep") or md.get("sleep") or {}
    qm = {q.get("type"): q for q in sl.get("quick_metrics", []) or []}
    qt = {q.get("type"): q for q in sl.get("quick_metrics_tiled", []) or []}

    def stage(t):
        s = first(sl.get("sleep_stages"), type=t)
        return None if not s else round(s.get("stage_time", 0) / 60)

    cycles = (sl.get("sleep_cycles") or {}).get("cycles") or []
    full = sum(1 for c in cycles if c.get("cycleType") == "complete")
    part = sum(1 for c in cycles if c.get("cycleType") == "partial")
    legend = (sl.get("sleep_cycles") or {}).get("legend") or []
    for item in legend:
        t = (item.get("title") or "").lower()
        m = re.search(r"\d+", t)
        if m and "full" in t:
            full = int(m.group())
        if m and "partial" in t:
            part = int(m.group())

    steps_o = md.get("steps") or {}
    steps = steps_o.get("total")
    if steps is None and steps_o.get("values"):
        steps = sum(v.get("value", 0) or 0 for v in steps_o["values"])

    def v(key):
        o = md.get(key) or {}
        return o.get("value")

    sp = sl.get("spo2")
    spo2 = sp.get("value") if isinstance(sp, dict) else None

    bands = {}
    for t, q in qt.items():
        bands[t] = {"tag": q.get("tag"), "color": q.get("tag_color")}
    for item in sl.get("summary", []) or []:
        bands["contrib:" + str(item.get("title"))] = {
            "state": item.get("state"), "label": item.get("state_title"), "score": item.get("score")}
    for key in ("total_sleep", "time_in_bed", "restorative_sleep"):
        b = (sl.get(key) or {}).get("badge")
        if b:
            bands["badge:" + key] = {"tag": b.get("text"), "id": b.get("id")}
    for key in ("rem_sleep", "deep_sleep", "sleep_efficiency", "temperature_deviation", "average_body_temperature"):
        c = (sl.get(key) or {}).get("contributor")
        if c is not None:
            bands["score:" + key] = round(c, 1)
    st = sl.get("score_trend") or {}
    if st:
        bands["sleep_score_avgs"] = {k: st.get(k) for k in ("day_avg", "week_avg", "month_avg", "year_avg")}

    def qval(t):
        q = qm.get(t)
        return None if not q else q.get("value")

    tib = (sl.get("time_in_bed") or {}).get("minutes")
    row = {
        "date": date,
        "sleep_score": (sl.get("sleep_score") or {}).get("score"),
        "recovery_index": v("recovery_index"),
        "movement_index": v("movement_index"),
        "sleep_rhr": v("sleep_rhr"),
        "avg_hrv": v("avg_sleep_hrv") if v("avg_sleep_hrv") is not None else qval("avg_hrv"),
        "avg_hr": qval("avg_hr"),
        "total_sleep_min": (sl.get("total_sleep") or {}).get("minutes"),
        "time_in_bed_min": tib,
        "efficiency": (sl.get("sleep_efficiency") or {}).get("percentage"),
        "restorative_pct": (sl.get("restorative_sleep") or {}).get("percentage"),
        "deep_min": stage("deep_sleep"), "light_min": stage("light_sleep"),
        "rem_min": stage("rem_sleep"), "awake_min": stage("awake"),
        "cycles_full": full if (cycles or legend) else None,
        "cycles_partial": part if (cycles or legend) else None,
        "steps": steps, "vo2_max": v("vo2_max"), "spo2": spo2,
        "temp_dev_c": (sl.get("temperature_deviation") or {}).get("celsius"),
        "avg_body_temp_c": (sl.get("average_body_temperature") or {}).get("celsius"),
        "bedtime_start": sl.get("bedtime_start"), "bedtime_end": sl.get("bedtime_end"),
        "timezone": data.get("latest_time_zone"),
        "bands": bands,
    }
    for k in ("temp_dev_c", "avg_body_temp_c"):
        if row[k] is not None:
            row[k] = round(row[k], 2)

    def series(key):
        g = sl.get(key)
        pts = (g or {}).get("data") if isinstance(g, dict) else None
        return [[p["timestamp"], p["value"]] for p in pts or [] if "timestamp" in p]

    stages = [[s["start"], s["end"], s["type"]] for s in (sl.get("sleep_graph") or {}).get("data", []) or []]
    night = {"date": date, "hr": series("hr_graph") or None, "hrv": series("hrv_graph") or None,
             "temp": series("temp_graph") or None, "stages": stages or None}
    has_sleep = bool(sl) and row["sleep_score"] is not None or row["total_sleep_min"] is not None
    return row, night, has_sleep
