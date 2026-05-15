#!/usr/bin/env python3
"""
Acton Bridge VSTP Collector — pathing capture version

Purpose
- Pull VSTP / short-term schedule records when your existing source endpoint/file is available.
- Upsert service-level data to public.vstp_services.
- Upsert location-level rows to public.vstp_locations when locations are present.
- Preserve pathing / traction fields so service detail pages can later show diesel/electric/timing-load info.

Environment variables supported
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY
- VSTP_SOURCE_URL optional: JSON endpoint/file URL for VSTP records
- VSTP_SOURCE_FILE optional: local JSON file path for VSTP records

This file is deliberately defensive because Network Rail/VSTP feeds can arrive in slightly different shapes.
It will not invent pathing data; if fields are absent, it stores blanks/nulls.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone, date
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib import request, error

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or ""
VSTP_SOURCE_URL = os.getenv("VSTP_SOURCE_URL", "")
VSTP_SOURCE_FILE = os.getenv("VSTP_SOURCE_FILE", "")
STATION_CRS = os.getenv("STATION_CRS", "ACB")
STATION_TIPLOC = os.getenv("STATION_TIPLOC", "ACTONB").upper()

SERVICE_TABLE = "vstp_services"
LOCATION_TABLE = "vstp_locations"

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
    """Return first non-empty value from direct/nested-ish key variants."""
    for key in keys:
        if key in record and clean(record.get(key)):
            return clean(record.get(key))
        # try case-insensitive flat keys
        lower = key.lower()
        for k, v in record.items():
            if str(k).lower() == lower and clean(v):
                return clean(v)
    return None


def nested_first(record: Dict[str, Any], *paths: str) -> Optional[str]:
    for path in paths:
        cur: Any = record
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and clean(cur):
            return clean(cur)
    return None


def derive_power_label(raw: Dict[str, Any]) -> Tuple[str, str]:
    vals = " ".join(str(raw.get(k) or "") for k in PATHING_FIELDS).upper()
    if not vals.strip():
        return "unknown", "Pathing unknown"
    # common CIF/field encodings. Keep conservative.
    if any(x in vals for x in ["ELECTRIC", "AC ELECTRIC", " OHE", "ELECTRIC LOCO"]):
        return "electric", "Pathed as electric loco"
    if any(x in vals for x in ["DIESEL", "DIESEL LOCO"]):
        return "diesel", "Pathed as diesel loco"
    # Single-letter planned power fields often use D/E, but only use when field is directly power-like.
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
    # Common fields seen in CIF-like, JSON transformed, or custom collector objects.
    out: Dict[str, Optional[str]] = {
        "power_type": first(record, "power_type", "powerType", "power", "traction_power", "plannedTraction"),
        "planned_power": first(record, "planned_power", "plannedPower", "planned_power_type", "planned_power_code"),
        "traction_type": first(record, "traction_type", "tractionType", "traction", "traction_code"),
        "traction_class": first(record, "traction_class", "tractionClass", "train_class", "trainClass", "traction_class_code"),
        "timing_load": first(record, "timing_load", "timingLoad", "timing_load_code", "load", "trailing_load"),
        "operating_characteristics": first(record, "operating_characteristics", "operatingCharacteristics", "op_chars", "operating_characteristics_code"),
        "stock_type": first(record, "stock_type", "stockType", "stock", "rolling_stock"),
        "speed": first(record, "speed", "max_speed", "planned_speed", "maxSpeed"),
    }
    pwr, label = derive_power_label({k: v for k, v in out.items() if v is not None})
    out["pathing_power"] = pwr
    out["pathing_power_label"] = label
    out["pathing_power_source"] = "vstp_collector" if pwr != "unknown" else "vstp_collector_no_source_field"
    return out


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


def extract_service(record: Dict[str, Any]) -> Dict[str, Any]:
    headcode = first(record, "train_id", "headcode", "identity", "trainIdentity", "train_id_current") or ""
    uid = first(record, "uid", "train_uid", "CIF_train_uid", "trainUid")
    start_date = parse_date(first(record, "running_date", "date", "schedule_start_date", "start_date", "service_date"))
    origin = first(record, "origin", "origin_tiploc", "origin_name", "from")
    destination = first(record, "destination", "destination_tiploc", "destination_name", "to")
    stp = first(record, "stp_indicator", "stp", "STP_indicator") or "V"
    toc = first(record, "toc", "atoc_code", "operator", "operator_code")
    row: Dict[str, Any] = {
        "train_uid": uid,
        "train_id": headcode,
        "running_date": start_date,
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
        tiploc = first(loc, "tiploc", "TIPLOC", "location", "location_tiploc")
        arr = first(loc, "arr", "arrival", "arrival_time", "wtt_arrival")
        dep = first(loc, "dep", "departure", "departure_time", "wtt_departure")
        pass_time = first(loc, "pass", "pass_time", "wtt_pass")
        platform = first(loc, "platform", "plat")
        rows.append({
            "train_uid": service.get("train_uid"),
            "train_id": service.get("train_id"),
            "running_date": service.get("running_date"),
            "location_order": i,
            "tiploc": tiploc,
            "arr": arr,
            "dep": dep,
            "pass": pass_time,
            "platform": platform,
            "updated_at": now_iso(),
        })
    return [{k: v for k, v in row.items() if v is not None} for row in rows]


def load_source() -> List[Dict[str, Any]]:
    if VSTP_SOURCE_FILE and os.path.exists(VSTP_SOURCE_FILE):
        log(f"Loading VSTP source file {VSTP_SOURCE_FILE}")
        with open(VSTP_SOURCE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    elif VSTP_SOURCE_URL:
        log(f"Fetching VSTP source URL {VSTP_SOURCE_URL}")
        req = request.Request(VSTP_SOURCE_URL, headers={"User-Agent": "ActonBridgeVSTPCollector/1.0"})
        with request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    else:
        log("No VSTP_SOURCE_URL or VSTP_SOURCE_FILE set. Nothing to collect.")
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    for key in ["services", "vstp", "rows", "data", "items"]:
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
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
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
    log("VSTP COLLECTOR PATHING CAPTURE ACTIVE")
    records = load_source()
    if not records:
        log("No VSTP records found; exiting cleanly.")
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
        supabase_upsert(SERVICE_TABLE, batch, on_conflict="train_id,running_date")
    # Location unique constraints vary by project; try without conflict first.
    for batch in chunks(locations):
        try:
            supabase_upsert(LOCATION_TABLE, batch, on_conflict="train_id,running_date,location_order")
        except Exception as exc:
            log(f"Location upsert skipped/failed: {exc}")
            break
    log("VSTP collector complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
