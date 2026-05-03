import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests
import stomp


NETWORK_RAIL_USERNAME = os.environ["NETWORK_RAIL_USERNAME"]
NETWORK_RAIL_PASSWORD = os.environ["NETWORK_RAIL_PASSWORD"]

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

# Acton Bridge identifiers
ACTON_BRIDGE_STANOX = "37001"
ACTON_BRIDGE_CRS = "ACB"
ACTON_BRIDGE_TIPLOC = "ACBG"

SUPABASE_TABLE_URL = f"{SUPABASE_URL}/rest/v1/station_movements"


def classify_train(train_id: str) -> str:
    """
    Rough GB headcode classification.
    4/6/7/8 are normally freight/empty stock-style non-passenger classes.
    1/2/9 are normally passenger.
    This will be improved later when we add schedule enrichment.
    """
    if not train_id:
        return "other"

    first = train_id[0]

    if first in {"4", "6", "7", "8"}:
        return "freight"

    if first in {"1", "2", "9"}:
        return "passenger"

    return "other"


def trust_time_to_date_and_hhmm(timestamp_ms: Any) -> tuple[str, str]:
    """
    Network Rail TRUST actual_timestamp is normally epoch milliseconds.
    We store UTC for the first version. Later we can convert to Europe/London.
    """
    if not timestamp_ms:
        now = datetime.now(timezone.utc)
        return now.date().isoformat(), now.strftime("%H:%M")

    dt = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc)
    return dt.date().isoformat(), dt.strftime("%H:%M")


def insert_movement(row: dict) -> None:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    response = requests.post(
        SUPABASE_TABLE_URL,
        headers=headers,
        params={"on_conflict": "running_date,station_stanox,train_id,actual_time,event_type"},
        data=json.dumps(row),
        timeout=20,
    )

    if response.status_code not in (200, 201, 204):
        print("Supabase insert failed:", response.status_code, response.text)


class TrainMovementListener(stomp.ConnectionListener):
    def on_error(self, frame):
        print("STOMP error:", frame.body)

    def on_disconnected(self):
        print("Disconnected from Network Rail feed")

    def on_message(self, frame):
        try:
            messages = json.loads(frame.body)
        except json.JSONDecodeError:
            print("Could not decode message body")
            return

        if not isinstance(messages, list):
            messages = [messages]

        for wrapper in messages:
            # Network Rail messages are commonly wrapped as {"header": ..., "body": {...}}
            message = wrapper.get("body", wrapper)

            loc_stanox = str(message.get("loc_stanox") or "")
            if loc_stanox != ACTON_BRIDGE_STANOX:
                continue

            train_id = str(message.get("train_id") or "").strip()
            event_type = str(message.get("event_type") or "PASS").strip()
            running_date, actual_time = trust_time_to_date_and_hhmm(message.get("actual_timestamp"))

            row = {
                "running_date": running_date,
                "station_crs": ACTON_BRIDGE_CRS,
                "station_tiploc": ACTON_BRIDGE_TIPLOC,
                "station_stanox": ACTON_BRIDGE_STANOX,
                "train_id": train_id,
                "train_type": classify_train(train_id),
                "origin": None,
                "destination": None,
                "toc": None,
                "planned_time": None,
                "actual_time": actual_time,
                "event_type": event_type,
                "status": "Passed",
                "source": "Network Rail TRUST",
                "raw": message,
            }

            print(f"{actual_time} {train_id} {row['train_type']} through Acton Bridge")
            insert_movement(row)


def main():
    hosts = [("publicdatafeeds.networkrail.co.uk", 61618)]

    conn = stomp.Connection12(host_and_ports=hosts, heartbeats=(10000, 10000))
    conn.set_listener("", TrainMovementListener())

    print("Connecting to Network Rail Train Movements feed...")
    conn.connect(
        username=NETWORK_RAIL_USERNAME,
        passcode=NETWORK_RAIL_PASSWORD,
        wait=True,
    )

    conn.subscribe(
        destination="/topic/TRAIN_MVT_ALL_TOC",
        id=1,
        ack="auto",
    )

    print("Listening for Acton Bridge train movements for 4 minutes...")
    time.sleep(240)

    conn.disconnect()
    print("Collector finished.")


if __name__ == "__main__":
    main()
