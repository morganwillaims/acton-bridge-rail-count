#!/usr/bin/env python3
"""
Acton Bridge Rail Count - Pathing Auto Backfill v1.3

Purpose:
  Fill freight pathing metadata automatically and cautiously.

Order of trust:
  1) schedule_services confirmed fields -> source schedule_auto
  2) vstp_services confirmed fields -> source vstp_auto
  3) cautious visible-freight fallback only -> source safe_freight_fallback

Important:
  - Does NOT guess traction_class.
  - Does NOT use schedule_locations/vstp_locations train_uid columns.
  - Does NOT touch schedule_loader.py.
  - Does NOT apply fallback to completely blank ghost rows.
  - Applies metadata to the visible station_movement row, not just a blank sibling.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "PATHING AUTO BACKFILL V1.3 ACTIVE"

STATION_MOVEMENT_COLUMNS = [
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

SCHEDULE_SERVICE_COLUMNS = [
    "id",
    "created_at",
    "updated_at",
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
    "passes_acb",
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
]

VSTP_SERVICE_COLUMNS = [
    "id",
    "created_at",
    "updated_at",
    "train_uid",
    "stp_indicator",
    "schedule_start_date",
    "schedule_end_date",
    "days_runs",
    "signalling_id",
    "atoc_code",
    "transaction_type",
    "origin_tiploc",
    "origin_name",
    "destination_tiploc",
    "destination_name",
    "passes_acb",
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
]

UPDATE_FIELDS = [
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


def env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def clean(value: Any) -> str:
    return str(value or "").strip()


def norm_headcode(value: Any) -> str:
    return re.sub(r"\s+", "", clean(value).upper())


def norm_text(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", clean(value).upper())


def is_missing(value: Any) -> bool:
    s = clean(value)
    return not s or s.lower() in {"unknown", "null", "none", "route pending", "-", "—"}


def row_has_any_pathing(row: Dict[str, Any]) -> bool:
    keys = [
        "pathing_power_label",
        "pathing_power",
        "power_type",
        "planned_power",
        "traction_type",
        "traction_class",
        "timing_load",
        "operating_characteristics",
        "stock_type",
        "speed",
    ]
    return any(not is_missing(row.get(k)) for k in keys)


def row_needs_update(row: Dict[str, Any]) -> bool:
    return not row_has_any_pathing(row)


def is_visible_real_movement(row: Dict[str, Any]) -> bool:
    """
    Avoid blank sibling/ghost rows:
      train_id only + everything else null = not safe.
    But allow visible Unknown->Unknown rows if they have time/platform/status/source.
    """
    if is_missing(row.get("train_id")):
        return False

    context_keys = ["planned_time", "actual_time", "origin", "destination", "platform", "status", "source"]
    if not any(not is_missing(row.get(k)) for k in context_keys):
        return False

    return True


def active_on_date(service: Dict[str, Any], target_date: str) -> bool:
    start = clean(service.get("schedule_start_date"))[:10]
    end = clean(service.get("schedule_end_date"))[:10]
    if start and target_date < start:
        return False
    if end and target_date > end:
        return False

    days = clean(service.get("days_runs"))
    if len(days) >= 7:
        d = dt.date.fromisoformat(target_date)
        idx = d.weekday()  # Mon=0
        c = days[idx] if idx < len(days) else "1"
        if c in {"0", " ", "N", "n"}:
            return False
    return True


def power_code_from_values(*values: Any) -> Optional[str]:
    joined = " ".join(clean(v) for v in values if not is_missing(v)).upper()
    joined = joined.replace("_", " ").replace("-", " ")
    if not joined:
        return None

    if re.search(r"ELECTRO\s*DIESEL|DIESEL\s*ELECTRIC|BI\s*MODE|DUAL", joined):
        return "dual"
    if re.search(r"\bEMU\b|ELECTRIC|OHLE|PANTOGRAPH|AC\s*LOCO", joined):
        return "electric"
    if re.search(r"\bDMU\b|DIESEL|DIESEL\s*LOCO|LOCO\s*DIESEL", joined):
        return "diesel"

    # common compact pathing values
    compact = joined.strip()
    if compact in {"D", "DSL"}:
        return "diesel"
    if compact in {"E", "ELEC"}:
        return "electric"
    if compact in {"ED", "DE", "BI", "B"}:
        return "dual"
    return None


def label_for(power: Optional[str], traction_type: Any = "") -> Optional[str]:
    tr = clean(traction_type).lower()
    is_loco = "loco" in tr or "locomotive" in tr

    if power == "diesel":
        return "Pathed as diesel locomotive" if is_loco else "Pathed as diesel"
    if power == "electric":
        return "Pathed as electric locomotive" if is_loco else "Pathed as electric"
    if power == "dual":
        return "Pathed diesel/electric locomotive" if is_loco else "Pathed diesel/electric"
    return None


def confirmed_update_from_service(service: Dict[str, Any], source: str) -> Tuple[Dict[str, Any], List[str]]:
    """
    Extract confirmed metadata. We only set fields that exist on the matched service.
    traction_class is copied only when present; we never invent it.
    """
    updates: Dict[str, Any] = {}
    reasons: List[str] = []

    for key in [
        "planned_power",
        "power_type",
        "traction_type",
        "traction_class",
        "timing_load",
        "operating_characteristics",
        "stock_type",
        "speed",
    ]:
        if not is_missing(service.get(key)):
            updates[key] = service.get(key)
            reasons.append(f"{key}_from_{source}")

    if not is_missing(service.get("pathing_power")):
        updates["pathing_power"] = service.get("pathing_power")
        reasons.append(f"pathing_power_from_{source}")

    if not is_missing(service.get("pathing_power_label")):
        updates["pathing_power_label"] = service.get("pathing_power_label")
        reasons.append(f"pathing_power_label_from_{source}")

    # If the service has power/traction fields but not the normalised pathing label, derive a safe label.
    power = updates.get("pathing_power") or power_code_from_values(
        service.get("pathing_power"),
        service.get("pathing_power_label"),
        service.get("planned_power"),
        service.get("power_type"),
        service.get("traction_type"),
        service.get("stock_type"),
        service.get("timing_load"),
    )
    if power and is_missing(updates.get("pathing_power")):
        updates["pathing_power"] = power
        reasons.append(f"pathing_power_derived_from_{source}")

    if power and is_missing(updates.get("pathing_power_label")):
        label = label_for(power, updates.get("traction_type") or service.get("traction_type"))
        if label:
            updates["pathing_power_label"] = label
            reasons.append(f"pathing_power_label_derived_from_{source}")

    if updates:
        updates["pathing_power_source"] = source
        updates["pathing_power_updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()

    return updates, reasons


def safe_fallback_update() -> Tuple[Dict[str, Any], List[str]]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    return {
        "pathing_power": "likely_diesel_locomotive",
        "pathing_power_label": "Likely diesel locomotive path (unconfirmed fallback)",
        "pathing_power_source": "safe_freight_fallback",
        "planned_power": "diesel",
        "power_type": "diesel",
        "traction_type": "locomotive",
        "pathing_power_updated_at": now,
    }, ["safe_freight_fallback_visible_freight_no_confirmed_pathing"]


def merge_updates_for_row(existing: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """
    Do not overwrite better existing values. Fill blanks only, except pathing_power_updated_at
    when at least one real field is filled.
    """
    out: Dict[str, Any] = {}
    for key, value in updates.items():
        if key == "pathing_power_updated_at":
            continue
        if key not in UPDATE_FIELDS:
            continue
        if is_missing(value):
            continue
        if is_missing(existing.get(key)):
            out[key] = value

    if out:
        out["pathing_power_updated_at"] = updates.get("pathing_power_updated_at") or dt.datetime.now(dt.timezone.utc).isoformat()
    return out


class Supabase:
    def __init__(self, url: str, key: str) -> None:
        self.url = url.rstrip("/")
        self.key = key

    def request(self, method: str, path: str, params: Optional[Dict[str, str]] = None, payload: Any = None, prefer: str = "return=minimal") -> Any:
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params, doseq=True, safe=",.*():")
        url = f"{self.url}/rest/v1/{path}{query}"

        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "apikey": self.key,
                "authorization": f"Bearer {self.key}",
                "accept": "application/json",
                "content-type": "application/json",
                "prefer": prefer,
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                if not body:
                    return None
                return json.loads(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase {method} {path} failed: HTTP {e.code}: {body}\nURL: {url}") from e

    def get(self, path: str, params: Dict[str, str]) -> Any:
        return self.request("GET", path, params=params, prefer="")

    def patch_by_id(self, table: str, row_id: str, payload: Dict[str, Any]) -> None:
        self.request("PATCH", table, params={"id": f"eq.{row_id}"}, payload=payload)

    def post(self, path: str, payload: Any) -> None:
        self.request("POST", path, payload=payload, prefer="return=minimal")


@dataclass
class MatchResult:
    source: str
    service: Optional[Dict[str, Any]]
    score: int
    reasons: List[str]
    updates: Dict[str, Any]


def service_match_score(row: Dict[str, Any], service: Dict[str, Any], source: str, target_date: str) -> Tuple[int, List[str]]:
    score = 0
    reasons: List[str] = []

    if norm_headcode(row.get("train_id")) and norm_headcode(row.get("train_id")) == norm_headcode(service.get("signalling_id")):
        score += 70
        reasons.append("headcode")

    if active_on_date(service, target_date):
        score += 10
        reasons.append("date/days")
    else:
        return -999, ["inactive_date"]

    origin_row = norm_text(row.get("origin"))
    dest_row = norm_text(row.get("destination"))

    origin_candidates = [norm_text(service.get("origin_tiploc")), norm_text(service.get("origin_name"))]
    dest_candidates = [norm_text(service.get("destination_tiploc")), norm_text(service.get("destination_name"))]

    if origin_row and origin_row not in {"UNKNOWN"}:
        if origin_row in origin_candidates or any(origin_row and c and (origin_row in c or c in origin_row) for c in origin_candidates):
            score += 10
            reasons.append("origin")

    if dest_row and dest_row not in {"UNKNOWN"}:
        if dest_row in dest_candidates or any(dest_row and c and (dest_row in c or c in dest_row) for c in dest_candidates):
            score += 10
            reasons.append("destination")

    if source == "vstp_auto":
        score += 2
        reasons.append("vstp/new-short-term")

    if service.get("passes_acb") is True:
        score += 5
        reasons.append("passes_acb")

    if any(not is_missing(service.get(k)) for k in ["pathing_power", "pathing_power_label", "power_type", "planned_power", "traction_type", "timing_load", "speed"]):
        score += 5
        reasons.append("has_pathing_metadata")

    return score, reasons


def fetch_station_rows(db: Supabase, target_date: str) -> List[Dict[str, Any]]:
    rows = db.get(
        "station_movements",
        {
            "select": ",".join(STATION_MOVEMENT_COLUMNS),
            "running_date": f"eq.{target_date}",
            "train_type": "eq.freight",
            "limit": "5000",
            "order": "planned_time.asc.nullslast,actual_time.asc.nullslast,train_id.asc",
        },
    )
    return rows or []


def fetch_services_for_headcode(db: Supabase, table: str, headcode: str) -> List[Dict[str, Any]]:
    columns = SCHEDULE_SERVICE_COLUMNS if table == "schedule_services" else VSTP_SERVICE_COLUMNS
    rows = db.get(
        table,
        {
            "select": ",".join(columns),
            "signalling_id": f"eq.{headcode}",
            "limit": "500",
            "order": "updated_at.desc.nullslast,created_at.desc.nullslast",
        },
    )
    return rows or []


def best_confirmed_match_for_row(db: Supabase, row: Dict[str, Any], target_date: str, service_cache: Dict[Tuple[str, str], List[Dict[str, Any]]]) -> MatchResult:
    headcode = norm_headcode(row.get("train_id"))
    best = MatchResult(source="", service=None, score=-999, reasons=[], updates={})

    for table, source in [("schedule_services", "schedule_auto"), ("vstp_services", "vstp_auto")]:
        cache_key = (table, headcode)
        if cache_key not in service_cache:
            try:
                service_cache[cache_key] = fetch_services_for_headcode(db, table, headcode)
            except Exception as exc:
                print(f"WARN fetch {table} {headcode} failed: {exc}", flush=True)
                service_cache[cache_key] = []

        for service in service_cache[cache_key]:
            score, reasons = service_match_score(row, service, source, target_date)
            if score < 0:
                continue

            updates, update_reasons = confirmed_update_from_service(service, source)
            if not updates:
                # Keep a score for diagnostics, but don't treat as confirmed pathing.
                if score > best.score:
                    best = MatchResult(source=source, service=service, score=score, reasons=reasons + ["matched_service_no_pathing_metadata"], updates={})
                continue

            score_with_metadata = score + 20
            if score_with_metadata > best.score:
                best = MatchResult(
                    source=source,
                    service=service,
                    score=score_with_metadata,
                    reasons=reasons + update_reasons,
                    updates=updates,
                )

    return best


def insert_audit(db: Supabase, row: Dict[str, Any], match: MatchResult, updates: Dict[str, Any], dry_run: bool) -> None:
    payload = {
        "station_movement_id": clean(row.get("id")),
        "running_date": clean(row.get("running_date"))[:10],
        "train_id": norm_headcode(row.get("train_id")),
        "matched_source": match.source or updates.get("pathing_power_source") or "",
        "match_score": match.score if match.score != -999 else None,
        "match_reasons": match.reasons,
        "applied_updates": updates,
        "matched_service_id": clean(match.service.get("id")) if match.service else None,
        "matched_train_uid": clean(match.service.get("train_uid")) if match.service else None,
        "dry_run": dry_run,
    }
    try:
        db.post("pathing_auto_backfill_audit", payload)
    except Exception as exc:
        print(f"AUDIT INSERT SKIPPED: {exc}", flush=True)


def main() -> int:
    print(VERSION, flush=True)

    supabase_url = clean(os.getenv("SUPABASE_URL"))
    supabase_key = clean(os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY"))
    if not supabase_url or not supabase_key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.", file=sys.stderr)
        return 2

    target_date = clean(os.getenv("TARGET_DATE") or os.getenv("SNAPSHOT_DATE"))
    if not target_date:
        target_date = dt.datetime.now(dt.timezone.utc).date().isoformat()

    dry_run = env_bool("DRY_RUN", True)
    enable_fallback = env_bool("ENABLE_SAFE_FREIGHT_FALLBACK", True)

    print(f"Target date: {target_date}; station=ACB; dry_run={dry_run}; safe_fallback={enable_fallback}", flush=True)

    db = Supabase(supabase_url, supabase_key)

    rows = fetch_station_rows(db, target_date)
    freight_rows = [r for r in rows if norm_headcode(r.get("train_id"))]
    missing_rows = [r for r in freight_rows if row_needs_update(r)]

    print(f"Freight rows fetched: {len(rows)}; with headcode: {len(freight_rows)}; missing pathing-ish fields: {len(missing_rows)}", flush=True)

    service_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    updated = 0
    confirmed = 0
    fallback = 0
    skipped = 0

    for row in missing_rows:
        row_id = clean(row.get("id"))
        headcode = norm_headcode(row.get("train_id"))

        if not is_visible_real_movement(row):
            skipped += 1
            print(f"SKIP {row_id} {headcode}: blank/ghost row; no visible context", flush=True)
            continue

        match = best_confirmed_match_for_row(db, row, target_date, service_cache)
        candidate_updates = dict(match.updates)

        if not candidate_updates and enable_fallback:
            candidate_updates, fb_reasons = safe_fallback_update()
            match = MatchResult(
                source="safe_freight_fallback",
                service=match.service,
                score=max(match.score, 0),
                reasons=(match.reasons or []) + fb_reasons,
                updates=candidate_updates,
            )

        updates = merge_updates_for_row(row, candidate_updates)

        if not updates:
            skipped += 1
            print(f"SKIP {row_id} {headcode}: no safe metadata update; best={match.source or 'None'} score={match.score} reasons={'; '.join(match.reasons)}", flush=True)
            continue

        source = updates.get("pathing_power_source") or match.source
        print(
            f"{'DRY ' if dry_run else ''}UPDATE {row_id} {headcode}: "
            f"source={source} score={match.score} reasons={'; '.join(match.reasons)} "
            f"updates={json.dumps(updates, sort_keys=True)}",
            flush=True,
        )

        if not dry_run:
            try:
                db.patch_by_id("station_movements", row_id, updates)
                insert_audit(db, row, match, updates, dry_run=False)
            except Exception as exc:
                print(f"ERROR updating {row_id} {headcode}: {exc}", flush=True)
                skipped += 1
                continue

        updated += 1
        if source in {"schedule_auto", "vstp_auto"}:
            confirmed += 1
        elif source == "safe_freight_fallback":
            fallback += 1

    print(f"Done. updated={updated}; confirmed={confirmed}; fallback={fallback}; skipped={skipped}; dry_run={dry_run}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
