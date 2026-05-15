/*
  Acton Bridge pathing/power injector
  Matches today's station_movements to schedule_locations/vstp_locations by headcode + closest Acton Bridge time,
  then writes pathing/traction fields back onto station_movements.

  Required GitHub secrets:
  - SUPABASE_URL
  - SUPABASE_SERVICE_ROLE_KEY

  Optional env:
  - ACTON_DATE=YYYY-MM-DD
*/

const SUPABASE_URL = (process.env.SUPABASE_URL || '').replace(/\/$/, '');
const KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_SERVICE_KEY || '';
const CRS = 'ACB';

if (!SUPABASE_URL || !KEY) {
  console.error('Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY');
  process.exit(1);
}

function todayUkIso() {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date());
  const y = parts.find(p => p.type === 'year')?.value;
  const m = parts.find(p => p.type === 'month')?.value;
  const d = parts.find(p => p.type === 'day')?.value;
  return `${y}-${m}-${d}`;
}

const DATE = process.env.ACTON_DATE || todayUkIso();

async function sb(path, opts = {}) {
  const res = await fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    ...opts,
    headers: {
      apikey: KEY,
      authorization: `Bearer ${KEY}`,
      'content-type': 'application/json',
      prefer: opts.prefer || '',
      ...(opts.headers || {})
    }
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${text.slice(0, 500)}`);
  if (!text) return null;
  try { return JSON.parse(text); } catch { return text; }
}

function parseMinutes(value) {
  const raw = String(value || '').trim();
  const m = raw.match(/(\d{1,2})[:.]?(\d{2})/);
  if (!m) return null;
  const h = Number(m[1]);
  const min = Number(m[2]);
  if (!Number.isFinite(h) || !Number.isFinite(min) || min > 59) return null;
  return (h % 24) * 60 + min;
}

function decodePathingPowerValue(value) {
  const raw = String(value || '').trim();
  if (!raw) return null;
  const s = raw.toUpperCase().replace(/[_-]+/g, ' ').trim();
  if (/ELECTRO\s*DIESEL|BI\s*MODE|DUAL|DIESEL\s*ELECTRIC|ELECTRIC\s*DIESEL/.test(s) || /^(ED|DE|B|BMU|BI)$/.test(s)) {
    return { code: 'dual', label: 'Pathed diesel/electric', short_label: 'Diesel/electric' };
  }
  if (/ELECTRIC|EMU|AC\s*LOCO|OHLE|PANTOGRAPH/.test(s) || /^E$/.test(s)) {
    return { code: 'electric', label: 'Pathed as electric loco', short_label: 'Electric' };
  }
  if (/DIESEL|DMU|DIESEL\s*LOCO|LOCO\s*DIESEL/.test(s) || /^D$/.test(s)) {
    return { code: 'diesel', label: 'Pathed as diesel loco', short_label: 'Diesel' };
  }
  return null;
}

const FIELD_KEYS = [
  'pathing_power', 'power_type', 'planned_power', 'traction', 'traction_type', 'traction_class',
  'timing_load', 'operating_characteristics', 'stock_type', 'train_category', 'category'
];
const DETAIL_KEYS = ['power_type','planned_power','traction_type','traction_class','timing_load','operating_characteristics','stock_type'];

function nestedService(row, key) {
  const s = Array.isArray(row?.[key]) ? row[key][0] : row?.[key];
  return s && typeof s === 'object' ? s : {};
}

function extractPathing(...objects) {
  for (const obj of objects) {
    if (!obj || typeof obj !== 'object') continue;
    for (const key of FIELD_KEYS) {
      if (obj[key] == null || obj[key] === '') continue;
      const decoded = decodePathingPowerValue(obj[key]);
      if (decoded) return { ...decoded, source: key, raw: String(obj[key]) };
    }
  }
  return null;
}

function buildItem(row, serviceKey) {
  const service = nestedService(row, serviceKey);
  const headcode = String(row.signalling_id || service.signalling_id || row.train_id || service.train_id || '').toUpperCase().trim();
  if (!headcode) return null;
  const rawTime = row.pass_time || row.departure || row.arrival || service.origin_departure || service.destination_arrival || '';
  const minutes = parseMinutes(rawTime);
  if (minutes == null) return null;
  const p = extractPathing(service, row);
  const details = {};
  for (const k of DETAIL_KEYS) details[k] = row[k] || service[k] || '';
  return { headcode, minutes, row, service, pathing: p, details };
}

function bestMatch(items, headcode, movementMinutes) {
  const matches = items.filter(i => i.headcode === String(headcode || '').toUpperCase().trim() && i.pathing);
  if (!matches.length) return null;
  matches.sort((a, b) => Math.abs(a.minutes - movementMinutes) - Math.abs(b.minutes - movementMinutes));
  const best = matches[0];
  const diff = Math.abs(best.minutes - movementMinutes);
  return diff <= 180 ? best : null;
}

async function main() {
  console.log(`Pathing injector running for ${DATE}`);
  const movements = await sb(`station_movements?select=*&station_crs=eq.${CRS}&running_date=eq.${DATE}&limit=2000`);
  const scheduleRows = await sb(`schedule_locations?select=*,schedule_services(*)&tiploc=in.(ACBG,ACB,ACTNBDG)&schedule_start_date=lte.${DATE}&limit=5000`).catch(err => { console.warn('schedule query failed:', err.message); return []; });
  const vstpRows = await sb(`vstp_locations?select=*,vstp_services(*)&tiploc=in.(ACBG,ACB,ACTNBDG)&limit=5000`).catch(err => { console.warn('vstp query failed:', err.message); return []; });

  const scheduleItems = (scheduleRows || []).map(r => buildItem(r, 'schedule_services')).filter(Boolean);
  const vstpItems = (vstpRows || []).map(r => buildItem(r, 'vstp_services')).filter(Boolean);
  const allItems = [...scheduleItems, ...vstpItems];

  let updated = 0, skipped = 0, noMatch = 0;
  for (const m of movements || []) {
    const headcode = String(m.train_id || '').toUpperCase().trim();
    if (!headcode) { skipped++; continue; }
    if (m.pathing_power && m.pathing_power !== 'unknown') { skipped++; continue; }
    const mins = parseMinutes(m.actual_time || m.planned_time || m.time);
    if (mins == null) { skipped++; continue; }
    const match = bestMatch(allItems, headcode, mins);
    if (!match) { noMatch++; continue; }
    const p = match.pathing;
    const body = {
      pathing_power: p.code,
      pathing_power_label: p.label,
      pathing_power_short_label: p.short_label,
      pathing_power_source: `injector_${p.source}`,
      pathing_power_raw: p.raw,
      pathing_injected_at: new Date().toISOString()
    };
    for (const k of DETAIL_KEYS) if (match.details[k]) body[k] = match.details[k];
    await sb(`station_movements?id=eq.${encodeURIComponent(m.id)}`, { method: 'PATCH', body: JSON.stringify(body), prefer: 'return=minimal' });
    updated++;
    console.log(`updated ${headcode} ${m.actual_time || m.planned_time || ''}: ${p.label} (${p.source}=${p.raw})`);
  }
  console.log(JSON.stringify({ date: DATE, movements: movements?.length || 0, schedule_items: scheduleItems.length, vstp_items: vstpItems.length, updated, skipped, noMatch }, null, 2));
}

main().catch(err => { console.error(err); process.exit(1); });
