#!/usr/bin/env python3
"""
Acton Bridge RTT NG API Loco Allocation Collector

This version uses the Realtime Trains NEXT GENERATION API:

  Server:   https://data.rtt.io
  Auth:     Bearer token
  Endpoints:
    /api/info
    /api/get_access_token  (only if your token is a refresh token)
    /gb-nr/location
    /gb-nr/service

It does NOT use the old Pull API endpoint:
  https://api.rtt.io/api/v1/json/search/...

That old endpoint caused the 500 error when used with the new bearer-token API.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import requests


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

RTT_API_BASE = os.environ.get("RTT_API_BASE", "https://data.rtt.io").rstrip("/")
RTT_BEARER_TOKEN = os.environ["RTT_BEARER_TOKEN"]

STATION = os.environ.get("RTT_STATION", "ACBG")
RUNNING_DATE = os.environ.get("RUNNING_DATE") or datetime.now().strftime("%Y-%m-%d")

SLEEP_BETWEEN_CALLS = float(os.environ.get("RTT_SLEEP_BETWEEN_CALLS", "0.7"))
MAX_SERVICES = int(os.environ.get("RTT_MAX_SERVICES", "500"))

# If set to 1, writes one debug JSON file into the GitHub Action workspace.
DEBUG_WRITE_JSON = os.environ.get("RTT_DEBUG_WRITE_JSON", "0") == "1"


access_token = RTT_BEARER_TOKEN


def supabase_headers(prefer: str = "return=representation") -> Dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def supabase_table_url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def rtt_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "Acton-Bridge-Rail-Count/1.0",
    }


def rtt_get_json(path: str, params: Optional[Dict[str, Any]] = None, allow_204: bool = True) -> Optional[Dict[str, Any]]:
    url = f"{RTT_API_BASE}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"

    response = requests.get(url, headers=rtt_headers(), timeout=45)

    if response.status_code == 204 and allow_204:
        return None

    if response.status_code == 404:
        return None

    if response.status_code == 401:
        raise RuntimeError(
            f"RTT API authentication failed for {path}. "
            "Check RTT_BEARER_TOKEN, or whether it is a refresh token that needs /api/get_access_token."
        )

    if response.status_code == 403:
        raise RuntimeError(
            f"RTT API access denied for {path}. "
            "Your token may lack detailed/allocation/Know Your Train entitlement."
        )

    if response.status_code >= 500:
        raise RuntimeError(f"RTT API server error {response.status_code} for {url}: {response.text[:500]}")

    response.raise_for_status()

    if not response.text.strip():
        return None

    return response.json()


def ensure_access_token() -> None:
    """
    The NG API may issue either:
    - a long-life access token, or
    - a refresh token which must be exchanged through /api/get_access_token.

    Try /api/info first. If that fails with 401, try /api/get_access_token.
    """
    global access_token

    print("Checking RTT API token with /api/info...")

    try:
        info = rtt_get_json("/api/info", allow_204=False)
        print_api_info(info)
        return
    except RuntimeError as exc:
        msg = str(exc)
        if "authentication failed" not in msg.lower():
            raise
        print("Initial /api/info failed; trying /api/get_access_token in case this is a refresh token...")

    token_data = rtt_get_json("/api/get_access_token", allow_204=False)
    if not token_data or not token_data.get("token"):
        raise RuntimeError("Could not exchange RTT token. /api/get_access_token did not return a token.")

    access_token = token_data["token"]
    print("Received short-life RTT access token from refresh token.")

    info = rtt_get_json("/api/info", allow_204=False)
    print_api_info(info)


def print_api_info(info: Optional[Dict[str, Any]]) -> None:
    if not info:
        print("RTT /api/info returned no body")
        return

    version = info.get("api_version") or info.get("apiVersion") or "unknown"
    credentials = info.get("credentials") or {}
    entitlements = credentials.get("entitlements") or []

    print(f"RTT API version: {version}")
    print(f"RTT entitlements: {', '.join(entitlements) if entitlements else 'none shown'}")

    if "allowAllocations" not in entitlements:
        print("WARNING: entitlement allowAllocations not shown. Loco allocation data may not be returned.")
    if "allowDetailed" not in entitlements:
        print("WARNING: entitlement allowDetailed not shown. Detailed service fields may be limited.")


def fetch_target_movements() -> List[Dict[str, Any]]:
    url = (
        supabase_table_url("station_movements")
        + f"?running_date=eq.{RUNNING_DATE}"
        + "&source=eq.Network%20Rail%20TRUST"
        + "&select=running_date,actual_time,planned_time,train_id,train_type,origin,destination,toc"
        + "&order=actual_time.asc"
        + "&limit=1000"
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
        supabase_table_url("loco_allocations")
        + f"?running_date=eq.{RUNNING_DATE}"
        + "&select=train_id,loco_number"
    )
    response = requests.get(url, headers=supabase_headers(), timeout=45)

    if response.status_code == 404:
        return {}

    response.raise_for_status()
    return {str(row["train_id"]).upper(): str(row["loco_number"]) for row in response.json()}


def parse_hhmm(value: Any) -> Optional[int]:
    if value is None:
        return None

    text = str(value).strip()

    # ISO datetime
    if "T" in text:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.hour * 60 + dt.minute
        except Exception:
            pass

    # HH:MM, HHMM, HHMM½ etc
    text = text.replace(":", "")
    match = re.search(r"(\d{4})", text)
    if not match:
        return None

    hhmm = match.group(1)
    hh = int(hhmm[:2])
    mm = int(hhmm[2:4])
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return hh * 60 + mm

    return None


def movement_time_minutes(row: Dict[str, Any]) -> Optional[int]:
    return parse_hhmm(row.get("actual_time") or row.get("planned_time"))


def service_headcode(service: Dict[str, Any]) -> str:
    meta = service.get("scheduleMetadata") or {}
    return str(
        meta.get("trainReportingIdentity")
        or meta.get("reportingIdentity")
        or meta.get("headcode")
        or meta.get("identity")
        or service.get("runningIdentity")
        or service.get("trainIdentity")
        or service.get("identity")
        or ""
    ).strip().upper()


def service_identity(service: Dict[str, Any]) -> Optional[str]:
    meta = service.get("scheduleMetadata") or {}
    return (
        meta.get("identity")
        or meta.get("uniqueIdentity")
        or service.get("uniqueIdentity")
    )


def service_departure_date(service: Dict[str, Any]) -> Optional[str]:
    meta = service.get("scheduleMetadata") or {}
    return (
        meta.get("departureDate")
        or service.get("departureDate")
        or RUNNING_DATE
    )


def service_time_at_station(service: Dict[str, Any]) -> Optional[int]:
    temporal = service.get("temporalData") or {}
    location_meta = service.get("locationMetadata") or {}

    # NG API shape:
    # temporalData: { pass: { scheduleInternal, realtimeActual }, departure: {...}, arrival: {...} }
    for section_name in ("pass", "departure", "arrival"):
        section = temporal.get(section_name) or {}
        for key in ("realtimeActual", "realtimeForecast", "realtimeEstimate", "scheduleInternal", "scheduleAdvertised"):
            mins = parse_hhmm(section.get(key))
            if mins is not None:
                return mins

    # Legacy-ish fallback fields.
    for key in ("realtimePass", "realtimeDeparture", "realtimeArrival", "wttBookedPass", "wttBookedDeparture", "wttBookedArrival"):
        mins = parse_hhmm(service.get(key))
        if mins is not None:
            return mins

    return None


def fetch_location_services() -> List[Dict[str, Any]]:
    start = f"{RUNNING_DATE}T00:00:00"
    end = f"{RUNNING_DATE}T23:59:00"

    # Try full TIPLOC first, then CRS fallback if needed.
    code_attempts = [STATION]
    if STATION.upper() == "ACBG":
        code_attempts.append("ACB")

    last_error = None

    for code in code_attempts:
        params = {
            "code": code,
            "timeFrom": start,
            "timeTo": end,
            "detailed": "true",
            "timeTolerance": "true",
        }

        try:
            data = rtt_get_json("/gb-nr/location", params=params)
        except Exception as exc:
            print(f"RTT location lookup failed for code={code}: {exc}")
            last_error = exc
            continue

        if not data:
            print(f"RTT location lookup returned no services for code={code}")
            continue

        services = data.get("services") or []
        print(f"RTT NG location services returned for {code}: {len(services)}")

        if DEBUG_WRITE_JSON:
            Path(f"rtt_location_{code}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

        if services:
            return services[:MAX_SERVICES]

    if last_error:
        raise last_error

    return []


def find_matching_service(movement: Dict[str, Any], services: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    target_headcode = str(movement.get("train_id") or "").strip().upper()
    target_minutes = movement_time_minutes(movement)

    matches = []
    for service in services:
        headcode = service_headcode(service)
        if headcode != target_headcode:
            continue

        mins = service_time_at_station(service)
        if target_minutes is None or mins is None:
            diff = 9999
        else:
            diff = min(abs(target_minutes - mins), abs(target_minutes + 1440 - mins), abs(target_minutes - (mins + 1440)))

        matches.append((diff, service))

    if not matches:
        return None

    matches.sort(key=lambda item: item[0])
    return matches[0][1]


def fetch_service_detail(service: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    meta = service.get("scheduleMetadata") or {}

    unique_identity = meta.get("uniqueIdentity") or service.get("uniqueIdentity")
    identity = meta.get("identity")
    departure_date = meta.get("departureDate") or RUNNING_DATE

    if unique_identity:
        params = {"uniqueIdentity": unique_identity}
    elif identity:
        params = {"identity": identity, "departureDate": departure_date}
    else:
        return None

    data = rtt_get_json("/gb-nr/service", params=params)
    if DEBUG_WRITE_JSON and data:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(identity or unique_identity))
        Path(f"rtt_service_{safe}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    return data


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
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    compact = re.sub(r"[^0-9]", "", text)

    # British loco examples: 66315, 66797, 88003, 92010, 56091.
    if re.fullmatch(r"[0-9]{5}", compact):
        return compact

    return None


def extract_loco_from_allocation_data(data: Dict[str, Any]) -> Tuple[Optional[str], str]:
    # Prefer the documented NG location: service.allocationData[].allocationItems[].identity.
    service = data.get("service") or data

    allocation_data = service.get("allocationData") or service.get("allocations") or []
    if isinstance(allocation_data, dict):
        allocation_data = [allocation_data]

    for alloc_idx, allocation in enumerate(allocation_data):
        items = allocation.get("allocationItems") or []
        for item_idx, item in enumerate(items):
            stock_type = str(item.get("stockType") or "").upper()
            identity_suppressed = bool(item.get("identitySuppressed"))
            identity = item.get("identity")

            if identity_suppressed:
                continue

            if stock_type == "LOCO":
                loco = normalise_loco_number(identity)
                if loco:
                    return loco, f"RTT NG allocationData[{alloc_idx}].allocationItems[{item_idx}].identity"

            # Some sets/units may contain component vehicles flagged as locomotives.
            for veh_idx, vehicle in enumerate(item.get("componentVehicles") or []):
                if vehicle.get("isLocomotive"):
                    loco = normalise_loco_number(vehicle.get("identity"))
                    if loco:
                        return loco, f"RTT NG component locomotive allocationData[{alloc_idx}].allocationItems[{item_idx}].componentVehicles[{veh_idx}].identity"

    # Fallback: scan likely paths only.
    likely_words = ("allocation", "loco", "locomotive", "traction", "stock", "vehicle")
    for path, value in walk_json(data):
        if any(word in path.lower() for word in likely_words):
            loco = normalise_loco_number(value)
            if loco:
                return loco, f"RTT NG fallback field {path}"

    return None, ""


def upsert_loco_allocation(train_id: str, loco_number: str, source_note: str) -> None:
    payload = [{
        "running_date": RUNNING_DATE,
        "train_id": train_id,
        "loco_number": loco_number,
        "source_note": source_note,
    }]

    url = supabase_table_url("loco_allocations") + "?on_conflict=running_date,train_id"
    headers = supabase_headers(prefer="resolution=merge-duplicates,return=representation")

    response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=45)

    if response.status_code not in (200, 201):
        raise RuntimeError(f"Supabase upsert failed: {response.status_code} {response.text}")


def main() -> int:
    print(f"RTT NG loco allocation collector starting for {RUNNING_DATE} at {STATION}")

    ensure_access_token()

    movements = fetch_target_movements()
    already = existing_allocations()

    services = fetch_location_services()
    if not services:
        print("No RTT NG location services found. Check station code, token entitlements, and date/window access.")
        return 0

    updates = 0
    misses = 0

    for movement in movements:
        headcode = str(movement.get("train_id") or "").strip().upper()

        if headcode in already:
            print(f"Skip {headcode}: already has loco {already[headcode]}")
            continue

        service = find_matching_service(movement, services)
        if not service:
            print(f"No RTT NG service match for {headcode}")
            misses += 1
            continue

        meta = service.get("scheduleMetadata") or {}
        identity = meta.get("identity") or meta.get("uniqueIdentity") or "unknown"

        time.sleep(SLEEP_BETWEEN_CALLS)

        try:
            detail = fetch_service_detail(service)
        except Exception as exc:
            print(f"Detail lookup failed for {headcode} / {identity}: {exc}")
            misses += 1
            continue

        if not detail:
            print(f"No service detail for {headcode} / {identity}")
            misses += 1
            continue

        loco, source = extract_loco_from_allocation_data(detail)

        if not loco:
            print(f"No loco allocation found for {headcode} / {identity}")
            misses += 1
            continue

        note = f"RTT NG API allocation; identity={identity}; {source}"
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
