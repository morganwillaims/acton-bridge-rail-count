// Diagnostic: checks whether schedule/vstp tables contain pathing fields.
const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;
if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) throw new Error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY");

async function get(table) {
  const url = `${SUPABASE_URL}/rest/v1/${table}?select=power_type,planned_power,traction_type,traction_class,timing_load,operating_characteristics,stock_type,speed,pathing_power,pathing_power_label&limit=10`;
  const res = await fetch(url, { headers: { apikey: SUPABASE_SERVICE_ROLE_KEY, Authorization: `Bearer ${SUPABASE_SERVICE_ROLE_KEY}` } });
  const text = await res.text();
  console.log(`\n${table}: HTTP ${res.status}`);
  console.log(text.slice(0, 2000));
}

(async () => {
  console.log("CHECK SCHEDULE/VSTP PATHING FIELDS ACTIVE");
  await get("schedule_services");
  await get("vstp_services");
})();
