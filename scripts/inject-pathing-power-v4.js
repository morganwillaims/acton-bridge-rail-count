/*
  Acton Bridge Pathing Power Injector v4

  Fixes v3 failure:
    public.information_schema.columns is not available through Supabase REST.

  This version does NOT query information_schema and does NOT assume station_movements.date exists.
  It uses the known working station_movements columns from Snapshot Builder v3:
    id, running_date, station_crs, train_id, train_type, origin, destination, toc,
    planned_time, actual_time, status, source, platform, route_enrichment_source

  It tries several safe schedule/VSTP column sets. If your schedule tables do not yet store
  power/timing-load fields, it logs that and leaves movements unchanged.
*/

const SUPABASE_URL = (process.env.SUPABASE_URL || '').replace(/\/$/, '');
const SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || '';
const STATION_CRS = process.env.STATION_CRS || 'ACB';
const TARGET_DATE = process.env.TARGET_DATE || todayUkIso();

console.log('PATHING POWER INJECTOR V4 NO-INFORMATION-SCHEMA ACTIVE for', TARGET_DATE);

if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) {
  console.error('Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY');
  process.exit(1);
}

function todayUkIso() {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit'
  }).formatToParts(new Date());
  const y = parts.find(p => p.type === 'year')?.value;
  const m = parts.find(p => p.type === 'month')?.value;
  const d = parts.find(p => p.type === 'day')?.value;
  return `${y}-${m}-${d}`;
}

