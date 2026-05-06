import json
import os
import time
from datetime import datetime, timezone

import requests
import stomp

NETWORK_RAIL_USERNAME = os.environ["NETWORK_RAIL_USERNAME"]
NETWORK_RAIL_PASSWORD = os.environ["NETWORK_RAIL_PASSWORD"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

TOPIC = "/topic/TD_ALL_SIG_AREA"
TD_AREA_FILTER = os.environ.get("TD_AREA_FILTER", "CE").upper()
LISTEN_SECONDS = int(os.environ.get("LISTEN_SECONDS", "240"))

# Safety default: do not refill td_berth_events.
# Live freight only needs td_current_berths.
STORE_TD_EVENTS = os.environ.get("STORE_TD_EVENTS", "false").lower() in ("1", "true", "yes", "y")


def headers(return_representation=False):
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


def post_row(table, row):
    response = requests.post(
        rest(table),
        headers=headers(False),
        data=json.dumps(row),
        timeout=20,
    )
    if response.status_code not in (200, 201, 204):
        print(f"{table} insert warning: {response.status_code} {response.text}", flush=True)


def upsert_row(table, row, conflict):
    response = requests.post(
        rest(table) + f"?on_conflict={conflict}",
        headers={**headers(False), "Prefer": "resolution=merge-duplicates,return=minimal"},
        data=json.dumps(row),
        timeout=20,
    )
    if response.status_code not in (200, 201, 204):
        print(f"{table} upsert warning: {response.status_code} {response.text}", flush=True)


def patch_current_berth(area_id, berth, description):
    if not area_id or not berth:
        return
    upsert_row(
        "td_current_berths",
        {
            "area_id": area_id,
            "berth": berth,
            "description": description,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        "area_id,berth",
    )


def clear_current_berth(area_id, berth):
    if not area_id or not berth:
        return
    upsert_row(
        "td_current_berths",
        {
            "area_id": area_id,
            "berth": berth,
            "description": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        "area_id,berth",
    )


def get_berth_map():
    try:
        response = requests.get(
            rest("td_berth_map") + "?select=area_id,berth,side,label&area_id=eq." + TD_AREA_FILTER,
            headers=headers(True),
            timeout=20,
        )
        response.raise_for_status()
        return {(row["area_id"], row["berth"]): row for row in response.json()}
    except Exception as exc:
        print(f"td_berth_map read warning: {exc}", flush=True)
        return {}


def update_live_status(berth_map):
    try:
        response = requests.get(
            rest("td_current_berths") + "?select=area_id,berth,description,updated_at&area_id=eq." + TD_AREA_FILTER + "&limit=1000",
            headers=headers(True),
            timeout=20,
        )
        response.raise_for_status()
        current = response.json()
    except Exception as exc:
        print(f"td_current_berths read warning: {exc}", flush=True)
        return

    hartford = None
    weaver = None

    for row in current:
        if not row.get("description"):
            continue
        mapping = berth_map.get((row.get("area_id"), row.get("berth")))
        if not mapping:
            continue
        if mapping.get("side") == "hartford":
            hartford = row
        elif mapping.get("side") == "weaver":
            weaver = row

    upsert_row(
        "acton_live_status",
        {
            "id": "ACB",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "td_last_seen": datetime.now(timezone.utc).isoformat(),
            "hartford_headcode": hartford.get("description") if hartford else None,
            "hartford_berth": hartford.get("berth") if hartford else None,
            "weaver_headcode": weaver.get("description") if weaver else None,
            "weaver_berth": weaver.get("berth") if weaver else None,
            "confidence": "mapped" if (hartford or weaver) else "discovery",
            "note": "TD active; live freight matching uses fresh td_current_berths.",
        },
        "id",
    )


def parse_td_message(message):
    parsed = json.loads(message)
    if isinstance(parsed, dict):
        parsed = [parsed]

    rows = []
    for item in parsed:
        if not isinstance(item, dict):
            continue

        for msg_type, body in item.items():
            if not isinstance(body, dict):
                continue

            area_id = clean(body.get("area_id") or body.get("area"))
            if area_id and area_id.upper() != TD_AREA_FILTER:
                continue

            description = clean(body.get("descr") or body.get("description"))
            from_berth = clean(body.get("from") or body.get("from_berth") or body.get("fromBerth"))
            to_berth = clean(body.get("to") or body.get("to_berth") or body.get("toBerth"))
            explicit_berth = clean(body.get("berth"))

            if msg_type in ("CA_MSG", "CT_MSG"):
                berth = to_berth or explicit_berth or from_berth
            elif msg_type in ("CB_MSG", "CC_MSG"):
                berth = explicit_berth or from_berth or to_berth
            else:
                berth = explicit_berth or to_berth or from_berth

            rows.append(
                {
                    "event_ts": datetime.now(timezone.utc).isoformat(),
                    "area_id": area_id,
                    "msg_type": msg_type,
                    "from_berth": from_berth,
                    "to_berth": to_berth,
                    "berth": berth,
                    "description": description,
                    "raw": item,
                }
            )
    return rows


class Listener(stomp.ConnectionListener):
    def __init__(self):
        self.messages = 0
        self.live_updates = 0
        self.events_saved = 0
        self.last_status_update = 0
        self.berth_map = {}

    def on_message(self, frame):
        self.messages += 1
        try:
            rows = parse_td_message(frame.body)
            if not rows:
                return

            if not self.berth_map or time.time() - self.last_status_update > 60:
                self.berth_map = get_berth_map()

            for row in rows:
                if STORE_TD_EVENTS:
                    post_row("td_berth_events", row)
                    self.events_saved += 1

                msg_type = row.get("msg_type")
                area_id = row.get("area_id")
                description = row.get("description")
                from_berth = row.get("from_berth")
                to_berth = row.get("to_berth")
                berth = row.get("berth")

                if msg_type in ("CA_MSG", "CT_MSG"):
                    if to_berth and description:
                        patch_current_berth(area_id, to_berth, description)
                    if from_berth and from_berth != to_berth:
                        clear_current_berth(area_id, from_berth)

                elif msg_type in ("CB_MSG", "CC_MSG"):
                    if berth:
                        clear_current_berth(area_id, berth)

                else:
                    if berth and description:
                        patch_current_berth(area_id, berth, description)

                self.live_updates += 1

            if time.time() - self.last_status_update > 15:
                update_live_status(self.berth_map)
                self.last_status_update = time.time()

        except Exception as exc:
            print(f"TD parse/save error: {exc}", flush=True)


def main():
    print(
        f"Connecting to {TOPIC}, area={TD_AREA_FILTER}, listen={LISTEN_SECONDS}s, STORE_TD_EVENTS={STORE_TD_EVENTS}",
        flush=True,
    )

    conn = stomp.Connection(
    [("publicdatafeeds.networkrail.co.uk", 61618)],
    vhost="publicdatafeeds.networkrail.co.uk",
    heartbeats=(10000, 10000)
)
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

    print(
        f"TD collector finished. messages={listener.messages}, live_updates={listener.live_updates}, events_saved={listener.events_saved}",
        flush=True,
    )


if __name__ == "__main__":
    main()
