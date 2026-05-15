/*
  Acton Bridge Pathing Power Injector v2

  Purpose:
  - Fill station_movements pathing/traction columns from schedule/VSTP data when available.
  - Never invent traction. If no safe schedule/VSTP fields exist, leave unknown.

  Required secrets:
  - SUPABASE_URL
  - SUPABASE_SERVICE_ROLE_KEY

  Optional env:
  - TARGET_DATE=YYYY-MM-DD
*/

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;
const TARGET_DATE = process.env.TARGET_DATE || new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit'
}).format(new Date());

if (!SUPABASE_URL || !SUPABASE_KEY) {
  throw new Error('Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY');
}

const BASE = SUPABASE_URL.replace(/\/$/, '') + '/rest/v1';
const HEADERS = {
  apikey: SUPABASE_KEY,
  Authorization: `Bearer ${SUPABASE_KEY}`,
  'Content-Type': 'application/json',
  Prefer: 'return=representation'
};

function clean(v) { return String(v ?? '').trim(); }
function upper(v) { return clean(v).toUpperCase(); }
function firstValue(obj, keys) {
  for (const k of keys) {
    if (obj && obj[k] !== undefined && obj[k] !== null && clean(obj[k])) return clean(obj[k]);
  }
  return '';
}

function decodePathingPower(row) {
  const powerType = firstValue(row, ['power_type','power','pwr','traction_power']);
  const plannedPower = firstValue(row, ['planned_power','planned_traction','planned_power_type']);
  const tractionType = firstValue(row, ['traction_type','traction','train_power_type']);
  const tractionClass = firstValue(row, ['traction_class','planned_class','class','timing_class']);
  const timingLoad = firstValue(row, ['timing_load','timing_load_text','load','timing_load_desc']);
  const operatingCharacteristics = firstValue(row, ['operating_characteristics','operating_characteristic','op_chars','characteristics']);
  const stockType = firstValue(row, ['stock_type','rolling_stock','stock']);

  const joined = upper([powerType, plannedPower, tractionType, tractionClass, timingLoad, operatingCharacteristics, stockType].join(' '));

  let pathing_power = 'unknown';
  let label = 'Pathing unknown';

  if (/ELECTRIC|ELEC|\bAC\b|\bOHLE\b|\bOHL\b/.test(joined) && /DIESEL|DSL/.test(joined)) {
    pathing_power = 'diesel_electric';
    label = 'Pathed diesel/electric';
  } else if (/ELECTRIC|ELEC|\bAC\b|\bOHLE\b|\bOHL\b/.test(joined)) {
    pathing_power = 'electric';
    label = 'Pathed as electric loco';
  } else if (/DIESEL|DSL/.test(joined)) {
    pathing_power = 'diesel';
    label = 'Pathed as diesel loco';
  }

  return {
    pathing_power,
    pathing_power_label: label,
    power_type: powerType,
    planned_power: plannedPower,
    traction_type: tractionType,
    traction_class: tractionClass,
    timing_load: timingLoad,
    operating_characteristics: operatingCharacteristics,
    stock_type: stockType
  };
}

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, { headers: HEADERS, ...options });
  const text = await res.text();
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${text}`);
  return text ? JSON.parse(text) : null;
}

async function getMovements() {
  const select = [
    'id','date','time','planned_time','actual_time','train_id','headcode','type','origin','destination','platform',
    'pathing_power','pathing_power_label'
  ].join(',');
  return await request(`/station_movements?date=eq.${encodeURIComponent(TARGET_DATE)}&select=${select}&order=time.asc&limit=1000`);
}

async function safeLookup(table, trainId) {
  const id = encodeURIComponent(trainId);
  const select = '*';
  const candidates = [
    `/` + table + `?train_id=eq.${id}&select=${select}&limit=5`,
    `/` + table + `?headcode=eq.${id}&select=${select}&limit=5`,
    `/` + table + `?identity=eq.${id}&select=${select}&limit=5`
  ];
  for (const path of candidates) {
    try {
      const rows = await request(path);
      if (Array.isArray(rows) && rows.length) return rows[0];
    } catch (err) {
      // Optional schema mismatch. Keep trying other options/tables.
    }
  }
  return null;
}

async function findPathingSource(trainId) {
  if (!trainId) return null;
  return await safeLookup('schedule_services', trainId)
      || await safeLookup('vstp_services', trainId)
      || null;
}

async function patchMovement(id, patch) {
  return await request(`/station_movements?id=eq.${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { ...HEADERS, Prefer: 'return=minimal' },
    body: JSON.stringify(patch)
  });
}

async function main() {
  console.log(`PATHING POWER INJECTOR V2 ACTIVE for ${TARGET_DATE}`);
  const movements = await getMovements();
  console.log(`Loaded ${movements.length} movement rows`);
  let updated = 0, unknown = 0, skipped = 0;

  for (const m of movements) {
    const trainId = clean(m.train_id || m.headcode);
    const isLikelyFreightOrSpecial = /^(4|6|7|3Z|1Q|3Q|0)/i.test(trainId) || ['freight','network_rail','light_engine'].includes(clean(m.type));
    if (!isLikelyFreightOrSpecial) { skipped++; continue; }

    const source = await findPathingSource(trainId);
    if (!source) { unknown++; continue; }

    const decoded = decodePathingPower(source);
    if (decoded.pathing_power === 'unknown') { unknown++; continue; }

    await patchMovement(m.id, {
      ...decoded,
      pathing_power_source: source.stp_indicator ? 'schedule_services' : 'schedule_or_vstp',
      pathing_power_updated_at: new Date().toISOString()
    });
    updated++;
  }

  console.log(JSON.stringify({ date: TARGET_DATE, updated, unknown, skipped }, null, 2));
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
