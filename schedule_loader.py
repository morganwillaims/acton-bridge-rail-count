#!/usr/bin/env python3
"""
Acton Bridge Schedule Loader - Full Raw + Pathing Capture v1

Replacement/template schedule_loader.py.

It preserves the full raw source schedule record in schedule_services.raw and
extracts pathing/power/timing-load fields if the upstream source contains them.

If your existing schedule_loader.py already downloads Network Rail data correctly,
you can instead merge these functions into it:
- first_value
- extract_pathing_fields
- build_service_row
"""

import os, sys, json, gzip, urllib.request, urllib.error
from datetime import datetime, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_ANON_KEY") or ""
SCHEDULE_SOURCE_URL = os.environ.get("SCHEDULE_SOURCE_URL", "")
SCHEDULE_SOURCE_FILE = os.environ.get("SCHEDULE_SOURCE_FILE", "")

def log(msg):
    print(f"[schedule_loader_full_raw_v1] {msg}", flush=True)

def clean(v):
    if v is None or v == "" or v == [] or v == {}:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"))
    s = str(v).strip()
    return s or None

def first_value(obj, keys):
    if not isinstance(obj, dict):
        return None
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", [], {}):
            return v
    for ck in ("new_schedule_segment", "applicable_timetable", "schedule", "service", "record"):
        nested = obj.get(ck)
        if isinstance(nested, dict):
            v = first_value(nested, keys)
            if v not in (None, "", [], {}):
                return v
    return None

def normalise_power(v):
    s = clean(v)
    if not s:
        return None
    u = s.upper().strip()
    if u in ("D", "DM", "DIESEL", "DSL") or "DIESEL" in u:
        if "ELECTRIC" in u:
            return "diesel_electric"
        return "diesel"
    if u in ("E", "EL", "ELECTRIC", "ELEC") or "ELECTRIC" in u:
        return "electric"
    if u in ("ED", "DE", "D/E", "E/D", "BI", "BI-MODE", "BIMODE"):
        return "diesel_electric"
    return u.lower()

def pathing_label(power):
    if power == "diesel":
        return "Pathed as diesel loco"
    if power == "electric":
        return "Pathed as electric loco"
    if power == "diesel_electric":
        return "Pathed as diesel/electric"
    if power:
        return f"Pathed as {power}"
    return None

def extract_pathing_fields(raw):
    power_type = clean(first_value(raw, ["power_type","CIF_power_type","cif_power_type","traction_power_type","power"]))
    planned_power = clean(first_value(raw, ["planned_power","plannedPower","planned_power_type","CIF_power_type"]))
    traction_type = clean(first_value(raw, ["traction_type","tractionType","traction","CIF_traction_type","cif_traction_type"]))
    traction_class = clean(first_value(raw, ["traction_class","tractionClass","CIF_traction_class","cif_traction_class","train_class","class"]))
    timing_load = clean(first_value(raw, ["timing_load","timingLoad","CIF_timing_load","cif_timing_load","load","trailing_load","trailingLoad"]))
    operating_characteristics = clean(first_value(raw, ["operating_characteristics","operatingCharacteristics","CIF_operating_characteristics","cif_operating_characteristics","operating_chars"]))
    stock_type = clean(first_value(raw, ["stock_type","stockType","stock","rolling_stock"]))
    speed = clean(first_value(raw, ["speed","max_speed","planned_speed","CIF_speed","cif_speed","maximum_speed","plannedSpeed"]))

    power = normalise_power(planned_power or power_type or traction_type)
    has_any = bool(power or timing_load or speed or traction_class or operating_characteristics or stock_type)

    return {
        "power_type": power_type,
        "planned_power": planned_power,
        "traction_type": traction_type,
        "traction_class": traction_class,
        "timing_load": timing_load,
        "operating_characteristics": operating_characteristics,
        "stock_type": stock_type,
        "speed": speed,
        "pathing_power": power,
        "pathing_power_label": pathing_label(power),
        "pathing_power_source": "schedule_loader_full_raw_v1" if has_any else None,
        "pathing_power_updated_at": datetime.now(timezone.utc).isoformat() if has_any else None,
    }

