// Acton Bridge Pathing Power Injector v7 - Metadata Override Layer
// Reads trusted rows from public.service_pathing_metadata and copies them into station_movements.
// Designed to avoid station_movements.date/type/loco and avoid information_schema.

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_KEY;
const RUN_DATES = (process.env.RUN_DATES || '').split(',').map(s => s.trim()).filter(Boolean);
const DAYS_BACK = Number(process.env.DAYS_BACK || 2);
const LIMIT = Number(process.env.LIMIT || 500);
const DRY_RUN = String(process.env.DRY_RUN || '').toLowerCase() === 'true';

if (!SUPABASE_URL || !SUPABASE_KEY) {
  console.error('Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY/SUPABASE_KEY');
  process.exit(1);
}

const headers = {
  apikey: SUPABASE_KEY,
  authorization: `Bearer ${SUPABASE_KEY}`,
  'content-type': 'application/json',
  prefer: 'return=representation'
};

function ukDate(offsetDays = 0) {
  const now = new Date();
  // UK local-ish date without pulling in timezone libraries. Good enough for workflow date windows.
  const d = new Date(now.getTime() + offsetDays * 86400000);
  return d.toISOString().slice(0, 10);
}

function datesToProcess() {
  if (RUN_DATES.length) return RUN_DATES;
  const out = [];
  for (let i = 0; i < DAYS_BACK; i++) out.push(ukDate(-i));
  return out;
}

function clean(v) {
  if (v === undefined || v === null) return null;
  const s = String(v).trim();
  if (!s || s.toLowerCase() === 'null' || s === '—' || s === '-') return null;
  return s;
}

function normalisePower(v) {
  const s = clean(v);
  if (!s) return null;
  const lower = s.toLowerCase();
  if (lower.includes('electric') || ['e', 'elec'].includes(lower)) return 'electric';
  if (lower.includes('diesel') || ['d', 'dies'].includes(lower)) return 'diesel';
  if (lower.includes('bi') || lower.includes('dual')) return 'diesel/electric';
  return lower;
}

function labelFrom(meta) {
  const power = normalisePower(meta.pathing_power || meta.planned_power || meta.power_type);
  if (clean(meta.pathing_power_label)) return clean(meta.pathing_power_label);
  if (power === 'electric') return 'Pathed as electric loco';
  if (power === 'diesel') return 'Pathed as diesel loco';
  if (power === 'diesel/electric') return 'Pathed as diesel/electric loco';
  if (clean(meta.traction_class)) return `Traction class ${clean(meta.traction_class)}`;
  if (clean(meta.stock_type)) return `Stock/UIC ${clean(meta.stock_type)}`;
  return null;
}

async function supa(path, opts = {}) {
  const url = `${SUPABASE_URL.replace(/\/$/, '')}/rest/v1/${path}`;
  const res = await fetch(url, { ...opts, headers: { ...headers, ...(opts.headers || {}) } });
  const text = await res.text();
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${text.slice(0, 1200)}`);
  if (!text) return null;
  try { return JSON.parse(text); } catch { return text; }
}

function eq(column, value) {
  return `${encodeURIComponent(column)}=eq.${encodeURIComponent(value)}`;
}

async function getMetadata(date) {
  const select = [
    'id','running_date','train_id','origin_tiploc','destination_tiploc','planned_time','actual_time',
    'pathing_power','pathing_power_label','pathing_power_source','power_type','planned_power',
    'traction_type','traction_class','timing_load','operating_characteristics','stock_type','speed','source','notes'
  ].join(',');
  const path = `service_pathing_metadata?select=${select}&${eq('running_date', date)}&limit=${LIMIT}`;
  return await supa(path, { method: 'GET' }) || [];
}

async function findMovements(meta) {
  const select = 'id,running_date,train_id,planned_time,actual_time,origin,destination,pathing_power,pathing_power_label';
  const parts = [
    `select=${select}`,
    eq('running_date', meta.running_date),
    eq('train_id', meta.train_id),
    `limit=50`
  ];
  // Keep matching broad at query level; filter optional fields locally to avoid schema/operator surprises.
  const rows = await supa(`station_movements?${parts.join('&')}`, { method: 'GET' }) || [];
  return rows.filter(row => {
    const originOk = !clean(meta.origin_tiploc) || clean(row.origin) === clean(meta.origin_tiploc);
    const destOk = !clean(meta.destination_tiploc) || clean(row.destination) === clean(meta.destination_tiploc);
    const plannedOk = !clean(meta.planned_time) || clean(row.planned_time) === clean(meta.planned_time);
    const actualOk = !clean(meta.actual_time) || clean(row.actual_time) === clean(meta.actual_time);
    return originOk && destOk && plannedOk && actualOk;
  });
}

function updatePayload(meta) {
  const pathingPower = normalisePower(meta.pathing_power || meta.planned_power || meta.power_type);
  return {
    pathing_power: pathingPower,
    pathing_power_label: labelFrom(meta),
    pathing_power_source: clean(meta.pathing_power_source) || clean(meta.source) || 'service_pathing_metadata',
    power_type: clean(meta.power_type),
    planned_power: clean(meta.planned_power) || pathingPower,
    traction_type: clean(meta.traction_type),
    traction_class: clean(meta.traction_class),
    timing_load: clean(meta.timing_load),
    operating_characteristics: clean(meta.operating_characteristics),
    stock_type: clean(meta.stock_type),
    speed: clean(meta.speed),
    pathing_power_updated_at: new Date().toISOString()
  };
}

async function patchMovement(id, payload) {
  const body = JSON.stringify(payload);
  return await supa(`station_movements?${eq('id', id)}`, {
    method: 'PATCH',
    headers: { prefer: 'return=representation' },
    body
  });
}

async function main() {
  const dates = datesToProcess();
  console.log(`PATHING POWER INJECTOR V7 METADATA active. dates=${dates.join(',')} dryRun=${DRY_RUN}`);
  let checked = 0, matched = 0, updated = 0, skippedNoPayload = 0;
  const details = [];

  for (const date of dates) {
    const metas = await getMetadata(date);
    console.log(`metadata ${date}: ${metas.length}`);
    for (const meta of metas) {
      checked++;
      const payload = updatePayload(meta);
      const hasUseful = Object.entries(payload).some(([k, v]) => k !== 'pathing_power_updated_at' && clean(v));
      if (!hasUseful) { skippedNoPayload++; continue; }
      const movements = await findMovements(meta);
      matched += movements.length;
      for (const row of movements) {
        if (!DRY_RUN) await patchMovement(row.id, payload);
        updated++;
      }
      details.push({ train_id: meta.train_id, date: meta.running_date, movements: movements.length, label: payload.pathing_power_label });
    }
  }

  console.log(JSON.stringify({ ok: true, checked, matched, updated, skippedNoPayload, details: details.slice(0, 50) }, null, 2));
}

main().catch(err => {
  console.error('Injector v7 failed:', err && err.stack ? err.stack : err);
  process.exit(1);
});
