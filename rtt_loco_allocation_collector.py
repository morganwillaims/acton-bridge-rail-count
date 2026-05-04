#!/usr/bin/env python3
"""
Acton Bridge RTT Loco Allocation Collector

Purpose:
- Finds today's freight/light-engine movements in Supabase.
- Looks up matching services at Acton Bridge through the Realtime Trains API.
- Fetches detailed service info.
- Tries to extract loco / rolling-stock allocation from the response.
- Writes matches into public.loco_allocations.

Important:
- This uses the authorised RTT API route, not website scraping.
- Whether loco numbers appear depends on your RTT API entitlements and whether that service has allocation data.
"""

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

RTT_API_BASE = os.environ.get("RTT_API_BASE", "https://api.rtt.io/api/v1").rstrip("/")
RTT_AUTH_MODE = os.environ.get("RTT_AUTH_MODE", "basic").lower().strip()
RTT_USERNAME = os.environ.get("RTT_USERNAME", "")
RTT_PASSWORD = os.environ.get("RTT_PASSWORD", "")
RTT_BEARER_TOKEN = os.environ.get("RTT_BEARER_TOKEN", "")

STATION = os.environ.get("RTT_STATION", "ACBG")
RUNNING_DATE = os.environ.get("RUNNING_DATE") or datetime.now().strftime("%Y-%m-%d")
SLEEP_BETWEEN_CALLS = float(os.environ.get("RTT_SLEEP_BETWEEN_CALLS", "0.7"))
MAX_SERVICES = int(os.environ.get("RTT_MAX_SERVICES", "250"))


