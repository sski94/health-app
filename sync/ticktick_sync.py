#!/usr/bin/env python3
"""Pull the TickTick habit "酒1杯" (one pint) check-ins and upsert them into Supabase.

Environment:
  TICKTICK_TOKEN                        TickTick API token (web app: Settings > Account & Security > API Token)
  SUPABASE_URL, SUPABASE_SERVICE_KEY    Supabase project URL and service-role key
  TICKTICK_MCP_URL  default https://mcp.ticktick.com (TickTick's own MCP server, Streamable HTTP, Bearer token)
  HABIT_NAME      default "酒1杯" (matched after width normalisation, so full-width digits also match)
  HABIT_ID        optional; skips the name lookup
  START_DATE      first day to backfill (default 2025-11-27)
  REFRESH_DAYS    always re-fetch this many most recent days (default 14), because a day is
                  often ticked the following morning or edited later

Mapping (as the habit was used): status 2 (completed) = under a pint -> over_pint = false
                                 status 1 (not completed) = over a pint -> over_pint = true
                                 status 0 (unmarked) = skipped, so it stays unknown.

Safe to run repeatedly. Local test without network:  python3 ticktick_sync.py --from-file checkins.json
where the file holds [{"date": "YYYY-MM-DD", "status": 1|2}, ...] (prints, does not write).
"""
import datetime as dt
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

CHUNK_DAYS = 400  # the connector returned a whole year in one call


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


def norm(s):
    return unicodedata.normalize("NFKC", s or "").strip().casefold()


def stamp_to_date(stamp):
    s = str(stamp)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def rows_from_checkins(checkins):
    """checkins: iterable of dicts with stamp + status. Later entries win."""
    out = {}
    for c in checkins:
        st = c.get("status")
        if c.get("stamp") is None or st not in (1, 2):
            continue
        out[stamp_to_date(c["stamp"])] = {"date": stamp_to_date(c["stamp"]), "over_pint": st == 1, "source": "ticktick"}
    return [out[d] for d in sorted(out)]


class Supa:
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def latest_date(self):
        st, js = http("GET", f"{self.url}/rest/v1/health_drinks?select=date&order=date.desc&limit=1", self.h)
        if st != 200:
            raise SystemExit(f"Supabase read failed: {st} {js}")
        return js[0]["date"] if js else None

    def upsert(self, rows):
        for i in range(0, len(rows), 200):
            h = dict(self.h, Prefer="resolution=merge-duplicates,return=minimal")
            st, js = http("POST", f"{self.url}/rest/v1/health_drinks?on_conflict=date", h, rows[i:i + 200])
            if st not in (200, 201, 204):
                raise SystemExit(f"Supabase upsert failed: {st} {js}")


