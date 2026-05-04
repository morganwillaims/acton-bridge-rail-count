import os
import json
import time
from datetime import datetime

import requests


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

ACB_TIPLOC = "ACBG"
LOOP_SECONDS = int(os.environ.get("ROUTE_BACKFILL_LOOP_SECONDS", "21300"))  # 5h55m
SLEEP_SECONDS = int(os.environ.get("ROUTE_BACKFILL_SLEEP_SECONDS", "60"))


STATIC_TIPLOC_NAMES = {
    "ACBG": "Acton Bridge",
    "CREWE": "Crewe",
    "CREWECS": "Crewe C.S.",
    "CREWMD": "Crewe C.S.",
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
    "DIRFDR2": "Daventry DRS",
    "DIRFTFL": "Daventry Int Recep Term",
    "COATDRS": "Coatbridge Down Refuge Siding",
    "STOKMAR": "Stoke Marcroft Engineering",
    "CLITGBR": "Clitheroe Castle Cement GBRf",
    "BREDFHH": "Bredbury R.T.S. Freightliner Heavy Haul",
    "BRNDFHH": "Brindle Heath R.T.S. Flhh",
    "BTNUNMD": "Barton Under Needwood Rsmd",
    "238302": "Arpley Sidings",
    "320011": "Avonmouth Hanson Siding GBRf",
    "229115": "Folly Lane ICI Sidings",
    "MNTSDGS": "Mountsorrel Sdgs",
}


def headers(return_representation=True):
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation" if return_representation else "return=minimal",
    }


def rest(table):
    return f"{SUPABASE_URL}/rest/v1/{table}"


def is_bad_name(value):
    text = str(value or "").strip()
    return (
        not text
        or text.lower() in {"unknown", "null", "undefined", "route pending"}
        or text.upper() == "NAME GOES HERE"
        or text.isdigit()
    )


def displayable_name(value, fallback=None, name_map=None):
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


def active_on_date(service, running_date):
    start = service.get("schedule_start_date")
    end = service.get("schedule_end_date") or start

    if start and running_date < start:
        return False
    if end and running_date > end:
        return False

    days = service.get("days_runs")
    if days and len(days) == 7:
        d = datetime.strptime(running_date, "%Y-%m-%d").date()
        return days[d.weekday()] == "1"

    return True


def get_tiploc_name_map():
    try:
        response = requests.get(
            rest("tiploc_names") + "?select=tiploc,name",
            headers=headers(),
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
        result = {
            row["tiploc"]: row["name"]
            for row in rows
            if row.get("tiploc") and row.get("name")
        }
        result.update(STATIC_TIPLOC_NAMES)
        return result
    except Exception as exc:
        print(f"TIPLOC name map warning: {exc}", flush=True)
        return dict(STATIC_TIPLOC_NAMES)


def upsert_static_tiplocs():
    rows = [{"tiploc": key, "name": value} for key, value in STATIC_TIPLOC_NAMES.items()]
    response = requests.post(
        rest("tiploc_names") + "?on_conflict=tiploc",
        headers={**headers(return_representation=False), "Prefer": "resolution=merge-duplicates,return=minimal"},
        data=json.dumps(rows),
        timeout=60,
    )
    if response.status_code not in (200, 201, 204):
        print(f"TIPLOC upsert warning: {response.status_code} {response.text}", flush=True)


def fetch_recent_movements():
    url = (
        rest("station_movements")
        + "?source=eq.Network%20Rail%20TRUST"
        + "&select=id,running_date,actual_time,train_id,origin,destination,toc,planned_time,platform,created_at"
        + "&order=created_at.desc"
        + "&limit=500"
    )
    response = requests.get(url, headers=headers(), timeout=60)
    response.raise_for_status()
    return response.json()


def needs_backfill(row):
    origin = str(row.get("origin") or "").strip()
    destination = str(row.get("destination") or "").strip()
    platform = str(row.get("platform") or "").strip()

    return (
        is_bad_name(origin)
        or is_bad_name(destination)
        or origin in STATIC_TIPLOC_NAMES
        or destination in STATIC_TIPLOC_NAMES
        or origin.isdigit()
        or destination.isdigit()
        or not platform
        or platform in {"—", "-"}
    )


def fetch_schedules_for_headcode(headcode):
    response = requests.get(
        rest("schedule_services") + f"?signalling_id=eq.{headcode}&select=*,schedule_locations(*)",
        headers=headers(),
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


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
    clean_update = {key: value for key, value in update.items() if value not in (None, "")}
    if not clean_update:
        return False

    response = requests.patch(
        rest("station_movements") + f"?id=eq.{movement_id}",
        headers=headers(return_representation=False),
        data=json.dumps(clean_update),
        timeout=60,
    )
    if response.status_code not in (200, 204):
        raise RuntimeError(f"Patch failed {movement_id}: {response.status_code} {response.text}")
    return True


def run_once():
    upsert_static_tiplocs()
    name_map = get_tiploc_name_map()
    movements = [row for row in fetch_recent_movements() if needs_backfill(row)]

    print(f"Route backfill check: {len(movements)} recent rows need checking", flush=True)

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

        origin = displayable_name(
            service.get("origin_name"),
            fallback=displayable_name(service.get("origin_tiploc"), name_map=name_map),
            name_map=name_map,
        )
        destination = displayable_name(
            service.get("destination_name"),
            fallback=displayable_name(service.get("destination_tiploc"), name_map=name_map),
            name_map=name_map,
        )

        update = {
            "origin": origin,
            "destination": destination,
            "toc": movement.get("toc") or service.get("atoc_code"),
            "planned_time": movement.get("planned_time") or loc.get("pass_time") or loc.get("departure") or loc.get("arrival"),
            "platform": movement.get("platform") or loc.get("platform"),
        }

        if patch_movement(movement["id"], update):
            updated += 1
            print(
                f"Updated {movement.get('actual_time')} {headcode}: {origin} -> {destination} platform={update.get('platform') or '—'} score={score}m",
                flush=True,
            )

    print(f"Route backfill pass complete. Updated {updated} rows.", flush=True)
    return updated


def main():
    started = time.time()
    pass_number = 0

    print(
        f"Starting route backfill loop for {LOOP_SECONDS}s with {SLEEP_SECONDS}s gap",
        flush=True,
    )

    while True:
        pass_number += 1
        print(f"--- Route backfill pass #{pass_number} ---", flush=True)

        try:
            run_once()
        except Exception as exc:
            print(f"Route backfill pass failed: {exc}", flush=True)

        elapsed = time.time() - started
        if elapsed + SLEEP_SECONDS >= LOOP_SECONDS:
            break

        time.sleep(SLEEP_SECONDS)

    print("Route backfill loop finished cleanly.", flush=True)


if __name__ == "__main__":
    main()