def supabase_headers(prefer: str = "return=representation") -> Dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def supabase_url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def rtt_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "Acton-Bridge-Rail-Count/1.0",
    }

    if RTT_AUTH_MODE == "bearer":
        if not RTT_BEARER_TOKEN:
            raise RuntimeError("RTT_AUTH_MODE=bearer but RTT_BEARER_TOKEN is missing")
        headers["Authorization"] = f"Bearer {RTT_BEARER_TOKEN}"
    else:
        if not RTT_USERNAME or not RTT_PASSWORD:
            raise RuntimeError("RTT_AUTH_MODE=basic but RTT_USERNAME / RTT_PASSWORD are missing")
        token = base64.b64encode(f"{RTT_USERNAME}:{RTT_PASSWORD}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"

    return headers


def rtt_get_json(path: str) -> Optional[Dict[str, Any]]:
    url = f"{RTT_API_BASE}{path}"
    response = requests.get(url, headers=rtt_headers(), timeout=45)

    if response.status_code == 404:
        return None

    if response.status_code == 401:
        raise RuntimeError("RTT API authentication failed. Check username/password or bearer token.")

    if response.status_code == 403:
        raise RuntimeError("RTT API access denied. You may not have detailed / allocation entitlement.")

    response.raise_for_status()
    return response.json()


def fetch_target_movements() -> List[Dict[str, Any]]:
    """
    Pull candidate freight/light engine movements from your existing station_movements table.
    Passenger rows are skipped because the current goal is freight/Class 66-type allocations.
    """
    url = (
        supabase_url("station_movements")
        + f"?running_date=eq.{RUNNING_DATE}"
        + "&source=eq.Network%20Rail%20TRUST"
        + "&select=running_date,actual_time,planned_time,train_id,train_type,origin,destination,toc"
        + "&order=actual_time.asc"
        + "&limit=500"
    )
    response = requests.get(url, headers=supabase_headers(), timeout=45)
    response.raise_for_status()
    rows = response.json()

    candidates = []
    for row in rows:
        headcode = str(row.get("train_id") or "").strip().upper()
        ttype = str(row.get("train_type") or "").strip().lower()

        if not re.match(r"^[0-9][A-Z][0-9]{2}$", headcode):
            continue

        is_freight = ttype == "freight" or re.match(r"^[4678]", headcode)
        is_light_engine = ttype == "light_engine" or headcode.startswith("0")

        if is_freight or is_light_engine:
            candidates.append(row)

    print(f"Candidate freight/light-engine movements: {len(candidates)}")
    return candidates


def existing_allocations() -> Dict[str, str]:
    url = (
        supabase_url("loco_allocations")
        + f"?running_date=eq.{RUNNING_DATE}"
        + "&select=train_id,loco_number"
    )
    response = requests.get(url, headers=supabase_headers(), timeout=45)
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    return {str(row["train_id"]).upper(): row["loco_number"] for row in response.json()}


def hhmm_to_minutes(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    text = str(value).strip().replace(":", "")
    if len(text) < 4 or not text[:4].isdigit():
        return None
    return int(text[:2]) * 60 + int(text[2:4])


def movement_time_minutes(row: Dict[str, Any]) -> Optional[int]:
    return hhmm_to_minutes(row.get("actual_time") or row.get("planned_time"))


def service_headcode(service: Dict[str, Any]) -> str:
    return str(
        service.get("runningIdentity")
        or service.get("trainIdentity")
        or service.get("identity")
        or ""
    ).strip().upper()


def service_time_at_station(service: Dict[str, Any]) -> Optional[int]:
    loc = service.get("locationDetail") or service.get("location") or {}
    for key in ("realtimePass", "realtimeDeparture", "realtimeArrival", "wttBookedPass", "wttBookedDeparture", "wttBookedArrival"):
        val = loc.get(key)
        mins = hhmm_to_minutes(val)
        if mins is not None:
            return mins
    return None


def fetch_location_services() -> List[Dict[str, Any]]:
    yyyy, mm, dd = RUNNING_DATE.split("-")

    # Legacy Pull API endpoint. If your account uses a different base, set RTT_API_BASE.
    data = rtt_get_json(f"/json/search/{STATION}/{yyyy}/{int(mm)}/{int(dd)}")
    if not data:
        return []

    services = data.get("services") or []
    print(f"RTT location services returned: {len(services)}")
    return services[:MAX_SERVICES]


def find_matching_service_uid(movement: Dict[str, Any], services: List[Dict[str, Any]]) -> Optional[str]:
    target_headcode = str(movement.get("train_id") or "").strip().upper()
    target_minutes = movement_time_minutes(movement)

    matches = []
    for service in services:
        headcode = service_headcode(service)

        if headcode != target_headcode:
            # Freight in RTT may sometimes be obfuscated as FRGT in public/detailed docs.
            # If runningIdentity is missing, do not force a weak match.
            continue

        service_minutes = service_time_at_station(service)
        diff = 0 if target_minutes is None or service_minutes is None else abs(target_minutes - service_minutes)
        matches.append((diff, service))

    if not matches:
        return None

    matches.sort(key=lambda x: x[0])
    return matches[0][1].get("serviceUid")


def walk_json(obj: Any, path: str = "") -> Iterable[Tuple[str, Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from walk_json(value, f"{path}.{key}" if path else key)
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from walk_json(value, f"{path}[{i}]")
    else:
        yield path, obj


def normalise_loco_number(value: Any) -> Optional[str]:
    """
    Accepts values like:
    - 66797
    - 66 797
    - 66/797
    - Class 66 797
    - 66315
    """
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    compact = re.sub(r"[^0-9]", "", text)

    # Common UK diesel/electric loco number pattern: 5 digits.
    # Examples: 66315, 66797, 88003, 92010, 56091.
    if re.fullmatch(r"[0-9]{5}", compact):
        return compact

    return None


def extract_loco_from_service_json(data: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """
    This is intentionally flexible because allocation fields can depend on RTT API version/entitlement.
    It searches likely field names first, then scans text values.
    """
    likely_field_names = (
        "allocation",
        "allocations",
        "loco",
        "locomotive",
        "locomotives",
        "vehicle",
        "vehicles",
        "unit",
        "units",
        "stock",
        "formation",
        "consist",
        "operatedWith",
        "operated_with",
        "traction",
        "rollingStock",
        "rolling_stock",
    )

    best = None
    best_source = ""

    for path, value in walk_json(data):
        lower_path = path.lower()
        if any(name.lower() in lower_path for name in likely_field_names):
            loco = normalise_loco_number(value)
            if loco:
                return loco, f"RTT field: {path}"

    # Fallback scan all simple text values.
    for path, value in walk_json(data):
        loco = normalise_loco_number(value)
        if loco:
            best = loco
            best_source = f"RTT text scan: {path}"
            break

    return best, best_source


def fetch_service_detail(service_uid: str) -> Optional[Dict[str, Any]]:
    yyyy, mm, dd = RUNNING_DATE.split("-")
    return rtt_get_json(f"/json/service/{service_uid}/{yyyy}/{int(mm)}/{int(dd)}")


def upsert_loco_allocation(train_id: str, loco_number: str, source_note: str) -> None:
    payload = [{
        "running_date": RUNNING_DATE,
        "train_id": train_id,
        "loco_number": loco_number,
        "source_note": source_note,
    }]

    url = supabase_url("loco_allocations") + "?on_conflict=running_date,train_id"
    headers = supabase_headers(prefer="resolution=merge-duplicates,return=representation")

    response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=45)
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Supabase upsert failed: {response.status_code} {response.text}")


def main() -> int:
    print(f"RTT loco allocation collector starting for {RUNNING_DATE} at {STATION}")

    movements = fetch_target_movements()
    allocated = existing_allocations()

    services = fetch_location_services()
    if not services:
        print("No RTT location services found. Check RTT credentials / endpoint / station.")
        return 0

    updates = 0
    misses = 0

    for movement in movements:
        headcode = str(movement.get("train_id") or "").strip().upper()

        if headcode in allocated:
            print(f"Skip {headcode}: already has loco {allocated[headcode]}")
            continue

        uid = find_matching_service_uid(movement, services)
        if not uid:
            print(f"No RTT service UID match for {headcode}")
            misses += 1
            continue

        time.sleep(SLEEP_BETWEEN_CALLS)

        try:
            detail = fetch_service_detail(uid)
        except Exception as exc:
            print(f"Detail lookup failed for {headcode} / {uid}: {exc}")
            misses += 1
            continue

        if not detail:
            print(f"No service detail for {headcode} / {uid}")
            misses += 1
            continue

        loco, source = extract_loco_from_service_json(detail)

        if not loco:
            print(f"No loco allocation found for {headcode} / {uid}")
            misses += 1
            continue

        note = f"RTT API allocation; serviceUid={uid}; {source}"
        upsert_loco_allocation(headcode, loco, note)
        print(f"Saved {headcode} = {loco} ({source})")
        updates += 1

    print(f"Finished. Updates={updates}, misses={misses}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Collector failed: {exc}", file=sys.stderr)
        raise
