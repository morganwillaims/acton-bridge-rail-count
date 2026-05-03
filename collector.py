import json
import os
import signal
import time
from datetime import datetime, timezone
from typing import Any

import requests
import stomp


NETWORK_RAIL_USERNAME = os.environ["NETWORK_RAIL_USERNAME"]
NETWORK_RAIL_PASSWORD = os.environ["NETWORK_RAIL_PASSWORD"]

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

LISTEN_SECONDS = int(os.environ.get("LISTEN_SECONDS", "3300"))

ACTON_BRIDGE_STANOX = "37001"
ACTON_BRIDGE_CRS = "ACB"
ACTON_BRIDGE_TIPLOC = "ACBG"

SUPABASE_TABLE_URL = f"{SUPABASE_URL}/rest/v1/station_movements"

running = True
captured_count = 0
seen_messages = 0


def stop_gracefully(_signum=None, _frame=None):
    global running
    running = False


signal.signal(signal.SIGTERM, stop_gracefully)
signal.signal(signal.SIGINT, stop_gracefully)


def headers(return_representation: bool = False) -> dict[str, str]:
    prefer = "resolution=merge-duplicates"
    prefer += ",return=representation" if return_representation else ",return=minimal"
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def table_url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def extract_headcode(raw_train_id: str) -> str:
    value = str(raw_train_id or "").strip().upper()

    if len(value) == 4 and value[0].isdigit() and value[1].isalpha():
        return value

    if len(value) >= 6:
        candidate = value[2:6]
        if len(candidate) == 4 and candidate[0].isdigit() and candidate[1].isalpha():
            return candidate

    return value


def classify_train(display_headcode: str) -> str:
    if not display_headcode:
        return "other"

    first = display_headcode[0]

    if first in {"4", "6", "7", "8"}:
        return "freight"

    if first in {"1", "2", "9"}:
        return "passenger"

    return "other"


def trust_time_to_date_and_hhmm(timestamp_ms: Any) -> tuple[str, str]:
    if not timestamp_ms:
        now = datetime.now(timezone.utc)
        return now.date().isoformat(), now.strftime("%H:%M")

    dt = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc)
    return dt.date().isoformat(), dt.strftime("%H:%M")


