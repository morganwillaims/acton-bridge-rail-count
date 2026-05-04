import json
import os
import re
import time
from datetime import datetime

import requests
import stomp


NETWORK_RAIL_USERNAME = os.environ["NETWORK_RAIL_USERNAME"]
NETWORK_RAIL_PASSWORD = os.environ["NETWORK_RAIL_PASSWORD"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

TOPIC = "/topic/VSTP_ALL"
LISTEN_SECONDS = int(os.environ.get("LISTEN_SECONDS", "3300"))
ACTON_TIPLOC = "ACBG"

STATIC_NAMES = {
    "ACBG": "Acton Bridge",
    "CREWE": "Crewe",
    "LVRPLSH": "Liverpool South Parkway",
    "LVRPLSL": "Liverpool Lime Street",
    "BHAMNWS": "Birmingham New Street",
    "EUSTON": "London Euston",
    "GLGC": "Glasgow Central",
    "EDGHDHS": "Edge Hill Depot",
    "LVRPGBF": "Liverpool Biomass Terminal",
    "DRAXGBR": "Drax Power Station",
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


def clean(value):
    text = str(value or "").strip()
    return text if text else None


def hhmm(value):
    text = clean(value)
    if not text:
        return None
    text = text.replace(" ", "")
    if len(text) >= 4 and text[:4].isdigit():
        return text[:4]
    return None


def get_tiploc_name(tiploc):
    if not tiploc:
        return None
    if tiploc in STATIC_NAMES:
        return STATIC_NAMES[tiploc]
    try:
        r = requests.get(rest("tiploc_names") + f"?tiploc=eq.{tiploc}&select=name&limit=1", headers=headers(), timeout=20)
        r.raise_for_status()
        rows = r.json()
        if rows:
            return rows[0].get("name") or tiploc
    except Exception:
        pass
    return tiploc


def extract_tiploc(loc):
    try:
        return clean(loc["location"]["tiploc"]["tiploc_id"])
    except Exception:
        return None


def parse_vstp_message(body):
    payload = json.loads(body)
    msg = payload.get("VSTPCIFMsgV1") or payload
    schedule = msg.get("schedule") or {}
    if not schedule:
        return None

    segments = schedule.get("schedule_segment") or []
    if isinstance(segments, dict):
        segments = [segments]
    if not segments:
        return None

    segment = segments[0]
    locations = segment.get("schedule_location") or []
    if isinstance(locations, dict):
        locations = [locations]

    tiplocs = [extract_tiploc(loc) for loc in locations]
    if ACTON_TIPLOC not in tiplocs:
        return None

    origin_tiploc = next((t for t in tiplocs if t), None)
    destination_tiploc = next((t for t in reversed(tiplocs) if t), None)

    return {
        "train_uid": clean(schedule.get("CIF_train_uid")),
        "signalling_id": clean(segment.get("signalling_id")),
        "origin_tiploc": origin_tiploc,
        "origin_name": get_tiploc_name(origin_tiploc),
        "destination_tiploc": destination_tiploc,
        "destination_name": get_tiploc_name(destination_tiploc),
        "atoc_code": clean(segment.get("atoc_code")),
        "schedule_start_date": clean(schedule.get("schedule_start_date")),
        "schedule_end_date": clean(schedule.get("schedule_end_date")),
        "days_runs": clean(schedule.get("schedule_days_runs")),
        "stp_indicator": clean(schedule.get("CIF_stp_indicator")),
        "transaction_type": clean(schedule.get("transaction_type")),
        "locations": locations,
        "raw": payload,
    }


def upsert_service(parsed):
    service_row = {k: parsed[k] for k in [
        "train_uid", "signalling_id", "origin_tiploc", "origin_name",
        "destination_tiploc", "destination_name", "atoc_code",
        "schedule_start_date", "schedule_end_date", "days_runs",
        "stp_indicator", "transaction_type", "raw"
    ]}

    r = requests.post(
        rest("vstp_services") + "?on_conflict=train_uid,signalling_id,schedule_start_date,schedule_end_date",
        headers={**headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
        data=json.dumps(service_row),
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return None
    service_id = rows[0]["id"]

    # Replace locations for this service_id.
    requests.delete(rest("vstp_locations") + f"?service_id=eq.{service_id}", headers=headers(False), timeout=30)

    location_rows = []
    for idx, loc in enumerate(parsed["locations"]):
        tiploc = extract_tiploc(loc)
        if not tiploc:
            continue
        location_rows.append({
            "service_id": service_id,
            "location_order": idx,
            "tiploc": tiploc,
            "location_name": get_tiploc_name(tiploc),
            "signalling_id": parsed.get("signalling_id"),
            "arrival": hhmm(loc.get("scheduled_arrival_time")),
            "departure": hhmm(loc.get("scheduled_departure_time")),
            "pass_time": hhmm(loc.get("scheduled_pass_time")),
            "platform": clean(loc.get("CIF_platform")),
            "line": clean(loc.get("CIF_line")),
            "path": clean(loc.get("CIF_path")),
        })

    if location_rows:
        r = requests.post(rest("vstp_locations"), headers=headers(False), data=json.dumps(location_rows), timeout=30)
        if r.status_code not in (200, 201, 204):
            print(f"VSTP location insert warning: {r.status_code} {r.text}", flush=True)

    return service_id


class Listener(stomp.ConnectionListener):
    def __init__(self):
        self.count = 0
        self.saved = 0

    def on_message(self, frame):
        self.count += 1
        try:
            parsed = parse_vstp_message(frame.body)
            if not parsed:
                return
            if not parsed.get("signalling_id"):
                return
            service_id = upsert_service(parsed)
            self.saved += 1
            print(f"Saved VSTP {parsed['signalling_id']} {parsed['origin_name']} -> {parsed['destination_name']} service={service_id}", flush=True)
        except Exception as exc:
            print(f"VSTP parse/save error: {exc}", flush=True)


def main():
    print(f"Connecting to Network Rail VSTP feed for {LISTEN_SECONDS}s...", flush=True)
    conn = stomp.Connection([("datafeeds.networkrail.co.uk", 61618)], heartbeats=(10000, 10000))
    listener = Listener()
    conn.set_listener("", listener)
    conn.connect(NETWORK_RAIL_USERNAME, NETWORK_RAIL_PASSWORD, wait=True)
    conn.subscribe(destination=TOPIC, id=1, ack="auto")

    started = time.time()
    try:
        while time.time() - started < LISTEN_SECONDS:
            time.sleep(1)
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass

    print(f"VSTP collector finished. Messages={listener.count}, saved Acton Bridge VSTP={listener.saved}", flush=True)


if __name__ == "__main__":
    main()
