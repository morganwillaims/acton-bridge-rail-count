#!/usr/bin/env python3
"""
Automatic Pathing Metadata Capture / Pathing Auto Backfill v1

Purpose
-------
Find Acton Bridge freight rows in station_movements where pathing metadata is missing,
match them against schedule_services first and vstp_services second, then copy only
confirmed/cautious metadata into station_movements.

This script does NOT alter the schedule loader. It only reads existing schedule/VSTP rows
and backfills station_movements.

Required env:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY

Optional env:
  TARGET_DATE=YYYY-MM-DD          default: today in Europe/London
  STATION_CODE=ACB                default: ACB
  DRY_RUN=true|false              default: false
  LIMIT=5000                      default: 5000
  ENABLE_SAFE_FREIGHT_FALLBACK=true|false  default: true
  REQUIRE_ACB_LOCATION_MATCH=true|false     default: false
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from zoneinfo import ZoneInfo


METADATA_FIELDS = [
    "pathing_power",
    "pathing_power_label",
    "pathing_power_source",
    "planned_power",
    "power_type",
    "traction_type",
    "traction_class",
    "timing_load",
    "operating_characteristics",
    "stock_type",
    "speed",
    "pathing_power_updated_at",
]

STATION_SELECT = ",".join([
    "id",
    "running_date",
    "train_id",
    "train_type",
    "origin",
    "destination",
    "toc",
    "planned_time",
    "actual_time",
    "status",
    "source",
    "platform",
    *METADATA_FIELDS,
])

SERVICE_SELECT = ",".join([
    "id",
    "train_uid",
    "stp_indicator",
    "schedule_start_date",
    "schedule_end_date",
    "days_runs",
    "signalling_id",
    "atoc_code",
    "train_status",
    "train_category",
    "origin_tiploc",
    "origin_name",
    "origin_departure",
    "destination_tiploc",
    "destination_name",
    "destination_arrival",
    "raw",
    "power_type",
    "planned_power",
    "traction_type",
    "traction_class",
    "timing_load",
    "operating_characteristics",
    "stock_type",
    "speed",
    "pathing_power",
    "pathing_power_label",
    "pathing_power_source",
    "pathing_power_updated_at",
])


@dataclass
class Config:
    supabase_url: str
    service_key: str
    target_date: str
    station_code: str = "ACB"
    dry_run: bool = False
    limit: int = 5000
    safe_fallback: bool = True
    require_acb_location_match: bool = False


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config() -> Config:
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not service_key:
        raise SystemExit("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")

    target_date = os.getenv("TARGET_DATE") or datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    # Validate date early so GitHub Actions fails loudly on typo.
    date.fromisoformat(target_date)

    return Config(
        supabase_url=supabase_url,
        service_key=service_key,
        target_date=target_date,
        station_code=os.getenv("STATION_CODE", "ACB").strip().upper() or "ACB",
        dry_run=env_bool("DRY_RUN", False),
        limit=int(os.getenv("LIMIT", "5000")),
        safe_fallback=env_bool("ENABLE_SAFE_FREIGHT_FALLBACK", True),
        require_acb_location_match=env_bool("REQUIRE_ACB_LOCATION_MATCH", False),
    )


class SupabaseRest:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = f"{cfg.supabase_url}/rest/v1"
        self.headers = {
            "apikey": cfg.service_key,
            "Authorization": f"Bearer {cfg.service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def request(self, method: str, table: str, params: Optional[Dict[str, str]] = None, body: Any = None) -> Any:
        qs = f"?{urlencode(params or {}, safe='(),.*:') }" if params else ""
        url = f"{self.base}/{table}{qs}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = Request(url, data=data, headers=self.headers, method=method)
        try:
            with urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except HTTPError as e:
            msg = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase {method} {table} failed: HTTP {e.code}: {msg}\nURL: {url}") from e

    def get(self, table: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
        result = self.request("GET", table, params=params)
        if result is None:
            return []
        if not isinstance(result, list):
            raise RuntimeError(f"Expected list from {table}, got {type(result)}")
        return result

    def patch_by_id(self, table: str, row_id: Any, updates: Dict[str, Any]) -> Any:
        return self.request(
            "PATCH",
            table,
            params={"id": f"eq.{row_id}"},
            body=updates,
        )

    def insert_audit(self, payload: Dict[str, Any]) -> None:
        try:
            self.request("POST", "pathing_auto_backfill_audit", body=payload)
        except Exception as exc:  # Audit table is optional; do not fail a successful backfill.
            print(f"AUDIT INSERT SKIPPED: {exc}")


def normalise_headcode(value: Any) -> str:
    if not value:
        return ""
    text = str(value).strip().upper()
    # TRUST IDs often include the headcode in the middle/end; prefer a normal 4-char headcode.
    m = re.search(r"\b([0-9][A-Z][0-9]{2})\b", text)
    if m:
        return m.group(1)
    m = re.search(r"([0-9][A-Z][0-9]{2})", text)
    return m.group(1) if m else text[:4]


def compact(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def safe_time_minutes(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Accept 2115, 21:15, 21:15:00.
    m = re.match(r"^(\d{1,2}):?(\d{2})(?::\d{2})?$", text)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 27 and 0 <= mm <= 59):
        return None
    return (hh % 24) * 60 + mm


def minute_gap(a: Optional[int], b: Optional[int]) -> Optional[int]:
    if a is None or b is None:
        return None
    raw = abs(a - b)
    return min(raw, 1440 - raw)


def active_on_date(row: Dict[str, Any], target: str) -> bool:
    d = date.fromisoformat(target)
    start = row.get("schedule_start_date")
    end = row.get("schedule_end_date")
    if start and d < date.fromisoformat(str(start)[:10]):
        return False
    if end and d > date.fromisoformat(str(end)[:10]):
        return False
    days = str(row.get("days_runs") or "").strip()
    if len(days) >= 7 and set(days) <= {"0", "1"}:
        # CIF days_runs is normally Monday first.
        return days[d.weekday()] == "1"
    return True


def is_freight_row(row: Dict[str, Any]) -> bool:
    train_type = str(row.get("train_type") or "").lower()
    headcode = normalise_headcode(row.get("train_id"))
    if "freight" in train_type:
        return True
    return bool(headcode and headcode[0] in {"4", "6", "7"})


def is_missing_pathing(row: Dict[str, Any]) -> bool:
    key_fields = ["pathing_power", "pathing_power_label", "planned_power", "power_type", "traction_type", "timing_load", "speed"]
    return any(row.get(field) in (None, "") for field in key_fields)


def recursively_find(raw: Any, wanted: Iterable[str]) -> Dict[str, Any]:
    wanted_lc = {w.lower(): w for w in wanted}
    found: Dict[str, Any] = {}

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                kl = str(key).lower()
                if kl in wanted_lc and value not in (None, "") and wanted_lc[kl] not in found:
                    found[wanted_lc[kl]] = value
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(raw)
    return found


def metadata_from_service(row: Dict[str, Any], source: str) -> Dict[str, Any]:
    # Direct typed columns are safest.
    meta = {field: row.get(field) for field in METADATA_FIELDS if row.get(field) not in (None, "")}

    # Raw JSON can contain the same fields under new_schedule_segment etc.
    raw = row.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if raw:
        nested = recursively_find(raw, [
            "power_type",
            "planned_power",
            "traction_type",
            "traction_class",
            "timing_load",
            "operating_characteristics",
            "stock_type",
            "speed",
            "pathing_power",
            "pathing_power_label",
        ])
        for key, value in nested.items():
            meta.setdefault(key, value)

    # Never convert uic_code into traction_class. UIC is not a confirmed locomotive class.
    if str(meta.get("traction_class") or "").lower() in {"", "unknown", "none", "null"}:
        meta.pop("traction_class", None)

    if meta:
        meta["pathing_power_source"] = row.get("pathing_power_source") or source
        meta["pathing_power_updated_at"] = datetime.now(timezone.utc).isoformat()

    # Build readable label only from confirmed generic fields.
    label = build_label(meta)
    if label and not meta.get("pathing_power_label"):
        meta["pathing_power_label"] = label
    if not meta.get("pathing_power"):
        meta["pathing_power"] = build_pathing_power(meta)
    return clean_updates(meta)


def build_pathing_power(meta: Dict[str, Any]) -> Optional[str]:
    power = compact(meta.get("power_type") or meta.get("planned_power"))
    traction = compact(meta.get("traction_type"))
    if "DIESEL" in power and ("LOCO" in traction or "LOCOMOTIVE" in traction):
        return "diesel_locomotive"
    if "ELECTRIC" in power and ("LOCO" in traction or "LOCOMOTIVE" in traction):
        return "electric_locomotive"
    if "DIESEL" in power:
        return "diesel"
    if "ELECTRIC" in power:
        return "electric"
    return None


def build_label(meta: Dict[str, Any]) -> Optional[str]:
    power = str(meta.get("power_type") or meta.get("planned_power") or "").strip().lower()
    traction = str(meta.get("traction_type") or "").strip().lower()
    if "diesel" in power and ("loco" in traction or "locomotive" in traction):
        return "Pathed as diesel locomotive"
    if "electric" in power and ("loco" in traction or "locomotive" in traction):
        return "Pathed as electric locomotive"
    if "diesel" in power:
        return "Pathed as diesel"
    if "electric" in power:
        return "Pathed as electric"
    return None


def clean_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
    cleaned: Dict[str, Any] = {}
    for key in METADATA_FIELDS:
        value = updates.get(key)
        if value in (None, ""):
            continue
        if key == "traction_class":
            # Keep only explicit class-like values; do not invent from timing load or UIC.
            text = str(value).strip()
            if not re.search(r"\d", text):
                continue
            value = text
        cleaned[key] = value
    return cleaned


def fallback_metadata(row: Dict[str, Any]) -> Dict[str, Any]:
    headcode = normalise_headcode(row.get("train_id"))
    # Conservative fallback: only for clear freight headcodes. Never fill exact class.
    if not is_freight_row(row) or not headcode or headcode[0] not in {"4", "6", "7"}:
        return {}
    now = datetime.now(timezone.utc).isoformat()
    return {
        "pathing_power": "likely_diesel_locomotive",
        "pathing_power_label": "Likely diesel locomotive path (unconfirmed fallback)",
        "pathing_power_source": "safe_freight_fallback",
        "planned_power": "diesel",
        "power_type": "diesel",
        "traction_type": "locomotive",
        "pathing_power_updated_at": now,
        # Intentionally no traction_class.
    }


def score_candidate(movement: Dict[str, Any], service: Dict[str, Any], locations: Optional[List[Dict[str, Any]]], target_date: str) -> Tuple[int, List[str]]:
    score = 0
    reasons: List[str] = []
    move_headcode = normalise_headcode(movement.get("train_id"))
    service_headcode = normalise_headcode(service.get("signalling_id"))
    if move_headcode and service_headcode and move_headcode == service_headcode:
        score += 60
        reasons.append("headcode")

    if active_on_date(service, target_date):
        score += 20
        reasons.append("date/days")
    else:
        return -999, ["not active on target date"]

    if compact(movement.get("origin")) and compact(service.get("origin_name")):
        if compact(movement.get("origin"))[:8] in compact(service.get("origin_name")) or compact(service.get("origin_name"))[:8] in compact(movement.get("origin")):
            score += 8
            reasons.append("origin")

    if compact(movement.get("destination")) and compact(service.get("destination_name")):
        if compact(movement.get("destination"))[:8] in compact(service.get("destination_name")) or compact(service.get("destination_name"))[:8] in compact(movement.get("destination")):
            score += 8
            reasons.append("destination")

    movement_time = safe_time_minutes(movement.get("planned_time") or movement.get("actual_time"))
    best_gap: Optional[int] = None
    if locations:
        for loc in locations:
            tiploc = compact(loc.get("tiploc") or loc.get("location") or loc.get("tiploc_code"))
            if tiploc and tiploc not in {"ACB", "ACBG", "ACTONBRIDGE"}:
                continue
            for key, value in loc.items():
                if any(word in key.lower() for word in ["pass", "arrival", "departure", "wtt", "gbtt", "planned"]):
                    gap = minute_gap(movement_time, safe_time_minutes(value))
                    if gap is not None:
                        best_gap = gap if best_gap is None else min(best_gap, gap)
        if best_gap is not None:
            if best_gap <= 3:
                score += 25
                reasons.append(f"ACB time ±{best_gap}m")
            elif best_gap <= 10:
                score += 12
                reasons.append(f"ACB time ±{best_gap}m")
            else:
                score -= 10
                reasons.append(f"ACB time gap {best_gap}m")

    # Prefer richer metadata.
    richness = sum(1 for f in ["power_type", "planned_power", "traction_type", "timing_load", "speed", "pathing_power_label"] if service.get(f) not in (None, ""))
    score += min(richness * 3, 18)
    if richness:
        reasons.append(f"metadata x{richness}")

    # STP priority: VSTP/O overlays are usually more specific than permanent schedules.
    stp = str(service.get("stp_indicator") or "").upper()
    if stp == "O":
        score += 8
        reasons.append("overlay")
    elif stp == "N":
        score += 12
        reasons.append("new/short-term")

    return score, reasons


def fetch_station_movements(db: SupabaseRest, cfg: Config) -> List[Dict[str, Any]]:
    rows = db.get("station_movements", {
        "select": STATION_SELECT,
        "running_date": f"eq.{cfg.target_date}",
        "limit": str(cfg.limit),
        "order": "planned_time.asc.nullslast,actual_time.asc.nullslast",
    })
    return [r for r in rows if is_freight_row(r) and is_missing_pathing(r)]


def fetch_services(db: SupabaseRest, table: str, headcode: str, target_date: str) -> List[Dict[str, Any]]:
    if not headcode:
        return []
    params = {
        "select": SERVICE_SELECT,
        "signalling_id": f"eq.{headcode}",
        "limit": "50",
        "order": "updated_at.desc.nullslast,created_at.desc.nullslast",
    }
    rows = db.get(table, params)
    return [r for r in rows if active_on_date(r, target_date)]


def fetch_locations(db: SupabaseRest, table: str, train_uid: str, target_date: str) -> List[Dict[str, Any]]:
    if not train_uid:
        return []
    try:
        return db.get(table, {
            "select": "*",
            "train_uid": f"eq.{train_uid}",
            "limit": "200",
        })
    except Exception as exc:
        print(f"LOCATION LOOKUP SKIPPED for {table}/{train_uid}: {exc}")
        return []


def best_match_for(db: SupabaseRest, movement: Dict[str, Any], cfg: Config) -> Tuple[Optional[str], Optional[Dict[str, Any]], int, List[str]]:
    headcode = normalise_headcode(movement.get("train_id"))
    best: Tuple[Optional[str], Optional[Dict[str, Any]], int, List[str]] = (None, None, -999, [])

    for table, source, loc_table in [
        ("schedule_services", "schedule_auto", "schedule_locations"),
        ("vstp_services", "vstp_auto", "vstp_locations"),
    ]:
        for service in fetch_services(db, table, headcode, cfg.target_date):
            locations = fetch_locations(db, loc_table, str(service.get("train_uid") or ""), cfg.target_date)
            score, reasons = score_candidate(movement, service, locations, cfg.target_date)
            if cfg.require_acb_location_match and not any(r.startswith("ACB time") for r in reasons):
                score -= 25
                reasons.append("no ACB location time match")
            # Prefer schedule first if scores are tied; VSTP must beat it to replace it.
            if score > best[2]:
                best = (source, service, score, reasons)
    return best


def merge_updates(existing: Dict[str, Any], new_meta: Dict[str, Any]) -> Dict[str, Any]:
    updates: Dict[str, Any] = {}
    for key, value in new_meta.items():
        if key not in METADATA_FIELDS or value in (None, ""):
            continue
        # Preserve exact confirmed traction_class if already present.
        if existing.get(key) not in (None, ""):
            continue
        updates[key] = value
    return updates


def main() -> int:
    cfg = load_config()
    db = SupabaseRest(cfg)
    print("PATHING AUTO BACKFILL V1 ACTIVE")
    print(f"Target date: {cfg.target_date}; station={cfg.station_code}; dry_run={cfg.dry_run}; safe_fallback={cfg.safe_fallback}")

    movements = fetch_station_movements(db, cfg)
    print(f"Freight rows with missing pathing fields: {len(movements)}")

    updated = 0
    skipped = 0
    fallbacked = 0

    for movement in movements:
        headcode = normalise_headcode(movement.get("train_id"))
        source, service, score, reasons = best_match_for(db, movement, cfg)
        meta: Dict[str, Any] = {}
        final_source = source
        final_reasons = reasons[:]

        if service and source and score >= 75:
            meta = metadata_from_service(service, source)
        elif cfg.safe_fallback:
            meta = fallback_metadata(movement)
            final_source = "safe_freight_fallback" if meta else source
            final_reasons.append(f"fallback used; best_score={score}")

        updates = merge_updates(movement, meta)
        if not updates:
            skipped += 1
            print(f"SKIP {movement.get('id')} {headcode}: no safe metadata update; best={source} score={score} reasons={'; '.join(reasons)}")
            continue

        print(f"UPDATE {movement.get('id')} {headcode}: source={final_source} score={score} reasons={'; '.join(final_reasons)} updates={json.dumps(updates, ensure_ascii=False)}")
        if not cfg.dry_run:
            db.patch_by_id("station_movements", movement["id"], updates)
            db.insert_audit({
                "station_movement_id": movement.get("id"),
                "running_date": movement.get("running_date"),
                "train_id": movement.get("train_id"),
                "matched_source": final_source,
                "match_score": score,
                "match_reasons": final_reasons,
                "applied_updates": updates,
                "matched_service_id": service.get("id") if service else None,
                "matched_train_uid": service.get("train_uid") if service else None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        updated += 1
        if final_source == "safe_freight_fallback":
            fallbacked += 1

    print(f"Done. updated={updated}; fallback={fallbacked}; skipped={skipped}; dry_run={cfg.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
