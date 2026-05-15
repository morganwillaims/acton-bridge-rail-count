/* Acton Bridge Pathing Power Injector v6
   Fixes v5 failure: does NOT select station_movements.type/date/loco.
   Uses minimal station_movements columns only and updates pathing fields by id.
*/

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_KEY;
const STATION = process.env.STATION_CRS || "ACB";
const RUN_DATE = process.env.RUN_DATE || new Date().toISOString().slice(0, 10);

if (!SUPABASE_URL || !SUPABASE_KEY) {
  console.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY/SUPABASE_KEY");
  process.exit(1);
}

const headers = {
  apikey: SUPABASE_KEY,
  Authorization: `Bearer ${SUPABASE_KEY}`,
  "Content-Type": "application/json",
  Prefer: "return=representation"
};

async function request(path, options = {}) {
  const url = `${SUPABASE_URL.replace(/\/$/, "")}/rest/v1/${path}`;
  const res = await fetch(url, { headers, ...options });
  const txt = await res.text();
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${txt}`);
  if (!txt) return null;
  try { return JSON.parse(txt); } catch { return txt; }
}

function q(v) { return encodeURIComponent(String(v)); }
function clean(v) { return v == null ? "" : String(v).trim(); }
function first(obj, keys) {
  for (const k of keys) {
    if (obj && obj[k] != null && String(obj[k]).trim() !== "") return obj[k];
  }
  return "";
}

function decodePathing(raw) {
  const s = clean(raw).toUpperCase();
  if (!s) return { power: "unknown", label: "Pathing unknown" };
  if (["E", "ELEC", "ELECTRIC", "AC", "25KV"].includes(s) || /ELECTRIC/.test(s)) {
    return { power: "electric", label: "Pathed as electric loco" };
  }
  if (["D", "DSL", "DIESEL"].includes(s) || /DIESEL/.test(s)) {
    return { power: "diesel", label: "Pathed as diesel loco" };
  }
  if (["DE", "ED", "BI", "BIMODE", "BI-MODE", "DUAL"].includes(s) || /DIESEL.*ELECTRIC|ELECTRIC.*DIESEL|BI.?MODE/.test(s)) {
    return { power: "diesel_electric", label: "Pathed diesel/electric" };
  }
  return { power: "unknown", label: "Pathing unknown" };
}

function extractPathing(row) {
  const powerType = first(row, ["power_type", "power", "traction", "traction_type", "planned_power", "pathing_power", "train_power_type"]);
  const plannedPower = first(row, ["planned_power", "planned_traction", "power_type", "traction_type"]);
  const tractionType = first(row, ["traction_type", "traction", "power_type"]);
  const tractionClass = first(row, ["traction_class", "class", "timing_class"]);
  const timingLoad = first(row, ["timing_load", "load", "trailing_load", "timing_load_desc"]);
  const operatingCharacteristics = first(row, ["operating_characteristics", "op_chars", "operating_characteristic"]);
  const stockType = first(row, ["stock_type", "stock", "train_category"]);
  const speed = first(row, ["speed", "planned_speed", "max_speed"]);

  const decoded = decodePathing(powerType || plannedPower || tractionType || timingLoad || stockType);
  return {
    pathing_power: decoded.power,
    pathing_power_label: decoded.label,
    pathing_power_source: "schedule_vstp_injector_v6",
    power_type: clean(powerType),
    planned_power: clean(plannedPower),
    traction_type: clean(tractionType),
    traction_class: clean(tractionClass),
    timing_load: clean(timingLoad),
    operating_characteristics: clean(operatingCharacteristics),
    stock_type: clean(stockType),
    speed: clean(speed),
    pathing_power_updated_at: new Date().toISOString()
  };
}

function hasUsefulPathing(p) {
  return p.pathing_power !== "unknown" || p.power_type || p.planned_power || p.traction_type || p.traction_class || p.timing_load || p.operating_characteristics || p.stock_type || p.speed;
}

async function getMovements() {
  // Minimal safe select: no date, no type, no loco.
  const select = "id,running_date,station_crs,train_id,planned_time,actual_time,origin,destination,toc";
  let path = `station_movements?select=${select}&station_crs=eq.${q(STATION)}&running_date=eq.${q(RUN_DATE)}&order=actual_time.asc.nullslast,planned_time.asc.nullslast&limit=1000`;
  return await request(path);
}

async function findScheduleRows(trainId) {
  const tables = ["schedule_services", "vstp_services"];
  const results = [];
  for (const table of tables) {
    // Use broad select * because optional pathing columns may or may not exist after SQL migration.
    // Filter by common headcode-ish fields. If one filter fails, try another.
    const tries = [
      `train_id=eq.${q(trainId)}`,
      `headcode=eq.${q(trainId)}`,
      `identity=eq.${q(trainId)}`,
      `train_identity=eq.${q(trainId)}`
    ];
    for (const filter of tries) {
      try {
        const rows = await request(`${table}?select=*&${filter}&limit=10`);
        if (Array.isArray(rows) && rows.length) results.push(...rows.map(r => ({ ...r, _source_table: table })));
      } catch (e) {
        // Ignore missing columns/tables/filters; this script is defensive.
      }
    }
  }
  return results;
}

async function updateMovement(id, payload) {
  return await request(`station_movements?id=eq.${q(id)}`, {
    method: "PATCH",
    body: JSON.stringify(payload)
  });
}

async function main() {
  console.log(`PATHING POWER INJECTOR V6 NO-DATE-NO-TYPE ACTIVE for ${RUN_DATE}`);
  const movements = await getMovements();
  console.log(`Loaded movements: ${movements.length}`);
  let updated = 0, unknown = 0, noSource = 0;

  for (const m of movements) {
    const trainId = clean(m.train_id);
    if (!trainId) continue;
    const sourceRows = await findScheduleRows(trainId);
    if (!sourceRows.length) { noSource++; continue; }

    let chosen = null;
    let payload = null;
    for (const r of sourceRows) {
      const p = extractPathing(r);
      if (hasUsefulPathing(p)) { chosen = r; payload = p; break; }
    }
    if (!payload) { unknown++; continue; }
    await updateMovement(m.id, payload);
    updated++;
    console.log(`updated ${trainId} id=${m.id} from ${chosen._source_table}: ${payload.pathing_power_label}`);
  }

  console.log(`PATHING POWER INJECTOR V6 done: updated=${updated} noSource=${noSource} sourceButUnknown=${unknown}`);
}

main().catch(err => {
  console.error("Error:", err.message || err);
  process.exit(1);
});