async function supabase(path, options = {}) {
  const url = `${SUPABASE_URL}/rest/v1/${path}`;
  const res = await fetch(url, {
    ...options,
    headers: {
      apikey: SUPABASE_SERVICE_ROLE_KEY,
      Authorization: `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`,
      'Content-Type': 'application/json',
      Prefer: options.prefer || 'return=representation',
      ...(options.headers || {})
    }
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${text}`);
  return text ? JSON.parse(text) : null;
}

function clean(v) { return String(v || '').trim(); }
function head(v) { return clean(v).toUpperCase(); }
function timeToMinutes(v) {
  const s = clean(v);
  const m = s.match(/(\d{1,2})[:.]?(\d{2})/);
  if (!m) return null;
  const hh = Number(m[1]) % 24;
  const mm = Number(m[2]);
  if (!Number.isFinite(hh) || !Number.isFinite(mm) || mm > 59) return null;
  return hh * 60 + mm;
}
function movementMinutes(row) { return timeToMinutes(row.actual_time || row.planned_time || row.time); }
function scheduleMinutes(row) {
  return timeToMinutes(row.public_time || row.planned_time || row.wtt_time || row.gbtt_ptd || row.departure_time || row.arrival_time || row.pass_time || row.time || row.schedule_time);
}
function scheduleHeadcode(row) {
  return head(row.headcode || row.identity || row.train_id || row.train_identity || row.service_id || row.train_uid || row.uid);
}

function decodePower(raw) {
  const joined = Object.values(raw).filter(Boolean).join(' ').toUpperCase();
  if (!joined.trim()) return { power: 'unknown', label: 'Pathing unknown', short: 'Unknown' };
  const hasElectric = /ELECTRIC|\bELEC\b|\bAC\b|ELECTR/.test(joined);
  const hasDiesel = /DIESEL|\bDSL\b|\bDIE\b/.test(joined);
  if (hasElectric && hasDiesel) return { power: 'dual', label: 'Pathed diesel/electric', short: 'Diesel/electric' };
  if (hasElectric) return { power: 'electric', label: 'Pathed as electric loco', short: 'Electric' };
  if (hasDiesel) return { power: 'diesel', label: 'Pathed as diesel loco', short: 'Diesel' };
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

async function fetchMovements() {
  const columnSets = [
    'id,running_date,station_crs,train_id,train_type,origin,destination,toc,planned_time,actual_time,status,source,platform,route_enrichment_source,pathing_power',
    'id,running_date,station_crs,train_id,train_type,origin,destination,toc,planned_time,actual_time,status,source,platform,route_enrichment_source',
    'id,running_date,station_crs,train_id,origin,destination,toc,planned_time,actual_time,status,source,platform'
  ];
  let lastErr = null;
  for (const columns of columnSets) {
    try {
      const path = `station_movements?select=${encodeURIComponent(columns)}&station_crs=eq.${encodeURIComponent(STATION_CRS)}&running_date=eq.${encodeURIComponent(TARGET_DATE)}&order=actual_time.asc&limit=1000`;
      const rows = await supabase(path);
      console.log('Fetched movement rows:', rows?.length || 0, 'using columns:', columns);
      return rows || [];
    } catch (err) {
      lastErr = err;
      console.warn('Movement column set failed, trying fallback:', err.message);
    }
  }
  throw lastErr || new Error('Could not fetch station_movements');
}

async function fetchScheduleTable(table) {
  const columnSets = [
    'id,train_uid,uid,headcode,identity,train_id,train_identity,service_id,stp_indicator,schedule_start_date,schedule_end_date,service_date,running_date,origin,destination,power_type,power,cif_power_type,planned_power,pathing_power,traction_type,traction,traction_class,loco_class,timing_load,cif_timing_load,load,operating_characteristics,op_chars,stock_type,rolling_stock,public_time,planned_time,wtt_time,gbtt_ptd,departure_time,arrival_time,pass_time,time,schedule_time',
    'id,train_uid,uid,headcode,identity,train_id,train_identity,service_id,stp_indicator,schedule_start_date,schedule_end_date,origin,destination,power_type,planned_power,traction_type,traction_class,timing_load,operating_characteristics,stock_type,public_time,planned_time,wtt_time,departure_time,arrival_time,pass_time,time',
    'id,train_uid,uid,headcode,identity,train_id,train_identity,service_id,origin,destination,timing_load,public_time,planned_time,wtt_time,departure_time,arrival_time,pass_time,time',
    'id,train_uid,uid,headcode,identity,train_id,train_identity,service_id,origin,destination,public_time,planned_time,wtt_time,time'
  ];
  let lastErr = null;
  for (const columns of columnSets) {
    try {
      let path = `${table}?select=${encodeURIComponent(columns)}&limit=3000`;
      // Avoid date filters because table date column names vary. Limit keeps query bounded.
      const rows = await supabase(path);
      console.log(`Fetched ${rows?.length || 0} from ${table} using columns:`, columns);
      return (rows || []).map(r => ({ ...r, __table: table }));
    } catch (err) {
      lastErr = err;
      console.warn(`${table} column set failed, trying fallback:`, err.message);
    }
  }
  console.warn(`Skipping ${table}; no compatible select worked. Last error:`, lastErr?.message || 'unknown');
  return [];
}
async function fetchSchedules() {
  const a = await fetchScheduleTable('schedule_services');
  const b = await fetchScheduleTable('vstp_services');
  const all = [...a, ...b];
  console.log('Total schedule/VSTP candidate rows:', all.length);
  return all;
}

function findBestSchedule(m, schedules) {
  const h = head(m.train_id || m.headcode || m.identity);
  if (!h) return null;
  const mt = movementMinutes(m);
  const candidates = schedules.filter(s => scheduleHeadcode(s) === h || head(s.train_uid || s.uid) === h);
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
  await supabase(`station_movements?id=eq.${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
    headers: { Prefer: 'return=minimal' }
  });
}

async function main() {
  const movements = await fetchMovements();
  const schedules = await fetchSchedules();
  let updated = 0, noMatch = 0, noPower = 0;

  for (const m of movements) {
    if (!m.id) continue;
    const best = findBestSchedule(m, schedules);
    if (!best) { noMatch++; continue; }
    const p = extractPathing(best);
    if (p.power === 'unknown') { noPower++; continue; }
    await patchMovement(m.id, {
      pathing_power: p.power,
      pathing_power_label: p.label,
      pathing_power_source: `injector_v4:${best.__table}`,
      power_type: p.raw.power_type || null,
      planned_power: p.raw.planned_power || null,
      traction_type: p.raw.traction_type || null,
      traction_class: p.raw.traction_class || null,
      timing_load: p.raw.timing_load || null,
      operating_characteristics: p.raw.operating_characteristics || null,
      stock_type: p.raw.stock_type || null,
      pathing_power_updated_at: new Date().toISOString()
    });
    updated++;
  }

  console.log(`PATHING POWER INJECTOR V4 COMPLETE updated=${updated} no_match=${noMatch} no_power_data=${noPower}`);
  if (updated === 0) {
    console.log('No rows updated. Most likely schedule_services/vstp_services do not yet store power/timing-load fields. Next patch should update the Schedule Loader/VSTP Collector to save CIF power/timing-load data.');
  }
}

main().catch(err => {
  console.error('Error:', err.message || err);
  process.exit(1);
});
