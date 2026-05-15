#!/usr/bin/env python3
"""
Acton Bridge Schedule Loader — pathing capture version

Purpose
- Load schedule records from a JSON source file/URL if provided.
- Upsert service-level schedule rows into public.schedule_services.
- Upsert schedule locations into public.schedule_locations when included.
- Capture pathing/traction fields for later freight service detail display.

Environment variables
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY
- SCHEDULE_SOURCE_URL optional JSON endpoint/file URL
- SCHEDULE_SOURCE_FILE optional local JSON file

This is a safe generic replacement. If your old schedule loader was doing heavy Network Rail CIF downloads,
keep that logic and copy the extraction/upsert field additions from this file.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, date
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib import request, error

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or ""
SCHEDULE_SOURCE_URL = os.getenv("SCHEDULE_SOURCE_URL", "")
SCHEDULE_SOURCE_FILE = os.getenv("SCHEDULE_SOURCE_FILE", "")

SERVICE_TABLE = "schedule_services"
LOCATION_TABLE = "schedule_locations"

PATHING_FIELDS = [
    "power_type",
    "planned_power",
    "traction_type",
    "traction_class",
    "timing_load",
    "operating_characteristics",
    "stock_type",
    "speed",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}", flush=True)


def clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def first(record: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        if key in record and clean(record.get(key)):
            return clean(record.get(key))
        lower = key.lower()
        for k, v in record.items():
            if str(k).lower() == lower and clean(v):
                return clean(v)
    return None


def parse_date(value: Optional[str]) -> str:
    if not value:
        return date.today().isoformat()
    s = value.strip()
    for fmt in ["%Y-%m-%d", "%Y%m%d", "%d/%m/%Y"]:
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date().isoformat()
        except Exception:
            pass
    return date.today().isoformat()


def derive_power_label(raw: Dict[str, Any]) -> Tuple[str, str]:
    vals = " ".join(str(raw.get(k) or "") for k in PATHING_FIELDS).upper()
    if not vals.strip():
        return "unknown", "Pathing unknown"
    if any(x in vals for x in ["ELECTRIC", "AC ELECTRIC", " OHE", "ELECTRIC LOCO"]):
        return "electric", "Pathed as electric loco"
    if any(x in vals for x in ["DIESEL", "DIESEL LOCO"]):
        return "diesel", "Pathed as diesel loco"
    for key in ["power_type", "planned_power", "traction_type"]:
        v = str(raw.get(key) or "").strip().upper()
        if v in {"E", "EL", "ELEC"}:
            return "electric", "Pathed as electric loco"
        if v in {"D", "DE", "DSL"}:
            return "diesel", "Pathed as diesel loco"
        if v in {"ED", "DE/ED", "BI", "BIMODE", "BI-MODE", "D/E", "E/D"}:
            return "diesel_electric", "Pathed diesel/electric"
    return "unknown", "Pathing unknown"


def extract_pathing_fields(record: Dict[str, Any]) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {
        "power_type": first(record, "power_type", "powerType", "power", "traction_power", "plannedTraction", "CIF_power_type"),
        "planned_power": first(record, "planned_power", "plannedPower", "planned_power_type", "planned_power_code"),
        "traction_type": first(record, "traction_type", "tractionType", "traction", "traction_code"),
        "traction_class": first(record, "traction_class", "tractionClass", "train_class", "trainClass", "traction_class_code"),
        "timing_load": first(record, "timing_load", "timingLoad", "timing_load_code", "load", "trailing_load", "CIF_timing_load"),
        "operating_characteristics": first(record, "operating_characteristics", "operatingCharacteristics", "op_chars", "operating_characteristics_code", "CIF_operating_characteristics"),
        "stock_type": first(record, "stock_type", "stockType", "stock", "rolling_stock"),
        "speed": first(record, "speed", "max_speed", "planned_speed", "maxSpeed", "CIF_speed"),
    }
    pwr, label = derive_power_label({k: v for k, v in out.items() if v is not None})
    out["pathing_power"] = pwr
    out["pathing_power_label"] = label
    out["pathing_power_source"] = "schedule_loader" if pwr != "unknown" else "schedule_loader_no_source_field"
    return out


def extract_service(record: Dict[str, Any]) -> Dict[str, Any]:
    headcode = first(record, "train_id", "headcode", "identity", "trainIdentity") or ""
    uid = first(record, "uid", "train_uid", "CIF_train_uid", "trainUid")
    start_date = parse_date(first(record, "running_date", "date", "schedule_start_date", "start_date", "service_date"))
    end_date = parse_date(first(record, "schedule_end_date", "end_date")) if first(record, "schedule_end_date", "end_date") else None
    origin = first(record, "origin", "origin_tiploc", "origin_name", "from")
    destination = first(record, "destination", "destination_tiploc", "destination_name", "to")
    stp = first(record, "stp_indicator", "stp", "STP_indicator") or "P"
    toc = first(record, "toc", "atoc_code", "operator", "operator_code")
    row: Dict[str, Any] = {
        "train_uid": uid,
        "train_id": headcode,
        "schedule_start_date": start_date,
        "schedule_end_date": end_date,
        "origin": origin,
        "destination": destination,
        "stp_indicator": stp,
        "toc": toc,
        "updated_at": now_iso(),
    }
    row.update(extract_pathing_fields(record))
    return {k: v for k, v in row.items() if v is not None}


def extract_locations(record: Dict[str, Any], service: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates = record.get("locations") or record.get("schedule_locations") or record.get("Location") or []
    if isinstance(candidates, dict):
        candidates = [candidates]
    rows: List[Dict[str, Any]] = []
    for i, loc in enumerate(candidates if isinstance(candidates, list) else []):
        if not isinstance(loc, dict):
            continue
        rows.append({k: v for k, v in {
            "train_uid": service.get("train_uid"),
            "train_id": service.get("train_id"),
            "schedule_start_date": service.get("schedule_start_date"),
            "location_order": i,
            "tiploc": first(loc, "tiploc", "TIPLOC", "location", "location_tiploc"),
            "arr": first(loc, "arr", "arrival", "arrival_time", "wtt_arrival"),
            "dep": first(loc, "dep", "departure", "departure_time", "wtt_departure"),
            "pass": first(loc, "pass", "pass_time", "wtt_pass"),
            "platform": first(loc, "platform", "plat"),
            "updated_at": now_iso(),
        }.items() if v is not None})
    return rows


def load_source() -> List[Dict[str, Any]]:
    if SCHEDULE_SOURCE_FILE and os.path.exists(SCHEDULE_SOURCE_FILE):
        log(f"Loading schedule source file {SCHEDULE_SOURCE_FILE}")
        with open(SCHEDULE_SOURCE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    elif SCHEDULE_SOURCE_URL:
        log(f"Fetching schedule source URL {SCHEDULE_SOURCE_URL}")
        req = request.Request(SCHEDULE_SOURCE_URL, headers={"User-Agent": "ActonBridgeScheduleLoader/1.0"})
        with request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    else:
        log("No SCHEDULE_SOURCE_URL or SCHEDULE_SOURCE_FILE set. Nothing to load.")
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    for key in ["services", "schedules", "rows", "data", "items"]:
        if isinstance(data, dict) and isinstance(data.get(key), list):
            return [x for x in data[key] if isinstance(x, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def supabase_upsert(table: str, rows: List[Dict[str, Any]], on_conflict: Optional[str] = None) -> None:
    if not rows:
        return
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY/SUPABASE_KEY")
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
    body = json.dumps(rows).encode("utf-8")
    req = request.Request(url, data=body, method="POST", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    })
    try:
        with request.urlopen(req, timeout=60) as resp:
            resp.read()
    except error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Supabase {e.code} upsert {table}: {detail}") from e


def chunks(items: List[Dict[str, Any]], size: int = 500) -> Iterable[List[Dict[str, Any]]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def main() -> int:
    log("SCHEDULE LOADER PATHING CAPTURE ACTIVE")
    records = load_source()
    if not records:
        log("No schedule records found; exiting cleanly.")
        return 0
    services: List[Dict[str, Any]] = []
    locations: List[Dict[str, Any]] = []
    for rec in records:
        svc = extract_service(rec)
        if not svc.get("train_id") and not svc.get("train_uid"):
            continue
        services.append(svc)
        locations.extend(extract_locations(rec, svc))
    log(f"Prepared services={len(services)} locations={len(locations)}")
    for batch in chunks(services):
        supabase_upsert(SERVICE_TABLE, batch, on_conflict="train_uid,schedule_start_date")
    for batch in chunks(locations):
        try:
            supabase_upsert(LOCATION_TABLE, batch, on_conflict="train_uid,schedule_start_date,location_order")
        except Exception as exc:
            log(f"Location upsert skipped/failed: {exc}")
            break
    log("Schedule loader complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
