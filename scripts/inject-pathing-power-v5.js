// Acton Bridge Pathing Power Injector v5
// Reads captured pathing fields from schedule_services/vstp_services where available and injects onto station_movements.
// Conservative: leaves unknown where source data is missing.
const { extractPathingFields } = require("./pathing-field-normaliser.js");

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;
if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) throw new Error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY");
const DATE = process.env.RUN_DATE || new Date().toISOString().slice(0, 10);
const STATION = process.env.STATION_CRS || "ACB";

const headers = { apikey: SUPABASE_SERVICE_ROLE_KEY, Authorization: `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`, "Content-Type": "application/json", Prefer: "return=minimal" };

async function request(path, opts = {}) {
  const res = await fetch(`${SUPABASE_URL}/rest/v1/${path}`, { headers, ...opts });
  const text = await res.text();
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${text}`);
  return text ? JSON.parse(text) : null;
}

function q(v) { return encodeURIComponent(v); }
function trainOf(row) { return String(row.train_id || row.headcode || row.identity || "").trim().toUpperCase(); }
function useful(p) { return p && p.pathing_power && p.pathing_power !== "unknown"; }

async function getMovements() {
  // Known working columns from your project: running_date, station_crs, train_id, planned_time, actual_time.
  const select = "id,running_date,station_crs,train_id,planned_time,actual_time,type,origin,destination,pathing_power";
  return await request(`station_movements?select=${select}&station_crs=eq.${q(STATION)}&running_date=eq.${q(DATE)}&order=actual_time.asc.nullslast&limit=500`);
}

async function getCandidates(table, headcode) {
  // Try broad text search by common fields. Do not assume date columns exist.
  const fields = "train_uid,headcode,identity,origin,destination,power_type,planned_power,traction_type,traction_class,timing_load,operating_characteristics,stock_type,speed,pathing_power,pathing_power_label,pathing_power_source";
  const filters = [
    `${table}?select=${fields}&headcode=eq.${q(headcode)}&limit=10`,
    `${table}?select=${fields}&identity=eq.${q(headcode)}&limit=10`,
    `${table}?select=${fields}&train_uid=eq.${q(headcode)}&limit=10`
  ];
  for (const path of filters) {
    try {
      const rows = await request(path);
      if (Array.isArray(rows) && rows.length) return rows;
    } catch (e) {
      // Some columns might not exist in old table versions. Try next route.
    }
  }
  return [];
}

async function patchMovement(id, fields) {
  return await request(`station_movements?id=eq.${q(id)}`, { method: "PATCH", body: JSON.stringify({ ...fields, pathing_power_updated_at: new Date().toISOString() }) });
}

async function main() {
  console.log(`PATHING POWER INJECTOR V5 SOURCE-FIELD CAPTURE ACTIVE for ${DATE}`);
  const moves = await getMovements();
  let checked = 0, updated = 0, unknown = 0, noSource = 0;
  for (const m of moves || []) {
    const headcode = trainOf(m);
    if (!headcode || !m.id) continue;
    checked++;
    let sourceRows = [];
    sourceRows = sourceRows.concat(await getCandidates("schedule_services", headcode));
    sourceRows = sourceRows.concat(await getCandidates("vstp_services", headcode));
    if (!sourceRows.length) { noSource++; continue; }
    let best = null;
    for (const s of sourceRows) {
      const p = extractPathingFields(s);
      if (useful(p)) { best = p; break; }
      if (!best) best = p;
    }
    if (!best || !useful(best)) { unknown++; continue; }
    await patchMovement(m.id, best);
    updated++;
    console.log(`updated ${headcode}: ${best.pathing_power_label}`);
  }
  console.log(`done checked=${checked} updated=${updated} noSource=${noSource} unknown=${unknown}`);
}

main().catch(err => { console.error("Error:", err.message || err); process.exit(1); });
