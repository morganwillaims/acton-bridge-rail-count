/*
  Acton Bridge Public Snapshot Builder v1

  Creates one ready-made JSON snapshot row in public.public_rail_snapshots.
  The Cloudflare Worker can then load this small row quickly instead of doing
  heavy live enrichment every time a visitor opens the public site.

  Required GitHub secrets:
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY

  Optional env:
    SNAPSHOT_DATE=YYYY-MM-DD
*/

const SUPABASE_URL = (process.env.SUPABASE_URL || '').replace(/\/$/, '');
const SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || '';
const STATION_CRS = process.env.STATION_CRS || 'ACB';
const HISTORY_DAYS = 14;
const CACHE_SECONDS = 60;

if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) {
  console.error('Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY.');
  process.exit(1);
}

function ukDateParts(date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit'
  }).formatToParts(date);
  return {
    y: parts.find(p => p.type === 'year')?.value,
    m: parts.find(p => p.type === 'month')?.value,
    d: parts.find(p => p.type === 'day')?.value
  };
}
function todayUkIso() { const p = ukDateParts(); return `${p.y}-${p.m}-${p.d}`; }
function isoDateFromUtcOffsetDays(offsetDays) {
  const d = new Date(Date.now() + offsetDays * 86400000);
  const p = ukDateParts(d); return `${p.y}-${p.m}-${p.d}`;
}
function ukTimeString() {
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Europe/London', hour: '2-digit', minute: '2-digit', second: '2-digit'
  }).format(new Date());
}
function parseRailTimeToMinutes(value) {
  const raw = String(value || '').trim();
  const match = raw.match(/(\d{1,2})[:.]?(\d{2})/);
  if (!match) return null;
  const h = Number(match[1]), m = Number(match[2]);
  if (!Number.isFinite(h) || !Number.isFinite(m) || h > 29 || m > 59) return null;
  return (h % 24) * 60 + m;
}
function cleanText(v) { return String(v || '').trim(); }
function normaliseHeadcode(v) { return cleanText(v).toUpperCase(); }
function displayName(v) {
  const s = cleanText(v);
  const key = s.toUpperCase();
  const map = {
    ACB: 'Acton Bridge', ACBG: 'Acton Bridge', ACTNBDG: 'Acton Bridge',
    LVRPLSH: 'Liverpool Lime Street', LVPLSH: 'Liverpool Lime Street',
    BHAMNWS: 'Birmingham New Street', EUSTON: 'London Euston',
    CRNFSTM: 'Carnforth Steamtown', CRNFSTN: 'Carnforth Steamtown', CARNFST: 'Carnforth Steamtown',
    CREWDHS: 'Crewe H.S. (Dept)', CARLLSL: 'Carlisle LSL (arr)',
    DRBYRTC: 'Derby R.T.C. (Network Rail)',
    MOSEDGB: 'Mossend Down Yard GBRf', MOSSDGB: 'Mossend Down Yard GBRf', MOSSDBG: 'Mossend Down Yard GBRf',
    SOTOMCT: 'Southampton M.C.T.', GRSTNFT: 'Garston F.L.T.', THMSFLI: 'Thamesport F.L.T.',
    FLXSNGB: 'Felixstowe North GBRf', FLXSSGB: 'Felixstowe South Sidings GBRf',
    DITTGBR: "Ditton O'Connor GBRf", DIRFTFL: 'DIRFT F.L.T.', DVTYIRFT: 'Daventry Int Rft Recep Fl',
    BASFHLY: 'Basford Hall Yard (Fl)', BASFHFL: 'Basford Hall Yard (Fl)',
    CARLNY: 'Carlisle N.Y.', ARPLEYS: 'Arpley Sidings',
    WIGANLP: 'Wigan L.I.P.', WIGANLIP: 'Wigan L.I.P.',
    KNOWFT: 'Knowsley Freight Terminal', WLTNEFW: 'Wilton E.F.W. Terminal',
    FOLLYLN: 'Folly Lane (Runcorn) F.L.H.H.', RUNCFLH: 'Runcorn Folly Lane (Flhh)',
    BRNDFHH: 'Brindle Heath R.T.S. (Flhh)', BREDFHH: 'Bredbury R.T.S. (Flhh)',
    CLITGBR: 'Clitheroe Castle Cement GBRf', AVONHGB: 'Avonmouth Hanson Sidings GBRf',
    HMSHBRF: 'Hams Hall GBRf', MOSEGBR: 'Mossend Euroterminal GBRf'
  };
  return map[key] || s || 'Unknown';
}
function isBadRouteName(v) {
  const s = cleanText(v);
  return !s || /^unknown$/i.test(s) || /^route pending$/i.test(s) || /^—$/.test(s) || /^-$/.test(s);
}
function trainTypeFromHeadcode(headcode, storedType) {
  const id = normaliseHeadcode(headcode);
  const stored = String(storedType || '').toLowerCase();
  if (stored.includes('network') || stored.includes('tamper') || stored.includes('special') || stored.includes('mpv') || stored.includes('maintenance')) return 'network_rail';
  if (/^(1Q|3Q|3Z|6U)/.test(id) || /^6J0/.test(id)) return 'network_rail';
  if (/^0/.test(id)) return 'light_engine';
  if (/^5/.test(id)) return 'ecs';
  if (/^[4678]/.test(id)) return 'freight';
  if (/^[129]/.test(id)) return 'passenger';
  if (stored.includes('freight')) return 'freight';
  if (stored.includes('passenger')) return 'passenger';
  if (stored.includes('light')) return 'light_engine';
  if (stored.includes('ecs')) return 'ecs';
  return 'other';
}
function routeIndicatesSpecialTamper(origin, destination, headcode, operator) {
  const id = normaliseHeadcode(headcode);
  const op = cleanText(operator).toUpperCase();
  const route = `${origin || ''} ${destination || ''}`.toUpperCase();
  if (/^1Q\d{2}$/.test(id)) return true;
  if (/^(3Q|3Z|6U)\d{2}$/.test(id)) return true;
  if (/^6J0\d$/.test(id)) return true;
  if (['LS', 'WR', 'TY'].includes(op)) return true;
  return route.includes('NETWORK RAIL') || route.includes('DERBY R.T.C') || route.includes('DRBYRTC');
}
function typeLabel(type) {
  return { passenger:'Passenger', freight:'Freight', light_engine:'Light Engine', network_rail:'Special / Tamper', ecs:'ECS' }[type] || 'Other';
}
function pathingFromRow(r) {
  const raw = [r.pathing_power, r.power_type, r.planned_power, r.traction_type, r.traction_class, r.timing_load, r.operating_characteristics, r.stock_type]
    .map(cleanText).filter(Boolean).join(' ').toUpperCase();
  if (!raw) return { code:'unknown', label:'Pathing unknown', short_label:'Unknown', source:'none', raw:'' };
  if (raw.includes('ELECTRIC') || /(^|\s)E($|\s)/.test(raw) || raw.includes('EMU')) {
    if (raw.includes('DIESEL') || /(^|\s)D($|\s)/.test(raw)) return { code:'dual', label:'Pathed diesel/electric', short_label:'Diesel/electric', source:'movement', raw };
    return { code:'electric', label:'Pathed as electric loco', short_label:'Electric', source:'movement', raw };
  }
  if (raw.includes('DIESEL') || /(^|\s)D($|\s)/.test(raw) || raw.includes('DMU')) return { code:'diesel', label:'Pathed as diesel loco', short_label:'Diesel', source:'movement', raw };
  return { code:'unknown', label:'Pathing unknown', short_label:'Unknown', source:'movement_unclear', raw };
}
async function supabase(path, options = {}) {
  const url = `${SUPABASE_URL}/rest/v1/${path}`;
  const res = await fetch(url, {
    ...options,
    headers: {
      apikey: SUPABASE_SERVICE_ROLE_KEY,
      Authorization: `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`,
      'Content-Type': 'application/json',
      Prefer: 'return=representation',
      ...(options.headers || {})
    }
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${text}`);
  return text ? JSON.parse(text) : null;
}
function routeFallback(headcode, type) {
  const id = normaliseHeadcode(headcode);
  const exact = {
    '1Q30': { origin:'Derby R.T.C. (Network Rail)', destination:'Longsight Car M.D.', toc:'ZZ' },
  };
  if (exact[id]) return exact[id];
  if (/^3Z[56]\d$/.test(id)) return { origin:'Wigan L.I.P.', destination:'Wigan L.I.P.', toc:'ZZ' };
  if (/^1G\d{2}$/.test(id)) return { origin:'Liverpool Lime Street', destination:'Birmingham New Street', toc:'LM' };
  return null;
}
function applyKnownRoute(r) {
  const id = normaliseHeadcode(r.train_id);
  const exact = {
    '4M52': { origin:'Thamesport F.L.T.', destination:'Ditton A.H.F.', toc:'ZZ' },
    '6X77': { origin:'Dagenham Dock Reception GBRf', destination:'Mossend Down Yard GBRf', toc:'ZZ' },
  };
  return exact[id] ? { ...r, ...exact[id], route_enrichment_source:'known_route_override' } : r;
}
function mergeClose(rows) {
  const out = [];
  for (const row of rows) {
    const mins = parseRailTimeToMinutes(row.time);
    const idx = out.findIndex(x => x.train_id === row.train_id && x.type === row.type && mins !== null && parseRailTimeToMinutes(x.time) !== null && Math.abs(mins - parseRailTimeToMinutes(x.time)) <= 3);
    if (idx < 0) out.push(row);
    else {
      const ex = out[idx];
      out[idx] = {
        ...ex,
        platform: ex.platform && ex.platform !== '—' ? ex.platform : row.platform,
        origin: isBadRouteName(ex.origin) ? row.origin : ex.origin,
        destination: isBadRouteName(ex.destination) ? row.destination : ex.destination,
        toc: ex.toc || row.toc,
        loco: ex.loco || row.loco,
        pathing_power: ex.pathing_power !== 'unknown' ? ex.pathing_power : row.pathing_power,
        pathing_power_label: ex.pathing_power !== 'unknown' ? ex.pathing_power_label : row.pathing_power_label,
        pathing_power_short_label: ex.pathing_power !== 'unknown' ? ex.pathing_power_short_label : row.pathing_power_short_label,
        pathing_power_source: ex.pathing_power !== 'unknown' ? ex.pathing_power_source : row.pathing_power_source,
        pathing_power_raw: ex.pathing_power !== 'unknown' ? ex.pathing_power_raw : row.pathing_power_raw,
      };
    }
  }
  return out;
}
function latestByType(rows, type) { return [...rows].reverse().find(r => type === 'ecs_other' ? (r.type === 'ecs' || r.type === 'other') : r.type === type) || null; }
function lastSeenByPlatform(rows) {
  const out = {};
  for (const p of ['1','2','3']) out[p] = [...rows].reverse().find(r => String(r.platform) === p) || null;
  return out;
}
async function main() {
  const date = process.env.SNAPSHOT_DATE || todayUkIso();
  console.log(`Building public snapshot for ${STATION_CRS} ${date}`);
  const columns = [
    'id','running_date','train_id','train_type','origin','destination','toc','planned_time','actual_time','status','source','platform','loco',
    'pathing_power','pathing_power_label','pathing_power_short_label','pathing_power_source','pathing_power_raw',
    'power_type','planned_power','traction_type','traction_class','timing_load','operating_characteristics','stock_type','route_enrichment_source'
  ].join(',');
  const movementPath = `station_movements?select=${columns}&station_crs=eq.${encodeURIComponent(STATION_CRS)}&running_date=eq.${encodeURIComponent(date)}&order=actual_time.asc&limit=2000`;
  const rawRows = await supabase(movementPath);
  let rows = (rawRows || []).map(r => {
    const id = normaliseHeadcode(r.train_id);
    let type = trainTypeFromHeadcode(id, r.train_type);
    let origin = displayName(r.origin);
    let destination = displayName(r.destination);
    const fb = routeFallback(id, type);
    let routeSource = r.route_enrichment_source || 'movement';
    if (isBadRouteName(origin) && fb) { origin = fb.origin; routeSource = 'snapshot_headcode_fallback'; }
    if (isBadRouteName(destination) && fb) { destination = fb.destination; routeSource = 'snapshot_headcode_fallback'; }
    if (routeIndicatesSpecialTamper(origin, destination, id, r.toc)) type = 'network_rail';
    const p = pathingFromRow(r);
    let row = {
      date: r.running_date || date,
      time: r.actual_time || r.planned_time || '--:--',
      planned_time: r.planned_time || '', actual_time: r.actual_time || '',
      type, type_label: typeLabel(type), train_id: id, platform: r.platform || '—',
      origin, destination, toc: r.toc || '', status: r.status || 'Passed', source: r.source || 'Network Rail TRUST',
      loco: r.loco || '',
      pathing_power: r.pathing_power || p.code,
      pathing_power_label: r.pathing_power_label || p.label,
      pathing_power_short_label: r.pathing_power_short_label || p.short_label,
      pathing_power_source: r.pathing_power_source || p.source,
      pathing_power_raw: r.pathing_power_raw || p.raw,
      power_type: r.power_type || '', planned_power: r.planned_power || '', traction_type: r.traction_type || '', traction_class: r.traction_class || '', timing_load: r.timing_load || '', operating_characteristics: r.operating_characteristics || '', stock_type: r.stock_type || '',
      route_enrichment_source: routeSource
    };
    row = applyKnownRoute(row);
    row.type_label = typeLabel(row.type);
    return row;
  }).filter(r => r.train_id).sort((a,b) => (parseRailTimeToMinutes(a.time) ?? 99999) - (parseRailTimeToMinutes(b.time) ?? 99999));
  rows = mergeClose(rows);

  const counts = {
    total: rows.length,
    passenger: rows.filter(r => r.type === 'passenger').length,
    freight: rows.filter(r => r.type === 'freight').length,
    light_engine: rows.filter(r => r.type === 'light_engine').length,
    network_rail: rows.filter(r => r.type === 'network_rail').length,
    ecs_other: rows.filter(r => r.type === 'ecs' || r.type === 'other').length
  };
  const latest = {
    passenger: latestByType(rows, 'passenger'), freight: latestByType(rows, 'freight'), light_engine: latestByType(rows, 'light_engine'), network_rail: latestByType(rows, 'network_rail'), ecs_other: latestByType(rows, 'ecs_other')
  };
  const operatorCounts = {};
  for (const r of rows) operatorCounts[r.toc || 'Unknown'] = (operatorCounts[r.toc || 'Unknown'] || 0) + 1;
  const operators = Object.entries(operatorCounts).map(([toc,count]) => ({ toc, count })).sort((a,b) => b.count - a.count);
  const snapshot = {
    ok:true, station:'Acton Bridge', crs:STATION_CRS, date, today:todayUkIso(), oldest_available_date: isoDateFromUtcOffsetDays(-(HISTORY_DAYS - 1)), history_days:HISTORY_DAYS,
    generated_at: new Date().toISOString(), generated_uk_time: ukTimeString(), cache_seconds:CACHE_SECONDS,
    snapshot_source:'github_public_snapshot_builder_v1', raw_rows_count: rawRows.length, deduped_rows_count: rows.length,
    route_enrichment:{ version:'public_snapshot_builder_v1', order:'stored movement fields -> snapshot safe fallbacks', schedule_status:'snapshot', vstp_status:'snapshot' },
    loco_allocation:{ status:'not applied by snapshot builder v1' },
    counts, latest, next_services:{ passenger:null, freight:null }, next_by_platform:{}, last_seen_by_platform:lastSeenByPlatform(rows), operators, rows
  };
  const body = [{ station_crs: STATION_CRS, snapshot_date: date, generated_at: snapshot.generated_at, updated_at: snapshot.generated_at, snapshot }];
  await supabase('public_rail_snapshots?on_conflict=station_crs,snapshot_date', {
    method: 'POST',
    headers: { Prefer: 'resolution=merge-duplicates,return=representation' },
    body: JSON.stringify(body)
  });
  console.log(`Snapshot saved: ${rows.length} rows (${counts.freight} freight, ${counts.passenger} passenger).`);
}

main().catch(err => { console.error(err); process.exit(1); });
