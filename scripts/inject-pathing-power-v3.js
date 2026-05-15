/*
  Acton Bridge Pathing Power Injector v3
  Safe version: does NOT assume station_movements.date exists.
  It discovers available columns through information_schema, then uses the safest date/time filters it can.
*/

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;
const STATION = process.env.STATION || 'ACB';
const TARGET_DATE = process.env.TARGET_DATE || todayUkIso();

console.log('PATHING POWER INJECTOR V3 NO-DATE-COLUMN ACTIVE for', TARGET_DATE);

if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) {
  throw new Error('Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY');
}

function todayUkIso() {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit'
  }).formatToParts(new Date());
  const y = parts.find(p => p.type === 'year').value;
  const m = parts.find(p => p.type === 'month').value;
  const d = parts.find(p => p.type === 'day').value;
  return `${y}-${m}-${d}`;
}

async function request(path, opts = {}) {
  const res = await fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    ...opts,
    headers: {
      apikey: SUPABASE_SERVICE_ROLE_KEY,
      authorization: `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`,
      'content-type': 'application/json',
      prefer: opts.prefer || '',
      ...(opts.headers || {})
    }
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${text}`);
  if (!text) return null;
  try { return JSON.parse(text); } catch { return text; }
}

async function columnsFor(table) {
  const rows = await request(`information_schema.columns?table_schema=eq.public&table_name=eq.${encodeURIComponent(table)}&select=column_name`);
  return new Set((rows || []).map(r => r.column_name));
}

function pick(cols, names) {
  for (const n of names) if (cols.has(n)) return n;
  return null;
}

function buildSelect(cols, baseNames) {
  return baseNames.filter(c => cols.has(c)).join(',');
}

function normaliseHeadcode(row) {
  return String(row.train_id || row.headcode || row.identity || row.train_identity || '').trim().toUpperCase();
}

function timeToMinutes(v) {
  const s = String(v || '').trim();
  const m = s.match(/(\d{1,2})[:.]?(\d{2})/);
  if (!m) return null;
  const hh = Number(m[1]) % 24;
  const mm = Number(m[2]);
  if (!Number.isFinite(hh) || !Number.isFinite(mm) || mm > 59) return null;
  return hh * 60 + mm;
}

function getMovementMinutes(row) {
  return timeToMinutes(row.actual_time || row.planned_time || row.time || row.public_time || row.pass_time || row.wtt_time || row.created_at || row.updated_at);
}

function decodePower(rawFields) {
  const joined = Object.values(rawFields).filter(Boolean).join(' ').toUpperCase();
  if (!joined.trim()) return { power: 'unknown', label: 'Pathing unknown', short: 'Unknown' };

  if (/ELECTRIC/.test(joined) || /\bELEC\b/.test(joined) || /\bAC\b/.test(joined) || /\bELECTR/.test(joined)) {
    if (/DIESEL/.test(joined)) return { power: 'dual', label: 'Pathed diesel/electric', short: 'Diesel/electric' };
    return { power: 'electric', label: 'Pathed as electric loco', short: 'Electric' };
  }
  if (/DIESEL/.test(joined) || /\bDSL\b/.test(joined) || /\bDIE\b/.test(joined)) {
    return { power: 'diesel', label: 'Pathed as diesel loco', short: 'Diesel' };
  }
  // CIF timing load examples can sometimes contain class/type hints but are not enough to infer safely.
  return { power: 'unknown', label: 'Pathing unknown', short: 'Unknown' };
}

function extractPathing(s) {
  const raw = {
    power_type: s.power_type || s.power || s.cif_power_type || '',
    planned_power: s.planned_power || s.pathing_power || '',
    traction_type: s.traction_type || s.traction || '',
    traction_class: s.traction_class || s.loco_class || '',
    timing_load: s.timing_load || s.cif_timing_load || s.load || '',
    operating_characteristics: s.operating_characteristics || s.op_chars || '',
    stock_type: s.stock_type || s.rolling_stock || ''
  };
  const decoded = decodePower(raw);
  return { ...decoded, raw };
}

function scheduleHeadcode(s) {
  return String(s.headcode || s.identity || s.train_id || s.train_identity || s.service_id || '').trim().toUpperCase();
}

function scheduleMinutes(s) {
  return timeToMinutes(s.public_time || s.planned_time || s.wtt_time || s.gbtt_ptd || s.departure_time || s.arrival_time || s.pass_time || s.time || s.schedule_time);
}

async function getMovements() {
  const cols = await columnsFor('station_movements');
  console.log('station_movements columns include:', Array.from(cols).slice(0,60).join(', '));

  const selectCols = buildSelect(cols, [
    'id','station','crs','location','train_id','headcode','identity','train_identity','type','type_label',
    'planned_time','actual_time','time','public_time','pass_time','wtt_time','platform','origin','destination','toc','source','status','created_at','updated_at',
    'pathing_power','pathing_power_label','pathing_power_source'
  ]);
  if (!selectCols) throw new Error('No usable station_movements columns found');

  let url = `station_movements?select=${encodeURIComponent(selectCols)}&limit=500&order=${encodeURIComponent((cols.has('actual_time')?'actual_time':(cols.has('created_at')?'created_at':'id'))+'.desc')}`;

  const stationCol = pick(cols, ['station','crs','location']);
  if (stationCol) url += `&${stationCol}=eq.${encodeURIComponent(STATION)}`;

  // Filter by date only if a date-like column exists. Otherwise pull recent rows and filter in JS where possible.
  const dateCol = pick(cols, ['date','movement_date','service_date','running_date','created_date']);
  if (dateCol) url += `&${dateCol}=eq.${encodeURIComponent(TARGET_DATE)}`;

  const rows = await request(url);
  return { rows: rows || [], cols };
}

async function getSchedules() {
  const candidates = ['schedule_services','vstp_services'];
  const all = [];
  for (const table of candidates) {
    let cols;
    try { cols = await columnsFor(table); } catch (e) { console.log(`Skipping ${table}:`, e.message); continue; }
    if (!cols.size) continue;
    const selectCols = buildSelect(cols, [
      'id','train_uid','uid','headcode','identity','train_id','train_identity','service_id','stp_indicator',
      'schedule_start_date','schedule_end_date','date','service_date','running_date','days_runs',
      'origin','destination','power_type','power','cif_power_type','planned_power','pathing_power','traction_type','traction','traction_class','loco_class',
      'timing_load','cif_timing_load','load','operating_characteristics','op_chars','stock_type','rolling_stock',
      'public_time','planned_time','wtt_time','gbtt_ptd','departure_time','arrival_time','pass_time','time','schedule_time'
    ]);
    if (!selectCols) continue;
    let url = `${table}?select=${encodeURIComponent(selectCols)}&limit=1000`;
    const dateCol = pick(cols, ['date','service_date','running_date']);
    if (dateCol) url += `&${dateCol}=eq.${encodeURIComponent(TARGET_DATE)}`;
    const rows = await request(url);
    for (const r of rows || []) all.push({ ...r, __table: table });
  }
  console.log('Loaded schedule/VSTP candidate rows:', all.length);
  return all;
}

function findBestSchedule(m, schedules) {
  const h = normaliseHeadcode(m);
  if (!h) return null;
  const mt = getMovementMinutes(m);
  const candidates = schedules.filter(s => scheduleHeadcode(s) === h || String(s.train_uid || s.uid || '').toUpperCase() === h);
  if (!candidates.length) return null;
  let best = null;
  let bestGap = Infinity;
  for (const s of candidates) {
    const st = scheduleMinutes(s);
    const gap = (mt != null && st != null) ? Math.abs(mt - st) : 9999;
    if (gap < bestGap) { best = s; bestGap = gap; }
  }
  return best;
}

async function patchMovement(id, patch) {
  if (!id) return;
  await request(`station_movements?id=eq.${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
    prefer: 'return=minimal'
  });
}

