import os
import json
from datetime import date, datetime, timedelta

import requests


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

ACB_TIPLOC = "ACBG"

STATIC_TIPLOC_NAMES = {
    "CREWE": "Crewe",
    "LVRPLSH": "Liverpool South Parkway",
    "LVRPLSL": "Liverpool Lime Street",
    "ALERTN": "Allerton",
    "EDGHDHS": "Edge Hill Depot",
    "EDGHILL": "Edge Hill",
    "BHAMNWS": "Birmingham New Street",
    "WRGTNBQ": "Warrington Bank Quay",
    "WVRMPTN": "Wolverhampton",
    "GLGC": "Glasgow Central",
    "EDINBUR": "Edinburgh",
    "EUSTON": "London Euston",
    "PRST": "Preston",
    "LANCSTR": "Lancaster",
    "OXNHOLE": "Oxenholme Lake District",
    "CARLILE": "Carlisle",
    "MNCRPIC": "Manchester Piccadilly",
    "MNCRVIC": "Manchester Victoria",
    "LVRPGBF": "Liverpool Biomass Terminal",
    "DRAXGBR": "Drax Power Station",
    "ACBG": "Acton Bridge",
}


def headers(return_representation: bool = True) -> dict:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation" if return_representation else "return=minimal",
    }


def rest(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def is_bad_name(value) -> bool:
    text = str(value or "").strip()
    return not text or text.lower() in {"unknown", "null", "undefined"} or text.isdigit()


def readable(value, fallback=None, name_map=None):
    text = str(value or "").strip()
    if name_map and text in name_map:
        return name_map[text]
    if text in STATIC_TIPLOC_NAMES:
        return STATIC_TIPLOC_NAMES[text]
    if is_bad_name(text):
        return fallback or "Unknown"
    return text


def time_to_minutes(value):
    if not value:
        return None
    text = str(value).strip().upper().replace(":", "").replace("H", "")
    if len(text) < 4 or not text[:4].isdigit():
        return None
    return int(text[:2]) * 60 + int(text[2:4])


def active_on_date(service, running_date: str) -> bool:
    start = service.get("schedule_start_date")
    end = service.get("schedule_end_date") or start

    if start and running_date < start:
        return False
    if end and running_date > end:
        return False

    days = service.get("days_runs")
    if days and len(days) == 7:
        d = datetime.strptime(running_date, "%Y-%m-%d").date()
        # Monday = index 0, Sunday = index 6
        return days[d.weekday()] == "1"

    return True


def get_tiploc_name_map() -> dict:
    try:
        r = requests.get(rest("tiploc_names") + "?select=tiploc,name", headers=headers(), timeout=30)
        r.raise_for_status()
        rows = r.json()
        result = {row["tiploc"]: row["name"] for row in rows if row.get("tiploc") and row.get("name")}
        result.update(STATIC_TIPLOC_NAMES)
        return result
    except Exception as exc:
        print(f"TIPLOC name map warning: {exc}", flush=True)
        return dict(STATIC_TIPLOC_NAMES)


def fetch_recent_movements():
    # Recent rows where route is missing/Unknown/numeric/TIPLOC-like.
    url = (
        rest("station_movements")
        + "?source=eq.Network%20Rail%20TRUST"
        + "&select=id,running_date,actual_time,train_id,origin,destination,toc,planned_time,platform"
        + "&order=created_at.desc"
        + "&limit=250"
    )
    r = requests.get(url, headers=headers(), timeout=60)
    r.raise_for_status()
    rows = r.json()

    needs = []
    for row in rows:
        if is_bad_name(row.get("origin")) or is_bad_name(row.get("destination")):
            needs.append(row)
            continue
        # TIPLOC-looking all-caps codes are okay as a fallback, but try to improve them.
        if str(row.get("origin", "")).isupper() or str(row.get("destination", "")).isupper():
            needs.append(row)

    return needs


def fetch_schedules_for_headcode(headcode: str):
    url = (
        rest("schedule_services")
        + f"?signalling_id=eq.{headcode}"
        + "&select=*,schedule_locations(*)"
    )
    r = requests.get(url, headers=headers(), timeout=60)
    r.raise_for_status()
    return r.json()


def best_match(movement, schedules):
    actual_mins = time_to_minutes(movement.get("actual_time"))
    if actual_mins is None:
        return None

    best = None
    best_score = 999999

    for service in schedules:
        if not active_on_date(service, movement["running_date"]):
            continue

        acb_locations = [
            loc for loc in service.get("schedule_locations", [])
            if loc.get("tiploc") == ACB_TIPLOC
        ]
        if not acb_locations:
            continue

        loc = acb_locations[0]
        booked = loc.get("pass_time") or loc.get("departure") or loc.get("arrival")
        booked_mins = time_to_minutes(booked)
        if booked_mins is None:
            continue

        score = min(
            abs(actual_mins - booked_mins),
            abs((actual_mins + 1440) - booked_mins),
            abs(actual_mins - (booked_mins + 1440)),
        )

        if score < best_score:
            best_score = score
            best = (service, loc, score)

    if best and best[2] <= 240:
        return best

    return None


def patch_movement(movement_id, update):
    r = requests.patch(
        rest("station_movements") + f"?id=eq.{movement_id}",
        headers=headers(return_representation=False),
        data=json.dumps(update),
        timeout=60,
    )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Patch failed {movement_id}: {r.status_code} {r.text}")


def main():
    name_map = get_tiploc_name_map()
    movements = fetch_recent_movements()
    print(f"Found {len(movements)} recent rows needing route check", flush=True)

    updated = 0
    cache = {}

    for movement in movements:
        headcode = movement.get("train_id")
        if not headcode:
            continue

        if headcode not in cache:
            cache[headcode] = fetch_schedules_for_headcode(headcode)

        match = best_match(movement, cache[headcode])
        if not match:
            continue

        service, loc, score = match

        origin = readable(
            service.get("origin_name"),
            fallback=readable(service.get("origin_tiploc"), name_map=name_map),
            name_map=name_map,
        )
        destination = readable(
            service.get("destination_name"),
            fallback=readable(service.get("destination_tiploc"), name_map=name_map),
            name_map=name_map,
        )

        if origin == "Unknown" or destination == "Unknown":
            continue

        update = {
            "origin": origin,
            "destination": destination,
            "toc": movement.get("toc") or service.get("atoc_code"),
            "planned_time": movement.get("planned_time") or loc.get("pass_time") or loc.get("departure") or loc.get("arrival"),
            "platform": movement.get("platform") or loc.get("platform"),
        }

        patch_movement(movement["id"], update)
        updated += 1
        print(
            f"Updated {movement.get('actual_time')} {headcode}: {origin} -> {destination} score={score}m",
            flush=True,
        )

    print(f"Route backfill complete. Updated {updated} rows.", flush=True)


if __name__ == "__main__":
    main()