def time_to_minutes(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip().upper().replace("H", "")
    if len(text) < 4 or not text[:4].isdigit():
        return None
    return int(text[:2]) * 60 + int(text[2:4])


def find_schedule_enrichment(headcode: str, actual_time: str, running_date: str) -> dict:
    url = (
        table_url("schedule_services") +
        f"?signalling_id=eq.{headcode}"
        "&select=*,schedule_locations(*)"
    )

    try:
        r = requests.get(url, headers=headers(return_representation=True), timeout=30)
        if r.status_code != 200:
            return {}
        schedules = r.json()
    except Exception:
        return {}

    movement_minutes = time_to_minutes(actual_time)
    best = None
    best_score = 999999

    for service in schedules:
        if service.get("schedule_start_date") and running_date < service["schedule_start_date"]:
            continue
        if service.get("schedule_end_date") and running_date > service["schedule_end_date"]:
            continue

        acb_locs = [
            loc for loc in service.get("schedule_locations", [])
            if loc.get("tiploc") == ACTON_BRIDGE_TIPLOC
        ]
        if not acb_locs:
            continue

        loc = acb_locs[0]
        booked = loc.get("pass_time") or loc.get("departure") or loc.get("arrival")
        booked_minutes = time_to_minutes(booked)

        if movement_minutes is None or booked_minutes is None:
            score = 500
        else:
            score = min(
                abs(movement_minutes - booked_minutes),
                abs((movement_minutes + 60) - booked_minutes),
                abs((movement_minutes - 60) - booked_minutes),
            )

        if score < best_score:
            best_score = score
            best = (service, loc)

    if not best:
        return {}

    service, loc = best
    return {
        "origin": service.get("origin_name") or service.get("origin_tiploc"),
        "destination": service.get("destination_name") or service.get("destination_tiploc"),
        "toc": service.get("atoc_code"),
        "planned_time": loc.get("pass_time") or loc.get("departure") or loc.get("arrival"),
        "platform": loc.get("platform"),
    }


def insert_movement(row: dict) -> bool:
    r = requests.post(
        SUPABASE_TABLE_URL,
        headers=headers(return_representation=False),
        params={"on_conflict": "running_date,station_stanox,train_id,actual_time,event_type"},
        data=json.dumps(row),
        timeout=20,
    )

    if r.status_code not in (200, 201, 204):
        print("Supabase insert failed:", r.status_code, r.text, flush=True)
        return False

    return True


class TrainMovementListener(stomp.ConnectionListener):
    def on_error(self, frame):
        print("STOMP error:", frame.body, flush=True)

    def on_disconnected(self):
        print("Disconnected from Network Rail feed", flush=True)

    def on_message(self, frame):
        global captured_count, seen_messages

        try:
            messages = json.loads(frame.body)
        except json.JSONDecodeError:
            print("Could not decode message body", flush=True)
            return

        if not isinstance(messages, list):
            messages = [messages]

        seen_messages += len(messages)

        for wrapper in messages:
            message = wrapper.get("body", wrapper)

            loc_stanox = str(message.get("loc_stanox") or "")
            if loc_stanox != ACTON_BRIDGE_STANOX:
                continue

            raw_train_id = str(message.get("train_id") or "").strip()
            display_headcode = extract_headcode(raw_train_id)

            event_type = str(message.get("event_type") or "PASS").strip()
            running_date, actual_time = trust_time_to_date_and_hhmm(message.get("actual_timestamp"))

            enrichment = find_schedule_enrichment(display_headcode, actual_time, running_date)

            row = {
                "running_date": running_date,
                "station_crs": ACTON_BRIDGE_CRS,
                "station_tiploc": ACTON_BRIDGE_TIPLOC,
                "station_stanox": ACTON_BRIDGE_STANOX,
                "train_id": display_headcode,
                "train_type": classify_train(display_headcode),
                "origin": enrichment.get("origin"),
                "destination": enrichment.get("destination"),
                "toc": enrichment.get("toc"),
                "platform": str(message.get("platform") or enrichment.get("platform") or "").strip() or None,
                "planned_time": enrichment.get("planned_time"),
                "actual_time": actual_time,
                "event_type": event_type,
                "status": "Passed",
                "source": "Network Rail TRUST",
                "raw": {
                    **message,
                    "_raw_train_id": raw_train_id,
                    "_display_headcode": display_headcode,
                    "_schedule_enriched": bool(enrichment),
                },
            }

            print(
                f"CAPTURED {actual_time} raw={raw_train_id} headcode={display_headcode} "
                f"{row['train_type']} {row.get('origin') or 'Unknown'} to {row.get('destination') or 'Unknown'}",
                flush=True,
            )
            if insert_movement(row):
                captured_count += 1


def connect_and_listen(listen_seconds: int) -> None:
    hosts = [("publicdatafeeds.networkrail.co.uk", 61618)]

    conn = stomp.Connection12(host_and_ports=hosts, heartbeats=(10000, 10000))
    conn.set_listener("", TrainMovementListener())

    print("Connecting to Network Rail Train Movements feed...", flush=True)
    conn.connect(
        username=NETWORK_RAIL_USERNAME,
        passcode=NETWORK_RAIL_PASSWORD,
        wait=True,
    )

    conn.subscribe(destination="/topic/TRAIN_MVT_ALL_TOC", id=1, ack="auto")

    start = time.time()
    end = start + listen_seconds
    print(f"Listening for Acton Bridge train movements for {listen_seconds} seconds...", flush=True)

    last_status = 0
    while running and time.time() < end:
        elapsed = int(time.time() - start)

        if elapsed - last_status >= 300:
            remaining = max(0, int(end - time.time()))
            print(
                f"Collector heartbeat: elapsed={elapsed}s remaining={remaining}s "
                f"feed_messages_seen={seen_messages} acton_bridge_captures={captured_count}",
                flush=True,
            )
            last_status = elapsed

        if not conn.is_connected():
            print("Connection dropped; reconnecting...", flush=True)
            break

        time.sleep(5)

    try:
        if conn.is_connected():
            conn.disconnect()
    except Exception as exc:
        print(f"Disconnect warning: {exc}", flush=True)


def main():
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"Collector started at {started} UTC", flush=True)

    overall_end = time.time() + LISTEN_SECONDS

    while running and time.time() < overall_end:
        remaining = int(overall_end - time.time())
        if remaining <= 0:
            break

        try:
            connect_and_listen(remaining)
        except Exception as exc:
            print(f"Collector connection/run error: {repr(exc)}", flush=True)
            if time.time() < overall_end:
                print("Waiting 15 seconds before reconnect attempt...", flush=True)
                time.sleep(15)

    print(
        f"Collector finished. feed_messages_seen={seen_messages} "
        f"acton_bridge_captures={captured_count}",
        flush=True,
    )


if __name__ == "__main__":
    main()
