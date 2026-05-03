import gzip
import json
import os
from datetime import date, datetime
from typing import Any

import requests


NETWORK_RAIL_USERNAME = os.environ["NETWORK_RAIL_USERNAME"]
NETWORK_RAIL_PASSWORD = os.environ["NETWORK_RAIL_PASSWORD"]

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

ACB_TIPLOC = "ACBG"

SCHEDULE_URL = (
    "https://publicdatafeeds.networkrail.co.uk/ntrod/CifFileAuthenticate"
    "?type=CIF_ALL_FULL_DAILY&day=toc-full"
)


def supabase_headers(return_representation: bool = False) -> dict[str, str]:
    prefer = "resolution=merge-duplicates"
    prefer += ",return=representation" if return_representation else ",return=minimal"

    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def postgrest_url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def clean_time(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def time_to_minutes(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip().upper().replace("H", "")
    if len(text) < 4 or not text[:4].isdigit():
        return None
    return int(text[:2]) * 60 + int(text[2:4])


def active_on_date(start: str | None, end: str | None, target: date, days_runs: str | None) -> bool:
    if not start:
        return False

    start_date = datetime.strptime(start, "%Y-%m-%d").date()
    end_date = datetime.strptime(end, "%Y-%m-%d").date() if end else start_date

    if not (start_date <= target <= end_date):
        return False

    # schedule_days_runs: character 1 = Monday, character 7 = Sunday
    if days_runs and len(days_runs) == 7:
        return days_runs[target.weekday()] == "1"

    return True


def train_type_from_headcode(headcode: str | None) -> str:
    if not headcode:
        return "other"
    first = headcode[0]
    if first in {"4", "6", "7", "8"}:
        return "freight"
    if first in {"1", "2", "9"}:
        return "passenger"
    return "other"


def upsert_rows(table: str, rows: list[dict], conflict: str | None = None, batch_size: int = 500) -> None:
    if not rows:
        return

    url = postgrest_url(table)
    if conflict:
        url += f"?on_conflict={conflict}"

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        r = requests.post(
            url,
            headers=supabase_headers(return_representation=False),
            data=json.dumps(batch),
            timeout=60,
        )
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"Upsert failed for {table}: {r.status_code} {r.text}")


def upsert_service(row: dict) -> str:
    url = postgrest_url("schedule_services") + "?on_conflict=train_uid,schedule_start_date,stp_indicator"
    r = requests.post(
        url,
        headers=supabase_headers(return_representation=True),
        data=json.dumps(row),
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Service upsert failed: {r.status_code} {r.text}")

    data = r.json()
    if not data:
        raise RuntimeError("Service upsert returned no data")

    return data[0]["id"]


def download_schedule_stream():
    print("Downloading Network Rail JSON schedule full daily file...", flush=True)
    response = requests.get(
        SCHEDULE_URL,
        auth=(NETWORK_RAIL_USERNAME, NETWORK_RAIL_PASSWORD),
        stream=True,
        allow_redirects=True,
        timeout=120,
    )
    response.raise_for_status()
    response.raw.decode_content = False
    return gzip.GzipFile(fileobj=response.raw)


def extract_tiploc(record: dict) -> tuple[str, dict] | None:
    # Common wrapper is JsonTiplocV1. Keep this loose because the JSON feed has varied field naming.
    item = record.get("JsonTiplocV1") or record.get("TiplocV1")
    if not item:
        return None

    tiploc = item.get("tiploc_code") or item.get("TIPLOC") or item.get("tiploc")
    if not tiploc:
        return None

    name = (
        item.get("nalco") or
        item.get("description") or
        item.get("tps_description") or
        item.get("stanox_location_description")
    )

    return str(tiploc).strip(), {
        "tiploc": str(tiploc).strip(),
        "name": name,
        "stanox": item.get("stanox"),
        "crs": item.get("crs_code") or item.get("crs"),
        "raw": item,
    }


def parse_schedule(record: dict, tiploc_lookup: dict[str, str]) -> tuple[dict, list[dict]] | None:
    sched = record.get("JsonScheduleV1")
    if not sched:
        return None

    segment = sched.get("schedule_segment") or {}
    locations = segment.get("schedule_location") or []

    if not any((loc.get("tiploc_code") or "").strip() == ACB_TIPLOC for loc in locations):
        return None

    train_uid = sched.get("CIF_train_uid")
    stp = sched.get("CIF_stp_indicator")
    start = sched.get("schedule_start_date")
    end = sched.get("schedule_end_date")
    days_runs = sched.get("schedule_days_runs")
    signalling_id = clean_time(segment.get("signalling_id"))
    atoc_code = sched.get("atoc_code")
    train_status = sched.get("train_status")
    train_category = segment.get("CIF_train_category")

    if not train_uid or not stp or not start:
        return None

    origin_loc = locations[0] if locations else {}
    dest_loc = locations[-1] if locations else {}

    origin_tiploc = origin_loc.get("tiploc_code")
    dest_tiploc = dest_loc.get("tiploc_code")

    service_row = {
        "train_uid": train_uid,
        "stp_indicator": stp,
        "schedule_start_date": start,
        "schedule_end_date": end,
        "days_runs": days_runs,
        "signalling_id": signalling_id,
        "atoc_code": atoc_code,
        "train_status": train_status,
        "train_category": train_category,
        "origin_tiploc": origin_tiploc,
        "origin_name": tiploc_lookup.get(origin_tiploc, origin_tiploc),
        "origin_departure": clean_time(origin_loc.get("departure") or origin_loc.get("public_departure")),
        "destination_tiploc": dest_tiploc,
        "destination_name": tiploc_lookup.get(dest_tiploc, dest_tiploc),
        "destination_arrival": clean_time(dest_loc.get("arrival") or dest_loc.get("public_arrival")),
        "passes_acb": True,
        "raw": sched,
    }

    location_rows = []
    for idx, loc in enumerate(locations):
        tiploc = loc.get("tiploc_code")
        if not tiploc:
            continue

        location_rows.append({
            "train_uid": train_uid,
            "stp_indicator": stp,
            "schedule_start_date": start,
            "signalling_id": signalling_id,
            "sort_order": idx,
            "tiploc": tiploc,
            "location_name": tiploc_lookup.get(tiploc, tiploc),
            "location_type": loc.get("location_type") or loc.get("record_identity"),
            "arrival": clean_time(loc.get("arrival")),
            "departure": clean_time(loc.get("departure")),
            "pass_time": clean_time(loc.get("pass")),
            "public_arrival": clean_time(loc.get("public_arrival")),
            "public_departure": clean_time(loc.get("public_departure")),
            "platform": clean_time(loc.get("platform")),
            "line": clean_time(loc.get("line")),
            "path": clean_time(loc.get("path")),
            "raw": loc,
        })

    return service_row, location_rows


def delete_locations_for_service(service_id: str) -> None:
    url = postgrest_url("schedule_locations") + f"?service_id=eq.{service_id}"
    r = requests.delete(url, headers=supabase_headers(return_representation=False), timeout=60)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Could not delete old schedule locations: {r.status_code} {r.text}")


def fetch_movements_to_enrich() -> list[dict]:
    url = (
        postgrest_url("station_movements") +
        "?source=eq.Network%20Rail%20TRUST"
        "&select=id,running_date,actual_time,train_id,origin,destination"
        "&order=created_at.desc"
        "&limit=500"
    )
    r = requests.get(url, headers=supabase_headers(return_representation=True), timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_matching_schedules(headcode: str) -> list[dict]:
    url = (
        postgrest_url("schedule_services") +
        f"?signalling_id=eq.{headcode}"
        "&select=*,schedule_locations(*)"
    )
    r = requests.get(url, headers=supabase_headers(return_representation=True), timeout=60)
    r.raise_for_status()
    return r.json()


def best_schedule_for_movement(movement: dict, schedules: list[dict]) -> dict | None:
    running_date = datetime.strptime(movement["running_date"], "%Y-%m-%d").date()
    movement_minutes = time_to_minutes(movement.get("actual_time"))

    best = None
    best_score = 999999

    for service in schedules:
        if not active_on_date(
            service.get("schedule_start_date"),
            service.get("schedule_end_date"),
            running_date,
            service.get("days_runs"),
        ):
            continue

        acb_locs = [
            loc for loc in service.get("schedule_locations", [])
            if loc.get("tiploc") == ACB_TIPLOC
        ]
        if not acb_locs:
            continue

        loc = acb_locs[0]
        booked = loc.get("pass_time") or loc.get("departure") or loc.get("arrival")
        booked_minutes = time_to_minutes(booked)

        if movement_minutes is None or booked_minutes is None:
            score = 500
        else:
            # allow timezone/DST and small reporting differences
            diffs = [
                abs(movement_minutes - booked_minutes),
                abs((movement_minutes + 60) - booked_minutes),
                abs((movement_minutes - 60) - booked_minutes),
            ]
            score = min(diffs)

        if score < best_score:
            best_score = score
            best = {**service, "_acb_location": loc, "_score": score}

    # If nothing close, still return the only active headcode match.
    if best and best_score <= 180:
        return best
    return best


def enrich_existing_movements() -> None:
    print("Enriching recent Network Rail TRUST movements with schedule origin/destination...", flush=True)
    movements = fetch_movements_to_enrich()
    updated = 0

    for movement in movements:
        schedules = fetch_matching_schedules(movement["train_id"])
        if not schedules:
            continue

        match = best_schedule_for_movement(movement, schedules)
        if not match:
            continue

        acb = match.get("_acb_location") or {}
        update = {
            "origin": match.get("origin_name") or match.get("origin_tiploc"),
            "destination": match.get("destination_name") or match.get("destination_tiploc"),
            "toc": match.get("atoc_code"),
            "planned_time": acb.get("pass_time") or acb.get("departure") or acb.get("arrival"),
            "platform": movement.get("platform") or acb.get("platform"),
            "train_type": train_type_from_headcode(movement.get("train_id")),
        }

        url = postgrest_url("station_movements") + f"?id=eq.{movement['id']}"
        r = requests.patch(url, headers=supabase_headers(return_representation=False), data=json.dumps(update), timeout=60)
        if r.status_code not in (200, 204):
            print(f"Movement enrich failed {movement['id']}: {r.status_code} {r.text}", flush=True)
            continue

        updated += 1

    print(f"Enriched {updated} recent movement rows.", flush=True)


def main() -> None:
    tiploc_lookup: dict[str, str] = {}
    tiploc_rows: list[dict] = []

    schedule_count = 0
    kept_count = 0

    with download_schedule_stream() as fh:
        for raw_line in fh:
            if not raw_line.strip():
                continue

            try:
                record = json.loads(raw_line.decode("utf-8"))
            except Exception:
                continue

            tiploc_data = extract_tiploc(record)
            if tiploc_data:
                code, row = tiploc_data
                tiploc_lookup[code] = row.get("name") or code
                tiploc_rows.append(row)

                if len(tiploc_rows) >= 1000:
                    upsert_rows("tiploc_names", tiploc_rows, conflict="tiploc")
                    tiploc_rows = []
                continue

            parsed = parse_schedule(record, tiploc_lookup)
            if parsed:
                schedule_count += 1
                service_row, location_rows = parsed

                service_id = upsert_service(service_row)
                delete_locations_for_service(service_id)

                for loc in location_rows:
                    loc["service_id"] = service_id

                upsert_rows("schedule_locations", location_rows, conflict="service_id,sort_order")
                kept_count += 1

                if kept_count % 25 == 0:
                    print(f"Loaded {kept_count} schedules passing Acton Bridge...", flush=True)

    if tiploc_rows:
        upsert_rows("tiploc_names", tiploc_rows, conflict="tiploc")

    print(f"Schedule loader finished. Found {kept_count} Acton Bridge schedules.", flush=True)
    enrich_existing_movements()


if __name__ == "__main__":
    main()
