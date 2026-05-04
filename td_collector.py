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
LISTEN_SECONDS = int(os.environ.get("LISTEN_SECONDS", "3300"))

# Acton Bridge is in the Crewe PSB area in the public TD list. If this proves wrong,
# change TD_AREA_FILTER in GitHub Actions secrets/env to the real TD area ID.
TD_AREA_FILTER = os.environ.get("TD_AREA_FILTER", "CE").upper()


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


def post(table, row):
    r = requests.post(rest(table), headers=headers(False), data=json.dumps(row), timeout=20)
    if r.status_code not in (200, 201, 204):
        print(f"Insert {table} warning: {r.status_code} {r.text}", flush=True)


def patch_current_berth(area_id, berth, description):
    if not area_id or not berth:
        return
    row = {
        "area_id": area_id,
        "berth": berth,
        "description": description,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    r = requests.post(
        rest("td_current_berths") + "?on_conflict=area_id,berth",
        headers={**headers(False), "Prefer": "resolution=merge-duplicates,return=minimal"},
        data=json.dumps(row),
        timeout=20,
    )
    if r.status_code not in (200, 201, 204):
        print(f"td_current_berths upsert warning: {r.status_code} {r.text}", flush=True)


def get_berth_map():
    try:
        r = requests.get(rest("td_berth_map") + "?select=area_id,berth,side,label", headers=headers(), timeout=20)
        r.raise_for_status()
        return {(row["area_id"], row["berth"]): row for row in r.json()}
    except Exception as exc:
        print(f"TD berth map read warning: {exc}", flush=True)
        return {}


def update_live_status(berth_map):
    # Pull current mapped berths.
    try:
        r = requests.get(rest("td_current_berths") + "?select=area_id,berth,description,updated_at", headers=headers(), timeout=20)
        r.raise_for_status()
        current = r.json()
    except Exception as exc:
        print(f"TD current read warning: {exc}", flush=True)
        return

    hartford = None
    weaver = None

    for row in current:
        key = (row["area_id"], row["berth"])
        mapping = berth_map.get(key)
        if not mapping:
            continue
        if mapping["side"] == "hartford" and row.get("description"):
            hartford = row
        elif mapping["side"] == "weaver" and row.get("description"):
            weaver = row

    status = {
        "id": "ACB",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "td_last_seen": datetime.now(timezone.utc).isoformat(),
        "hartford_headcode": hartford.get("description") if hartford else None,
        "hartford_berth": hartford.get("berth") if hartford else None,
        "weaver_headcode": weaver.get("description") if weaver else None,
        "weaver_berth": weaver.get("berth") if weaver else None,
        "confidence": "mapped" if (hartford or weaver) else "discovery",
        "note": "TD active; add berth mappings in td_berth_map to improve approach display",
    }

    r = requests.post(
        rest("acton_live_status") + "?on_conflict=id",
        headers={**headers(False), "Prefer": "resolution=merge-duplicates,return=minimal"},
        data=json.dumps(status),
        timeout=20,
    )
    if r.status_code not in (200, 201, 204):
        print(f"acton_live_status upsert warning: {r.status_code} {r.text}", flush=True)


def parse_td_message(msg):
    # TD messages are usually arrays of dicts, each containing a single key such as CA_MSG, CB_MSG, CC_MSG, CT_MSG.
    parsed = json.loads(msg)
    if isinstance(parsed, dict):
        parsed = [parsed]

    rows = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        for msg_type, body in item.items():
            if not isinstance(body, dict):
                continue

            area_id = clean(body.get("area_id") or body.get("area") or body.get("descr"))
            if area_id and area_id.upper() != TD_AREA_FILTER:
                continue

            desc = clean(body.get("descr") or body.get("description"))
            berth = clean(body.get("berth"))
            from_berth = clean(body.get("from") or body.get("from_berth"))
            to_berth = clean(body.get("to") or body.get("to_berth"))

            # Common C-class structures:
            # CA/CB/CC may use berth/descr; CT uses from/to/descr.
            if msg_type == "CT_MSG":
                berth = to_berth or berth
            elif msg_type in ("CB_MSG", "CC_MSG"):
                desc = None

            rows.append({
                "event_ts": datetime.now(timezone.utc).isoformat(),
                "area_id": area_id,
                "msg_type": msg_type,
                "from_berth": from_berth,
                "to_berth": to_berth,
                "berth": berth,
                "description": desc,
                "raw": item,
            })
    return rows


class Listener(stomp.ConnectionListener):
    def __init__(self):
        self.count = 0
        self.saved = 0
        self.last_status_update = 0
        self.berth_map = {}

    def on_message(self, frame):
        self.count += 1
        try:
            rows = parse_td_message(frame.body)
            if not rows:
                return

            if not self.berth_map or time.time() - self.last_status_update > 60:
                self.berth_map = get_berth_map()

            for row in rows:
                post("td_berth_events", row)
                if row.get("berth"):
                    patch_current_berth(row.get("area_id"), row.get("berth"), row.get("description"))
                self.saved += 1

            if time.time() - self.last_status_update > 15:
                update_live_status(self.berth_map)
                self.last_status_update = time.time()

        except Exception as exc:
            print(f"TD parse/save error: {exc}", flush=True)


def main():
    print(f"Connecting to TD feed {TOPIC}, filtering area {TD_AREA_FILTER}, for {LISTEN_SECONDS}s...", flush=True)
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

    print(f"TD collector finished. Messages={listener.count}, saved area events={listener.saved}", flush=True)


if __name__ == "__main__":
    main()
