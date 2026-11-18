#!/usr/bin/env python3
"""Build a daily king tide dataset for Tableau Pulse.

Pulls NOAA CO-OPS high and low tide predictions and tidal datums for each
station in STATIONS, keeps each day's highest tide, measures it against a
normal high tide (Mean Higher High Water), and writes data/tide_alerts_daily.csv.

Each row is dated by its alert date, ALERT_LEAD_DAYS before the tide, so that
Tableau Pulse's "today" value is the tide coming in a few days.

The script is idempotent: every run rebuilds the full window, from
DAYS_BEHIND days ago to DAYS_AHEAD days ahead. It uses only the Python
standard library, so it runs on a bare GitHub Actions runner with no installs.
"""

import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

# Station ID -> display name and alert threshold, in feet above Mean Higher
# High Water. Add a station here to add it to the dataset.
STATIONS = {
    "8725577": {"name": "Port Boca Grande", "threshold_ft": 0.6},
}

ALERT_LEAD_DAYS = 3
DAYS_BEHIND = 365  # history, so Pulse can compare against earlier periods
DAYS_AHEAD = 365  # predictions are known well in advance

DATA_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
DATUMS_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/{station_id}/datums.json"

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "tide_alerts_daily.csv"

MAX_RETRIES = 4


def get_json(url: str) -> dict:
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                return json.load(resp)
        except Exception as exc:  # noqa: BLE001 - retry any transient failure
            last_error = exc
            wait = 2 ** (attempt + 1)
            print(f"  attempt {attempt + 1} failed ({exc}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def mhhw_above_mllw(station_id: str) -> float:
    """Gap between Mean Higher High Water and Mean Lower Low Water, in feet."""
    payload = get_json(DATUMS_URL.format(station_id=station_id))
    if payload.get("units") != "feet":
        raise ValueError(f"{station_id}: expected datums in feet, got {payload.get('units')}")
    values = {d["name"]: d["value"] for d in payload["datums"]}
    return values["MHHW"] - values["MLLW"]


def fetch_highs(station_id: str, start: date, end: date) -> list[dict]:
    params = urllib.parse.urlencode(
        {
            "product": "predictions",
            "station": station_id,
            "begin_date": start.strftime("%Y%m%d"),
            "end_date": end.strftime("%Y%m%d"),
            "datum": "MLLW",
            "interval": "hilo",
            "units": "english",
            # Local standard/daylight time, so dates match what an owner sees
            "time_zone": "lst_ldt",
            "format": "json",
            "application": "tide_pulse",
        }
    )
    payload = get_json(f"{DATA_URL}?{params}")
    if "error" in payload:
        raise RuntimeError(f"{station_id}: {payload['error'].get('message')}")
    return [p for p in payload["predictions"] if p["type"] == "H"]


def main() -> int:
    today = date.today()
    start = today - timedelta(days=DAYS_BEHIND)
    end = today + timedelta(days=DAYS_AHEAD)
    print(f"Building {start} .. {end} for {len(STATIONS)} station(s)")

    rows = []
    for station_id, station in STATIONS.items():
        gap_ft = mhhw_above_mllw(station_id)
        print(f"{station['name']} ({station_id}): MHHW is {gap_ft:.2f} ft above MLLW")
        time.sleep(1)  # NOAA asks callers to space out requests

        # {tide date -> (height above MHHW in ft, local time of that tide)}
        peaks: dict[str, tuple[float, str]] = {}
        for p in fetch_highs(station_id, start, end):
            day, clock = p["t"].split(" ")
            above_ft = round(float(p["v"]) - gap_ft, 3)
            if day not in peaks or above_ft > peaks[day][0]:
                peaks[day] = (above_ft, clock)
        time.sleep(1)

        threshold_ft = station["threshold_ft"]
        for day in sorted(peaks):
            above_ft, clock = peaks[day]
            tide_date = date.fromisoformat(day)
            alert_date = tide_date - timedelta(days=ALERT_LEAD_DAYS)
            rows.append(
                {
                    "Record ID": f"{alert_date:%Y%m%d}-{station_id}",
                    "Alert Date": alert_date.isoformat(),
                    "Tide Date": day,
                    "Station": station["name"],
                    "Station ID": station_id,
                    "Peak Tide Time": clock,
                    "Peak Above Normal High (in)": round(above_ft * 12, 1),
                    "Threshold (in)": round(threshold_ft * 12, 1),
                    "Above Threshold": "Yes" if above_ft >= threshold_ft else "No",
                }
            )

    if not rows:
        print("ERROR: no data returned by the API", file=sys.stderr)
        return 1

    rows.sort(key=lambda r: (r["Alert Date"], r["Station ID"]))
    alerts = sum(r["Above Threshold"] == "Yes" for r in rows)
    print(f"Writing {len(rows)} rows ({alerts} days above threshold) to {OUTPUT_PATH}")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())