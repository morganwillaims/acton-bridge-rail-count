#!/usr/bin/env python3
"""
Runs the Supabase loco prediction engine.

This uses only your own approved loco sightings in Supabase.
No RTT/API allocation data is used.
"""

import json
import os
import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

DAYS_AHEAD = int(os.environ.get("LOCO_PREDICT_DAYS_AHEAD", "3"))
LOOKBACK_DAYS = int(os.environ.get("LOCO_PREDICT_LOOKBACK_DAYS", "21"))
MIN_EVIDENCE = int(os.environ.get("LOCO_PREDICT_MIN_EVIDENCE", "2"))
MIN_TOP_SHARE = float(os.environ.get("LOCO_PREDICT_MIN_TOP_SHARE", "0.60"))

headers = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json",
}

payload = {
    "days_ahead": DAYS_AHEAD,
    "lookback_days": LOOKBACK_DAYS,
    "min_evidence": MIN_EVIDENCE,
    "min_top_share": MIN_TOP_SHARE,
}

url = f"{SUPABASE_URL}/rest/v1/rpc/refresh_loco_predictions_next_days"

print("Running loco prediction engine...")
print(json.dumps(payload, indent=2))

response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
print("Status:", response.status_code)
print(response.text[:5000])

response.raise_for_status()