async function main() {
  const { rows: movements } = await getMovements();
  console.log('Loaded movements:', movements.length);
  const schedules = await getSchedules();

  let updated = 0, unknown = 0, skippedNoId = 0;
  for (const m of movements) {
    if (!m.id) { skippedNoId++; continue; }
    const best = findBestSchedule(m, schedules);
    if (!best) { unknown++; continue; }
    const p = extractPathing(best);
    if (p.power === 'unknown') { unknown++; continue; }
    const patch = {
      pathing_power: p.power,
      pathing_power_label: p.label,
      pathing_power_source: `injector_v3:${best.__table}`,
      power_type: p.raw.power_type || null,
      planned_power: p.raw.planned_power || null,
      traction_type: p.raw.traction_type || null,
      traction_class: p.raw.traction_class || null,
      timing_load: p.raw.timing_load || null,
      operating_characteristics: p.raw.operating_characteristics || null,
      stock_type: p.raw.stock_type || null,
      pathing_power_updated_at: new Date().toISOString()
    };
    await patchMovement(m.id, patch);
    updated++;
  }

  console.log(`Pathing injector v3 complete. updated=${updated} unknown_or_no_match=${unknown} skipped_no_id=${skippedNoId}`);
}

main().catch(err => {
  console.error('Error:', err.message || err);
  process.exit(1);
});