def build_service_row(raw):
    train_uid = clean(first_value(raw, ["train_uid","CIF_train_uid","cif_train_uid","uid"]))
    if not train_uid:
        raise ValueError("missing train_uid/CIF_train_uid")

    return {
        "train_uid": train_uid,
        "stp_indicator": clean(first_value(raw, ["stp_indicator","CIF_stp_indicator","cif_stp_indicator"])),
        "schedule_start_date": clean(first_value(raw, ["schedule_start_date","CIF_schedule_start_date","start_date"])),
        "schedule_end_date": clean(first_value(raw, ["schedule_end_date","CIF_schedule_end_date","end_date"])),
        "days_runs": clean(first_value(raw, ["days_runs","schedule_days_runs","CIF_schedule_days_runs"])),
        "signalling_id": clean(first_value(raw, ["signalling_id","CIF_train_identity","train_identity","headcode"])),
        "atoc_code": clean(first_value(raw, ["atoc_code","CIF_atoc_code","toc"])),
        "train_status": clean(first_value(raw, ["train_status","CIF_train_status"])),
        "train_category": clean(first_value(raw, ["train_category","CIF_train_category","category"])),
        "origin_tiploc": clean(first_value(raw, ["origin_tiploc","origin","from_tiploc"])),
        "origin_name": clean(first_value(raw, ["origin_name","origin_display","from_name"])),
        "origin_departure": clean(first_value(raw, ["origin_departure","origin_dep","departure"])),
        "destination_tiploc": clean(first_value(raw, ["destination_tiploc","destination","to_tiploc"])),
        "destination_name": clean(first_value(raw, ["destination_name","destination_display","to_name"])),
        "destination_arrival": clean(first_value(raw, ["destination_arrival","destination_arr","arrival"])),
        "raw": raw,
        **extract_pathing_fields(raw),
    }

def read_source():
    if SCHEDULE_SOURCE_FILE:
        with open(SCHEDULE_SOURCE_FILE, "rb") as f:
            data = f.read()
    elif SCHEDULE_SOURCE_URL:
        log(f"Downloading {SCHEDULE_SOURCE_URL}")
        req = urllib.request.Request(SCHEDULE_SOURCE_URL, headers={"User-Agent": "acton-bridge-schedule-loader"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
    else:
        raise RuntimeError("Set SCHEDULE_SOURCE_URL or SCHEDULE_SOURCE_FILE, or merge this patch into your current loader.")

    if data[:2] == b"\\x1f\\x8b":
        data = gzip.decompress(data)
    text = data.decode("utf-8", errors="replace").strip()

    if text.startswith("["):
        arr = json.loads(text)
        return [x for x in arr if isinstance(x, dict)]
    if text.startswith("{"):
        obj = json.loads(text)
        for key in ("services","schedules","schedule_services","data","JsonScheduleV1"):
            if isinstance(obj.get(key), list):
                return [x for x in obj[key] if isinstance(x, dict)]
        return [obj]

    rows = []
    for line in text.splitlines():
        try:
            o = json.loads(line)
            if isinstance(o, dict):
                rows.append(o)
        except Exception:
            pass
    return rows

def supabase_upsert(table, rows, conflict="train_uid"):
    if not rows:
        return
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY/SUPABASE_KEY")
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={conflict}"
    req = urllib.request.Request(
        url,
        data=json.dumps(rows, separators=(",", ":"), default=str).encode(),
        method="POST",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            if r.status not in (200, 201, 204):
                raise RuntimeError(f"Supabase HTTP {r.status}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase {e.code}: {e.read().decode(errors='replace')}")

def main():
    log("Starting")
    records = read_source()
    rows, with_pathing = [], 0
    for rec in records:
        try:
            row = build_service_row(rec)
            if row.get("pathing_power") or row.get("timing_load") or row.get("speed"):
                with_pathing += 1
            rows.append(row)
        except Exception as e:
            log(f"Skipped record: {e}")
    log(f"Rows built: {len(rows)}; rows with pathing fields: {with_pathing}")
    for i in range(0, len(rows), 200):
        supabase_upsert("schedule_services", rows[i:i+200])
    log("Done")

if __name__ == "__main__":
    sys.exit(main())