class TickTick:
    """Minimal MCP (Streamable HTTP) client for TickTick's hosted MCP server."""

    def __init__(self, token, url):
        self.url = url
        self.h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                  "Accept": "application/json, text/event-stream"}
        self.n = 0
        self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "health-sync", "version": "1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect=False)

    def _post(self, msg, expect=True):
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), method="POST", headers=self.h)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    sid = r.headers.get("Mcp-Session-Id")
                    if sid:
                        self.h["Mcp-Session-Id"] = sid
                    raw = r.read().decode()
                    ctype = r.headers.get("Content-Type", "")
                break
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise SystemExit("TickTick rejected the token (401). Create a new API token (Settings > Account & Security > API Token) and update the TICKTICK_TOKEN secret.")
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise SystemExit(f"TickTick MCP request failed: {e.code} {e.read().decode(errors='replace')[:300]}")
            except urllib.error.URLError:
                if attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise
        if not expect or not raw.strip():
            return None
        if "text/event-stream" in ctype:
            for line in raw.splitlines():
                if line.startswith("data:") and line[5:].strip():
                    msg = json.loads(line[5:].strip())
                    if msg.get("id") == self.n:
                        return msg
            raise SystemExit("TickTick MCP: no response in event stream")
        return json.loads(raw)

    def _rpc(self, method, params):
        self.n += 1
        msg = self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        if msg is None or "error" in msg:
            raise SystemExit(f"TickTick MCP error on {method}: {msg and msg.get('error')}")
        return msg["result"]

    def call(self, tool, args):
        res = self._rpc("tools/call", {"name": tool, "arguments": args})
        if res.get("isError"):
            raise SystemExit(f"TickTick tool {tool} failed: {json.dumps(res)[:300]}")
        out = []
        for part in res.get("content", []):
            if part.get("type") == "text":
                try:
                    out.append(json.loads(part["text"]))
                except ValueError:
                    pass
        return out

    def habit_id(self, name):
        habits = []
        for o in self.call("list_habits", {}):
            habits.extend(o if isinstance(o, list) else [o])
        hits = [h for h in habits if isinstance(h, dict) and norm(h.get("name")) == norm(name)]
        if not hits:
            raise SystemExit(f"No habit named {name!r}. Habits seen: {[h.get('name') for h in habits if isinstance(h, dict)]}")
        return hits[0]["id"]

    def checkins(self, hid, d0, d1):
        out, cur = [], d0
        while cur <= d1:
            end = min(cur + dt.timedelta(days=CHUNK_DAYS - 1), d1)
            for o in self.call("get_habit_checkins", {"habit_ids": [hid], "from_stamp": int(f"{cur:%Y%m%d}"),
                                                       "to_stamp": int(f"{end:%Y%m%d}"), "client_timezone": "Europe/London"}):
                out.extend(flatten_checkins(o, hid))
            cur = end + dt.timedelta(days=1)
        return out


def flatten_checkins(js, hid):
    """Accepts {"checkins": {id: [..]}}, [{"habitId":..,"checkins":[..]}], or a flat list."""
    if isinstance(js, dict) and isinstance(js.get("checkins"), dict):
        return list(js["checkins"].get(hid, []))
    if isinstance(js, dict) and isinstance(js.get("checkins"), list):
        return list(js["checkins"])
    out = []
    if isinstance(js, list):
        for e in js:
            if isinstance(e, dict) and "checkins" in e:
                if e.get("habitId") in (None, hid):
                    out.extend(e["checkins"])
            elif isinstance(e, dict) and "stamp" in e:
                out.append(e)
    return out


def main():
    if "--from-file" in sys.argv:
        data = json.load(open(sys.argv[sys.argv.index("--from-file") + 1]))
        checkins = [{"stamp": int(x["date"].replace("-", "")), "status": x["status"]} for x in data]
        rows = rows_from_checkins(checkins)
        over = sum(r["over_pint"] for r in rows)
        print(f"dry run: {len(rows)} days, {over} over a pint, {len(rows) - over} under; first {rows[0]['date']}, last {rows[-1]['date']}")
        return

    token = os.environ["TICKTICK_TOKEN"]
    supa = Supa(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    tt = TickTick(token, os.environ.get("TICKTICK_MCP_URL", "https://mcp.ticktick.com"))
    hid = os.environ.get("HABIT_ID") or tt.habit_id(os.environ.get("HABIT_NAME", "酒1杯"))

    start = dt.date.fromisoformat(os.environ.get("START_DATE", "2025-11-27"))
    refresh = int(os.environ.get("REFRESH_DAYS", "14"))
    latest = supa.latest_date()
    d0 = start if latest is None else max(start, dt.date.fromisoformat(latest) - dt.timedelta(days=refresh))
    d1 = dt.date.today()
    print(f"Fetching check-ins {d0} to {d1}", flush=True)
    rows = rows_from_checkins(tt.checkins(hid, d0, d1))
    if not rows:
        print("No marked days returned in that range; nothing to write.")
        return
    supa.upsert(rows)
    over = sum(r["over_pint"] for r in rows)
    print(f"Upserted {len(rows)} days ({over} over a pint), {rows[0]['date']} to {rows[-1]['date']}", flush=True)


if __name__ == "__main__":
    main()
